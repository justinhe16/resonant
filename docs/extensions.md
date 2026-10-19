# Writing a Resonant extension

An extension teaches Resonant about one product or domain, such as a trading bot, an on-call rotation, or a home lab, without changing the core. It is usually its own repo containing a `resonant.yaml` manifest and the scripts or servers the manifest points at. The core never imports extension code. It reads the manifest, runs your commands as separate processes, and enforces policy around them.

> **Status.** The manifest schema is v1, with Phase 3's additive fields (job effects and levels, `catchup`, `description`, `examples`) and MCP servers over stdio (MCP-1) and streamable HTTP (MCP-2, [dashboard-plan.md](dashboard-plan.md) D8). Everything in this guide is built except critical jobs, which are deferred: `ext add` refuses `critical: true` for now ([`followups.md`](followups.md)). [`extensions/github`](../extensions/github) is a real one.
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
| `mcp` | `{servers: {<id>: …}}`, optional | MCP servers (local over stdio, or remote over https) and the typed list of tools each may expose, described in §4. |
| `notify` | `{on: [event…], channel: imessage\|slack}` | Which of your emitted events are forwarded to people. |
| `report` | `{max_urgency: urgent\|normal\|low}` | The highest urgency your `report()` items may use (`urgent` texts the owner right away, `normal` joins the hourly pulse, `low` waits for the 08:00 briefing). Without it, reports are refused. `item` is reserved and can't be in `notify.on`. |
| `health` | `{check: <cmd>, every: 30s\|5m\|1h}` | A health command. A zero exit code means healthy. |
| `secrets` | list of names | Keychain secret names (`^[a-z][a-z0-9_]{0,31}$`) your tools and jobs may receive. Every tool's and job's `secrets` must be listed here. |
| `bins` | list of `gh`/`git`, optional | Host binaries your own code runs (the github extension's `gh`). Each is pinned by its real absolute path at install and reaches every process of the extension as `RESONANT_BIN_<NAME>` (`RESONANT_BIN_GH`); run it by that path, never by a `PATH` lookup. Shown as an escalation at install. |
| `repos` | list of `owner/name`, ≤ 20, optional | GitHub repos this extension is about (e.g. `me/trading-bot`). Saying "work on trading" in a request then means those repos: one link resolves to it, several ask which. Links are only a name for the repo; they grant nothing, and the repo is checked against GitHub when first used. `ext add`/`update` replace the links, `ext remove` drops them, and `/ext list` shows them. |

## 4. Tools

Every tool declares an **effect**, which says what it can change, and optionally a **level**, which says how much autonomy Resonant has with it. **The model never chooses these. They come only from your manifest.**

| Effect | Default level | Rules |
|---|---|---|
| `read` | L0 | A read with no secrets is **always L0**. A read that uses secrets may be set higher. |
| `write` | L2 (GT-1; was L1) | You may set L0–L3. Set `level: L1` for anything destructive, so it always needs `yes <code>`. |
| `pay` | L1 | **Always L1, permanently.** No override, ever. Approval requires an exact `yes <code>` reply. |

The gate weighs the level (the risk) against **who chose the action**. A slash command the owner typed is `explicit`; anything the model chose is `inferred`; a job's run is `scheduled`, which the gate treats like `inferred`.

| Level | Explicit | Inferred or scheduled (model, jobs) |
|---|---|---|
| L0 | Act silently. Use it for reads and triage. | same |
| L3 | Act, then report. Use it for reversible actions with a small blast radius. | same |
| L2 | Act, then report. | Ask first, and a plain `yes` approves. For the owner the prompt also offers `yes always` (`[A7] … (yes always = don't ask before running github.merge_pr in jhe/dori). Reply yes / yes always / no`): that saves a standing allow for the tool on its repo (a `repo` argument) or your extension, and the owner's later chat calls run as L3. `/rules` lists it, `/rules forget <n>` removes it. Members and jobs always ask. |
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
- **argv[0]** must be a `.py` file in your extension directory (`./cli.py`; files there run only as Python), `python3 <script.py in your dir>`, `gh` or `git`. Commands that fetch code at run time (`npx`, `bunx`, `pipx`, `uvx`, `uv`, `docker`, `curl`, …) and shells (`sh`, `bash`, `env`, …) are refused. Python scripts run under the daemon's own interpreter with `-I -S`; the shebang is ignored. `gh` and `git` (as argv[0] or in `bins`) are pinned at install by their real path, looked up on `/opt/homebrew/bin:/usr/bin:/bin`; one a Claude worker could replace is refused (it must be root-owned all the way up, or a Homebrew keg the sandbox guard protects: `/opt/homebrew/bin/gh` linking into `/opt/homebrew/Cellar/gh/<version>`). After `brew upgrade` moves it, `resonant ext update <name>` pins the new one.
- Exit 0 with **exactly one JSON object** on stdout means success.
- A **non-zero exit means nothing took effect**: you promise that a failed run changed nothing. A timeout or kill is treated as "maybe happened" for writes.
- Write state to `$RESONANT_EXT_DATA` (also your `HOME`), never next to your code: the installed copy is read-only.
- The environment is built from scratch, never the daemon's: `PATH` from `extensions.process.path` (default `/usr/bin:/bin`; workers can write `/opt/homebrew/bin`, so it isn't there: use `bins` for `gh`/`git`), `RESONANT_BIN_<NAME>` for each pinned `bins` entry, `HOME` and `RESONANT_EXT_DATA` (your data dir), `TMPDIR` (`<data>/tmp`), `LANG`, `USER`, `LOGNAME`, `RESONANT_EXT`, `RESONANT_EVENTS_FD=3`, `GIT_TERMINAL_PROMPT=0`, `RESONANT_JOB_DUE_AT` for jobs, and `RESONANT_SECRETS_FD=4` when you declare secrets (below).
- **Secrets are never in your environment or argv** (a same-user process can read both with `sysctl KERN_PROCARGS2`, and no sandbox can deny that). Resonant writes the ones this tool or job declares as one JSON object `{name: value}` into a pipe, closes the write end, and hands you the read end as **fd 4** (`RESONANT_SECRETS_FD=4`; the variable holds the fd number, never a value). Read it once with the stdlib helper `resonant_sdk/secret.py`, copied next to your script: `from secret import secret; token = secret("token")`. Values are cached in memory; a missing one raises `SecretError`. Don't put a secret in argv or a child's environment: `subprocess` closes fd 4 in children by default, and to hand one to another program use a private 0600 file in a 0700 dir you remove afterwards (the github extension does this for `gh`'s `GH_CONFIG_DIR`). A tool with no declared secrets gets no fd 4. The model never sees them.
- stdout is capped at 256 KB (past that the process is killed); the result is stored up to 32 KB and the model sees at most 4 KB of it, as untrusted data. stderr (8 KB) is never shown or logged; a redacted 200-character tail goes to the failure audit row.
- On a timeout, a cancel or the kill switch, the whole process group and any tracked children are killed.
- Calls are idempotent per (task, step, intent). Resonant won't re-run a call that already succeeded.

### MCP tools

Use an MCP server for stateful or long-running tools (a warm browser, a database connection) or when a server already exists for the product. Its tools become ordinary extension tools: the same effects, levels, gate, approvals, secrets, audit and kill switch as CLI tools, and the brain sees them like any other tool of yours. MCP servers don't declare effects, so **you list every tool you want exposed, with its effect**. A tool the server offers that you don't list is never registered or called.

```yaml
secrets: [token]
mcp:
  servers:
    mail:                             # server id: lowercase, digits, - and _
      transport: stdio                # or http (below)
      command: ./server.py            # or a server binary: mail-mcp, /usr/local/bin/mail-mcp
      args: [--readonly]              # literal; no placeholders
      tools:
        - name: search_mail           # the server's tool name (lowercase, digits, _)
          description: Search the mailbox by sender or subject.   # what the model sees
          effect: read
          secrets: [token]
        - name: send_mail
          description: Send a drafted reply.
          effect: write
          level: L1
          commit: true
          secrets: [token]
          timeout_s: 60               # default 30, at most 300
```

Contract:
- **Command.** One word: a `.py` file in your extension dir (run as `python -I -S server.py <args>` under the daemon's interpreter, like CLI scripts), or a server binary looked up on `/opt/homebrew/bin:/usr/bin:/bin` (or given by absolute path). A binary is pinned by its real absolute path at install, like `bins`, and must be one no Claude worker can replace: root-owned all the way up (e.g. `sudo install -m 755 mail-mcp /usr/local/bin/`), so a plain Homebrew install in `/opt/homebrew` is refused. Fetch-runners (`npx`, `uvx`, `uv`, `bunx`, `pipx`, `docker`, `curl`, …), shells and interpreters or package managers (`node`, `deno`, `bun`, `python*`, `npm`, `pip`, `brew`, …) are refused: they would run code the pin doesn't cover. Since extensions are stdlib only, a Python server is a stdlib JSON-RPC loop (see `tests/fixtures/fake_mcp_server.py`).
- **What the model sees.** Your manifest `description`, and the server's input schema stripped to validation keywords (types, properties, required, enums, bounds, patterns, local `$ref`), with every value type-checked and short (names at most 64 chars, enum/const/pattern/format strings at most 100); a schema that doesn't fit is refused at install. A `pattern` is the exception: JSON Schema patterns are ECMA-262 regexes and generated schemas carry long ones (zod's UUID and ISO date-time patterns) or ECMA-only syntax (`\p{L}`, `(?<name>…)`, `[^]`), so a string pattern over 100 chars, not printable, or one Python's `re` won't compile is **dropped**: the model doesn't see it, it isn't enforced locally, and the server still validates its own schema. Dropping is deterministic, so the pin is stable, and a server change to a dropped pattern is not drift (it changes nothing the model sees or Resonant checks). The stripped schema is only ever **looser** than the server's (LN-2). A subschema is loosened when it drops a pattern, drops any key that isn't a pure annotation (`description`, `title`, `default`, `examples`, `$comment`, `deprecated`, `readOnly`, `writeOnly`, `$schema`, `$id`, `$anchor`, `$dynamicAnchor`, `$vocabulary`, `x-*`): so `if`/`then`/`else`, `dependencies`, `dependentRequired`, `unevaluated*`, `minContains`, vendor keywords and the like, whatever draft the server uses; or keeps a `format` (shown to the model, but no format checker runs locally, and the server's may). Every kept keyword is monotone except `not` and `oneOf`, so over a loosened subschema (directly, or through a `$ref` to a `$defs` entry) a `oneOf` becomes an `anyOf` of the same branches and a `not` is removed; zod's nullable fields are `oneOf: [<string with pattern>, {type: null}]`. `contains` goes with a dropped `minContains`. A `$ref` to anything but a plain root `$defs`/`definitions` name is treated as loosened whenever something was dropped. A nested `$id` is refused (it would change what a `#/…` pointer means). A pattern that isn't a string, a `patternProperties` key that fails the same test, and a `$defs` name over 64 chars are refused. (A short pattern Python can't compile used to be kept; an install pinned that way sees the tool as changed once, and `ext update` re-pins it.) The install screen lists each tool's argument names. Every call's arguments are validated against that pinned schema (and capped at 64 KB) before they reach the server. The server's tool description, every `description`/`title`/`default`/`examples` inside the schema, and its annotations (`readOnlyHint`, …) never reach the model or the gate.
- **Pins.** `ext add` starts each server once from the staged copy, with **no secrets** and a scratch `HOME`, reads its tool list (so the server's code runs before you confirm, as your user, like `ext add`'s own checks; only add sources you trust; an http server is asked over the network, below), and pins each listed tool as `sha256(name, input_schema, output_schema)` over the stripped schemas (pin v2, GT-1): what the model sees and arguments are validated against. A reworded server description, title or annotation is not a change. Pins from before GT-1 (v1, which also covered the description and annotations) are re-pinned once at the first start when the stored input schema still matches the live one (audited `ext.mcp_pin_migrated`, no text); a different schema is drift as below. The review screen lists every MCP server and every MCP tool with its pin under Escalations (and the server's unlisted tools as a note); install refuses if the server lists something else the second time. A listed tool the server doesn't offer refuses the install (`mcp server <id> has no tool <name> (renamed? it has <close names>); fix or remove it in the manifest's tools`), rather than installing with that tool disabled: the manifest is your typed list of what each tool is and does, and disabling is for drift after you approved it. A stdio server that needs a secret to list its tools can't be installed yet. At every start of the server, and every time it sends `notifications/tools/list_changed`, Resonant lists the tools again: a listed tool that is gone or whose pin changed is **disabled** (hidden from the model, refused on call), audited `ext.mcp_drift`, and you get one text: `<ext>: MCP tool <tool> changed since you approved it; disabled. resonant ext update <ext> to review`. `ext update` shows the old and new pins even when no file of yours changed (`MCP tools: changed`).
- **Secrets.** The server is one long-running process, so it gets the **union of its tools' declared secrets**, once, on fd 4 (`RESONANT_SECRETS_FD=4`, read with `secret.py`), never in its environment or argv. A third-party binary that only reads a token from an environment variable is not supported over stdio. A secret declared only by your CLI tools never reaches the server.
- **Environment and process.** The same scratch environment as CLI tools (no `RESONANT_EVENTS_FD`: MCP tools don't emit events), cwd your read-only copy, `HOME` your data dir. The server starts when the daemon starts (or on the first call after it ended) and runs in its own process group. `/kill` ends it and its children and no server starts again until `/resume`; on shutdown Resonant closes its stdin and then always ends the whole group, because MCP servers often leave children behind.
- **Results.** Text content is joined into `{"text": …}` (images and other content are named, not passed), plus `structured` when the server returns `structuredContent`; then capped like CLI results (32 KB stored, 4 KB to the model, untrusted data) and redacted. One message over 256 KB ends the server. A call times out at its `timeout_s` and is cancelled on the server.
- **Failures.** A protocol error, a server that won't start, a missing secret, or a disabled tool means nothing ran. A tool result with `isError`, a timeout, a kill or a server exit is a failure for a read and **maybe happened** for a write: a third-party server doesn't promise that a failed call changed nothing. Error texts are built by Resonant; the server's error text never reaches you. stderr is never logged raw: a redacted 200-character tail goes to the failure audit row.
- Only tools: no MCP resources, prompts, sampling, roots or elicitation (server requests other than `ping` are refused).

#### Remote servers over HTTP

A hosted MCP server is reached over **streamable HTTP** (the MCP spec's transport, protocol `2025-06-18`) at one fixed URL, with a bearer token from the Keychain. There is no process: nothing of the server runs on the Mini. [`extensions/linear`](../extensions/linear/README.md) is the worked example.

```yaml
secrets: [token]
mcp:
  servers:
    linear:
      transport: http
      url: https://mcp.linear.app/mcp   # fixed; https only
      auth: bearer                      # optional; needs secret
      secret: token                     # a declared secret, sent as Authorization: Bearer <value>
      tools:
        - name: list_issues
          description: List or search Linear issues.
          effect: read
        - name: save_issue
          description: Create or update a Linear issue.
          effect: write
          level: L2
```

Everything above applies (listed tools only, your descriptions, stripped and enforced schemas, pins at `ext add` and at every session start and `tools/list_changed`, drift disables and texts you once, results capped and redacted, the same failure rules). What differs:

- **URL.** One fixed `https://` URL with a host, no user info or fragment (plain `http://` is accepted by the SDK only for a loopback host with a port, and the daemon refuses it: tests only). Redirects are **never followed** (a `3xx` is an error, nothing ran), and proxy settings and `.netrc` from the environment are ignored, so the token only goes to that host. The URL is part of your manifest, so changing it is a manifest change you review.
- **Token.** `auth: bearer` with `secret: <name>` (a declared secret). The daemon reads it from the Keychain when a session starts, exactly like other extension secrets, and sends it only as `Authorization: Bearer <value>` to the URL. It never reaches the model, a process, a log or the audit log; every response is redacted with it. A value that isn't a valid header value is refused before anything is sent. Tools of an http server declare no `secrets` of their own.
- **`ext add` and the token.** Listing a hosted server's tools usually needs the token. `ext add`/`ext update` asks first, before the review screen: `Send <ext>/<secret> to <host> now?`. Say no and the install stops with `needs <ext>/<secret> to list its tools, and you didn't allow sending it`; with no stored value it says to `resonant secret set` it first. After you type the name, the install sends it again only if the manifest is byte-for-byte the one you confirmed (a changed manifest is refused before any server is contacted). The escalation line reads `mcp server "<id>" calls "<url>" (http, network), sends secret <name> as its bearer token`.
- **Sessions.** Each message is a POST (JSON or SSE answers); `Mcp-Session-Id` and `MCP-Protocol-Version` are sent after `initialize`; a GET stream (if the server has one) carries `tools/list_changed` between calls; the session is ended with a DELETE on shutdown. A `404` for a dropped session starts a new session (re-checking pins) and retries once: nothing ran.
- **Caps and time.** One message (a JSON body or one SSE event) at most 1 MB, one response at most 4 MB; past that the call is `maybe happened` and the session ends. Connect timeout 10 s; each call is bounded by the tool's `timeout_s` and cancelled on the server when it runs out.
- **Kill switch.** `/kill` ends every session at once: in-flight calls end as `maybe happened` for writes, and no session starts until `/resume`.
- **Failures.** Not connected (DNS, refused, TLS, connect timeout), `401`/`403` (the token was refused), any other `4xx` or a redirect: nothing ran. A `5xx`, a dropped connection mid-call or a malformed answer: a failure for a read, **maybe happened** for a write.

##### OAuth (`auth: oauth`)

For a server that takes short-lived OAuth 2.0 access tokens from your own OAuth client (Google's): [`extensions/gcal`](../extensions/gcal/README.md) is the worked example.

```yaml
secrets: [client_id, client_secret, refresh_token]
mcp:
  servers:
    calendar:
      transport: http
      url: https://calendarmcp.googleapis.com/mcp/v1
      auth: oauth
      oauth:
        token_url: https://oauth2.googleapis.com/token   # fixed; https only
        client_id: client_id                              # declared secrets, by name
        client_secret: client_secret
        refresh_token: refresh_token
```

- The daemon reads the three secrets from the Keychain when a session starts and posts them (the refresh-token grant, RFC 6749 §6) only to `token_url`, under the same rules as `url` (https, no redirects, no proxies or `.netrc`). The access token it gets lives only in memory, is sent only to `url` as the bearer, is refreshed 2 minutes before it expires (and once after a `401` on any call but `initialize`, which starts a new session and retries once: nothing ran), and every one issued is redacted from what the server sends. Nothing is written to disk and no process gets any of them.
- A refused grant is definite (nothing reached the server) and shows only the RFC 6749 error code: `the OAuth token endpoint refused the refresh token (invalid_grant)`. Re-set the secret; the next session reads it.
- `ext add` asks `Send <ext>/<refresh_token> to <token host> now?`. Saying yes sends the client id and client secret there too (the escalation line names all three); the access token it returns then goes only to `url`. The escalation reads `… sends secrets <client_id>, <client_secret>, <refresh_token> to "<token_url>" (oauth) and the access token it gets as its bearer token`.
- Getting the refresh token is up to you (Resonant has no interactive OAuth flow): the gcal README uses Google's OAuth 2.0 Playground with your own client.

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
    effect: read                   # default write, which is L2 and asks every run
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
- `notify.on` chooses which events are forwarded to people over `channel`. Leave out what isn't urgent: the owner's trust rules can still text one type (`notify_now` naming it) or hold a listed one (`notify_digest`), per repo (the event's `data.repo`) or for the whole extension (GH-2). github lists nothing; its events feed the hourly rollup.
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
2. Validates the manifest and the directory: stdlib only, the argv[0] allowlist, MCP server commands, no symlink that leaves the tree, the name rules and approver grants. Critical jobs are refused for now. Then it starts each stdio MCP server once (no secrets, a scratch home), asks each http server for its tools (after asking you before sending its token), and pins the tools you listed.
3. Shows a heading built by code, `my-ext repo:my-ext@a1b2c3d4 digest 9f3e01aa` (commit and content digest), then **escalations first** (secrets, approvers, tools and jobs that ask, commands that use `gh`/`git`, each MCP server and each MCP tool with its pin), then every tool, job, report grant, notify event, linked repo and the health check. Every string from your manifest is shown sanitized.
4. You confirm by typing the extension name.
5. It stages the source **again** and refuses if the commit, digest, manifest or MCP tool pins changed since you confirmed. The copy goes to `~/.resonant/extensions/<name>/` (read-only), its digest is recomputed, and only then is the row written, with an `ext.install` audit row.
6. If the daemon is up, it restarts (reply-first path), and the owner gets `Back up …` and `Installed my-ext`.

The digest is sha256 over the sorted `(path, file/executable/symlink, sha256(content))` of the tree. The daemon re-checks it at every load, so an edit to the installed copy disables the extension until you run `ext update`.

**Data and secrets from another source.** Resonant remembers each extension's last source and secret names, even after `remove --purge`. If `~/.resonant/ext-data/<name>/`, or Keychain items an earlier install declared, came from a different source, `ext add` refuses until you pass `--adopt`. Secrets you stored before the first install are fine.

**Changing an extension later.** `resonant ext update <name>` shows the pins before and after, the files added, removed or changed, and the escalations added or removed (or `code only` when the manifest is unchanged), then the full review, and asks for the typed confirmation **every time**. To move to another source, `remove` and `add` again (with `--adopt`).

**Removing.** `ext remove` deletes the row, the job state, the repo links and the copy (repos already registered stay). It keeps the data dir unless you pass `--purge`, and lists the Keychain items left with the `resonant secret rm …` command for each.

## 10. Checklist

- [ ] `resonant ext validate` passes, and your CI validates with `resonant_sdk.load_manifest`.
- [ ] Stdlib only: no dependency files; every command starts with a `.py` file in your dir, `python3 <script.py>`, `gh` or `git`; an MCP server is a `.py` in your dir, a root-owned binary, or a fixed https URL.
- [ ] Every MCP tool you want is listed with its real effect and a description written for the model.
- [ ] A non-zero exit means nothing took effect; state goes to `$RESONANT_EXT_DATA`.
- [ ] Every job declares its real effect; asking jobs (L1/L2) run at most hourly.
- [ ] Every tool prints exactly one JSON object and exits 0 on success.
- [ ] Effects and levels are as low as safely possible, and `commit: true` is on irreversible actions.
- [ ] No secrets in the repo; secrets are declared and stored in the Keychain.
- [ ] Jobs are idempotent and safe to skip when paused.
- [ ] Events carry `dedupe_key`.
- [ ] Each approver holds the `approve` grant in `principals.yaml`.
- [ ] Anything that moves money or places orders runs inside your extension with its own risk controls. Core only sees read tools, events and the kill switch, as trade-jev does.
