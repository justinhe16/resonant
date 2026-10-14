# Phase 2 plan (v3): risk × who chose it, approvals, and real tools

Status: v3, after the owner's feedback. Scope: the real gate, text-reply approvals and the kill switch from [`architecture.md`](architecture.md#components), plus the first real tools: notes, reminders, Mini ops, projects and command discovery. Veto windows and Keychain injection move to Phase 3 ([§7](#7-deferred-and-out-of-scope)). Deviations from the frozen [`interfaces.md`](interfaces.md) are listed under [Contract changes](#contract-changes-flagged).

Each ticket is one PR (~200–600 lines including tests), run through robot-surf: worktree, tests, PR, CI, code-review agent, merge to `main`. Each ticket updates **its own** `interfaces.md` section.

---

## 1. Goal and exit criteria

**Goal.** Every side effect goes through a gate that weighs the **risk** (the registered level) against **who chose the action** (origin):
- Typing a slash command is consent, so it just runs.
- Something the model chose asks first when the risk is medium.
- Destructive actions always need a typed code.

The owner can run the Mini remotely, keep notes and reminders, and see all work in progress, by slash command or in plain language.

**Exit criteria.** Each one is an automated in-process e2e test (`FakeChatDB` + `RecordingOsaRunner` + `ScriptedModel` + fake clock and fake subprocess runner) and an item on the live checklist ([§6](#6-rollout)).

| # | Scenario | Pass condition |
|---|---|---|
| E1 | **Explicit L3.** `/note buy milk` | `Saved note 1: "buy milk"` with no prompt. One `executions` row. `gate.evaluate` audited `origin=explicit, decision=Allow`. |
| E2 | **Explicit L2.** `/restart resonant` | It runs with no prompt. The reply `Restarting Resonant…` is sent **before** the restart, and `Back up (abc1234), down 6s` comes after it. |
| E3 | **Inferred L2.** "please restart resonant" (model calls `restart_resonant`) | The owner gets `[A7] restart Resonant? Reply yes / no`. A plain `yes` binds (single pending + sent-before) and it runs once. `no` gives `Not done.` |
| E4 | **L1 always confirms.** `/notes clear`, explicit | The owner gets `[D7] delete all 4 notes? This can't be undone. Reply yes D7 / no`. A plain `yes` does **not** approve (`reply yes D7 to confirm`). `yes D7` clears them. `no` denies. |
| E5 | **Kill switch.** `/kill`, then `/note x` and `/restart ollama` | Both ask (`[A?] … Reply yes / no`), and L1 still needs the code. `/kill` itself is never read as a reply. After `/resume` they run directly again. |
| E6 | **Reminder on time.** `/remind in 20 min stretch` | `⏰ stretch` arrives at T+20 min (± loop wake precision ≤ 1 s with the next-wake change). `/reminders` lists it until then. `/unremind 3` cancels it, and nothing is sent. |
| E7 | **Reminder durability.** Restart before T | It still fires at T, once. Daemon down from T−1 to T+10 min: it fires once on start as `⏰ stretch (late, due 3:00pm)`. |
| E8 | **Bad times.** `/remind 3 call mom`, `/remind yesterday 9am x`, `/remind 2:30am …` on a DST-gap day | A usage reply (`say 3pm or 15:00`, `that time has passed`). No task is created. |
| E9 | **`/update`.** Dirty tree, or not on `main` | Refused (`working tree has changes; not updating`) with nothing run after the checks. Clean tree: it reports `a1b2c3d → e4f5a6b`, restarts, then says "back up". Already current: `Already up to date`, no restart. |
| E10 | **Command discovery.** `/remnd 3pm x` | `Unknown command /remnd. Did you mean /remind?` and **nothing executes**. `/help` lists the registry, `/help remind` filters it, and "what can you do?" (model → `list_commands`) answers from the registry. |
| E11 | **`/projects`** | Items ordered by `updated_at` descending, labeled by state (running, waiting(reason), queued, done, failed, cancelled) with age, kind and title. At most 15; `/projects all` shows up to 50. |
| E12 | **Restart mid-approval.** E3 pending, then SIGTERM and a new stack on the same DB (CI); also `kill -9` (live only) | `yes` → it runs exactly once. |
| E13 | **Replay and race** | The same chat.db row routed twice gives one resolution and no extra reply (`resolved_event_id`). Two `yes` replies in one poll give one resolution and one execution. A reply whose `occurred_at` is before the prompt was sent never binds. |
| E14 | **Orphans and single use** | Cancelling, failing or finishing a task expires its pending approvals, and a later `yes` binds nothing. A used approval (`used_at`) never yields another `Allow`. A retry after a reply-send failure replays from `executions`. |
| E15 | **dry_run** | With `dry_run: true`, the notes and reminder tools run. Ops tools reply `Dry run: would restart Ollama`. |

---

## 2. Current state

### Exists

| Area | Where | State |
|---|---|---|
| Gate types | `gate/types.py` | `Intent.for_tool` and `.hash`, every `Decision` variant, the approval dataclasses, the `Gate` protocol. **`Intent` has no `origin`.** |
| Dry-run gate | `gate/dry_run.py` `DryRunGate` | Registration and permission checks; an L0-read token; everything else `DryRun`. Ignores `settings.dry_run` and kv `autonomy`. No approvals. |
| Codes | `gate/codes.py` | `new_code`, `sanitize_external`, `approval_text` (normal and pay variants). |
| Executor | `executor.py` | Token consume, `executions` idempotency, ambiguity, audit. **No `used_at` and no `recorded(key)`.** |
| Kill switch | `killswitch.py`, `/kill` and `/resume`, `cli.py` | Writes kv `autonomy` and `paused`. **Nothing reads `autonomy`.** |
| Store | `001_init.sql`, `002_…sql` | `approvals` (code, require_code, expires_at, channel_refs, …) and `approval_batches`. No repository code. |
| Loop | `loop/loop.py`, `store/tasks.py` | `Wait`/`wake_at`. Queued tasks honor `wake_at` (`runnable_tasks`: not before). `Loop.submit` wakes a task by `task_id`. A `Wait` requeues if events arrived mid-step, so a `yes` that lands while the task is still `running` isn't lost. The loop sleeps `tick_s` (30 s) between ticks. `NewTask` has no `wake_at`. `save_step` increments `step_no` on every outcome. |
| Router | `router/fast_path.py`, `router.py`, `intents.py`, `prefixes.py` | Slash commands are hard-coded (`/status`, `/tasks`, `/help`, `/kill`, `/resume`); anything else gets "Unknown command". Intents: question, job_control, system, run_project, unknown. |
| Local runner | `runners/local.py` | Single step; a non-`Allow` decision becomes a tool error; never returns `Wait`. |
| Channel | `gateway/imessage/channel.py` | `send` (allowlist, dedupe, rate limit). `request_approval` raises `NotImplementedError("Phase 2")`. The reader drops SMS/RCS, tapbacks, groups, our own rows and unknown handles. |
| Tools | `tools/builtin.py` | Five L0 reads (`list_tasks`, `task_counts`, `system_status` (includes disk), `daemon_status`, `model_status`). |
| launchd | `launchd.py` | `DAEMON_LABEL = "com.resonant.daemon"`, `OLLAMA_LABEL = "com.resonant.ollama"`, domain `gui/<uid>`. |
| Wiring | `components.py`, `wiring.py` | `Components.gate: DryRunGate`. Local runner step limit = `(3 + 1) × model.timeout_s + 15` (255 s). |

### Gaps
`Intent.origin`; `PolicyGate`; the approvals repository and migration; the parser; `request_approval`; reply recognition; Wait/resume in the runner; a command registry and fuzzy matching; the notes, reminders, ops and projects tools; a reminder runner; loop next-wake; new intents; wiring; CLI; go-live updates.

---

## 3. Design decisions

### D1. Gate: risk × origin
- **Origin.** `Intent.origin: Literal["explicit", "inferred"] = "inferred"` (additive, **not** part of `hash`).
  - Code sets `explicit` in exactly one place: the local runner's `direct_call` path. A `direct_call` checkpoint is written **only** by the router's deterministic command parser, for a principal allowed to request the tool.
  - Everything the model chooses is `inferred`.
  - Phase 3 scheduled and extension jobs will be `inferred` too.
- **Evaluation order** (replaces the frozen step 4):
  1. Registration (unchanged).
  2. Permission (unchanged).
  3. Unsafe args (bidi, control or zero-width characters) → `Deny`.
  4. dry_run: a non-exempt, non-L0-read tool → `DryRun(would=…)`.
  5. Level × origin × autonomy, per the table below.

| Level (registry) | autonomy on, explicit | autonomy on, inferred | autonomy off (kill switch) |
|---|---|---|---|
| L0 read | Allow | Allow | Allow |
| L3 small / reversible | Allow, then code-built `Done: <summary>` | Allow + report | NeedsApproval (plain yes) |
| L2 medium | Allow + report | NeedsApproval (plain yes) | NeedsApproval (plain yes) |
| L1 destructive / pay | NeedsApproval, `require_code=True` | same | same |

- **Defaults.** `DEFAULT_LEVEL[write] = L1` stays: an unlevelled write is treated as destructive, which is the safe default. Pay stays L1.
- **No veto in Phase 2.** `evaluate` never returns `Veto`. The type stays in the contract for Phase 3 unattended jobs.
- **Kill switch.** kv `autonomy` is read on every call. A missing or garbage value counts as off (fail closed).
- **Approvers** are `(owner,)` for builtins (global scope is owner-only).

### D2. Approval prompts and the parser
- **Prompt text:**
  - L2/L3: `[A7] <summary>? Reply yes / no`.
  - L1: `[D7] <summary>? This can't be undone. Reply yes D7 / no`. This is a new `approval_text(..., destructive=True)` variant; the existing pay variant is unchanged.
- **Parser** (`gate/replies.py`, whole message after NFKC, lowercasing, quote straightening and whitespace collapse):
  - `(yes|approve|👍)( <code>)?`
  - `<code> yes`
  - `(no|deny|👎)( <code>)?`
  - `<code> no`
  - `yes( <code>)? with comments: <≤500>`

  `y`, `ok`, `sure`, `yes all` are not replies. There is no classifier and there are no batches (deferred).
- **Binding** (`resolve_reply`):
  - The actor must be an approver. A non-approver gets the same generic `Nothing is waiting for approval.`, so pending approvals are never revealed.
  - **Sent-before rule.** The approval's prompt must have been sent (a `channel_refs` entry `{ref, at, principal}`) **before** the reply event's `occurred_at`, with or without a code.
  - A plain `yes`/`no` binds only when exactly one approval is pending for that approver. Otherwise the router re-asks, listing the codes.
  - `require_code` approvals: only `yes <code>` approves. A plain `no` may deny under the single-pending rule.
  - A `payload.truncated` reply never binds.
  - iMessage edits and unsends are invisible to the reader (documented). Tapbacks are already dropped.
  - **Idempotent per event:** `approvals.resolved_event_id`. Re-routing the same event returns the original result and sends no reply.
- **Resolution** is one conditional UPDATE: `… WHERE id=? AND status='pending' AND expires_at > :now`, plus a check that the task is `waiting` or `running` (else `task_not_waiting`). The CLI and the daemon can't race between check and write.
- **Router order:**
  1. Slash commands (registry) always win.
  2. Then the parser. On success `route` returns **None** (the event is consumed); the woken runner sends the templated confirmation.
  3. Then normal routing.

### D3. Exactly-once, orphans and validity (kept from v2)
- **TTL** `gate.approval_ttl_s` (default 1800). Expiry is lazy: every gate entry point first runs `expire_due(now)`. The waiting task's `wake_at = expires_at`, so the runner tells the owner at expiry.
- **Orphans.** When a task becomes terminal (`store/tasks.py` `save_step` / `set_status`), its pending approvals expire in the same transaction (audit `approval.orphaned`).
- **Single use.** `approvals.used_at` is set in the same transaction as the executor's `started` row. `evaluate` never issues `Allow` for an approval that has been used. An approval is valid for the life of its task.
- **Runner order on `NeedsApproval`:**
  1. `ctx.save` the pending call
  2. `request_approval` to each approver (dedupe `approval:<id>:<principal>`)
  3. record each channel ref with its time
  4. `Wait("approval", wake_at=expires_at)`

  It re-prompts on every pending step (idempotent through the dedupe key), and a send failure is a retryable `Fail`.
- **Waking.** `wake_resolved(conn, result)` inserts an `Event(source="gate", type="approval.reply", task_id)` and requeues the task, in the resolver's transaction.
- **Resume.**
  1. Pin `exec = {step_no, call_index}` with `ctx.save`.
  2. Check `Executor.recorded(key)`:
     - `succeeded`: replay without calling the gate.
     - `started`: ambiguous; report it and never re-run.
     - none: `gate.evaluate` → `Allow(token, approval_id)` → execute with the pinned key.

  Tokens are in memory, and restarts are safe by construction.
- **One gated side effect per local task.** Whatever runs after an approval or an explicit command is templated, with no more model rounds.

### D4. dry_run: flip it off at Phase 2 go-live (recommended)
- **Exemption** for Resonant-internal builtins, keyed on registry identity: `spec.extension is None and spec.name in STORE_INTERNAL_TOOLS`. That is a code constant in `tools/__init__.py`, `{add_note, delete_note, clear_notes, add_reminder, cancel_reminder}`. A test pins it to exactly that set and asserts that none of them declares secrets.
- **Ops tools are not exempt.** They act on the system.
- **Recommendation: flip `dry_run: false` at go-live, after stage A of the checklist** (all internal tools, approvals, kill switch and dry-run replies verified).
- **Why:**
  - The ops tools are the point of this phase, since the owner is remote.
  - They are owner-only (global scope), run argv-only subprocesses with timeouts, and act only on two fixed launchd labels and the repo.
  - Exempting them would make "dry_run" meaningless for exactly the effects it exists to hold back.
- Rollback: `dry_run: true` + `resonant daemon install`, or `/kill`.

### D5. Command registry and discovery (`router/commands.py`)
- `Command(name, usage, description, tool: str | None, parse: (rest) -> args | UsageError, owner_only)`. The level shown comes from the registry spec, never from the command.
- It is the single source for the fast path, `/help`, `/help <word>` (substring, then `difflib` match over name and description), and the `list_commands` L0 tool (filtered by `can(principal, "request", …)`).
- **Unknown `/word`.** The reply is `Unknown command /remnd. Did you mean /remind?`, using `difflib.get_close_matches(n=3, cutoff=0.6)`. It **never executes**, even at distance 1.
- **Dispatch.**
  - Read commands keep today's fast path (a gated read, templated reply).
  - Side-effecting commands return `NewTask(kind="command", runner="local", checkpoint={"direct_call": {tool, args}, "title": "/note"})`.
  - A bad argument returns the usage line and creates no task.

### D6. Tools (one module each, `tools/<area>.py`, registered through `tools/builtin.py`'s `BUILTIN_MODULES` tuple)

| Tool | Level | Command | Notes |
|---|---|---|---|
| `list_notes` | L0 | `/notes` | Newest first, at most 20, soft-deleted hidden |
| `add_note` `{text ≤ 200}` | L3 | `/note <text>` | Stored in `memory(scope="notes")` as JSON `{text, at, deleted_at}`, with ids from kv `notes.seq` |
| `delete_note` `{id}` | L3 | `/unnote <id>` | **Soft delete**: sets `deleted_at` (undo later) |
| `clear_notes` | **L1** | `/notes clear` | Soft-deletes all. The live test of the "are you sure" path. The summary says how many. |
| `list_reminders` | L0 | `/reminders` | Pending reminders, soonest first |
| `add_reminder` `{at: ISO-8601 with offset, text ≤ 200}` | L3 | `/remind <when> <text>` | Creates a reminder task (D7) |
| `cancel_reminder` `{id}` | L3 | `/unremind <id>` | `set_status(cancelled, expect=("queued",))` |
| `restart_ollama` | L3 | `/restart ollama` | `launchctl kickstart -k gui/<uid>/com.resonant.ollama`, 15 s timeout |
| `restart_resonant` | L2 | `/restart resonant` | Delayed self-restart (D8) |
| `update_resonant` | L2 | `/update` | D8 |
| `system_status` (existing) | L0 | `/disk` | Renders only the `disk` part |
| `recent_logs` `{n ≤ 20}` | L0 | `/logs [n]` | Tail of `logs/resonant.jsonl`. URLs → `<url>`, emails and phones → `<handle>`, at most 300 chars a line. |
| `projects` `{all?: bool}` | L0 | `/projects`, `/projects all` (`/tasks` aliases it) | D9 |
| `list_commands` | L0 | `/help` | From the registry (D5) |

All handlers return JSON, raise `ToolFailed` for bad args or definite failures, and run subprocesses as argv with no shell, a timeout, and stderr never logged.

### D7. Reminders without a scheduler
- **Creating one.** `add_reminder` inserts `NewTask(kind="reminder", runner="reminder", principal, wake_at=at, checkpoint={text, at})`. `NewTask.wake_at` is additive and queued tasks already honor "not before". The task id is the reminder id. The executor's idempotency key prevents a duplicate on replay.
- **Firing.** The `ReminderRunner` (`runners/reminder.py`, no model, no tools) sends `⏰ <text>` through `channel.send(principal, dedupe_key=f"reminder:{task.id}")`, then `Done`.
  - If `now - at > 120 s`, the text gets `(late, due 3:00pm)`.
  - A send failure is a retryable `Fail`, and the dedupe key prevents a double send.
  - The send is the deferred effect of an already-allowed L3 call, so it doesn't go through the gate again.
- **Durability** comes from the `tasks` row. `requeue_interrupted` only touches running tasks, so a queued reminder survives any restart and fires once on start if it was missed.
- **Precision.** `Loop.run` waits `min(tick_s, time until the earliest queued wake_at)`, using a new `repo.next_wake_at`. Today the wait is up to 30 s.
- **Time parser** (`tools/when.py`, pure, stdlib `zoneinfo`, in `settings.timezone`). Accepted forms:
  - `now`
  - `in N (m|min|mins|minutes|h|hr|hrs|hours)`
  - `H(:MM)?(am|pm)`, or `HH:MM` 24-hour when HH ≥ 13 or zero-padded
  - `tomorrow <time>`
  - `<weekday> <time>` (the next occurrence)

  Rejected:
  - A bare `3` or `9:30` without am/pm is ambiguous: usage reply.
  - A time in the past, more than 365 days out, or a nonexistent local time (DST gap) gets a usage reply.

  The `/remind` command takes the longest prefix that parses as `<when>`; the rest is the text. No new dependency.
- **Model path.** The model supplies an ISO datetime with an offset. The handler requires it to be in the future and ≤ 365 days out.

### D8. Ops tools: reply first, restart detached
- **`restart_resonant`:**
  1. Writes kv `ops.restart_pending = {by, at, commit}`.
  2. Spawns `[sys.executable, "-m", "resonant.ops.restart_later", DAEMON_LABEL, "3"]` with `start_new_session=True`, so it leaves launchd's process group. The helper sleeps 3 s, then runs `launchctl kickstart -k gui/<uid>/<label>`.
  3. Returns at once. The runner's reply goes out before the kill, and graceful shutdown drains in-flight sends for 10 s.
- **"Back up".** On start (`IMessageStack.start`), a pending marker gets `Back up (<commit>), down Ns` (`dedupe_key=restart:<at>`), and the marker is cleared.
- **No double restart.** The tool's `executions` row is `succeeded`, so a resumed task replays rather than restarting again.
- **`update_resonant`,** in the repo path (`ops.repo_path`, default: the checkout that contains the running package):
  1. Refuse unless `git status --porcelain` is empty and the branch is `main`.
  2. `git fetch` then `git pull --ff-only` (60 s).
  3. If HEAD didn't change: `Already up to date`.
  4. `uv sync --frozen` (`ops.uv_path`, resolved by bootstrap; 240 s).
  5. Restart as above, and reply `a1b2c3d → e4f5a6b, restarting`.
  - A `uv sync` failure reports it and does **not** restart. The tree is at the new commit but the running process is unaffected; the owner fixes it by hand.
- **Step limit.** `local_step_limit` becomes `max(model-based limit, max tool timeout_s + 15)`, so `/update` is never cancelled into an ambiguous outcome.
- **Risk.** `/update` runs code from `origin/main`. That is acceptable for the owner's repo at L2 (explicit from the owner; asks if inferred). Noted in §5.

### D9. `/projects`
- The `projects` tool reads `tasks` ordered by `updated_at` descending (15 items, or 50 with `all`). Each item: `{id, state, wait_reason, age_s, kind, title}`.
- **Title** is code-built: `checkpoint.title` (written by the router: the command name or `chat: <intent>`), `⏰ <reminder text clipped to 30>` for reminders, else the kind. Never message bodies.
- Grouping is done in the renderer: running, waiting(reason), queued, then recent done/failed/cancelled.
- `list_tasks` stays, because it is a frozen builtin used by `job_control`. Phase 5 `run_project` sessions will appear here automatically.

### D10. Chat actions (frozen `IntentLabel` change)
- **New intents** `notes`, `reminders`, `ops`. `INTENT_TOOLS`:
  - `notes`: notes tools
  - `reminders`: reminder tools
  - `ops`: `restart_ollama`, `restart_resonant`, `update_resonant`, `recent_logs`
  - `job_control` += `projects`
  - `system` += `list_commands` ("what can you do" is about Resonant itself)
- **Prompt changes.** The labeler's definitions and few-shots change, so the `label_prefix.json` snapshot changes. `runner_system` tells the model to pass reminder times as ISO datetimes, with the current local time and the zone given just before the message. That is a per-minute-varying suffix, so it goes after the stable prefix: in the last user turn rather than the system message, since some chat templates render the tools inside the system turn (as built in P2-16).
- **Evals.** Add ~15 synthetic cases to `evals/router.example.jsonl`. `resonant eval router` on the example set must stay ≥ 90%.
- Inferred L2 asks and inferred L3 runs, with one gated side effect per task.

### D11. Trust model
The gate protects against remote and iMessage principals and against the model, **not** against local code. Any process running as the same user can:
- run `resonant approvals approve` or `resonant resume`
- write `~/.resonant/resonant.db`
- read the Keychain

This must go into `architecture.md` (Trust model). Phase 3/5 runners with shell access must be sandboxed from all three (deferred).

---

## Contract changes (flagged)

| # | Contract | Change | Ticket |
|---|---|---|---|
| C1 | Gate: `Intent` | `origin: explicit\|inferred = "inferred"`, not hashed, set only by code (D1) | P2-3 |
| C2 | Gate evaluation order and levels | Risk × origin table; no `Veto` in Phase 2; dry_run exemption set. Also update the levels tables in `architecture.md` (Trust model), `architecture.html` (approval flow, `veto` wait reason), and `extensions.md` §Levels. | P2-6, P2-17 |
| C3 | Approvals text | `approval_text(..., destructive=True)` → `[D7] …? This can't be undone. Reply yes D7 / no` | P2-3 |
| C4 | Approvals classifier and batches | Deferred. Phase 2 is parser-only, and `open_batch` stays `NotImplementedError`. | P2-4 |
| C5 | `ApprovalReply` | Add `event_id`, `occurred_at` (sent-before rule, idempotency) | P2-10 |
| C6 | Executor | `recorded(key)`; approval `used_at` marked with `started`. Keychain injection deferred to Phase 3 (the "Phase 2" sentence moves). | P2-6 |
| C7 | Components | `Components.gate: Gate` | P2-9 |
| C8 | Built-in tools | The new tools; "five global read tools" becomes the D6 table; `STORE_INTERNAL_TOOLS` | P2-5, P2-7, P2-8, P2-14 |
| C9 | Router | Command registry, fuzzy suggestions, `/help <word>`, approval-reply step, the new commands; `IntentLabel` + `INTENT_TOOLS` + prefix snapshot | P2-1, P2-12, P2-15, P2-16 |
| C10 | Runner / Task | Local runner `Wait`/resume/`direct_call`; new `runner="reminder"`; `NewTask.wake_at`; loop next-wake | P2-8, P2-11 |
| C11 | Store | Migration 003: `approvals.used_at`, `resolved_event_id`; orphan expiry | P2-2 |

---

## 4. Tickets

**Hard rules.** Tickets that touch the same hot file run one after another, in this order:
- `router/fast_path.py`: P2-1 → P2-12 → P2-15
- `router/commands.py`: P2-1 → P2-15
- `runners/local.py`: P2-11 only
- `gate/policy.py`: P2-6 → P2-10
- `cli.py`: P2-13 only
- `store/tasks.py`: P2-2 → P2-8
- `loop/loop.py`: P2-8 only
- `wiring.py` / `components.py` / `daemon.py`: P2-9 → P2-14 → P2-17
- `router/intents.py` / `prefixes.py`: P2-16 only

Tool tickets touch `tools/builtin.py` only to add their module to `BUILTIN_MODULES` (a one-line rebase). From P2-9 on, every ticket adds its own cases to `tests/test_e2e_phase2.py`.

| ID | Title | Deps | Size | Wave |
|---|---|---|---|---|
| P2-1 | Command registry, `/help [word]`, fuzzy suggestions, `list_commands`; builtins split into modules | – | M | 1 |
| P2-2 | Approvals store, migration 003, orphan expiry, `wake_resolved` | – | M | 1 |
| P2-3 | Gate prerequisites: `Intent.origin`, summaries, destructive prompt text, `gate:` config | – | S | 1 |
| P2-4 | Deterministic reply parser | – | S | 1 |
| P2-5 | `projects` tool + renderer | 1 | S | 2 |
| P2-6 | PolicyGate core: risk × origin, kill switch, dry_run exemption, expiry, single use, `recorded` | 2, 3 | L | 2 |
| P2-7 | Notes tools (incl. L1 `clear_notes`, soft delete) | 1, 3 | S | 2 |
| P2-8 | Reminders: `when` parser, tools, `ReminderRunner`, `NewTask.wake_at`, loop next-wake | 1, 2, 3 | L | 2 |
| P2-9 | Early wiring: PolicyGate in components, reminder runner, startup log, `approvals_pending` in `/api/status` | 6, 8 | S | 3 |
| P2-10 | Reply binding: `resolve_reply`, sent-before, per-event idempotency | 4, 6 | M | 3 |
| P2-11 | LocalRunner: `direct_call` (explicit), Wait/resume, execute once, report, dry-run replies | 3, 6 | L | 3 |
| P2-12 | Router approval inbox + `request_approval` | 1, 10, 11 | M | 4 |
| P2-13 | CLI `resonant approvals list\|show\|approve\|deny` | 10 | S | 4 |
| P2-14 | Ops tools: restarts, `/update`, `/logs`, "back up", step limit | 1, 3, 9 | L | 4 |
| P2-15 | Slash commands for all tools in the registry (`/note`, `/remind`, `/restart`, `/update`, `/disk`, `/logs`, `/projects`, …) | 1, 5, 7, 8, 12, 14 | M | 5 |
| P2-16 | Chat intents `notes` / `reminders` / `ops`, toolsets, prefix snapshot, router eval cases | 5, 7, 8, 14 | M | 5 |
| P2-17 | Remaining e2e (restart, race/replay, kill switch, orphans), go-live, `--check`, docs and level tables | all | M | 6 |

### Ticket notes
- **P2-1**
  - **Files:** `router/commands.py` (new), `router/fast_path.py`, `tools/commands.py`, `tools/builtin.py` (`BUILTIN_MODULES`, `register_builtins` iterating modules), tests.
  - Moves today's commands into the registry with no behavior change except the fuzzy reply.
  - **Tests:** `/stauts` suggests `/status` and runs nothing; `/help remind` filters; `list_commands` respects the principal; every registry tool exists in the tool registry.
- **P2-2**
  - **Files:** `store/migrations/003_approvals_phase2.sql`, `store/approvals.py`, `store/tasks.py`.
  - **Tests:** migration over 002; the conditional UPDATE refuses expired rows, already-resolved rows and `task_not_waiting`; orphan expiry on done, failed and cancelled; `by_resolved_event`; `wake_resolved` dedupes and requeues only waiting tasks.
- **P2-3**
  - **Files:** `gate/types.py` (origin), `gate/summary.py`, `gate/codes.py`, `config.py` (`GateConfig(approval_ttl_s)`, `OpsConfig(repo_path, uv_path)`).
  - Summaries are built by code: sanitized values, `+N more args` / `(truncated)` markers, `unsafe_args()`. A test pins "vitamin d3" → `vitamin ?` as an accepted cosmetic loss.
  - **Tests:** `origin` isn't in `hash`; destructive text format; injection corpus.
- **P2-4**
  - **Files:** `gate/replies.py`.
  - **Tests:** the grammar, the negatives (`y`, `ok`, `yes all`, `yes A7 and C4`, look-alike codes, pasted prompt), emoji variants, and the parser-path lines of `evals/approvals.example.jsonl`. Marks the classifier and batches deferred in `interfaces.md`.
- **P2-5**
  - **Files:** `tools/projects.py`, renderer in `router/render.py`.
  - **Tests:** ordering by `updated_at`, the 15/50 caps, titles never contain message bodies, every state labeled.
- **P2-6**
  - **Files:** `gate/policy.py`, `gate/__init__.py`, `executor.py` (`used_at`, `recorded`), `tools/__init__.py` (`STORE_INTERNAL_TOOLS`).
  - **Tests:**
    - the full D1 table × autonomy (on, off, garbage) × dry_run (exempt, non-exempt)
    - the exempt set is pinned, none of its tools has secrets, and an extension tool with the same name is not exempt
    - L1 explicit still asks with `require_code`
    - reuse of a pending approval on re-evaluate
    - single-use token; `used_at` refusal; `Veto` never returned
- **P2-7**
  - **Files:** `tools/notes.py`.
  - **Tests:** soft delete hides the note; `clear_notes` counts in its summary; replay doesn't duplicate; text over 200 chars → `ToolFailed`.
- **P2-8**
  - **Files:** `tools/when.py`, `tools/reminders.py`, `runners/reminder.py`, `loop/types.py` (`NewTask.wake_at`), `store/tasks.py` (`insert_task` wake_at, `next_wake_at`), `loop/loop.py`.
  - **Tests:**
    - parser table: zone, DST gap and fold, past, ambiguous, weekday rollover, `tomorrow` at 23:59
    - fires once with dedupe; late marker; survives a new loop on the same DB; cancel prevents firing
    - loop wakes within 1 s of `wake_at` with a fake clock
- **P2-9**
  - **Files:** `components.py`, `wiring.py`, `daemon.py`, `api.py`, `status.py`, `tests/test_e2e_phase2.py` (scaffold + E15).
- **P2-10**
  - **Files:** `gate/policy.py`, `gate/types.py` (C5).
  - **Tests:** the binding matrix including `require_code`; two connections racing; the same `event_id` twice; a reply sent before the prompt never binds; the generic reply for non-approvers.
- **P2-11**
  - **Files:** `runners/local.py`.
  - **Build:**
    - D3 order: save → prompt → record ref → `Wait`
    - re-prompt on every pending step; `recorded()` before the gate; pinned key
    - templated replies: done, not done, expired, ambiguous, `Dry run: would …`, `Done: <summary>`
    - `direct_call` sets `origin="explicit"`, and the model path is always `inferred`
    - the tool loop stops at the first gated side effect
    - `_context` skips earlier messages that started no task (approval replies)
  - **Tests:**
    - crash after `ctx.save(exec)`, before `started` → runs exactly once
    - crash after `started` → ambiguous
    - send failure after execute → replay with no gate call
    - crash between save and prompt → the prompt is sent on retry
    - the model can't produce `origin=explicit`
- **P2-12**
  - **Files:** `router/approvals.py`, `router/router.py`, `router/fast_path.py` (`/approvals [code]` shows full args), `gateway/imessage/channel.py` (`request_approval`, both texts, dedupe, audit `imessage.approval_sent` without a summary).
  - **Tests:** `/kill` with pending approvals; plain `yes` with 0, 1 or 2 pending; truncated payload; router retry sends no second reply; a `yes` that arrives while the task is `running` is caught by the `Wait` requeue.
- **P2-13**
  - **Files:** `cli.py`.
  - `approve|deny <id|code>` prints the summary, then resolves (`path="cli"`) + `wake_resolved` in one transaction, using `open_existing`.
- **P2-14**
  - **Files:** `tools/ops.py`, `ops/restart_later.py`, `wiring.py` (step limit, "back up" on start).
  - **Tests** (fake subprocess runner): argv shapes; timeouts; dirty, off-`main`, already-current and sync-failure paths for `/update`; the reply is recorded before the helper fires; a replayed task doesn't restart twice; log scrubbing.
- **P2-15**
  - **Files:** `router/commands.py`, `router/fast_path.py`.
  - Arg parsers for each command, usage replies, `checkpoint.title`, owner-only checks.
  - e2e: E1, E2, E4, E6, E8, E9, E10, E11.
- **P2-16**
  - **Files:** `router/intents.py`, `router/prefixes.py`, `tests/fixtures/router/label_prefix.json`, `evals/router.example.jsonl`, `e2e/fakes.py` (`ScriptedModel` rules).
  - e2e: E3 and a natural-language reminder.
- **P2-17**
  - E5, E7, E12, E13, E14; `docs/go-live.md`, `scripts/go_live_check.py`, `architecture.md` / `.html`, `extensions.md` levels, the Decisions log (D1, D4, D7, D8).

---

## 5. Risks and security considerations

| Risk | Mitigation |
|---|---|
| The model forges consent | `origin` is set only by the runner's `direct_call` path, whose checkpoint only the router's deterministic parser writes. The model path is hard-coded `inferred`, and a test asserts it (P2-11). |
| A mistyped command runs the wrong thing | Fuzzy matching only suggests; it never executes (P2-1). |
| Destructive actions on a plain `yes` | L1 always needs `yes <code>`, whatever the origin (P2-6, P2-10). |
| Prompt injection into summaries | Built by code, sanitized, lossiness marked, unsafe characters denied, full args via `/approvals <code>` (P2-3). |
| Replay, a late reply, races, CLI vs daemon | Sent-before rule, `resolved_event_id`, a single conditional UPDATE, event dedupe (P2-2, P2-10). |
| Restart or token loss; double execution | Durable approvals, memory-only tokens, `wake_at` backstop, pinned key, `recorded()`, `used_at` (P2-6, P2-11). |
| Orphaned approvals | Expired when the task goes terminal; `task_not_waiting` (P2-2). |
| `/restart resonant` kills its own reply | Detached 3 s delay + graceful drain. The reply is resent from the checkpoint on start, and `executions` replay prevents a second restart (P2-14). |
| `/update` brings in bad code | Owner-only; ff-only from `main` on a clean tree; no restart on a failed sync; old→new reported. It is effectively remote code execution from the owner's own GitHub repo, which is accepted. Inferred `/update` asks. |
| A reminder floods or duplicates | A dedupe key per reminder; text ≤ 200; outbound rate limit per principal; fired once even after downtime. |
| Log exfiltration through `/logs` | Owner-only, ≤ 20 lines, URLs and handles scrubbed. Logs never carry bodies or secrets by design. |
| Time-parse mistakes | Ambiguous, past and DST-gap times are rejected with a usage reply. The reply echoes the resolved time (`⏰ set for Tue 3:00pm`). |
| Clock skew on the sent-before rule | A lagging phone clock may refuse an instant reply, and the owner re-sends. Fail-safe; checked live. |
| Local same-user code | Outside the gate's threat model (D11); runner sandboxing deferred. |

---

## 6. Rollout

### Go-live step 11 (rewritten)

**Stage A: `dry_run: true`.** Deploy with `git pull && uv sync && resonant daemon install`; the log shows `gate=policy dry_run=True`. Then:
1. `/note milk` runs with no prompt. `/notes`, `/unnote 1`, `/notes` (E1).
2. `/notes clear`, then plain `yes` is refused; `yes D?` clears (E4). Repeat and reply `no`.
3. `/remind in 2 min test` fires. `/remind in 5 min x`, then `resonant daemon install`, and it still fires. `/remind 3 x` gets a usage reply (E6–E8).
4. `/restart ollama` replies `Dry run: would …` (E15). Then `/projects`, `/help`, `/help rem`, `/remnd` (E10, E11).
5. `/kill`, then `/note y` asks; `/resume` (E5).

**Stage B: flip.** Set `dry_run: false`, run `resonant daemon install`, and `resonant status` shows `dry_run False`. Then:
1. `/disk`, `/logs 5`.
2. `/restart ollama`; `resonant model probe` passes.
3. "please restart ollama" (inferred L3) runs. "please restart resonant" (inferred L2) asks: reply `yes` → restart → "back up" (E3, E2).
4. `/update` on a clean tree (or `Already up to date`). Touch a file in the repo, then `/update` is refused (E9). `git checkout .`.
5. Inferred L2 pending, then `kill -9 <pid>`, then `yes` → runs once (E12).
6. `resonant approvals list` is empty.

### `scripts/bootstrap.sh --check`

| Check | Required | Pass when |
|---|---|---|
| `dry_run` | yes | `true` passes. `false` passes only when the store is at schema 3 (Phase 2 gate installed); otherwise FAIL (`dry_run off on a pre-Phase-2 install`). |
| store schema 3 | yes | `open_existing` succeeds |
| kill-switch rows | yes | `autonomy` ∈ {on, off}, `paused` boolean. `autonomy off` is also an advisory FAIL. |
| gate and ops config | yes | `approval_ttl_s` in bounds; `ops.repo_path` is a git checkout on `main`; `ops.uv_path` is executable |
| timezone | yes | `settings.timezone` loads in `zoneinfo` |
| repo clean | advisory | `git status --porcelain` is empty (otherwise `/update` will refuse) |
| stale approvals | advisory | No pending rows past `expires_at` + 1 h |

---

## 7. Deferred and out of scope

**Deferred to follow-ups** (the coordinator adds `followups.md` entries):
- **Veto windows (L2 "unless you stop")** → Phase 3, for unattended jobs. `Veto` stays an unused type. The v2 lateness and watermark design is carried into that entry.
- **Keychain `SecretStore` and executor env injection** → Phase 3, with the first tool that needs a secret (`security` CLI, `RESONANT_SECRET_<NAME>`, exact-value redaction, as in v2).
- **Approval classifier** and `resonant eval approvals`. Parser-only for now.
- **Batch approvals** (`yes all`). A re-ask with codes covers ambiguity.
- **GitHub tools** (not picked by the owner for now).
- **Note undo** (soft-deleted rows are kept for it).
- **Sandboxing Phase 3/5 runners with shell access** from `~/.resonant`, the `resonant` CLI and `/usr/bin/security` (D11).

**Dropped:** code cooldown (replaced by the sent-before rule); `approvals verify` evidence and kv markers; extra approvals columns (the audit carries them).

**Later phases:**
- Phase 3: extension tools and manifest approvers; scheduled jobs (always `inferred`); L3 reports for jobs with no requester.
- Phases 5–6: Slack approvals; on-call paging.
- Live pay tools.
- Phase 7: computer-use `commit` scopes.
- Binding by iMessage thread.
- Phase 4: dashboard approvals UI.
- Phase 5: Claude runner continuing after approval with comments (`run_project` items will show in `/projects`).
