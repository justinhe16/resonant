# Writing a Resonant extension

An extension teaches Resonant about one product or domain, such as a trading bot, an on-call rotation, or a home lab, without changing the core. It is usually its own repo containing a `resonant.yaml` manifest and the scripts or servers the manifest points at. The core never imports extension code. It reads the manifest, runs your commands as separate processes, and enforces policy around them.

> **Status.** The manifest schema is frozen (v1), and `resonant ext validate` works today. Registration (`resonant ext add`), scheduling, tool exposure and notifications are built in Phase 3. This guide describes the full contract so you can write extensions now.

## 1. The smallest extension

```
my-ext/
├─ resonant.yaml
└─ cli.py          # prints one JSON object on stdout
```

```yaml
name: my-ext          # slug: lowercase, digits, dashes
version: 1            # manifest schema version (only 1 exists)
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

## 2. Depending on the SDK

Extensions depend only on `resonant-sdk`, never on the daemon package:

```toml
# pyproject.toml of your extension
dependencies = ["resonant-sdk @ git+https://github.com/justinhe16/resonant#subdirectory=packages/resonant-sdk"]
```

The SDK gives you `Manifest` and `load_manifest` for validating in your own CI, `ToolSpec`, `Effect`, `Level`, `Event`, and later `emit()` and MCP helpers (Phase 3). Plain-CLI extensions don't need the SDK at all.

## 3. Manifest reference (`version: 1`)

Unknown keys are errors. YAML is parsed so that only `true`/`false` are booleans: `on:`, `yes:` and `no:` stay strings.

| Key | Type | Meaning |
|---|---|---|
| `name` | slug | Unique extension id. It namespaces tools, jobs, events and secrets. |
| `version` | `1` | Schema version. |
| `uses` | slug, optional | A first-party extension this one configures. It allows exactly one extra block with that name. |
| `approvers` | list of principals | Who may approve this extension's L1/L2 actions. Each must hold the `approve` grant for this extension in `principals.yaml`. |
| `jobs` | list | Scheduled commands, described in §6. |
| `tools` | map | Command-backed tools, described in §4. |
| `mcp` | object, optional | An MCP server and the typed list of tools it may expose, described in §4. |
| `notify` | `{on: [event…], channel: imessage\|slack}` | Which of your emitted events are forwarded to people. |
| `health` | `{check: <cmd>, every: 30s\|5m\|1h}` | A health command. A zero exit code means healthy. |
| `secrets` | list of names | Keychain secret names your tools may receive. Every tool's `secrets` must be listed here. |

## 4. Tools

Every tool declares an **effect**, which says what it can change, and optionally a **level**, which says how much autonomy Resonant has with it. **The model never chooses these. They come only from your manifest.**

| Effect | Default level | Rules |
|---|---|---|
| `read` | L0 | A read with no secrets is **always L0**. A read that uses secrets may be set higher. |
| `write` | L1 | You may set L0–L3. |
| `pay` | L1 | **Always L1, permanently.** No override, ever. Approval requires an exact `yes <code>` reply. |

| Level | Behavior |
|---|---|
| L0 | Act silently. Use it for reads and triage. |
| L1 | Draft first, then an approver must approve by text reply. |
| L2 | Act after a veto window ("doing X in N min unless you stop") unless someone stops it. |
| L3 | Act, then report. Use it for reversible actions with a small blast radius. |

Pick the lowest level you'd be comfortable with if the model misunderstood a request. Mark irreversible, user-visible actions (send, pay, delete) with `commit: true`.

### CLI tools (default)

```yaml
tools:
  get_pnl:
    description: P&L for a trading day.
    run: ./cli pnl --date {date} --json     # {placeholders} must be declared in args
    effect: read
    args: {date: string}                    # string | integer | number | boolean
    timeout_s: 30
  restart_worker:
    run: ./cli restart
    effect: write
    level: L3
    secrets: [api_token]                    # must also appear in top-level secrets
