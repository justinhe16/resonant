# Resonant

An always-on agent harness that runs as a daemon on a dedicated Mac. It takes events in from iMessage, Slack, webhooks and schedules, and routes simple requests to a local LLM. Hard reasoning and coding go to Claude Code. Every side effect passes an approval gate. Product-specific behaviour lives in **extensions**: separate repos that plug in through a `resonant.yaml` manifest.

> Status: Phase 0 (skeleton). Not usable yet.

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
| `config/*.example.yaml` | Example config. Copy it to `~/.resonant/` |
| `docs/` | Architecture and interface contracts |

## Development
```sh
uv sync
uv run pre-commit install
uv run pytest && uv run ruff check && uv run pyright
```

## License
Apache-2.0
