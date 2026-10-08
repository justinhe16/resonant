# Resonant architecture

Resonant is an always-on personal agent harness that runs as a daemon on a dedicated home Mac mini. It takes events in from iMessage, Slack, webhooks, schedules and extensions. A local LLM handles simple routing and tool calls. Hard reasoning and coding are supervised Claude Code sessions. Every side effect passes a policy gate. Product logic lives in **extensions**, which are separate repos that plug in through `resonant.yaml`.

For an interactive version, open [`architecture.html`](architecture.html), which has a clickable graph, the lifecycle, the approval flow, the roadmap and the trust model. The frozen contracts are in [`interfaces.md`](interfaces.md).

## Shape

```
 iMessage (owner) ─────┐ principal map   ┌── runner=local : router → qwen3 (≤3 tool calls)
 Slack (owner+cofndr) ─┤ (identity →     │── runner=claude: Agent SDK session in a worktree
 Webhook (tailnet+HMAC)┼─> principal) ─> events ─> Loop ─┤              (slot pool, priority)
 Scheduler / ext emit ─┤                 (event+tick)     └── runner=cu    : computer use (Phase 7)
 Claude SDK events ────┘                                         │ tool calls
                       Permissions(principal, extension) → Gate(level, scope, approvers) → Executor
                         (idempotency key, Keychain secrets, audit: requested_by / approved_by)
   Store (SQLite WAL): tasks · events · approvals · executions · audit_log · spans · decision_log · memory · kv
   FastAPI on 127.0.0.1 ← tailscale serve   │   watchdog thread + dead-man ping   │   LaunchAgents
```

- **One asyncio process** runs the loop and the local API. Extensions, MCP servers, Claude sessions and computer-use workers run as child processes, so a crash never takes down the core.
- **launchd supervises it** as a per-user LaunchAgent (`gui/<uid>`). Messages.app, computer use and the login Keychain all need the GUI session. Ollama has its own LaunchAgent (`OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_CONTEXT_LENGTH=32768`). Critical extension jobs get their own LaunchAgents, so they fire even if the daemon is down. They run through `resonant job-guard`, which honors the kill switch.
- **There are no public ports.** The API binds to loopback only, and config validation enforces it. iMessage reads `chat.db` locally. Slack uses Socket Mode. Webhooks are reachable from the tailnet only and must be HMAC-signed. CI status is polled, never pushed to us.
- **Runtime state** lives in `~/.resonant/`: `config.yaml`, `principals.yaml`, `resonant.db`, `logs/`, `worktrees/`, `evals/`.

## Components

