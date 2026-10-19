#!/usr/bin/env python3
"""gcal extension CLI: ``draft``, a proposed event checked and laid out (CAL-1).

It never reaches Google and writes nothing: no network, no secrets, no files. The model
drafts an event here, the owner sees what ``create_event`` would do (who would be emailed),
and only ``create_event`` (L2, asks first) writes it. Prints one JSON object on stdout, and
exits 0 for a draft with problems too (``ok: false``), so the model can fix and retry.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime

MAX_ATTENDEES = 50
MAX_SUMMARY = 300
MAX_DAYS = 31  # a longer "event" is almost always a typo in the dates
# A plain address check: local@domain.tld, no spaces, angle brackets or quotes.
_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")


def _when(value: str) -> datetime | None:
    """An ISO 8601 date-time (``2026-10-20T15:00:00-07:00``, ``…Z``) or a date (all day)."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def draft(summary: str, start: str, end: str, attendees: str) -> dict[str, object]:
    problems: list[str] = []
    title = " ".join(summary.split())
    if not title:
        problems.append("summary is empty")
    elif len(title) > MAX_SUMMARY:
        problems.append(f"summary is over {MAX_SUMMARY} characters")
    begin, finish = _when(start), _when(end)
    if begin is None:
        problems.append("start isn't an ISO 8601 date or date-time")
    if finish is None:
        problems.append("end isn't an ISO 8601 date or date-time")
    all_day = False
    minutes: int | None = None
    if begin is not None and finish is not None:
        if (begin.tzinfo is None) != (finish.tzinfo is None):
            problems.append("start and end must both have a UTC offset, or neither")
        else:
            all_day = "T" not in start.upper() and "T" not in end.upper()
            span = finish - begin
            minutes = int(span.total_seconds() // 60)
            if minutes <= 0:
                problems.append("end must be after start")
            elif span.days > MAX_DAYS:
                problems.append(f"the event is longer than {MAX_DAYS} days; check the dates")
    people = [a.strip() for a in attendees.replace(";", ",").split(",") if a.strip()]
    unique = list(dict.fromkeys(p.lower() for p in people))
    if bad := [p for p in unique if not _EMAIL.fullmatch(p)]:
        problems.append(f"not an email address: {', '.join(bad[:5])}")
    if len(unique) > MAX_ATTENDEES:
        problems.append(f"more than {MAX_ATTENDEES} attendees")
    if problems:
        return {"ok": False, "draft": True, "problems": problems}
    if unique:
        invites = (
            f"create_event would email an invite to {len(unique)} "
            f"{'person' if len(unique) == 1 else 'people'} ({', '.join(unique)})."
        )
    else:
        invites = "No attendees: create_event would email nobody."
    return {
        "ok": True,
        "draft": True,
        "summary": title,
        "start": start.strip(),
        "end": end.strip(),
        "all_day": all_day,
        "duration_minutes": minutes,
        "attendees": unique,
        "note": (
            "Draft only: nothing was saved to the calendar or sent. "
            f"{invites} create_event asks the owner first."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--summary", required=True)
    d.add_argument("--start", required=True)
    d.add_argument("--end", required=True)
    d.add_argument("--attendees", default="")
    args = parser.parse_args()
    out = draft(args.summary, args.start, args.end, args.attendees)
    json.dump(out, sys.stdout, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
