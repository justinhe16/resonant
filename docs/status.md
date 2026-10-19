# Status (2026-10-19)

The starting point for a new session. What's live, how we work, what's open. Detail lives in the linked docs; this page stays short. Update it at the end of each working session.

## What's live

Main is deployed on the Mac mini (`com.resonant.daemon`), schema 18, two extensions installed (github, linear).

| Area | What it does | PRs |
|---|---|---|
| Core (Phases 0–3.5) | iMessage in/out, event loop, policy gate, executor, store, scheduler, extensions, pulse and 08:00 briefing, kill switch | #1–#86 |
| Dashboard and relay (Phase 4) | Local read-only dashboard on 127.0.0.1:7777; Supabase/Fly relay built but off (go-live step 15) | #85–#88 |
| Workers (employee mode) | Claude Code workers in worktrees, unguarded, per-run GitHub App tokens, worker desk (ask/note/check), trust rules, Remote Control and live input (RC-1), transcript reading | #89–#92 |
| Repos | Registry; adding a repo clones it to `~/code/<name>` (RP-1) | #96 |
| GitHub and Linear | Quiet github with an employee-style rollup (GH-2); Linear MCP extension (LN-1, LN-2) | #93–#95 |
| Brain (BR-1…BR-11) | qwen3-30B triages each text: easy ones answered locally, hard ones get an instant ack and go to one long-lived Claude session that has Resonant's tools through the gate. Read tools, facts memory, rolling summaries, media/links/watches, taint rules for outside text | #97–#108 |
| Tool core (TC-1) | `resonant/toolcore/`: the single call path (registry → gate → executor) for qwen, the Claude brain, jobs and watches; a boundary test enforces it | #109 |
| Backups (BK-1) | Daily SQLite backup at 03:30, 7 daily + 4 weekly, 36h alert, `resonant backup now/list/restore` | #110 |
| Quiet briefing (BF-1) | All-zero items dropped; an empty briefing is one line | #111 |
| Batch approvals (BA-1) | One yes/no prompt for all of a task's changes; `allow_tools` lets "don't ask for X" set standing allows for extension tools | #113 |
| Calendar (CAL-1) | Google's official Calendar MCP server as the `gcal` extension (reads run, writes ask, never `yes always`) | #112 (in review at time of writing) |

Latest task eval (synthetic set, live qwen): 95.8%, 0 unapproved writes, triage 100%. Claude's own answers are not graded yet.

## How we work

- **Docs-only changes** go straight to main.
- **Code** goes through robot-surf, one fresh agent per ticket in its own worktree:
  1. The agent builds the change, opens a PR, gets CI green, and runs the code-reviewer until no must-fix remains.
  2. The coordinator verifies `gh pr checks` and that mergeStateStatus is CLEAN, then squash-merges with `--match-head-commit`.
  3. Before deploying a migration, back up the DB (`resonant backup now`).
  4. Deploy: `uv sync`, then `launchctl kickstart -k gui/$(id -u)/com.resonant.daemon`.
- **Main moves under parallel agents.** When main changes, have each open branch merge origin/main and get CI green again before merging it.
- **Hard rules (owner's):**
  - Claude never runs `resonant secret set` or `resonant ext add/update`. These need the owner's typed confirmation.
  - Never put secrets in chat.
  - Ask before anything that sends an iMessage.
  - Never write to the login keychain.
  - Tests and agents never touch `~/.resonant` or `~/code`.
  - Workers stay unguarded; don't propose sandbox layers.
- **Keep replies short.**

## Owner to-dos

- `resonant ext update github`: picks up `recent_prs` (BR-9) and the quiet flag (BF-1).
- Calendar setup once CAL-1 is merged: `extensions/gcal/README.md`, which covers the Google Cloud project, OAuth Playground refresh token, three `resonant secret set gcal/...` and `resonant ext add repo:gcal`.
- Phone tests to run:
  - the Claude brain (go-live "The Claude brain");
  - a Linear batch ("close RES-…, RES-…");
  - "dont ask for perms for linear tickets".
- Branch protection on the repos you care about; relay go-live (step 15).

## Open work

The ranked list with urgency is in [`followups.md`](followups.md) under "Recommended next". The main open items:

1. 🔴 Owner eval set from real texts (`~/.resonant/evals/task.jsonl`).
2. 🟠 Shorter taint window, or exempting low-risk writes. A Claude answer taints about 24 turns.
3. 🟡 Live Claude eval in a scratch home.
4. 🟡 Branch protection; relay go-live.
5. 🟢 Flaky tests:
   - scheduled jobs firing mid-test around 08:00/10:00 local;
   - the Q9 approval code;
   - the watchdog;
   - e2e ConnectErrors;
   - `test_kill_switch_stops_running_sessions`.
6. 🟢 Retire the intents path, whisper, and prune worktrees.
7. Direct MCP for the Claude brain on owner-granted servers (Linear, gcal), with `--permission-prompt-tool` deciding each call. Needs a feasibility probe first. Deferred: batch approvals solved the immediate pain.
8. Dashboard: show a batch as one row.

Each ticket's carry-overs are in `followups.md` and `brain-plan.md`.

## Where things are

- Architecture: [`architecture.md`](architecture.md) and `architecture.html`. Published as an artifact with System and Modules tabs: https://claude.ai/artifact/3iezGdhqteYUx8dRc6bv4q
- Contracts: [`interfaces.md`](interfaces.md). Brain design and as-built notes: [`brain-plan.md`](brain-plan.md). Go-live: [`go-live.md`](go-live.md).
- Runtime state: `~/.resonant` (config.yaml, resonant.db, logs/, backups/).
