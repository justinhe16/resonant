"""PER-297: several Resonant instances on one macOS user.

Every name an instance derives (home, launchd label, Keychain service, run dirs), the CLI's
targeting (``--instance``, ``RESONANT_INSTANCE``, ``RESONANT_HOME``), the API port check and
where the instance shows. No real launchctl or Keychain call: launchd and the secret store are
faked, and HOME is a temp dir (conftest), so ``~/.resonant-cori`` is never the owner's.
"""

from __future__ import annotations

import asyncio
import itertools
import plistlib
import socket
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from resonant import cli, instance, launchd
from resonant.config import Settings, load_settings
from resonant.daemon import started_line
from resonant.ops import is_ops_label, kickstart_argv, restart_later_argv
from resonant.ops import restart_later as restart_later_mod
from resonant.router.fast_path import render_status
from resonant.sandbox import SandboxParams
from resonant.sandbox.profile import render
from resonant.secrets import (
    SECURITY,
    SET_ARGV,
    FakeKeychain,
    SecretStore,
    SecurityResult,
    add_command,
    delete_argv,
    find_argv,
)
from resonant.status import collect_status
from resonant.store.db import open_db

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def no_home_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pick the home from the instance, as on the Mini (conftest sets RESONANT_HOME)."""
    monkeypatch.delenv("RESONANT_HOME", raising=False)


@pytest.fixture
def cori_home(user_home: Path) -> Path:
    home = user_home / ".resonant-cori"
    home.mkdir()
    return home


def write_cori_config(home: Path, extra: str = "") -> None:
    (home / "config.yaml").write_text("instance:\n  name: cori\napi:\n  port: 7778\n" + extra)


# --- derived names ------------------------------------------------------------------------


def test_default_instance_is_byte_for_byte_today(user_home: Path, no_home_env: None) -> None:
    """No ``instance:`` block and no RESONANT_INSTANCE: every name is what it was before
    instances existed."""
    s = load_settings()
    assert s.instance.name == "resonant" and s.instance.display_name == "Resonant"
    assert s.home == user_home / ".resonant"
    assert s.daemon_label == "com.resonant.daemon" == launchd.DAEMON_LABEL
    assert s.keychain_service == "resonant"
    assert s.secrets.service is None
    assert s.api.port == 7777
    assert s.runner.work_dir == (user_home / "resonant-runs").resolve()
    assert s.runner.code_dir == (user_home / "code").resolve()
    assert s.runner.cache_dir == (user_home / "resonant-runs" / ".cache").resolve()
    assert s.runner.mirror_dir == user_home / ".resonant" / "runner" / "mirrors"
    assert s.runner.secrets_dir == user_home / ".resonant" / "run-secrets"
    line = started_line(s, imessage=True, relay=False, extensions=2, jobs=9)
    assert line == (
        "resonant daemon started (api 127.0.0.1:7777, gate=policy dry_run=True, "
        "imessage=on, relay=off, extensions=2, jobs=9)"
    )
    plist = launchd.daemon_plist(s, python="/py")
    assert plist["Label"] == "com.resonant.daemon"
    assert plist["EnvironmentVariables"] == {
        "RESONANT_HOME": str(user_home / ".resonant"),
        "PATH": "/:/usr/bin:/bin:/usr/sbin:/sbin",
    }


def test_hand_set_resonant_home_is_unchanged(resonant_home: Path) -> None:
    """An existing personal install with a hand-set RESONANT_HOME doesn't move."""
    s = load_settings()
    assert s.home == resonant_home
    assert (s.instance.name, s.daemon_label, s.keychain_service) == (
        "resonant",
        "com.resonant.daemon",
        "resonant",
    )


def test_derived_names() -> None:
    assert instance.home_for("cori") == Path("~/.resonant-cori").expanduser()
    assert instance.home_for("resonant") == Path("~/.resonant").expanduser()
    assert instance.daemon_label("cori") == "com.resonant.cori.daemon"
    assert instance.keychain_service("cori") == "resonant-cori"
    dirs = instance.run_dirs("cori")
    assert (dirs.work_dir, dirs.code_dir, dirs.cache_dir) == (
        Path("~/cori/runs"),
        Path("~/cori/code"),
        Path("~/cori/runs/.cache"),
    )
    assert instance.run_dirs("resonant") == instance.RunDirs(
        Path("~/resonant-runs"), Path("~/code"), Path("~/resonant-runs/.cache")
    )
    assert instance.service_owner("resonant") == "resonant"
    assert instance.service_owner("resonant-cori") == "cori"
    assert instance.service_owner("cori-keys") is None


