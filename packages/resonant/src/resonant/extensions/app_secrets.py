"""Extension secrets minted by the instance's GitHub App (PER-336).

An instance whose only GitHub credential is its App (``runner.github_app``) can back an
extension's secret with it instead of a static Keychain token: ``extensions.secret_sources``
maps the ref (``github/token``) to ``github_app``. Every call that declares that secret then
gets a fresh installation token, minted just before the call and revoked when it ends:

- The token's reach comes from the call's spec (``ToolSpec.github_app``, the manifest's
  ``github_app:`` block): one repo, the ``owner/name`` in the arg it names, or every repo the
  installation sees when it names none (a job, a health check); and the permissions it lists,
  read on what a PR read needs by default. Never administration.
- A repo arg that isn't ``owner/name`` mints nothing: the call fails as a missing secret
  would, before anything runs.
- The value reaches the extension exactly like a Keychain secret (fd 4, redacted from every
  output by value). The extension can't tell the two apart, except that GitHub installation
  tokens start with ``ghs_``.
- Revoking is best effort: a failure is logged (no value) and the token dies within the hour.

The core stays instance-agnostic: nothing here knows about any extension. Personal Resonant,
with no ``secret_sources``, reads every extension secret from the Keychain as before.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from resonant.runner.github_app import GitHubApp, GitHubAppError
from resonant.secrets import MintScope, SecretUnavailable
from resonant_sdk.tool import APP_READ_DEFAULT

log = logging.getLogger(__name__)

_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


class GitHubAppMinter:
    """A ``SecretMinter`` for the refs in ``refs``, minting with ``app``."""

    def __init__(self, app: GitHubApp, refs: Iterable[str]) -> None:
        self.app = app
        self.refs = frozenset(refs)

    def mints(self, ref: str) -> bool:
        return ref in self.refs

    async def mint(self, ref: str, scope: MintScope) -> str:
        perms: dict[str, str] = dict(scope.permissions) or {
            k: v for k, v in APP_READ_DEFAULT.items()
        }
        try:
            if scope.repo is None:
                token = await self.app.mint_wide(perms)
            elif _REPO.fullmatch(scope.repo) and {".", ".."}.isdisjoint(scope.repo.split("/")):
                token = await self.app.mint_scoped(scope.repo, perms)
            else:
                raise SecretUnavailable(
                    ref, "unavailable: the GitHub App mints for one owner/name repo, and the "
                    "call named none"
                )  # fmt: skip
        except GitHubAppError as e:
            raise SecretUnavailable(ref, f"unavailable: {e}") from None
        return token.token

    async def release(self, value: str) -> None:
        try:
            await self.app.revoke(value)
        except GitHubAppError as e:
            # The token expires within the hour anyway; the error names no value.
            log.warning("extension token not revoked: %s", e)
        except Exception as e:  # release never raises into the caller's finally
            log.warning("extension token not revoked: %s", type(e).__name__)
