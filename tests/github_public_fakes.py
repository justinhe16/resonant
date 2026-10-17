"""A fake of GitHub's unauthenticated REST API for ``PublicGitHub`` (GT-1): no network.

``FakePublicApi(repos)`` answers ``GET /repos/{owner}/{name}`` (200 for a public repo, 404
for anything else, like GitHub does for a private one without a token) and
``GET /repos/{owner}/{name}/branches/{branch}``. ``status`` forces the next answers (a rate
limit is 403). It records every request, so a test can check that no credential was sent.
"""

from __future__ import annotations

from typing import Any

import httpx

from resonant.runner.resolve import PublicGitHub


class FakePublicApi:
    def __init__(
        self, repos: dict[str, dict[str, Any]] | None = None, *, protected: bool = True
    ) -> None:
        # "owner/name" (any case) -> {"full_name", "default_branch", "private"}
        self.repos = {k.lower(): v for k, v in (repos or {}).items()}
        self.protected = protected
        self.status: int | None = None
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status is not None:
            return httpx.Response(self.status, json={"message": "API rate limit exceeded"})
        parts = request.url.path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "repos":
            return httpx.Response(404, json={"message": "Not Found"})
        repo = self.repos.get(f"{parts[1]}/{parts[2]}".lower())
        if repo is None or repo.get("private"):
            return httpx.Response(404, json={"message": "Not Found"})
        if len(parts) == 3:
            return httpx.Response(200, json=repo)
        if parts[3] == "branches":
            return httpx.Response(
                200, json={"name": "/".join(parts[4:]), "protected": self.protected}
            )
        return httpx.Response(404, json={"message": "Not Found"})

    def github(self) -> PublicGitHub:
        return PublicGitHub(transport=httpx.MockTransport(self.handler))


def public(slug: str, base: str = "main", *, private: bool = False) -> dict[str, Any]:
    return {"full_name": slug, "default_branch": base, "private": private}
