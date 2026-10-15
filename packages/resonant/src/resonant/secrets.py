"""Keychain secrets: ``SecretStore`` over ``/usr/bin/security``.

Every secret is a generic-password item in the login keychain with service ``resonant`` and
account ``<ext>/<name>`` (a *ref*, e.g. ``github/token``). Extensions see a secret only as
``RESONANT_SECRET_<NAME>`` in their own process env (injection is the executor's job).

Rules this module keeps:

- ``security`` is run by absolute path with an argv list (never a shell), a timeout, a new
  session (no controlling terminal, so it can never prompt there) and a minimal env. stderr is
  discarded unread.
- A value is never in argv. Reads take it from stdout; ``set`` feeds it on stdin to the
  trailing ``-w`` prompt.
- Refs are slug-validated before any argv is built: both parts start with ``[a-z]``, so the
  ``-a`` value can never look like an option.
- Errors carry the ref and an exit code or reason, never a value or ``security``'s output.
- Nothing is cached: every call asks the Keychain.

The daemon must run as a GUI-session LaunchAgent: the login keychain is unlocked there and
items created by ``security`` trust that binary, so reads don't prompt. Python ``keyring``
isn't used because its ACL follows the interpreter binary.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

SECURITY = "/usr/bin/security"
SERVICE = "resonant"
TIMEOUT_S = 5.0
MIN_LEN = 8
# ``security`` reads the trailing ``-w`` value with getpass(3), which keeps 128 bytes.
MAX_LEN = 128
ENV_PREFIX = "RESONANT_SECRET_"

EXT_SLUG = r"[a-z][a-z0-9-]{0,47}"  # the manifest's extension name rule
NAME_SLUG = r"[a-z][a-z0-9_]{0,31}"  # the manifest's secret name rule
_REF = re.compile(rf"(?P<ext>{EXT_SLUG})/(?P<name>{NAME_SLUG})")
_NAME = re.compile(NAME_SLUG)

# security(1) exits with the low byte of the OSStatus.
EXIT_NOT_FOUND = 44  # errSecItemNotFound (-25300)
_LOCKED = {
    36: "keychain locked or no user interaction allowed",  # errSecInteractionNotAllowed
    51: "keychain authorization failed",  # errSecAuthFailed
    128: "keychain prompt canceled",  # userCanceledErr
    29: "keychain needs user interaction",  # errSecInteractionRequired
    50: "no default keychain",  # errSecNoSuchKeychain
    53: "keychain not available",  # errSecNotAvailable
}


class InvalidSecretRef(ValueError):
    """A ref that isn't ``<ext>/<name>``. The message quotes the ref, which is never a value."""


class InvalidSecretValue(ValueError):
    """A value ``set`` refuses. The message never includes the value."""


class SecretError(Exception):
    def __init__(self, ref: str, reason: str) -> None:
        super().__init__(f"secret {ref} {reason}")
        self.ref = ref
        self.reason = reason


class SecretMissing(SecretError):
    """No Keychain item for the ref."""

    def __init__(self, ref: str) -> None:
        super().__init__(ref, "is missing")


class SecretUnavailable(SecretError):
    """The Keychain couldn't answer: locked, timed out, or ``security`` failed."""


def validate_ref(ref: str) -> str:
    if not _REF.fullmatch(ref):
        raise InvalidSecretRef(f"bad secret ref {ref!r}: want <ext>/<name>, like github/token")
    return ref


def secret_ref(ext: str, name: str) -> str:
    """``github`` + ``token`` → ``github/token`` (validated)."""
    return validate_ref(f"{ext}/{name}")


def split_ref(ref: str) -> tuple[str, str]:
    m = _REF.fullmatch(validate_ref(ref))
    assert m is not None
    return m["ext"], m["name"]


def env_name(name: str) -> str:
    """``token`` → ``RESONANT_SECRET_TOKEN``."""
    if not _NAME.fullmatch(name):
        raise InvalidSecretRef(f"bad secret name {name!r}")
    return ENV_PREFIX + name.upper()


def validate_value(value: str) -> None:
    """``set`` takes 8..128 printable ASCII characters (spaces allowed, no newlines)."""
    if len(value) < MIN_LEN:
        raise InvalidSecretValue(f"too short: at least {MIN_LEN} characters")
    if len(value) > MAX_LEN:
        raise InvalidSecretValue(f"too long: at most {MAX_LEN} characters")
    if not all(" " <= c <= "~" for c in value):
        raise InvalidSecretValue("only printable ASCII (no newlines or control characters)")


# --- the security runner ---------------------------------------------------------------


@dataclass(frozen=True)
class SecurityResult:
    returncode: int
    stdout: bytes


