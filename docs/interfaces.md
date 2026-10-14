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
| `kind`, `runner` | free text; `runner` selects the `Runner` (`local`, `reminder`, `claude`, `cu`, `echo`) |
| `extension`, `principal`, `lane` | scope and requester; `lane="oncall"` gets reserved capacity |
| `priority` | `PRIORITY_ONCALL=0 < PRIORITY_OWNER=1 < PRIORITY_MEMBER=2 < PRIORITY_BACKGROUND=3`; set by core from lane and principal, never by the model |
| `status` | `queued`, `running`, `waiting`, `done`, `failed`, `cancelled` |
| `wait_reason` | `approval`, `human_reply`, `usage_reset`, `ci`, `veto`; a CHECK constraint requires it exactly when status is `waiting` |
| `wake_at` | for `waiting`, when to wake; for `queued`, not before (retry backoff, or a reminder's due time). `NewTask.wake_at` (default None) sets it at creation |
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
7. **Next wake.** Between ticks `Loop.run` sleeps `Loop.wait_s()`: `tick_s`, or less when the earliest future `wake_at` of a queued or waiting task (`store.tasks.next_wake_at(conn, after=last_tick)`) comes sooner; one that fell due since the last tick means tick at once. A task is dispatched within loop precision of its `wake_at`, not up to a tick late. Tasks already due at the last tick are excluded, so they never cause a busy loop.

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

- An identity is `imessage:<E.164 | lowercase email>` or `slack:<user id>`. An email handle must start with a letter or digit (never `-`, which osascript could read as an option).
- There is exactly one owner, and an identity can belong to only one principal.
- `can(principal, action, extension)`:
  - The owner can do everything.
  - `admin` and global scope (`extension=None`) are owner-only.
  - Everyone else can do only what their grants list.
- The local CLI acts as the owner (`owner:cli`).

## Tool registry: `resonant.tools.ToolRegistry`

`register(spec)`, `get(extension, name)`, `specs(extension=None)`. Specs are keyed by `(extension, name)`, and builtins use `extension=None`, so two extensions may each define a tool with the same name. The registry is the only source of a tool's effect and level.

## Gate: `resonant.gate`

```python
Intent.for_tool(spec: ToolSpec, *, task_id, principal, args, scope_id=None, trace_id=None,
                origin: Origin = "inferred")
    # effect / level / commit / tool / extension are read from spec: callers can't set them
    # args are validated with canonical_json and deep-copied
    .hash  # recomputed on every access:
           # sha256(canonical_json({tool, extension, args, effect, level, commit}))
           # origin is not hashed: an approval binds to what runs, not to who asked
Origin = Literal["explicit", "inferred"]   # anything else raises ValueError
canonical_json(value)  # sorted keys; rejects non-str keys, NaN/Infinity, non-JSON types

Decision = Allow(token, approval_id=None)
         | Deny(reason)
         | NeedsApproval(approval_id, code, approvers, expires_at, summary, effect, level, require_code)
         | Veto(approval_id, until, summary)
         | DryRun(would)

class Gate(TokenVerifier, Protocol):
    def evaluate(self, intent: Intent) -> Decision: ...
    def consume(self, token: str, intent_hash: str) -> bool: ...   # single-use; executor calls it
    def pending_for(self, principal: str) -> Sequence[PendingApproval]: ...
    def open_batch(self, principal: str, approval_ids: Sequence[str]) -> ApprovalBatch: ...
    def resolve(self, resolution: ApprovalResolution) -> ResolveResult: ...   # by id: button / CLI
    def resolve_reply(self, reply: ApprovalReply) -> ResolveResult: ...      # text reply: gate binds it
```

**Origin** says who chose the action. Code sets `"explicit"` in exactly one place, the local runner's `direct_call` path, whose checkpoint only the router's deterministic command parser writes. Everything the model chooses, and later every scheduled or extension job, is `"inferred"`. It is never read from model output.

Evaluation order (Phase 2, `resonant.gate.PolicyGate`):
1. **Registration:** if `registry.get(extension, tool) != intent.spec`, Deny. An intent can't claim an effect or level its tool wasn't registered with.
2. **Permission:** if `can(principal, "request", extension)` is false, Deny.
3. **Unsafe args:** if `unsafe_args(args)` finds anything, Deny. The reason gives only a count, never the keys or values.
4. **dry_run:** a tool that is neither an L0 read nor store-internal becomes `DryRun(would=<summary>)`. The summary uses the tool's code-built heading when it has one (`restart Ollama`), as approval prompts do.
5. **Level × origin × autonomy** (`policy.rule`):

| Level | autonomy on, explicit | autonomy on, inferred | autonomy off |
|---|---|---|---|
| L0 | Allow | Allow | Allow |
| L3 | Allow | Allow | NeedsApproval |
| L2 | Allow | NeedsApproval | NeedsApproval |
| L1 (destructive, pay) | NeedsApproval, `require_code=True` | same | same |

`evaluate` never returns `Veto` in Phase 2; the type stays for Phase 3 unattended jobs. Reporting after an L3 or explicit Allow (`Done: <summary>`) is the runner's job.

```python
PolicyGate(conn, principals, registry, *, dry_run: bool, approval_ttl_s: float,
           actions: {(extension, tool): (Intent) -> str | None} = {}, clock=utcnow)
rule(level, origin, autonomy_on) -> "allow" | "ask" | "ask_code"
autonomy_on(conn) -> bool
NOT_APPROVED, APPROVAL_EXPIRED, APPROVAL_USED, NO_TASK   # Deny reasons the runner templates
REPLY_CHANNELS = {"imessage"}                           # channels whose text replies may bind
STORE_INTERNAL_TOOLS, is_store_internal(spec)             # resonant.tools
```

- **Kill switch:** kv `autonomy` is read on every `evaluate`. Anything but `"on"` (missing, garbage, unparseable) counts as off: fail closed.
- **dry_run exemption** is keyed on registry identity: `is_store_internal(spec)` is true only for a builtin (`extension=None`, `source="builtin"`, no secrets) named in `STORE_INTERNAL_TOOLS = {add_note, delete_note, clear_notes, add_reminder, cancel_reminder}`. A test pins the set. An extension tool with the same name is not exempt, and ops tools never are. Exempt tools still go through step 5.
- **Approvals** per (task, intent hash). "Ask" reuses the pending approval if there is one; otherwise it inserts one with `approvers=(owner,)` (global scope is owner-only), `expires_at = now + gate.approval_ttl_s`, a fresh code, and a code-built summary (`summarize(intent, action=actions[(extension, tool)](intent))` when an action is registered). An intent whose `task_id` has no task row (the router's fast path) gets `Deny(NO_TASK)`.
  - Approved and unused: `Allow(token, approval_id)`.
  - Any rejected approval for the intent: `Deny(NOT_APPROVED)`, even if the table would now Allow (the owner's "no" holds for that intent).
  - Expired: `Deny(APPROVAL_EXPIRED)`. Used: `Deny(APPROVAL_USED)`; a used approval never yields another Allow.
  - So an intent gets at most one approval per task: after it expires, is rejected or is used, the gate never re-asks (one gated side effect per local task). A retry needs a new task.
- **Lazy expiry:** `evaluate`, `pending_for` and `resolve` first run `expire_due(now)`. The waiting task's `wake_at = expires_at` is the backstop that wakes it.
- `resolve(resolution)` (CLI, button): the actor must be in `approvers` and hold `approve`; then `store.approvals.resolve` and `wake_resolved` in one transaction, audited `approval.resolve`. Refusal reasons: `not_found` (also for a non-approver, so ids aren't confirmed; the audit row says `not an approver`) or the store's reason. A refused attempt is audited `decision="refused"`. This authenticated path approves L1 without typing the code.
- `resolve_reply(reply)` (text replies) binds the reply to one approval (see **Binding** below), then `store.approvals.resolve(..., event_id=reply.event_id)` and `wake_resolved` in one transaction. Every attempt writes one `approval.resolve` audit row with every reply field (raw text, event id, `occurred_at`, code, path, channel, actor identity, comments) and the reason. `open_batch` raises `NotImplementedError` (deferred).
- `pending_for(principal)` lists unexpired pending approvals as `PendingApproval`.

**Allow tokens.** Every `Allow` carries a single-use token that the gate issued for one intent hash. After an approval, the runner re-evaluates the intent on wake and gets an `Allow(token, approval_id)`. Tokens live in memory, so after a restart the runner simply re-evaluates.

Every evaluation writes one `gate.evaluate` audit row (decision, origin, autonomy, dry_run, approval id, deny reason, args). `DryRunGate` (Phase 0, kept as a test double) checks registration and permissions, allows L0 reads with a token, and returns `DryRun(would=<decision name>)` for everything else. It has no approvals.

**Transactions.** `transaction()` is not reentrant. Helpers that write bookkeeping (audit, spans, gate, executor, kill switch) use `atomic()`, which joins an enclosing transaction via SAVEPOINT. So they are safe to call standalone or inside a caller's transaction.

Kill switch (`resonant.killswitch`, owner-only): `engage` sets `autonomy="off"` and `paused=true`; `release` reverses both. Both write an audit row that includes the prior state. Migration 002 writes explicit default rows.

Critical-job LaunchAgents run `resonant job-guard --job <ext>/<id> -- <cmd>`. It **fails closed**:
- It never creates or migrates the store.
- It opens it with `open_existing`.
- It exits 75 if the store is missing or its schema is behind, if the `paused` row is missing or isn't a boolean, or if paused is true.
- Before exec it audits `job.exec`. If exec fails, it audits `job.exec_failed` and exits 127.

On-call autonomy checks `paused` before acting.

## Approvals: binding, codes, and replies

Request (`resonant.gate.codes`):
- `new_code(pending)` returns 2 characters, a letter then a digit, with no look-alikes, unique among pending approvals.
- `approval_text(code, summary, *, require_code=False, destructive=False)`:
  - L2/L3: `"[A7] <summary>? Reply yes / no"`.
  - L1 (`destructive=True`): `"[D7] <summary>? This can't be undone. Reply yes D7 / no"`. It takes precedence over `require_code`.
  - Pay (`require_code=True`): `"[P3] <summary>? Reply exactly: yes P3 / no P3"`.
- The summary is built by code from intent fields. Every external string goes through `sanitize_external`, which quotes it, collapses whitespace and newlines, strips code-like tokens, and truncates to 60 characters. `clean_external` does the same without truncating or quoting.
- `resonant.gate.summary`:
  - `summarize(intent, *, action=None)` returns `<extension>: <tool name> key="value", …` (underscores become spaces; no extension prefix for builtins), e.g. `add note text="buy milk"`. `action` is an optional code-built heading (`delete all 4 notes`) that replaces the tool name; it is cleaned too and never comes from a model.
  - `describe_args(args)`: sorted keys, at most 3 shown, then `+N more args`. Keys (≤ 20 chars) and values (≤ 40 chars) go through `clean_external`; a clipped value gets `(truncated)`, and a value that lost non-ASCII characters gets `(non-ASCII hidden)`. Numbers, booleans and null print as JSON; lists and objects print as `(list, N items)` / `(object, N keys)`.
  - Masking is lossy on purpose: `"vitamin d3"` shows as `"vitamin ?"`. The full args are available to the owner via `/approvals <code>`.
  - `unsafe_args(args)` returns the JSON paths (`$.text`, `$.items[1]`, `$.<key>`) of keys or strings containing control characters other than tab/newline/CR, format characters (bidi overrides and isolates, zero-width characters, BOM) line/paragraph separators, or lone surrogates. Variation selectors and private-use characters are allowed. The gate denies such intents rather than sanitizing them, so what runs always matches what was shown.
- Config (`gate:`): `approval_ttl_s` (default 1800, 60–86400) is how long a prompt stays answerable.

**Binding** (`PolicyGate.resolve_reply`). A reply resolves an approval only if all of these hold:
- It comes from an authenticated identity whose principal is in the approval's `approvers` list and holds `approve` for the extension.
- It arrived as iMessage (`channel="imessage"`, not SMS) via the parser. Slack comes later. The router never builds a reply from a `truncated` payload.
- **Sent-before.** The approval's prompt was sent to the actor (`Approval.sent_at(actor)`) strictly before the reply's `occurred_at`, with or without a code. A reply never binds a prompt sent after it.
- With a code, it names one of the actor's pending, unexpired approvals. Without a code, exactly one approval is pending for the actor, both now and among those open and already sent to the actor at `occurred_at`. So an old plain reply re-routed later can't bind what has since become the only pending approval. If it's ambiguous, re-ask and never guess.
- `require_code` (L1, pay): only `yes <code>` approves. A plain `no` may deny under the single-pending rule.
- **Idempotent per event.** If `reply.event_id` already resolved an approval (`resolved_event_id`), the gate returns that original result with no write, wake or audit. A replay under another actor gets `nothing_pending`.
- `yes with comments: …` approves and appends one `Event(source="gate", type="human_input", payload={approval_id, comments})` to the task (`dedupe_key="approval:<id>:comments"`), before the wake.

Refusals are closed (`ReplyRefusal`, `REPLY_REFUSALS` in `resonant.gate.types`):

| Reason | When |
|---|---|
| `nothing_pending` | Nothing is pending for the actor. A non-approver always gets this, whatever it sends, so pending approvals are never revealed. |
| `ambiguous` | No code and several pending (now or when the reply was sent). Re-ask listing the codes. |
| `unknown_code` | The code matches none of the actor's pending approvals. |
| `code_required` | A plain `yes` for a `require_code` approval. |
| `not_sent_before` | The prompt wasn't sent to the actor before the reply. |
| `unsupported` | Not an iMessage parser reply, or a batch reply (deferred). |
| `already_resolved`, `expired`, `task_not_waiting`, `not_found` | The store's conditional UPDATE refused (a race, or the task moved on). |

**Reply parser** (`resonant.gate.replies`, Phase 2 is parser-only):

```python
parse_reply(text, *, truncated=False) -> ParsedReply | None
ParsedReply(decision: approve|deny, code=None, comments=None)   # code uppercase
normalize_reply(text) -> str   # NFKC, straight quotes, collapsed whitespace
MAX_COMMENTS_LEN = 500
```

- The whole message must match, after normalization and ignoring (ASCII) case:
  - `(yes|approve|👍)( <code>)?` and `<code> yes`
  - `(no|deny|👎)( <code>)?` and `<code> no`
  - `yes( <code>)? with comments: <text>`, where `text` is 1–500 characters and is kept verbatim
- Outside comments, a trailing run of `.` and `!` is dropped (`Yes.`, `yes!!`, `yes A7.`). `?` never is: `yes?` is a question.
- 👍 and 👎 may carry a skin tone and the emoji presentation selector.
- A code must come from the `new_code` alphabet, so look-alikes (`A1`, `O7`) never match.
- Anything else returns None: `y`, `ok`, `sure`, `yes?`, `yes all`, `yes A7 and C4`, a pasted prompt, or text with invisible characters. A `truncated` payload is never a reply.
- The parser only reads text. Binding (approvers, single pending, `require_code`, sent-before) is the gate's job.

**Deferred** (C4): the approval classifier, `resonant eval approvals`, and batch approvals (`yes all`). `open_batch` stays `NotImplementedError`, and `ApprovalReply.path` is always `"parser"` for now. With several approvals pending, a plain `yes` gets a re-ask that lists the codes. The design kept for later:
- **Classifier**, only if the parser fails. It is separate from the router and uses a small, focused prompt. Its input is the reply text and the summary, and nothing else. Its output is `{decision: approve|deny|unclear, approval_code, comments, confidence}`. It approves only if `decision == approve` and `confidence >= 0.9`; anything else gets a re-ask.
- **Batch approvals:** one batch message lists an approver's pending codes, and its snapshot is stored. `yes all` approves **exactly the approvals in that snapshot**: never ones that arrived later, never pay, and never any the replier isn't an approver for. The audit log records the batch id and each approval it resolved.

**Concurrency:** resolution is atomic (`UPDATE approvals … WHERE status = 'pending'`), so the first valid reply on any channel wins. On-call pages (Phase 6) go to iMessage and Slack together with one dedupe key; the other channel's message is updated to "resolved by X via Y".

**Pay:** only a parser match that includes the code (`yes P3`) counts. The classifier is never used for pay, and the same rule applies on Slack.

**Comments:** `yes with comments: …` approves and appends a `human_input` event to the task, which the runner treats as a constraint.

**Gate API for replies.** Channels don't pick approval ids. They pass an `ApprovalReply` and the gate binds it:

```python
ApprovalReply(actor, actor_identity, channel, raw_text, decision: approve|deny,
              path: parser|classifier, event_id, occurred_at, code=None, all_in_batch=False,
              batch_id=None, confidence=None, comments=None)
    # event_id, occurred_at: from the reply Event (C5); occurred_at must be timezone-aware
ApprovalResolution(approval_id, approved, actor, actor_identity, path: button|cli, comments=None)
ResolveResult(ok, reason, resolved: tuple[ResolvedApproval(approval_id, task_id, intent_hash, approved), ...])
PendingApproval(approval_id, code, task_id, summary, effect, level, require_code, expires_at)
ApprovalBatch(batch_id, principal, approval_ids, created_at)   # stored in approval_batches
```

`ResolveResult.resolved` tells the caller which tasks to wake.

**Store** (`resonant.store.approvals`, migration 003). Plain functions over a connection; the gate owns the policy, this module owns the rows:

```python
NewApproval(task_id, intent_hash, tool, effect, level, summary, approvers, code, expires_at,
            require_code=False, extension=None, requested_by=None)
insert_approval(conn, spec, now) -> str              # IntegrityError if one is pending for (task, intent) or the code is taken
get_approval(conn, id) / pending_for_intent(conn, task_id, intent_hash, now) / pending_by_code(conn, code, now) -> Approval | None
latest_for_intent(conn, task_id, intent_hash) -> Approval | None   # newest, any status
was_rejected(conn, task_id, intent_hash) -> bool
pending_for(conn, principal, now) -> list[Approval]  # unexpired, principal in approvers
open_at(conn, principal, at) -> list[Approval]       # open (created, undecided, unexpired) at `at`, any status now
pending_codes(conn) -> set[str]                      # for new_code
add_channel_ref(conn, id, *, ref, at, principal) -> bool   # keeps the first send per (ref, principal)
resolve(conn, id, *, approved, decided_by, now, event_id=None)
    -> None | "not_found" | "already_resolved" | "expired" | "task_not_waiting"
by_resolved_event(conn, event_id) -> list[Approval]; resolve_result(approvals) -> ResolveResult
approved_unused(conn, task_id, intent_hash) -> Approval | None
mark_used(conn, id, now) -> bool                     # True exactly once
expire_due(conn, now) -> int                         # audits approval.expired
wake_resolved(conn, result, now, *, principal=None) -> int
intent_args(conn, approval) -> dict | None           # full args, from its gate.evaluate audit row
```

- `resolve` is one conditional UPDATE: the row must be pending, `expires_at > now`, and its task `waiting` or `running`. It records `decided_by`, `decided_at` and `resolved_event_id`. The refusal reason is read afterwards and is informational.
- **Single use.** `used_at` is set once, with the executor's `started` row. Once approved, an approval has no expiry; it is valid for the life of its task.
- **Orphans.** Every `store.tasks` path that makes a task terminal (`set_status`, `save_step`, `requeue_interrupted`) expires its pending approvals in the same transaction and audits `approval.orphaned`. Decided approvals are left alone.
- **Waking.** `wake_resolved` inserts `Event(source="gate", type="approval.reply", task_id, payload={approval_id, approved})` with `dedupe_key="approval:<id>:resolved"`, and requeues the task only if it is `waiting`. A running task sees the event when its step returns `Wait`. Call it in the resolver's transaction.
- `Approval.sent_at(principal)` is the first time the prompt went to that principal (from `channel_refs`, entries `{ref, at, principal}`), for the sent-before rule.

**Audit.** Every field of the reply goes to the audit log: the raw text, the path, the confidence, the actor identity, the code, and the batch id. Approvals carry `expires_at`. There is at most one pending approval per (task, intent hash), and pending codes are unique.

## Executor: `resonant.executor`

```python
idempotency_key(task_id, step_no, call_index, intent_hash) -> str
Executor(conn, verifier: TokenVerifier, handlers: {(extension | None, tool): async (args) -> dict})
    .execute(intent, decision, *, step_no, call_index=0) -> ToolResult
    .recorded(key) -> ToolResult | None   # what executions holds for key; read-only
    .resolve_ambiguous(key, *, succeeded, actor, result=None, note=None) -> bool   # audited
ToolResult(ok, data, error, duration_ms, replayed, ambiguous, idempotency_key)
class ToolFailed(Exception)   # raise from a handler: "definitely did not take effect"
```

- **Token check.** The decision must be `Allow`, and `verifier.consume(token, intent.hash)` must be true. Otherwise it raises `NotAllowedError` and audits `executor.refused`. A forged Allow, a reused token, or a token issued for different args is refused.
- **Approvals are single use.** An `Allow` with an `approval_id` spends it: `mark_used` runs in the same transaction as the `started` row. An approval that is already used, or whose task or intent hash differs, raises `NotAllowedError` (audited `executor.refused`) and nothing runs. A replayed or ambiguous key doesn't spend it.
- **`recorded(key)`** is for a runner resuming at a pinned key, before it calls the gate: `succeeded` returns the result (`replayed=True`), `started` returns `ambiguous=True`, `failed` returns the definite error, and no row returns None. The approval is spent with `started`, so after a definite failure of an approved call, re-evaluating gives `Deny(APPROVAL_USED)`: a retry needs a new task.
- **What happens to a key**, by its row in `executions`:
  - `succeeded`: the recorded result is returned (`replayed=True`).
  - `started`: the outcome is unknown, so it is **ambiguous** and is never re-run. It stays that way until `resolve_ambiguous` is called.
  - `failed`: a definite failure; it may be retried.
- **Outcomes:**
  - Reads: any error is a definite failure.
  - Write and pay: only `ToolFailed` is definite. A timeout (`spec.timeout_s`), cancellation, unexpected exception, or unserializable result is ambiguous.
- **Never inside a transaction:** `execute()` refuses to run inside a caller's transaction. Its `started` row must be committed before the side effect.
- **Calls in one step:** use a distinct `call_index` for each call within a step. Two calls with the same index and intent are deduped by design.
- **Audit:** `executor.start`, `executor.result`, `executor.ambiguous`, `executor.replayed`, `executor.refused`, and `executor.ambiguous_resolved` are all written.
- Keychain secret injection (the executor as the only reader, env vars for the tool process, redaction in audit rows) is deferred to Phase 3, with the first tool that needs a secret.

## Built-in tools: `resonant.tools.builtin`

Global tools (`extension=None`, `source="builtin"`): L0 reads, plus the notes writes (`add_note` and `delete_note` L3, `clear_notes` L1), the reminder writes (`add_reminder` and `cancel_reminder` L3) and the ops writes (`restart_ollama` L3, `restart_resonant` and `update_resonant` L2). Global scope is owner-only, so a member principal is denied them. `register_builtins(registry, conn, state, *, principals, clock=utcnow, ops_runner=None) -> {(None, name): handler}` registers the specs and returns the handlers.

- **Modules.** Each tool area is a module under `resonant.tools` (`tasks`, `system`, `commands`, `notes`, `reminders`, `projects`, `ops`) that exports `SPECS: tuple[ToolSpec, ...]` and `handlers(ctx: BuiltinContext) -> {name: handler}` (the `BuiltinModule` protocol in `tools/base.py`). `builtin.BUILTIN_MODULES` lists them and `register_builtins` iterates it, so a new area is one module plus one line in that tuple. `BUILTIN_SPECS` is every module's specs, in order.
- `BuiltinContext(conn, state, registry, principals, clock=utcnow, ops_runner=None)` is what a module gets. `registry` is the full registry, read at call time. `clock` returns aware UTC (tests pass a fake). `ops_runner` runs the ops tools' subprocesses (`None`: the real `SubprocessRunner`; tests pass `FakeOpsRunner`). New fields get defaults.

Handlers return JSON-serializable dicts and never raise for "no data". Bad args raise `ToolFailed` (unknown keys, a bad `status`, a non-integer `limit`, note text that is empty or over 200 characters, a missing or already-deleted note id, or any arg to a no-arg tool). No tool returns message bodies, checkpoints, task results, or file contents, except the notes and reminder tools, which return the owner's own text, `projects`, which reads only the code-built `checkpoint.title` and a reminder's own text, and `recent_logs`, which returns a few scrubbed lines of Resonant's own log.

| Tool | Args | Returns |
|---|---|---|
| `list_tasks` | `status?`: `queued\|running\|waiting\|done\|failed\|cancelled`; `limit?`: int, default 10, clamped to 1..50 | `{tasks: [{id, kind, status, wait_reason, age_s}], count, limit, status}`, newest first (by ULID id) |
| `projects` | `all?`: bool (not a bool: `ToolFailed`) | `{items: [{id, state, wait_reason, age_s, kind, title}], count, more, all}`. Fast-path replies (`kind="reply"` with a `fast_reply` checkpoint, e.g. `/status`, `/help`, `/projects` itself) are left out in SQL, so they never count toward the caps or `more`; a waiting one would still show. Open tasks (running, waiting, queued) first, then done, failed and cancelled; each group by `updated_at` descending. At most 15, or 50 with `all`; `more` is how many tasks aren't shown. `age_s` is since `updated_at`. `title` is `checkpoint.title` (clipped to 40), else `⏰ <text clipped to 30>` for a reminder, else the kind; never message text. `list_tasks` stays for the model's `job_control` toolset. |
| `task_counts` | none | `{by_status: {status: n}, waiting_by_reason: {reason: n}, total}` |
| `system_status` | none | `{cpu_percent, cpu_count, load_avg: [1m, 5m, 15m] \| null, memory: {used_bytes, total_bytes, percent}, swap: {used_bytes, total_bytes, percent}, memory_pressure: {level: normal\|warn\|critical\|unknown, free_pct: int \| null}, disk: [{path, free_bytes, total_bytes, used_pct}], uptime_s}` |
| `daemon_status` | none | `{version, uptime_s, last_tick, inflight, dry_run, autonomy, paused}` (a subset of `collect_status`) |
| `model_status` | none | `{known: false}` when `kv["model.probe"]` is absent or not an object; otherwise the cached probe's fields plus `known: true`. It never calls the model. |
| `add_reminder` (write, L3) | `at`: ISO-8601 datetime with a UTC offset, not more than 60s in the past and at most 365 days out; `text`: 1..200 chars (whitespace collapsed); `routed_at` (from `/remind` only, not in the schema, so the model can't send it): `at` is checked against it instead of the clock, so a late-running command still sets the reminder (it fires at once, marked late); it may not be in the future or over a day old | `{id, task_id, at, due, text}`. Creates the reminder task ([Reminders](#reminders-resonantrunnersreminder-resonanttoolswhen)). `id` is the short number from kv `reminders.seq`; `at` is ISO UTC; `due` is `format_due` in `settings.timezone` (`3:00pm`, `tomorrow 9:30am`, `Fri 9:00am`, `Oct 20 9:00am`) |
| `list_reminders` | none | `{reminders: [{id, at, due, text}], count}`: the owner's queued or firing reminders, soonest first, at most 50 |
| `cancel_reminder` (write, L3) | `id`: int ≥ 1 | `{cancelled: true, id, at, due, text}`. `set_status(cancelled, expect=("queued",))`. Unknown, already fired, firing or cancelled → `ToolFailed` |
| `list_commands` | `query?`: string, at most 50 chars | `{commands: [{name, usage, description, tool, level, owner_only, subcommands: [{word, tool, level}]}], count, query}`: the command registry (`router/commands.py`) as the owner sees it, filtered by `query` like `/help <word>`. `level` is the tool's registered level (`null` for commands without a tool of their own). Handlers don't see the requester, and the gate only lets principals who can request global tools (the owner) call it. |
| `list_notes` | none | `{notes: [{id, text, at}], count, total}`: live notes, newest first, at most 20. `total` counts every live note. |
| `add_note` (L3) | `text`: string, 1–200 characters after trimming | `{id, text, at}` |
| `delete_note` (L3) | `id`: int ≥ 1 | `{id, text, at, deleted_at}`. Soft delete. |
| `clear_notes` (L1) | none | `{cleared, deleted_at}`: soft-deletes every live note. |
| `restart_ollama` (L3) | none | `{label, restarted: true}`. `launchctl kickstart -k gui/<uid>/com.resonant.ollama`, 15s. A non-zero exit, timeout or missing binary → `ToolFailed`. |
| `restart_resonant` (L2) | none | `{label, restarting: true, delay_s: 3, commit}`. Writes the restart marker and spawns the detached helper; returns at once ([Ops](#ops-resonantops-resonanttoolsops)). `commit` is the repo's short HEAD, or null. |
| `update_resonant` (L2) | none | `{old, new, updated, synced, restarting}` (+ `delay_s` when restarting, `sync_error` when the sync failed). Refuses (`ToolFailed`) without `ops.repo_path` or `ops.uv_path`, off `main` (`not on main; not updating`), with a dirty tree (`working tree has changes; not updating`), or when `git pull --ff-only` fails. HEAD unchanged: `updated: false`, nothing else runs. A failed `uv sync --frozen` returns `synced: false` and does not restart. |
| `disk` | none | `{disk: [{path, free_bytes, total_bytes, used_pct}]}` for `/` and `RESONANT_HOME`, like `system_status` without the slow probes. |
| `recent_logs` | `n?`: int 1..20, default 10; larger is capped at 20 | `{lines, count, n}`: the last `n` records of `logs/resonant.jsonl`, oldest first, each as `15:04:05 warning resonant.daemon: event k=v`. URLs → `<url>`, emails and phone numbers → `<handle>`, at most 300 chars a line. Reads at most the last 256 KB. No log yet: `lines: []`. |

- `system_status`: `cpu_percent` is measured since the previous call (primed at registration), so it never blocks. `memory_pressure` comes from `sysctl kern.memorystatus_vm_pressure_level` and `memory_pressure -Q` with a 500ms timeout, best effort; off macOS, or when they are missing or slow, it is `{level: "unknown", free_pct: null}`. `disk` lists `/` and `RESONANT_HOME`; an unreadable path has null numbers.
- Notes (`tools/notes.py`) live in `memory` with `scope="notes"`: key = the note id (decimal), value = JSON `{text, at, deleted_at}`. Ids come from kv `notes.seq` and are never reused. Deletes are soft (they set `deleted_at` and keep the row); reads hide deleted notes. Replay can't duplicate a note, because the executor returns the recorded result for an idempotency key that already succeeded. `notes.summary_action(conn, intent)` is the code-built approval heading for the builtin `clear_notes` (`delete all 4 notes`, counting live notes), for `summarize(intent, action=...)`; it returns `None` for any other intent, including an extension tool with the same name.
- The `model.probe` kv key is written by `resonant model probe` and the health monitor, in one shape: `{ok, model_present, ctx_ok, thinking_off_ok, tool_call_ok, call_failed, latency_ms, detail, model, api, checked_at}`. The monitor writes only these fields and scrubs URLs from `detail`. Builtins read it only.

## Reminders: `resonant.runners.reminder`, `resonant.tools.when`

A reminder is a durable queued task; there is no scheduler.

- **Task.** `add_reminder` inserts `NewTask(kind="reminder", runner="reminder", principal=owner, priority=PRIORITY_OWNER, wake_at=at, checkpoint={n, text, at})`, where `at` is ISO UTC. The task id is internal; the owner-facing id is `n`. The executor's idempotency key prevents a duplicate on replay. Handlers don't see the requester; global builtins are owner-only, so reminders belong to `principals.owner`.
- **Firing.** `ReminderRunner(conn, *, channel, timezone, clock=utcnow, max_step_s=30.0)` (`name="reminder"`, `uses_claude_slot=False`, no model, no tools). One step sends `⏰ <text>` with `channel.send(principal, …, dedupe_key=f"reminder:{task.id}")` and returns `Done({late, late_s})`. More than 120s past `at`, the text ends `(late, due 3:00pm)`. `ChannelRefused` or a malformed checkpoint is a final `Fail`; any other `ChannelError` is retryable, and the dedupe key prevents a double send. The send is the deferred effect of an allowed `add_reminder`, so it doesn't go through the gate. Span `runner.reminder.send` (`late`, `ok`), never the text.
- **Durability.** The `tasks` row is the reminder. `requeue_interrupted` touches only running tasks, so a queued reminder survives any restart, and one missed while the daemon was down fires once on start, marked late.
- **Precision.** The loop sleeps until the next `wake_at` (Runner, guarantee 7).
- **Wiring.** `build_imessage_stack` registers `ReminderRunner(conn, channel=<the iMessage channel>, timezone=settings.timezone)` as `runner="reminder"` (in `loop.runners` and `components.runners`). `add_reminder` only runs inside the loop (router or runner step); the loop ticks after every step and sleeps until the next `wake_at`, so a new reminder needs no extra wakeup.
- **iMessage disabled.** No reminder runner exists, and nothing can create a reminder (no router, no local runner). A reminder left from an earlier run stays queued until due, then fails as `unknown runner 'reminder'`: it couldn't be delivered.

**Time parser** (`tools/when.py`, pure, stdlib `zoneinfo`, an explicit aware `now` and the zone):

```python
parse_when(text, *, now, tz) -> datetime             # aware, in tz; raises WhenError
split_when(text, *, now, tz) -> (datetime, rest)     # /remind: longest leading words that parse
check_at(at, *, now, grace=PAST_GRACE) -> None       # the model path: aware, ≥ now-60s, ≤ 365 days
format_due(at, *, now, tz) -> str                    # 3:00pm | tomorrow 3:00pm | Tue 3:00pm | Oct 20 3:00pm
```

- **Accepted** (case-insensitive): `now`; `in N (m|min|mins|minute|minutes|h|hr|hrs|hour|hours)`; a time today, `H(:MM)?(am|pm)` or 24-hour `HH:MM` when the hour is ≥ 13 or zero-padded, with an optional `at`; `today|tomorrow|<weekday> [at] <time>`. A weekday is the next one, today if the time is still ahead. Weekdays may be abbreviated.
- **Rejected** with `WhenError`, whose message is the usage hint: an ambiguous time (`3`, `9:30`, `12:00`: `say 3pm or 15:00`), a time that has passed (`yesterday …` too: `that time has passed`), more than 365 days out, or a nonexistent local time (DST gap). A repeated local time (DST fold) is the first one.
- `split_when` tries at most 5 leading words, longest first. The first that parses wins; the first that is a recognizable but bad time raises its hint; empty text after the time raises `say what to remind you about`.

## Ops: `resonant.ops`, `resonant.tools.ops`

```python
class OpsRunner(Protocol):
    async def run(self, argv, *, timeout_s, cwd: Path | None = None) -> ProcResult  # TimeoutError, OSError
    def spawn_detached(self, argv) -> None                                          # OSError
ProcResult(returncode, stdout, stderr)
SubprocessRunner()          # the real one; FakeOpsRunner (resonant.ops.fakes) for tests
kickstart_argv(label, uid=None) -> list[str]       # only the two ops labels
restart_later_argv(label, delay_s=3) -> list[str]  # [sys.executable, "-m", "resonant.ops.restart_later", label, "3"]
mark_restart(conn, *, by, commit, now) / restart_pending(conn) / clear_restart(conn)
async back_up(conn, channel, owner, *, clock=utcnow) -> bool
```

- **Subprocesses.** argv only, never a shell; each has a timeout (git reads 10s, `git pull --ff-only` 60s, `uv sync --frozen` 240s, `launchctl` 15s); stdin is `/dev/null` and git never prompts (`GIT_TERMINAL_PROMPT=0`, ssh `BatchMode=yes`). Each runs in its own process group, and a timeout or cancel kills the whole group (ssh, build backends). stderr is never logged or returned. Each tool's `timeout_s` exceeds the sum of its steps (`update_resonant`: 360s), so the executor never times one out first. A timeout or a failed check is a definite `ToolFailed` with a short reason.
- **Restart, reply first.** `restart_resonant` (and `update_resonant` after a good sync) writes kv `ops.restart_pending = {by, at, commit}`, then spawns the helper with `start_new_session=True`, so it leaves launchd's process group. The helper sleeps 3s and runs `launchctl kickstart -k gui/<uid>/com.resonant.daemon`. The tool returns at once; the runner saves and sends its reply well inside the delay, and graceful shutdown drains in-flight sends. If the kill lands before the reply is saved, the resumed task replays the `succeeded` execution (no second restart) and sends the report.
- **Back up.** `IMessageStack.start` calls `back_up` after the reader starts. With a marker it sends `Back up (<commit>), down 6s` to the marker's principal (`dedupe_key=restart:<at>`) and clears it. No marker (a normal start or a crash): nothing. A marker older than 10 minutes is stale (the kickstart never happened) and is dropped silently. A failure here is logged and never blocks the start. A `ChannelRefused` clears the marker; another `ChannelError` keeps it for the next start.
- **dry_run.** Ops tools are never exempt: the gate returns `Dry run: would restart Ollama` (`restart Resonant`, `update Resonant`) and nothing runs. `tools.ops.summary_action(intent)` gives those headings for approval prompts too.
- **Reports** (`runners.local.REPORTS`): `Restarted Ollama.`, `Restarting Resonant…`, `Updated a1b2c3d → e4f5a6b, restarting…`, `Already up to date (a1b2c3d).`, and `Updated a1b2c3d → e4f5a6b, but uv sync failed (exit 1): not restarting. Fix it on the Mini.` Read templates: `router.render.render_disk` and `render_logs` (newest lines within one 2000-char chunk).
- **Trust.** Owner-only (global scope), two fixed launchd labels, one configured repo. `/update` runs whatever is on the repo's `main` upstream, fast-forward only, from a clean tree: remote code execution from the owner's own repo, accepted (plan §5).

## Extension store: `resonant.store.extensions` (migration 004)

The DB is the registry of record for installed extensions; there is no `extensions.yaml`. Plain functions over a connection; callers own transactions and audit rows.

- **`extensions`**: one row per installed extension, as `ExtensionRecord(name, source, commit, tree_digest, manifest_sha, manifest, installed_at, installed_by, argv0_paths={}, status="enabled")`. `source` is `repo:<name>` or `path:<abs dir>`. `commit` (column `commit_sha`) is None without git. `manifest` is the approved snapshot (JSON). `argv0_paths` maps a spec name to its pinned absolute `argv[0]`. `status` is `enabled`, `disabled` or `disabled_tampered` (a CHECK).
  - `save_extension(conn, rec)` inserts or fully replaces the row (install and every update). The name `core` (`CORE_EXT`) is refused: it is reserved for core jobs.
  - `get_extension(conn, name)`, `list_extensions(conn, *, status=None)` (sorted by name).
  - `set_status(conn, name, status) -> bool`: False when the row is missing or already has that status, so "disable and alert once" can key off it.
  - `delete_extension(conn, name) -> bool` deletes the row and its `ext_jobs` rows together. Data dirs and Keychain items are the manager's job.
- **`ext_jobs`**: scheduler state per `(ext, job_id)`, as `JobState(ext, job_id, last_due, last_run_at, last_outcome, open_task_id, fail_streak)`. Core jobs use `ext="core"`, so there is no FK to `extensions`. `open_task_id` references `tasks`. `last_outcome` is free text.
  - `mark_due(conn, ext, job_id, due, *, open_task_id=None) -> JobState` upserts `last_due`, which **never moves back**, and sets `open_task_id` when given. Call it in the same transaction as `insert_task`, so a due time is claimed once.
  - `record_run(conn, ext, job_id, *, at, outcome, ok) -> JobState` sets `last_run_at` and `last_outcome`, clears `open_task_id`, and resets `fail_streak` on `ok` or adds one.
  - `get_job`, `list_jobs(conn, ext=None)` (sorted by `(ext, job_id)`), and `prune_jobs(conn, ext, keep) -> int` for jobs an update removed.
- `open_existing` (job-guard, status, the approvals CLI) refuses a v3 store until the daemon has migrated it. `scripts/go_live_check.py` labels its row `store schema <latest>`.

## Cron: `resonant.extensions.cron`

Pure functions over an explicit aware `now` and a zone (`settings.timezone`); no I/O. The scheduler (P3-10) reads `ext_jobs`, the pause flag and the downtime, calls `decide`, and writes what it says.

```python
parse_schedule(expr, tz: ZoneInfo | str) -> Schedule        # raises CronError(ValueError)
Schedule(expr, tz).next_after(after) -> datetime | None     # strictly after; Loop.wait_s
Schedule.due_between(after, until) -> list[datetime]        # (after, until], ascending
Downtime(since, until)        # daemon not running over (since, until]: last tick, then started_at
decide(schedule, *, now, last_due, paused, catchup: timedelta | None, downtime: Downtime | None,
       busy: bool, on_time: timedelta = ON_TIME) -> Decision
Decision(outcome: Outcome, due, late, missed, skipped, truncated, last_due)
Outcome: idle | run | skipped_paused | overlap | missed
```

- **Expressions** are five-field cron on `cronsim`. `@daily`, six fields (seconds), bad fields and schedules that never fire raise `CronError` with the reason (an unknown zone too) (`'60 * * * *': bad minute`). Whitespace is normalized into `Schedule.expr`.
- **Every returned datetime is UTC.** Aware datetimes sharing a `ZoneInfo` compare by wall time and ignore `fold`, so compare and store in UTC (`store.db.iso` already does).
- **DST** (Debian cron semantics, pinned for `America/Los_Angeles`):

  | Schedule | Spring forward (02:00 → 03:00) | Fall back (02:00 → 01:00) |
  |---|---|---|
  | Fixed time (`30 2 * * *`, `30 1 * * *`) | A gap time runs once, at 03:00 | Runs once, at the first 01:30 |
  | `*` in minute or hour (`*/10 * * * *`) | Wall time: the skipped hour doesn't run | Wall time: the repeated hour runs twice |

- **`decide` precedence**, over the due times in `(last_due, now]` (with `last_due=None`, only an on-time one, nothing missed):
  1. **Pause wins.** `paused` → `skipped_paused` with every due time in `skipped`; `last_due` moves past them, so nothing is caught up after `/resume`.
  2. **Catch-up.** Only the latest due time can run: if it's on time (within `on_time` of `now`, default `ON_TIME` = 1 min; the scheduler passes at least its tick interval), or `catchup` is set, it is within `catchup` of `now`, and `downtime` covers it. A paused or merely stalled daemon never catches up. Otherwise `missed`. Earlier due times are always in `missed` (`job.missed`).
  3. **Overlap.** `busy` (the previous run is still running, or its task is waiting) turns `run` into `overlap` (`job.overlap`); the due time is still consumed.
- `run` carries `due` and `late` (a catch-up). Whenever something came due, `last_due` is the latest due time, for `store.extensions.mark_due`.
- `catchup` must be within 0–24h (`MAX_CATCHUP`), else `ValueError`. `missed`/`skipped` list at most the 50 most recent (`MAX_LISTED`), and only due times within 31 days (`LOOKBACK`); `truncated` says more were dropped; an `idle` with `truncated` returns `last_due` at the lookback edge so later ticks don't rescan.

## Phase 3 config: `resonant.config`

Every section has defaults, so a Phase 2 `config.yaml` loads unchanged. Unknown keys are errors, and caps are bounded both ways.

| Key | Default | Bounds | Used for |
|---|---|---|---|
| `extensions.max_concurrent_jobs` | 4 | 1–16 | L0 jobs running at once |
| `extensions.result_max_bytes` | 32768 | 1 KiB–256 KiB | the stored (redacted) result |
| `extensions.model_result_max_chars` | 4096 | 256–32768, ≤ `result_max_bytes` | the model's untrusted view |
| `extensions.process.path` | `/opt/homebrew/bin`, `/usr/bin`, `/bin` | absolute, non-empty | the child's whole `PATH` (`path_env`) |
| `extensions.process.stdout_max_bytes` | 262144 | 1 KiB–4 MiB | stdout and fd-3 events; past it the tree is killed |
| `extensions.process.stderr_max_bytes` | 8192 | 256–65536 | kept redacted, never shown |
| `extensions.process.kill_grace_s` | 2 | > 0, ≤ 30 | SIGTERM, then SIGKILL |
| `extensions.process.tree_sample_s` | 2 | > 0, ≤ 30 | `ProcessTracker` sampling |
| `extensions.process.health_timeout_s` | 20 | > 0, ≤ 120 | a manifest `health.check` run |
| `extensions.notify.max_per_hour` | 20 | 1–200 | per extension, then `+N more updates` |
| `extensions.notify.max_chars` | 300 | 40–1000 | the notification text clip |
| `secrets.security_path` | `/usr/bin/security` | absolute | Keychain access |
| `secrets.timeout_s` | 5 | > 0, ≤ 30 | each `security` call |
| `briefing.enabled` | true | | `core/briefing` |
| `briefing.at` | `"08:00"` | 24-hour `HH:MM` in `timezone` | `BriefingConfig.local_time` |

`Settings.extensions_dir` (`<home>/extensions`) holds the approved read-only copies, and `Settings.ext_data_dir` (`<home>/ext-data`) each extension's writable state.

## Components: `resonant.components`

```python
build_components(settings, conn, state: DaemonState, *, ops_runner: OpsRunner | None = None) -> Components
Components(registry, principals, gate: Gate, executor, handlers, principals_loaded=True,
           runners: dict[str, Runner] = {})
    .register_local_runner(conn, *, model: ModelClient, channel: Channel, timezone="UTC") -> LocalRunner
```

- `Daemon.__init__` calls it once and sets `DaemonState.components`.
- `gate` is `PolicyGate(conn, principals, registry, dry_run=settings.dry_run, approval_ttl_s=settings.gate.approval_ttl_s, actions=summary_actions(conn))`. `summary_actions` maps `(None, "clear_notes")` to the notes hook (`delete all 4 notes`) and the ops writes to their headings (`restart Ollama`, `restart Resonant`, `update Resonant`). `ops_runner` goes to `register_builtins`. `DryRunGate` stays as a test double.
- If `principals.yaml` is missing, it logs a warning and uses an identity-less stand-in owner, so no channel identity resolves and every channel request is denied (`principals_loaded=False`). An invalid file is an error.
- New fields (runners, channels, ...) get defaults so existing callers keep working.
- `register_local_runner` builds `LocalRunner` from the components' gate, executor and registry and adds it to `runners["local"]`. The daemon builds the model client and channel (the channel needs `principals`), so it calls this after `build_components`.

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

`request_approval` sends `prompt.text()` (`approval_text` with the destructive variant for L1 non-pay, the pay variant for pay) and dedupes by `approval_dedupe_key(approval_id, principal) = "approval:<id>:prompt:<principal>"`: a repeat returns the first ref without sending, so the local runner can re-prompt on every pending step.

Every adapter must:
- Resolve identity to a principal before calling `submit`. Unknown identities are dropped and audited.
- **Never emit events for its own messages**:
  - iMessage reads only rows with `is_from_me = 0`.
  - Resonant's own handle is skipped before routing.
  - Self-test messages carry a nonce and go only to the self-test checker.
- Dedupe sends by `dedupe_key`, rate-limit them, and never interpolate text into scripts. osascript gets its text through argv.
- Treat all message content as untrusted.

`send` failures raise `resonant.gateway.channel` errors, all subclasses of `ChannelError`, whose messages never contain bodies:
- `ChannelRefused`: unknown principal, or no identity on this channel (a raw address passed as `principal` is just an unknown principal).
- `ChannelRateLimited`: over the per-principal limit for longer than the adapter will wait. Nothing was sent.
- `ChannelSendError`: the adapter tried and failed; the message says how to fix it.

**iMessage** (Phase 1):
- Resonant's own Apple ID, with an **email handle** (`channels.imessage.self_handle`, e.g. a dedicated `@icloud.com` address), is signed into Messages.app. It is iMessage only: no SMS, and no phone number on Resonant's account. Rows are still checked for `service='iMessage'` as a second guard.
- A principal may list several handles (phone number and Apple ID email). A sender handle that maps to nobody is dropped and audited.
- **Outbound allowlist:** it sends only to handles listed in principals.yaml (`send` takes a principal, never a raw handle). Any other destination is refused and audited.
- **Send:** osascript, one call per message: `["/usr/bin/osascript", "-e", SEND_SCRIPT, handle, text]` with a 15s timeout (`resonant.gateway.imessage.sender`). The runner is an injectable `OsaRunner` protocol (`async (argv, timeout_s) -> OsaResult(returncode, stdout, stderr)`). Errors map to actionable `SendError`s: -1743 (grant Automation), Messages not running, unknown participant, timeout. stderr is never logged or passed on.
- **Receive:** reads `~/Library/Messages/chat.db` read-only:
  - It is opened with `mode=ro` and never written to, WAL included.
  - A watch on `chat.db-wal` triggers reads, with 2s polling as the fallback.
  - A cursor on `message.ROWID` is kept in `kv`.
  - When `text` is NULL, it parses `attributedBody`.
  - Implemented by `resonant.gateway.imessage.IMessageReader(config, principals, conn, clock)`: `async run(submit)`, `stop()`, `poll_once(submit)`, `health() -> {ok, last_read_at, last_error, fda_ok}`, and `register_selftest_callback(cb) -> unregister`. Events are `source="imessage"`, `type="message"`, `dedupe_key="imessage:<guid>"`, payload `{text, guid, handle, chat_guid, at, truncated}`. The cursor is kv `imessage.cursor`; a first start begins at the newest row. Audits (never with bodies): `imessage.unknown_sender`, `imessage.group_ignored`, `imessage.parse_failed`, `imessage.rate_limited`, `imessage.cursor_reset`.
  - Bodies matching `^resonant-selftest:[A-Za-z0-9_-]{8,}$` go only to the self-test callbacks, whatever their direction or sender, and never become events.
- **Adapter:** `resonant.gateway.imessage.IMessageChannel(config, principals, conn, *, runner=None, reader=None, clock, monotonic, sleep)` implements `Channel` with `name="imessage"`. `start(submit)` sweeps expired send records and runs the reader as a task; `stop()` stops it; `health()` is the reader's health. The daemon builds and starts it when `channels.imessage.enabled` (see Daemon wiring).
  - `send(principal, text, *, thread=None, dedupe_key=None) -> MessageRef(channel="imessage", ref="imessage-out:<id>")`. The handle is `channels.imessage.preferred_handles[principal]` if that is one of the principal's `imessage:` identities, else the first one. Otherwise `ChannelRefused`, audited `imessage.send_refused`. `thread` is ignored (1:1 chats).
  - Dedupe: a `dedupe_key` already sent returns the earlier ref without sending. Records are kv `imessage.sent:<sha256(key)>` = `{ref, at}`, valid 7 days, swept by `start()`. A send that fails partway is not recorded.
  - Rate limit: a token bucket per principal of `channels.imessage.outbound_per_minute` (default 60), one token per part. A short bucket waits up to 30s (audited `imessage.send_rate_limited`, `decision="delayed"`); longer raises `ChannelRateLimited` (`decision="refused"`). Sends are serialized.
  - Text over 2,000 characters is split on paragraph, sentence, then word boundaries, with a ` (i/n)` suffix per part.
  - `request_approval(principal, prompt)` sends `prompt.text()` through `send` (same allowlist, rate limit and errors) with `dedupe_key=approval_dedupe_key(approval_id, principal)`. A repeat returns the first ref and sends nothing. The runner records the ref and send time with `add_channel_ref` after it returns.
  - Audits (never with bodies): `imessage.sent` (`requested_by`, `idempotency_key`, handle, length, chunks, ref), `imessage.approval_sent` (`requested_by`, `level`, `idempotency_key`, approval_id, code, ref; never the summary), `imessage.send_failed` (reason, code, chunk), `imessage.send_refused`, `imessage.send_rate_limited`, `imessage.selftest`.
- **Self-test:** `IMessageChannel.self_test(*, timeout_s=10) -> SelfTestResult(ok, detail)` sends `resonant-selftest:<nonce>` to `self_handle` and waits for a reader to see it. Only this run's in-memory nonce from `self_handle` counts: the `is_from_me = 1` row proves the send and the read path (required); the incoming copy is reported in `detail` but optional. Automation denied, Full Disk Access missing (checked before sending), a missing `self_handle`, and a timeout each give `ok=False` with the fix in `detail`. The result is stored in kv `imessage.selftest` = `{ok, at, detail}` and audited `imessage.selftest`.
  - On a started channel it listens on the running reader (after its first read). Otherwise it polls a private reader with its own cursor, kv `imessage.selftest.cursor` (reset each run), so `resonant selftest imessage` can run next to a live daemon without moving `imessage.cursor`. Both readers drop self-test rows, so they are never routed or answered.
  - The health monitor runs it on its first round after start whenever it is due (no result yet, the last one older than 6h, or a failed one older than 30 min), re-runs it on that schedule, and reports failures (see Health monitor). No Slack.

**Slack:** outbound notifier in Phase 1, two-way Socket Mode in Phase 5.

## Model client: `resonant.models`

```python
class ModelClient(Protocol):
    async def chat_json(self, messages: list[ChatMessage], *, schema: dict, max_tokens=512,
                        temperature=0.0, spans: SpanFactory | None = None) -> ModelReply
    async def chat_tools(self, messages: list[ChatMessage], *, tools: list[ToolSpec],
                         max_tokens=512, spans: SpanFactory | None = None) -> ModelReply
    async def probe(self) -> ProbeResult

ChatMessage = TypedDict(role: "system" | "user" | "assistant", content: str)
ModelReply(text, tool_calls: list[ToolCall], json: dict | None, latency_ms, prompt_tokens, completion_tokens)
ToolCall(name, args)                     # args validated against ToolSpec.args_schema
ProbeResult(ok, model_present, ctx_ok, thinking_off_ok, latency_ms, detail, tool_call_ok=True, call_failed=False)
```

- `OllamaClient(cfg.model)` implements it. `model.api` picks the adapter:
  - `ollama` (default): native `/api/chat` with top-level `think: false`.
  - `openai`: the `/v1` endpoint via the `openai` SDK, with `think: false` and `reasoning_effort: "none"` in `extra_body`.
- Thinking is off on every call, and `<think>…</think>` is stripped before parsing anyway.
- `chat_json` returns schema-valid JSON. Invalid or truncated JSON gets one retry with a "return only JSON" nudge, then `ModelOutputError`.
- `chat_tools` sends tools sorted by name with canonical key order: the same toolset is a byte-identical prefix (`tools_prefix()`). Calls to unknown tools, or with args that fail the schema, are dropped and recorded on the span. Args sent as a JSON string are parsed first.
- Each call gets `model.timeout_s`. A connection error is retried once, immediately. Otherwise (timeout, HTTP error, missing model) it raises `ModelUnavailableError`, so callers can send a templated fallback.
- Each call opens an `llm.call` span through the injected `SpanFactory` (e.g. `partial(span, conn, trace_id=...)`; default: none). Spans record model, adapter, latency, tokens and dropped calls, never prompt or reply text.
- `probe()` never raises. It checks `/api/tags` (pulled), `/api/show` (context ≥ `model.context_tokens`, including a Modelfile `num_ctx`), `/api/ps` (notes a cold model), a 1-token JSON call with thinking off, and one small tool call (`tool_call_ok`): it must return a valid call with nothing in `content` or `thinking`. The tool call is skipped (and `tool_call_ok` is false) when the JSON call already failed. `call_failed` is true when a generation call raised (timeout, connection or HTTP error); the monitor then reports a failed call rather than thinking or leaked reasoning. JSON mode is grammar-constrained, so only the tool call shows a model that reasons despite `think: false` (a thinking-only build such as `qwen3:30b-a3b`, which is Thinking-2507).
- `chat_tools` drops any text that comes beside a valid tool call (it is reasoning, not an answer) and records `content_chars`, `truncated` and `thinking_leak` (an opening or closing think tag, or a `thinking` field) on the span.

## Router: `resonant.router`

```python
class Router:                     # the Loop's router hook (Callable[[Event], Awaitable[NewTask | None]])
    def __init__(self, conn, *, principals, registry, gate: Gate, handlers, model: ModelClient,
                 clock=utcnow, timezone: str = "UTC"): ...
    async def route(self, event: Event) -> NewTask | None
    def toolset_for(self, intent, principal) -> list[ToolSpec]

IntentLabel = Literal["question", "job_control", "system", "notes", "reminders", "ops",
                      "run_project", "unknown"]                               # router/intents.py
toolset_for(intent, principal, *, registry, principals) -> list[ToolSpec]   # sorted by name
match_fast_path(text, ctx: ParseContext | None = None) -> FastCommand | None  # no I/O; ctx: now (UTC) + zone
Labeler(model).label(text, *, spans=None) -> Label(intent, confidence, fallback, latency_ms, prefix_hash)
classify(text, labeler, *, spans=None) -> Classification(intent, path: "fast" | "llm", confidence, fallback, command)
```

`route` handles only `source="imessage"`, `type="message"` events with a known principal and non-empty `payload.text`; it returns None (event consumed) for everything else. Priority is `PRIORITY_OWNER` for the owner and `PRIORITY_MEMBER` otherwise, never from the model. `channel_ref` is `{channel, principal, handle, chat_guid, guid}`. It returns one of:

| Path | `NewTask` | `checkpoint` |
|---|---|---|
| Fast (no model call) | `kind="reply"`, `runner="local"` | `{fast_reply: str, tool_used: str \| None, intent, title}` |
| Side-effecting slash command (no model call) | `kind="command"`, `runner="local"` | `{direct_call: {tool, args}, intent, title}` |
| Labeled (one `chat_json` call) | `kind="chat"`, `runner="local"` | `{intent, confidence, toolset: [tool names], fallback: bool, toolset_prefix_hash, title}` |

`title` is code-built for `/projects`: the fast-path command name (`/status`, `what's running`, `/unknown`) plus a subcommand word (`/notes clear`, `/restart ollama`), `approval reply`, or `chat: <intent>`. Never message text.

`gate` is the full `Gate` (`PolicyGate` once wired, `DryRunGate` before): the fast path uses `evaluate`, `consume` and `pending_for`; the approval inbox uses `resolve_reply`. `clock` drives the "expires in" times of `/approvals` and, with `timezone` (`settings.timezone`), the `/remind` parser.

**Order:** slash commands first (so `/kill` is never read as a reply), then approval replies, then keywords and the labeler.

- **Approval inbox** (`router/approvals.py`, `ApprovalInbox(conn, *, principals, gate)`):
  1. `parse_reply(text, truncated=payload.truncated)`. Text that doesn't parse (including a pasted prompt, `ok`, `yes please`, or any truncated payload) routes normally; it is never swallowed. There is no classifier.
  2. The sender identity `imessage:<payload.handle>` must resolve to the event's principal, because the gate trusts `reply.actor`. A mismatch gets `Nothing is waiting for approval.` and never reaches the gate.
  3. `gate.resolve_reply(ApprovalReply(actor=principal, actor_identity, channel="imessage", raw_text, decision, path="parser", event_id=event.id, occurred_at=event.occurred_at, code, comments))`. It resolves and wakes in one transaction and is idempotent per event id.
  4. On success, or a replay of the same event, `route` returns None: the event is consumed with no new task, and the woken runner sends the confirmation (`Done: …`, `Not done.`).
  5. A refusal gets a code-built `fast_reply` task (`intent="job_control"`, `title="approval reply"`):

  | Refusal | Reply |
  |---|---|
  | `nothing_pending`, `unsupported`, `not_found`, anything else | `Nothing is waiting for approval.` (also every non-approver) |
  | `ambiguous` | `Which one? A7 (restart Resonant), D3 (delete all 4 notes). Reply yes <code> or no <code>.`, listing the actor's pending approvals whose prompts were already sent to them |
  | `code_required` | `Reply yes D7 to confirm.` |
  | `unknown_code` | `Nothing is waiting with code Q9.` |
  | `expired` | `That approval expired. Not done.` |
  | `already_resolved` | `That was already answered.` |
  | `task_not_waiting` | `That task isn't waiting anymore. Not done.` |
  | `not_sent_before` | `That reply came before the question. Reply again to answer it.` |

- **Fast path** (`router/fast_path.py`). The whole message, after lowercasing, straightening apostrophes, collapsing whitespace and dropping trailing `?!.`:
  - Slash commands come from the command registry (below). The first word names the command; the rest (original case) goes to its parser, and a bad argument gets `<hint>. Usage: <usage>` and runs nothing. Trailing `?!.` is not an argument to a command that takes none. None of these ever reach the model.

  | Command | Tool | Notes |
  |---|---|---|
  | `/status` | `daemon_status` | |
  | `/projects [all]`, `/tasks [all]` | `projects` | |
  | `/note <text>` | `add_note {text}` | Text as typed (inner newlines kept), one pair of surrounding quotes dropped, at most 200 chars |
  | `/notes` | `list_notes` | `Notes (2):` then `2. call mom`, newest first |
  | `/notes clear` | `clear_notes` (L1) | Always asks: `[D7] delete all 4 notes? This can't be undone. Reply yes D7 / no` |
  | `/unnote <n>` | `delete_note {id}` | `3` or `#3` |
  | `/remind <when> <text>` | `add_reminder {at, text, routed_at}` | `tools.when.split_when(rest, now, tz=settings.timezone)`; `at` is ISO with the zone's offset. A `WhenError` is the hint (`say 3pm or 15:00. Usage: …`, `that time has passed`) |
  | `/reminders` | `list_reminders` | `Reminders (1):` then `3. Tue 3:00pm: stretch` |
  | `/unremind <n>` | `cancel_reminder {id}` | |
  | `/restart ollama\|resonant` | `restart_ollama` / `restart_resonant` | Owner only |
  | `/update` | `update_resonant` | Owner only |
  | `/disk` | `disk` | Owner only; `render_disk` |
  | `/logs [n]` | `recent_logs {n?}` | Owner only; any positive `n`, capped at 20; `render_logs` |
  | `/approvals [code]` | none | Owner only, below |
  | `/help [word]` | none | Below |
  | `/kill`, `/resume` | none | Owner only: `killswitch.engage` / `release`, audited |

  - **Permission before arguments.** An owner-only command from anyone else gets `not allowed` and is audited as `router.admin_refused`; a command whose tools the asker can't request gets `not allowed`. Either way no usage line is shown.
  - **Reads** (`effect=read`) run inline and reply from a template (`COMMAND_RENDERS`). **Side effects** never run here: the router returns a `kind="command"` task with `checkpoint.direct_call = {tool, args}`, and the local runner pins exactly that call and gates it with `origin="explicit"` (the only place that origin comes from). The args, note and reminder text included, are kept in the task checkpoint, as on the model path; the title never has them. The task's intent is the command's (`notes`, `reminders`, `ops`, …). The gate decides as for any call: explicit L3 and L2 run and reply with a code-built report (`Saved note 1: "buy milk"`, `⏰ Reminder 3 set for Tue 3:00pm: "stretch"`, `Cancelled reminder 3.`, `Restarting Resonant…`), L1 asks for a code, the kill switch asks for everything, and dry_run replies `Dry run: would restart Ollama` for ops while notes and reminders run.
  - An unknown `/word` gets `Unknown command /remnd. Did you mean /remind?` (`difflib.get_close_matches`, n=3, cutoff 0.6, over the names of the commands the asker may use, keeping those within 0.1 of the closest), or `Unknown command /word. Try /help.` when nothing is close. A member is never pointed at an owner-only command. A suggestion never executes, even one letter away. Text that isn't a `/word` gets `Unknown command. Try /help.`
  - `/help` lists the commands the asking principal may use (no tool call, so members get it too), plus the keyword hint for principals who can use global tools. `/help <word>` keeps the ones whose name or description contains the word, else a `difflib` match over names and description words; none gives `No command matches "<word>". Try /help.`
  - `/approvals` lists the owner's pending approvals from `gate.pending_for`, oldest first: `- A7 · restart Resonant · expires in 29m`, then `Reply yes <code> or no <code>. /approvals <code> shows the details.` (or `Nothing is waiting for approval.`). `/approvals <code>` adds the tool, level, effect and the intent's full args (`store.approvals.intent_args`, pretty JSON, clipped at 1500 characters), since the summary masks them. A code outside the approval alphabet gets the usage line; one that isn't pending gets `Nothing is waiting with code Q9.`
  - Only read tools run inline here. A keyword never names a side effect (if one did, it would fall through to the labeler).
  - Keywords: `status` and `ping` (`daemon_status`), `what's running` / `whats running` / `what is running` (`list_tasks {status: running}`), `how's the mini` / `hows the mini` / `how is the mini` (`system_status`).
  - A read command maps to one builtin read; the reply is a template over its output (code computes, text narrates). The read is evaluated by the gate (audited, `task_id="route:<event id>"`). No task exists yet, so the fast path consumes the Allow token and calls the read handler directly instead of the executor (reads only; nothing to dedupe). A tool error becomes a templated "Couldn't read … right now."
  - If the principal can't request the tool, a slash command replies `not allowed` and a keyword falls through to the labeler.
- **Command registry** (`router/commands.py`). `Command(name, usage, description, intent, tool: str | None, parse: (rest, ctx) -> args, owner_only, subs: tuple[Sub, ...])`; `Sub(word, tool, parse)` is a fixed first word that picks another tool (`/notes clear`, `/restart ollama`). `cmd.resolve(rest, ctx) -> (sub word | None, tool, args)`; a command with no tool of its own needs a subcommand. `ParseContext(now, tz)` carries the clock and zone for `/remind`. `parse` raises `UsageError(hint)` for bad args, and the reply is `<hint>. Usage: <usage>` (or just the usage). `COMMANDS` is the single source for the fast path, `/help`, suggestions and `list_commands`. Helpers: `get(name)`, `suggest(word, commands)`, `unknown_reply(word, commands)`, `available(principal, *, principals, registry)` (owner-only commands need `admin`; a command with tools needs each registered and `request` on its extension), `can_request(principal, tool, *, principals, registry)`, `search(query, commands)`, `describe(cmd, registry)` (the level comes from the tool registry, never the command) and `help_text(commands, query, *, keywords)`. A command with a read tool has a reply template in `fast_path.COMMAND_RENDERS`, and each side-effecting tool has a report line in `runners.local.REPORTS` (a test checks both); shared templates live in `router/render.py`. `render_projects` lists running, waiting(reason), queued, then finished items, one line each (`- waiting(approval) · chat · chat: ops · 2m`), and stays within one iMessage chunk (2000 chars), ending `+N more (/projects all)` when items are left out.
- **Labeler** (`router/labeler.py`, `router/prefixes.py`). One `chat_json` call with schema `{intent: enum, confidence: 0..1}`, `max_tokens=20`, temperature 0. The prompt is the constant `LABEL_PREFIX` (system rules, intent definitions, 12 few-shot turns) and then the user text, truncated to 2000 characters and wrapped by `wrap_untrusted` as `<message>…</message>` (tags inside it are stripped). `ModelUnavailableError` gives `unknown` with `fallback=true` (the runner sends `prefixes.FALLBACK_REPLY`); `ModelOutputError` or an out-of-enum intent gives `unknown` with `fallback=false`.
- **Intents and toolsets** (contract change C9, Phase 2 D10: `notes`, `reminders` and `ops` were added, so plain chat reaches the same tools as the slash commands through the model). `INTENT_TOOLS` maps intents to builtins:

  | Intent | Tools |
  |---|---|
  | `question`, `run_project`, `unknown` | none |
  | `job_control` | `list_tasks`, `projects`, `task_counts` |
  | `system` | `daemon_status`, `list_commands` ("what can you do?"), `model_status`, `system_status` |
  | `notes` | `add_note`, `clear_notes`, `delete_note`, `list_notes` |
  | `reminders` | `add_reminder`, `cancel_reminder`, `list_reminders` |
  | `ops` | `disk`, `recent_logs`, `restart_ollama`, `restart_resonant`, `update_resonant` |

  It is filtered by `principals.can(principal, "request", spec.extension)` before anything reaches the model, so a member gets no global builtins. Every call the model chooses is `origin="inferred"`, so the gate decides by level: L3 (`add_note`, `add_reminder`, `restart_ollama`, …) runs and reports, L2 (`restart_resonant`, `update_resonant`) asks `[A7] … Reply yes / no`, L1 (`clear_notes`) asks `[D7] … Reply yes D7 / no`. The local runner still makes at most one gated side effect per task. `extension:<name>` intents are reserved for Phase 3.
- **Prefixes for the local runner.** `prefixes.runner_system(intent)` is the runner's constant system message (plus a constant per-intent rule: for `reminders`, pass `at` as an ISO-8601 datetime with its UTC offset); `toolset_prefix_hash(intent, toolset)` hashes it with `tools_prefix(toolset)`, so it is stable per (intent, principal scope). `prefixes.current_turn(intent, text, *, now, tz, max_chars)` is the runner's last user turn: `wrap_untrusted(text)`, and for `TIMED_INTENTS` (`reminders`) preceded by the code-built `time_context(now, tz)` line `Current time: 2026-10-13T15:04:30-07:00 (Tue, America/Los_Angeles).`. The time changes every minute, so it goes last rather than in the system message: the system message, tools block (some chat templates render it inside the system turn) and history stay byte-identical for KV-cache reuse.
- **Label prefix snapshot.** `tests/fixtures/router/label_prefix.json` pins `LABEL_PREFIX` and `LABEL_SCHEMA`. Changing an intent, a definition or a few-shot changes `LABEL_PREFIX_HASH` and invalidates the KV cache; regenerate the snapshot deliberately in the same PR.
- **Spans.** `router.approval` (`path, outcome: resolved|replay|refused|unauthenticated, reason, decision`), `router.fast_path` (`command, handled, tool_used, latency_ms`), `router.label` (`intent, confidence, fallback, latency_ms, prefix_hash`) and `router.toolset` (`intent, principal, toolset, toolset_prefix_hash`). Never message text. A non-owner `/kill` or `/resume` is audited as `router.admin_refused` (principal and command only).
- **Evals.** `classify` uses the production matcher and labeler from the owner's point of view, but runs no tools and never touches the kill switch.

## Health monitor: `resonant.monitor`, `resonant.health`

```python
HealthMonitor(conn, *, model: ModelProber | None, channel: MonitoredChannel | None, owner: str | None,
              daemon_healthy: Callable[[], bool], db_path=None, model_meta=None, clock=utcnow,
              sleep=asyncio.sleep, disk_free=..., interval_s=60, ...)
    async def run() -> None            # evaluate() every interval_s until stop()
    async def evaluate() -> dict[check, CheckState]
    def deadman_reason() -> str | None
deadman_task(url, interval_s, healthy, client=None, reason: Callable[[], str | None] | None = None)
health_snapshot(conn) -> {check: {state, since, detail, checked_at}}
channels_snapshot(conn, *, imessage_enabled, channel) -> [{name, enabled, ok, last_read_at, last_selftest}]
```

- Checks, every 60s: **model** (`probe()` every 5 min, cached in kv `model.probe`); **imessage** (channel `health()` plus kv `imessage.selftest`; the self-test re-runs every 6h, every 30 min while the last one failed, and after 2 consecutive reader errors); **loop** (`Daemon.healthy()`, any `event.dead_letter` audit row in the last hour, more than 2 tasks failed in the last hour); **store** (a kv write succeeds, free disk on the DB volume > 2GB). A check whose dependency is None (no model client, no channel) is not evaluated and stays `unknown`.
- State per check: `unknown | ok | degraded`, persisted in kv `health.<check>` (`{state, since, detail, checked_at}` plus alert bookkeeping), so a restart doesn't re-alert an incident.
- Owner alerts: after ≥ 2 consecutive degraded evaluations (a reused, cached model probe doesn't count, so the model alerts after 2 failed probes), `channel.send(owner, "hey, some issues here: <check>: <detail>", dedupe_key="health:<check>:<window>")`, where `window = floor(epoch / 6h)`. One alert per incident, at most one per check per 6h. When an alerted incident clears: `"resolved: <check> is healthy again"` with `dedupe_key="health:<check>:<window>:resolved"`. A failed send is retried next round. Audited `health.alert` / `health.resolved` (`decision` `sent`, or `failed` once per streak of failed sends).
- Texts and reasons are built by code from short details: URLs are replaced with `<url>`, details are capped (120 chars), and no secrets are included.
- While the `imessage` check is degraded, or there is no channel or owner, nothing is sent over iMessage. `deadman_reason()` then returns the degraded checks; it also does so, prefixed `owner alerts failing`, after 3 consecutive failed owner sends (`"imessage: imessage selftest failed: …; model: …"`), else None.
- `deadman_task`: while `healthy()` and `reason()` is None it GETs `url`; otherwise it POSTs `url/fail` with a `text/plain` body (≤ 500 chars): the reason, prefixed by `event loop stale` when the loop is stale. Ping failures log only the exception type; the URL is never logged.
- The daemon starts `DaemonState.monitor.run()` and passes `deadman_reason` to the dead-man only when `DaemonState.monitor` is set, which `build_imessage_stack` does when `channels.imessage.enabled` (owner: the principals.yaml owner, or None when the file is missing; `model_meta` = `{model, api}`). Without it, `/api/status.health` shows `unknown` and `/api/channels` shows the channel's config and last self-test.

## Local runner: `resonant.runners.local`

```python
LocalRunner(conn, *, model: ModelClient, channel: Channel, gate: RunnerGate, executor: ToolExecutor,
            registry, max_step_s=30.0, now=utcnow, timezone="UTC")   # name="local", uses_claude_slot=False
# ToolExecutor: execute(intent, decision, *, step_no, call_index) and recorded(key)
```

The daemon sets `max_step_s = wiring.local_step_limit(model, registry.specs()) = max((MAX_TOOL_ITERATIONS + 1) * model.timeout_s, longest tool timeout_s) + 15` (375s with the defaults: `update_resonant`'s 360s beats the model's 240s), so the model client's or the executor's own timeout always fires first: the user gets the templated fallback instead of a cancelled step, and `/update` is never cancelled into an ambiguous outcome. The loop's stuck check stays a backstop.

One step answers the whole task, unless it waits for an approval. In this order:

1. `checkpoint.reply` exists: an earlier attempt computed it, so only (re)send it.
2. `checkpoint.call` exists: resume the task's pinned gated call (below). No model call.
3. `checkpoint.direct_call = {tool, args}` (written only by the router's command parser): pin it as `call` with `call_index=0` and run it as a gated call with `origin="explicit"`. This is the only place `explicit` is set, and only when the pinned call is exactly the `direct_call`. An unregistered tool replies `Not done: that tool isn't available.` and runs nothing. No model call.
4. `checkpoint.fast_reply`: send it as is (the router already ran its read). No model call.
5. `checkpoint.fallback`: send `prefixes.FALLBACK_REPLY`. `intent == "run_project"`: send `ESCALATE_REPLY` ("needs the Claude runner, which arrives in Phase 5") with `escalate=true`. No tools, no model, never Claude.
6. Context: `runner_system(intent)`, the checkpoint's `toolset` (looked up in the registry, sorted by name), this principal's previous `imessage` messages (at most 5, so 6 events with the current one; each with our reply to it, from the task it started; messages that started no task, such as approval replies, are skipped; earlier tool outputs appear only as those replies), then the current message as `prefixes.current_turn` (wrapped by `wrap_untrusted`, 2000 chars; for `reminders`, after the `Current time:` line in the runner's `timezone`, which the daemon sets to `settings.timezone`).
7. Tools: up to 3 `chat_tools` calls and 3 tool calls in all. Each call is `Intent.for_tool(spec from the toolset, task_id, principal, args, origin="inferred")` → `gate.evaluate`. The model path is always `inferred`.
   - A read that is allowed runs (`executor.execute(..., step_no=task.step_no, call_index=i)`) and its result goes back to the model. A refused read, a tool outside the toolset, bad args, or a failed read goes back as a tool error.
   - Anything else (a side effect, or any call that needs approval) is the task's **one gated call** and ends the loop. `Allow` and `NeedsApproval` pin it as `call` first. Allowed, it runs; then the model may narrate, but the reply always contains the call's code-built report line (appended if the model left it out; the report alone if the model fails). Needs approval: the task waits. `DryRun` or `Deny`: the templated reply only.
   - A model reply asking for more calls than are left escalates like `run_project`. An empty toolset skips `chat_tools`.
   - `ChatMessage` has no `tool` role, so a call is replayed as an assistant turn `{"tool_call": {name, args}}` and its result as a user turn labeled untrusted data. The model contract is unchanged.
8. Answer: `chat_json` with schema `{reply: string}`, clipped to 600 chars. **Number guard:** every number in the reply must appear in a tool result or in the user's message; otherwise, and on `ModelOutputError` or an empty reply, the reply is a templated summary of the tool results.
9. `ctx.save(checkpoint={...checkpoint, "reply", "reply_meta"})`, then `channel.send(principal, reply, dedupe_key=f"reply:{task.id}")`, then `Done({reply_len, tools_used, latency_ms, path, escalate})`. `path` is `fast | llm | summary | fallback | escalate`, or for a gated call `done | replay | failed | ambiguous | refused | dry_run`; `tools_used` lists the tools that ran successfully.

**Gated call** (`checkpoint.call = {tool, extension, args, step_no, call_index}`), at most one per task, exactly once:
- **Pinned** with `ctx.save` before it is gated (direct) or run or prompted (model), so `idempotency_key(task_id, step_no, call_index, intent.hash)` stays the same across retries, waits and restarts.
- **Resume:** `executor.recorded(key)` first. Succeeded: replay the report, no gate call. Started: ambiguous, reported (`Not sure whether <summary> ran…`) and never re-run. Failed: the definite failure is reported. None: `gate.evaluate`.
- `Allow`: `executor.execute(intent, decision, step_no=pinned, call_index=pinned)`. The reply is code-built: `Saved note 3: "buy milk"`, `Deleted note 3.`, `Cleared 4 notes.`, `⏰ Reminder 3 set for 3:20pm: "stretch"` (the due time from `format_due`), `Cancelled reminder 3.`, `Restarting Resonant…` (builtins in `REPORTS`; see [Ops](#ops-resonantops-resonanttoolsops)), else `Done: <summary>`. A definite failure replies `Failed: <summary>` with the handler's `ToolFailed` message. `<summary>` uses the ops heading when there is one (`Failed: update Resonant (…)`), as approval prompts and dry-run replies do.
- `NeedsApproval`: `channel.request_approval(approver, ApprovalPrompt)` for each approver, then `approvals.add_channel_ref(id, ref, at=now(), principal)`, then `Wait("approval", wake_at=expires_at)`. Every pending step re-prompts (a spurious wake, a crash between save and prompt); the channel dedupes by `approval_dedupe_key`, and the first send time is kept. `ChannelRefused` fails for good; any other `ChannelError` is a retryable `Fail`.
- `DryRun`: `Dry run: would <summary>`. `Deny`: `Not done.` (`NOT_APPROVED`), `Not done: the approval for <summary> expired.` (`APPROVAL_EXPIRED`), `Not done: that approval was already used.` (`APPROVAL_USED`), else `Not done: <reason>.`
- After an approval or a direct call there are no more model rounds: the reply is templated.

Failures: `ModelUnavailableError` sends one templated fallback (the report line if a side effect ran, else the tool summary if tools already ran, else `FALLBACK_REPLY`) and the task is `done`. `ChannelRefused` is a final `Fail`; any other `ChannelError` is `Fail(retryable=True)`, and the dedupe key prevents a double send. Spans: `runner.local`, `runner.local.context`, `runner.local.tool`, `runner.local.call` (`origin`, `decision`, `recorded`), `runner.local.approval`, `runner.local.final` (`outcome`: `ok | number_guard | bad_output | empty`) and `runner.local.send`, plus the model's `llm.call`. None carry message or reply text.

## Daemon wiring: `resonant.wiring`, `resonant.daemon`

```python
build_imessage_stack(settings, conn, components, loop, *, daemon_healthy, model=None, osa_runner=None)
    -> IMessageStack(channel, router, runner, reminders, monitor, model, owner)
IMessageStack.start(loop) / .stop() / .aclose() / .running
local_step_limit(model_cfg, tools: Iterable[ToolSpec] = ()) -> float
Daemon(settings, *, model: ModelClient | None = None, osa_runner: OsaRunner | None = None,
       ops_runner: OpsRunner | None = None)
```

- **Gate.** Always `PolicyGate`, iMessage on or off. The startup log line includes `gate=policy dry_run=<bool>`. With `dry_run: true`, L0 reads (chat and the fast path) are allowed exactly as before.
- **`channels.imessage.enabled: false`** (the default): the daemon is Phase 0 apart from the gate. `loop.router` is None, `loop.runners == {"echo"}`, `DaemonState.monitor` is None, and nothing imports the model client.
- **Enabled:** `Daemon.__init__` calls `build_imessage_stack` once, after `build_components`. It builds `IMessageChannel(config, principals, conn, runner=osa_runner)`, `Router(...)` over the components' registry, gate and handlers, registers `LocalRunner` (`components.register_local_runner(..., timezone=settings.timezone)`, then `loop.runners["local"]`, `max_step_s = local_step_limit`) and `ReminderRunner` (`runners["reminder"]`, see Reminders), sets `loop.router = router.route`, and builds `HealthMonitor(conn, model, channel, owner, daemon_healthy=Daemon.healthy, db_path, model_meta)`, which becomes `DaemonState.monitor`. `model` defaults to `OllamaClient(settings.model)` (closed by `aclose`); `osa_runner` defaults to real osascript. Tests and `resonant bench e2e` inject `resonant.e2e.ScriptedModel` and `RecordingOsaRunner`; tests also pass `ops_runner=FakeOpsRunner()` (it goes to `build_components`).
- **Start:** `Daemon.run` starts the channel (`channel.start(loop.submit)`, then `ops.back_up`: `Back up (<commit>), down Ns` once after a requested restart) before the loop, then the loop, the API, the monitor and the dead-man (with `monitor.deadman_reason`).
- **Stop** (SIGTERM/SIGINT, or a component exiting): the API is told to exit; the monitor and the chat.db reader stop first, so no new events arrive; then the loop drains in-flight steps (10s grace; a draining step can still send its reply); then the model client closes and the store closes. Unfinished steps resume from their checkpoints on the next start, and the send dedupe key (`reply:<task id>`) prevents a double reply.

## Spans and logs: `resonant.observability`

- `with span(conn, name, trace_id=..., task_id=..., parent_id=..., **attrs) as s: s.set(...)` writes a row to `spans` with status and timing.
- Logs are JSON lines in `~/.resonant/logs/resonant.jsonl`, rotated at 20MB × 5. Standard-library loggers flow through structlog.
- `httpx` and `httpcore` are kept at WARNING because request URLs can carry credentials, such as a secret in the dead-man URL.

## CLI exit codes

- `resonant status`: 0 when the daemon is up, 1 when it is down (in both text and `--json` modes). It never creates the store. Text mode ends with a health section: one line per check (state, since, and the detail when degraded).
- `resonant approvals list [--json]`, `show <id|code> [--json]`, `approve <id|code>`, `deny <id|code>`: 0 on success, 1 when nothing matches or the gate refuses (already resolved, expired, task no longer waiting), 75 when the store is missing or its schema is behind. They never create or migrate the store; `list` and `show` open it read-only. `list` shows pending approvals the owner can approve (code, tool, expires in, summary, id). `show` adds the full args from the `gate.evaluate` audit row, which the summary masks. An id is tried first; codes are reused over time, so a code matches only a currently pending approval. `approve` and `deny` print the summary, then call `PolicyGate.resolve(ApprovalResolution(actor=<owner>, actor_identity="cli", path="cli"))`, which resolves and wakes the task in one transaction; the running daemon picks the task up on its next tick.
- `resonant job-guard`: 75 when paused or when the state is unknown, 127 when exec fails, 2 when no command is given.
- `resonant selftest imessage [--timeout S] [--json]`: 0 when the self-test passes, 1 otherwise. It sends a real iMessage to `self_handle`, stores kv `imessage.selftest`, and is safe to run while the daemon is up. macOS grants Automation per responsible app, so running it from a terminal proves the terminal's grant, not the daemon's.
- `resonant model probe` and `resonant model bench`: 1 when the probe fails (model missing, context too small, or thinking not off). `bench` also exits 1 when every call of a kind failed. `probe` stores its result in kv `model.probe` if the store exists, and never creates it.
- `resonant bench e2e [--n N] [--model fake|real] [--json]`: 0 when every prompt got a reply and each path's p50 meets its target (`fast` ≤ 1000 ms, `llm` ≤ 3000 ms), 1 otherwise, 2 for an unknown `--model`. It runs the production reader, loop, router, runner and channel in a throwaway home with a `FakeChatDB` and a `RecordingOsaRunner`, timing each prompt from the chat.db row insert to the osascript call (N per path, after one untimed warm-up per path). `--model fake` (default, CI) uses `ScriptedModel`; `--model real` uses the configured Ollama model. It never texts anyone and never touches `~/.resonant/resonant.db`.
- `resonant eval router [--set PATH] [--repeat N] [--json] [--min-accuracy F]`: 0 when accuracy ≥ `--min-accuracy` (default 0.9), 1 when it's below, 2 when the set is missing or malformed. Texts appear only in its output. The summary saved to `~/.resonant/evals/results/<ts>.json` refers to examples by line number.

## Local API

All endpoints are loopback only. Remote status access is planned via the push-only status relay ([`followups.md`](followups.md#status-relay-dead-man-switch-and-remote-dashboard)), never by exposing the API.

| Endpoint | Returns |
|---|---|
| `GET /healthz` | `{"ok": true}` |
| `GET /api/status` | version, uptime, last tick, dry_run, autonomy, paused, task counts by status and wait reason, `approvals_pending` (unexpired pending approvals, all approvers), in-flight count, `health: {model, imessage, loop, store}` each `{state: unknown\|ok\|degraded, since, detail, checked_at}` |
| `GET /api/channels` | `[{name: "imessage", enabled, ok, last_read_at, last_selftest: {ok, at, detail} \| null}]`; `ok` and `last_read_at` are null without a live channel |
| `GET /api/tools` | registered tool specs: `[{name, extension, effect, level, description, args_schema}]` (`level` is the effective level) |
