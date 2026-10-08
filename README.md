# Resonant

An always-on agent harness that runs as a daemon on a dedicated Mac. It takes events in from iMessage, Slack, webhooks and schedules, and routes simple requests to a local LLM. Hard reasoning and coding go to Claude Code. Every side effect passes an approval gate. Product-specific behaviour lives in **extensions**: separate repos that plug in through a `resonant.yaml` manifest.

> Status: Phase 0 (skeleton) is done. The daemon runs, but it has no channels or runners yet. Phase 1 adds iMessage and the local model.

**Architecture:** open [`docs/architecture.html`](docs/architecture.html) for an interactive diagram, or read [`docs/architecture.md`](docs/architecture.md). The frozen contracts are in [`docs/interfaces.md`](docs/interfaces.md).

## Quick start (fresh Mac)
```sh
git clone https://github.com/justinhe16/resonant && cd resonant
scripts/bootstrap.sh          # deps, venv, ~/.resonant, LaunchAgents, local model, then manual steps
resonant status               # or: .venv/bin/resonant status
```

## Principles
- The model never sees secrets. The executor injects them from the macOS Keychain at runtime.
- A tool's effect tier (`read | write | pay`) comes from its registration, never from the model.
- Code computes numbers. The LLM narrates.
- All state is persisted in SQLite. Tasks are idempotent and resume after a crash.
- Every action is in the audit log.
- There are no public ports. The dashboard is reachable only through `tailscale serve`.
- Core stays generic. Anything only one use case needs belongs in that extension.

## Layout
| Path | What |
|---|---|
| `packages/resonant` | Daemon, CLI, store, gate, and the rest of core |
| `packages/resonant-sdk` | The only package extensions depend on: manifest schema, tools, events |
| `extensions/example` | A minimal example extension (`resonant.yaml`) |
| `config/*.example.yaml` | Example config. Copy it to `~/.resonant/` |
| `scripts/bootstrap.sh` | Takes a fresh Mac to a running daemon |
| `evals/*.example.jsonl` | Synthetic eval sets. Real sets live in `~/.resonant/evals/` |
| `docs/` | Architecture and interface contracts |

## Development
```sh
uv sync
uv run pre-commit install
uv run pytest && uv run ruff check && uv run pyright
```

## License
Apache-2.0
