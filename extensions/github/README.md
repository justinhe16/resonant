# github extension

GitHub for Resonant: your open pull requests with CI and review state, a watch that texts you when CI breaks or recovers or someone asks for your review, a morning summary that appears in the briefing's Updates section, and two approved writes: re-run failed CI and merge. It is stdlib Python plus the [`gh`](https://cli.github.com) CLI (`/opt/homebrew/bin/gh` on the Mini).

| Name | Kind | Level | What it does |
|---|---|---|---|
| `prs` | tool | L0 | Open PRs you authored (CI rollup, failing checks, review decision), plus PRs awaiting your review. |
| `pr {repo, number}` | tool | L0 | One PR with its checks, mergeability and size. |
| `rerun_failed {repo, number}` | tool | L3 | Re-runs the failed jobs of each workflow whose latest run on the PR's head failed. |
| `merge_pr {repo, number, head_sha}` | tool | L1, commit | Squash-merges the PR if its head is still `head_sha`. Always asks `yes <code>`. |
| `watch` | job, every 10 min | L0 | Emits `ci_failed`, `ci_fixed` and `review_requested` events. |
| `digest` | job, 07:50 | L0 | Reports the pulse item `github/summary` (`3 open PRs, 1 failing CI, 2 awaiting your review`, urgency `low`, state = the three counts). It shows under the briefing's Updates once per change and never texts on its own. Needs the manifest's `report: {max_urgency: low}` grant. |
| health | every 30 min | | `gh api user` succeeds. |

## Token

A **fine-grained personal access token**, read-only, limited to the repos you want watched:
- Repository permissions: Metadata, Contents, Pull requests, Checks and Actions, all **read**.

The two write tools need more, and **only** they do. Leave the token read-only if you don't want them: they then fail cleanly (`Not done: … the token can't do this: it needs …; nothing was changed`) and everything else keeps working. To enable them, edit the token and add:
- `rerun_failed`: Actions **read and write**.
- `merge_pr`: Contents **read and write** and Pull requests **read and write**.

Store it on the Mini, in its terminal: `resonant secret set github/token`. Resonant passes it to this extension on a pipe (fd 4, `RESONANT_SECRETS_FD`), never in its environment or argv. The extension writes it to a `hosts.yml` (0600) in a fresh private (0700) `GH_CONFIG_DIR` under the data dir's `tmp/` and points `gh` at it, so `gh` has no token in its env or argv; the dir is removed when the run ends (and any left by a killed run is swept next time). It never uses `gh`'s own login, and never prints the token.

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

Cancelled, skipped and in-progress runs are ignored. A re-run keeps its run id, so a failed run that passes on re-run sends no `fixed`. The same state always produces the same keys, so Resonant's events table (unique per key) drops repeats, and the first run after install is a silent baseline. Events go to fd 3 when Resonant provides it, otherwise into the result's `events` list.

## The writes

`rerun_failed` looks at the PR's current head commit and, per workflow, at its latest run on that commit. Each failed one (failure, timed out, startup failure) gets `gh run rerun <id> --failed`. A workflow that's queued or running, including one already re-running, is left alone. If there's nothing to re-run it fails with `nothing to rerun on resonant#40 (a1b2c3d): …` and changes nothing.

`merge_pr` takes the head SHA you're approving (the `pr` tool shows it), so the approval binds to that exact commit. It refuses, changing nothing, when:
- the PR's head is no longer that SHA (new commits since you approved),
- any check is failing or still running,
- the PR is a draft, closed or already merged,
- GitHub says it isn't mergeable: conflicts, behind its base, blocked by branch protection, or not yet computed.

Otherwise it runs `gh pr merge <n> --repo <repo> --squash --match-head-commit <sha>`, so GitHub itself refuses if the head moves in between. It always squashes and never passes `--admin` or `--auto`: branch protection always applies.

```sh
# by hand, the token goes on fd 4 like Resonant does it:
RESONANT_SECRETS_FD=4 ./cli.py prs 4< <(printf '{"token": "%s"}' "$GITHUB_TOKEN")
```
