"""Per-run GitHub App installation tokens (docs/repos-plan.md D10).

The Claude session drives its own run: it pushes, opens the PR and watches CI. What it holds
is not the owner's token but an installation token of the owner's GitHub App, limited to one
repository, with every permission but Administration (no delete, rename, settings or
protection changes), and valid for an hour. Workflows is left out when the repo's
``allow_github_changes`` is off.

- The app id and installation id are config (``runner.github_app``, public). The private key
  is the Keychain item ``runner/github_app_key``, read for each call and never kept, written
  to a file, put in env or argv, or logged. It is a PEM, or the PEM's base64 on one line.
- The JWT (RS256, 9 minutes) is built here with ``cryptography`` and sent as a bearer header
  over HTTPS. Nothing here runs a subprocess.
- ``mint`` asks for ``repositories=[name]`` and the permissions below; ``revoke`` is
  ``DELETE /installation/token`` with the token itself. Errors name the HTTP status and
  GitHub's own message, never a token, a key or a response body.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from resonant.config import GitHubAppConfig
from resonant.secrets import GITHUB_APP_KEY_REF, SecretError, SecretStore

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
TIMEOUT_S = 15.0
ATTEMPTS = 3
BACKOFF_S = 0.5
JWT_TTL_S = 540  # GitHub allows at most 10 minutes
CLOCK_SKEW_S = 60  # iat is back-dated so a slow clock doesn't make the JWT "from the future"
MAX_MESSAGE = 200

# What a run's token may do. No Administration, so a run can't delete, rename or reconfigure.
# issues (PR comments and labels) and actions (re-run a workflow) are write on the owner's
# decision, as is Workflows by default; the review noted that a same-repo workflow can read
# the repo's Actions secrets, so Workflows can be turned off per repo (allow_github_changes).
WRITE = ("contents", "pull_requests", "issues", "workflows", "actions")
READ = ("checks", "statuses", "metadata")


class GitHubAppError(Exception):
    """A token that couldn't be minted or revoked. The message is fit for ``R3 failed: …``
    and carries no secret."""


def permissions(*, workflows: bool) -> dict[str, str]:
    """The installation token's permissions; ``workflows`` is dropped when it's False."""
    perms = {name: "write" for name in WRITE if workflows or name != "workflows"}
    perms.update({name: "read" for name in READ})
    return perms


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def parse_private_key(text: str) -> rsa.RSAPrivateKey:
    """The key from a PEM, or from the base64 (or hex, which is how ``security`` prints a value
    with newlines) of a PEM or of a DER key. Raises ``GitHubAppError``, never quoting the text."""
    raw = text.strip()
    data = raw.replace("\\n", "\n").encode()
    if not raw.startswith("-----BEGIN"):
        squashed = "".join(raw.split())
        try:
            if len(squashed) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in squashed):
                data = bytes.fromhex(squashed)
            else:
                data = base64.b64decode(squashed, validate=True)
        except (binascii.Error, ValueError):
            raise GitHubAppError("the GitHub App key isn't a PEM or its base64") from None
    try:
        if data.lstrip().startswith(b"-----BEGIN"):
            key = serialization.load_pem_private_key(data, password=None)
        else:
            key = serialization.load_der_private_key(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise GitHubAppError("the GitHub App key can't be read as an RSA private key") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubAppError("the GitHub App key isn't an RSA key")
    return key


def make_jwt(app_id: int, key: rsa.RSAPrivateKey, now: float) -> str:
    """An RS256 JWT for the app, valid for ``JWT_TTL_S`` seconds."""
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iat": int(now) - CLOCK_SKEW_S, "exp": int(now) + JWT_TTL_S, "iss": str(app_id)}
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


@dataclass(frozen=True)
class InstallationToken:
    # repr=False: a token must never reach a log line or a traceback by accident.
    token: str = field(repr=False)
    expires_at: str  # GitHub's ISO timestamp (UTC, "...Z"); "" if the reply had none

    def expires(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.expires_at)
        except ValueError:
            return None


_LEVEL = {"none": 0, "read": 1, "write": 2, "admin": 3}
_ESSENTIAL = ("contents", "pull_requests")  # a session without these can't do its job


def check_grant(
    data: dict[str, Any], repo_name: str, requested: dict[str, str], owner: str | None = None
) -> str | None:
    """Why a mint reply is not what was asked for, or None. The token must cover exactly the
    one repository and no permission beyond (or outside) the request; a mis-installed app is
    caught here, not by a session finding it can do too much (or too little). With ``owner``
    (a fork run's fork) the repository's ``full_name`` must also be ``owner/repo_name``: the
    installation's account must be the one the fork was made in."""
    if data.get("repository_selection") != "selected":
        return "it covers more than the one repository"
    repos = data.get("repositories")
    items = cast(list[object], repos) if isinstance(repos, list) else []
    names = [str(cast(dict[str, object], r).get("name")) for r in items if isinstance(r, dict)]
    if len(names) != 1 or str(names[0]).lower() != repo_name.lower():
        return "it covers a different set of repositories than asked"
    if owner is not None:
        repo = next(cast(dict[str, object], r) for r in items if isinstance(r, dict))
        full = str(repo.get("full_name"))
        if full.lower() != f"{owner}/{repo_name}".lower():
            return f"it is for {full}, not {owner}/{repo_name}"
    granted_raw = data.get("permissions")
    if not isinstance(granted_raw, dict):
        return "it lists no permissions"
    granted = {str(k): str(v) for k, v in cast(dict[object, object], granted_raw).items()}
    for perm, level in granted.items():
        if perm not in requested or _LEVEL.get(level, 99) > _LEVEL[requested[perm]]:
            return f"it grants {perm}: {level}, which was not asked for"
    for perm in _ESSENTIAL:
        if _LEVEL.get(granted.get(perm, "none"), 0) < _LEVEL[requested[perm]]:
            return f"the app lacks {perm}: {requested[perm]} on this repository"
    return None


class GitHubApp:
    """Mints and revokes a repo-scoped installation token. ``transport`` is the test seam
    (an ``httpx.MockTransport``); by default the real network. Mint, revoke and the bot
    lookup try up to ``ATTEMPTS`` times on a network error, a timeout or a 5xx, waiting
    ``backoff_s``, then twice that."""

    def __init__(
        self,
        cfg: GitHubAppConfig,
        secrets: SecretStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = API_URL,
        clock: Callable[[], float] = time.time,
        timeout_s: float = TIMEOUT_S,
        backoff_s: float = BACKOFF_S,
    ) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self._transport = transport
        self._base = base_url.rstrip("/")
        self._clock = clock
        self._timeout_s = timeout_s
        self._backoff_s = backoff_s
        self._bot: str | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout_s,
            follow_redirects=False,
            trust_env=False,
        )

    async def _jwt(self) -> str:
        try:
            pem = await self.secrets.get(GITHUB_APP_KEY_REF)
        except SecretError as e:
            raise GitHubAppError(f"the GitHub App key can't be read ({e.reason})") from None
        return make_jwt(self.cfg.app_id, parse_private_key(pem), self._clock())

    async def _send(
        self, method: str, url: str, bearer: str, *, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        """One request, retried on a network error, a timeout or a 5xx. The last response (or
        ``GitHubAppError`` for no response at all) is the answer."""
        failure = "couldn't reach GitHub"
        last: httpx.Response | None = None
        for attempt in range(ATTEMPTS):
            if attempt:
                await asyncio.sleep(self._backoff_s * 2 ** (attempt - 1))
            try:
                async with self._client() as client:
                    resp = await client.request(method, url, headers=_headers(bearer), json=body)
            except httpx.HTTPError as e:
                failure = f"couldn't reach GitHub ({type(e).__name__})"
                continue
            if resp.status_code < 500:
                return resp
            last = resp
        if last is not None:
            return last
        raise GitHubAppError(failure)

    async def mint(
        self, repo_name: str, *, workflows: bool, owner: str | None = None
    ) -> InstallationToken:
        """A token for ``repo_name`` (the name only, no owner) with ``permissions``. A reply
        that isn't exactly that (see ``check_grant``) is revoked and refused. ``owner`` is
        checked against the reply when given (a fork run: the owner's fork, which the app's
        "all repositories" installation covers)."""
        jwt = await self._jwt()
        url = f"{self._base}/app/installations/{self.cfg.installation_id}/access_tokens"
        wanted = permissions(workflows=workflows)
        resp = await self._send(
            "POST", url, jwt, body={"repositories": [repo_name], "permissions": wanted}
        )
        if resp.status_code != 201:
            raise GitHubAppError(_why("minting the run's GitHub token", resp, repo_name))
        data = _json(resp)
        token, expires = data.get("token"), data.get("expires_at")
        if not isinstance(token, str) or not token:
            raise GitHubAppError("GitHub's token reply had no token")
        if (problem := check_grant(data, repo_name, wanted, owner)) is not None:
            with contextlib.suppress(GitHubAppError):
                await self.revoke(token)
            raise GitHubAppError(f"GitHub's token for {repo_name} was refused: {problem}")
        return InstallationToken(token, expires if isinstance(expires, str) else "")

    async def revoke(self, token: str) -> None:
        """``DELETE /installation/token`` as the token itself. A token already dead (401) is
        fine. Raises ``GitHubAppError`` otherwise."""
        resp = await self._send("DELETE", f"{self._base}/installation/token", token)
        if resp.status_code not in (204, 401):
            raise GitHubAppError(_why("revoking the run's GitHub token", resp, ""))

    async def bot_login(self) -> str:
        """``<slug>[bot]``, the actor the app's pushes carry in GitHub's activity feed."""
        if self._bot is None:
            resp = await self._send("GET", f"{self._base}/app", await self._jwt())
            slug = _json(resp).get("slug")
            if resp.status_code != 200 or not isinstance(slug, str) or not slug:
                raise GitHubAppError(_why("reading the GitHub App", resp, ""))
            self._bot = f"{slug}[bot]"
        return self._bot


def _headers(bearer: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "resonant-runner",
    }


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


_HINTS = {
    403: "does the app have the permissions asked for on {repo}?",
    404: "is the app installed on {repo}?",
    422: "the app may lack a permission asked for on {repo}",
}


def _why(what: str, resp: httpx.Response, repo: str) -> str:
    """The failure with GitHub's own message and, for the common statuses, what to check."""
    message = _json(resp).get("message")
    text = f"{what} failed (HTTP {resp.status_code})"
    if isinstance(message, str) and message:
        text += f": {' '.join(message.split())[:MAX_MESSAGE]}"
    if repo and (hint := _HINTS.get(resp.status_code)):
        text += f" ({hint.format(repo=repo)})"
    return text
