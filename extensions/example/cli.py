#!/usr/bin/env python3
"""Example extension CLI. Tools print one JSON object on stdout; Resonant parses it."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# emit.py is the SDK's stdlib-only helper, copied next to this script. Under `python -I` the
# script's directory is not on sys.path, so add it.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from emit import report

# Installed copies are read-only; writable state goes in the data dir Resonant provides.
DATA = Path(os.environ.get("RESONANT_EXT_DATA") or Path(__file__).parent)
NOTES = DATA / "notes.txt"
INCIDENT = DATA / "incident"  # "failing" or "resolved", written by the `incident` tool
PNL_BUCKET = 500


def pnl_today(hour: int) -> int:
    """A fake, deterministic day's PnL in dollars (-3000..2999), a function of the hour."""
    return (hour * 7919) % 6000 - 3000


def money(dollars: int) -> str:
    return f"{'-' if dollars < 0 else '+'}${abs(dollars) / 1000:.1f}k"


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping")
    sub.add_parser("health")
    sub.add_parser("pnl")
    sub.add_parser("incident-check")
    i = sub.add_parser("incident")
    i.add_argument("--state", required=True, choices=["failing", "resolved"])
    t = sub.add_parser("time")
    t.add_argument("--tz", default="UTC")
    n = sub.add_parser("note")
    n.add_argument("--text", required=True)
    args = parser.parse_args()

    if args.cmd == "time":
        out = {"now": datetime.now(ZoneInfo(args.tz)).isoformat(), "tz": args.tz}
    elif args.cmd == "note":
        with NOTES.open("a") as f:
            f.write(args.text.replace("\n", " ") + "\n")
        out = {"appended": True}
    elif args.cmd == "ping":
        # One event per due time; notify.on: [ping] texts it as "[example] ping".
        due = os.environ.get("RESONANT_JOB_DUE_AT", "manual")
        out = {
            "ok": True,
            "events": [{"type": "ping", "text": "ping", "dedupe_key": f"ping:{due}"}],
        }
    elif args.cmd == "pnl":
        # The title has the exact figure; `state` has the $500 bucket, which is what decides
        # whether it is news. Normal urgency: it rides the hourly pulse, never a text on its own.
        due = os.environ.get("RESONANT_JOB_DUE_AT")
        pnl = pnl_today(datetime.fromisoformat(due).hour if due else datetime.now().hour)
        bucket = round(pnl / PNL_BUCKET) * PNL_BUCKET
        report(
            "example/pnl/today",
            f"PnL {money(pnl)} today",
            urgency="normal",
            state={"bucket": bucket},
        )
        out = {"ok": True, "summary": f"PnL {money(pnl)}, bucket {bucket}"}
    elif args.cmd == "incident":
        # Tools don't persist events, only job runs do, so this just records the state;
        # the `incident` job picks it up within 5 minutes (or run `/ext run example/incident`).
        INCIDENT.write_text(args.state)
        out = {"ok": True, "summary": f"demo incident set to {args.state}"}
    elif args.cmd == "incident-check":
        state = INCIDENT.read_text().strip() if INCIDENT.exists() else ""
        if state == "failing":
            # Same state every run: told once. Urgent: texted at once, at any hour.
            report(
                "example/incident/demo",
                "Demo service failing: checkout 5xx",
                urgency="urgent",
                state={"status": "failing"},
            )
        elif state == "resolved":
            report(
                "example/incident/demo",
                "Demo service recovered",
                urgency="urgent",
                state={"status": "resolved"},
                clear=True,
            )
            INCIDENT.unlink()  # a clear is reported once
        out = {"ok": True, "summary": state or "no incident"}
    else:
        out = {"ok": True}
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
