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
| `description` | string, ≤ 120 chars, optional | One line saying what the extension is for. The install screen shows it (since PER-256 no model routes by it). |
| `examples` | ≤ 3 lines of ≤ 80 chars, optional | Example requests; shown to humans only (PER-256 deleted the router few-shots). |
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
| `launch_agents` | list of `{label, description}`, ≤ 10, optional | launchd agents your own setup installs (a trading loop, a token refresher), e.g. `{label: com.tradejev.paper, description: The paper trading loop.}`. Names only: Resonant never installs, loads or reads them, and they grant nothing. The dashboard's Diagram tab shows each one under your extension with whether it is running (PER-277). `label` is `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, unique; `description` is one line, ≤ 160 chars. |
| `panels` | list, ≤ 8, optional | Dashboard panels: a command Resonant runs on a schedule and draws from the JSON it prints, described in §6a (PER-280). |

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
- **Pins.** `ext add` starts each server once from the staged copy, with **no secrets** and a scratch `HOME`, reads its tool list (so the server's code runs before you confirm, as your user, like `ext add`'s own checks; only add sources you trust; an http server is asked over the network, below), and pins each listed tool as `sha256(name, input_schema, output_schema)` over the stripped schemas (pin v2, GT-1): what the model sees and arguments are validated against. A reworded server description, title or annotation is not a change. Pins from before GT-1 (v1, which also covered the description and annotations) are re-pinned once at the first start when the stored input schema still matches the live one (audited `ext.mcp_pin_migrated`, no text); a different schema is drift as below. The review screen lists every MCP server and every MCP tool with its pin under Escalations (and the server's unlisted tools as a note); install refuses if the server lists something else the second time. A listed tool the server doesn't offer refuses the install (`mcp server <id> has no tool <name> (renamed? it has <close names>); fix or remove it in the manifest's tools`), rather than installing with that tool disabled: the manifest is your typed list of what each tool is and does, and disabling is for drift after you approved it. A stdio server that needs a secret to list its tools can't be installed yet. At every start of the server, and every time it sends `notifications/tools/list_changed`, Resonant lists the tools again and compares each pin (PER-275, MCP drift: accepted or asked):

  | What changed | What happens |
  |---|---|
  | A **read** tool's pin | Re-pinned on the spot (unless it adds server text that isn't plain, below): the new input schema is saved in `extensions.mcp_pins`, shown to the model and enforced; audited `ext.mcp_repinned` (`how: auto`) and one pulse line (`Linear updated list_issues; I accepted it (read-only)`). No alert, no health degrade. |
  | A **write or pay** tool, only **new optional input properties** (or only its output schema) | Re-pinned the same way (`I accepted it (optional field added)`). The output schema is hashed into the pin but never shown to the model or enforced, so it is not diffed: a change to it alone is benign. |
  | A write or pay tool, anything else (a new required field, a removed or retyped field, a field that became required or optional, other input rules, an existing `$defs` entry) | It **keeps working on its old approved schema, effect and level**, and you get **one** text: `[A7] Linear changed save_issue (+required field team_id). Accept? Reply yes / no`. `yes` re-pins it (audited `ext.mcp_repinned`, `how: owner`, and `Re-pinned save_issue.`). `no` turns it off, with no alert, until `resonant ext update <ext>` (`Okay: save_issue is off until you run resonant ext update linear.`; also on the very next call). No answer: it expires after 24 hours (a reminder at 12 h, the usual approval reminder) and the tool stays as it is; only a newer change asks again. A restart neither asks again nor forgets an answer: the approval row is the record. |
  | The tool is **gone** | Disabled for the life of the process, audited `ext.mcp_drift`, and **one non-urgent** heads-up: `Linear: the server no longer offers save_issue; I turned it off. resonant ext update linear to review` (it respects pause and quiet hours, and rides the pulse if held). Not a health degrade. |
  | Listed **twice**, an **unusable** schema, or a **v1 pin** whose input schema differs | Disabled (hidden from the model, refused on call), audited `ext.mcp_drift`, and one urgent text: `<ext>: MCP tool <tool> changed since you approved it; disabled. resonant ext update <ext> to review`. Health degrades (unchanged from before). |

  **Plain text only.** An automatic re-pin (read or write) happens only when every string the change adds is plain: new property names and new `enum`/`const` values of at most 40 letters, digits, `_ . -` (a value may also hold spaces; a name may not), and a `$ref` only to a `#/$defs/<name>`. A new `pattern`, `format` or `patternProperties` key is never plain. Anything else, a read tool too, is asked about like a non-benign write change (`new field text needs a look`) and the tool stays on its old schema, so server text never reaches the model unseen. Schemas are compared as JSON, so `1`, `1.0` and `true` differ. At most **3 questions per tool per 24 h**: past that the tool just stays as it is (audited `ext.mcp_repin_capped` once per live pin, no text). A `no` holds until `ext update` even if the server later reverts. A `yes` that no longer applies (the server changed again) is answered with `<tool> changed again or is off, so that yes didn't apply.`

  A re-pin persists, so a restart finds the pin already saved and says nothing again. What never moves on any path: the manifest, a tool's effect or level, its description (always yours), its secrets, an http server's URL, transport or bearer secret (those are manifest fields), and the server's text never reaches the model. The question is a task-less approval (`approvals.tool = mcp_repin`, its `intent_hash` the live pin, so a yes binds exactly the definition you were told about); the `core/mcp` job (every minute) sends it, applies the answer and sends the heads-ups, so a reply takes up to a minute to land. An intent built just before a re-pin may be refused once as "not registered with this spec"; the next try works. A model with an older tool list may fail one call after a re-pin. `ext update` shows the old and new pins even when no file of yours changed (`MCP tools: changed`).
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

