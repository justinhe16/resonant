"""Read-only go-live checks for ``scripts/bootstrap.sh --check``.

Loads config and principals with the daemon's own loaders and prints one line per
check: ``STATUS<TAB>REQUIRED<TAB>STEP<TAB>DETAIL`` (STATUS is PASS, FAIL or SKIP ...).
It never writes anything: no files, no database, no network. See docs/go-live.md.
"""

from __future__ import annotations

from pathlib import Path

# The values config/*.example.yaml ship with. Seeded config still holding them is not live.
EXAMPLE_SELF_HANDLE = "resonant.agent@icloud.com"
EXAMPLE_IDENTITIES = frozenset({"imessage:+15550000000", "imessage:owner@icloud.com"})
NOT_YET = "SKIP (available after Phase 1)"
DEFAULT_CHAT_DB = "~/Library/Messages/chat.db"


def out(status: str, required: bool, step: str, detail: str = "") -> None:
    print(f"{status}\t{int(required)}\t{step}\t{detail}", flush=True)


def _err(e: Exception) -> str:
    first = (str(e).splitlines() or [""])[0]
    return f"{type(e).__name__}: {first}"


def main() -> None:
    from resonant.config import load_settings
    from resonant.principals import load_principals

    try:
        settings = load_settings()
    except Exception as e:  # any load or validation error is a FAIL, not a crash
        out("FAIL", True, "config.yaml loads", _err(e))
        return
    cfg = settings.home / "config.yaml"
    if cfg.is_file():
        out("PASS", True, "config.yaml loads", str(cfg))
    else:
        out("FAIL", True, "config.yaml loads", f"{cfg} missing; run scripts/bootstrap.sh")

    out("PASS" if settings.dry_run else "FAIL", True, "dry_run is true", f"{settings.dry_run}")

    im = settings.channels.imessage
    handle = im.self_handle
    if not handle:
        out("FAIL", True, "imessage self_handle set", "channels.imessage.self_handle is empty")
    elif handle == EXAMPLE_SELF_HANDLE:
        out("FAIL", True, "imessage self_handle set", f"{handle} is the example value")
    else:
        out("PASS", True, "imessage self_handle set", handle)

    # `enabled` / `db_path` arrive with the iMessage channel; detect them at runtime.
    enabled: object = getattr(im, "enabled", None)
    if enabled is None:
        out(NOT_YET, False, "imessage channel enabled")
    else:
        out("PASS", False, "imessage channel enabled", f"enabled: {enabled} (turn on in step 6)")

    try:
        principals = load_principals(settings.principals_path)
        owner = principals.principals[principals.owner]
        handles = [i for i in owner.identities if i.startswith("imessage:")]
        real = [i for i in handles if i not in EXAMPLE_IDENTITIES]
        if real:
            out("PASS", True, "owner has imessage identity", f"{len(real)} handle(s)")
        elif handles:
            out("FAIL", True, "owner has imessage identity", "only the example handles listed")
        else:
            out("FAIL", True, "owner has imessage identity", "no imessage: identity on owner")
    except Exception as e:
        out("FAIL", True, "owner has imessage identity", _err(e))

    db_path: object = getattr(im, "db_path", None)
    chat_db = Path(str(db_path or DEFAULT_CHAT_DB)).expanduser()
    try:
        with chat_db.open("rb") as f:
            f.read(1)
        out("PASS", True, "Full Disk Access (chat.db)", f"{chat_db} readable")
    except OSError as e:
        out("FAIL", True, "Full Disk Access (chat.db)", f"{chat_db}: {e.strerror or e}")

    if settings.health.healthchecks_url:
        out("PASS", True, "healthchecks URL set", "health.healthchecks_url is set")
    else:
        out("FAIL", True, "healthchecks URL set", "health.healthchecks_url is empty")


if __name__ == "__main__":
    main()
