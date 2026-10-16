"""Per-run GitHub App tokens: the JWT, the mint request (repo scoping and permissions), revoke
and error text. A fake GitHub API over ``httpx.MockTransport``; no network, no Keychain."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from github_app_fakes import (
    APP_ID,
    INSTALLATION_ID,
    FakeGitHubApi,
    private_pem,
    private_pem_one_line,
)

from resonant.config import GitHubAppConfig, RunnerConfig
from resonant.runner import github_app
from resonant.runner.github_app import GitHubApp, GitHubAppError, make_jwt, parse_private_key
from resonant.secrets import GITHUB_APP_KEY_REF, FakeKeychain, SecretStore

CFG = GitHubAppConfig(app_id=APP_ID, installation_id=INSTALLATION_ID)


def make(api: FakeGitHubApi, *, key: str | None = None) -> tuple[GitHubApp, FakeKeychain]:
    kc = FakeKeychain()
    if key is not None:
        kc.items[GITHUB_APP_KEY_REF] = key
    return GitHubApp(CFG, SecretStore(kc), transport=api.transport()), kc


def test_config_is_two_public_numbers() -> None:
    assert RunnerConfig().github_app is None
    cfg = RunnerConfig.model_validate({"github_app": {"app_id": 12, "installation_id": 34}})
    assert cfg.github_app == GitHubAppConfig(app_id=12, installation_id=34)
    with pytest.raises(ValueError):
        GitHubAppConfig.model_validate({"app_id": 0, "installation_id": 1})
    with pytest.raises(ValueError):
        GitHubAppConfig.model_validate({"app_id": 1, "installation_id": 2, "private_key": "x"})


def test_permissions_drop_workflows_on_request() -> None:
    full = github_app.permissions(workflows=True)
    assert full == {
        "contents": "write", "pull_requests": "write", "issues": "write", "workflows": "write",
        "actions": "write", "checks": "read", "statuses": "read", "metadata": "read",
    }  # fmt: skip
    assert "administration" not in full
    assert "workflows" not in github_app.permissions(workflows=False)


def test_the_jwt_is_rs256_with_the_app_as_issuer() -> None:
    key = parse_private_key(private_pem())
    now = 1_800_000_000.0
    jwt = make_jwt(APP_ID, key, now)
    head, claims, sig = jwt.split(".")

    def unb64(t: str) -> bytes:
        return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))

    assert json.loads(unb64(head)) == {"alg": "RS256", "typ": "JWT"}
    body = json.loads(unb64(claims))
    assert body["iss"] == str(APP_ID) and body["exp"] - int(now) <= 600 and body["iat"] < now
    key.public_key().verify(
        unb64(sig),
        f"{head}.{claims}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_keys_parse_as_pem_one_line_base64_hex_or_der() -> None:
    pem = private_pem()
    der = parse_private_key(pem).private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    for text in (
        pem,
        pem.replace("\n", "\\n"),
        private_pem_one_line(),
        pem.encode().hex(),
        base64.b64encode(der).decode(),
        "  " + pem + "\n",
    ):
        assert isinstance(parse_private_key(text), rsa.RSAPrivateKey)


@pytest.mark.parametrize(
    "bad", ["", "not a key", "-----BEGIN PRIVATE KEY-----\nabc\n-----END-----"]
)
def test_bad_keys_are_refused_without_quoting_them(bad: str) -> None:
    with pytest.raises(GitHubAppError) as e:
        parse_private_key(bad)
    assert bad.strip() not in str(e.value) or not bad.strip()


def test_an_ec_key_is_not_an_rsa_key() -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(GitHubAppError, match="isn't an RSA key"):
        parse_private_key(pem)


async def test_mint_scopes_to_one_repo_with_the_permissions() -> None:
    api = FakeGitHubApi()
    app, _kc = make(api, key=private_pem_one_line())
    token = await app.mint("proj", workflows=True)
    assert token.token == api.tokens[0] and token.token.startswith("ghs_")
    [mint] = api.mints
    assert mint["repositories"] == ["proj"]  # the name, not owner/name
    assert mint["permissions"] == github_app.permissions(workflows=True)
    assert mint["headers"]["authorization"].startswith("Bearer ey")  # a JWT, verified by the fake
    assert ("POST", f"/app/installations/{INSTALLATION_ID}/access_tokens") in api.requests
    assert "ghs_" not in repr(token)  # never in a log line

    await app.mint("other", workflows=False)
    assert "workflows" not in api.mints[1]["permissions"]
    assert api.mints[1]["repositories"] == ["other"]


async def test_the_key_is_read_from_the_keychain_each_time_and_never_kept() -> None:
    api = FakeGitHubApi()
    app, kc = make(api, key=private_pem())
    await app.mint("proj", workflows=True)
    await app.mint("proj", workflows=True)
    gets = [c for c in kc.calls if "-w" in c.argv]
    assert len(gets) == 2 and all(GITHUB_APP_KEY_REF in c.argv for c in gets)
    assert not any("BEGIN" in a for c in kc.calls for a in c.argv)  # never in argv
    assert not any("_key" in k or "pem" in k for k in vars(app))
    # the key goes only into the JWT: the request body and URL have no key material
    del kc.items[GITHUB_APP_KEY_REF]
    with pytest.raises(GitHubAppError, match="GitHub App key can't be read"):
        await app.mint("proj", workflows=True)


async def test_mint_errors_name_the_status_not_the_secrets() -> None:
    api = FakeGitHubApi()
    app, _ = make(api, key=private_pem())
    api.fail_mint = (404, "Not Found")
    with pytest.raises(GitHubAppError, match=r"HTTP 404.*installed on proj"):
        await app.mint("proj", workflows=True)
    api.fail_mint = (500, "Server\n  exploded")
    with pytest.raises(GitHubAppError, match=r"HTTP 500\): Server exploded"):
        await app.mint("proj", workflows=True)
    wrong, _ = make(FakeGitHubApi(app_id=1), key=private_pem())
    with pytest.raises(GitHubAppError, match="HTTP 401"):  # the fake rejects a foreign JWT
        await wrong.mint("proj", workflows=True)

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to api.github.com")

    kc = FakeKeychain({GITHUB_APP_KEY_REF: private_pem()})
    down = GitHubApp(CFG, SecretStore(kc), transport=httpx.MockTransport(boom))
    with pytest.raises(GitHubAppError, match="couldn't reach GitHub"):
        await down.mint("proj", workflows=True)
    with pytest.raises(GitHubAppError, match="couldn't reach GitHub"):
        await down.revoke("ghs_x")


async def test_a_reply_without_a_token_is_an_error() -> None:
    def odd(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"expires_at": "x"})

    kc = FakeKeychain({GITHUB_APP_KEY_REF: private_pem()})
    app = GitHubApp(CFG, SecretStore(kc), transport=httpx.MockTransport(odd))
    with pytest.raises(GitHubAppError, match="had no token"):
        await app.mint("proj", workflows=True)


async def test_revoke_deletes_the_installation_token_as_itself() -> None:
    api = FakeGitHubApi()
    app, _ = make(api, key=private_pem())
    token = (await app.mint("proj", workflows=True)).token
    assert api.live == {token}
    await app.revoke(token)
    assert api.live == set() and api.revoked == [token]
    await app.revoke(token)  # already dead: a 401 is fine
    api.fail_revoke = True
    with pytest.raises(GitHubAppError, match=r"revoking.*HTTP 500"):
        await app.revoke(token)


# --- retries, messages, the grant check, the bot ---------------------------------------------


def quick(api: FakeGitHubApi, *, key: str | None = None) -> GitHubApp:
    kc = FakeKeychain({GITHUB_APP_KEY_REF: key or private_pem()})
    return GitHubApp(CFG, SecretStore(kc), transport=api.transport(), backoff_s=0)


async def test_mint_and_revoke_retry_a_transient_failure_three_times() -> None:
    api = FakeGitHubApi()
    app = quick(api)
    api.flaky = 2
    token = (await app.mint("proj", workflows=True)).token
    assert api.requests.count(("POST", f"/app/installations/{INSTALLATION_ID}/access_tokens")) == 3
    api.flaky = 2
    await app.revoke(token)
    assert api.revoked == [token]
    api.flaky = 3  # all three tries fail
    with pytest.raises(GitHubAppError, match="HTTP 503"):
        await app.mint("proj", workflows=True)
    # a 4xx answer is final: no retry
    api.fail_mint = (403, "nope")
    before = len(api.requests)
    with pytest.raises(GitHubAppError, match="HTTP 403"):
        await app.mint("proj", workflows=True)
    assert len(api.requests) == before + 1


async def test_network_errors_are_retried_then_reported() -> None:
    calls = 0

    def down(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow")

    kc = FakeKeychain({GITHUB_APP_KEY_REF: private_pem()})
    app = GitHubApp(CFG, SecretStore(kc), transport=httpx.MockTransport(down), backoff_s=0)
    with pytest.raises(GitHubAppError, match=r"couldn't reach GitHub \(ReadTimeout\)"):
        await app.mint("proj", workflows=True)
    assert calls == 3


@pytest.mark.parametrize(
    ("status", "message", "hint"),
    [
        (403, "Resource not accessible by integration", "does the app have the permissions"),
        (404, "Not Found", "is the app installed on proj"),
        (422, "Validation Failed: permission workflows", "may lack a permission asked for"),
    ],
)
async def test_mint_errors_carry_githubs_message_and_a_hint(
    status: int, message: str, hint: str
) -> None:
    api = FakeGitHubApi()
    app = quick(api)
    api.fail_mint = (status, message)
    with pytest.raises(GitHubAppError) as e:
        await app.mint("proj", workflows=True)
    assert f"HTTP {status}): {message}" in str(e.value) and hint in str(e.value)


@pytest.mark.parametrize(
    ("grant", "why"),
    [
        ({"repository_selection": "all"}, "more than the one repository"),
        ({"repositories": []}, "different set of repositories"),
        ({"repositories": [{"name": "proj"}, {"name": "x"}]}, "different set of repositories"),
        ({"permissions": None}, "lists no permissions"),
        ({"permissions": {"contents": "write", "pull_requests": "write", "metadata": "admin"}},
         "metadata: admin"),
        ({"permissions": {"contents": "write", "pull_requests": "write", "members": "read"}},
         "members: read"),
        ({"permissions": {"contents": "write"}}, "lacks pull_requests"),
    ],
)  # fmt: skip
async def test_a_mint_reply_that_isnt_the_request_is_revoked_and_refused(
    grant: dict[str, object], why: str
) -> None:
    api = FakeGitHubApi()
    app = quick(api)
    api.grant = grant
    with pytest.raises(GitHubAppError, match=why):
        await app.mint("proj", workflows=False)
    assert api.live == set() and len(api.revoked) == 1  # the odd token did not survive


async def test_a_lower_grant_than_asked_for_is_fine_for_the_optional_ones() -> None:
    api = FakeGitHubApi()
    app = quick(api)
    perms = github_app.permissions(workflows=True)
    perms.pop("actions")
    perms["checks"] = "none"
    api.grant = {"permissions": perms}
    assert (await app.mint("proj", workflows=True)).token


async def test_bot_login_is_the_apps_slug_and_asked_once() -> None:
    api = FakeGitHubApi()
    app = quick(api)
    assert await app.bot_login() == "resonant-runner[bot]"
    assert await app.bot_login() == "resonant-runner[bot]"
    assert api.requests.count(("GET", "/app")) == 1
    bad = quick(FakeGitHubApi(app_id=1))
    with pytest.raises(GitHubAppError, match="HTTP 401"):
        await bad.bot_login()
