"""Real iMessage self-test: osascript -> Messages.app -> chat.db.

Skipped unless ``RESONANT_IT_IMESSAGE=1``. Needs Messages signed in as
``channels.imessage.self_handle``, Automation (Python -> Messages) and Full Disk Access::

    RESONANT_IT_IMESSAGE=1 uv run pytest -m integration tests/test_imessage_integration.py -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from resonant.components import deny_all_principals
from resonant.config import DEFAULT_HOME, load_settings
from resonant.gateway.imessage import IMessageChannel
from resonant.store.db import open_db

# Captured at import: the autouse conftest fixture isolates RESONANT_* env vars per test.
_ENABLED = os.environ.get("RESONANT_IT_IMESSAGE") == "1"
_HOME = Path(os.environ.get("RESONANT_HOME", DEFAULT_HOME)).expanduser()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ENABLED, reason="set RESONANT_IT_IMESSAGE=1 to send a real iMessage"),
]


async def test_real_selftest(tmp_path: Path) -> None:
    settings = load_settings(home=_HOME)
    assert settings.channels.imessage.self_handle, "set channels.imessage.self_handle"
    conn = open_db(tmp_path / "it.db")  # a scratch store: never touches the daemon's cursor
    try:
        channel = IMessageChannel(settings.channels.imessage, deny_all_principals(), conn)
        result = await channel.self_test()
    finally:
        conn.close()
    print(result)
    assert result.ok, result.detail
