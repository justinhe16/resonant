# Status (2026-10-20)

The starting point for a new session. What's live, how we work, what's open. Detail lives in the linked docs; this page stays short. Update it at the end of each working session.

## What's live

Main is deployed on the Mac mini (`com.resonant.daemon`), schema 19, two extensions installed (github, linear).

| Area | What it does | PRs |
|---|---|---|
| Core (Phases 0–3.5) | iMessage in/out, event loop, policy gate, executor, store, scheduler, extensions, pulse and 08:00 briefing, kill switch | #1–#86 |
| Dashboard and relay (Phase 4) | Local read-only dashboard on 127.0.0.1:7777. Remote copy live since 2026-10-20: the daemon pushes to Supabase `resonant-relay` (ref `lqebsuuskxndfyunffhd`), Fly app `resonant-relay-jhe` serves https://resonant-relay-jhe.fly.dev/ (password + TOTP) and is the dead-man switch (ntfy to the owner's phone; freeze test passed) | #85–#88 |
| Workers (employee mode) | Claude Code workers in worktrees, unguarded, per-run GitHub App tokens, worker desk (ask/note/check), trust rules, Remote Control and live input (RC-1), transcript reading | #89–#92 |
| Repos | Registry; adding a repo clones it to `~/code/<name>` (RP-1) | #96 |
| GitHub and Linear | Quiet github with an employee-style rollup (GH-2); Linear MCP extension (LN-1, LN-2) | #93–#95 |
| Brain (BR-1…BR-11) | qwen3-30B triages each text: easy ones answered locally, hard ones get an instant ack and go to one long-lived Claude session that has Resonant's tools through the gate. Read tools, facts memory, rolling summaries, media/links/watches, taint rules for outside text | #97–#108 |
| Tool core (TC-1) | `resonant/toolcore/`: the single call path (registry → gate → executor) for qwen, the Claude brain, jobs and watches; a boundary test enforces it | #109 |
| Backups (BK-1) | Daily SQLite backup at 03:30, 7 daily + 4 weekly, 36h alert, `resonant backup now/list/restore` | #110 |
| Quiet briefing (BF-1) | All-zero items dropped; an empty briefing is one line | #111 |
| Batch approvals (BA-1) | One yes/no prompt for all of a task's changes; `allow_tools` lets "don't ask for X" set standing allows for extension tools | #113 |
| Phase 6b (PER-245…250, 258) | Taint lasts 3 turns, with notes/reminders/facts exempt. Flaky tests fixed (test daemons run no wall-clock jobs). Scheduler goes through the tool core (no boundary exceptions). `resonant eval draft --from-history` and `--set owner` with a review gate. `--route claude --live-claude` grades Claude's answers. qwen names things by title, drops relayed injected actions, refuses email/calendar. The Claude brain has full tools: reads run, shell changes run-and-report or ask by text (see brain-plan.md), the never-list is refused, and every call is audited | #114–#120 |
| Dashboard redesign, disk watchdog, truthful texts (R1, PER-267, PER-268) | Two-column shadcn dashboard. The disk watchdog no longer kills a run it can't measure (partial counts, free-space floor, GiB units). A PR merge isn't a direct push; one coworker-style text per run outcome; readable approval prompts with the failure reason; both brains see Resonant's own texts (`system_texts`, migration 019) | #121–#123 |

Latest task eval (synthetic set, live qwen, PER-249, 3 runs): 99.5%, 0 unapproved writes, triage 100%. Live Claude eval (PER-250, 5 hard rows): 5/5, p50 14.7s ack to answer, about $0.60. Most synthetic hard rows have no facts, so the owner set is the real test.

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

The list lives in [`followups.md`](followups.md). The next ones: OK and review the owner eval set (`resonant eval draft --from-history`, then set `review: false`, then `resonant eval task --set owner --repeat 3`), `resonant ext update github` and `resonant ext update linear` (needed for PER-268's prompt templates), the phone tests (PER-252, now including a brain shell approval), and mycelium by text (PER-253).

## Open work: Phase 6b, harden and prove

Linear epic [PER-257](https://linear.app/doriapp/issue/PER-257) in the Resonant project (Personal team). The first build tickets merged on 2026-10-20 (#114–#120). Your own to-dos are in [`followups.md`](followups.md); known limits are in [`limits.md`](limits.md).

| Who | Tickets |
|---|---|
| Owner | PER-252 phone tests (incl. `resonant ext update github`), PER-253 mycelium by text, PER-265 design decisions (webhooks, worker text in the briefing, nudges, bare yes/no, `/update` trust) |
| Build | PER-254 batch as one dashboard row, PER-255 `yes 1,3`, PER-256 retire intents, PER-259 store upkeep, PER-260 ops and inbound hardening, PER-261 approvals polish, PER-262 Claude session upkeep, PER-263 relay snapshot, PER-264 small gaps, PER-266 pulse wording |

Order: 254 → 255 → 261 → 266, and 254 → 263. PER-264's turn budget lands after PER-256.

**Roadmap after 6b:** 5b Slack → 6 on-call → 7 life lane (Gmail, browser, computer use). One card per phase on the Roadmap tab of `architecture.html`.

**Deferred:** direct MCP for the Claude brain (batch approvals covered the need). Headless `claude -p` loads no claude.ai connectors, so this would need directly configured servers.

**Dropped:**
- Calendar integration (CAL-1, PR #112 closed, 2026-10-19). Email and calendar asks get "can't reach it".
- Branch protection is rolling (protect each repo as it's added); no ticket.

## Where things are

- Architecture: [`architecture.md`](architecture.md) and `architecture.html`. Published as an artifact with System and Modules tabs: https://claude.ai/artifact/3iezGdhqteYUx8dRc6bv4q
- Contracts: [`interfaces.md`](interfaces.md). Brain design and as-built notes: [`brain-plan.md`](brain-plan.md). Go-live: [`go-live.md`](go-live.md).
- Runtime state: `~/.resonant` (config.yaml, resonant.db, logs/, backups/).
- Hosted: Supabase project `resonant-relay` and Fly app `resonant-relay-jhe` (both reachable from Claude Code: the Supabase MCP and `fly`). Rollback is in go-live step 15.
