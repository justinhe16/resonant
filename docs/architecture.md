# Resonant architecture

Resonant is an always-on personal agent harness that runs as a daemon on a dedicated home Mac mini. It takes events in from iMessage, Slack, webhooks, schedules and extensions. A local LLM handles simple routing and tool calls. Hard reasoning and coding are supervised Claude Code sessions. Every side effect passes a policy gate. Product logic lives in **extensions**, which are separate repos that plug in through `resonant.yaml`.

For an interactive version, open [`architecture.html`](architecture.html), which has a clickable graph, the lifecycle, the approval flow, the roadmap and the trust model. The frozen contracts are in [`interfaces.md`](interfaces.md).

## Shape

```
 iMessage (owner) ─────┐ principal map   ┌── runner=local : router → qwen3 (≤3 tool calls)
 Slack (owner+cofndr) ─┤ (identity →     │── runner=claude: Agent SDK session in a worktree
 Webhook (HMAC, TBD) ──┼─> principal) ─> events ─> Loop ─┤              (slot pool, priority)
 Scheduler / ext emit ─┤                 (event+tick)     └── runner=cu    : computer use (Phase 7)
 Claude SDK events ────┘                                         │ tool calls
                       Permissions(principal, extension) → Gate(level, scope, approvers) → Executor
                         (idempotency key, Keychain secrets, audit: requested_by / approved_by)
   Store (SQLite WAL): tasks · events · approvals · executions · audit_log · spans · decision_log · memory · kv
   FastAPI on 127.0.0.1 (loopback only)     │   watchdog thread + dead-man ping   │   LaunchAgents
```

