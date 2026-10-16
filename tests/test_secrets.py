"""Keychain secrets (P3-3): ``SecretStore``, redaction, and ``resonant secret``.

Every ``security`` call goes to ``FakeKeychain``: nothing here runs ``/usr/bin/security`` or
touches the real Keychain.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import pytest
from typer.testing import CliRunner

from resonant import cli
from resonant.cli import EX_TEMPFAIL, app
from resonant.redact import REDACTED, Redactor
from resonant.secrets import (
    EXIT_NOT_FOUND,
    SECURITY,
    SERVICE,
    SET_ARGV,
    FakeKeychain,
    InvalidSecretRef,
    InvalidSecretValue,
    SecretMissing,
    SecretStore,
    SecretUnavailable,
    SubprocessSecurity,
    add_command,
    decode_value,
    delete_argv,
    encode_value,
    env_name,
    find_argv,
    secret_ref,
    split_ref,
    validate_ref,
    values_of,
)

TOKEN = "ghp_S3ntinel+Value/abc=XYZ"  # has URL- and base64-special characters


def store(*items: tuple[str, str]) -> tuple[SecretStore, FakeKeychain]:
    keychain = FakeKeychain(items=dict(items))
    return SecretStore(keychain), keychain


# --- refs ------------------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["github/token", "my-ext/api_key2", "a/b", "core/x"])
def test_valid_refs(ref: str) -> None:
    assert validate_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "-w/token",
        "github/-w",
        "--help",
        "github",
        "github/",
        "/token",
        "github/token/x",
        "GitHub/token",
        "github/Token",
        "github/to ken",
        "github/to-ken",  # names use underscores
        "github/token\n",
        "github/" + "t" * 33,
        "",
    ],
)
def test_bad_refs_are_refused(ref: str) -> None:
    with pytest.raises(InvalidSecretRef):
        validate_ref(ref)
    with pytest.raises(InvalidSecretRef):
        find_argv(ref, value=True)


def test_ref_helpers() -> None:
    assert secret_ref("github", "token") == "github/token"
    assert split_ref("github/token") == ("github", "token")
    assert env_name("api_key") == "RESONANT_SECRET_API_KEY"
    with pytest.raises(InvalidSecretRef):
        env_name("-x")
    with pytest.raises(InvalidSecretRef):
        secret_ref("github", "-w")


# --- argv shapes -----------------------------------------------------------------------


def test_argv_shapes() -> None:
    assert find_argv("github/token", value=True) == [
        SECURITY, "find-generic-password", "-s", SERVICE, "-a", "github/token", "-w",
    ]  # fmt: skip
    assert find_argv("github/token", value=False)[-1] == "github/token"
    assert SET_ARGV == [SECURITY, "-i"]  # the command is on stdin
    assert add_command("github/token", TOKEN) == (
        f"add-generic-password -U -s {SERVICE} -a github/token -w {TOKEN}\n".encode()
    )
    assert delete_argv("github/token") == [
        SECURITY, "delete-generic-password", "-s", SERVICE, "-a", "github/token",
    ]  # fmt: skip


async def test_get_reads_value_from_stdout_with_devnull_stdin() -> None:
    s, keychain = store(("github/token", TOKEN))
    assert await s.get("github/token") == TOKEN
    (call,) = keychain.calls
    assert call.stdin is None  # /dev/null
    assert call.timeout_s == 5.0
    assert call.argv == tuple(find_argv("github/token", value=True))


async def test_set_passes_value_on_stdin_never_argv() -> None:
    s, keychain = store()
    await s.set("github/token", TOKEN)
    (call,) = keychain.calls
    assert call.argv == tuple(SET_ARGV)
    assert all(TOKEN not in a for a in call.argv)
    assert call.stdin == add_command("github/token", TOKEN)
    assert keychain.items == {"github/token": TOKEN}
    await s.set("github/token", TOKEN + "2")  # -U replaces
    assert keychain.items == {"github/token": TOKEN + "2"}


async def test_exists_and_delete_never_read_the_value() -> None:
    s, keychain = store(("github/token", TOKEN))
    assert await s.exists("github/token")
    assert not await s.exists("github/other")
    assert await s.delete("github/token")
    assert not await s.delete("github/token")
    assert not await s.exists("github/token")
    assert all("-w" not in argv for argv in keychain.argvs())


@pytest.mark.parametrize(
    "value",
    ["s3cr3t!", "x" * 7, "x" * 3501, "tab\there!", "é" * 8, "ctl\x01chars!", "n" * 2700 + " "],
)
async def test_set_refuses_bad_values_before_running_security(value: str) -> None:
    s, keychain = store()
    with pytest.raises(InvalidSecretValue) as e:
        await s.set("github/token", value)
    assert value not in str(e.value)
    assert keychain.calls == []


async def test_no_caching() -> None:
    s, keychain = store(("github/token", TOKEN))
    await s.get("github/token")
    keychain.items["github/token"] = "rotated-value"
    assert await s.get("github/token") == "rotated-value"
    assert len(keychain.calls) == 2


# --- exit codes and errors -------------------------------------------------------------


async def test_missing_item() -> None:
    s, _ = store()
    with pytest.raises(SecretMissing) as e:
        await s.get("github/token")
    assert str(e.value) == "secret github/token is missing"
    assert e.value.ref == "github/token"


async def test_empty_value_counts_as_missing() -> None:
    s, _ = store(("github/token", ""))
    with pytest.raises(SecretMissing):
        await s.get("github/token")


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (36, "keychain locked or no user interaction allowed (exit 36)"),
        (51, "keychain authorization failed (exit 51)"),
        (128, "keychain prompt canceled (exit 128)"),
        (1, "security failed (exit 1)"),
    ],
)
async def test_unavailable_exit_codes(code: int, reason: str) -> None:
    s, keychain = store(("github/token", TOKEN))
    keychain.fail_with = code
    for call in (s.get, s.exists, s.delete):
        with pytest.raises(SecretUnavailable) as e:
            await call("github/token")
        assert e.value.reason == f"unavailable: {reason}"
    with pytest.raises(SecretUnavailable):
        await s.set("github/token", TOKEN)


async def test_exists_is_false_on_not_found() -> None:
    s, keychain = store()
    keychain.fail_with = EXIT_NOT_FOUND
    assert not await s.exists("github/token")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError(), "unavailable: keychain timed out after 5s"),
        (FileNotFoundError(2, "No such file", SECURITY), "unavailable: can't run security (2)"),
    ],
)
async def test_timeout_and_spawn_failure(error: BaseException, reason: str) -> None:
    s, keychain = store()
    keychain.fail_with = error
    with pytest.raises(SecretUnavailable) as e:
        await s.get("github/token")
    assert e.value.reason == reason
    assert e.value.__cause__ is None and e.value.__suppress_context__


async def test_value_never_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    s, keychain = store(("github/token", TOKEN))
    await s.get("github/token")
    await s.set("github/token", TOKEN)
    messages: list[str] = []
    for code in (36, 1, EXIT_NOT_FOUND):
        keychain.fail_with = code
        for coro in (s.get("github/token"), s.set("github/token", TOKEN)):
            try:
                await coro
            except Exception as e:
                messages.append(repr(e))
    assert messages
    assert all(TOKEN not in m for m in messages)
    assert TOKEN not in caplog.text


async def test_env_for_builds_declared_env_or_fails_whole() -> None:
    s, _ = store(("github/token", TOKEN), ("github/hook_key", "hook-value-1"))
    env = await s.env_for("github", ["token", "hook_key"])
    assert env == {"RESONANT_SECRET_TOKEN": TOKEN, "RESONANT_SECRET_HOOK_KEY": "hook-value-1"}
    assert sorted(values_of({**env, "PATH": "/bin"})) == sorted([TOKEN, "hook-value-1"])
    with pytest.raises(SecretMissing) as e:
        await s.env_for("github", ["token", "absent"])
    assert e.value.ref == "github/absent"


async def test_real_runner_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """SubprocessSecurity: argv exec (no shell), new session, devnull stdin, stderr dropped.
    The exec is intercepted, so no process starts."""
    import asyncio

    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    async def fake_exec(*argv: str, **kwargs: object) -> object:
        calls.append((argv, kwargs))
        raise FileNotFoundError(2, "intercepted")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    for key, value in [("HOME", "/Users/x"), ("USER", "x"), ("LOGNAME", "x")]:
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RESONANT_SECRET_TOKEN", TOKEN)
    s = SecretStore(SubprocessSecurity())
    with pytest.raises(SecretUnavailable):
        await s.get("github/token")
    argv, kwargs = calls[-1]
    assert argv == tuple(find_argv("github/token", value=True))
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["env"] == {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "HOME": "/Users/x",
        "USER": "x",
        "LOGNAME": "x",
    }
    with pytest.raises(SecretUnavailable):
        await s.set("github/token", TOKEN)
    argv, kwargs = calls[-1]
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert all(TOKEN not in a for a in argv)


# --- redaction -------------------------------------------------------------------------


def b64_forms(value: str) -> list[str]:
    """The value base64-encoded alone and inside larger blobs, at alignments 0, 1 and 2."""
    raw = value.encode()
    out: list[str] = []
    for k in range(3):
        out.append(base64.b64encode(bytes(k) + raw).decode())
        for lead in (b"", b"lead-in:"):
            blob = lead + bytes(range(1, k + 1)) + raw + b" trailing bytes!"
            out += [base64.b64encode(blob).decode(), base64.urlsafe_b64encode(blob).decode()]
    return out


def leaks(value: str, text: str) -> bool:
    """Whether text still holds the value in any covered form (or 6+ base64 chars of it)."""
    forms = [value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1]]
    if any(f in text for f in forms):
        return True
    raw = value.encode()
    for k in range(3):
        enc = base64.b64encode(bytes(k) + raw).decode()
        interior = enc[-(-8 * k // 6) : 8 * (k + len(raw)) // 6]
        if interior in text or interior.replace("+", "-").replace("/", "_") in text:
            return True
    return False


@pytest.mark.parametrize(
    "value", [TOKEN, "plain-alnum-token-123", 'p@ss wörd "q" \\ /é😀', "ab%2Fcd+ef/gh"]
)
def test_redacts_every_text_form(value: str) -> None:
    r = Redactor([value])
    forms = [
        value,
        quote(value, safe=""),
        re.sub("%[0-9A-F]{2}", lambda m: m[0].lower(), quote(value, safe="")),
        quote_plus(value),
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        json.dumps(value).replace("/", "\\/")[1:-1],
    ]
    for form in forms:
        out = r.text(f"before {form} after")
        assert out == f"before {REDACTED} after", form


@pytest.mark.parametrize("value", [TOKEN, "plain-alnum-token-123", "x" * 8, "été-pass"])
def test_redacts_base64_at_every_alignment(value: str) -> None:
    r = Redactor([value])
    for blob in b64_forms(value):
        out = r.text(f"data={blob};")
        assert REDACTED in out, blob
        assert not leaks(value, out), (blob, out)


def test_base64_inside_json_and_url() -> None:
    r = Redactor([TOKEN])
    blob = base64.b64encode(b"Authorization: token " + TOKEN.encode()).decode()
    assert not leaks(TOKEN, r.text(json.dumps({"x": blob}).replace("/", "\\/")))
    assert not leaks(TOKEN, r.text(quote(blob, safe="")))


def test_overlapping_and_repeated_values() -> None:
    a, b = "abcdefgh12", "defgh12345"
    r = Redactor([a, b])
    assert r.text("xx abcdefgh12345 yy") == f"xx {REDACTED} yy"
    r2 = Redactor(["aaaaaaaa"])
    assert r2.text("aaaaaaaaaaa!") == f"{REDACTED}!"
    assert r.text(f"{a}-{a}") == f"{REDACTED}-{REDACTED}"
    nested = Redactor(["inner-secret", "outer-inner-secret-x"])
    assert nested.text("outer-inner-secret-x") == REDACTED


def test_deep_redaction() -> None:
    r = Redactor([TOKEN, "second-secret"])
    data: dict[str, Any] = {
        "ok": True,
        "n": 3,
        TOKEN: [f"Bearer {TOKEN}", ("second-secret", None), {"url": quote(TOKEN, safe="")}],
        "nested": {"deeper": [{"b64": base64.b64encode(TOKEN.encode()).decode()}]},
    }
    out = r.deep(data)
    assert out["ok"] is True and out["n"] == 3
    dumped = json.dumps(out)
    assert not leaks(TOKEN, dumped) and "second-secret" not in dumped
    assert out[REDACTED][1] == (REDACTED, None)
    assert data[TOKEN][0] == f"Bearer {TOKEN}"  # the input isn't mutated


def test_empty_redactor_and_empty_values() -> None:
    r = Redactor(["", ""])
    assert not r
    assert r.text("anything") == "anything"
    obj = {"a": [1]}
    assert r.deep(obj) is obj
    assert Redactor(["secret-value"]).text("") == ""


def test_no_false_positives_on_unrelated_text() -> None:
    r = Redactor([TOKEN])
    text = "ghp_ token S3ntinel " + base64.b64encode(b"unrelated content here").decode()
    assert r.text(text) == text


# --- CLI -------------------------------------------------------------------------------


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    fake = FakeKeychain()
    monkeypatch.setattr(cli, "_secret_store", lambda: SecretStore(fake))
    return fake


def run(*args: str, input: str | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(app, ["secret", *args], input=input)
    return result.exit_code, result.output


def test_cli_set_from_stdin_verifies_and_never_echoes(keychain: FakeKeychain) -> None:
    code, out = run("set", "github/token", input=TOKEN + "\n")
    assert code == 0, out
    assert out.strip() == "stored github/token"
    assert keychain.items == {"github/token": TOKEN}
    assert [a[1] for a in keychain.argvs()] == ["-i", "find-generic-password"]
    assert all(TOKEN not in arg for argv in keychain.argvs() for arg in argv)


def test_cli_set_prompts_hidden_twice_on_a_tty(
    keychain: FakeKeychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    import getpass

    answers = iter([TOKEN, TOKEN])
    prompts: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    code, out = run("set", "github/token")
    assert code == 0 and TOKEN not in out
    assert prompts == ["Value for github/token (hidden): ", "Again: "]

    answers = iter([TOKEN, TOKEN + "x"])
    code, out = run("set", "github/other")
    assert code == 1 and "didn't match" in out and TOKEN not in out
    assert "github/other" not in keychain.items


def test_cli_set_refuses_short_and_bad_refs(keychain: FakeKeychain) -> None:
    code, out = run("set", "github/token", input="short\n")
    assert code == 2 and "too short" in out and "short\n" not in out.split("error")[0]
    code, out = run("set", "-w", input=TOKEN + "\n")
    assert code == 2
    code, out = run("set", "github/-w", input=TOKEN + "\n")
    assert code == 2 and TOKEN not in out
    assert keychain.calls == []


def test_cli_set_with_value_in_argv_is_rejected(keychain: FakeKeychain) -> None:
    code, _ = run("set", "github/token", TOKEN, input=TOKEN + "\n")
    assert code != 0
    assert keychain.calls == []


def test_cli_set_readback_mismatch_removes(
    keychain: FakeKeychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def wrong_get(self: SecretStore, ref: str) -> str:
        return "something-else"

    monkeypatch.setattr(SecretStore, "get", wrong_get)
    code, out = run("set", "github/token", input=TOKEN + "\n")
    assert code == 1 and "didn't read back" in out and TOKEN not in out
    assert keychain.items == {}


def test_cli_set_locked_keychain(keychain: FakeKeychain) -> None:
    keychain.fail_with = 36
    code, out = run("set", "github/token", input=TOKEN + "\n")
    assert code == EX_TEMPFAIL
    assert "keychain locked" in out and TOKEN not in out


def test_cli_check_reports_presence_only(keychain: FakeKeychain) -> None:
    keychain.items["github/token"] = TOKEN
    code, out = run("check", "github/token")
    assert (code, out.strip()) == (0, "github/token: ok (no prompt)")
    code, out = run("check", "github/absent")
    assert code == 1 and "resonant secret set github/absent" in out
    keychain.fail_with = TimeoutError()
    code, out = run("check", "github/token")
    assert code == EX_TEMPFAIL and "timed out" in out and TOKEN not in out


def test_cli_rm(keychain: FakeKeychain) -> None:
    keychain.items["github/token"] = TOKEN
    assert run("rm", "github/token") == (0, "removed github/token\n")
    code, out = run("rm", "github/token")
    assert code == 1 and "not found" in out
    assert run("rm", "--", "-x/y")[0] == 2


MANIFEST = """\
version: 1
name: {name}
secrets: [{secrets}]
tools:
  t:
    run: ./cli.py t
    effect: read
