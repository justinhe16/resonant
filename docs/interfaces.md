# Resonant interfaces (frozen contracts)

Later phases build against these contracts. Changing one requires updating this file, `architecture.md`, and every implementation in the same PR. Code is the source of truth; file paths are given for each contract.

## Event: `resonant_sdk.events.Event`

```python
class Event(BaseModel, extra="forbid", frozen=True):
    id: str            # ULID (resonant_sdk.new_id), time-sortable
    source: str        # "imessage" | "slack" | "webhook" | "scheduler" | "ext:<name>" | "claude" | "gate"
    type: str          # "message" | "job.due" | "approval.reply" | "human_input" | "ext.<name>.<event>" ...
    payload: dict[str, Any] = {}
    task_id: str | None = None      # None: unrouted, goes to the router
    principal: str | None = None    # always set by core from an authenticated identity
    dedupe_key: str | None = None   # UNIQUE: duplicates are dropped on ingest
    occurred_at: datetime
    trace_id: str
```

- Events are append-only. The only change ever made to one is setting `consumed_at`.
- `Loop.submit(event) -> bool` returns False for a duplicate. An event with a `task_id` wakes that task if it is waiting.
- Unrouted events go to the router. If the router raises, the event is kept and retried each tick, and after 5 failures it is dead-lettered with an audit row. If the router returns None, the event is consumed. If it returns a `NewTask`, the task is created and the event becomes its first input.
- An extension can never set `principal`; core overwrites it.

## Task envelope: `tasks` table, `resonant.loop.types.Task`

| Field | Meaning |
|---|---|
| `kind`, `runner` | free text; `runner` selects the `Runner` (`local`, `claude`, `cu`, `echo`) |
| `extension`, `principal`, `lane` | scope and requester; `lane="oncall"` gets reserved capacity |
| `priority` | `PRIORITY_ONCALL=0 < PRIORITY_OWNER=1 < PRIORITY_MEMBER=2 < PRIORITY_BACKGROUND=3`; set by core from lane and principal, never by the model |
| `status` | `queued`, `running`, `waiting`, `done`, `failed`, `cancelled` |
| `wait_reason` | `approval`, `human_reply`, `usage_reset`, `ci`, `veto`; a CHECK constraint requires it exactly when status is `waiting` |
| `wake_at` | for `waiting`, when to wake; for `queued`, not before (retry backoff) |
| `attempts`, `step_no` | `attempts` counts failures and crashes and resets on progress; `step_no` counts persisted steps |
| `checkpoint` | opaque JSON owned by the runner, written every step |
| `claude_session_id`, `worktree_path`, `scope` | runner state that must survive a crash |
| `origin_event_id`, `channel_ref`, `trace_id` | provenance, where to reply, and tracing |

## Runner: `resonant.loop.types`

```python
class Runner(Protocol):
    name: str
    uses_claude_slot: bool
    max_step_s: float | None          # overrides LoopConfig.stuck_after_s
    async def step(self, task: Task, events: list[Event], ctx: StepContext) -> StepOutcome: ...

class StepContext(Protocol):          # call from the event-loop thread only
    def save(self, *, checkpoint=None, claude_session_id=None, worktree_path=None) -> bool: ...

StepOutcome = Continue(checkpoint)                                  # requeue now
            | Wait(reason, checkpoint, wake_at=None)                # park; frees a Claude slot
            | Done(result, checkpoint=None)
            | Fail(error, retryable=False, checkpoint=None, retry_at=None)
```

Loop guarantees:
1. Each step's outcome, checkpoint, and consumed events commit in one transaction.
2. An event that arrives during a step that returns `Wait` requeues the task, so no wakeup is lost.
3. A retryable `Fail` keeps its events for the retry. Backoff is 5s·2ⁿ up to 300s, or `retry_at` if set.
4. A crash mid-step means the task resumes from its last checkpoint with its events replayed, and the crash counts as an attempt. A graceful shutdown requeues without using an attempt.
5. A step that exceeds `max_step_s` is cancelled and treated as a retryable failure.
6. An outcome that isn't JSON-serializable becomes a non-retryable failure. A database error while persisting stops the loop: the daemon exits, launchd restarts it, and `resume()` recovers the task.

Runner obligations:
- Make steps idempotent relative to the last checkpoint. Side effects go only through the executor.
- On `CancelledError`, clean up any subprocess, then re-raise.
- Save anything a retry must not lose (session ids, worktrees) with `ctx.save` as soon as it exists.

**Claude slots** (`SlotPool`): a cap (`claude.max_concurrent`, default 5) with `reserved_oncall_slots` (default 1) that only on-call can use. Slots are taken in dispatch order (priority, then age) and held only while a step is in flight. Running sessions are never preempted.

## ToolSpec: `resonant_sdk.tool`

