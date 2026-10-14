# Phase 3 plan (v2): first-party extensions, scheduled jobs, secrets, the daily briefing

Status: **implemented** (P3-1 to P3-16, 2026-10-14). The live checks are [`go-live.md`](go-live.md#12-phase-3-extensions-jobs-and-the-briefing) step 12; what was deferred is in [`followups.md`](followups.md). This is v2 of the plan, after the architecture and security reviews and the owner's decisions. Scope: the extension manager from [`architecture.md`](architecture.md#components), cut down to **first-party, owner-installed** extensions: CLI tools, scheduled jobs and notifications. Also in scope:
- Keychain secret injection and L3 reports for jobs with no requester ([`followups.md`](followups.md)).
- A code-built **daily briefing**: the first proactive nudge.
- The first real extension, `github`.

MCP, critical-job plists, veto windows, the Seatbelt sandbox, iMessage installs and third-party sources are deferred, with their designs kept in [§8](#8-out-of-scope-and-deferred). Deviations from the frozen [`interfaces.md`](interfaces.md) are under [Contract changes](#contract-changes-flagged).

Each ticket is one PR (~200–600 lines including tests), run through robot-surf: worktree, tests, PR, CI, code-review agent, squash-merge to `main`. Each ticket updates **its own** `interfaces.md` section.

---

## 1. Goal and exit criteria

**Goal.** The owner installs first-party extensions from the Mini's terminal and uses them from iMessage. Their jobs and Resonant's own briefing run on schedule with nobody texting. The trust rules don't change:
- Effects and levels come from the approved manifest, never from the model.
- Extension processes get an environment built from scratch, holding only their own declared secrets.
- Jobs nobody is watching run only at L0 (silent) or L3 (run and report). L2 and L1 ask.
- The kill switch stops jobs, including any extension process already running.

**Exit criteria.** Required (**R**) items are automated in-process e2e tests (`tests/test_e2e_phase3.py`: `FakeChatDB`, `RecordingOsaRunner`, `ScriptedModel`, fake clock, `FakeExtProcess`, `FakeSecretStore`, a fake `gh` on `PATH`) and items on the live checklist ([§7](#7-rollout)). Deferred (**D**) items move to follow-ups.

| # | | Scenario | Pass condition |
|---|---|---|---|
| E1 | R | **Terminal install.** `resonant ext add repo:example` | 1. A code-built heading `example repo:example@a1b2c3d4 digest 9f3e01aa`. 2. Escalations first (secrets, approvers, L1/L2 tools, network-using tools), then tools, jobs and schedules; every manifest string is sanitized. 3. Confirm by typing `example`. 4. The approved tree is copied to `~/.resonant/extensions/example/` and re-verified before the row is written, with audit `ext.install`. 5. The daemon restarts, and the owner gets `Back up …` plus `Installed example`. |
| E2 | R | **Explicit tools.** `/example get_time Asia/Tokyo`, `/example write_note hi` | The L0 read answers inline. The L3 write runs and replies `Done: example: write note text="hi"`. `/help example` lists both with their levels. `/exmaple` gets `Did you mean /example?` and runs nothing. |
| E3 | R | **Chat.** "what time is it in tokyo" | Labeled `extension:example`. The toolset holds only example's tools. The call is `inferred`. Earlier replies in the history are wrapped as untrusted. |
| E4 | R | **Scheduled L0 job + notify.** `morning-ping` due | On the fake clock it runs within `tick_s` of its due time, and `[example] ping` is sent exactly once. No `tasks` row: only `ext_jobs.last_due` and a `job.run` audit row. |
| E5 | R | **L3 job.** | Runs, then reports `example/cleanup: done` to the extension's approvers (the owner by default). A definite failure reports `example/cleanup failed: <reason>` at most once per job per local day. |
| E6 | R | **L2 / L1 job asks.** | L2: `[A7] example: rotate logs? Reply yes / no`. L1: `yes <code>`. Unanswered, it expires as not done. A job whose effective level asks and whose cron fires more often than hourly is refused by `ext validate`. `scheduled` never counts as `explicit`. |
| E7 | R | **Secrets.** `/github prs` with `github/token` in the Keychain | 1. An env-dump fixture tool sees exactly its declared `RESONANT_SECRET_*`, and a sibling tool without secrets sees none. 2. A sentinel value printed raw, URL-encoded, JSON-escaped and base64-encoded at each of the 3 alignments becomes `[redacted]` in `executions.result`, `audit_log`, spans, `logs/resonant.jsonl`, model messages and replies. 3. A missing secret replies `Not done: secret github/token is missing. On the Mini: resonant secret set github/token`. |
| E8 | R | **Kill switch.** `/kill` (or `resonant kill`) | 1. Due jobs are skipped (audit `job.skipped_paused`), with no catch-up after `/resume`. 2. An in-flight extension process (a fixture that sleeps and spawns a child) is killed by process group and its tracked tree within one tick. 3. Notifications are held. 4. `/resume` restores it. |
| E9 | R | **Pin and tamper.** | 1. Editing a file in `~/.resonant/extensions/example/` means that on start it is disabled and the owner is alerted once: `example changed on disk since you approved it; disabled. resonant ext update example to review`. 2. A Resonant `/update` that changes `extensions/example` in the repo does **not** change the installed copy. 3. A tracked symlink that resolves outside the root is refused at add. |
| E10 | R | **Update, remove, adopt.** | 1. `resonant ext update example` shows a diff and asks for the typed confirm every time (`code only` when the manifest is unchanged). 2. `resonant ext remove example` removes its tools, jobs and commands; keeps the data dir; lists the Keychain items left. 3. A re-add from a different source while old secrets or data exist is refused without `--adopt`. |
| E11 | R | **Missed runs, precedence, DST.** | 1. Daemon down over a due time: one run if within `catchup`, marked late; older ones are audited `job.missed`. 2. Paused over a due time: skipped, never caught up. 3. Previous run still running or waiting: skipped, audited `job.overlap`. 4. Spring-forward gap: runs once just after the jump. Fall-back fold: once. |
| E12 | R | **github watch, deterministic.** A fake `gh` drives PR #40 red → green → red, and a crash is injected between persisting an event and sending it | Exactly three notifications (`✗ resonant#40 CI failed (lint)`, `✓ resonant#40 CI fixed`, `✗ …`), none duplicated after the restart, and no "fixed" message for PRs that were green on the first run. |
| E13 | R | **Daily briefing.** 08:00 local, or `/ext run core/briefing` | One message: today's reminders, open and waiting work, pending approvals, degraded health, and a github line from today's `ext.github.digest` event if there is one. Each section is omitted when empty, and it says `Nothing on today.` when all are. It is sent once a day (dedupe `briefing:<date>`) and skipped while paused. |
| E14 | R | **`/ext list`, `/ext run`, `/ext mute`** | `/ext list` shows name, pin, status, jobs, last run and health. `/ext run github/watch` is gated as an explicit call at the job's level. `/ext mute github 1h` holds notifications until the time passes (audit `ext.notify_muted`), and `/ext mute github off` lifts it. All three are owner-only. |
| E15 | R | **github, live.** | A real PR going red produces one iMessage within one watch period; going green again produces `fixed`. `/github prs` lists open PRs with their CI state. |
| — | D | MCP typing and pins; critical jobs via launchd; veto windows; Seatbelt isolation; iMessage `/ext add`; git sources | See §8. |

---

## 2. Current state

### Exists

| Area | Where | State |
|---|---|---|
| Manifest v1 | `resonant_sdk/manifest.py` `Manifest`, `Job`, `CliTool`, `tool_specs()` | Strict. `Job = {id, schedule, tz, critical, run}`, with **no effect or level**. Cron is only checked for having 5 fields. `secrets` entries aren't slug-checked. `args_schema` lacks `additionalProperties: false`. |
| Validate | `cli.py` `ext_validate`, `extensions/checks.py` `approver_errors` | Schema and approver grants only. `extensions/__init__.py` is a stub. |
| Registry | `tools/registry.py` | Keyed by `(extension, name)`. `unregister_extension` exists. |
| Executor | `executor.py` | `self.handlers = dict(handlers)` is copied at construction. `ToolHandler = (args) -> dict`. **No secrets, no redaction.** |
| Gate | `gate/policy.py` `rule`, `_ask` | Risk × origin. `Origin = explicit \| inferred`. `_ask` always uses `approvers=(owner,)`. |
| L1 prompt text | `gateway/channel.py:48` `ApprovalPrompt.text` | Every L1 non-pay uses `[D7] …? This can't be undone. Reply yes D7 / no`. **Checked:** that's accurate for every L1 that reaches iMessage in Phase 3 (`clear_notes`, `merge_pr`) and only over-cautious for an unlevelled write, so **no change**. Installs are terminal-only, so they never use it. |
| Local runner | `runners/local.py` `_toolset`, `_direct`, `_is_explicit`, `_context`, `_ask` | Looks up `registry.get(None, name)` only. `_is_explicit` requires `extension is None`. Prior replies go into the history as **unwrapped** assistant turns. A `ChannelRefused` from the approver fails the task. |
| Router | `router/intents.py` (static `IntentLabel`, reserved `extension:`), `prefixes.py` (constant `LABEL_PREFIX`), `commands.py` (static `COMMANDS`) | Builtins only. |
| Step limit | `wiring.py` `local_step_limit(settings.model, components.registry.specs())` | **Checked:** it takes the max over every registered tool's `timeout_s`, so an extension tool with a long timeout would inflate the local runner's step limit. |
| Kill switch | `killswitch.py`, `cli.py` `job_guard` | The `paused`/`autonomy` flags. Nothing kills running child processes. |
| Subprocess | `ops/__init__.py` `SubprocessRunner` | argv, timeout, `killpg`. It inherits `os.environ` and has **no output cap**. |
| Health | `monitor.py` | `CHECKS` is a fixed tuple of four. |
| Loop | `loop/loop.py` `wait_s`, `_route_unrouted` | Next-wake. Unrouted events go to the router hook, are retried every tick, and are dead-lettered after 5 failures. |
| Example | `extensions/example/` | One job, an L0 read, an L3 write, `notify`, `health`. It writes `notes.txt` beside `cli.py`. |
| Deps | `packages/resonant/pyproject.toml` | No cron library. `gh` is at `/opt/homebrew/bin/gh` on the Mini. |

### Gaps
- The extension store, sources, tree copy and pins.
- The process runner (scratch environment, output caps, tracked process trees).
- `SecretStore` and redaction.
- The scheduler, job execution and catch-up.
- Origin `scheduled` and manifest approvers.
- Extension intents, toolsets and commands.
- Notify from persisted events, mute, extension health.
- The briefing, the `github` extension, go-live.

---

## 3. Design decisions

### D1. Sources, the approved copy, pins
- **Sources.**
  - `repo:<name>` is `extensions/<name>` in the Resonant checkout (`ops.repo_path`).
  - `path:<abs dir>` must be inside a git work tree.
  - Nothing else: **no third-party or network sources in Phase 3** (D13).
- **Install copies the approved tree.**
  - `repo:` uses `git archive <HEAD commit> extensions/<name>`.
  - `path:` copies the tracked files (`git ls-files -z`).
  - Either way it goes to `~/.resonant/extensions/<name>/` (made read-only with `chmod -R a-w`).
  - Extensions **never run from the live checkout**, so `/update` can't change extension code underneath them (E9).
- **Pin.**
  - `commit` (when there is one) plus `tree_digest`: a sha256 over the sorted `(path, mode, sha256(content))` of the installed copy.
  - The digest is computed on the source at staging and recomputed on the copy **before** the row is written. A mismatch aborts the install.
  - It is re-verified at every load. A mismatch sets `status=disabled_tampered` and alerts the owner once (E9).
  - A tracked symlink whose target resolves outside the extension root is refused.
- **Stdlib only.** `ext validate` refuses a `pyproject.toml` with dependencies, `requirements*.txt`, `uv.lock`, `package.json` or `setup.py`. There are no venvs and no `uv sync`, so no build-time code ever runs.
- **Interpreter.** A `run` starting with `python3` or `./*.py` runs as `[<daemon interpreter>, -I, -S, <script>, …]`. The shebang is ignored. `-I` isolates from user site and env vars; `-S` skips `site`.
- **argv[0] allowlist.** `argv[0]` must resolve inside the extension dir, or be one of `gh`, `git`, `python3` (the daemon interpreter). Fetch-runners (`npx`, `bunx`, `pipx`, `uvx`, `uv`, `docker`, `curl \| sh` shapes) are refused at add time. The resolved absolute path is pinned in the row.
- **Registry of record:** an `extensions` table (migration 004), not `extensions.yaml` (C2).
  - Columns: `name, source, commit, tree_digest, manifest_sha, manifest_json` (the approved snapshot), `argv0_paths, status (enabled | disabled | disabled_tampered), installed_at, installed_by`.
- **Leftovers.** If Keychain items `resonant/<name>/*` or `ext-data/<name>/` exist and the previous row had a different `source`, `ext add` says so and requires `--adopt`. A new source doesn't silently inherit old secrets.
- **Data.** `~/.resonant/ext-data/<name>/` is writable state and is also the child's `HOME`; `<data>/tmp` is its `TMPDIR`.

### D2. `resonant ext add | update | remove | list | validate` (terminal only)
- Install, update and remove happen **only in the Mini's terminal** (`owner:cli`). iMessage gets read and operate commands only (D14). The iMessage install flow is deferred (§8).
- **The confirmation binds what runs.** `stage()` computes `{source, commit, tree_digest, manifest_sha}` and prints:
  1. A heading: `<name> <source>@<sha8> digest <8>`.
  2. **Escalations first:** secrets, approvers, L1/L2 tools, tools whose `argv0` is `gh`/`git` (network-using).
  3. Then every tool (effect, level), every job (schedule in words, effect, level, `catchup`) and the notify events.

  Every manifest string goes through `sanitize_external`. The owner types the extension name to confirm. `install()` re-checks that the four bound values are unchanged before and after the copy, writes the row, and audits `ext.install {source, commit, tree_digest, manifest_sha, path="cli"}`.
- **Update** is a full re-approval every time (`code only` when the manifest is unchanged). That replaces `extensions.md` §9's "only escalations" rule (C1).
- **Remove** deletes the row and the copy, and keeps the data dir (`--purge` deletes it). It lists the Keychain items left (`resonant secret rm …`).
- **Advisory router check.** `ext add` runs `resonant eval router` on the example set with the new intent included, and prints builtin accuracy before and after. A drop is printed as a warning; it doesn't block.
- **Restart.** Install, update and remove restart the daemon through the existing reply-first path, if it's running. Everything is materialized once at start, so there's no hot reload.

### D3. Manifest → ToolSpec
- `Manifest.tool_specs()` stays the only source of effect, level, `commit`, secrets and timeout. New, additive (C1):
  - `args_schema` gets `additionalProperties: false`.
  - Tool `timeout_s` ≤ 300; job `timeout_s` ≤ 1800.
  - `secrets` entries match `^[a-z][a-z0-9_]{0,31}$`.
  - Top-level `description` (≤ 120 chars, the intent definition) and `examples` (≤ 3 lines ≤ 80 chars, few-shots).
  - The extension name may not equal a builtin command word or `core`.
  - A tool name may not collide with a job's spec name.
- **Jobs** gain:
  - `effect` (default `write`, so L1, the safe default) and `level`.
  - `secrets` and `timeout_s`.
  - `catchup` (`none` or a duration ≤ 24h, default `none`).
  - Cadence rules: fires no more often than every 5 minutes, and **no more than hourly when the effective level asks** (L1/L2).

  Each job becomes `ToolSpec(name=f"job_{id}"` (underscored), args `{due_at: string}`, `description="scheduled job")`. Job specs never enter a model toolset or a member's commands.
- **Step limit** (C10). `local_step_limit` counts builtin tools plus extension **tool** timeouts (≤ 300), never jobs. A test pins that a 1800 s job leaves it unchanged.

### D4. Process runner (`extensions/process.py`)
- **argv.** `run` is `shlex.split` once at load. Placeholders are substituted *inside* tokens, so a value never adds tokens.
  - Strings: no NUL, at most 1000 chars.
  - A value filling a **whole token** may not start with `-`. Authors who need that write `--tz={tz}`.
- **Environment, built from scratch** (no `os.environ`):

  | Var | Value |
  |---|---|
  | `PATH` | `/opt/homebrew/bin:/usr/bin:/bin` |
  | `HOME` | the data dir |
  | `TMPDIR` | `<data>/tmp` |
  | `LANG` | `en_US.UTF-8` |
  | `USER`, `LOGNAME` | the user |
  | `RESONANT_EXT`, `RESONANT_EXT_DATA` | the name and data dir |
  | `RESONANT_JOB_DUE_AT` | jobs only |
  | `RESONANT_SECRET_<NAME>` | **only this tool's or job's declared secrets** |
  | `GIT_TERMINAL_PROMPT=0` | always |

- **Process.**
  - cwd is the extension copy. stdin is `/dev/null`. `start_new_session=True`.
  - Every live child is tracked in a `ProcessTracker`: its pgid, plus its descendants from `psutil` sampled every 2 s, which catches children that `setsid` out of the group.
  - On timeout, cancel or kill switch: `killpg` SIGTERM, plus SIGTERM to the tracked tree, then SIGKILL after 2 s.
- **Output caps.**
  - stdout is read in bounded chunks to 256 KB; past that everything is killed and the result is ambiguous for writes, like any kill (`ExtAmbiguous("output too large")`, P3-4).
  - stderr is capped at 8 KB, redacted, and only a 200-char tail goes to the failure audit row. It is never logged or shown.
- **Result contract** (`extensions.md`):
  - Exit 0 with exactly one JSON object means success.
  - A non-zero exit means `ToolFailed`: the author promises **nothing took effect**.
  - A timeout or kill is ambiguous for writes (the executor's existing rule).
  - Results are redacted (D5) and stored up to 32 KB; the model sees 4 KB as untrusted data.
- **Events.** A run may return `{"events": [{type, text, dedupe_key, data?}]}`, or use the SDK's `emit()`, which writes JSON lines to fd 3 (`RESONANT_EVENTS_FD=3`, same caps). `dedupe_key` is required, at most 200 chars.

### D5. Keychain secrets, injection, redaction
- **`SecretStore`** (`resonant/secrets.py`) over `/usr/bin/security find-generic-password -s resonant -a <ext>/<name> -w`: argv, 5 s timeout, the value read from stdout, never in argv. `exists`, `set` and `delete` work the same way.
  - Refs are slug-validated.
  - The daemon is a GUI-session LaunchAgent with an unlocked login keychain. Items created by `/usr/bin/security` trust that binary, so there's no prompt (verified live, [§7](#7-rollout)).
  - Python `keyring` isn't used: its ACL follows the interpreter binary.
- **Local entry only.**
  - `resonant secret set <ext>/<name>` reads the value with `getpass` and passes it to `security add-generic-password -U` on stdin (an interactive-prompt path), never argv. Values shorter than 8 chars are refused.
  - `resonant secret list` shows declared versus present (names only), and `resonant secret check <ref>` does a no-prompt read test.
  - There is **no iMessage path**: chat.db and iCloud would keep the value.
- **Injection** (C3).
  - `Executor(…, secrets=SecretStore)` reads `spec.secrets` just before `_run` and passes `CallContext(secrets, idempotency_key, task_id)` to handlers marked `wants_context`. Builtins are unchanged.
  - Env names are `RESONANT_SECRET_<UPPER(slug)>`.
  - A missing secret is `ToolFailed`, raised **before** the `started` row, so it's definite.
  - L0 jobs (no executor, D6) get secrets through the same `SecretStore` call inside the job host. In the daemon, only those two code paths read the Keychain.
- **Redaction**, done twice:
  1. The process runner rewrites stdout and stderr before JSON parsing.
  2. The executor and job host deep-redact the result, the error, the events and the audit fields before storing anything.

  Forms covered, for each value: raw, URL-encoded, JSON-string-escaped, and base64 at all three alignments (the value prefixed with 0, 1 or 2 bytes, the stable interior of each encoding matched, so a value embedded in a larger base64 blob is still caught).

### D6. Scheduler and job execution (`extensions/scheduler.py`, `runners/job.py`)
- **Engine: `cronsim`** (zoneinfo; Debian-cron DST semantics: a spring-forward gap runs right after the jump, a fall-back fold runs once; `*`-hour jobs follow wall time), wrapped by `extensions/cron.py`. A table test pins `America/Los_Angeles`.
- **State:** `ext_jobs(ext, job_id, last_due, last_run_at, last_outcome, open_task_id, fail_streak)`. Core jobs use `ext="core"`.
- **Tick** (`Scheduler.tick(now)`, driven by the loop. `Loop.wait_s` also counts the next due time, so a job runs within `tick_s` of its due time on the fake clock, and within loop precision live). Precedence, in order:
  1. **Pause wins.** If `paused`, advance `last_due` past every due time, audit `job.skipped_paused`, and never catch up.
  2. **Catch-up.** Of the due times in `(last_due, now]`, keep only the latest. It runs if it's the current minute; or if it's within `catchup` **and** the daemon wasn't running at that time (the `daemon.started_at` and last-tick kv rows show the gap). Every other one is audited `job.missed`.
  3. **Overlap.** If the job's previous run is still running, or its task is waiting, skip it and audit `job.overlap`.
- **Execution** by effective level (origin `scheduled`, principal = the owner, approvers per D8):
  - **L0:** no task row. The scheduler runs it inline as a bounded background coroutine (at most `extensions.max_concurrent_jobs`, default 4), gated with `task_id="job:<ext>/<id>:<due>"` like the fast path's `route:` ids. The gate audits it; there's no executor row, since a read has nothing to dedupe. Success writes `ext_jobs` plus a `job.run` audit row, and events are persisted (D10). This keeps `/projects` clean.
  - **L3 / L2 / L1:** `insert_task(kind="job", runner="job", extension, priority=PRIORITY_BACKGROUND, wake_at=due, checkpoint={job, due_at, late})`, with `last_due` in the same transaction. `JobRunner` (no model) pins the call `job_<id> {due_at}` → `recorded()` → `gate.evaluate(origin="scheduled")`:
    - `Allow`: execute; L3 reports.
    - `NeedsApproval`: prompts (D8), then `Wait("approval")`.
    - `Deny`: audited, reported once a day.
- **Reports with no requester.** These go to the job's recipients (D8). Code-built: `example/cleanup: done` plus the run's `summary` field, sanitized, ≤ 120 chars. Definite failures are reported at most once per job per local day (`dedupe jobfail:<ext>/<id>:<date>`). `fail_streak` feeds health (D11).
- **Kill switch.** `killswitch.engage` (in the daemon, via `/kill`) calls the scheduler's `on_pause` hook at once. A CLI `resonant kill` is noticed on the next tick (≤ `tick_s`). Either way, every tracked extension process is killed (D4); an L0 run is audited `job.killed`, and a gated one is ambiguous for writes as usual. Notify holds while paused (D10).
- **Core jobs.** `scheduler.CORE_JOBS` is a tuple, like `BUILTIN_MODULES`. The briefing (D12) is the first entry.

### D7. Origin `scheduled` (C4)
- `Origin` becomes `explicit | inferred | scheduled`. Only the scheduler and `JobRunner` set `scheduled`, and it isn't hashed. `rule` treats `scheduled` like `inferred`: it is **never explicit**.

| Level | explicit | inferred / scheduled | autonomy off |
|---|---|---|---|
| L0 | Allow | Allow | Allow |
| L3 | Allow + report | Allow + report | ask |
| L2 | Allow + report | ask | ask |
| L1 | ask code | ask code | ask code |

- Veto windows are deferred (§8), so `evaluate` still never returns `Veto`.
- `/ext run <ext>/<job>` is an owner's explicit `direct_call` of the job spec. It's gated at the job's own level, so an explicit L2 runs.

### D8. Approvers and recipients
- **Extension approvers:** `(owner,) ∪ {p ∈ manifest.approvers : principals.can(p, "approve", ext)}`.
  - Grants live **only** in `principals.yaml`; a manifest naming a principal without the grant is refused at add (`approver_errors`) and re-checked at load (a lost grant is dropped and logged).
  - An extension can't grant itself approvers.
- **Sending prompts** (`runners/approve.py`, used by both `LocalRunner` and `JobRunner`): send to every approver with an iMessage identity; skip the rest with audit `approval.unreachable`. Fail (final) only if **none** was reachable. This replaces today's fail-on-any `ChannelRefused`.
- **Recipients** of job reports and notifications are the same reachable approvers, falling back to the owner.

### D9. Router: extension intents, toolsets, commands (C6)
- **Intents.** `IntentLabel` becomes `str`, validated against `intents(enabled extensions)`, built **once at start**.
  - `extension:<name>` takes its definition from the manifest `description` (sanitized, ≤ 120 chars, shown in the install diff) and up to 3 few-shots from `examples`, sorted by name, after the builtins.
  - `label_prefix(extensions)` is byte-identical between installs (KV-cache reuse), and every change goes with a restart.
  - Snapshot tests pin the builtin-only prefix (unchanged hash) and a with-`example` prefix.
- **Toolsets.** `extension:<name>` gets its manifest tools (not jobs), filtered by `can(principal, "request", ext)`. `ext add` warns above 12 tools.
  - The checkpoint carries `toolset: [{extension, name}]`. Old name-only checkpoints still resolve to builtins.
  - `_toolset`, `_direct` and `_is_explicit` honor `extension`.
- **History.** In `_context`, a prior reply now goes in as a user turn wrapped `<prior_reply>…</prior_reply>` (untrusted), not as a raw assistant turn, for every intent. Replies can carry extension output. The model contract is unchanged; the runner snapshot changes deliberately.
- **Commands.** `commands_for(registry)` = `COMMANDS` + one `Command` per enabled extension: `/<ext> <tool> [args]`.
  - A single-arg tool takes the rest of the text. Otherwise `key=value` pairs, typed per the manifest.
  - Unknown tool words get a `difflib` suggestion and never execute.
  - Reads run inline. Side effects become `direct_call {tool, extension, args}`, which is explicit.
  - `/help` lists `/<ext> <tool>` with the description and level, and `/help <ext>` filters to one extension.
- **`/ext`** (builtin tools in `tools/ext.py`, owner-only):

  | Command | Tool | Level |
  |---|---|---|
  | `/ext list` | `list_extensions` | L0 |
  | `/ext run <ext>/<job>` | the job spec itself | the job's level, explicit |
  | `/ext mute <ext> [1h \| until tomorrow \| off]` | `mute_extension` | L3, kv `ext.<name>.muted_until` |

  `core/briefing` is runnable through `/ext run`.
- **Evals.** `examples` join the router eval at add time (D2, advisory).

### D10. Notify: exactly once, from persisted events
- **Persist first.** A run's events become `Event(source=f"ext:{name}", type=f"ext.{name}.{type}", principal=None, task_id=None, dedupe_key=f"ext:{name}:{dedupe_key}", payload={text, data})`. They are inserted with `insert_event`, where the UNIQUE `dedupe_key` drops repeats. This happens **after** the run's result is recorded, and in the same transaction as `ext_jobs` for L0 runs.
- **Deliver from the store.** Unrouted `ext:*` events reach the loop's router hook, which `wiring` dispatches (`route_event`) to `Notifier.route(event)`. Router logic is unchanged.
  - A type in `notify.on`, not muted and not paused: `channel.send(recipient, "[<name>] <text>", dedupe_key=<event dedupe_key>)`, then return None (consumed).
  - A send error raises, so the loop retries every tick and dead-letters after 5, as for any event.
  - A crash between persist and send leaves the event unrouted, and it's delivered on start. A crash after the send but before consumption is caught by the channel's dedupe. So delivery is exactly once (E12).
  - Paused or muted: consumed without sending (audit `ext.notify_held`). There's no backlog flood on `/resume`.
- **Anti-spoofing.** The text goes through `clean_external` and is clipped to 300 chars. Code-like `[A7]` becomes `(A7)`, and a leading `yes`/`no`/`reply`/`stop` is neutralized, so an extension can't forge an approval prompt.
- **Caps.** 20 notifications per extension per hour; beyond that they collapse into one `[github] +N more updates` at the end of the hour.
- **First run after install** persists events but marks them `payload.baseline=true`, and they aren't sent. No flood from pre-existing state.

### D11. Extension health
- `health: {check, every}` runs through the process runner (20 s); exit 0 means ok. `HealthMonitor` takes dynamic checks `ext:<name>` (C9), fed by:
  - the health command
  - the job `fail_streak` (3 or more)
  - load refusals (tamper, a missing `argv0`)
- Alerts use the existing machinery: one per incident per 6 h, then `resolved: …`. They show in `/api/status.health`, `resonant status` and `/ext list`.

### D12. Core daily briefing (`core/briefing`)
- **Schedule.** `briefing: {enabled: true, at: "08:00"}` in `settings.timezone`, daily (C11). A core L0 job with no model, sent to the owner.
- **Content**, code-built from builtin reads, one message ≤ 600 chars. Each line is omitted when empty, and `Nothing on today.` is sent when all are:
  - `⏰ 2 reminders: 9:30am stretch, 3:00pm call mom` (`list_reminders`, today)
  - `▶ 1 running, 2 waiting (approval)` (`projects`)
  - `✋ 1 approval pending (A7)` (`gate.pending_for`)
  - `⚠ ext:github degraded` (`health_snapshot`)
  - `github: 3 open PRs, #41 ✗ lint 2d` (the text of today's latest `ext.github.digest` event, sanitized; github schedules `digest` at 07:50)
- Sent with `dedupe_key=briefing:<local date>`. Skipped while paused. Catch-up: 3h.
- This is the seed for [proactive nudges](followups.md#proactive-nudges-btw-). Ranking and model use are later.

### D13. Interim trust bar (stated plainly)
- Phase 3 extensions are **first-party, owner-installed code** from this repo or a local path. They run **as the same macOS user** and **inherit the daemon's Full Disk Access**. They can read `~/.resonant/resonant.db`, chat.db and the Keychain via `security` like any same-user process ([trust model](architecture.md#trust-model)).
- Phase 3's protections are **env and secret isolation**, not filesystem isolation:
  - an environment built from scratch
  - `HOME`/`TMPDIR` in the data dir
  - per-tool secret injection and redaction
  - process-group plus tracked-tree kill
  - output caps
  - an argv0 allowlist
  - a pinned, copied, read-only tree
- **No third-party sources** until the Seatbelt sandbox (§8) and network sources land together.

### D14. First real extension: `github` (in-repo, `extensions/github`)
- Stdlib Python plus `gh`. A fine-grained PAT (`github/token`, **read-only scopes** first: metadata, contents, pull requests, checks/actions read) is passed as `GH_TOKEN` to that process only. It isn't `gh`'s own login.
- **Ticket A: read and notify.**
  - `prs` (L0): open PRs authored by the owner, with CI rollup and review state.
  - `pr {repo, number}` (L0).
  - Job `watch` (`*/10 * * * *`, L0), **stateless**. From GitHub's current state, per open PR:
    - The latest run on the head failed: event `ci_failed`, key `<repo>#<n>:run:<run id>:failed`.
    - The latest succeeded and the previous run on the branch failed: `ci_fixed`, key `…:run:<run id>:fixed`.
    - The owner was requested as a reviewer: `review_requested`, key `<repo>#<n>:<head sha>:review`.

    The events table's UNIQUE key is the only memory, so there's no state file.
  - Job `digest` (`50 7 * * *`, L0): one event with the summary line for the briefing (not in `notify.on`).
  - `notify.on: [ci_failed, ci_fixed, review_requested]`.
  - `health`: `gh api user`, every 30m.
- **Ticket B: writes** (needs PAT write scopes: actions, pull requests).
  - `rerun_failed {repo, number}` (L3).
  - `merge_pr {repo, number, head_sha}` (L1, `commit`: always `yes <code>`). The approval binds to that head commit.

---

## Contract changes (flagged)

| # | Contract | Change | Ticket |
|---|---|---|---|
| C1 | Manifest v1 (additive) and `extensions.md` | Job `effect`/`level`/`secrets`/`timeout_s`/`catchup`; cadence rules; top-level `description`/`examples`; slug `secrets`; `additionalProperties: false`; timeout caps; name rules; stdlib-only and argv0 allowlist in `ext validate`; result contract (non-zero exit means nothing took effect, fd-3 events); every update re-approved; terminal-only install | P3-1 |
| C2 | Store | Migration 004: `extensions`, `ext_jobs`. Records live in the DB, not `extensions.yaml`. | P3-2 |
| C3 | Executor | `secrets`, `CallContext` for `wants_context` handlers, definite missing-secret `ToolFailed`, redaction before `executions.result` and audit | P3-6 |
| C4 | Gate | `Origin` adds `scheduled` (never explicit); approvers from manifest ∩ grants; the reachable-approver send rule. No `Veto` yet. `architecture.md`/`.html` level tables. | P3-8 |
| C5 | Events | Sources `scheduler`, `ext:<name>`; type `ext.<name>.<type>`; core-set principal; the router hook dispatches `ext:*` to `Notifier`; SDK fd-3 `emit()` | P3-10, P3-12 |
| C6 | Router / local runner | Dynamic intents and `label_prefix(extensions)`; toolset entries carry `extension`; `direct_call.extension`; `<prior_reply>` wrapping; `commands_for(registry)`; `/<ext>`, `/ext list\|run\|mute` | P3-11, P3-14 |
| C7 | Runners / loop | `runner="job"`; L0 jobs make no task rows; `Loop.wait_s` counts the next job due; `killswitch.engage` pause hook | P3-10 |
| C8 | Builtin tools | `list_extensions` (L0), `mute_extension` (L3) | P3-14 |
| C9 | Health | Dynamic `ext:<name>` checks | P3-12 |
| C10 | Components / wiring | `Components.extensions: ExtensionHost`; handlers merged at start; `local_step_limit` counts extension tool timeouts ≤ 300 and excludes jobs | P3-6 |
| C11 | Config | `extensions: {max_concurrent_jobs: 4}`, `briefing: {enabled, at}`: all new sections land in P3-2 | P3-2 |

---

## 4. Tickets

**Hard serialization** (same-file tickets run in this order):
- `config.py`: P3-2 only
- `resonant_sdk/manifest.py`: P3-1 only
- `store/migrations`, `store/extensions.py`: P3-2 only
- `store/approvals.py`: none (unchanged)
- `cli.py`: P3-3 → P3-7
- `extensions/process.py`: P3-4 → P3-6 → P3-10
- `executor.py`: P3-6 only
- `extensions/host.py`: P3-6 → P3-10 → P3-12
- `components.py` / `wiring.py` / `daemon.py`: P3-6 → P3-10 → P3-12
- `gate/*`: P3-8 only
- `runners/local.py`: P3-8 → P3-11
- `router/*`: P3-11 → P3-14
- `monitor.py`, `killswitch.py`: P3-12 and P3-10 respectively
- `extensions/github/*`: P3-9 → P3-15
- `pyproject.toml` / `uv.lock`: P3-5 only

`scheduler.CORE_JOBS` and `tools/builtin.BUILTIN_MODULES` take one-line additions; rebase as needed. From P3-6 on, each ticket adds its own cases to `tests/test_e2e_phase3.py`.

| ID | Title | Deps | Size | Wave |
|---|---|---|---|---|
| P3-1 | SDK manifest additions + `ext validate` rules (stdlib only, argv0 allowlist, cadence, names) | – | M | 1 |
| P3-2 | All new config sections + migration 004 + `store/extensions.py` | – | M | 1 |
| P3-3 | `SecretStore` (Keychain), redaction (all encodings), `resonant secret set\|list\|check\|rm` | – | M | 1 |
| P3-4 | Process runner: argv templating, env from scratch, caps, `ProcessTracker` kill, JSON + fd-3 events | – | M | 1 |
| P3-5 | Cron engine on `cronsim`: due times, catch-up precedence, DST table | – | S | 1 |
| P3-6 | `ExtensionHost`: load rows, verify pins, register CLI tools and job specs; executor secrets + redaction; step limit | 1, 2, 3, 4 | L | 2 |
| P3-7 | Extension manager + terminal `resonant ext add\|update\|remove\|list` (copy, pins, `--adopt`, advisory eval) | 1, 2, 3 | L | 2 |
| P3-8 | Gate: origin `scheduled`, manifest approvers ∩ grants, reachable-approver sends | 2 | M | 2 |
| P3-9 | `extensions/github` A: `prs`, `pr`, stateless `watch`, `digest`, health; fake-`gh` tests | 1 | M | 2 |
| P3-10 | Scheduler + `JobRunner`: tick precedence, L0 inline, gated tasks, reports, kill-switch kills | 4, 5, 6, 8 | L | 3 |
| P3-11 | Router: `extension:<name>` intents, `label_prefix`, toolsets, `<prior_reply>` wrapping, local runner | 1, 6, 8 | L | 3 |
| P3-15 | `extensions/github` B: `rerun_failed` (L3), `merge_pr` (L1) | 9 | S | 3 |
| P3-12 | Notifier from persisted events (exactly once, mute, caps, baseline) + `ext:<name>` health | 9, 10 | L | 4 |
| P3-13 | Core daily briefing (`core/briefing`) | 10 | M | 4 |
| P3-14 | `/<ext> <tool>` commands, `/help <ext>`, `/ext list\|run\|mute` | 10, 11 | M | 4 |
| P3-16 | Remaining e2e, go-live step 12, `--check` rows, docs, decisions log, follow-ups | all | M | 5 |

### Ticket notes
- **P3-1**
  - **Files:** `resonant_sdk/manifest.py`, `extensions/checks.py` (`dir_errors`: stdlib-only, argv0, symlinks), `cli.py` is **not** touched (P3-7 wires `dir_errors` into `ext validate`), `tests/test_sdk.py`, `extensions/example/*` (`morning-ping: effect: read`; notes to `$RESONANT_EXT_DATA`), `docs/extensions.md`.
  - **Tests:** a job without effect is L1; L2 `*/30` is refused; `npx` and `uv run` are refused; a symlink out of the root is refused; `pyproject` with deps is refused; the example passes.
- **P3-2**
  - **Files:** `config.py`, `store/migrations/004_extensions.sql`, `store/extensions.py`.
  - **Tests:** migration over 003; row CRUD and status; `ext_jobs` upsert; config defaults and bounds; `job-guard`'s schema check still fails closed.
- **P3-3**
  - **Files:** `resonant/secrets.py`, `cli.py`.
  - **Tests:** the value never in argv (a recorded runner); min length; not found; the redaction corpus (raw, URL, JSON-escaped, base64 at offsets 0/1/2 inside larger blobs, nested, overlapping values); `list` shows names only.
- **P3-4**
  - **Files:** `extensions/process.py` (`ExtCommand.compile`, `render`, `ExtProcess.run`, `ProcessTracker`, `FakeExtProcess`).
  - **Tests:** a substitution table; `-`-led values; a scratch-env sentinel (no `os.environ` leak); the 256 KB cap kills; a `setsid` grandchild is killed via the tracked tree; non-zero means `ToolFailed`; fd-3 events capped; stderr never returned.
- **P3-5**
  - **Files:** `extensions/cron.py`, `pyproject` (`cronsim`).
  - **Tests:** the LA DST table; coalescing; pause beats catch-up; `catchup` window versus "daemon was down".
- **P3-6**
  - **Files:** `extensions/host.py`, `extensions/handlers.py`, `executor.py`, `components.py`, `wiring.py`.
  - **Tests:** pin mismatch disables the extension and alerts once; registry and `/api/tools` show specs; the E7 env-dump and sentinel; missing secret has no `started` row; the step limit ignores a 1800 s job.
- **P3-7**
  - **Files:** `extensions/manager.py`, `extensions/sources.py`, `cli.py`.
  - **Tests:** `repo:` uses `git archive` of HEAD (a working-tree edit isn't copied); the digest is re-verified after the copy; the four bound values change between confirm and install → abort; `--adopt`; remove keeps data; `--purge`; the escalations-first output is sanitized.
- **P3-8**
  - **Files:** `gate/types.py`, `gate/policy.py`, `runners/approve.py`, `runners/local.py` (use the helper).
  - **Tests:** the D7 table × autonomy; `scheduled` never allows L2; approvers ∩ grants; one unreachable approver is skipped; none reachable fails.
- **P3-9**
  - **Files:** `extensions/github/{resonant.yaml,cli.py,README.md}`, `tests/test_ext_github.py`, `tests/fixtures/fake_gh`.
  - **Tests:** JSON shapes; `watch` keys are deterministic per run id; `fixed` only after a failed previous run; `GH_TOKEN` only from `RESONANT_SECRET_TOKEN`; `ext validate` passes.
- **P3-10**
  - **Files:** `extensions/scheduler.py`, `runners/job.py`, `extensions/process.py` (pause kill), `killswitch.py` (hook), `loop/loop.py` (`wait_s`), `wiring.py`, `resonant_sdk/emit.py`.
  - **Tests:** E4 (within `tick_s`), E5, E6, E8, E11; one run per due time across a double tick and a restart; the concurrency cap.
- **P3-11**
  - **Files:** `router/intents.py`, `router/prefixes.py`, `router/labeler.py`, `router/router.py`, `runners/local.py`, snapshots, `evals/`.
  - **Tests:** E3; the builtin prefix hash is unchanged; a member without the grant gets no toolset; old checkpoints still work; prior replies are wrapped.
- **P3-15**
  - **Files:** `extensions/github/*`.
  - **Tests:** argv shapes; `merge_pr` is `commit`/L1; rerun is L3.
- **P3-12**
  - **Files:** `extensions/notify.py`, `extensions/host.py`, `monitor.py`, `wiring.py` (`route_event`).
  - **Tests:** E12 with a crash injected between persist and send, and between send and consume; mute; paused holds; the anti-spoof rewrite; the rate-cap collapse; baseline suppression; health alerts once and resolves.
- **P3-13**
  - **Files:** `extensions/briefing.py`, `scheduler.CORE_JOBS` (one line), `router/render.py`-style template.
  - **Tests:** E13; empty sections are omitted; the github line from today's digest only, sanitized; dedupe per date; skipped while paused.
- **P3-14**
  - **Files:** `router/commands.py`, `router/fast_path.py`, `tools/ext.py`, `tools/builtin.py` (one line).
  - **Tests:** E2, E14; typed args; suggestions never execute; a member can't see `job_*` or `/ext`.
- **P3-16**
  - E1, E9, E10, E15 (live); `docs/go-live.md` step 12; `scripts/go_live_check.py`; `architecture.md` (components, trust model incl. D13, decisions log) and `.html` roadmap; `extensions.md`; `followups.md` (close Keychain injection and L3 job reports; add the §8 entries).

---

## 5. Risks and security considerations

| Risk | Mitigation and residual |
|---|---|
| **Extension code runs as the same user with Full Disk Access** | Accepted for Phase 3: only first-party code, owner-installed from the terminal, copied, pinned and re-verified at load (D1, D13). **Residual:** an extension can read the DB, chat.db, other extensions' data, and the Keychain via `security`. Same-user code is outside the gate's threat model. The Seatbelt follow-up closes this before any third-party source. |
| **Keychain ACL** | Items trust `/usr/bin/security`, so **any same-user caller** can read every `resonant/*` item, including the owner's own Claude Code sessions and any extension. **Residual**, accepted; it's documented in the trust model, and the PAT is scoped minimally (read-only first). |
| **Code changes under an approved extension** | It runs from a read-only copy, never the checkout; the digest is re-verified at load; every update re-confirms with a diff (D1, D2). |
| **Install-time code execution** | Stdlib only: no deps, venvs or builds. Fetch-runners are refused; `argv0` is allowlisted and pinned. |
| **Approval doesn't match what runs** | The confirm binds `{source, commit, tree_digest, manifest_sha}`, which is re-verified after the copy and stored with the audit row. |
| **Leftover secrets reused by a new source** | `--adopt` is required (D1). |
| **Secret leakage** | Per-tool env only; values never in argv; no iMessage entry path; redaction (raw, URL, JSON-escaped, 3 base64 alignments) before parsing and before every sink; sentinel tests. **Residual:** an extension can send its own secrets over the network, or print them in transformed forms we don't match. |
| **Prompt injection via tool output and history** | Output is capped and labeled untrusted; prior replies are wrapped (D9); one gated side effect per task; inferred L2/L1 ask. Jobs have no model. **Residual:** an injected read can steer one L3 in the same chat (as in Phase 2). |
| **Notify spoofing an approval prompt** | `[ext]` prefix, code rewrite, neutralized leading reply words; binding still needs sent-before and a single pending approval (D10). |
| **argv injection** | Split once, substitute inside tokens, refuse `-`-led whole-token values (D4). |
| **Runaway jobs, storms, floods** | ≥ 5 min cadence (hourly when it asks); coalesced catch-up; pause beats catch-up; overlap skip; concurrency cap 4; timeouts with group plus tree kill; 20 notifications/h; failures reported once a day; baseline run silent. |
| **Duplicate or lost notifications** | Persist then deliver through the router hook; the UNIQUE event key plus the channel dedupe give exactly once across crashes (E12). |
| **Kill switch coverage** | `paused` skips jobs (no catch-up), holds notifications and the briefing, and kills in-flight extension processes; `autonomy off` makes every tool and job above L0 ask. **Residual:** a CLI `resonant kill` takes effect within one tick. |
| **Unattended L2** | Asks (no veto yet); asking jobs are at most hourly, so prompts can't pile up. |
| **Label accuracy drop** | Advisory eval at add time; a warning above 12 tools. |
| **DB growth** | L0 jobs create no task rows; audit and event retention is a follow-up. |

---

## 6. Owner questions (defaults recommended)

| # | Question | Default |
|---|---|---|
| Q1 | Briefing: time, days, sections | 08:00 every day; reminders, work, approvals, health, github |
| Q2 | Watch cadence and quiet hours | `*/10`, no quiet hours in Phase 3 (mute covers it) |
| Q3 | `path:` sources outside this repo | Allowed (terminal only, git work tree required) |
| Q4 | When the github write ticket (P3-15) goes live | Ship the code; widen the PAT scopes only when the owner wants `merge_pr` |

---

## 7. Rollout

### Go-live step 12

Deploy with `/update`. The log shows `extensions=0`, and `bootstrap.sh --check` passes with schema 4.

**In the terminal on the Mini:**
1. `resonant ext add repo:example`: check the heading and the escalations-first list, type `example`. `Back up …` plus `Installed example` arrive over iMessage (E1).
2. `resonant secret set github/token` (fine-grained PAT, read-only). `resonant secret check github/token` gives `ok (no prompt)`. `resonant secret list` shows it as present.
3. `resonant ext add repo:github`, type `github`.

**Over iMessage:**
1. `/ext list` shows both extensions, enabled, with pins and health ok (E14).
2. `/example get_time Asia/Tokyo`, `/example write_note hi`, "what time is it in tokyo" (E2, E3).
3. `/github prs` lists PRs. On the Mini, `grep` the logs and `sqlite3` the audit table for the token's first 6 characters: both find 0 (E7).
4. `/ext run github/watch` runs silently the first time (baseline). Push a failing commit to a scratch PR: `[github] ✗ … CI failed` within 10 min, once. Fix it: `✓ … CI fixed` (E15).
5. `/ext mute github 1h`, then break CI again: nothing arrives. `/ext mute github off`.
6. `/ext run core/briefing` sends the briefing; the next morning it arrives at 08:00 (E13).
7. `/kill`: the next `watch` is skipped (`resonant status` and the audit show `job.skipped_paused`). `/resume`: no catch-up run (E8).
8. Edit `~/.resonant/extensions/example/cli.py` (`chmod u+w` first), then `/restart resonant`. The owner gets `example changed on disk…; disabled` (E9). `resonant ext update example` restores it.

### `scripts/bootstrap.sh --check` (new rows)

As built (P3-16), the rows are `extensions dir`, `extension <name> pin` (advisory; covers the copy and its pinned `argv0` paths), `secret <ext>/<name>` (existence only; required for enabled extensions), `extension jobs scheduled` and `briefing scheduled`, with next due times computed from the manifests and config (no `ext_jobs` row needed). A failed `cronsim` import fails those two rows. The no-prompt read and `gh` auth are checked live in go-live step 12 instead. The table below is the original plan.

| Check | Required | Pass when |
|---|---|---|
| store schema 4 | yes | `open_existing` succeeds |
| extension pins | yes | Every `enabled` row's copy re-verifies (`tree_digest`, read-only, no escaping symlinks) |
| extension `argv0` | yes | Every pinned `argv0` path exists (e.g. `/opt/homebrew/bin/gh`) |
| declared secrets present | yes for enabled extensions | `security find-generic-password` finds each `resonant/<ext>/<name>` (names only, no value read) |
| secret readable without a prompt | advisory | `resonant secret check` for one declared secret returns within 5 s |
| `gh` authenticated | advisory (github enabled) | `ext:github` health ok |
| jobs scheduled | yes | Each enabled job has an `ext_jobs` row with `next due` in the future; `core/briefing` too when `briefing.enabled` |
| briefing config | yes | `briefing.at` parses; `settings.timezone` loads |
| kill-switch hook | advisory | No extension processes are tracked while `paused` |
| `cronsim` importable | yes | Import succeeds |

---

## 8. Out of scope and deferred

**Deferred to `followups.md`** (P3-16 adds each entry with its design):
- **MCP stdio client.** No Phase 3 extension uses it. Future design:
  - Only manifest-listed tools, with manifest descriptions; schema descriptions stripped; annotations ignored.
  - Per-tool `sha256(name, description, input_schema, output_schema, annotations)` pins, checked at start and on `list_changed`; a change disables the tool and alerts.
  - The server's stderr goes to a redacting sink, never a raw log.
  - `npx`/`uvx`-style unpinned fetch-runners are refused.
  - The server env is the union of its tools' secrets.
  - The group is killed on stop.
- **Critical-job LaunchAgents and a `job-guard` rewrite.** Their only consumer, trade-jev, is deferred. Carried notes:
  - Critical jobs must be Allow-only (L0, or L3 with a report).
  - `job-guard` stays the **parent** (no exec) so it can pipe output through redaction.
  - It checks `autonomy` and `paused`, the extension row, the job and the pin.
  - The command comes from the approved manifest, never the plist.
- **Veto windows.** Asking jobs are rare and at most hourly in Phase 3. Carried design:
  - Origin `scheduled` L2 → `Veto`; `approvals.kind=veto`.
  - A `stop` beats deadline expiry: a `stop` whose `occurred_at` ≤ the deadline wins even if it's ingested after it (reader watermark plus no unrouted earlier events), and within a short late-stop grace after the deadline, if the call hasn't started.
  - An ambiguous `stop` (several vetoes pending) converts them all to asks.
  - A veto may run only if its prompt was recorded as sent at least the window before.
  - Late (daemon down) or under `/kill`, it converts to an ask.
- **Seatbelt sandbox for extension processes.** It needs its own review. Deny-default design:
  - Deny `lsopen`, `launchctl`, `open`, `shortcuts`.
  - Deny `mach-lookup` of `securityd`/`com.apple.SecurityServer`.
  - Deny `process-info` and `signal` on other processes.
  - Deny loopback (including Ollama `:11434` and the API `:7777`) and unix sockets except mDNSResponder.
  - Deny the pasteboard, Apple Events, and exec from the data dir or `TMPDIR`.
  - Deny `file-read*` under `$HOME` outside the extension and data dirs.
  - Tests: a copied `security` binary, ctypes `SecItemCopyMatching`, a read of `~/Library/Mail`.
- **iMessage `/ext add|update|remove`.** Terminal confirmation is enough for first-party code, and it keeps remote installs out of the threat model.
- **Git and network sources, Python deps with per-extension venvs.** They need the sandbox, clone hardening and `uv sync --locked` review first.
- **HTTP `emit()` for long-running extension processes** (trade-jev); per-extension network allowlists (an `srt`-style proxy); a separate macOS user for extensions; CPU and memory limits; task, audit and event retention.
- **Model-ranked proactive nudges** beyond the code-built briefing: quiet hours, "not useful" feedback.

**Later phases:**
- Phase 4: dashboard tiles for extensions, jobs and the briefing.
- Phase 5: Slack notify and approvers (DORI co-founder), the Claude runner.
- Phase 6: on-call.
- Phase 7: life-lane extensions.

**Closed by Phase 3:** Keychain secret injection; L3 reports for jobs with no requester; GitHub tools (as the extension, read first); the scheduler that proactive nudges depend on.

---

## 9. Appendix: research sources

| Source | Takeaway |
|---|---|
| [MCP Python SDK migration](https://py.sdk.modelcontextprotocol.io/migration/), [`client/stdio.py`](https://github.com/modelcontextprotocol/python-sdk/blob/main/src/mcp/client/stdio.py) | (Deferred MCP) v2 API; default env is HOME/PATH/USER-only; children survive a graceful exit, so the host kills the group. |
| [MCP spec: tools](https://modelcontextprotocol.io/specification/latest/server/tools), [security best practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices) | Annotations are untrusted; show the exact server command; sandbox servers. |
| [Invariant: tool poisoning](https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks), [mcp-scan](https://invariantlabs.ai/blog/introducing-mcp-scan) | Rug pulls and poisoned descriptions; hash-pin tool definitions. |
| [Goose bypass advisory](https://www.vulncheck.com/advisories/goose-from-1.28.0-before-1.44.0-manual-approval-bypass-via-read-only-tool-annotations), [Cursor MCPoison](https://research.checkpoint.com/2025/cursor-vulnerability-mcpoison/) | Trusting annotations, and approvals not bound to config hashes, were real bugs; bind approval to what runs. |
| [Simon Willison: lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) | Private data + untrusted content + egress = exfiltration; the reason for a network allowlist follow-up. |
| [Claude Code sandboxing](https://code.claude.com/docs/en/sandboxing), [sandbox-runtime](https://github.com/anthropics/sandbox-runtime) | Seatbelt via `sandbox-exec`; Go CLIs need `trustd` for TLS; deny-write default plus a proxy allowlist (deferred sandbox). |
| [Gastown: sandbox-exec status](https://gastown.dev/docs/research/macos-sandbox-exec) | Deprecated since about 10.12 but functional (third-party; verify per macOS release). |
| [keyring](https://pypi.org/project/keyring/), Apple forums [656000](https://developer.apple.com/forums/thread/656000) / [746931](https://developer.apple.com/forums/thread/746931) | The ACL is per binary; LaunchDaemons see only the System keychain; headless contexts can't prompt. Use `/usr/bin/security` from a GUI-session LaunchAgent and verify no prompt. |
| [cronsim](https://github.com/cuu508/cronsim), [Debian cron(8)](https://manpages.debian.org/unstable/cron/cron.8.en.html) | zoneinfo cron with Debian DST semantics (gap runs after the jump, fold runs once). |
| [croniter](https://pypi.org/project/croniter/), [APScheduler](https://pypi.org/project/APScheduler/) | croniter maintained again under pallets-eco; APScheduler 4 still alpha; a small loop plus cronsim is simpler. |
| [launchd.plist(5)](https://keith.github.io/xcode-man-pages/launchd.plist.5.html) | (Deferred critical jobs) `StartCalendarInterval` coalesces missed intervals on wake. |
| [Homebrew tap trust](https://docs.brew.sh/Tap-Trust), [Claude Code plugin sources](https://code.claude.com/docs/en/plugins/marketplace-reference), [VS Code extension security](https://code.visualstudio.com/docs/configure/extensions/extension-runtime-security) | No code loads until trusted; pin by commit, not tag; a publisher badge isn't integrity. |
| [uv sync](https://docs.astral.sh/uv/concepts/projects/sync/) | (Deferred deps) `--locked` refuses a stale lock. |
| [asyncio subprocess](https://docs.python.org/3/library/asyncio-subprocess.html) | `limit=` isn't an output cap and `kill()` hits only the child: use bounded reads, a new session, and `killpg`. |