Everything above applies (listed tools only, your descriptions, stripped and enforced schemas, pins at `ext add` and at every session start and `tools/list_changed`, drift is re-pinned or asked about (or disabled and texted once), results capped and redacted, the same failure rules). What differs:

- **URL.** One fixed `https://` URL with a host, no user info or fragment (plain `http://` is accepted by the SDK only for a loopback host with a port, and the daemon refuses it: tests only). Redirects are **never followed** (a `3xx` is an error, nothing ran), and proxy settings and `.netrc` from the environment are ignored, so the token only goes to that host. The URL is part of your manifest, so changing it is a manifest change you review.
- **Token.** `auth: bearer` with `secret: <name>` (a declared secret). The daemon reads it from the Keychain when a session starts, exactly like other extension secrets, and sends it only as `Authorization: Bearer <value>` to the URL. It never reaches the model, a process, a log or the audit log; every response is redacted with it. A value that isn't a valid header value is refused before anything is sent. Tools of an http server declare no `secrets` of their own.
- **`ext add` and the token.** Listing a hosted server's tools usually needs the token. `ext add`/`ext update` asks first, before the review screen: `Send <ext>/<secret> to <host> now?`. Say no and the install stops with `needs <ext>/<secret> to list its tools, and you didn't allow sending it`; with no stored value it says to `resonant secret set` it first. After you type the name, the install sends it again only if the manifest is byte-for-byte the one you confirmed (a changed manifest is refused before any server is contacted). The escalation line reads `mcp server "<id>" calls "<url>" (http, network), sends secret <name> as its bearer token`.
- **Sessions.** Each message is a POST (JSON or SSE answers); `Mcp-Session-Id` and `MCP-Protocol-Version` are sent after `initialize`; a GET stream (if the server has one) carries `tools/list_changed` between calls; the session is ended with a DELETE on shutdown. A `404` for a dropped session starts a new session (re-checking pins) and retries once: nothing ran.
- **Caps and time.** One message (a JSON body or one SSE event) at most 1 MB, one response at most 4 MB; past that the call is `maybe happened` and the session ends. Connect timeout 10 s; each call is bounded by the tool's `timeout_s` and cancelled on the server when it runs out.
- **Kill switch.** `/kill` ends every session at once: in-flight calls end as `maybe happened` for writes, and no session starts until `/resume`.
- **Failures.** Not connected (DNS, refused, TLS, connect timeout), `401`/`403` (the token was refused), any other `4xx` or a redirect: nothing ran. A `5xx`, a dropped connection mid-call or a malformed answer: a failure for a read, **maybe happened** for a write.

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
    description: polls your PRs' CI for the hourly rollup   # optional, one line, ≤ 160 chars
```

