# Writing a Resonant extension

An extension teaches Resonant about one product or domain, such as a trading bot, an on-call rotation, or a home lab, without changing the core. It is usually its own repo containing a `resonant.yaml` manifest and the scripts or servers the manifest points at. The core never imports extension code. It reads the manifest, runs your commands as separate processes, and enforces policy around them.

> **Status.** The manifest schema is v1, with Phase 3's additive fields (job effects and levels, `catchup`, `description`, `examples`). Everything in this guide is built (Phase 3) except MCP tools and critical jobs, which are deferred: `ext add` refuses an `mcp` block or `critical: true` for now ([`followups.md`](followups.md)). [`extensions/github`](../extensions/github) is a real one.
>
> **Phase 3 extensions are first-party, owner-installed code** from this repo or a local git work tree. They run as the owner's macOS user. See [phase3-plan.md](phase3-plan.md) D13.

## 1. The smallest extension

```
my-ext/
├─ resonant.yaml
└─ cli.py          # prints one JSON object on stdout
```

```yaml
name: my-ext          # slug: lowercase, digits, dashes
version: 1            # manifest schema version (only 1 exists)
description: Status of the thing.   # one line; how Resonant recognizes requests for you
tools:
  get_status:
    description: Current status of the thing.
    run: ./cli.py status
    effect: read
```

```sh
resonant ext validate ./my-ext    # schema + invariants + approver grants
```

See [`extensions/example`](../extensions/example) for a complete, working reference.

## 2. Stdlib only, and the SDK

**Extensions are stdlib only.** `ext validate` and `ext add` refuse a `pyproject.toml` that declares dependencies, `requirements*.txt`, lock files (`uv.lock`, `poetry.lock`, `pdm.lock`, `Pipfile*`), `package.json`, `setup.py`, `setup.cfg` and `environment.yml`, anywhere in the tree. There are no venvs and no install step, so no build-time code ever runs.

Extensions run as `python -I -S`, so no package (the SDK included) is importable from them. The runtime helpers, `resonant_sdk/emit.py` (events, §7) and `resonant_sdk/secret.py` (secrets), are stdlib only: copy them next to your script. `-I` also keeps your script's directory off `sys.path`, so add it yourself before importing a sibling module.

Use `resonant-sdk` in your own CI or dev environment only, never as a runtime dependency (install it there with `pip install "resonant-sdk @ git+https://github.com/justinhe16/resonant#subdirectory=packages/resonant-sdk"`). It gives you `Manifest` and `load_manifest` for validating, `ToolSpec`, `Effect`, `Level` and `Event`.

## 3. Manifest reference (`version: 1`)

Unknown keys are errors. YAML is parsed so that only `true`/`false` are booleans: `on:`, `yes:` and `no:` stay strings.

| Key | Type | Meaning |
|---|---|---|
| `name` | slug | Unique extension id. It namespaces tools, jobs, events and secrets, and becomes the `/<name>` command, so it may not be `core` or a builtin command word (`help`, `status`, `ext`, …). |
| `version` | `1` | Schema version. |
| `description` | string, ≤ 120 chars, optional | One line saying what the extension is for. Resonant's router uses it to recognize requests meant for you, and the install screen shows it. |
| `examples` | ≤ 3 lines of ≤ 80 chars, optional | Example requests, used as few-shots for the router. |
| `uses` | slug, optional | A first-party extension this one configures. It allows exactly one extra block with that name. |
| `approvers` | list of principals | Who may approve this extension's actions when the gate asks (L1, inferred L2, or anything above L0 under the kill switch). Each must hold the `approve` grant for this extension in `principals.yaml`. |
| `jobs` | list | Scheduled commands, described in §6. |
| `tools` | map | Command-backed tools, described in §4. |
| `mcp` | object, optional | An MCP server and the typed list of tools it may expose, described in §4. |
| `notify` | `{on: [event…], channel: imessage\|slack}` | Which of your emitted events are forwarded to people. |
| `report` | `{max_urgency: urgent\|normal\|low}` | The highest urgency your `report()` items may use (`urgent` texts the owner right away, `normal` joins the hourly pulse, `low` waits for the 08:00 briefing). Without it, reports are refused. `item` is reserved and can't be in `notify.on`. |
| `health` | `{check: <cmd>, every: 30s\|5m\|1h}` | A health command. A zero exit code means healthy. |
| `secrets` | list of names | Keychain secret names (`^[a-z][a-z0-9_]{0,31}$`) your tools and jobs may receive. Every tool's and job's `secrets` must be listed here. |

