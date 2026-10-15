# Phase 5a plan: the Seatbelt sandbox and the Claude runner

Status: **approved, building** (2026-10-15). The owner approved going ahead ("execute!") before this write-up; the decisions in §1 are theirs, the rest is the design. Built with robot-surf: a worktree per ticket, tests, PR, CI, code-review agent, squash-merge to `main`, in the dependency waves of §4. Slack (Phase 5b) is out of scope. The plan had a security self-review (escape paths: persistence files, launchd, planted git config and hooks, symlinks into the run dir, token exposure, the owner's `~/.claude` hooks); each ticket's PR still gets the code-review agent, and P5A-1's PR a dedicated sandbox review.

**Goal.** The owner texts "fix the flaky test in resonant" from anywhere. A Claude Code session on the Mini does the work in a throwaway clone, inside a macOS sandbox, and Resonant texts back a PR link. Merging is the one approval. While it runs, the hourly pulse says it is still going; `/runs` shows it; `/kill` stops it.

The sandbox is **a wall, not a gate**: no new approvals, a few milliseconds per process, and Claude runs with permission checks off inside it. It closes the gap in the [trust model](architecture.md#trust-model): "same-user processes can run `resonant approvals approve`, write `resonant.db` and read the Keychain". With it, a Claude session (or, in P5A-5, an extension process) can do none of the three, whatever a malicious repo file talks it into.

---

## 1. Owner decisions

| # | Question | Decision |
|---|---|---|
| Q1 | Approvals | None to **start** a run (an owner request is the go-ahead). The only approval is the merge: `/merge R3` is explicit and runs; "merge it" in chat asks a plain `yes` (L2, as every model-chosen L2). |
| Q2 | Speed vs caution | Speed inside the wall: `--permission-mode bypassPermissions`, outbound network open, localhost dev servers allowed, shared package caches. Caution at the wall: the denies in D1. |
| Q3 | Repos | Only repos listed in `runner.repos`. **Empty by default**; the owner names them (§6). |
| Q4 | Progress | A `normal` pulse item while a run is going (the hourly `btw` mentions it, cleared when done). Start, finish and failure are direct replies to the request, sent at once, so nothing needs `urgent`. |
| Q5 | Concurrency | 2 runs at once (`claude.max_concurrent: 3` with 1 slot reserved for on-call). |
| Q6 | Limits | 45 minutes wall time per run (`runner.max_minutes`), then the process group is killed and the run fails with what it has. |

## 2. How a run flows

```
iMessage "fix the flaky pulse test in resonant"
  → router: intent run_project → local runner → tool start_run{repo: "resonant", ask: "..."}  (L3, owner only)
      → reply "On it: R3 · resonant. Up to 45m. /runs to check, /cancel R3 to stop."
      → inserts a queued task {kind: "run", runner: "claude"}  (same transaction)
  → ClaudeRunner (takes a Claude slot):
      1. workspace: refresh the daemon's bare mirror (token, outside the sandbox),
         clone it --no-hardlinks into ~/resonant-runs/R3/repo, branch resonant/r3-<slug>
      2. sandbox-exec -f runner.sb  claude -p "<ask>" --session-id <uuid> ...   (≤ 45m)
      3. sandbox-exec -f runner.sb  git bundle create out.bundle <base>..HEAD
      4. daemon (outside): git fetch out.bundle into the mirror, checks, push with the token,
         gh pr create
      → reply "R3 done: PR #41 · <url> · 3 files. /merge R3 when CI is green."
  → pulse item core/run/R3 cleared; workspace deleted after 7 days or on merge
```

The GitHub token **never enters the sandbox**, and the daemon **never runs git inside the clone**: it only reads a bundle file with its own mirror's config (D3).

---

## 3. Design

### D1. The sandbox (`resonant/sandbox/`)

`sandbox-exec -f <profile> -D KEY=VALUE … <argv>` with a profile generated from a template and parameters (no string-built paths in SBPL: every path goes in as a `-D` parameter and is checked to be absolute and normalized). `sandbox-exec` is deprecated but present on macOS 27.0.1; `resonant sandbox check` (D6) re-proves it after every OS update.

Two profiles from one template:

| | `runner` (Claude sessions) | `extension` (P5A-5) |
|---|---|---|
| Base | `(allow default)`, then the denies below. Deny-default breaks too much of a dev toolchain (node, uv, compilers each need dozens of mach services), and the risks that matter are enumerable. | `(deny default)` with an explicit allow list. Extensions are small, stdlib Python plus `gh`/`git`. |
| File write | **Deny everywhere**, then allow: the run dir (`repo/`, `home/`, `tmp/`), the shared cache dir, `/dev/null`, `/dev/tty*`. Not the per-user `/private/var/folders/…` dirs (other apps' caches live there); the spike adds only a `…/T/` subpath if a tool proves to need it. | Its data dir and its `tmp/` only. |
| File read | Deny `$HOME`, then allow: the run dir, the cache dir, toolchain dirs (`runner.sandbox.read_paths`: `~/.local/bin`, `~/.local/share/claude`, `~/.local/share/uv`, `~/.cargo`, `~/.rustup`, `~/.nvm`, `~/.bun`, configurable). Always denied even if listed: `~/.resonant`, `~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.claude`, `~/Library` (Keychains, Messages, cookies), the owner's own checkouts. | The extension's pinned dir, its data dir, the interpreter's install. |
| Exec | Deny `/usr/bin/security`, `/usr/bin/osascript`, `/usr/bin/open`, `/bin/launchctl`, `/usr/bin/shortcuts`, `/usr/bin/crontab`, `/usr/bin/at`, `/usr/bin/sudo`, `/usr/sbin/screencapture`, the `resonant` entry point and the daemon's venv. | Allow only argv[0] from the Phase 3 allowlist, plus what `python3`/`gh`/`git` exec. |
| Mach / IPC | Deny `mach-lookup` of `com.apple.SecurityServer`, `com.apple.securityd*` (Keychain), `com.apple.coreservices.launchservicesd` and `com.apple.lsd.*` (open apps), `com.apple.pasteboard.*`, `com.apple.tccd*`; deny `appleevent-send`; deny `signal` and `process-info*` on processes outside the sandbox. Keep `com.apple.trustd*` (TLS for Go and Apple git). | Same denies by default; allow `trustd` and `mDNSResponder`. |
| Network | Outbound allowed. Loopback allowed **except** ports 7777 (API), 11434 (Ollama), 22 (ssh), 5900 (screen sharing), on both `127.0.0.1` and `::1`. Unix sockets denied except `mDNSResponder`. | Outbound TCP 443 and DNS only; no loopback; no unix sockets but `mDNSResponder`. |

**Why these close the three gaps.** `resonant.db` and the config are under `~/.resonant` (read and write denied). The `resonant` CLI is denied to exec and couldn't do anything anyway without the DB or the API port. The Keychain is unreachable without `securityd` (a copied `security` binary fails the same way). The sandbox is inherited by every child, so `bash`, `node` and `git` all stay inside.

**Persistence escapes are covered by the write allow-list**: `~/Library/LaunchAgents`, `~/.zshrc`, `~/.gitconfig`, `~/.claude/settings.json`, the owner's repos' `.git/hooks`, cron tabs. `launchctl submit` (which would start a process outside the sandbox) is denied twice: exec of `launchctl`, and a deny test runs a copied binary to prove launchd refuses a sandboxed submit (if it doesn't, the profile adds `(deny mach-lookup (global-name-regex #"^com\.apple\.xpc\.launchd"))`, verified in the P5A-1 spike).

**Environment** (built from scratch, as `scratch_env`): `PATH` (runner PATH), `HOME=<run>/home`, `TMPDIR=<run>/tmp`, `CLAUDE_CONFIG_DIR=<run>/home/.claude`, `LANG`, `UV_CACHE_DIR`/`npm_config_cache`/`PIP_CACHE_DIR` pointing at the shared cache, and `CLAUDE_CODE_OAUTH_TOKEN` (runner only). Nothing from `os.environ`; no `SSH_AUTH_SOCK`.

**The spike comes first** (P5A-1, before anything is built on it): on the Mini, prove `claude -p "say hi"` authenticates from the env token with Keychain mach-lookup denied; `git clone/commit`, `gh api` (in the extension profile), `uv run pytest`, `npm ci` from the shared cache, a `python -m http.server 8123` reachable on loopback, and HTTPS to github.com all work. It also confirms SBPL's last-match-wins ordering for the `$HOME` deny-then-allow reads, and that Ollama and the API listen on loopback only. Any surprise changes the profile, and the spike's findings go into this plan's As built.

### D2. Claude Code invocation

```
claude -p "<ask>" --session-id <uuid> --output-format stream-json --verbose
  --permission-mode bypassPermissions --strict-mcp-config --setting-sources project
  --append-system-prompt "<runner prompt>"  [--max-budget-usd N]
```
- **Auth.** `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` (the owner's subscription), stored as `runner/claude_token`. `ANTHROPIC_API_KEY` (`runner/anthropic_key`) is the alternative if the owner prefers API billing; then `--max-budget-usd` (`runner.max_budget_usd`) applies. Not `--bare`: it ignores OAuth and skips the repo's `CLAUDE.md`, which good work needs.
- **The owner's own Claude config is not loaded.** `HOME`/`CLAUDE_CONFIG_DIR` point into the run, and `~/.claude` is read-denied: no user hooks, plugins, MCP servers, memory or permissions leak in. `--setting-sources project` keeps the repo's `.claude/settings.json` (its hooks run inside the sandbox, so they can't do more than Claude itself).
- **Runner prompt** (code-built): you are on branch `X`, already checked out; commit your work with clear messages; don't push, don't change remotes; end with a 1–3 line summary of what changed and anything left undone.
- **Session id** is chosen by the runner and saved (`ctx.save(claude_session_id=…, worktree_path=…)`) **before** the spawn, so a daemon crash resumes with `--resume <uuid>` in the same run dir instead of starting over (counts as an attempt; 2 attempts max).
- **The transcript** (stream-json, read from the pipe) goes to `~/.resonant/runner/transcripts/R3.jsonl`, outside the run dir, never into the DB or logs. The DB keeps the session id, durations, token counts and exit status. The final summary is `clean_external`-ed, redacted (the Claude token in every encoding), clipped to 600 chars, and only ever shown after a code-built `R3:` prefix, so it can't pose as an approval prompt.
- **Usage limit.** A rate-limit result (`is_error` with a usage-limit subtype in the final stream-json message) returns `Wait("usage_reset", wake_at=<reset time or +1h>)` and frees the slot; the run resumes the same session after.

### D3. Workspace, harvest and PR (`resonant/runner/workspace.py`)

- **Config.** `runner.repos: {name: {remote: "github.com/owner/repo", base: "main"}}`; `runner.work_dir` (default `~/resonant-runs`, **not** under `~/.resonant`, which the sandbox denies); `runner.mirror_dir` (default `~/.resonant/runner/mirrors`, daemon-only).
- **Mirror.** One bare mirror per repo, owned by the daemon. `git fetch` with the token passed by `GIT_CONFIG_COUNT`/`http.extraHeader` env (never argv, never a file), `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `core.hooksPath=/dev/null`, absolute `/usr/bin/git`.
- **Clone.** `git clone --no-hardlinks --branch <base> <mirror> <run>/repo`, then `remote set-url origin <https remote, no token>` and a new branch `resonant/r3-<slug>`. The base commit is recorded.
- **Harvest** (after Claude exits, whatever the exit): inside the sandbox, `git -C repo bundle create <run>/out.bundle <base>..HEAD` (also `git add -A && git commit` first if Claude left uncommitted changes, with message `R3: uncommitted work`). Outside, the daemon opens `<run>/out.bundle` with `O_NOFOLLOW` (a regular file, ≤ 50 MB), copies it into the mirror dir, and runs `git -C <mirror> fetch <copy> HEAD:refs/runs/r3` with the hardened env above. No commits since the base means no bundle: the run ends `no_changes`. **The daemon never executes git in the clone**, so a planted `.git/config` (`core.fsmonitor`, `credential.helper`, `core.sshCommand`) or hook is never run outside the sandbox.
- **Checks before push**, code-built, each a clear failure reply: the head descends from the base; ≤ 200 files and ≤ 20 MB changed; **no change under `.github/workflows/` or `.github/actions/`** unless `runner.repos.<r>.allow_workflow_changes` (a pushed workflow would run in Actions with the repo's secrets; the PAT also lacks the Workflows permission, so GitHub refuses it too); no file matching the redaction patterns for the tokens Resonant holds.
- **Push and PR.** `git push <remote> refs/runs/r3:refs/heads/resonant/r3-<slug>` from the mirror, then `gh pr create --head … --base … --title "<first commit subject>" --body <code-built: R3, the ask, Claude's summary, the checks>` with `GH_TOKEN` in gh's env only. PR title and body are untrusted text going to GitHub only; nothing from them is parsed back. The body quotes the owner's ask (clipped to 2000 chars), so a public repo shows it: `runner.repos.<r>.quote_ask: false` leaves it out.
- **Cleanup.** The run dir is deleted on merge, on `/cancel`, or 7 days after the run ends (a daily core job), with a no-follow tree delete. The mirror stays.
- **The daemon never writes into a run dir after the session starts** (the sandbox could have planted a symlink there, e.g. to `~/.zshrc`); everything it writes later goes under `~/.resonant/runner/`.

### D4. The runner (`runners/claude.py`, migration 006)

- `ClaudeRunner(name="claude", uses_claude_slot=True, max_step_s=max_minutes*60 + 120)`. One task per run, kind `run`, priority from the principal (owner), `title` `run R3 · resonant` (code-built, never the ask, so `/projects` stays PII-free).
- Steps, each checkpointed: `prepare` → `session` (the long step) → `harvest` → `publish` → `reply`. A crash resumes at the last checkpoint; `publish` is idempotent (existing branch/PR found by name and reused).
- **Table `runs`** (migration 006): `code` (`R1`, `R2`, …, never reused), `task_id`, `repo`, `branch`, `base_sha`, `head_sha`, `pr_number`, `pr_url`, `status` (`running` / `ready` / `merged` / `failed` / `cancelled` / `no_changes`), timestamps, `files_changed`. The ask itself is stored only in the task checkpoint (PII, like message bodies).
- **Processes.** The Claude process runs in its own process group under the daemon's `ProcessTracker`, so the kill switch's `kill_all` and `/cancel R3` end it (SIGTERM, then SIGKILL after 10 s). A cancelled run still harvests what it has but doesn't push.
- **Progress.** On start, the runner upserts pulse item `core` / `run/R3` (`normal`, title `R3 still running · resonant · started 14:05`); the next hourly pulse mentions it (its state is the elapsed-time bucket `<30m` / `30m+`, so it's mentioned at most twice). Done, failed or cancelled clears it.
- **Replies.** Start (from `start_run`), done (`R3 done: PR #41 · <url> · 3 files · 12m`), no changes (`R3: no changes. <summary>`), failed (`R3 failed: timed out at 45m (work kept: /runs R3)` or the check that refused), cancelled. Deduped per run and kind.
- **Merge** (`merge_run`, L2): only a run in `ready`; `gh pr merge --squash --match-head-commit <head_sha>` with the runner token; refuses if CI isn't green (`gh pr checks`). Explicit `/merge R3` runs; the model choosing it asks `yes`.

### D5. Entry points (router; **after the NL-intents PR merges**)

- Builtin tools: `start_run{repo, ask}` (L3, owner only; `repo` is an enum of `runner.repos` names, so the model can't invent a path), `list_runs` (L0), `cancel_run{code}` (L3), `merge_run{code}` (L2).
- `INTENT_TOOLS["run_project"] = ("start_run", "list_runs", "cancel_run", "merge_run")`; `ESCALATE_REPLY` goes away. When `runner.repos` is empty or a secret is missing, `start_run` replies `The runner isn't set up: <what's missing>. On the Mini: …` instead of failing silently.
- Slash commands: `/run <repo> <ask>`, `/runs [R3]`, `/cancel R3`, `/merge R3`. `/cancel` and `/merge` are control-plane-ish but are about a run the owner started; they stay owner-only.
- Labeler eval cases: "fix X in resonant", "open a PR that …", "how's R3 going", "merge it", "stop the run".
- The `/kill` reply counts runs killed. `/resume` doesn't restart killed runs; they are `cancelled`. While paused, queued runs don't start; under autonomy off `start_run` (L3) asks first, like any L3.

### D6. Checks and visibility

- `resonant sandbox check`: runs every deny and allow probe from P5A-1 against the live profiles and prints a table; exit 1 on any failure. A `--check` (go-live) row `sandbox`. Claude can run it on the Mini without the owner.
- `--check` rows `runner secrets` (both present, not their values), `runner repos` (count, each mirror fetchable).
- `/status` shows runs running and the Claude slots in use.

---

## 4. Tickets

| Ticket | Title | Depends on | Wave |
|---|---|---|---|
| P5A-1 | Sandbox: spike on the Mini, profile template + parameters (`runner` and `extension`), `wrap(argv, profile, params)`, deny and allow probe suite (real `sandbox-exec`, macOS CI job), `resonant sandbox check`, `--check` row | — | 1 |
| P5A-2 | Runner config (`runner:`), secrets refs, workspace: mirror, clone, bundle harvest, checks, push, `gh pr create`, merge helper, cleanup job. Fake git remote in tests (a local bare repo), fake `gh` on `PATH` | — | 1 |
| P5A-3 | `ClaudeRunner` + migration 006 `runs`: steps, session id save and resume, timeout, process tracker + kill switch + cancel, usage-limit wait, pulse progress item, replies. Tests use a fake `claude` script (writes a file, commits, prints stream-json) | 1, 2 | 2 |
| P5A-5 | Extension processes under the `extension` profile (`extensions.sandbox: true` by default once the probes pass; the github and example extensions keep working), docs: architecture trust model, `extensions.md` | 1 | 2 |
| P5A-4 | Entry points: the four tools, `run_project` toolset, `/run` `/runs` `/cancel` `/merge`, setup-missing replies, `/kill` count, labeler eval cases. **Starts only after the NL-intents PR is merged** (it touches `router/intents.py`, `router/prefixes.py`, `fast_path.py`, `commands.py`) | 3, NL PR | 3 |
| P5A-6 | e2e (`tests/test_e2e_runner.py`, §5), go-live step 14, `--check` rows, `docs/test.md` rows, `interfaces.md`/`architecture.md`/`followups.md` updates (close "Runner sandboxing" and "Seatbelt sandbox", note what's left), README | all | 4 |

P5A-1 and P5A-2 don't touch the same files and run in parallel. P5A-3 and P5A-5 both read the sandbox module and touch different callers.

## 5. Exit criteria (e2e, `tests/test_e2e_runner.py`, fake `claude`, real sandbox on macOS)

| # | Scenario | Pass condition |
|---|---|---|
| R1 | "fix X in resonant" | `On it: R1 …` at once; one PR on the fake remote with the fake Claude's commit; `R1 done: PR #1 …`; pulse item opened then cleared; `/runs` shows `ready`. |
| R2 | Escape attempts from the fake Claude | Reading `~/.resonant/resonant.db`, running `resonant approvals approve`, `security find-generic-password`, `curl 127.0.0.1:7777`, writing `~/Library/LaunchAgents/x.plist` and `~/.zshrc`, `launchctl submit` all fail; the run still finishes. |
| R3 | Planted `.git/config` (`core.fsmonitor`, `credential.helper`) and `.git/hooks/pre-push` in the clone | Nothing runs outside the sandbox (a marker file outside stays absent); the push works. |
| R4 | Workflow change | Refused with `R1 failed: changes .github/workflows (not allowed for resonant)`; nothing pushed. |
| R5 | Daemon crash mid-session | Resumed with the same session id; one PR, not two. |
| R6 | `/kill` mid-run | The process tree is gone within 15 s, run `cancelled`, nothing pushed, `/kill` reply counts 1 run. |
| R7 | Timeout | Killed at `max_minutes` (shortened in the test); `failed`, work kept, no push. |
| R8 | Third concurrent run | Waits for a slot; starts when one frees. |
| R9 | `/merge R1` with CI red, then green | Refused, then merged with `--match-head-commit`; "merge it" in chat asks `yes`. |
| R10 | No repos / no token | `The runner isn't set up: …` naming exactly what's missing. |
| R12 | Planted symlinks: `out.bundle` → `~/.resonant/resonant.db`, a run-dir file → `~/.zshrc` | The bundle is refused (not a regular file); nothing outside the run dir is written or deleted, including by cleanup. |
| R11 | Token hygiene | Neither token appears in the DB, the logs, argv (`ps`), the clone's `.git/config`, the transcript, or any reply. |

## 6. Owner steps (terminal on the Mini, once)

1. `claude setup-token`, then `resonant secret set runner/claude_token` and paste it. (P5A-2 checks two things: the secret store caps values at 128 bytes and a setup token is about 108; and `secret set` accepts the core `runner/` prefix, not only installed extensions.) Or `runner/anthropic_key` for API billing.
2. A fine-grained GitHub PAT for **only** the runner repos: Contents read/write, Pull requests read/write, Commit statuses and Checks read, Metadata read. **No Workflows permission.** Then `resonant secret set runner/github_token`.
3. Name the repos. Claude adds them to `runner.repos` in `~/.resonant/config.yaml` (not a secret) and restarts.
4. Recommended: branch protection on each repo's `main` requiring CI, so `/merge` can never land red code.

Until steps 1–3 are done, everything else is deployable and testable: `resonant sandbox check` (Claude runs it), `/runs`, and "fix X in Y" → `The runner isn't set up: …`.

## 7. Risks

- **Allow-default runner profile.** Anything not enumerated is allowed. Mitigated by deny-by-default **writes** (no persistence), the Keychain and DB denies, and probes for every known escape; `resonant sandbox check` is re-run after OS updates. Residual: an unknown mach service that acts on the user's behalf. The extension profile is deny-default.
- **Exfiltration.** The session has network and can send the repo's code or its own Claude token anywhere. Accepted (the owner's own repos, a revocable token). The GitHub token is never inside.
- **Prompt injection from the repo** steers Claude, within the wall; the result is a PR the owner reviews before `/merge`.
- **Shared caches** can be poisoned by one run for the next. Accepted: every run is owner-initiated.
- **Remote Login.** ssh to the Mini's LAN address isn't blocked by port (only loopback :22 is); the sandbox can't read `~/.ssh` or reach an agent socket, so it has no credential. Keep Remote Login off or key-only.
- **`sandbox-exec` deprecation.** If a macOS update removes it, the runner refuses to start (`sandbox check` fails closed) and the fallback is a separate macOS user (designed in [`followups.md`](followups.md#runner-sandboxing-phase-35)).

## 8. Deferred (to `followups.md`)

- Follow-up instructions on a run ("also add tests" → resume the same session on the same branch) and PR review comments fed back.
- Runs on repos not in the allow-list, or creating new repos.
- Network allow-listing by domain (a filtering proxy, as Claude Code's own `/sandbox` does).
- A separate macOS user for runs, if Seatbelt isn't enough.
- Members (non-owner) starting runs.