- `description` says what the job does, in one line, for the dashboard's Diagram tab (`github/watch: every 10 min job (read, runs without asking): polls …`). Display only; optional.

- A job runs as `scheduled`: nobody chose it, so L0 runs silently, L3 runs and reports to your approvers, and L2 and L1 ask first.
- **Cadence.** A job may fire at most every 5 minutes, and **at most hourly if it asks** (L1 or L2), so prompts can't pile up. Give a job the lowest effect that's true; a job without `effect` is a write and asks.
- Each job also exists as a tool spec named `job_<id>` (dashes become underscores) so `/ext run <ext>/<id>` can run it. No tool may use that name.
- **Normal jobs** run on the daemon's scheduler.
- While the kill switch is on, due jobs are skipped and never caught up, and a running job's processes are killed.
- **Critical jobs** (deferred: `ext add` refuses `critical: true` for now) will get their own LaunchAgent, wrapped as `resonant job-guard --job <ext>/<id> -- <run>`, which **refuses to run while the kill switch is engaged** and fails closed (exit 75). Make them idempotent and safe to skip.

## 6a. Panels

A panel is a page of your extension on the dashboard: stats, charts, tables, status lists. You **compute**, Resonant **draws**. Your command prints data; Resonant never runs your code in the browser and never renders your markup. Every string is plain text.

```yaml
panels:
  - id: paper                      # slug, unique among your panels, at most 8 panels
    title: Paper trading           # 1-60 chars, one line
    description: PnL and positions # optional, ≤ 160 chars
    command: [./cli.py, panel, paper]   # a list of 1-16 printable-ASCII items, each ≤ 200; no { or }
    refresh:
      idle: "*/15 * * * *"         # required: the cron when nothing else applies
      active: "*/1 9-16 * * 1-5"   # optional: the cron instead of idle while its hours/days/months match
    tz: America/New_York           # default: the daemon's timezone
    secrets: [alpaca_key]          # must also appear in top-level secrets
    timeout_s: 30                  # default 30, at most 120
    max_bytes: 24576               # default 16384, 1024-32768
```

- **A panel is an L0 read.** It is never an escalation: it can't ask, can't write, and runs through the same gate and audit as any scheduled L0 read (`origin: scheduled`). A command that runs `gh` or `git` still shows as "network" at install. Its command follows the tool rules: a `.py` file in your directory, `python3 <script.py>`, `gh` or `git`; no placeholders, so no arguments from anywhere but your manifest. Each panel also has a hidden `panel_<id>` spec (dashes become underscores) that never appears in chat. No tool may use that name.
- **Install and update.** `ext add` shows a `Panels:` block (id, title, schedule, `read L0`, size cap, the command), and `ext update` lists `+ panel <id>` and `- panel <id>`. An update that drops a panel drops its stored copy.
- **Where it shows.** The Overview tab shows up to two panels' first stats under your extension, tapping opens your extension's page (`#/ext/<name>`) with every panel, and the Diagram tab has a node per panel. The brain can read the last copy with `read_panel` (below).

### What the command prints

One JSON object on stdout and exit 0, like a tool. The JSON schema is [`panel.schema.json`](panel.schema.json), generated from `resonant_sdk/panel_schema.py` (`uv run python -m resonant_sdk.panel_schema > docs/panel.schema.json`; a test fails when it is stale).

```json
{ "schema": 1,
  "updated_at": "2026-10-16T16:58:30Z",
  "health": "ok",
  "local_url": "http://127.0.0.1:8765/paper",
  "views": [ { "id": "overview", "title": "Overview", "widgets": [ … ] } ] }
```