@pytest.mark.parametrize("bad", ["Cori", "1cori", "co.ri", "co/ri", "", "a" * 33, "co ri"])
def test_a_name_that_isnt_a_slug_is_refused(bad: str, resonant_home: Path) -> None:
    with pytest.raises(instance.InstanceError):
        instance.validate_name(bad)
    (resonant_home / "config.yaml").write_text(f"instance:\n  name: {bad!r}\n")
    with pytest.raises(ValidationError):
        load_settings()


def test_cori_settings(
    user_home: Path, cori_home: Path, no_home_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_cori_config(cori_home)
    monkeypatch.setenv("RESONANT_INSTANCE", "cori")
    s = load_settings()
    assert s.home == cori_home
    assert (s.instance.name, s.instance.display_name) == ("cori", "Cori")
    assert s.daemon_label == "com.resonant.cori.daemon"
    assert s.keychain_service == "resonant-cori"
    assert s.api.port == 7778
    assert s.runner.work_dir == (user_home / "cori" / "runs").resolve()
    assert s.runner.code_dir == (user_home / "cori" / "code").resolve()
    assert s.runner.cache_dir == (user_home / "cori" / "runs" / ".cache").resolve()
    assert s.runner.mirror_dir == cori_home / "runner" / "mirrors"
    line = started_line(s, imessage=False, relay=False, extensions=0, jobs=1)
    assert line.startswith("resonant daemon started (instance cori (Cori), api 127.0.0.1:7778,")


def test_set_runner_dirs_win(
    user_home: Path, cori_home: Path, no_home_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_cori_config(cori_home, f"runner:\n  code_dir: {user_home / 'dori'}\n")
    monkeypatch.setenv("RESONANT_INSTANCE", "cori")
    s = load_settings()
    assert s.runner.code_dir == (user_home / "dori").resolve()
    assert s.runner.work_dir == (user_home / "cori" / "runs").resolve()  # still derived


def test_cori_example_config_loads(
    cori_home: Path, no_home_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    (cori_home / "config.yaml").write_text((REPO / "config" / "cori.example.yaml").read_text())
    monkeypatch.setenv("RESONANT_INSTANCE", "cori")
    s = load_settings()
    assert (s.instance.name, s.instance.display_name, s.api.port) == ("cori", "Cori", 7778)
    assert s.channels.imessage.enabled is False
    assert s.claude.max_concurrent == 5
    assert s.dry_run is False
    assert s.keychain_service == "resonant-cori"


# --- run dirs never overlap ---------------------------------------------------------------


def _expanded(name: str) -> list[Path]:
    return [p.expanduser().resolve() for _, p in instance.run_dirs(name).items()]


def test_two_instances_derived_run_dirs_never_overlap(user_home: Path) -> None:
    names = ["resonant", "cori", "dori", "cori2", "a", "resonant-x", "runs"]
    for a, b in itertools.combinations(names, 2):
        for pa in _expanded(a):
            for pb in _expanded(b):
                assert not instance.overlaps(pa, pb), (a, pa, b, pb)


@pytest.mark.parametrize("name", ["code", "resonant-runs"])
def test_a_name_whose_dirs_land_in_the_defaults_is_refused(name: str, resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(f"instance:\n  name: {name}\n")
    with pytest.raises(ValidationError, match="overlaps the resonant instance's"):
        load_settings()


@pytest.mark.parametrize(
    ("key", "path"),
    [
        ("code_dir", "code/cori"),
        ("work_dir", "resonant-runs/cori"),
        ("cache_dir", "resonant-runs/.cache"),
        ("work_dir", ".resonant/cori-runs"),
    ],
)
def test_a_set_path_overlapping_the_default_instance_is_refused(
    key: str, path: str, user_home: Path, resonant_home: Path
) -> None:
    (resonant_home / "config.yaml").write_text(
        f"instance:\n  name: cori\nrunner:\n  {key}: {user_home / path}\n"
    )
    with pytest.raises(ValidationError, match=f"runner.{key} .* overlaps the resonant instance"):
        load_settings()


# --- Keychain service ---------------------------------------------------------------------


def test_secrets_argv_with_a_custom_service() -> None:
    assert find_argv("runner/claude_token", value=True, service="resonant-cori") == [
        SECURITY, "find-generic-password", "-s", "resonant-cori", "-a", "runner/claude_token",
        "-w",
    ]  # fmt: skip
    assert delete_argv("relay/supabase_key", service="resonant-cori") == [
        SECURITY, "delete-generic-password", "-s", "resonant-cori", "-a", "relay/supabase_key",
    ]  # fmt: skip
    line = add_command("runner/github_app_key", "value-12345", service="resonant-cori")
    assert line.startswith(b"add-generic-password -U -s resonant-cori -a runner/github_app_key ")
    # the default is the personal service, exactly as before
    assert find_argv("github/token", value=False)[3] == "resonant"
    with pytest.raises(ValueError, match="keychain service"):
        find_argv("github/token", value=False, service="bad service")


def test_secret_store_uses_its_service() -> None:
    keychain = FakeKeychain(service="resonant-cori")
    store = SecretStore(keychain, service="resonant-cori")

    async def go() -> str:
        await store.set("runner/claude_token", "token-value-123")
        return await store.get("runner/claude_token")

    assert asyncio.run(go()) == "token-value-123"
    assert keychain.calls[0].argv == tuple(SET_ARGV)
    assert all(
        "resonant-cori" in c.argv or b"-s resonant-cori " in (c.stdin or b"")
        for c in keychain.calls
    )


@dataclass
class TwoServiceKeychain:
    """A fake login keychain shared by two instances: items keyed by (service, account)."""

    items: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])
    argvs: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])

    async def run(
        self, argv: Sequence[str], *, stdin: bytes | None, timeout_s: float
    ) -> SecurityResult:
        args = tuple(argv)
        self.argvs.append(args)
        opts = {a: args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-s", "-a")}
        assert args[1] == "find-generic-password", args
        found = (opts["-s"], opts["-a"]) in self.items
        return SecurityResult(0 if found else 44, b"")


def _secret_list(
    monkeypatch: pytest.MonkeyPatch, keychain: TwoServiceKeychain, args: list[str]
) -> str:
    real = cli._secret_store  # pyright: ignore[reportPrivateUsage]

    def store() -> SecretStore:
        s = real()
        return SecretStore(keychain, service=s.service)

    monkeypatch.setattr(cli, "_secret_store", store)
    result = CliRunner().invoke(
        cli.app, [*args, "secret", "list", str(REPO / "extensions" / "github")]
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_secret_list_shows_only_its_own_instance(
    cori_home: Path, user_home: Path, no_home_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_cori_config(cori_home)
    keychain = TwoServiceKeychain(items={("resonant", "github/token"): "x"})
    personal = _secret_list(monkeypatch, keychain, [])
    assert "github/token" in personal and "present" in personal
    assert "1 of 1 present (service resonant)" in personal
    cori = _secret_list(monkeypatch, keychain, ["--instance", "cori"])
    assert "missing" in cori
    assert "0 of 1 present (service resonant-cori)" in cori
    services = {a[a.index("-s") + 1] for a in keychain.argvs}
    assert services == {"resonant", "resonant-cori"}


def test_a_changed_service_says_which(
    cori_home: Path, no_home_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """secrets.service changed after secrets were stored: the list says which service it
    asked, so "0 of N" explains itself."""
    write_cori_config(cori_home, "secrets:\n  service: cori-keys\n")
    keychain = TwoServiceKeychain(items={("resonant-cori", "github/token"): "x"})
    out = _secret_list(monkeypatch, keychain, ["--instance", "cori"])
    assert "0 of 1 present (service cori-keys)" in out


@pytest.mark.parametrize("stated", ["resonant", "resonant-dori"])
def test_a_stated_service_must_stay_unique(stated: str, resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(
        f"instance:\n  name: cori\nsecrets:\n  service: {stated}\n"
    )
    with pytest.raises(ValidationError, match="Keychain service"):
        load_settings()


def test_a_stated_service_may_be_the_instances_own(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(
        "instance:\n  name: cori\nsecrets:\n  service: resonant-cori\n"
    )
    assert load_settings().keychain_service == "resonant-cori"
    (resonant_home / "config.yaml").write_text("secrets:\n  service: resonant-cori\n")
    with pytest.raises(ValidationError, match="Keychain service"):
        load_settings()  # the personal instance can't take cori's


# --- launchd ------------------------------------------------------------------------------


def test_cori_plist(cori_home: Path, no_home_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    write_cori_config(cori_home)
    monkeypatch.setenv("RESONANT_INSTANCE", "cori")
    plist = launchd.daemon_plist(load_settings(), python="/venv/bin/python")
    assert plist["Label"] == "com.resonant.cori.daemon"
    assert plist["EnvironmentVariables"] == {
        "RESONANT_HOME": str(cori_home),
        "RESONANT_INSTANCE": "cori",
        "PATH": "/venv/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    assert plist["StandardErrorPath"] == str(cori_home / "logs" / "daemon.err.log")
    assert launchd.plist_path(plist["Label"]).name == "com.resonant.cori.daemon.plist"
    plistlib.dumps(plist)


def test_ops_labels_cover_every_instance_daemon() -> None:
    assert is_ops_label("com.resonant.daemon")
    assert is_ops_label("com.resonant.cori.daemon")
    assert is_ops_label("com.resonant.ollama")
    for bad in ("com.resonant.caffeinate", "com.resonant.resonant.daemon", "com.evil.daemon",
                "com.resonant.Cori.daemon", "com.resonant.cori.daemon.x"):  # fmt: skip
        assert not is_ops_label(bad), bad
        with pytest.raises(ValueError, match="not an ops label"):
            kickstart_argv(bad)
    assert kickstart_argv("com.resonant.cori.daemon", uid=501)[-1] == (
        "gui/501/com.resonant.cori.daemon"
    )
    assert restart_later_argv("com.resonant.cori.daemon")[-2] == "com.resonant.cori.daemon"
    calls: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> Any:
        calls.append(argv)
        return type("P", (), {"returncode": 0})()

    assert (
        restart_later_mod.main(["com.resonant.cori.daemon", "0"], run=run, sleep=lambda _: None)
        == 0
    )
    assert restart_later_mod.main(["com.evil.daemon", "0"], run=run, sleep=lambda _: None) == 2
    assert len(calls) == 1


def test_restart_resonant_uses_the_instances_label(resonant_home: Path) -> None:
    from resonant.ops.fakes import FakeOpsRunner
    from resonant.tools.ops import Ops

    (resonant_home / "config.yaml").write_text("instance:\n  name: cori\n")
    settings = load_settings()
    conn = open_db(settings.db_path)
    runner = FakeOpsRunner()
    ops = Ops(conn, settings, runner, owner="owner", clock=lambda: datetime.now(UTC))
    result = asyncio.run(ops.restart_resonant())
    assert result["label"] == "com.resonant.cori.daemon"
    (spawned,) = runner.spawned
    assert "com.resonant.cori.daemon" in spawned


# --- the API port -------------------------------------------------------------------------


@pytest.fixture
def bound_port() -> Iterator[int]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        yield s.getsockname()[1]


def test_port_in_use(bound_port: int) -> None:
    assert instance.port_in_use("127.0.0.1", bound_port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert not instance.port_in_use("127.0.0.1", free)


def _no_launchctl(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    installed: list[str] = []

    def install(plist: dict[str, Any]) -> Path:
        installed.append(plist["Label"])
        return Path("/nowhere") / f"{plist['Label']}.plist"

    monkeypatch.setattr(launchd, "install", install)
    monkeypatch.setattr(launchd, "uninstall", lambda label: False)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(launchd, "find_ollama", lambda: None)
    return installed


def test_install_refuses_a_bound_port(
    bound_port: int, resonant_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = _no_launchctl(monkeypatch)
    monkeypatch.setattr(cli, "_fetch_status", lambda settings: None)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    (resonant_home / "config.yaml").write_text(f"api:\n  port: {bound_port}\n")
    result = CliRunner().invoke(cli.app, ["daemon", "install"])
    assert result.exit_code == 1
    assert f"api.port {bound_port} on 127.0.0.1 is already in use" in result.output
    assert installed == []


def test_install_names_the_instance_on_the_port(
    bound_port: int, resonant_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = _no_launchctl(monkeypatch)
    live = {"instance": {"name": "resonant", "display_name": "Resonant"}}
    monkeypatch.setattr(cli, "_fetch_status", lambda settings: live)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    (resonant_home / "config.yaml").write_text(
        f"instance:\n  name: cori\napi:\n  port: {bound_port}\n"
    )
    result = CliRunner().invoke(cli.app, ["daemon", "install", "--no-ollama"])
    assert result.exit_code == 1
    assert f"api.port {bound_port} on 127.0.0.1 is in use by instance resonant" in result.output
    assert installed == []


def test_install_over_its_own_running_daemon_is_a_reinstall(
    bound_port: int, resonant_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = _no_launchctl(monkeypatch)
    # a daemon from before instances answers without a name: it is the personal one
    monkeypatch.setattr(cli, "_fetch_status", lambda settings: {"version": "x"})  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    (resonant_home / "config.yaml").write_text(
        f"power:\n  keep_awake: false\napi:\n  port: {bound_port}\n"
    )
    result = CliRunner().invoke(cli.app, ["daemon", "install", "--no-ollama"])
    assert result.exit_code == 0, result.output
    assert installed == ["com.resonant.daemon"]


def test_sandbox_denies_the_instances_port(tmp_path: Path) -> None:
    def params(port: int | None = None) -> SandboxParams:
        p = SandboxParams(run_dir=tmp_path / "run", cache_dir=tmp_path / "cache",
                          secrets_dir=tmp_path / "secrets", home=tmp_path, protect=())  # fmt: skip
        return p if port is None else replace(p, api_port=port)

    text, _ = render(params())
    assert '(remote ip "*:7777")' in text and "@API_PORT@" not in text
    text, _ = render(params(7778))
    assert '(remote ip "*:7778")' in text and '(local ip "*:7778")' in text
    assert "*:7777" not in text


# --- CLI targeting ------------------------------------------------------------------------


@dataclass
class StatusSeen:
    ports: list[int] = field(default_factory=list[int])
    homes: list[Path] = field(default_factory=list[Path])
    labels: list[str] = field(default_factory=list[str])


@pytest.fixture
def status_seen(monkeypatch: pytest.MonkeyPatch) -> StatusSeen:
    seen = StatusSeen()

    def fetch(settings: Settings) -> dict[str, Any] | None:
        seen.ports.append(settings.api.port)
        seen.homes.append(settings.home)
        return None

    def loaded(label: str) -> bool:
        seen.labels.append(label)
        return False

    monkeypatch.setattr(cli, "_fetch_status", fetch)
    monkeypatch.setattr(launchd, "is_loaded", loaded)
    return seen


def _status(args: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(cli.app, [*args, "status"], env=env)
    return result.exit_code, result.output


def test_env_targets_cori(
    cori_home: Path, no_home_env: None, status_seen: StatusSeen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``RESONANT_INSTANCE=cori resonant status`` reads ~/.resonant-cori/config.yaml and port
    7778, and prints cori."""
    write_cori_config(cori_home)
    open_db(cori_home / "resonant.db").close()
    monkeypatch.setenv("RESONANT_INSTANCE", "cori")
    code, out = _status([])
    assert code == 1  # down: nothing answers on 7778
    assert "instance    cori (Cori)" in out
    assert status_seen.ports == [7778]
    assert status_seen.homes == [cori_home]
    assert status_seen.labels == ["com.resonant.cori.daemon"]


def test_flag_and_env_agree(
    cori_home: Path, no_home_env: None, status_seen: StatusSeen, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_cori_config(cori_home)
    open_db(cori_home / "resonant.db").close()
    _, by_flag = _status(["--instance", "cori"])
    monkeypatch.setenv("RESONANT_INSTANCE", "cori")
    _, by_env = _status([])
    _, by_both = _status(["--instance", "cori"])
    assert by_flag == by_env == by_both
    assert status_seen.ports == [7778, 7778, 7778]


def test_flag_and_env_disagreeing_is_refused(
    cori_home: Path, no_home_env: None, status_seen: StatusSeen, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESONANT_INSTANCE", "dori")
    code, out = _status(["--instance", "cori"])
    assert code == 2
    assert "--instance cori disagrees with RESONANT_INSTANCE=dori" in out
    assert status_seen.ports == []


def test_home_name_mismatch_is_refused(
    user_home: Path, no_home_env: None, status_seen: StatusSeen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``instance.name: resonant`` with RESONANT_INSTANCE=cori: refused."""
    wrong = user_home / ".resonant-cori"
    wrong.mkdir()
    (wrong / "config.yaml").write_text("instance:\n  name: resonant\n")
    code, out = _status(["--instance", "cori"])
    assert code == 2
    assert f"this home ({wrong}) is instance resonant, not cori" in out
    monkeypatch.setenv("RESONANT_INSTANCE", "cori")
    with pytest.raises(instance.InstanceMismatch):
        load_settings()
    assert status_seen.ports == []


def test_explicit_resonant_home_wins(
    tmp_path: Path, user_home: Path, status_seen: StatusSeen, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    write_cori_config(elsewhere)
    monkeypatch.setenv("RESONANT_HOME", str(elsewhere))
    _status(["--instance", "cori"])
    assert status_seen.homes == [elsewhere]
    assert load_settings(home=elsewhere).instance.name == "cori"  # home= in tests too


def test_explicit_home_with_another_name_is_refused(
    tmp_path: Path, resonant_home: Path, status_seen: StatusSeen
) -> None:
    write_cori_config(resonant_home)
    code, out = _status(["--instance", "dori"])
    assert code == 2 and "is instance cori, not dori" in out


def test_a_bad_name_on_the_cli_is_refused(status_seen: StatusSeen) -> None:
    code, out = _status(["--instance", "Cori"])
    assert code == 2 and "must be a slug" in out
    code, out = _status([], env={"RESONANT_INSTANCE": "co.ri"})
    assert code == 2 and "must be a slug" in out


def test_another_instances_home_is_refused(
    user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RESONANT_HOME=~/.resonant with RESONANT_INSTANCE=cori (or the other way round) would
    run one instance on the other's DB: refused."""
    monkeypatch.setenv("RESONANT_HOME", str(user_home / ".resonant"))
    monkeypatch.setenv("RESONANT_INSTANCE", "cori")
    with pytest.raises(ValidationError, match="is instance resonant's home, not cori's"):
        load_settings()
    monkeypatch.setenv("RESONANT_HOME", str(user_home / ".resonant-cori"))
    monkeypatch.delenv("RESONANT_INSTANCE")
    with pytest.raises(ValidationError, match="is instance cori's home, not resonant's"):
        load_settings()


def test_no_instance_needs_no_flag(resonant_home: Path, status_seen: StatusSeen) -> None:
    open_db(resonant_home / "resonant.db").close()
    code, out = _status([])
    assert code == 1
    assert "instance    resonant (Resonant)" in out
    assert status_seen.ports == [7777]
    assert status_seen.labels == ["com.resonant.daemon"]


# --- shown everywhere ---------------------------------------------------------------------


def test_status_and_snapshot_name_the_instance(resonant_home: Path) -> None:
    from resonant.dashboard.snapshot import build_snapshot, to_remote

    (resonant_home / "config.yaml").write_text("instance:\n  name: cori\n  display_name: Cori D\n")
    settings = load_settings()
    conn = open_db(settings.db_path)
    status = collect_status(conn, settings)
    assert status["instance"] == {"name": "cori", "display_name": "Cori D"}
    assert render_status(status).startswith("Cori D up ")
    snap = build_snapshot(conn, settings, started_at=datetime.now(UTC), launch_agents=[])
    assert snap.instance is not None and snap.instance.name == "cori"
    assert to_remote(snap).instance == snap.instance
    assert snap.model_dump(by_alias=True)["schema"] == 2


def test_personal_status_text_is_unchanged(resonant_home: Path) -> None:
    settings = load_settings()
    status = collect_status(open_db(settings.db_path), settings)
    assert status["instance"] == {"name": "resonant", "display_name": "Resonant"}
    assert render_status(status).startswith("Resonant up ")
    assert render_status({"uptime_s": 5}).startswith("Resonant up ")  # an older daemon