class SecurityRunner(Protocol):
    async def run(
        self, argv: Sequence[str], *, stdin: bytes | None, timeout_s: float
    ) -> SecurityResult:
        """Run argv. ``stdin`` None means /dev/null. Raises ``TimeoutError`` past
        ``timeout_s`` (the process group is killed) and ``OSError`` if it can't start."""
        ...


def _security_env() -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
    for key in ("HOME", "USER", "LOGNAME"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


class SubprocessSecurity:
    """The real runner. Tests never use it: they inject ``FakeKeychain``."""

    async def run(
        self, argv: Sequence[str], *, stdin: bytes | None, timeout_s: float
    ) -> SecurityResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=_security_env(),
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # No controlling terminal: getpass(3) reads stdin, and nothing can prompt on a tty.
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout_s)
        except BaseException:  # timeout, cancel, or anything else: never leave it running
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            raise
        return SecurityResult(proc.returncode if proc.returncode is not None else -1, out)


# --- the store ---------------------------------------------------------------------------


def find_argv(ref: str, *, value: bool) -> list[str]:
    argv = [SECURITY, "find-generic-password", "-s", SERVICE, "-a", validate_ref(ref)]
    return [*argv, "-w"] if value else argv


def add_argv(ref: str) -> list[str]:
    # -U updates an existing item; -w last makes security read the value (twice) from stdin.
    return [SECURITY, "add-generic-password", "-U", "-s", SERVICE, "-a", validate_ref(ref), "-w"]


def delete_argv(ref: str) -> list[str]:
    return [SECURITY, "delete-generic-password", "-s", SERVICE, "-a", validate_ref(ref)]


class SecretStore:
    """Keychain items ``resonant`` / ``<ext>/<name>``. No caching."""

    def __init__(
        self, runner: SecurityRunner | None = None, *, timeout_s: float = TIMEOUT_S
    ) -> None:
        self.runner: SecurityRunner = runner if runner is not None else SubprocessSecurity()
        self.timeout_s = timeout_s

    async def _run(self, ref: str, argv: list[str], stdin: bytes | None = None) -> SecurityResult:
        try:
            return await self.runner.run(argv, stdin=stdin, timeout_s=self.timeout_s)
        except TimeoutError:
            raise SecretUnavailable(
                ref, f"unavailable: keychain timed out after {self.timeout_s:g}s"
            ) from None
        except OSError as e:
            raise SecretUnavailable(ref, f"unavailable: can't run security ({e.errno})") from None

    @staticmethod
    def _failed(ref: str, code: int) -> SecretError:
        if code == EXIT_NOT_FOUND:
            return SecretMissing(ref)
        reason = _LOCKED.get(code, "security failed")
        return SecretUnavailable(ref, f"unavailable: {reason} (exit {code})")

    async def get(self, ref: str) -> str:
        """The value. Raises ``SecretMissing`` or ``SecretUnavailable``."""
        result = await self._run(ref, find_argv(ref, value=True))
        if result.returncode != 0:
            raise self._failed(ref, result.returncode)
        out = result.stdout.decode(errors="replace")
        value = out[:-1] if out.endswith("\n") else out
        if not value:
            raise SecretMissing(ref)
        return value

    async def exists(self, ref: str) -> bool:
        """Whether the item exists, without reading its value."""
        result = await self._run(ref, find_argv(ref, value=False))
        if result.returncode == 0:
            return True
        error = self._failed(ref, result.returncode)
        if isinstance(error, SecretMissing):
            return False
        raise error

    async def set(self, ref: str, value: str) -> None:
        """Create or replace the item. The value goes on stdin, never in argv."""
        argv = add_argv(ref)
        validate_value(value)
        line = value.encode() + b"\n"
        result = await self._run(ref, argv, stdin=line + line)  # entered, then retyped
        if result.returncode != 0:
            raise self._failed(ref, result.returncode)

    async def delete(self, ref: str) -> bool:
        """Delete the item. False if there was none."""
        result = await self._run(ref, delete_argv(ref))
        if result.returncode == 0:
            return True
        error = self._failed(ref, result.returncode)
        if isinstance(error, SecretMissing):
            return False
        raise error

    async def env_for(self, ext: str, names: Iterable[str]) -> dict[str, str]:
        """``{RESONANT_SECRET_<NAME>: value}`` for one tool's declared secrets. Raises on the
        first missing or unavailable one, so a tool never runs with part of its secrets."""
        env: dict[str, str] = {}
        for name in names:
            env[env_name(name)] = await self.get(secret_ref(ext, name))
        return env