```

Contract:
- The command runs in your extension directory with arguments substituted. It is never run through a shell.
- It must print **one JSON object** on stdout and exit 0 on success. A non-zero exit or invalid JSON is a tool error.
- Secrets arrive as environment variables, for that process only. The model never sees them.
- Calls are idempotent per (task, step, intent). Resonant won't re-run a call that already succeeded.

### MCP tools

Use an MCP server for stateful or long-running tools (a warm browser, a database connection) or when you have many tools. MCP servers don't declare effects, so **you list every tool you want exposed, with its effect**. Unlisted tools are refused.

```yaml
mcp:
  command: uv run my-mcp-server
  tools:
    search_mail: {effect: read}
    send_mail:   {effect: write, level: L1, commit: true}
```

## 5. Secrets

- Declare secret names in `secrets:` and give each tool the ones it needs.
- Store values in the macOS Keychain on the Mini: `security add-generic-password -s resonant -a <ext>/<name> -w`
- Only the executor reads them, and only at call time.
- Never put secrets in the manifest, in `.env`, or in your repo. The Resonant repo runs gitleaks, and yours should too.

## 6. Jobs

```yaml
jobs:
  - id: daily-session
    schedule: "15 6 * * 1-5"       # 5-field cron
    tz: America/Los_Angeles        # default: the daemon's timezone
    critical: true                 # its own LaunchAgent; fires even if the daemon is down
    run: ./run_session.sh
```

- **Normal jobs** run on the daemon's scheduler.
- **Critical jobs** get their own LaunchAgent and are wrapped as `resonant job-guard --job <ext>/<id> -- <run>`. That wrapper **refuses to run while the kill switch is engaged**, and also when it can't read the paused flag (it fails closed, exit 75). Make critical jobs idempotent and safe to skip.

## 7. Events and notifications

Your processes report what happened by emitting events. Phase 3 adds the `emit()` helper and the local endpoint behind it.

```python
from resonant_sdk import emit                 # Phase 3
emit("fill", {"symbol": "ES", "qty": 1}, dedupe_key="fill:123")
```

- Events arrive as `ext.<name>.<event>`. Core sets the principal, so extensions can never claim one.
- `notify.on` chooses which events are forwarded to people over `channel`.
- Use `dedupe_key` so retries don't double-notify.

## 8. Permissions and approvers

`principals.yaml` decides who can do what with your extension:

```yaml
principals:
  justin:    {role: owner, identities: ["imessage:+15551234567", "imessage:me@icloud.com"]}
  cofounder: {identities: ["slack:U02"], grants: {my-ext: [request, approve]}}
```

- `request` allows asking Resonant to use the extension's tools or start its tasks.
- `approve` allows approving the extension's L1/L2 actions. The principal must also be listed in `approvers:`.
- The owner can always do everything. Identity grants permissions; message content never does.

## 9. Registering an extension

```sh
resonant ext validate ./my-ext     # works today: schema, invariants, approver grants
resonant ext add ./my-ext          # Phase 3
```

`ext add` (Phase 3) runs these steps:
1. Validates the manifest.
2. Shows a **diff** of the jobs, tools with their effects and levels, secrets, and approvers.
3. Asks the owner to approve. Registration is an L1 action.
4. Records the path in `~/.resonant/extensions.yaml`.

On daemon start or `resonant reload`, the extension is materialized:
- jobs go to the scheduler or to LaunchAgents
- tools go to the registry
- `notify` becomes event subscriptions
- `health` becomes periodic checks and dashboard tiles

**Changing a manifest later.** Adding a `write`/`pay` tool, a secret or an approver, or raising a level, requires approval again. Removing things, or lowering a level, doesn't.

## 10. Checklist

- [ ] `resonant ext validate` passes, and your CI validates with `resonant_sdk.load_manifest`.
- [ ] Every tool prints exactly one JSON object and exits 0 on success.
- [ ] Effects and levels are as low as safely possible, and `commit: true` is on irreversible actions.
- [ ] No secrets in the repo; secrets are declared and stored in the Keychain.
- [ ] Critical jobs are idempotent and safe to skip when paused.
- [ ] Events carry `dedupe_key`.
- [ ] Each approver holds the `approve` grant in `principals.yaml`.
- [ ] Anything that moves money or places orders runs inside your extension with its own risk controls. Core only sees read tools, events and the kill switch, as trade-jev does.
