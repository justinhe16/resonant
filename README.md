# Resonant

An always-on agent harness that runs as a daemon on a dedicated Mac. It takes events in from iMessage, Slack, webhooks and schedules, and routes simple requests to a local LLM. Hard reasoning and coding go to Claude Code. Every side effect passes an approval gate. Product-specific behaviour lives in **extensions**: separate repos that plug in through a `resonant.yaml` manifest.

> Status: Phase 1 (talk to it) is built: with `channels.imessage.enabled`, you text Resonant over iMessage and the local model answers with read-only tools. It stays off by default until the [go-live checklist](docs/go-live.md) passes. Phase 2 (built) adds the real gate, text-reply approvals and the kill switch, plus notes, reminders, Mini ops (`/restart`, `/update`) and `/projects`, by slash command or in plain language. Phase 3 (built) adds first-party extensions installed from the Mini's terminal (`resonant ext add`), Keychain secrets, scheduled jobs and notifications, a daily briefing, and the `github` extension (`/github prs`, CI alerts); see go-live [step 12](docs/go-live.md#12-phase-3-extensions-jobs-and-the-briefing). Phase 3.5 (built) adds the pulse: extensions report items, urgent ones text at once (any hour), normal ones arrive in an hourly "btw", and everything open is under Updates in the 08:00 briefing, each told once per change (`/pulse`, `/pulse now`); see [step 13](docs/go-live.md#13-phase-35-the-pulse) and the [plan](docs/pulse-plan.md).

**Architecture:** open [`docs/architecture.html`](docs/architecture.html) for an interactive diagram, or read [`docs/architecture.md`](docs/architecture.md). The frozen contracts are in [`docs/interfaces.md`](docs/interfaces.md).

## Quick start (fresh Mac)
```sh
git clone https://github.com/justinhe16/resonant && cd resonant
scripts/bootstrap.sh          # deps, venv, ~/.resonant, LaunchAgents, local model, then manual steps
resonant status               # or: .venv/bin/resonant status
```

Then work through the **[go-live checklist](docs/go-live.md)** to get from a running daemon to texting Resonant over iMessage. `scripts/bootstrap.sh --check` verifies it without changing anything.

## Principles
- The model never sees secrets. The executor injects them from the macOS Keychain at runtime.
- A tool's effect tier (`read | write | pay`) comes from its registration, never from the model.
- Code computes numbers. The LLM narrates.
- All state is persisted in SQLite. Tasks are idempotent and resume after a crash.
- Every action is in the audit log.
- There are no public ports. The dashboard and API are loopback only. Remote status access is planned through a push-only status relay ([follow-up](docs/followups.md#status-relay-dead-man-switch-and-remote-dashboard)), never by exposing the API.
- Core stays generic. Anything only one use case needs belongs in that extension.

## Layout
| Path | What |
|---|---|
| `packages/resonant` | Daemon, CLI, store, gate, and the rest of core |
| `packages/resonant-sdk` | The only package extensions depend on: manifest schema, tools, events |
| `extensions/example` | A minimal example extension (`resonant.yaml`) |
| `extensions/github` | GitHub PRs and CI: read tools, a CI watch, the morning summary, two approved writes |
| `config/*.example.yaml` | Example config. Copy it to `~/.resonant/` |
| `scripts/bootstrap.sh` | Takes a fresh Mac to a running daemon |
| `evals/*.example.jsonl` | Synthetic eval sets. Real sets live in `~/.resonant/evals/` |
| `docs/` | Architecture, interface contracts, and the [go-live checklist](docs/go-live.md) |

## Development
```sh
uv sync
uv run pre-commit install
uv run pytest && uv run ruff check && uv run pyright
```

## License
Apache-2.0