| Field | Rule |
|---|---|
| `schema` | Must be `1`; anything else is refused. |
| `updated_at` | Optional, ISO-8601 **with an offset**: when the data is from. A time in the future is clamped to the end of the run; absent means the end of the run. |
| `health` | Optional `ok`, `warn` or `bad`: your own verdict, shown as a dot. |
| `local_url` | Optional `http://127.0.0.1:<1-65535>/…`, ≤ 300 printable ASCII: your own page on this Mac. The **local** dashboard links it ("Open locally"); the relay never sees it. Anything else (https, another host, `localhost`, port 0) is refused. |
| `actions` | Reserved. Absent or `[]`; a non-empty list is refused. |
| `views` | 1 to 6 tabs `{id, title (cut at 40), widgets}`; 1 to 24 widgets per view and at most 48 per panel. `id` is `^[a-z0-9][a-z0-9_-]{0,31}$`. |

**Cut, never refused: strings.** A string over its cap is cut by Resonant (ending `… [cut]`). **Refused: structure.** An unknown key or widget type, a wrong type, or a count over a limit refuses the whole run: the last good copy stays on the page with the error. Errors are built by Resonant (`refused: view overview widget 3 (series): 5 series (max 4)`), redacted, and ≤ 200 characters.

Every widget may carry `id` (a slug) and `title` (cut at 64). `Format` is one of `number`, `integer`, `money`, `percent` (12.3 → `12.3%`), `duration` (seconds) or `bytes`; `Tone` is `ok`/`warn`/`bad`; `currency` is three capitals (default USD). A *time* is an ISO-8601 string with an offset, or unix seconds.

| `type` | Fields | Limits and behaviour |
|---|---|---|
| `stat` | `label` (cut 40), `value` (number, string cut 40, or null), `unit` (cut 16), `format`, `currency`, `delta` (number), `delta_format`, `health` (Tone), `hint` (cut 80) | A bool `value` is refused. |
| `series` | `kind` (`line`, `area`, `bar`), `x: {type: time or category, values}`, `series: [{name (cut 32), values}]`, `unit`, `format`, `currency`, `stacked` | 1-4 lines (5 is refused), each as long as `x.values` (`null` is a gap). At most 2400 points. A `time` axis must not go backwards; over 240 points it is downsampled to 240 (see below). A `category` axis is strings (cut 24), at most 240. |
| `table` | `columns: [{key (slug), label (cut 32), type, align, currency}]`, `rows: [{<key>: cell}]` | 1-8 columns (9 is refused). `type` is `text`, `number`, `money`, `time`, `pill` or `link`. Cells: `text` a string (cut 120); `number`/`money` a number; `time` a time; `pill` `{label (cut 24), tone: ok/warn/bad/neutral}` or a string (tone neutral); `link` `{label (cut 64), tool}`. `null` or a missing key is an empty cell. A wrong-typed cell or an unknown key is refused. A link to a tool that isn't in this manifest is shown as plain text; a link runs nothing, it only names a tool as `ext/tool`. The first 50 rows are kept: the widget says `truncated` and `total_rows`. |
| `status` | `items: [{name (cut 64), state: up/down/warn/unknown, detail (cut 120), since (time)}]` | The first 50 items. |
| `kv` | `items: [{key (cut 40), value (string cut 120, number, bool or null), format, unit}]` | The first 30 items. |
| `text` | `text` (cut 800) | Plain text, shown as is (`<b>` stays `<b>`). |
| `details` | `data` (any JSON value), `collapsed` (default true) | Flattened by Resonant into at most 200 rows `{depth, key, kind, value}` in document order (see below); a container at depth 5 becomes one `…` row. |

**Downsampling.** A time axis over 240 points keeps the first and last points exactly and splits the middle into 238 equal buckets. Each bucket takes the x of its first point and, per line, the mean of its non-null values (null if all are null). The widget says `downsampled_from: <n>`.

