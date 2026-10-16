"""A fake GitHub App API for the runner tests: no network.

``FakeGitHubApi`` answers the two calls ``GitHubApp`` makes through an ``httpx.MockTransport``:
``POST /app/installations/{id}/access_tokens`` (it checks the RS256 JWT against the app's
public key) and ``DELETE /installation/token``. It records what was asked for, so a test can
check the repo scoping, the permissions, and which tokens were revoked.
"""

from __future__ import annotations

import base64
import json
import time
from functools import cache
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

APP_ID = 424242
INSTALLATION_ID = 777


@cache
def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def private_pem() -> str:
    return (
        _key()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        .decode()
    )


def private_pem_one_line() -> str:
    """The PEM's base64 on a single line (what fits in a Keychain item)."""
    return base64.b64encode(private_pem().encode()).decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class FakeGitHubApi:
    def __init__(self, *, installation_id: int = INSTALLATION_ID, app_id: int = APP_ID) -> None:
        self.installation_id = installation_id
        self.app_id = app_id
        self.mints: list[dict[str, Any]] = []  # {repositories, permissions, claims, token}
        self.revoked: list[str] = []
        self.live: set[str] = set()
        self.fail_mint: tuple[int, str] | None = None  # (status, message) for the next mints
        self.fail_revoke = False
        self.flaky: int = 0  # the next N requests answer 503 (a transient failure)
        self.grant: dict[str, Any] = {}  # fields to override in a mint reply
        self.slug = "resonant-runner"
        self.requests: list[tuple[str, str]] = []  # (method, path)
        self.issued = 0

    @property
    def tokens(self) -> list[str]:
        return [m["token"] for m in self.mints]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _bearer(self, request: httpx.Request) -> str:
        auth = request.headers.get("Authorization", "")
        return auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""

    def _claims(self, jwt: str) -> dict[str, Any] | None:
        parts = jwt.split(".")
        if len(parts) != 3:
            return None
        try:
            header = json.loads(_unb64(parts[0]))
            _key().public_key().verify(
                _unb64(parts[2]),
                f"{parts[0]}.{parts[1]}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            claims: dict[str, Any] = json.loads(_unb64(parts[1]))
        except (ValueError, InvalidSignature):
            return None
        return claims if header == {"alg": "RS256", "typ": "JWT"} else None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        bearer = self._bearer(request)
        if self.flaky > 0:
            self.flaky -= 1
            return httpx.Response(503, json={"message": "unavailable"})
        if request.method == "GET" and path == "/app":
            claims = self._claims(bearer)
            if claims is None or claims.get("iss") != str(self.app_id):
                return httpx.Response(401, json={"message": "bad jwt"})
            return httpx.Response(200, json={"slug": self.slug, "id": self.app_id})
        if request.method == "POST" and path == (
            f"/app/installations/{self.installation_id}/access_tokens"
        ):
            claims = self._claims(bearer)
            now = time.time()
            if (
                claims is None
                or claims.get("iss") != str(self.app_id)
                or not claims["iat"] <= now + 1
                or not now < claims["exp"] <= now + 600
            ):
                return httpx.Response(
                    401, json={"message": "A JSON web token could not be decoded"}
                )
            if self.fail_mint is not None:
                status, message = self.fail_mint
                return httpx.Response(status, json={"message": message})
            body = json.loads(request.content)
            self.issued += 1
            token = f"ghs_{self.issued:04d}" + "Tk" * 17
            self.mints.append(
                {
                    "repositories": body.get("repositories"),
                    "permissions": body.get("permissions"),
                    "claims": claims,
                    "token": token,
                    "headers": dict(request.headers),
                }
            )
            self.live.add(token)
            reply: dict[str, Any] = {
                "token": token,
                "expires_at": "2099-01-01T00:00:00Z",
                "repository_selection": "selected",
                "repositories": [{"name": n} for n in body.get("repositories", [])],
                "permissions": body.get("permissions"),
                **self.grant,
            }
            return httpx.Response(201, json=reply)
        if request.method == "DELETE" and path == "/installation/token":
            if self.fail_revoke:
                return httpx.Response(500, json={"message": "boom"})
            if bearer not in self.live:
                return httpx.Response(401, json={"message": "Bad credentials"})
            self.live.discard(bearer)
            self.revoked.append(bearer)
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "Not Found"})
