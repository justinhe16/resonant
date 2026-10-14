# github extension

Read-only GitHub for Resonant: your open pull requests with CI and review state, a watch that texts you when CI breaks or recovers or someone asks for your review, and a morning digest line for the briefing. It is stdlib Python plus the [`gh`](https://cli.github.com) CLI (`/opt/homebrew/bin/gh` on the Mini). Writes (`rerun_failed`, `merge_pr`) come later.

| Name | Kind | Level | What it does |
|---|---|---|---|
| `prs` | tool | L0 | Open PRs you authored (CI rollup, failing checks, review decision), plus PRs awaiting your review. |
| `pr {repo, number}` | tool | L0 | One PR with its checks, mergeability and size. |
| `watch` | job, every 10 min | L0 | Emits `ci_failed`, `ci_fixed` and `review_requested` events. |
| `digest` | job, 07:50 | L0 | Emits one `digest` event; the core briefing includes its text. Not notified. |
| health | every 30 min | | `gh api user` succeeds. |

## Token

A **fine-grained personal access token**, read-only, limited to the repos you want watched:
- Repository permissions: Metadata, Contents, Pull requests, Checks and Actions, all **read**.

Store it on the Mini, in its terminal: `resonant secret set github/token`. Resonant passes it to this extension as `RESONANT_SECRET_TOKEN`, and the extension passes it to `gh` as `GH_TOKEN` in `gh`'s environment only. It never uses `gh`'s own login, and never prints the token.

## Config

Optional: `~/.resonant/ext-data/github/config.json`.

```json
{"author": "@me", "repos": ["justinhe16/resonant"]}
```

- `author`: whose PRs to list; `@me` (the default) is the token's user. Review requests are for the same user.
- `repos`: limit everything to these repos. Empty or missing means every repo the token can see.

## How `watch` decides

It keeps no state. Each run reads the current open PRs and their branch's workflow runs, and, per workflow:
- The latest finished run on the PR's head failed: `ci_failed`, key `<repo>#<n>:run:<run id>:failed`, text `✗ resonant#40 CI failed (<workflow>)`.
- It succeeded and the previous finished run of that workflow failed: `ci_fixed`, key `<repo>#<n>:run:<run id>:fixed`, text `✓ resonant#40 CI fixed`.
- You're a requested reviewer: `review_requested`, key `<repo>#<n>:<head sha>:review`.

Cancelled, skipped and in-progress runs are ignored. The same state always produces the same keys, so Resonant's events table (unique per key) drops repeats, and the first run after install is a silent baseline. Events go to fd 3 when Resonant provides it, otherwise into the result's `events` list.

```sh
RESONANT_SECRET_TOKEN=... ./cli.py prs     # by hand, with a token in the env
```