## 4. Tools

Every tool declares an **effect**, which says what it can change, and optionally a **level**, which says how much autonomy Resonant has with it. **The model never chooses these. They come only from your manifest.**

| Effect | Default level | Rules |
|---|---|---|
| `read` | L0 | A read with no secrets is **always L0**. A read that uses secrets may be set higher. |
| `write` | L1 | You may set L0–L3. |
| `pay` | L1 | **Always L1, permanently.** No override, ever. Approval requires an exact `yes <code>` reply. |

The gate weighs the level (the risk) against **who chose the action**. A slash command the owner typed is `explicit`; anything the model chose is `inferred`; a job's run is `scheduled`, which the gate treats like `inferred`.

| Level | Explicit | Inferred or scheduled (model, jobs) |
|---|---|---|
| L0 | Act silently. Use it for reads and triage. | same |
| L3 | Act, then report. Use it for reversible actions with a small blast radius. | same |
| L2 | Act, then report. | Ask first: `[A7] …? Reply yes / no`, and a plain `yes` approves. |
| L1 | Always ask: `[D7] …? This can't be undone. Reply yes D7 / no`. Only `yes <code>` approves. | same |

While the kill switch is on (`/kill`), everything above L0 asks first, and L1 still needs the code. There are no veto windows ("doing X in N min unless you stop") yet; they are deferred past Phase 3.

Pick the lowest level you'd be comfortable with if the model misunderstood a request. Mark irreversible, user-visible actions (send, pay, delete) with `commit: true`.

### CLI tools (default)

```yaml
tools:
  get_pnl:
    description: P&L for a trading day.
    run: ./cli.py pnl --date {date} --json     # {placeholders} must be declared in args
    effect: read
    args: {date: string}                    # string | integer | number | boolean
    timeout_s: 30                           # at most 300
  restart_worker:
    run: ./cli.py restart
    effect: write
    level: L3
    secrets: [api_token]                    # must also appear in top-level secrets
```