**`details` is flattened.** The relay's guard refuses any JSON key named `remote_url` anywhere, and keys are not scanned for secrets, so a free-form tree can't be sent as it is. Resonant turns it into rows whose keys are *values*: the root has an empty key, an object's key is its name, an array item is `[0]`, `[1]`, … and `kind` is `object`, `array`, `string`, `number`, `bool` or `null` (a string is cut at 200).

**Size.** After normalising, the panel's views are measured as compact JSON. Over `min(max_bytes, 32768)` is refused (`panel is 40123 bytes after normalising (max 24576)`). Rows and items cut for count mark the copy `truncated`; a cut string or a downsampled axis doesn't.

### Redaction and where words come from

Everything a panel prints is the extension's, not yours: it is marked `outside` on the dashboard. It passes the same redaction as every other text (credential-shaped text and the values of your declared secrets become `[redacted]`, Remote Control links are replaced) when it is stored **and** again every time a snapshot is built, so a stored copy can't leak what a later pattern catches. The brain's `read_panel` returns text from outside, so a task that reads it is treated as tainted (D6).

### Refresh

- The `idle` cron runs always. While the daemon's local time (in `tz`) matches the hours, days and months of `active`, **`active` runs instead of `idle`**, and the minute field of `active` sets how often. `active: "*/1 9-16 * * 1-5"` is every minute from 09:00 to 16:59 on weekdays and `idle` the rest of the time. Crons are five numeric fields (no names, no `@`); both fields of day-of-month and day-of-week restricted match either, as in cron; day-of-week 7 is Sunday.
- **Floor:** a panel can run at most once a minute, and never starts twice within 55 seconds. Each panel also runs **once at start-up**, about 30 seconds after the daemon starts, staggered by 3 seconds per panel, so the dashboard has data before the first cron time.
- **Pause.** While the kill switch is on, a panel doesn't run, and missed runs are not caught up or logged. The last copy stays.
- Runs share the scheduler's concurrency limit (`extensions.max_concurrent_jobs`). The audit has one `panel.run` row per good run (`ok`, `ms`, `bytes`, `truncated`) on top of the gate's own row, and `panel.failed`, `panel.denied`, `panel.killed`, `panel.dry_run` or `panel.skipped_paused` otherwise.

### Failure, stale and health

A failed run (non-zero exit, timeout, bad output, a missing secret) or a refused output **keeps the last good copy**, records the error and counts a streak. A panel is **stale** when its last run failed, it has no good copy yet, its extension isn't enabled, or it is later than its next expected run plus its timeout plus 120 seconds. The dashboard greys a stale panel and shows the error with the old copy. Three failures in a row (`panel <id> failed 3 times in a row`) turn the extension's `ext:<name>` health to degraded, like a job, and a good run clears it.

### Budgets

One panel is at most `min(max_bytes, 32 KiB)`. All panels in one snapshot are at most **100 KB**: over that, panels are dropped largest first (the oldest data among equals), each shown with its title and run state and "too big to send". The relay's own cap is 240 KB: when the snapshot is still over it, the pusher trims in this order: history, **panels** (largest first), the lists, detail, items (`trimmed` says how far).

### Helpers (stdlib only)

Extensions run as `python -I -S`, so the SDK isn't importable: copy `packages/resonant-sdk/src/resonant_sdk/panel.py` next to your script, like `emit.py`, and add the script's directory to `sys.path`.

```python
from panel import print_panel, stat, series, table, column, pill, link, status, kv, text, details, view
```

`stat`, `series`, `table` (with `column`, `pill`, `link`), `status`, `kv`, `text` and `details` build the widgets as dicts (None fields are left out, a `datetime` becomes ISO text and must have a timezone); `view(id, title, widgets)` builds a tab and `print_panel(views, updated_at=…, health=…, local_url=…)` prints the object.

### Example: a finance extension

