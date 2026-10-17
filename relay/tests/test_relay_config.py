"""Settings validation: the anon key guard, URL checks, secrets kept out of repr."""

from __future__ import annotations

import base64
import json

import pytest

from resonant_relay.config import ConfigError, check_anon_key, load, parse_duration, supabase_origin


def jwt(role: str) -> str:
    def part(d: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{part({'alg': 'HS256'})}.{part({'role': role})}.c2ln"


BASE = {
    "SUPABASE_URL": "https://proj.supabase.co",
    "SUPABASE_ANON_KEY": jwt("anon"),
    "RELAY_STATUS_TOKEN": "tok-secret-1",
    "NTFY_TOPIC": "topic-secret-2",
}


def test_defaults():
    s = load(BASE)
    assert s.read_mode == "rpc"
    assert (s.check_every, s.stale_after, s.read_fail_after) == (60, 300, 300)
    assert s.ntfy_url == "https://ntfy.sh"
    assert s.port == 8080


def test_secrets_stay_out_of_repr():
    s = load({**BASE, "WEBHOOK_URL": "https://hooks.example/secret-3", "NTFY_TOKEN": "tk-secret-4"})
    text = repr(s)
    for secret in ("tok-secret-1", "topic-secret-2", "secret-3", "tk-secret-4"):
        assert secret not in text
        assert any(secret in x for x in s.secrets())


@pytest.mark.parametrize(
    "key",
    [jwt("service_role"), jwt("authenticated"), "sb_secret_abc123", "", "has space", "a;b"],
)
def test_anon_key_guard_refuses_secret_or_odd_keys(key: str):
    with pytest.raises(ConfigError):
        check_anon_key(key)


@pytest.mark.parametrize("key", [jwt("anon"), "sb_publishable_abc123", "plainkey"])
def test_anon_key_guard_accepts_public_keys(key: str):
    assert check_anon_key(key) == key


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("https://proj.supabase.co", "https://proj.supabase.co"),
        ("https://proj.supabase.co/", "https://proj.supabase.co"),
        ("https://h.example:8443", "https://h.example:8443"),
    ],
)
def test_origin(raw: str, want: str):
    assert supabase_origin(raw) == want


@pytest.mark.parametrize(
    "raw",
    [
        "http://proj.supabase.co",
        "https://proj.supabase.co/rest/v1",
        "https://u:p@proj.supabase.co",
        "https://proj.supabase.co?x=1",
        "https://proj.supabase.co; script-src *",
        "https://a.co https://evil.co",
        "proj.supabase.co",
    ],
)
def test_origin_rejects_anything_but_an_https_origin(raw: str):
    with pytest.raises(ConfigError):
        supabase_origin(raw)


@pytest.mark.parametrize(("raw", "want"), [("300", 300), ("45s", 45), ("5m", 300), ("1h", 3600)])
def test_durations(raw: str, want: float):
    assert parse_duration("X", raw) == want


@pytest.mark.parametrize("raw", ["0", "-1", "5 minutes", "", "1.5m"])
def test_bad_durations(raw: str):
    with pytest.raises(ConfigError):
        parse_duration("X", raw)


@pytest.mark.parametrize(
    "change",
    [
        {"SUPABASE_URL": ""},
        {"RELAY_STATUS_TOKEN": ""},
        {"RELAY_READ": "table"},  # no RELAY_READ_KEY
        {"RELAY_READ": "magic"},
        {"NTFY_TOPIC": ""},  # no channel at all
        {"NTFY_TOPIC": "a/b"},
        {"NTFY_URL": "http://ntfy.example"},
        {"WEBHOOK_URL": "http://hooks.example/x"},
        {"PORT": "http"},
        {"STALE_AFTER": "soon"},
    ],
)
def test_bad_settings(change: dict[str, str]):
    with pytest.raises(ConfigError):
        load({**BASE, **change})


def test_error_messages_never_echo_values():
    for change in ({"WEBHOOK_URL": "http://hooks.example/secret-9"}, {"NTFY_TOPIC": "sec/ret-9"}):
        with pytest.raises(ConfigError) as e:
            load({**BASE, **change})
        assert "secret-9" not in str(e.value) and "ret-9" not in str(e.value)


def test_table_mode_and_webhook_only():
    s = load(
        {
            **BASE,
            "RELAY_READ": "table",
            "RELAY_READ_KEY": "rk",
            "NTFY_TOPIC": "",
            "WEBHOOK_URL": "https://hooks.example/x",
            "STALE_AFTER": "10m",
        }
    )
    assert s.read_mode == "table" and s.stale_after == 600 and s.read_fail_after == 600