Contract:
- The command runs in your installed (read-only) extension directory with arguments substituted. It is never run through a shell. Arguments the model doesn't declare are refused (`additionalProperties: false`).
- **argv[0]** must be a `.py` file in your extension directory (`./cli.py`; files there run only as Python), `python3 <script.py in your dir>`, `gh` or `git`. Commands that fetch code at run time (`npx`, `bunx`, `pipx`, `uvx`, `uv`, `docker`, `curl`, …) and shells (`sh`, `bash`, `env`, …) are refused. Python scripts run under the daemon's own interpreter with `-I -S`; the shebang is ignored.
- Exit 0 with **exactly one JSON object** on stdout means success.
- A **non-zero exit means nothing took effect**: you promise that a failed run changed nothing. A timeout or kill is treated as "maybe happened" for writes.
- Write state to `$RESONANT_EXT_DATA` (also your `HOME`), never next to your code: the installed copy is read-only.
- The environment is built from scratch, never the daemon's: `PATH=/opt/homebrew/bin:/usr/bin:/bin`, `HOME` and `RESONANT_EXT_DATA` (your data dir), `TMPDIR` (`<data>/tmp`), `LANG`, `USER`, `LOGNAME`, `RESONANT_EXT`, `RESONANT_EVENTS_FD=3`, `GIT_TERMINAL_PROMPT=0`, `RESONANT_JOB_DUE_AT` for jobs, and `RESONANT_SECRETS_FD=4` when you declare secrets (below).
- **Secrets are never in your environment or argv** (a same-user process can read both with `sysctl KERN_PROCARGS2`, and no sandbox can deny that). Resonant writes the ones this tool or job declares as one JSON object `{name: value}` into a pipe, closes the write end, and hands you the read end as **fd 4** (`RESONANT_SECRETS_FD=4`; the variable holds the fd number, never a value). Read it once with the stdlib helper `resonant_sdk/secret.py`, copied next to your script: `from secret import secret; token = secret("token")`. Values are cached in memory; a missing one raises `SecretError`. Don't put a secret in argv or a child's environment: `subprocess` closes fd 4 in children by default, and to hand one to another program use a private 0600 file in a 0700 dir you remove afterwards (the github extension does this for `gh`'s `GH_CONFIG_DIR`). A tool with no declared secrets gets no fd 4. The model never sees them.
- stdout is capped at 256 KB (past that the process is killed); the result is stored up to 32 KB and the model sees at most 4 KB of it, as untrusted data. stderr (8 KB) is never shown or logged; a redacted 200-character tail goes to the failure audit row.
- On a timeout, a cancel or the kill switch, the whole process group and any tracked children are killed.
- Calls are idempotent per (task, step, intent). Resonant won't re-run a call that already succeeded.

### MCP tools (deferred)

Not supported yet: `ext add` refuses an `mcp` block. The planned contract: use an MCP server for stateful or long-running tools (a warm browser, a database connection) or when you have many tools. MCP servers don't declare effects, so **you list every tool you want exposed, with its effect**. Unlisted tools are refused.

```yaml
mcp:
  command: uv run my-mcp-server
  tools:
    search_mail: {effect: read}
    send_mail:   {effect: write, level: L1, commit: true}
```

## 5. Secrets

- Declare secret names in `secrets:` and give each tool or job the ones it needs. Names are lowercase with underscores (`token`, `api_key`).
- Store values in the macOS Keychain on the Mini, from its terminal only: `resonant secret set <ext>/<name>`.
- Only the executor (and the scheduler, for L0 jobs) reads them, just before the call. `resonant secret check <ext>/<name>` does the same no-prompt read from the terminal; `resonant secret list` shows which declared secrets are present.
- Every encoding of a value (raw, URL-encoded, JSON-escaped, base64) is replaced with `[redacted]` in results, events, errors, audit rows and logs. Don't print secrets in other forms: those aren't caught.
- Never put secrets in the manifest, in `.env`, or in your repo. The Resonant repo runs gitleaks, and yours should too.

## 6. Jobs

```yaml
jobs:
  - id: watch
    schedule: "*/10 * * * *"       # 5-field cron
    tz: America/Los_Angeles        # default: the daemon's timezone
    run: ./cli.py watch
    effect: read                   # default write, which is L1 and asks every run
    level: L0                      # optional, same rules as tools
    secrets: [token]               # must also appear in top-level secrets
    timeout_s: 300                 # default 300, at most 1800
    catchup: 1h                    # none (default) or up to 24h: run once if missed while down
```

- A job runs as `scheduled`: nobody chose it, so L0 runs silently, L3 runs and reports to your approvers, and L2 and L1 ask first.
- **Cadence.** A job may fire at most every 5 minutes, and **at most hourly if it asks** (L1 or L2), so prompts can't pile up. Give a job the lowest effect that's true; a job without `effect` is a write and asks.
- Each job also exists as a tool spec named `job_<id>` (dashes become underscores) so `/ext run <ext>/<id>` can run it. No tool may use that name.
- **Normal jobs** run on the daemon's scheduler.
- While the kill switch is on, due jobs are skipped and never caught up, and a running job's processes are killed.
- **Critical jobs** (deferred: `ext add` refuses `critical: true` for now) will get their own LaunchAgent, wrapped as `resonant job-guard --job <ext>/<id> -- <run>`, which **refuses to run while the kill switch is engaged** and fails closed (exit 75). Make them idempotent and safe to skip.

## 7. Events and notifications

Your processes report what happened by emitting events: either an `"events"` list in the result object, or JSON lines written to file descriptor 3 (`RESONANT_EVENTS_FD`) with the stdlib helper `emit.py` copied from the SDK (`from emit import emit; emit("ci_failed", "✗ resonant#40 CI failed (lint)", "resonant#40:run:123:failed")`). Events count only when the run succeeds.

```json
{"summary": "2 PRs", "events": [{"type": "ci_failed", "text": "resonant#40 CI failed", "dedupe_key": "resonant#40:run:123:failed"}]}
```

- Events arrive as `ext.<name>.<event>`. Core sets the principal, so extensions can never claim one.
- `notify.on` chooses which events are forwarded to people over `channel`.
- `dedupe_key` is required (≤ 200 chars), so retries don't double-notify.
- Each job's first successful run after install is a silent baseline: its events are stored but not sent, so state that existed before the install is never announced.
- A notification reads `[<name>] <text>`. The text is one line, clipped to 300 characters, with brackets turned into parentheses and reply words quoted, so it can never pass for an approval prompt. Keep it short and lead with a symbol (`✗`, `✓`, `👀`).
- At most 20 notifications per extension per hour; the rest become one `[<name>] +N more updates` when the hour ends. Nothing is sent while Resonant is paused or the extension is muted (`/ext mute`), and nothing held is sent later.

### Reports

`from emit import report; report("dori/incident/inc-412", "Dori failing: checkout 5xx", urgency="urgent", state={"status": "triaging"})` tells the owner about a thing, by `key` (1..120 of `[a-z0-9/._:-]`). It is mentioned again only when its fingerprint changes: the hash of `state` when given, else of `title`, plus the urgency and `clear`, so a resolve or an escalation always counts. `state` is at most 2000 bytes of JSON. Keep `state` coarse (bucket a PnL, leave timestamps out) or every report counts as news. `clear=True` marks it over; `ttl` (`30m`, `24h`; 1 minute to `168h`) is how long it stays open without a new report. Urgency is clamped to the manifest's `report.max_urgency`.

What the grant means (shown on the `ext add` review screen and in the `ext update` diff, and printed by `ext validate`):

| `report.max_urgency` | Review line | Effect |
|---|---|---|
| `urgent` | `reports: may text you any time (urgent)`, under Escalations | An urgent item is texted right away, at any hour, as `[<name>] 🔴 <title>` (`✓` when `clear=True`). It is held (and joins the next hourly pulse) while paused, muted, or over the 20/hour cap shared with notifications. |
| `normal` | `Reports: hourly updates (normal)` | Stored for the hourly pulse. |
| `low` | `Reports: 8am briefing only (low)` | Stored for the 08:00 briefing. |
| absent | none | Reports are refused (audit `pulse.refused`) and never retried. |

A bad report (key, urgency, state, ttl, empty title) is refused the same way. Titles are cleaned like notification text, with a code-built `[<name>]` prefix. A job's first run after install is a baseline: its reports are stored as already told and never sent. A `clear=True` report of an item last stored urgent counts as urgent too (still clamped to the grant), so "recovered" is texted at once; a clear of an item the owner never heard still sends. If no recipient can be reached the item stays pending for the hourly pulse. Changing `report.max_urgency` changes the manifest hash, so `ext update` shows the change and asks for a fresh approval.

#### Walkthrough: the example extension

The example is a noisy demo (a 5-minute incident job, an hourly fake PnL that appears in pulses and the briefing): `resonant ext remove example` after trying it.

`extensions/example` has two reporters. Its manifest asks for the highest grant, `report: {max_urgency: urgent}`, and `cli.py` imports `report` from a copy of `emit.py` next to it (a test keeps the copy identical to the SDK's).

- **Normal, PnL-style.** The `pnl` job (`55 * * * *`) calls `report("example/pnl/today", "PnL -$1.8k today", urgency="normal", state={"bucket": -2000})`. The title carries the exact figure; `state` holds it rounded to $500, so only a move to another bucket counts as news. Nothing is texted: it is listed in the next hourly pulse (`[example] PnL -$1.8k today`) and then, if still open, in the briefing's `Updates:`.
- **Urgent, Dori-style.** Only job runs persist events (a tool's events are dropped), so the demo is a job plus a tool that feeds it. `/example incident failing` (L3: acts, then reports) writes `incident` in the extension's data dir. Within 5 minutes the `incident` job reports `example/incident/demo` with `urgency="urgent"` and `state={"status": "failing"}`, and you get `[example] 🔴 Demo service failing: checkout 5xx` at any hour, once, however many runs repeat that state. `/example incident resolved` makes the next run report `clear=True`, texted as `[example] ✓ Demo service recovered`; later runs repeat it silently, and `resolved` with no open `failing` does nothing. Run `/ext run example/incident` to skip the wait; do it once after install first, because a job's first run is a silent baseline.
- **Low, github-style.** `github`'s `digest` job (07:50) reports `github/summary` with `urgency="low"` and `state={"open": 3, "failing": 1, "review_requested": 2}` under `report: {max_urgency: low}`. It never texts; it appears in the 08:00 briefing's `Updates:` when the counts change.

## 8. Permissions and approvers

`principals.yaml` decides who can do what with your extension:

```yaml
principals:
  justin:    {role: owner, identities: ["imessage:+15551234567", "imessage:me@icloud.com"]}
  cofounder: {identities: ["slack:U02"], grants: {my-ext: [request, approve]}}
```

- `request` allows asking Resonant to use the extension's tools or start its tasks.
- `approve` allows approving the extension's actions when the gate asks. The principal must also be listed in `approvers:`.
- The owner can always do everything. Identity grants permissions; message content never does.

## 9. Registering an extension

```sh
resonant ext validate ./my-ext     # schema, names, directory checks, approver grants
resonant ext add repo:my-ext       # extensions/my-ext in the Resonant checkout, at HEAD
resonant ext add path:/abs/my-ext  # a clean directory inside a git work tree
resonant ext list                  # status, pins, and whether each copy still matches
resonant ext update my-ext         # re-stage from the same source, diff, re-approve
resonant ext remove my-ext         # keeps its data dir (--purge deletes it)
```

These run **only in the Mini's terminal**, never over iMessage.

`ext add`:
1. Copies the approved bytes into a staging dir. `repo:` reads the **HEAD commit** (`git archive`), so uncommitted edits are never installed. `path:` reads the tracked files from the git index, and refuses the directory if any tracked file there has uncommitted changes. Nothing else is a source.
2. Validates the manifest and the directory: stdlib only, the argv[0] allowlist, no symlink that leaves the tree, the name rules and approver grants. MCP servers and critical jobs are refused for now.
3. Shows a heading built by code, `my-ext repo:my-ext@a1b2c3d4 digest 9f3e01aa` (commit and content digest), then **escalations first** (secrets, approvers, tools and jobs that ask, commands that use `gh`/`git`), then every tool, job, report grant, notify event and the health check. Every string from your manifest is shown sanitized.
4. You confirm by typing the extension name.
5. It stages the source **again** and refuses if the commit, digest or manifest changed since you confirmed. The copy goes to `~/.resonant/extensions/<name>/` (read-only), its digest is recomputed, and only then is the row written, with an `ext.install` audit row.
6. If the daemon is up, it restarts (reply-first path), and the owner gets `Back up …` and `Installed my-ext`.

The digest is sha256 over the sorted `(path, file/executable/symlink, sha256(content))` of the tree. The daemon re-checks it at every load, so an edit to the installed copy disables the extension until you run `ext update`.

**Data and secrets from another source.** Resonant remembers each extension's last source and secret names, even after `remove --purge`. If `~/.resonant/ext-data/<name>/`, or Keychain items an earlier install declared, came from a different source, `ext add` refuses until you pass `--adopt`. Secrets you stored before the first install are fine.

**Changing an extension later.** `resonant ext update <name>` shows the pins before and after, the files added, removed or changed, and the escalations added or removed (or `code only` when the manifest is unchanged), then the full review, and asks for the typed confirmation **every time**. To move to another source, `remove` and `add` again (with `--adopt`).

**Removing.** `ext remove` deletes the row, the job state and the copy. It keeps the data dir unless you pass `--purge`, and lists the Keychain items left with the `resonant secret rm …` command for each.

## 10. Checklist

- [ ] `resonant ext validate` passes, and your CI validates with `resonant_sdk.load_manifest`.
- [ ] Stdlib only: no dependency files; every command starts with a `.py` file in your dir, `python3 <script.py>`, `gh` or `git`.
- [ ] A non-zero exit means nothing took effect; state goes to `$RESONANT_EXT_DATA`.
- [ ] Every job declares its real effect; asking jobs (L1/L2) run at most hourly.
- [ ] Every tool prints exactly one JSON object and exits 0 on success.
- [ ] Effects and levels are as low as safely possible, and `commit: true` is on irreversible actions.
- [ ] No secrets in the repo; secrets are declared and stored in the Keychain.
- [ ] Jobs are idempotent and safe to skip when paused.
- [ ] Events carry `dedupe_key`.
- [ ] Each approver holds the `approve` grant in `principals.yaml`.
- [ ] Anything that moves money or places orders runs inside your extension with its own risk controls. Core only sees read tools, events and the kill switch, as trade-jev does.
