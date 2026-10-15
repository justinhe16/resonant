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
| 12 | Plain words reach the pulse: "what's the latest update" | The same text as `/pulse`, through the `updates` intent (model reply, owner only) | 📱 after deploy |
| 13 | "what's running" | Tasks answer through chat (`job_control`), no longer a keyword | 📱 after deploy |
| 14 | Before the owner steps: `/runs`, `/run resonant fix the typo` | Both answer `The runner isn't set up: …`, naming what's missing. `/status` has no runs line. | 📱 after the Phase 5a deploy |
| 15 | Plain words reach the run tools: "fix the typo in the README in resonant", "what runs are going", "cancel R3", "merge R3" | `run_project` intent; before the owner steps the not-set-up text (or `No run R3.`); `merge R3` asks `yes` when R3 is ready | 📱 after the Phase 5a deploy |

## Needs the Mini

| # | Test | Where | Status |
|---|---|---|---|
| 16 | Sandbox self-check: `resonant sandbox check` | every deny and allow row passes (Claude can run it) | 🖥 (Claude) |
| 17 | GitHub token: `resonant secret set github/token`, then `check` | go-live 12, terminal 1 | 🖥 |
| 18 | `resonant ext add repo:github` (typed confirm) | go-live 12, terminal 2 | 🖥 |
| 19 | `scripts/bootstrap.sh --check` rows for the github pin, secret and jobs | go-live 12, terminal 4 | 🖥 |
| 20 | `/help github`, `/github prs`, "show my PRs" | go-live 12, iMessage 2–4 | 📱 after 18 |
| 21 | No token in logs or the DB (`grep`, `sqlite3`) | go-live 12, iMessage 5 | 🖥 |
| 22 | Watch: baseline silent, then a red and a green CI | go-live 12, iMessage 6–7 | 📱 after 18 |
| 23 | `/ext mute github 1h`, then `off` | go-live 12, iMessage 8 | 📱 after 18 |
| 24 | Next 08:00 briefing shows `Updates:` / `[github] 3 open PRs, …` | go-live 13.3 | 📱 after 18 |
| 25 | `/kill` skips jobs and kills running extension processes | go-live 12, iMessage 10 | 📱 + 🖥 (sqlite check) |
| 26 | Tamper test: edit the installed copy, then `/restart resonant`, then `resonant ext update github` | go-live 12, iMessage 11 | 🖥 |
| 27 | Crash mid-approval: `kill -9` the daemon before answering | go-live 11, Stage B 5 | 🖥 ❓ |
| 28 | Optional: github writes (`rerun_failed`, `merge_pr` with `yes D7`) | go-live 12, the writes | 📱 after a token with write access |
| 29 | Remove the example extension after tests 10–11 | `resonant ext remove example` | 🖥 |
| 30 | Runner secrets: `claude setup-token` → `resonant secret set runner/claude_token`; a fine-grained PAT (no Workflows permission) → `resonant secret set runner/github_token` | go-live 14, terminal 1–2 | 🖥 |
| 31 | Name the runner repos (Claude adds `runner.repos` and restarts) | go-live 14, terminal 3 | 📱 tell Claude |
| 32 | Per runner repo: CI on `pull_request`, no secrets for same-repo `pull_request` runs, branch protection on `main` | go-live 14, GitHub | 🖥 (Claude can check the workflows) |
| 33 | Authenticated probe: `CLAUDE_CODE_OAUTH_TOKEN=… resonant sandbox check` | go-live 14, terminal 5 | 🖥 after 30 |
| 34 | First real run, Claude's own sandbox is off and the session works inside ours: `/run` a trivial ask on a CI repo; the transcript (`~/.resonant/runner/transcripts/R1.jsonl`) shows no sandbox_apply or permission errors; `--max-turns` is accepted | go-live 14, first run | 🖥 (Claude) after 30–32 |
| 35 | First run over iMessage: "fix <something small> in <repo>" → `On it: R1`, then `R1 done: PR #… `; `/runs`; `/merge R1` once CI is green | go-live 14, iMessage | 📱 after 30–32 |
| 36 | `/kill` (and `/cancel R1`) during a run → run cancelled, nothing pushed; `/resume` | go-live 14, iMessage | 📱 after 30–32 |

## Needs real data or effort

| # | Test | Where | Status |
|---|---|---|---|
| 37 | Router evals on 30–50 real texts (≥ 90%) | go-live 9, [`followups.md`](followups.md#router-evals-on-real-texts-go-live-step-9) | deferred |

## Done

| Test | When |
|---|---|
| Phase 2 Stage A (notes, L1 codes, reminders, dry-run ops, discovery, chat, kill switch) | 2026-10-14 |
| Stage B: `/restart ollama`, `/restart resonant` → `Back up`, `/update` | 2026-10-14 |
| `/ext list` works after the Phase 3 deploy | 2026-10-14 |
| End-to-end latency bench, real model (run by Claude on the Mini): fast p50 354 ms, llm p50 1384 ms, both under target; recorded in `architecture.md` | 2026-10-14 |