"""


def write_ext(root: Path, name: str, secrets: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "resonant.yaml").write_text(MANIFEST.format(name=name, secrets=secrets))
    return d


def test_cli_list_shows_names_and_presence_only(
    keychain: FakeKeychain, resonant_home: Path, tmp_path: Path
) -> None:
    write_ext(resonant_home / "extensions", "github", "token, hook_key")
    extra = write_ext(tmp_path / "src", "example", "api_key")
    keychain.items["github/token"] = TOKEN
    code, out = run("list", str(extra))
    assert code == 0, out
    lines = [line.split() for line in out.strip().splitlines()]
    assert lines == [
        ["example/api_key", "missing"],
        ["github/hook_key", "missing"],
        ["github/token", "present"],
    ]
    assert TOKEN not in out
    assert all("-w" not in argv for argv in keychain.argvs())


def test_cli_list_empty_and_unavailable(keychain: FakeKeychain, resonant_home: Path) -> None:
    code, out = run("list")
    assert code == 0 and "no declared secrets" in out
    write_ext(resonant_home / "extensions", "github", "token")
    keychain.fail_with = 36
    code, out = run("list")
    assert code == EX_TEMPFAIL and "keychain locked" in out


# --- large and multi-line values (security -i, docs/repos-plan.md D10) ---------------------------

PEM = (
    "-----BEGIN RSA PRIVATE "
    + "KEY-----\n"
    + "MIIEowIBAAKCAQEA" * 90
    + "\n-----END RSA PRIVATE "
    + "KEY-----"
)


def test_values_are_stored_plain_or_as_one_safe_token() -> None:
    assert encode_value(TOKEN) == TOKEN
    for odd in ("has space in it", "quo\"te's", "back\\slash!", PEM, "resonant-b64:abc12345"):
        stored = encode_value(odd)
        assert stored.startswith("resonant-b64:") and " " not in stored and "\n" not in stored
        assert decode_value(stored) == odd
    assert decode_value("resonant-b64:!!notbase64") == "resonant-b64:!!notbase64"


async def test_set_takes_a_multi_line_value_without_argv_and_reads_it_back() -> None:
    s, keychain = store()
    await s.set("runner/github_app_key", PEM)
    (call,) = keychain.calls
    assert call.argv == (SECURITY, "-i")  # nothing of the key in argv
    assert call.stdin is not None and call.stdin.count(b"\n") == 1  # one command line
    assert len(call.stdin) < 4096 and b"BEGIN" not in call.stdin  # ...of one safe token
    assert await s.get("runner/github_app_key") == PEM
    assert all("MIIE" not in a for argv in keychain.argvs() for a in argv)


async def test_the_largest_value_fits_one_security_line() -> None:
    s, _ = store()
    biggest = "A" * 3500
    await s.set("github/token", biggest)
    assert await s.get("github/token") == biggest
    spacey = "ab " * 800  # 2400 characters, 3.2 KB once stored
    await s.set("github/token", spacey)
    assert await s.get("github/token") == spacey


def test_cli_set_from_file_reads_a_pem_in_process(keychain: FakeKeychain, tmp_path: Path) -> None:
    pem = tmp_path / "key.pem"
    pem.write_text(PEM + "\n")
    code, out = run("set", "runner/github_app_key", "--from-file", str(pem))
    assert code == 0, out
    assert out.strip() == "stored runner/github_app_key"
    assert asyncio.run(SecretStore(keychain).get("runner/github_app_key")) == PEM
    assert all("MIIE" not in a for argv in keychain.argvs() for a in argv)
    assert str(pem) not in " ".join(a for argv in keychain.argvs() for a in argv)


def test_cli_set_from_file_dash_reads_all_of_stdin(keychain: FakeKeychain) -> None:
    code, out = run("set", "runner/github_app_key", "--from-file", "-", input=PEM + "\n")
    assert code == 0, out
    assert asyncio.run(SecretStore(keychain).get("runner/github_app_key")) == PEM


def test_cli_set_from_file_errors(keychain: FakeKeychain, tmp_path: Path) -> None:
    code, out = run("set", "runner/github_app_key", "--from-file", str(tmp_path / "nope.pem"))
    assert code == 2 and "can't read" in out and keychain.calls == []
    big = tmp_path / "big.pem"
    big.write_text("x" * 5000)
    code, out = run("set", "runner/github_app_key", "--from-file", str(big))
    assert code == 2 and "too long" in out and keychain.calls == []