```yaml
panels:
  - id: paper
    title: Paper trading
    description: Equity, PnL and open positions
    command: [./cli.py, panel, paper]
    refresh: {idle: "*/15 * * * *", active: "*/1 9-16 * * 1-5"}
    tz: America/New_York
    secrets: [alpaca_key]
    max_bytes: 24576
```

```python
# cli.py, `panel paper`
from panel import column, link, pill, print_panel, series, stat, table, view

equity, history, positions = load()   # your own code
print_panel(
    [view("overview", "Overview", [
        stat("Equity", equity.now, format="money", delta=equity.now - equity.open),
        stat("Day PnL", equity.day, format="money", health="ok" if equity.day >= 0 else "bad"),
        series("area", [t for t, _ in history], {"Equity": [v for _, v in history]},
               format="money", title="Equity today"),
        table(
            [column("sym", "Symbol"), column("qty", "Qty", "number"),
             column("pnl", "PnL", "money"), column("side", "Side", "pill"),
             column("act", "", "link")],
            [{"sym": p.sym, "qty": p.qty, "pnl": p.pnl,
              "side": pill(p.side, "ok" if p.pnl >= 0 else "bad"),
              "act": link("close", "close_position")} for p in positions],
            title="Positions",
        ),
    ])],
    local_url="http://127.0.0.1:8765/paper",
)
```

### Example: an on-call extension

```yaml
panels:
  - id: oncall
    title: On call
    command: [./cli.py, panel, oncall]
    refresh: {idle: "*/5 * * * *"}
```

```python
from panel import details, kv, print_panel, status, view

print_panel([view("now", "Now", [
    status([{"name": s.name, "state": s.state, "detail": s.note} for s in services]),
    kv({"open incidents": open_count, "on call": who}),
    details(last_incident),     # any JSON; shown as a collapsed tree
])], health="warn" if open_count else "ok")
```

### `read_panel`

The brain can read what a panel last drew: `read_panel {extension}` lists the extension's panels (`id`, `title`, `updated_at`, `ok`, `stale`, `error`) with the stat widgets of each first view as `{label, value, unit}`; `read_panel {extension, panel}` returns that panel's views in full. It reads the stored copy only and never runs anything. Its output is outside text, so the calling task is tainted.

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
- [ ] Every panel prints one JSON object with `schema: 1`, stays under its `max_bytes`, and is safe to run every minute (it only reads). Its `local_url` is loopback http, and nothing secret goes in a title or a cell.
- [ ] Every tool prints exactly one JSON object and exits 0 on success.
- [ ] Effects and levels are as low as safely possible, and `commit: true` is on irreversible actions.
- [ ] No secrets in the repo; secrets are declared and stored in the Keychain.
- [ ] Jobs are idempotent and safe to skip when paused.
- [ ] Events carry `dedupe_key`.
- [ ] Each approver holds the `approve` grant in `principals.yaml`.
- [ ] Anything that moves money or places orders runs inside your extension with its own risk controls. Core only sees read tools, events and the kill switch, as trade-jev does.

## 11. Deferred designs

Designed, not built. Each waits for a consumer or a prerequisite. Planned work is in Linear; known limits are in [`limits.md`](limits.md).

### Seatbelt sandbox for extension processes

> **Flag.** The owner's rule is that Claude workers stay unguarded and nobody proposes sandbox layers for them. This design is for **extension processes only**, not workers. Confirm with the owner before ticketing it.

It must land before any third-party or network source, and needs its own security review. `resonant/sandbox/` today has only the allow-default thin guard for workers. This adds a second template: a deny-default `extension` profile, opt-in per extension (`sandbox: true`), proven with `gh api` and the example extension, then on by default. It can reuse the thin guard's mach and port denies.

- Deny `lsopen`, `launchctl`, `open`, `shortcuts`, and `mach-lookup` of `securityd` / `com.apple.SecurityServer`.
- Deny `process-info` and `signal` on other processes.
- Deny loopback (Ollama `:11434`, the API `:7777`) and unix sockets, except mDNSResponder.
- Deny the pasteboard, Apple Events, and exec from the data dir or `TMPDIR`.
- Deny `file-read*` under `$HOME` outside the extension and data dirs.
- Go CLIs such as `gh` need `trustd` for TLS. `sandbox-exec` is deprecated but works; verify per macOS release.