```python
class ToolSpec(BaseModel, frozen=True, extra="forbid"):
    name: str               # ^[a-z][a-z0-9_]{0,63}$
    description: str
    args_schema: dict       # JSON Schema
    effect: Effect          # read | write | pay
    level: Level | None     # L0..L3; None means DEFAULT_LEVEL[effect]: read=L0, write=L1, pay=L1
    source: str             # "builtin" | "ext:<name>"
    extension: str | None
    timeout_s: float = 30
    secrets: tuple[str, ...]
    commit: bool = False    # send/pay/delete: always pauses inside a computer-use scope
    effective_level: Level  # property
```

These invariants are enforced at registration:
- `pay` is always L1.
- `read` without secrets is always L0.
- A `commit` tool must have a side effect.

The model supplies only the tool name and args. Effect and level always come from the registry.

## Manifest: `resonant_sdk.manifest.Manifest` (`resonant.yaml`, `version: 1`)

```yaml
name: <slug>                     # required
version: 1                       # required
uses: <first-party ext>          # optional; allows exactly one extra block named after it
approvers: [<principal>, ...]    # each must hold the approve grant for this extension
jobs:     [{id, schedule (5-field cron), tz?, critical?, run}]
tools:    {<name>: {run, description?, effect, level?, args: {<arg>: string|integer|number|boolean},
                    secrets?, commit?, timeout_s?}}      # {placeholders} in run must be declared args
mcp:      {command, tools: {<name>: {effect, level?, commit?, secrets?}}}   # unlisted MCP tools are refused
notify:   {on: [<event>, ...], channel: imessage|slack}
health:   {check, every: 30s|5m|1h}
secrets:  [<keychain ref>, ...]  # every tool secret must be declared here
```

- Unknown keys are an error. A tool name defined in both `tools` and `mcp.tools` is an error.
- Load manifests with `load_manifest` / `parse_yaml`. In that loader only `true`/`false` are booleans, so `on:` stays a string key.
- `resonant.extensions.checks.approver_errors(manifest, principals)` validates the approvers against this installation.
- **trade-jev boundary:** order execution never goes through the loop or gate. Core only sees trade-jev's jobs, read tools, events, and kill switch. Its own process owns execution and its own risk controls.

## Principals: `resonant.principals` (`~/.resonant/principals.yaml`)

```yaml
principals:
  justin:    {role: owner, identities: ["imessage:+15551234567", "slack:U01"]}
  cofounder: {identities: ["slack:U02"], grants: {dori: [request, approve]}}
```

- An identity is `imessage:<E.164 | lowercase email>` or `slack:<user id>`.
- There is exactly one owner, and an identity can belong to only one principal.
- `can(principal, action, extension)`:
  - The owner can do everything.
  - `admin` and global scope (`extension=None`) are owner-only.
  - Everyone else can do only what their grants list.
- The local CLI acts as the owner (`owner:cli`).

## Gate: `resonant.gate`

```python
Intent(task_id, principal, extension, tool, args, effect, level, commit=False, scope_id=None, trace_id=None)
    .hash = sha256(canonical_json({"tool", "args", "extension"}))

Decision = Allow(approval_id=None) | Deny(reason) | NeedsApproval(approval_id, code, approvers, expires_at)
         | Veto(approval_id, until) | DryRun(would)

class Gate(Protocol):
    def evaluate(self, intent: Intent) -> Decision: ...
    def resolve(self, resolution: ApprovalResolution) -> ResolveResult: ...
```

Evaluation order (Phase 2):
1. **Permission:** if `can(principal, "request", extension)` is false, Deny.
2. **Kill switch:** if `autonomy=off`, everything above L0 needs approval.
3. **Level:**
   - L0: Allow.
   - L1: NeedsApproval.
   - L2: Veto, and the task waits with `wait_reason=veto` and `wake_at=until`.
   - L3: Allow, then report.
   - Inside an approved computer-use scope, `commit` tools still need approval and get a screenshot.
4. **dry_run:** anything that isn't a plain L0 read becomes DryRun.

Every evaluation writes an audit row. Phase 0 ships `DryRunGate`, which does permission checks, allows L0 reads, and returns DryRun for everything else.

Kill switch (`resonant.killswitch`, owner-only): `engage` sets `autonomy="off"` and `paused=true` and writes an audit row; `release` reverses both. Critical-job LaunchAgents run `resonant job-guard --job <ext>/<id> -- <cmd>`, which fails closed: it exits with code 75 and doesn't run the command when paused or when the flag can't be read. On-call autonomy checks `paused` before acting.

## Approvals: binding, codes, and the classifier

