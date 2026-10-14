# Test tracker

What still needs a human to try, in one place. The full steps and expected replies are in [`go-live.md`](go-live.md). This file only tracks **status**. Deferred work (not tests) is in [`followups.md`](followups.md).

Legend:
- ✅ done.
- 📱 can be done from the phone, over iMessage, from anywhere.
- 🖥 needs the Mini's terminal.
- ❓ not confirmed yet.

Update a row when you try it, with the date and anything odd.

## Can do now, from the phone

| # | Test | Expect | Status |
|---|---|---|---|
| 1 | `/pulse` | `Nothing new since the last update.` | 📱 |
| 2 | `/pulse now` | `Nothing new to send.` | 📱 |
| 3 | `/ext list` | `core · built in` with `briefing` and `pulse`, each with a next time | 📱 |
| 4 | `/github prs` (before install) | `github isn't installed. On the Mini: resonant ext add repo:github` | 📱 |
| 5 | `/ext run core/briefing` | The briefing, or `Nothing on today.`, then `Briefing sent.` | 📱 |
| 6 | The 08:00 briefing arrives on its own | One message, every day, even if it's `Nothing on today.` | 📱 (wait for it) |
| 7 | Chat asks first (Stage B step 3): "please restart resonant" | `[A7] restart Resonant? Reply yes / no`; `no` → `Not done.` | 📱 ❓ |
| 8 | Approval re-mention: around **:35–:45** past an hour, send "please restart resonant" and **don't answer** | At the top of the hour: `btw:` / `✋ A7 still waiting: restart Resonant`. Then reply `no` (or let it expire at 30 minutes). Only approvals asked :30–:50 are caught (follow-up: an expiry reminder). | 📱 |
| 9 | Kill switch and pulse: `/kill`, `/pulse now`, `/resume` | `Paused: pulse not sent.`; `/resume` → `resumed: autonomy on, unpaused` | 📱 |
| 10 | Urgent path with the example extension (go-live step 13.4): `/ext run example/incident`, then `/example incident failing`, then `/example incident resolved` | `[example] 🔴 Demo service failing…` within 5 min, once; then `[example] ✓ Demo service recovered` | 📱 after 🖥 `resonant ext add repo:example` |
| 11 | Hourly "btw" with the example (step 13.5): `/ext run example/pnl` once, then wait | At a top of the hour: `btw:` / `[example] PnL …` only in hours where it moved; nothing 00:00–06:00 | 📱 after 🖥 install |

## Needs the Mini

| # | Test | Where | Status |
|---|---|---|---|
| 20 | GitHub token: `resonant secret set github/token`, then `check` | go-live 12, terminal 1 | 🖥 |
| 21 | `resonant ext add repo:github` (typed confirm) | go-live 12, terminal 2 | 🖥 |
| 22 | `scripts/bootstrap.sh --check` rows for the github pin, secret and jobs | go-live 12, terminal 4 | 🖥 |
| 23 | `/help github`, `/github prs`, "show my PRs" | go-live 12, iMessage 2–4 | 📱 after 21 |
| 24 | No token in logs or the DB (`grep`, `sqlite3`) | go-live 12, iMessage 5 | 🖥 |
| 25 | Watch: baseline silent, then a red and a green CI | go-live 12, iMessage 6–7 | 📱 after 21 |
| 26 | `/ext mute github 1h`, then `off` | go-live 12, iMessage 8 | 📱 after 21 |
| 27 | Next 08:00 briefing shows `Updates:` / `[github] 3 open PRs, …` | go-live 13.3 | 📱 after 21 |
| 28 | `/kill` skips jobs and kills running extension processes | go-live 12, iMessage 10 | 📱 + 🖥 (sqlite check) |
| 29 | Tamper test: edit the installed copy, then `/restart resonant`, then `resonant ext update github` | go-live 12, iMessage 11 | 🖥 |
| 30 | Crash mid-approval: `kill -9` the daemon before answering | go-live 11, Stage B 5 | 🖥 ❓ |
| 31 | Optional: github writes (`rerun_failed`, `merge_pr` with `yes D7`) | go-live 12, the writes | 📱 after a token with write access |
| 32 | Remove the example extension after tests 10–11 | `resonant ext remove example` | 🖥 |

## Needs real data or effort

| # | Test | Where | Status |
|---|---|---|---|
| 40 | Router evals on 30–50 real texts (≥ 90%) | go-live 9, [`followups.md`](followups.md#router-evals-on-real-texts-go-live-step-9) | deferred |

## Done

| Test | When |
|---|---|
| Phase 2 Stage A (notes, L1 codes, reminders, dry-run ops, discovery, chat, kill switch) | 2026-10-14 |
| Stage B: `/restart ollama`, `/restart resonant` → `Back up`, `/update` | 2026-10-14 |
| `/ext list` works after the Phase 3 deploy | 2026-10-14 |
| End-to-end latency bench, real model (run by Claude on the Mini): fast p50 354 ms, llm p50 1384 ms, both under target; recorded in `architecture.md` | 2026-10-14 |