- **One asyncio process** runs the loop and the local API. Extensions, MCP servers, Claude sessions and computer-use workers run as child processes, so a crash never takes down the core.
- **launchd supervises it** as a per-user LaunchAgent (`gui/<uid>`). Messages.app, computer use and the login Keychain all need the GUI session. Ollama has its own LaunchAgent (`OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_CONTEXT_LENGTH=32768`). Extension jobs run on the daemon's scheduler. Critical jobs with their own LaunchAgents (through `resonant job-guard`, which honors the kill switch) are deferred ([`followups.md`](followups.md#critical-job-launchagents-and-job-guard)).
- **There are no public ports.** The API binds to loopback only, and config validation enforces it. iMessage reads `chat.db` locally. Slack uses Socket Mode. Inbound webhooks have no transport yet: with no tailnet, how third parties reach `/hooks` without a public port is an open follow-up (see [`followups.md`](followups.md#inbound-webhooks)). They must still be HMAC-signed. CI status is polled, never pushed to us. Remote status access is planned as a push-only, read-only relay, so this rule stays: see [`followups.md`](followups.md#status-relay-dead-man-switch-and-remote-dashboard).
- **Runtime state** lives in `~/.resonant/`: `config.yaml`, `principals.yaml`, `resonant.db`, `logs/`, `worktrees/`, `evals/`, `extensions/` (approved read-only copies) and `ext-data/` (each extension's writable state).

## Components

| Component | Where | Status |
|---|---|---|
| Config (YAML + `RESONANT_*` env, loopback-only API, `dry_run` default on) | `resonant/config.py` | Phase 0 ✅ |
| Principals and permissions | `resonant/principals.py` | Phase 0 ✅ |
| Store: migrations, kv, audit, task/event repos | `resonant/store/` | Phase 0 ✅ |
| Generic loop, task envelope, slot pool | `resonant/loop/` | Phase 0 ✅ |
| Daemon and local API (`/healthz`, `/api/status`) | `resonant/daemon.py`, `resonant/api.py` | Phase 0 ✅ |
| LaunchAgents (daemon, Ollama) | `resonant/launchd.py` | Phase 0 ✅ |
| Keep-awake LaunchAgent (`caffeinate -i -m -s`, `power.keep_awake`), installed by `resonant daemon install` | `resonant/launchd.py`, `resonant/cli.py` | Phase 2 ✅ |
| CLI: `daemon`, `status`, `kill`, `resume`, `job-guard` | `resonant/cli.py` | Phase 0 ✅ |
| Health: watchdog thread, dead-man ping | `resonant/health.py` | Phase 0 ✅ |
| Observability: JSON logs, spans | `resonant/observability/` | Phase 0 ✅ |
| Contracts: Event, ToolSpec, Manifest, ToolRegistry, Gate (tokens, reply binding), Channel, Executor | `resonant_sdk/`, `resonant/tools/`, `resonant/gate/`, `resonant/gateway/`, `resonant/executor.py` | Phase 0 ✅ (DryRunGate) |
| Extension authoring guide + `resonant ext validate` | `docs/extensions.md`, `resonant/cli.py` | Phase 0 ✅ |
| Built-in read tools (`list_tasks`, `task_counts`, `system_status`, `daemon_status`, `model_status`); registry, `DryRunGate` and executor wired into the daemon; `GET /api/tools` | `resonant/tools/builtin.py`, `resonant/components.py`, `resonant/api.py` | Phase 1 ✅ |
| Model client (Ollama native or OpenAI-compatible), `resonant model probe/bench` | `resonant/models/`, `resonant/cli.py` | Phase 1 ✅ |
| Router: deterministic fast path (slash commands, anchored keywords, owner-only `/kill`), one-call LLM intent label with stable prompt prefixes, principal-filtered toolsets | `resonant/router/` | Phase 1 ✅ |
| Brain health: `HealthMonitor` (model, imessage, loop, store checks), owner alerts over iMessage, dead-man `/fail` reasons, `GET /api/channels`, `/api/status.health` | `resonant/monitor.py`, `resonant/health.py`, `resonant/api.py` | Phase 1 ✅ |
| Local runner: ≤3 read-tool calls through gate and executor, number-guarded reply over the channel, crash-safe resend | `resonant/runners/local.py` | Phase 1 ✅ |
| iMessage channel: chat.db reader (WAL watch + polling, principal map, self-handle skip), osascript sender (argv only, allowlist, dedupe, rate limit), self-test, `resonant selftest imessage` | `resonant/gateway/imessage/` | Phase 1 ✅ |
| Phase 1 wiring: channel → loop → router → local runner → channel, health monitor, clean SIGTERM; off by default (`channels.imessage.enabled`); in-process E2E tests and `resonant bench e2e` | `resonant/wiring.py`, `resonant/daemon.py`, `resonant/e2e/`, `tests/test_e2e.py` | Phase 1 ✅ |
| Mini go-live checklist, `scripts/bootstrap.sh --check` | `docs/go-live.md`, `scripts/` | Phase 1 ✅ |
| Slack notifier | — | Not in core (no Slack in core; extensions own Slack) |
| Router evals: `resonant eval router` (production `classify`, accuracy, confusion, p50/p95 per path; texts only in terminal output) | `resonant/evals/`, `evals/README.md` | Phase 1 ✅ |
| Policy gate: risk × origin (`Intent.origin`), kill switch read on every call, dry_run exemption for store-internal builtins, approval expiry and single use; executor `recorded()` | `resonant/gate/policy.py`, `resonant/executor.py` | Phase 2 ✅ |
| Approvals: store and migration 003, deterministic reply parser, binding (sent-before, per-event idempotency, orphan expiry), router approval inbox, `request_approval`, `/approvals`, `resonant approvals list\|show\|approve\|deny` | `resonant/store/approvals.py`, `resonant/gate/replies.py`, `resonant/router/approvals.py`, `resonant/cli.py` | Phase 2 ✅ |
| Local runner: `direct_call` (explicit), Wait/resume on approval, execute once, code-built reports, one gated side effect per task | `resonant/runners/local.py` | Phase 2 ✅ |
| Notes (`/note`, `/notes [clear]`, `/unnote`; soft delete) | `resonant/tools/notes.py` | Phase 2 ✅ |
| Reminders (`/remind`, `/reminders`, `/unremind`; durable tasks, `ReminderRunner`, loop next-wake) | `resonant/tools/reminders.py`, `resonant/tools/when.py`, `resonant/runners/reminder.py` | Phase 2 ✅ |
| Mini ops (`/restart ollama\|resonant`, `/update`, `/disk`, `/logs`; "back up" after a restart) | `resonant/tools/ops.py`, `resonant/ops/` | Phase 2 ✅ |
| `/projects`: all work in progress by state | `resonant/tools/projects.py` | Phase 2 ✅ |
| Command registry: every slash command, `/help [word]`, "did you mean" suggestions that never execute, `list_commands` | `resonant/router/commands.py`, `resonant/router/fast_path.py` | Phase 2 ✅ |
| Chat intents `notes`, `reminders`, `ops` (model-chosen calls are `inferred`) | `resonant/router/intents.py`, `resonant/router/prefixes.py` | Phase 2 ✅ |
| Go-live Phase 2: stage A / B checklist, `--check` rows for store, dry_run, kill switch, ops, keep-awake, stale approvals | `docs/go-live.md`, `scripts/go_live_check.py` | Phase 2 ✅ |
| Extension manager: terminal `resonant ext validate\|add\|update\|remove\|list` (approved read-only copy, commit + tree-digest pins, escalations-first review, `--adopt`, every update re-approved) | `resonant/extensions/manager.py`, `sources.py`, `pin.py`, `checks.py`, `resonant/cli.py` | Phase 3 ✅ |
| Extension host and process runner: load rows, re-verify pins (tamper → `disabled_tampered` + one alert), register tools and job specs; env from scratch, output caps, process-group and tracked-tree kill, fd-3 events | `resonant/extensions/host.py`, `handlers.py`, `process.py` | Phase 3 ✅ |
| Keychain secrets (`SecretStore` over `/usr/bin/security`, `resonant secret set\|check\|list\|rm`), per-tool injection, redaction of every encoding | `resonant/secrets.py`, `resonant/redact.py`, `resonant/executor.py` | Phase 3 ✅ |
| Scheduler and `JobRunner` (cronsim, pause wins, catch-up, overlap; L0 inline with no task row; L3/L2/L1 gated tasks; reports to approvers); origin `scheduled`; manifest approvers ∩ grants | `resonant/extensions/scheduler.py`, `cron.py`, `resonant/runners/job.py`, `resonant/gate/`, `resonant/runners/approve.py` | Phase 3 ✅ |
| Notifier from persisted events (exactly once, mute, 20/h cap, silent baseline, anti-spoof) and `ext:<name>` health | `resonant/extensions/notify.py`, `resonant/monitor.py` | Phase 3 ✅ |
| Core daily briefing (`core/briefing`, 08:00) | `resonant/extensions/briefing.py` | Phase 3 ✅ |
| Router: `extension:<name>` intents and toolsets, `<prior_reply>` wrapping; `/<ext> <tool>`, `/help <ext>`, `/ext list\|run\|mute` | `resonant/router/`, `resonant/runners/local.py`, `resonant/tools/ext.py` | Phase 3 ✅ |
| `github` extension: `prs`, `pr`, `watch`, `digest`; `rerun_failed` (L3), `merge_pr` (L1) | `extensions/github/` | Phase 3 ✅ |
| Go-live Phase 3: step 12, `--check` rows for extensions, pins, secrets, jobs, briefing | `docs/go-live.md`, `scripts/go_live_check.py` | Phase 3 ✅ |
| Pulse: extensions `report()` items (key, title, urgency, coarse state), stored in `pulse_items`; urgent texts at once at any hour, normal rides the hourly `core/pulse` "btw" (quiet 00:00-06:00), low and everything open ride the 08:00 briefing's Updates; told once per state change; `/pulse`, `/pulse now` | `resonant/store/pulse.py`, `resonant/extensions/pulse.py`, `pulse_job.py`, `briefing.py`, `resonant_sdk/emit.py` | Phase 3.5 ✅ |
| Go-live Phase 3.5: step 13, `--check` row `pulse scheduled` | `docs/go-live.md`, `scripts/go_live_check.py` | Phase 3.5 ✅ |
| MCP tools, critical-job LaunchAgents and `job-guard` rewrite, veto windows, Seatbelt sandbox, iMessage `/ext add`, git and network sources, Python deps | — | Deferred ([`followups.md`](followups.md)) |
| Dashboard (Vite + React + shadcn, streak look) | — | Phase 4 |
| Two-way Slack, Claude runner | — | Phase 5 |
| On-call extension | — | Phase 6 |
| Life lane: Gmail, Calendar, iMessage, payments, browser, computer use | private repo, with core runner/scope support | Phase 7 |

## Phase 1 request path and latency

```
chat.db row ─(WAL watch 250ms + 100ms debounce; 2s poll fallback)─> IMessageReader ─> loop.submit
  ─> Router.route: fast path (one gated builtin read, templated reply)  │ or one LLM label call
  ─> NewTask(runner="local") ─> LocalRunner: send fast_reply │ ≤3 tool calls (gate → executor) + answer
  ─> IMessageChannel.send (dedupe "reply:<task id>") ─> osascript
```

The Phase 1 exit criterion is **text → answer in 2–3s**: p50 ≤ 1s on the fast path and p50 ≤ 3s on the labeled path, measured by `resonant bench e2e` from the chat.db row insert to the osascript call (Messages.app delivery and osascript's own run time excluded; the self-test covers that path).

| Run | Model | fast p50 / p95 | llm p50 / p95 |
|---|---|---|---|
| Dev machine, `--model fake` (n=20; CI runs the same code) | scripted | 354 / 370 ms | 354 / 361 ms |
| Mac mini (Apple M5 Pro, 64 GB), macOS 27.0.1, Ollama 0.40.3, `--model real` (n=20, 2026-10-13) | `qwen3:30b-a3b-instruct-2507-q4_K_M` (Ollama) | 353 / 367 ms | 1531 / 2129 ms |
| Same Mini, after Phase 3.5 (n=20, 2026-10-14) | same | 354 / 366 ms | 1384 / 2335 ms |

`resonant model bench` on the same run: label p50 / p95 86 / 89 ms, single tool call 229 / 232 ms, 0/20 failures.

With the scripted model nearly all of the time is the reader's WAL-watch interval plus its debounce, so the harness overhead is about 0.35s; on the Mini the labeled path adds the label call, up to three tool rounds, and the answer call.

## Task lifecycle

```
queued ──dispatch──> running ──Continue──> queued
                        │──Wait(reason, wake_at?)──> waiting ──event or wake_at──> queued
                        │──Done──> done
                        │──Fail(retryable, attempts left)──> queued (backoff or retry_at; events kept)
                        └──Fail(final) / cancel ──> failed / cancelled
wait reasons: approval · human_reply · usage_reset · ci · veto (Phase 3; unused in Phase 2)
```

- One transaction per step commits the outcome, the checkpoint and the consumed events together.
- A crash mid-step leaves the task `running` with its last checkpoint and its events unconsumed. On the next start it is requeued, and the crash counts as an attempt, so crash loops are bounded. A graceful shutdown requeues without using an attempt.
- Runners save anything a retry must not lose with `ctx.save(...)`. For example, `claude_session_id` is saved as soon as the session starts.
- Side effects are idempotent through `executions`. A key that succeeded is never re-run. A key that started but never finished is reported as ambiguous instead of being retried blindly.

## Scheduling and priority

- Priority is set by lane and principal, never by the model: **on-call 0 < owner 1 < co-founder 2 < background 3**.
- At most `max_parallel` steps run at once, and the last parallel slot is kept for on-call.
- Claude sessions also need a slot from `SlotPool`. The cap defaults to 5, with 1 slot reserved for on-call.
- A slot is held only while a step is in flight. A session that needs to wait (approval, human reply, usage reset, CI) returns `Wait`, which frees the slot, and resumes later by `claude_session_id`. Running sessions are never preempted.
- Usage limits are not throttled ahead of time. When a limit is hit, the affected tasks are parked with `wait_reason=usage_reset` and `wake_at`, they resume automatically, and you are notified once.

## Trust model

- **Identity grants permissions. Content never does.** Every message, email, web page and tool output is untrusted input.
- **Principals.** `principals.yaml` maps channel identities to people. Exactly one owner can do everything, including admin (kill switch, global settings). Everyone else has explicit per-extension `request` and `approve` grants. The co-founder is limited to DORI and can't reach the personal lane, trading, the owner's data, or global settings.
- **Permission is checked before anything else.** A principal without `request` on an extension is denied before any level logic runs. Toolsets are filtered by principal before the model sees them, so a prompt injection can't reach tools the requester lacks.
- **Effects come from registration** (`ToolSpec` or the manifest), never from the model.
  - `pay` is always L1.
  - A read with no secrets is always L0.
  - MCP tools must be typed in the manifest, and unlisted ones are refused (MCP itself is deferred; `ext add` refuses an `mcp` block today).
- **The gate weighs risk against who chose the action** (Phase 2). The level comes from registration; the origin is `explicit` only for a slash command the router parsed (the local runner's `direct_call` path), `inferred` for anything the model chose, and `scheduled` for a job that fired on its schedule (Phase 3). `scheduled` is never explicit: L0 runs silently, L3 runs and reports to the approvers, L2 asks, L1 asks with a code. An extension's approvers are the owner plus the principals its manifest names that hold `approve` for it in `principals.yaml`; prompts go to every reachable approver.

  | Level | Explicit (slash command) | Inferred (model) | Kill switch on |
  |---|---|---|---|
  | L0 read | Silent | Silent | Silent |
  | L3 small, reversible | Run, then report | Run, then report | Ask (plain `yes`) |
  | L2 medium | Run, then report | Ask (plain `yes`) | Ask (plain `yes`) |
  | L1 destructive or pay | Ask, `yes <code>` only | Ask, `yes <code>` only | Ask, `yes <code>` only |

  There are no veto windows yet: unattended L2 and L1 jobs ask, and asking jobs may fire at most hourly. `Veto` stays in the contract ([`followups.md`](followups.md#veto-windows-for-unattended-jobs)). Under `dry_run`, only the store-internal builtins (notes, reminders) run; everything else that isn't an L0 read replies `Dry run: would …`.
- **Approvals are text replies on iMessage**, bound to a 2-character code and an intent hash. They expire (`gate.approval_ttl_s`, 30 min) and are single use.
  - A deterministic parser reads the whole message (`yes`, `no`, `yes A7`, `A7 no`, `yes A7 with comments: …`). There is no classifier and no `yes all` yet ([`followups.md`](followups.md)).
  - A plain `yes` binds only when exactly one approval is pending, and only to a prompt sent before the reply. Each chat.db event resolves at most once.
  - L1 (including pay) needs an exact `yes <code>`.
  - Slash commands are checked first, so `/kill` is never read as a reply.
  - SMS-delivered messages are ignored.
  - Summaries are built by code from the registry and the args: sanitized, clipped, with losses marked. `/approvals <code>` shows the full args.
- **One side effect per task, and a residual risk.** Within one task, a tool read's output (untrusted) can still steer the model's choice of an L3 write, which runs without asking. Mitigations: the local runner stops at the first gated side effect, so one injected read buys at most one small, reversible effect; toolsets are owner-only and filtered by intent; and the runner prompt treats tool output as data. L2 and L1 always ask when the model chose them.
- **The model never sees secrets.** The executor (and the scheduler, for L0 jobs) reads a tool's or job's declared secrets from the Keychain just before the call and passes them as `RESONANT_SECRET_<NAME>` in that process's environment only. Every encoding of a value (raw, URL, JSON-escaped, base64 at three alignments) is redacted before anything is stored, logged or replied. Secrets are entered only in the Mini's terminal (`resonant secret set`), never over iMessage.
- **Phase 3 extensions: an interim trust bar.** Extensions are first-party, owner-installed code (this repo or a local git work tree), installed only from the Mini's terminal. They run as the same macOS user and inherit the daemon's Full Disk Access, so they can read `resonant.db`, chat.db, other extensions' data and the Keychain like any same-user process. Phase 3's protections are env and secret isolation, not filesystem isolation:
  - an environment built from scratch, with `HOME` and `TMPDIR` in the extension's data dir;
  - per-tool secret injection and redaction;
  - output caps, and a process-group plus tracked-tree kill;
  - an argv[0] allowlist (a `.py` file in the extension, `python3`, `gh`, `git`), stdlib only, Python under `-I -S`;
  - a pinned, copied, read-only tree, re-verified at every start.

  **Keychain ACL residual.** Items are created by `/usr/bin/security` and trust it, so any same-user caller can read every `resonant/*` item, including the owner's own Claude Code sessions and any extension. Accepted for now; tokens are scoped minimally (the github PAT is read-only unless the owner wants the writes). No third-party sources until the Seatbelt sandbox lands ([`followups.md`](followups.md#seatbelt-sandbox-for-extension-processes)).
- **trade-jev order execution never goes through the loop or gate.** Core only sees its jobs, read tools, events and kill switch.
- **The kill switch** sets `autonomy=off`, so everything above L0 asks first (L1 still needs its code), and `paused=true`: due jobs are skipped and never caught up, in-flight extension processes are killed, and notifications and the briefing are held (not sent later). A missing or garbage `autonomy` counts as off.
- **Local code is outside the gate's threat model.** The gate protects against remote principals, iMessage content and the model, not against processes running as the same macOS user: those can run `resonant approvals approve` or `resonant resume`, write `~/.resonant/resonant.db`, and read the Keychain. Phase 3 extension processes are such processes (see the interim bar above); they and Phase 5 runners with shell access must be sandboxed from all three ([`followups.md`](followups.md#runner-sandboxing-phase-35)).

## Health

- **Watchdog.** An asyncio heartbeat beats every second, and a separate thread calls `os._exit(1)` if it goes stale. launchd then restarts the daemon.
- **Dead-man switch.** While the loop is ticking, the daemon GETs the configured dead-man URL (`health.healthchecks_url`); when it isn't, it POSTs `<url>/fail` with the reason. A whole-process freeze (SIGSTOP), a power cut, or a reboot waiting at the FileVault login all stop the pings, and the receiver alerts on the silence. The planned receiver is the status relay, which isn't built yet: see [`followups.md`](followups.md#status-relay-dead-man-switch-and-remote-dashboard).
- **iMessage self-test (Phase 1).** The health monitor runs it on its first round after start whenever it is due (none yet, older than 6h, or a failure older than 30 min) and re-runs it every 6h; a failure (AppleScript or the `chat.db` schema broke) degrades the `imessage` check. It sends a random nonce to Resonant's own handle and waits for the reader to see it; the result is kept in kv `imessage.selftest`. `resonant selftest imessage` runs the same check by hand, next to a live daemon.
- **Health monitor (Phase 1).** `resonant/monitor.py` evaluates `model`, `imessage`, `loop` and `store` every 60s, keeps an `ok → degraded → ok` state per check in kv `health.<check>`, and texts the owner over iMessage ("hey, some issues here: …", then "resolved: …"), at most once per check per 6h. Slack is never involved: it belongs to extensions. When iMessage itself is degraded, nothing is sent over it, and the dead-man `/fail` ping carries the reason as a plain-text body instead. State shows in `/api/status.health`, `GET /api/channels` and `resonant status`.

## Decisions log

| Date | Decision |
|---|---|
| 2026-10-08 | Hybrid execution: Phase 0 is built in one context to freeze contracts. Later phases are ticketed one at a time (`/linear-surf`) and run in parallel (`/robot-surf-fan`). |
| 2026-10-08 | Ollama with `qwen3:30b-a3b` (Q4), thinking off for tool calls. The client is OpenAI-compatible, so MLX is a config swap later. |
| 2026-10-08 | The model client defaults to Ollama's native `/api/chat` (`model.api: ollama`), where `think: false` is the documented switch for qwen3. `/v1` stays available (`model.api: openai`) for a later MLX swap. `resonant model probe` verifies thinking is really off for whichever is configured. |
| 2026-10-08 | Durable task envelope on SQLite. No workflow engine and no generic queue. |
| 2026-10-08 | uv workspace with `resonant` and `resonant-sdk`. Extensions depend only on the SDK. |
| 2026-10-08 | LaunchAgent (GUI session), not LaunchDaemon. FileVault stays on, with a UPS and `fdesetup authrestart` for planned reboots. Manual login after an unplanned reboot is accepted. |
| 2026-10-08 | iMessage replaces Telegram. Resonant has its own Apple ID. Slack is shared and is the fallback channel. |
| 2026-10-08 | Approvals by text reply with codes, a two-stage parser and classifier, and a strict pay path. |
| 2026-10-08 | Resonant's Apple ID uses an email handle and iMessage only (`channels.imessage.self_handle`). Principals may list several handles. Outbound goes only to principals' handles. |
| 2026-10-08 | `yes all` approves a stored batch snapshot, never pay. On-call pages go to iMessage and Slack, and the first resolution wins. |
| 2026-10-08 | Public repo from Phase 0. gitleaks runs in pre-commit and CI. Only `*.example` configs ship. Real eval sets stay out of the repo. |
| 2026-10-08 | LaunchAgent plists are generated with `plistlib` instead of template files. |
| 2026-10-13 | Default model is `qwen3:30b-a3b-instruct-2507-q4_K_M`, the non-thinking Instruct-2507 build. Ollama's `qwen3:30b-a3b` tag now points at the thinking-only Thinking-2507 build, which ignores `think: false` and writes ~100-500 tokens of untagged reasoning before each tool call (llm path p50 6.4s). `resonant model probe` now also makes a tool call and fails if reasoning leaks or no call arrives. |
| 2026-10-08 | `waiting(ci)` polls CI through `gh` on the tick. No inbound GitHub webhooks. |
| 2026-10-08 | Router fast-path reads go through the gate but not the executor: no task exists at route time, and builtins are L0 reads with no side effect. `/kill` and `/resume` are deterministic and owner-only, never decided by the model. |
| 2026-10-08 | Manifests load with a YAML loader where only true/false are booleans, so `notify: {on: [...]}` stays a string key. |
| 2026-10-08 | The local runner replays tool calls to the model as assistant/user turns (no `tool` role in `ChatMessage`), guards every number in its reply against tool output and the user's message, and escalates run_project or >3 calls with a templated reply instead of calling Claude. |
| 2026-10-13 | Plain chat reaches the Phase 2 tools through the model: new intents `notes`, `reminders` and `ops` (and `projects` / `list_commands` in `job_control` / `system`). Model-chosen calls are always `inferred`, so the gate asks before L2 and L1 effects. The reminders turn carries the current local time after the stable prefix, so the model can pass `at` as an ISO datetime. |
| 2026-10-08 | Phase 1 is wired in one place (`resonant/wiring.py`) and only when `channels.imessage.enabled`; disabled, the daemon is exactly Phase 0. The local runner's step limit is derived from the model timeout (`(3 + 1) × timeout_s + 15s`), so the model client times out first and the user always gets a fallback reply. Shutdown stops the reader and monitor before the loop drains, so in-flight replies still go out. |
| 2026-10-13 | Phase 2 gate: risk × origin. A slash command is consent (explicit L3/L2 run and report), a model-chosen action asks at L2, L1 always needs `yes <code>`, and the kill switch makes everything above L0 ask. `origin` is set only by the router-written `direct_call` path. No veto windows in Phase 2 (Phase 3, for unattended jobs). |
| 2026-10-13 | Phase 2 approvals are parser-only, with the sent-before rule and per-event idempotency; the classifier and `yes all` batches from 2026-10-08 are deferred. One gated side effect per local task. |
| 2026-10-13 | `dry_run` exempts only store-internal builtins (notes, reminders), keyed on registry identity; ops tools are never exempt. Go-live flips `dry_run: false` after stage A of the checklist, so the remote ops work. |
| 2026-10-13 | Reminders are durable queued tasks with `wake_at` and a `ReminderRunner`, not a scheduler. A missed one fires once on start, marked late. `/remind` passes the routing time, so a late-running command still sets it. |
| 2026-10-13 | Ops reply first, then restart: `/restart resonant` spawns a detached helper that kickstarts the daemon after 3s, and the next start says "back up". `/update` is `git pull --ff-only` on a clean `main`, then `uv sync --frozen`; a failed sync doesn't restart. |
| 2026-10-13 | The Mini's keep-awake `caffeinate` LaunchAgent is part of the product (`power.keep_awake`), under the label already hand-installed on the Mini, so the install adopts it. |
| 2026-10-14 | Phase 3 extensions are first-party and owner-installed, from the Mini's terminal only (`resonant ext add repo:<name>\|path:<dir>`). The install copies the approved tree to a read-only `~/.resonant/extensions/<name>/`, pins commit and tree digest, and the daemon re-verifies it at every start; a changed copy is disabled with one alert. Every update is a full re-approval. The registry of record is the `extensions` table, not `extensions.yaml`. |
| 2026-10-14 | Extensions are stdlib only, with no install step. Python runs under the daemon's interpreter with `-I -S`; argv[0] must be a `.py` file in the extension, `python3`, `gh` or `git`. Each process gets an environment built from scratch with only its own declared secrets. Protection is env and secret isolation, not filesystem isolation, until the Seatbelt sandbox. |
| 2026-10-14 | Secrets go through `/usr/bin/security` from the GUI-session LaunchAgent, not Python `keyring` (whose ACL follows the interpreter). Any same-user process can read them; accepted and documented, with minimal token scopes. |
| 2026-10-14 | The scheduler is a small loop over `cronsim`, not APScheduler. Origin `scheduled` is never explicit: L0 jobs run inline with no task row, L3 runs and reports to the approvers, L2 and L1 ask, and asking jobs fire at most hourly. Veto windows are deferred. |
| 2026-10-14 | Notifications are delivered from persisted events through the router hook: the unique event key plus the channel's dedupe give exactly once across crashes. A job's first run after install is a silent baseline; paused or muted events are consumed unsent, so nothing floods on `/resume`. |
| 2026-10-14 | The daily briefing is code-built from builtin reads with no model, at 08:00 (`core/briefing`), once a day. Ranked proactive nudges come later. |
| 2026-10-14 | Phase 3.5 (pulse, [`pulse-plan.md`](pulse-plan.md)): any extension with a `report:` grant reports items; the daemon, not the extension, decides what is news by comparing a fingerprint of `{title, state}` with what the owner last heard. Urgent texts immediately (any hour, 20/h cap, held while paused or muted), normal is batched into an hourly "btw", low waits for 08:00. Quiet hours are 00:00-06:00 and the briefing always sends. Health stays with the monitor. |
| 2026-10-14 | Deferred past Phase 3, each with its design in `followups.md`: MCP, critical-job LaunchAgents and `job-guard`, veto windows, the Seatbelt sandbox, iMessage `/ext add`, git and network sources, Python deps. |
