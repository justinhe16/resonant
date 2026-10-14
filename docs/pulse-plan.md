# Pulse plan (Phase 3.5): the hourly briefing, urgent interrupts, extension reports

Status: **approved** by the owner (2026-10-14). Built as P35-1 to P35-6 with robot-surf: worktree, tests, PR, CI, code-review agent, squash-merge to `main`. It builds on Phase 3 ([`phase3-plan.md`](phase3-plan.md)): the scheduler, jobs, the fd-3 event pipe, the notifier and `core/briefing`.

**Goal.** Resonant tells the owner things when they matter, not just at 08:00:
- **Urgent** reports text right away, at any hour. Example: "Dori is failing, triaging".
- **Normal** reports are batched into an hourly "btw" message. Example: PnL moved.
- **Low** reports wait for the 08:00 briefing.
- The 08:00 briefing always sends, even when it's `Nothing on today.`
- Nothing is said twice: an item is mentioned again only when its state changes.

Any extension can report items, so future extensions (Dori on-call, a trading bot) plug in with no core changes.

---

## 1. Owner decisions

| # | Question | Decision |
|---|---|---|
| Q1 | Interruptions | `urgent` texts immediately, **including in quiet hours**. `normal` goes in the next hourly pulse. `low` waits for 08:00. |
| Q2 | Stale detection | Rules, not a model: a key plus a fingerprint of the item's state. A model can rank later (the [nudges follow-up](followups.md#proactive-nudges-btw-)). |
| Q3 | Quiet hours | 00:00–06:00 local. Only `urgent` goes out. Normal items held over show in the 06:00 pulse. |
| Q4 | Hourly cap | None. A pulse is one message an hour at most, and only when something changed. (The existing 20/h per-extension cap on immediate sends stays, as flood protection against a broken extension.) |
| Q5 | Re-mentions | A pending approval is mentioned again once, about 4 hours after it was asked. After that it waits for 08:00. |

## 2. The line of communication

Pull, over the pipe Phase 3 already has. Nothing outside the Mini pushes in.

1. The extension declares jobs as today (cron, at most every 5 minutes) and, new, a `report:` section in `resonant.yaml` that sets the **highest urgency it may use**. That grant is shown on the `ext add` review screen, so an extension can only interrupt you if you approved it to.
2. Each run checks its world (the Dori API, the trading bot's ledger) and writes **reports** to fd 3 with the SDK helper:
   ```python
   from emit import report
   report("dori/incident/INC-412", "Dori failing: checkout 5xx, triaging",
          urgency="urgent", state={"status": "triaging", "sev": 2})
   ...
   report("dori/incident/INC-412", "Dori recovered", urgency="normal",
          state={"status": "resolved"}, clear=True)
   report("trader/pnl/today", "PnL -$1.8k today", urgency="normal",
          state={"bucket": -2})        # coarse state = only "real" changes count
   ```
3. The daemon stores each report as an event (`ext.<name>.item`) in the run's transaction, then the loop routes it to the **pulse** (not the notifier).
4. The pulse upserts the item, clamps its urgency to the manifest grant, and compares its fingerprint (sha256 of the canonical `{title, state}` JSON, or `state` alone when given) with the last one **told** to the owner:
   - same: stale, nothing to say;
   - new or changed: `urgent` sends now, `normal` waits for the next pulse, `low` waits for 08:00.

Latency for urgent: one job period (≥ 5 minutes) plus a loop tick. A **local push endpoint** (a long-running bot calling the daemon directly, with a per-extension token) is a follow-up, for when 5 minutes is too slow.

The extension decides what counts as a real change by what it puts in `state` (bucket the PnL, leave timestamps out). The daemon never interprets `state`; it only hashes it.

## 3. Design

### D1. Items store (`store/pulse.py`, migration 005)

```sql
CREATE TABLE pulse_items (
    source          TEXT NOT NULL,      -- 'core' or the extension name
    key             TEXT NOT NULL,      -- 1..120 chars, [a-z0-9/._:-]
    title           TEXT NOT NULL,      -- sanitized, ≤ 200 chars
    urgency         TEXT NOT NULL CHECK (urgency IN ('urgent', 'normal', 'low')),
    fingerprint     TEXT NOT NULL,
    cleared         INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,      -- updated_at + ttl (default 24h, max 7d)
    told_fingerprint TEXT,              -- what the owner last heard
    told_at         TEXT,
    PRIMARY KEY (source, key)
);
```
- `upsert(...)` returns `new | changed | same`, compared with `told_fingerprint`, so A→B→A after B was told is a change again, and a report the owner never heard (paused, muted) is still pending.
- `pending(kind)`: untold, uncleared, unexpired items at an urgency.
- `mark_told(items, at)`: sets `told_fingerprint`/`told_at`.
- A `clear=True` report is told once (if it changed since the last told) and then drops out of every listing.
- Old rows (cleared or expired for 7 days) are pruned by the hourly job.

### D2. SDK and manifest
- `resonant_sdk/emit.py`: `report(key, title, *, urgency="normal", state=None, clear=False, ttl="24h")`, stdlib only, copied next to the script like `emit()`. It writes one `{"type": "item", ...}` event with a unique dedupe key, so re-reporting is never dropped at ingest. The pulse does the dedupe.
- Manifest: `report: {max_urgency: urgent | normal | low}` (default absent: reports are refused and audited `pulse.refused`). `item` is a reserved event type: it can't be in `notify.on`. `ext validate` and the install screen show the grant: `may interrupt you any time (urgent)`.
- Config: `pulse: {enabled: true, quiet_hours: "00:00-06:00", remention_after: "4h"}`.

### D3. Ingest and the urgent path (`extensions/pulse.py`)
`wiring.route_event` sends `ext.<name>.item` events to `Pulse.route`; other `ext:*` events still go to the notifier. For one item event:
1. Not from a loaded extension, no `report:` grant, or a bad payload: consumed, audited `pulse.refused`.
2. Clamp the urgency to the grant. Sanitize the title with the notifier's `render` rules (the anti-spoofing stays the same).
3. Upsert. `same`: done.
4. Urgent and new or changed, and not paused, muted, baselined or over the 20/h cap: send `[dori] 🔴 Dori failing: …` to the extension's approvers, with dedupe key `pulse:urgent:<event id>:<recipient>`, then `mark_told`. Otherwise it stays pending and the next pulse picks it up (held urgent items aren't lost; they ride the next pulse).