def values_of(env: Mapping[str, str]) -> list[str]:
    """The secret values in an env built by ``env_for`` (for a ``Redactor``)."""
    return [v for k, v in env.items() if k.startswith(ENV_PREFIX)]


# --- the runner's core secrets -----------------------------------------------------------
# The Claude runner (docs/phase5a-plan.md) is core, not an extension, but its secrets use the
# same store under the reserved ``runner`` prefix: ``resonant secret set runner/github_token``.
# A ``claude setup-token`` value is about 108 characters, inside ``MAX_LEN``.

RUNNER_EXT = "runner"
GITHUB_TOKEN_REF = secret_ref(RUNNER_EXT, "github_token")
CLAUDE_TOKEN_REF = secret_ref(RUNNER_EXT, "claude_token")
ANTHROPIC_KEY_REF = secret_ref(RUNNER_EXT, "anthropic_key")


@dataclass(frozen=True)
class RunnerSecrets:
    # repr=False: a value must never reach a log line or a traceback by accident.
    github_token: str = field(repr=False)
    # At most one of these is set: the subscription token, or the API key for API billing.
    claude_token: str | None = field(repr=False)
    anthropic_key: str | None = field(repr=False)

    def values(self) -> list[str]:
        """Every secret value, for a ``Redactor``."""
        return [v for v in (self.github_token, self.claude_token, self.anthropic_key) if v]


async def missing_runner_secrets(store: SecretStore) -> list[str]:
    """What the runner still needs, as refs (names only; no value is read)."""
    missing: list[str] = []
    if not await store.exists(GITHUB_TOKEN_REF):
        missing.append(GITHUB_TOKEN_REF)
    if not (await store.exists(CLAUDE_TOKEN_REF) or await store.exists(ANTHROPIC_KEY_REF)):
        missing.append(f"{CLAUDE_TOKEN_REF} (or {ANTHROPIC_KEY_REF})")
    return missing


async def load_runner_secrets(store: SecretStore) -> RunnerSecrets:
    """The runner's values. Raises ``SecretMissing`` for the GitHub token or for both Claude
    credentials; the Claude token wins when both exist."""
    github = await store.get(GITHUB_TOKEN_REF)
    try:
        return RunnerSecrets(github, await store.get(CLAUDE_TOKEN_REF), None)
    except SecretMissing:
        return RunnerSecrets(github, None, await store.get(ANTHROPIC_KEY_REF))


# --- the test double ---------------------------------------------------------------------
# ``FakeKeychain`` understands the three argv shapes above, keeps items in a dict, and records
# every call (argv and stdin). ``fail_with`` makes every call exit with that code or raise.
# Nothing in it runs ``security`` or touches the real Keychain.


@dataclass(frozen=True)
class SecurityCall:
    argv: tuple[str, ...]
    stdin: bytes | None
    timeout_s: float


@dataclass
class FakeKeychain:
    items: dict[str, str] = field(default_factory=dict[str, str])  # ref -> value
    calls: list[SecurityCall] = field(default_factory=list[SecurityCall])
    fail_with: int | BaseException | None = None  # every call fails this way while set

    async def run(
        self, argv: Sequence[str], *, stdin: bytes | None, timeout_s: float
    ) -> SecurityResult:
        args = tuple(argv)
        self.calls.append(SecurityCall(args, stdin, timeout_s))
        if isinstance(self.fail_with, BaseException):
            raise self.fail_with
        if self.fail_with is not None:
            return SecurityResult(self.fail_with, b"")
        assert args[0] == SECURITY, args
        opts = {a: args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-s", "-a")}
        assert opts.get("-s") == SERVICE, args
        ref = opts["-a"]
        match args[1]:
            case "find-generic-password":
                if ref not in self.items:
                    return SecurityResult(EXIT_NOT_FOUND, b"")
                if args[-1] == "-w":
                    return SecurityResult(0, self.items[ref].encode() + b"\n")
                return SecurityResult(0, f'keychain: "login"\n"acct"<blob>="{ref}"\n'.encode())
            case "add-generic-password":
                assert args[-1] == "-w" and stdin is not None, args
                first, again, *_ = stdin.decode().split("\n")
                if first != again:
                    return SecurityResult(1, b"")
                if ref in self.items and "-U" not in args:
                    return SecurityResult(45, b"")  # errSecDuplicateItem
                self.items[ref] = first
                return SecurityResult(0, b"")
            case "delete-generic-password":
                if self.items.pop(ref, None) is None:
                    return SecurityResult(EXIT_NOT_FOUND, b"")
                return SecurityResult(0, b"")
            case other:
                raise AssertionError(f"unexpected security command {other}")

    def argvs(self) -> list[tuple[str, ...]]:
        return [c.argv for c in self.calls]
