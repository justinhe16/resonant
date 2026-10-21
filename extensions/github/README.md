# github extension

GitHub for Resonant: your open pull requests with CI and review state, a quiet watch that feeds Resonant's hourly progress rollup (merges, PRs that went quiet, review requests), a morning summary that appears in the briefing's Updates section, and two approved writes: re-run failed CI and merge. It is stdlib Python plus the [`gh`](https://cli.github.com) CLI (`/opt/homebrew/bin/gh` on the Mini). The manifest declares `bins: [gh]`, so Resonant pins `gh`'s real path when you install the extension (it must be a Homebrew install the sandbox guard protects, or root-owned) and passes it as `RESONANT_BIN_GH`; `gh` is never looked up on `PATH`. After `brew upgrade gh` moves it, run `resonant ext update github` to pin the new one.

| Name | Kind | Level | What it does |
|---|---|---|---|
| `prs` | tool | L0 | Open PRs you authored (CI rollup, failing checks, review decision), plus PRs awaiting your review. |
| `pr {repo, number}` | tool | L0 | One PR with its checks, mergeability and size. |
| `recent_prs {repo}` | tool | L0 | The newest 15 PRs in one repo (`owner/name`), any state, newest first, with created, merged and closed dates and a `latest merged #N` summary. `prs` lists only open PRs and `pr` needs a number, so "what are the latest PRs?" had no tool (BR-9). |
| `rerun_failed {repo, number}` | tool | L3 | Re-runs the failed jobs of each workflow whose latest run on the PR's head failed. |
| `merge_pr {repo, number, head_sha}` | tool | L2, commit | Squash-merges the PR if its head is still `head_sha`. Runs when you ask for it; otherwise asks a plain yes. |
| `watch` | job, every 10 min | L0 | Emits `ci_failed`, `ci_fixed`, `review_requested`, `pr_active`, `pr_merged` and `pr_closed` events. None of them texts on its own (GH-2): the hourly rollup reads them. |
| `digest` | job, 07:50 | L0 | Reports the pulse item `github/summary` (`3 open PRs, 1 failing CI, 2 awaiting your review`, urgency `low`, state = the three counts). It shows under the briefing's Updates once per change and never texts on its own. When all three counts are 0 it is reported `quiet` (BF-1), so the briefing leaves it out instead of saying `0 open PRs, 0 failing CI, 0 awaiting your review`. Needs the manifest's `report: {max_urgency: low}` grant. |
| health | every 30 min | | `gh api user` succeeds. |

## Token

A **fine-grained personal access token**, read-only, limited to the repos you want watched:
- Repository permissions: Metadata, Contents, Pull requests, Checks and Actions, all **read**.

The two write tools need more, and **only** they do. Leave the token read-only if you don't want them: they then fail cleanly (`Couldn't merge PR #12 in owner/repo: the github token needs … access (see the github extension README). Nothing changed.`) and everything else keeps working. To enable them, edit the token and add:
- `rerun_failed`: Actions **read and write**.
- `merge_pr`: Contents **read and write** and Pull requests **read and write**.

Store it on the Mini, in its terminal: `resonant secret set github/token`. Resonant passes it to this extension on a pipe (fd 4, `RESONANT_SECRETS_FD`), never in its environment or argv. The extension writes it to a `hosts.yml` (0600) in a fresh private (0700) `GH_CONFIG_DIR` under the data dir's `tmp/` (`resonant-gh-*`) and points `gh` at it, so `gh` has no token in its env or argv; the dir is removed when the run ends (and any left by a killed run is swept next time, only real 0700 directories of ours older than an hour). It never uses `gh`'s own login, and never prints the token.

## Config

Optional: `~/.resonant/ext-data/github/config.json`.

```json
{"author": "@me", "repos": ["justinhe16/resonant"]}
```

- `author`: whose PRs to list; `@me` (the default) is the token's user. Review requests are for the same user.
- `repos`: limit everything to these repos. Empty or missing means every repo the token can see.

## Quiet by default (GH-2)

The manifest has no `notify:` block, so nothing this extension emits texts you right away. Instead:
- The hourly pulse's `Workers:` section is a progress rollup (`resonant/extensions/rollup.py`): `Merged: … dori#40 fix login`, `Looks like dori#41 cache layer (~5h quiet) got dropped. You should take a look.`, `Review requested: app#9 bump deps`, and your recently active PRs under `Still working on:`. Each merge, review request and stall is said once; a stall again only after new activity and a new quiet stretch (`pulse.stall_after`, default `4h`). A PR that was already quiet when you installed the extension (or when you updated to this version) is never announced, nor is a draft. If `watch` hasn't succeeded in the last 30 minutes (expired token, `gh` gone, paused), the rollup says nothing about which PRs are active or quiet rather than guess.
- CI is on demand: `/github prs`, "any of my PRs failing CI?", `show PR 40 in justinhe16/resonant`.
- The 08:00 briefing still has the `github/summary` line, except when every count is 0 (then it is left out). After updating to the BF-1 version, run `resonant ext update github` so the installed copy reports the all-zero summary as quiet.

To get a text again for one kind of event, teach a trust rule: "text me right away when CI fails in resonant" makes a `notify_now` rule with action `ci_failed` and scope `resonant` (the repo's registered name, its `owner/name` or its name), or scope `github` for every repo. Only a rule naming the event type counts: a `*` action never wakes these events. "just put dori's CI fixes in the digest" (`notify_digest`) does the reverse for a type that texts. `/rules` lists them and `/rules forget <n>` removes one. After updating to this version, run `resonant ext update github` on the Mini (it re-pins the new manifest).

## How `watch` decides

It keeps no state. Each run reads the current open PRs and their branch's workflow runs, and, per workflow:
- The latest finished run on the PR's head failed: `ci_failed`, key `<repo>#<n>:run:<run id>:failed`, text `✗ resonant#40 CI failed (<workflow>)`.
- It succeeded and the previous finished run of that workflow failed: `ci_fixed`, key `<repo>#<n>:run:<run id>:fixed`, text `✓ resonant#40 CI fixed`.
- You're a requested reviewer: `review_requested`, key `<repo>#<n>:<head sha>:review`, data `{repo, number, title, url}`.

And for the rollup:
- Each open PR of yours: `pr_active`, key `<repo>#<n>:active:<active_at>`, data `{repo, number, title, url, active_at, draft}`. `active_at` is when it last moved: its `updatedAt` (commits, comments, reviews) or its latest check start, finish or status, whichever is later. A new commit or a finished check makes a new key.
- Your PRs closed in the last two days (one more search, at most 20): `pr_merged`, key `<repo>#<n>:merged` (data adds `merged_at`), or `pr_closed`, key `<repo>#<n>:closed:<closed at>`.

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