Request (`resonant.gate.codes`):
- `new_code(pending)` returns 2 characters, a letter then a digit, with no look-alikes, unique among pending approvals.
- `approval_text(code, summary)` returns `"[A7] <summary>? Reply yes / no / yes with comments: …"`. For pay, it uses `require_code=True` and returns `"[P3] <summary>? Reply exactly: yes P3 / no P3"`.
- The summary is built by code from intent fields. Every external string goes through `sanitize_external`, which quotes it, collapses whitespace and newlines, strips code-like tokens, and truncates to 60 characters.

**Binding.** A reply resolves an approval only if all of these hold:
- It comes from an authenticated identity whose principal is in the approval's `approvers` list and holds `approve` for the extension.
- It arrived as iMessage, not SMS, or from the mapped Slack user.
- It maps to exactly one pending, unexpired approval. With several pending, the code is required. If it's ambiguous, re-ask and never guess.

**Two-stage decision** (Phase 2):
1. **Deterministic parser.** It recognizes yes/no/approve/deny/👍/👎, `yes with comments: …`, and an optional code, case-insensitive.
2. **Approval classifier**, only if the parser fails.
   - It is separate from the router and uses a small, focused prompt.
   - Its input is the reply text and the summary, and nothing else: no tool output and no external content.
   - Its output is `{decision: approve|deny|unclear, approval_code, comments, confidence}`.
   - It approves only if `decision == approve` and `confidence >= 0.9`. Anything else is unclear and gets a re-ask.

**Pay:** only a deterministic exact match that includes the code (`yes P3`) counts. The classifier is never used, and the same rule applies on Slack.

**Comments:** `yes with comments: …` approves and appends a `human_input` event to the task, which the runner treats as a constraint.

**Audit:**
```python
ApprovalResolution(approval_id, approved, actor, actor_identity, path: parser|classifier|button|cli,
                   raw_reply, confidence, comments)
```
Every field goes to the audit log. Approvals carry `expires_at`, and there is at most one pending approval per (task, intent hash).

## Executor: `resonant.executor`

```python
idempotency_key(task_id, step_no, intent_hash) -> str
Executor(conn, handlers: {tool: async (args) -> dict}).execute(intent, decision: Allow, *, step_no) -> ToolResult
ToolResult(ok, data, error, duration_ms, replayed, ambiguous)
```

- It requires `Allow`; anything else raises `NotAllowedError`.
- What happens to a key depends on its row in `executions`:
  - `succeeded`: the recorded result is returned (`replayed=True`).
  - `started` but never finished: `ambiguous=True`, and the tool is not re-run.
  - `failed`: it may be retried.
- An audit row is written before and after each call.
- Phase 2 makes the executor the only reader of Keychain secrets, injected as environment variables for the tool process only, with values redacted from audit rows.

## Channel: `resonant.gateway.channel`

```python
class Channel(Protocol):
    name: str                                            # "imessage" | "slack"
    async def start(self, submit: Callable[[Event], bool]) -> None
    async def stop(self) -> None
    async def send(self, principal, text, *, thread: MessageRef | None = None, dedupe_key=None) -> MessageRef
    async def request_approval(self, principal, prompt: ApprovalPrompt) -> MessageRef
    async def self_test(self) -> SelfTestResult
```

Every adapter must:
- Resolve identity to a principal before calling `submit`. Unknown identities are dropped and audited.
- **Never emit events for its own messages**:
  - iMessage reads only rows with `is_from_me = 0`.
  - Resonant's own handle is skipped before routing.
  - Self-test messages carry a nonce and go only to the self-test checker.
- Dedupe sends by `dedupe_key`, rate-limit them, and never interpolate text into scripts. osascript gets its text through argv.
- Treat all message content as untrusted.

**iMessage** (Phase 1):
- Resonant's own Apple ID is signed into Messages.app.
- **Send:** osascript.
- **Receive:** reads `~/Library/Messages/chat.db` read-only:
  - It is opened with `mode=ro` and never written to, WAL included.
  - A watch on `chat.db-wal` triggers reads, with 2s polling as the fallback.
  - A cursor on `message.ROWID` is kept in `kv`.
  - When `text` is NULL, it parses `attributedBody`.
- **Startup self-test:** send to Resonant's own handle and read the row back. On failure, alert through the Slack notifier.

**Slack:** outbound notifier in Phase 1, two-way Socket Mode in Phase 5.

## Spans and logs: `resonant.observability`

- `with span(conn, name, trace_id=..., task_id=..., parent_id=..., **attrs) as s: s.set(...)` writes a row to `spans` with status and timing.
- Logs are JSON lines in `~/.resonant/logs/resonant.jsonl`, rotated at 20MB × 5. Standard-library loggers flow through structlog.

## Local API

All endpoints are loopback only and exposed to the tailnet with `tailscale serve`.

| Endpoint | Returns |
|---|---|
| `GET /healthz` | `{"ok": true}` |
| `GET /api/status` | version, uptime, last tick, dry_run, autonomy, paused, task counts by status and wait reason, in-flight count |