Exactly once comes from the same mechanism as notify: the event is stored before it's routed, the send is deduped by event id, and `mark_told` runs after the send.

The first run after install is a baseline (as for notify): items are stored as told, nothing is sent.

### D4. The hourly pulse and the 08:00 briefing (`core/pulse`)
- One core job, cron `0 * * * *` in `settings.timezone`, catch-up 50 minutes. It skips the briefing's hour (the briefing covers it), quiet hours, and paused.
- Core items are refreshed in-process first:
  - `core/health/<check>`: a degraded check (normal); cleared on recovery.
  - `core/approval/<code>`: an approval pending for `remention_after` (normal), one re-mention.
- It collects untold `normal` and `urgent` items (the urgent ones were held), from extensions that aren't muted, and sends one message if there are any:
  ```
  btw:
  [dori] ✓ Dori recovered
  [trader] PnL -$1.8k today
  ✋ A7 still waiting: restart ollama?
  ```
  Dedupe `pulse:<local date>:<HH>`, then `mark_told`. Nothing changed: nothing sent.
- `core/briefing` keeps its sections and adds an **Updates** section: every open (uncleared, unexpired) item at any urgency, changed-since-told first, up to 8 lines (`+N more`). It then marks them told. The 600-char cap becomes 1,000.
- The briefing's hard-coded GitHub line is replaced by a `github/summary` `low` item from the digest job.

### D5. Commands
- `/pulse`: what the next pulse would say, and how many items are open per source. Read-only.
- `/pulse now`: sends the pending pulse now (owner only, keyed per minute like `/ext run core/briefing`).
- An unknown `/<word>` that names an extension shipped in `extensions/` but not installed replies `github isn't installed. On the Mini: resonant ext add repo:github`.

## 4. Tickets

| Ticket | Title | Depends on | Wave |
|---|---|---|---|
| P35-1 | Items store + migration 005, SDK `report()`, manifest `report:` grant, `pulse:` config | — | 1 |
| P35-2 | Ingest + urgent path (`Pulse.route`, wiring, baseline, mute, pause, cap), install-screen grant line | 1 | 2 |
| P35-3 | `core/pulse` hourly job, core items, quiet hours, briefing **Updates** section | 1 | 2 |
| P35-4 | github digest → `github/summary` item (drop the hard-coded line); example extension gets a fake `pnl` reporter | 2, 3 | 3 |
| P35-5 | `/pulse`, `/pulse now`, "isn't installed" reply | 3 | 3 |
| P35-6 | e2e (urgent at 03:00, normal held to 06:00, A→B→A, crash between send and told, pause), docs, go-live step 13, `--check` row `pulse scheduled`, follow-ups | all | 4 |

## 5. Exit criteria (e2e, `tests/test_e2e_pulse.py`)

| # | Scenario | Pass condition |
|---|---|---|
| U1 | Urgent at 03:00 | Texted within one tick of the report, exactly once, even after a restart. |
| U2 | Normal at 03:00 | Nothing until the 06:00 pulse, then one `btw` message. |
| U3 | Same state reported every 5 minutes | Told once. A→B→A: told three times. |
| U4 | Nothing changed in an hour | No pulse message. |
| U5 | 08:00 | The briefing always sends, with the Updates section; the 08:00 pulse doesn't. |
| U6 | Paused | Nothing sent; after `/resume` the next pulse carries what changed, once. |
| U7 | Grant | An extension granted `normal` that reports `urgent` waits for the pulse. One with no grant is refused. |
| U8 | Muted | Its items are skipped while muted and not replayed after. |
| U9 | Approval re-mention | Once, at about 4h. |

## 6. Risks

- **An extension spams urgent.** Only with an `urgent` grant you approved; the 20/h cap; `/ext mute`; dedupe by fingerprint means a stuck "failing" says it once.
- **Spoofing.** Titles go through the notifier's sanitizer and a code-built `[name]` prefix, so a report can't look like an approval prompt.
- **Missed hour.** A daemon down over an hour catches up within 50 minutes; past that, the items stay pending and ride the next pulse.
- **State churn.** A `state` that includes a timestamp would make every report a change. The SDK docs and the example say to keep `state` coarse; `/pulse` shows per-source counts so a noisy source is easy to spot and mute.

## 7. Deferred (to `followups.md`)
- Local push endpoint for long-running processes (per-extension token, loopback only).
- Model ranking, "not useful" feedback and per-item snooze (the nudges follow-up).
- Status relay visibility of pending items.
