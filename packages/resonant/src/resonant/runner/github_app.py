"""Per-run GitHub App installation tokens (docs/repos-plan.md D10).

The Claude session drives its own run: it pushes, opens the PR and watches CI. What it holds
is not the owner's token but an installation token of the owner's GitHub App, limited to one
repository, with every permission but Administration (no delete, rename, settings or
protection changes), and valid for an hour. Workflows is left out when the repo's
``allow_github_changes`` is off.

- The app id and installation id are config (``runner.github_app``, public). The private key
  is the Keychain item ``runner/github_app_key``, read for each call and never kept, written
  to a file, put in env or argv, or logged. It is a PEM, or the PEM's base64 on one line (the
  Keychain tooling is easier with that).
- The JWT (RS256, 9 minutes) is built here with ``cryptography`` and sent as a bearer header
  over HTTPS. Nothing here runs a subprocess.
- ``mint`` asks for ``repositories=[name]`` and the permissions below; ``revoke`` is
  ``DELETE /installation/token`` with the token itself. Errors name the HTTP status and
  GitHub's own message, never a token, a key or a response body.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
JWT_TTL_S = 540  # GitHub allows at most 10 minutes
CLOCK_SKEW_S = 60  # iat is back-dated so a slow clock doesn't make the JWT "from the future"
MAX_MESSAGE = 200

# What a run's token may do. No Administration, so a run can't delete, rename or reconfigure.
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
    expires_at: str  # GitHub's ISO timestamp, informational


class GitHubApp:
    """Mints and revokes a repo-scoped installation token. ``transport`` is the test seam
    (an ``httpx.MockTransport``); by default the real network."""

    def __init__(
        self,
        cfg: GitHubAppConfig,
        secrets: SecretStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = API_URL,
        clock: Callable[[], float] = time.time,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self._transport = transport
        self._base = base_url.rstrip("/")
        self._clock = clock
        self._timeout_s = timeout_s

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

    async def mint(self, repo_name: str, *, workflows: bool) -> InstallationToken:
        """A token for ``repo_name`` (the name only, no owner) with ``permissions``."""
        jwt = await self._jwt()
        url = f"{self._base}/app/installations/{self.cfg.installation_id}/access_tokens"
        body = {"repositories": [repo_name], "permissions": permissions(workflows=workflows)}
        try:
            async with self._client() as client:
                resp = await client.post(url, headers=_headers(jwt), json=body)
        except httpx.HTTPError as e:
            raise GitHubAppError(f"couldn't reach GitHub ({type(e).__name__})") from None
        if resp.status_code != 201:
            raise GitHubAppError(_why("minting the run's GitHub token", resp, repo_name))
        data = _json(resp)
        token, expires = data.get("token"), data.get("expires_at")
        if not isinstance(token, str) or not token:
            raise GitHubAppError("GitHub's token reply had no token")
        return InstallationToken(token, expires if isinstance(expires, str) else "")

    async def revoke(self, token: str) -> None:
        """``DELETE /installation/token`` as the token itself. A token already dead (401) is
        fine. Raises ``GitHubAppError`` otherwise."""
        try:
            async with self._client() as client:
                resp = await client.delete(
                    f"{self._base}/installation/token", headers=_headers(token)
                )
        except httpx.HTTPError as e:
            raise GitHubAppError(f"couldn't reach GitHub ({type(e).__name__})") from None
        if resp.status_code not in (204, 401):
            raise GitHubAppError(_why("revoking the run's GitHub token", resp, ""))


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


def _why(what: str, resp: httpx.Response, repo: str) -> str:
    message = _json(resp).get("message")
    text = f"{what} failed (HTTP {resp.status_code})"
    if resp.status_code in (403, 404, 422) and repo:
        text += f": is the app installed on {repo}?"
    elif isinstance(message, str) and message:
        text += f": {' '.join(message.split())[:MAX_MESSAGE]}"
    return text
