#!/usr/bin/env python3
"""Example extension CLI. Tools print one JSON object on stdout; Resonant parses it."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

NOTES = Path(__file__).with_name("notes.txt")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping")
    sub.add_parser("health")
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
    else:
        out = {"ok": True}
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