| Component | Where | Status |
|---|---|---|
| Config (YAML + `RESONANT_*` env, loopback-only API, `dry_run` default on) | `resonant/config.py` | Phase 0 ✅ |
| Principals and permissions | `resonant/principals.py` | Phase 0 ✅ |
| Store: migrations, kv, audit, task/event repos | `resonant/store/` | Phase 0 ✅ |
| Generic loop, task envelope, slot pool | `resonant/loop/` | Phase 0 ✅ |
| Daemon and local API (`/healthz`, `/api/status`) | `resonant/daemon.py`, `resonant/api.py` | Phase 0 ✅ |
| LaunchAgents (daemon, Ollama) | `resonant/launchd.py` | Phase 0 ✅ |
| CLI: `daemon`, `status`, `kill`, `resume`, `job-guard` | `resonant/cli.py` | Phase 0 ✅ |
| Health: watchdog thread, dead-man ping | `resonant/health.py` | Phase 0 ✅ |
| Observability: JSON logs, spans | `resonant/observability/` | Phase 0 ✅ |
| Contracts: Event, ToolSpec, Manifest, ToolRegistry, Gate (tokens, reply binding), Channel, Executor | `resonant_sdk/`, `resonant/tools/`, `resonant/gate/`, `resonant/gateway/`, `resonant/executor.py` | Phase 0 ✅ (DryRunGate) |
| Extension authoring guide + `resonant ext validate` | `docs/extensions.md`, `resonant/cli.py` | Phase 0 ✅ |
| Built-in read tools (`list_tasks`, `task_counts`, `system_status`, `daemon_status`, `model_status`); registry, `DryRunGate` and executor wired into the daemon; `GET /api/tools` | `resonant/tools/builtin.py`, `resonant/components.py`, `resonant/api.py` | Phase 1 |
| Model client (Ollama native or OpenAI-compatible), `resonant model probe/bench` | `resonant/models/`, `resonant/cli.py` | Phase 1 |
| Router: deterministic fast path (slash commands, anchored keywords, owner-only `/kill`), one-call LLM intent label with stable prompt prefixes, principal-filtered toolsets | `resonant/router/` | Phase 1 |
| Brain health: `HealthMonitor` (model, imessage, loop, store checks), owner alerts over iMessage, dead-man `/fail` reasons, `GET /api/channels`, `/api/status.health` | `resonant/monitor.py`, `resonant/health.py`, `resonant/api.py` | Phase 1 |
| Local runner | — | Phase 1 |
| iMessage channel, Slack notifier, router evals | — | Phase 1 |
| Real gate (L0–L3, text-reply approvals, veto, kill switch), Keychain injection | — | Phase 2 |
| Extension manager (`ext add`, CLI/MCP tools, scheduler, critical-job plists) | — | Phase 3 |
| Dashboard (Vite + React + shadcn, streak look) | — | Phase 4 |
| Two-way Slack, Claude runner | — | Phase 5 |
| On-call extension | — | Phase 6 |
| Life lane: Gmail, Calendar, iMessage, payments, browser, computer use | private repo, with core runner/scope support | Phase 7 |

## Task lifecycle

```
queued ──dispatch──> running ──Continue──> queued
                        │──Wait(reason, wake_at?)──> waiting ──event or wake_at──> queued
                        │──Done──> done
                        │──Fail(retryable, attempts left)──> queued (backoff or retry_at; events kept)
                        └──Fail(final) / cancel ──> failed / cancelled
wait reasons: approval · human_reply · usage_reset · ci · veto
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
  - MCP tools must be typed in the manifest, and unlisted ones are refused.
- **Approvals are text replies on iMessage** (Phase 2), bound to a 2-character code and an intent hash, and they expire.
  - A deterministic parser runs first. A separate classifier handles the rest, approves only at confidence ≥ 0.9, and sees only the reply and the code-built summary.
  - Pay approvals need an exact `yes <code>`.
  - SMS-delivered messages are ignored.
  - External strings in summaries are sanitized: quoted, truncated, newlines removed, codes removed.
- **The model never sees secrets.** The executor injects them from the Keychain as environment variables for the tool process only.
- **trade-jev order execution never goes through the loop or gate.** Core only sees its jobs, read tools, events and kill switch.
- **The kill switch** sets `autonomy=off`, so everything above L0 needs approval, and `paused=true`, which critical-job wrappers and on-call autonomy check before acting.

## Health

- **Watchdog.** An asyncio heartbeat beats every second, and a separate thread calls `os._exit(1)` if it goes stale. launchd then restarts the daemon.
- **Dead-man switch.** It pings healthchecks.io while the loop is ticking and sends `/fail` when it isn't. A whole-process freeze (SIGSTOP), a power cut, or a reboot waiting at the FileVault login all stop the pings, and healthchecks.io alerts.
- **iMessage self-test (Phase 1).** It runs at startup, and the health monitor re-runs it every 6h; a failure (AppleScript or the `chat.db` schema broke) degrades the `imessage` check. It sends a random nonce to Resonant's own handle and waits for the reader to see it; the result is kept in kv `imessage.selftest`. `resonant selftest imessage` runs the same check by hand, next to a live daemon.
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
| 2026-10-08 | `waiting(ci)` polls CI through `gh` on the tick. No inbound GitHub webhooks. |
| 2026-10-08 | Router fast-path reads go through the gate but not the executor: no task exists at route time, and builtins are L0 reads with no side effect. `/kill` and `/resume` are deterministic and owner-only, never decided by the model. |
| 2026-10-08 | Manifests load with a YAML loader where only true/false are booleans, so `notify: {on: [...]}` stays a string key. |