**Done when.** A copied `security` binary, ctypes `SecItemCopyMatching` and a read of `~/Library/Mail` are all denied, and the github digest and the pulse reporters still run under it.

### Critical-job LaunchAgents and `job-guard`

Deferred with their only consumer (trade-jev). `ext add` refuses `critical: true`. Critical jobs must be Allow-only (L0, or L3 with a report). `job-guard` stays the parent (no exec) so it can redact the job's output; it checks `autonomy` and `paused`, the extension row, the job and the pin, and fails closed (exit 75). The command comes from the approved manifest snapshot, never the plist.

**Done when.** A critical job fires with the daemon down, is skipped while paused, and refuses a tampered copy.

### Git and network sources, Python deps

`git+https://…` sources and extensions with dependencies (a per-extension venv). They need the extension sandbox, clone hardening (no hooks, no submodules, pinned commits, never tags) and a `uv sync --locked` review first: install-time code must not run unreviewed.

### iMessage `/ext add|update|remove`

A terminal confirmation is enough for first-party code and keeps remote installs out of the threat model.

**One narrow exception** (agreed 2026-11-02, PER-275): an MCP tool whose server changed what it takes can ask for a text `yes`. That `yes` re-pins **one tool's input schema** and nothing else: never the manifest, a tool's effect or level, a URL, a secret or code. It is bound to the exact live definition it was asked about. `ext update` stays terminal-only, and it is what clears a `no`.

**Done when.** A reviewed design binds a remote approval to `{source, commit, tree_digest, manifest_sha}` with a typed confirmation. It ships only after the sandbox.

### MCP stdio servers: env-token binaries and secret-gated discovery

- A stdio server that reads its token only from an env var can't be used: secrets go on fd 4, never the env. Options: a per-server "env secrets" grant shown as an escalation, or the server's HTTP endpoint (bearer held in the daemon).
- `ext add` lists a stdio server's tools with no secrets, so one that needs its token to list tools can't be installed. Option: the http servers' ask-then-send, with the secrets on fd 4 after the owner confirms the command line.

**Done when.** Each is decided, and built or dropped. No server needs either today.

### Extension runtime extras

- **A local push endpoint** (an HTTP `emit()`) for long-running processes, instead of a job period (5 minutes at best). Loopback only, a per-extension Keychain token, the same `report()` payload and pulse path.
- Per-extension network allowlists (an `srt`-style proxy).
- A separate macOS user for extensions.
- CPU and memory limits.

### Veto windows for unattended jobs

"Doing X in 10 min unless you stop": a scheduled L2 returns `Veto`, parks in `waiting(veto)` with `wake_at` at the deadline, and runs unless the owner replies `stop`. `Veto` is in the gate contract and never returned today; asking jobs fire at most hourly, so asking works for now.

- A `stop` with `occurred_at` ≤ the deadline wins even if ingested after it, and so does one within a short late-stop grace if the call hasn't started.
- An ambiguous `stop` (several vetoes pending) converts them all to asks.
- A veto may run only if its prompt was recorded as sent at least the window before. Late (daemon down past the window) or under `/kill`, it converts to an ask.

**Done when.** A job's L2 action runs after its window with no reply, never after `stop` or `/kill`, and a missed window asks.

### Approval classifier and `resonant eval approvals`

The parser covers the replies people send. For the ones it misses ("sure go ahead", "nah"), a small model call that sees only the reply and the code-built summary, approves only at confidence ≥ 0.9, and never for L1 or pay. `resonant eval approvals` scores it on `evals/approvals.example.jsonl` and a real set in `~/.resonant/evals/`.

**Done when.** Misses get a classifier decision or a re-ask, never a wrong approval, and the eval reports precision on approvals.
