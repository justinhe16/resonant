# Phase 5a plan: the Seatbelt sandbox and the Claude runner

Status: **implemented** (2026-10-15; P5A-1 to P5A-7 merged, see As built). The owner approved going ahead ("execute!") before this write-up; the decisions in §1 are theirs, the rest is the design. Built with robot-surf: a worktree per ticket, tests, PR, CI, code-review agent, squash-merge to `main`, in the dependency waves of §4. Slack (Phase 5b) is out of scope. The plan had a security self-review and then a code-review agent's review (below); each ticket's PR still gets the code-review agent, and P5A-1's PR a dedicated sandbox review.

**Goal.** The owner texts "fix the flaky test in resonant" from anywhere. A Claude Code session on the Mini does the work in a throwaway clone, inside a macOS sandbox, and Resonant texts back a PR link. Merging is the one approval. While it runs, the hourly pulse says it is still going; `/runs` shows it; `/kill` stops it.

The sandbox is **a wall, not a gate**: no new approvals, a few milliseconds per process, and Claude runs with permission checks off inside it. It closes the gap in the [trust model](architecture.md#trust-model): "same-user processes can run `resonant approvals approve`, write `resonant.db` and read the Keychain". With it, a Claude session can do none of the three, whatever a malicious repo file talks it into. Extension processes move under the sandbox later, opt-in ([`followups.md`](followups.md#seatbelt-sandbox-for-extension-processes)).

**Revised after review (2026-10-15).** A code-review agent read the first draft. Its four blockers are fixed here: launchd, window server and input injection are denied unconditionally with a probe each (D1), port denies cover every address and unix sockets are deny-default (D1), a PR branch that changes what CI runs is flagged to the owner (D3), and every descendant process is reaped, including after a crash, with a re-check before anything is published (D4). The extension profile moved out of this build, and the router wiring is split so most of the entry-point work doesn't wait for the NL-intents PR (§4).

---

## 1. Owner decisions

| # | Question | Decision |
|---|---|---|
| Q1 | Approvals | None to **start** a run (an owner request is the go-ahead). The only approval is the merge: `/merge R3` is explicit and runs; "merge it" in chat asks a plain `yes` (L2, as every model-chosen L2). *L2 rather than the github extension's L1 is Claude's proposal; the owner may change it.* |
| Q2 | Speed vs caution | Speed inside the wall: `--permission-mode bypassPermissions`, outbound network open, localhost dev servers allowed, a package cache shared between runs. Caution at the wall: the denies in D1. |
| Q3 | Repos | Only repos listed in `runner.repos`. **Empty by default**; the owner names them (§6). |
| Q4 | Progress | A `normal` pulse item while a run is going (the hourly `btw` mentions it, cleared when done). Start, finish and failure are direct replies to the request, sent at once, so nothing needs `urgent`. |
| Q5 | Concurrency | 2 runs at once (`claude.max_concurrent: 3` with 1 slot reserved for on-call), at most 1 per repo. |
| Q6 | Limits | 45 minutes of session time per run (`runner.max_minutes`), then every process of the run is killed and the run fails with what it has. |

## 2. How a run flows

```
iMessage "fix the flaky pulse test in resonant"
  → router: intent run_project → local runner → tool start_run{repo: "resonant", ask: "..."}  (L3, owner only)
      → reply "On it: R3 · resonant. Up to 45m. /runs to check, /cancel R3 to stop."
      → inserts a queued task {kind: "run", runner: "claude"}  (same transaction)
  → ClaudeRunner (takes a Claude slot and the repo's lock):
      0. cached sandbox self-probe (D6); fails closed
      1. workspace: refresh the daemon's bare mirror (token, outside the sandbox),
         clone it --no-hardlinks into ~/resonant-runs/R3/repo, branch resonant/r3-<slug>
         (the only daemon-side git in the clone, and it happens before the session exists)
      2. sandbox-exec -f runner.sb  claude -p "<ask>" --session-id <uuid> ...   (≤ 45m)
      3. reap every remaining process of the run
      4. sandbox-exec -f runner.sb  git bundle create out.bundle <base>..HEAD
      5. daemon (outside): read out.bundle by fd, fetch it into the mirror with fsck,
         checks, re-check cancel/pause, push with the token, gh pr create
      → reply "R3 done: PR #41 · <url> · 3 files. /merge R3 when CI is green."
  → pulse item core/run/R3 cleared; workspace deleted after 7 days or on merge
```

The GitHub token **never enters the sandbox**, and once the session has started the daemon **never runs git inside the clone**: it only reads a bundle file with its own mirror's config (D3).

---

## 3. Design

### D1. The sandbox (`resonant/sandbox/`)

`sandbox-exec -f <profile> -D KEY=VALUE … <argv>` with a profile generated from a template and parameters. No string-built paths in SBPL: every path goes in as a `-D` parameter, is `os.path.realpath`-ed first (`/tmp`, `/var` and `/etc` are symlinks into `/private`, and SBPL matches real paths), must be absolute, and is refused if it contains `"`, `\`, `)` or a control character. `sandbox-exec` is deprecated but present on macOS 27.0.1; `resonant sandbox check` (D6) re-proves it after every OS update.

One profile, `runner` (Claude sessions). The deny-default `extension` profile is a follow-up ([`followups.md`](followups.md#seatbelt-sandbox-for-extension-processes)); the module is built so a second profile is a new template, not a redesign.

| | `runner` |
|---|---|
| Base | `(allow default)`, then the denies below. Deny-default breaks too much of a dev toolchain (node, uv, compilers each need dozens of mach services), and the risks that matter are enumerable. Every deny below is **unconditional** in the profile and has a probe (D6). |
| File write | **Deny everywhere**, then allow: the run dir (`repo/`, `home/`, `tmp/`), the runner cache dir, `/dev/null`, `/dev/tty*`. Not `/private/tmp`, `/Users/Shared` or the per-user `/private/var/folders/…` dirs; the spike adds only the run's own `…/T/` subpath if a tool proves to need it. |
| File read | Deny `$HOME`, `/Volumes` (Time Machine and external disks hold copies of `~/.resonant`), `/Users/Shared`, `/private/tmp` and `/private/var/folders` (other apps' temp and caches), then allow: the run dir, the runner cache dir, and toolchain **subpaths** (`runner.sandbox.read_paths`: by default `~/.local/share/claude`, `~/.local/share/uv/python`, `~/.cargo/bin`, `~/.rustup/toolchains`, `~/.nvm/versions`, `~/.bun/bin`; whole tool homes like `~/.cargo` are not allowed, since they hold `credentials.toml`). `~/.local/bin` is not readable: the sandbox `PATH` gets a run-local `bin/` with symlinks to the few binaries it needs (`claude`, `uv`), never `resonant`. `file-read-metadata` (stat only) is allowed on the ancestor dirs of every allowed path, so `realpath` and `getcwd` work. Always denied even if listed: `~/.resonant`, `~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.claude`, `~/Library`, `~/.local/bin`, the owner's own checkouts. |
| Exec | Deny `/usr/bin/security`, `/usr/bin/osascript`, `/usr/bin/open`, `/bin/launchctl`, `/usr/bin/shortcuts`, `/usr/bin/crontab`, `/usr/bin/at`, `/usr/bin/sudo`, `/usr/sbin/screencapture`, the `resonant` entry point and the daemon's venv. Defense in depth only: the mach denies below are what stop a copied binary or a Python XPC client. |
| Mach / IPC | Deny `mach-lookup` of: `com.apple.xpc.launchd*` (a launchd submit would start a process **outside** the sandbox), `com.apple.SecurityServer`, `com.apple.securityd*` (Keychain), `com.apple.windowserver*`, `com.apple.hidsystem*` (CGEvent input injection: typing `resonant approvals approve` into an open Terminal; screenshots), `com.apple.accessibility*`, `com.apple.universalaccessd*`, `com.apple.coreservices.appleevents` and `appleevent-send` (Apple Events), `com.apple.coreservices.launchservicesd` and `com.apple.lsd.*` (open apps), `com.apple.pasteboard.*`, `com.apple.tccd*`. Deny `mach-priv-task-port` and `mach-task-name` (task_for_pid), and `signal` and `process-info*` on processes outside the sandbox. Keep `com.apple.trustd*` (TLS for Go and Apple git) and `mDNSResponder`. If the spike shows `claude`, git or node can't run without one of these, the runner switches to the **separate-user fallback** (§7) before P5A-3 starts; no deny is dropped to make a tool work. |
| Network | Outbound allowed, except `*:7777` (API) and `*:11434` (Ollama) on every address (`network-outbound` and local bind/connect), so the Mini's LAN IP is covered as well as loopback. Loopback otherwise allowed (dev servers, test databases). **Unix-socket connects denied by default**, with `mDNSResponder` re-allowed: that covers the Docker socket, the launchd ssh-agent socket and any daemon's local socket. |

**Why these close the gaps.** `resonant.db` and the config are under `~/.resonant` (read and write denied; their Time Machine copies under `/Volumes` too). The `resonant` CLI is denied to exec and couldn't do anything anyway without the DB or the API port. The Keychain is unreachable without `securityd` (a copied `security` binary fails the same way). Nothing can start outside the sandbox: launchd submits, Apple Events, LaunchServices and synthetic input are all denied at the mach level. The sandbox is inherited by every child, so `bash`, `node` and `git` all stay inside.

**Persistence escapes are covered by the write allow-list**: `~/Library/LaunchAgents`, `~/.zshrc`, `~/.gitconfig`, `~/.claude/settings.json` (and its hooks, plugins and MCP config, which would otherwise run unsandboxed in the owner's next Claude session), the owner's repos' `.git/hooks`, cron tabs, login items.

**Cache.** The runner gets its own cache dir, `runner.cache_dir` (default `~/resonant-runs/.cache`, with `uv/`, `npm/`, `pip/`), shared between runs and **used by nothing outside the sandbox**: not the owner's `~/.cache`, not the daemon, not extensions. A poisoned cache can only reach a later run, which is inside the same wall.

**Environment** (built from scratch, as `scratch_env`): `PATH` (the run-local `bin/`, then `/usr/bin:/bin:/usr/sbin:/sbin` and the allowed toolchain bins), `HOME=<run>/home`, `TMPDIR=<run>/tmp`, `CLAUDE_CONFIG_DIR=<run>/home/.claude`, `LANG`, `UV_CACHE_DIR`/`npm_config_cache`/`PIP_CACHE_DIR` in the runner cache, `DISABLE_AUTOUPDATER=1`, `GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL`/`GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL` (`Resonant runner`, a noreply address; set by env, never written to a git config file), and `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`). Nothing from `os.environ`; no `SSH_AUTH_SOCK`.

**The spike comes first** (P5A-1, before anything is built on the profile). On the Mini it proves:
- `claude -p "say hi"` authenticates from the env token with Keychain mach-lookup denied, and **how long it takes**: a denied `securityd` lookup must fail fast, not hang (pass: within 2 s of an unsandboxed run). Claude Code's own sandbox setting is off in the run's settings (`sandbox.enabled: false`), since a nested `sandbox_apply` fails.
- The allows: `git clone`/`commit`, `uv run pytest`, `npm ci` from the runner cache, a `python -m http.server 8123` reachable on loopback, HTTPS to github.com, `realpath` and `getcwd` inside the run dir.
- Every deny probe in D6 fails as expected, with **pass/fail exit criteria and no fallback wording**: if one can't be denied, the separate-user decision is made and written into this plan before P5A-3 starts.
- SBPL's last-match-wins ordering for the deny-then-allow reads.

The findings go into this plan's As built.

### D2. Claude Code invocation

```
claude -p "<ask>" --session-id <uuid> --output-format stream-json --verbose
  --permission-mode bypassPermissions --strict-mcp-config --setting-sources project
  --append-system-prompt "<runner prompt>"  [--max-budget-usd N]
```
- **Auth.** `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` (the owner's subscription), stored as `runner/claude_token`. `ANTHROPIC_API_KEY` (`runner/anthropic_key`) is the alternative if the owner prefers API billing; then `--max-budget-usd` (`runner.max_budget_usd`) applies. Not `--bare`: it ignores OAuth and skips the repo's `CLAUDE.md`, which good work needs.
- **The owner's own Claude config is not loaded.** `HOME`/`CLAUDE_CONFIG_DIR` point into the run, and `~/.claude` is read-denied: no user hooks, plugins, MCP servers, memory or permissions leak in. The run's `settings.json` is code-built (autoupdate off, Claude's own sandbox off). `--setting-sources project` keeps the repo's `.claude/settings.json`; its hooks run inside the sandbox. **Known residual:** a repo's settings can set `env.ANTHROPIC_BASE_URL` or an `apiKeyHelper` and so send the Claude token elsewhere. Low risk, since only owner-listed repos run, and the D3 sensitive-paths flag shows the owner any PR that changes `.claude/`.
- **Runner prompt** (code-built): you are on branch `X`, already checked out; commit your work with clear messages; don't push, don't change remotes; end with a 1–3 line summary of what changed and anything left undone.
- **Session id** is chosen by the runner and saved (`ctx.save(claude_session_id=…, worktree_path=…)`) **before** the spawn, so a daemon crash resumes with `--resume <uuid>` in the same run dir instead of starting over. Before resuming, the old run's processes are reaped (D4).
- **The transcript** (stream-json, read from the pipe) goes to `~/.resonant/runner/transcripts/R3.jsonl`, outside the run dir, never into the DB or logs. Each line is capped at 1 MB (a longer line is truncated and marked, and the reader never buffers more), and **both tokens** (Claude and GitHub, every encoding the redactor knows) are redacted on write: under `bypassPermissions`, `env` output lands in tool results. Claude's own session jsonl under `<run>/home/.claude` holds the same text unredacted; it stays in the run dir (inside the wall, deleted with it) and is never copied out. The DB keeps the session id, durations, token counts and exit status. The final summary is `clean_external`-ed, redacted, clipped to 600 chars, and only ever shown after a code-built `R3:` prefix, so it can't pose as an approval prompt.
- **Usage limit.** A rate-limit result (`is_error` with a usage-limit subtype in the final stream-json message) returns `Wait("usage_reset", wake_at=<reset time or +1h>)` and frees the slot and the repo lock; the run resumes the same session after.

### D3. Workspace, harvest and PR (`resonant/runner/workspace.py`, `resonant/runner/checks.py`)

- **Config.** `runner.repos: {name: {remote: "github.com/owner/repo", base: "main"}}`; `runner.work_dir` (default `~/resonant-runs`, **not** under `~/.resonant`, which the sandbox denies); `runner.mirror_dir` (default `~/.resonant/runner/mirrors`, daemon-only); `runner.cache_dir`; `runner.max_run_dir_mb` (default 5000).
- **Mirror.** One bare mirror per repo, owned by the daemon, behind a **per-repo lock** (fetch, harvest-fetch and push all take it; also why there is at most one run per repo at a time). `git fetch` with the token passed by `GIT_CONFIG_COUNT`/`http.extraHeader` env (never argv, never a file), `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `core.hooksPath=/dev/null`, absolute `/usr/bin/git`.
- **Clone** (before the session, so a clone the sandbox has never touched): `git clone --no-hardlinks --branch <base> <mirror> <run>/repo`, then `remote set-url origin <https remote, no token>` and a new branch `resonant/r3-<slug>`. The base commit is recorded. These three are the only git commands the daemon ever runs in the clone, and all happen before Claude starts.
- **Harvest** (after Claude exits and every process is reaped, whatever the exit): inside the sandbox, `git -C repo bundle create <run>/out.bundle <base>..HEAD` (also `git add -A && git commit` first if Claude left uncommitted changes, with message `R3: uncommitted work`). Outside, the daemon:
  1. Opens `<run>/out.bundle` with `O_NOFOLLOW`, then `fstat`s **the fd**: a regular file, ≤ 50 MB. It reads at most 50 MB + 1 byte **from that fd** into a new file in the mirror dir. It never re-opens the path. (`O_NOFOLLOW` covers only the last component, but `<run>` itself was created by the daemon before the session and is only ever opened by an fd held since then.)
  2. `git -C <mirror> -c transfer.fsckObjects=true -c fetch.fsckObjects=true fetch <copy> <expected-branch>:refs/runs/r3` with the hardened env. Exactly one ref, the run's branch; a bundle with other or extra refs is refused.
  No commits since the base means no bundle: the run ends `no_changes`. **The daemon never executes git in the clone after the session starts**, so a planted `.git/config` (`core.fsmonitor`, `credential.helper`, `core.sshCommand`) or hook is never run outside the sandbox.
- **Disk.** A watchdog checks the run dir size every minute during the session; over `runner.max_run_dir_mb`, the run is killed and fails (`R3 failed: used over 5 GB`). The runner cache is pruned to 20 GB by the daily cleanup job.
- **Checks before push**, code-built, on the fetched commits:
  - **Refused**, each with a clear failure reply: the head doesn't descend from the base; > 200 files or > 20 MB changed; any change under `.github/` (workflows, actions, and anything else Actions reads) unless `runner.repos.<r>.allow_github_changes`; a `.gitmodules` change; any of the **two tokens** (or anything matching the redaction patterns) in the diff, a commit message, the branch name or the PR body.
  - **Flagged, not refused** (the *sensitive paths*): `.claude/**`, `.mcp.json`, `.devcontainer/**`, `.vscode/tasks.json`, `.vscode/settings.json`, `Makefile`, `justfile`, `conftest.py`, `noxfile.py`, `tox.ini`, `package.json`, `pyproject.toml`, `setup.py`, `Dockerfile*`, `.pre-commit-config.yaml`. These are files a `pull_request` CI run or the owner's tooling executes, so a green CI is **not** proof the change is harmless. The done reply and the `/merge` reply list them (`⚠ changes Makefile, package.json`), code-built, so the owner sees them in iMessage, not only in Claude's summary.
- **Before publish**, the runner re-reads the run's status and the kill switch: a run that is `cancelled`, or a paused Resonant, stops here, after harvest and before `push` and before `gh pr create` (checked again between the two). `kill_all` doesn't reach this daemon-side step, so this check is what makes `/kill` and `/cancel` stop a publish.
- **Push and PR.** `git push <remote> refs/runs/r3:refs/heads/resonant/r3-<slug>` from the mirror, then `gh pr create --head … --base … --title "<first commit subject>" --body <code-built: R3, the ask, Claude's summary, the checks, the flagged paths>` with `GH_TOKEN` in gh's env only. PR title and body are untrusted text going to GitHub only; nothing from them is parsed back. The body quotes the owner's ask (clipped to 2000 chars), so a public repo shows it: `runner.repos.<r>.quote_ask: false` leaves it out.
- **Cleanup.** The run dir is deleted on merge, on `/cancel`, or 7 days after the run ends (a daily core job), with a no-follow tree delete (`rmtree` on fds, `dir_fd` all the way down). The mirror stays.
- **The daemon never writes into a run dir after the session starts** (the sandbox could have planted a symlink there, e.g. to `~/.zshrc`); everything it writes later goes under `~/.resonant/runner/`.

### D4. The runner (`runners/claude.py`, migration 006)

- `ClaudeRunner(name="claude", uses_claude_slot=True, max_step_s=max_minutes*60 + 120)`. One task per run, kind `run`, priority from the principal (owner), `title` `run R3 · resonant` (code-built, never the ask, so `/projects` stays PII-free).
- Steps, each checkpointed: `prepare` → `session` (the long step) → `harvest` → `publish` → `reply`. A crash resumes at the last checkpoint; `publish` is idempotent (existing branch/PR found by name and reused).
- **Table `runs`** (migration 006): `code` (`R1`, `R2`, …, never reused), `task_id`, `repo`, `branch`, `base_sha`, `head_sha`, `pr_number`, `pr_url`, `status` (`running` / `waiting` / `ready` / `merged` / `failed` / `cancelled` / `no_changes`), `pgid`, `leader_pid`, `leader_started_at` (the process start time, so a reused pid is never killed), `attempts`, `session_seconds`, timestamps, `files_changed`, `flagged_paths`. The ask itself is stored only in the task checkpoint (PII, like message bodies).
- **Processes.** The Claude process runs in its own process group under the daemon's `ProcessTracker`; `pgid`, pid and start time are persisted **before** the step waits on it. Ending a run (done, timeout, `/cancel`, `/kill`, disk watchdog) **reaps every descendant**: SIGTERM to the group, then a recursive `psutil` walk from the leader (catching children that `setsid`-ed into a new group, and dev servers left running) plus anything still in the pgid, then SIGKILL after 10 s. Harvest starts only once nothing of the run is alive, so nothing writes into the run dir while it is read. **On daemon start**, before any resume, every `runs` row in `running` is reaped the same way (pid matched by start time). A resumed session never overlaps its predecessor.
- **Budget.** `max_minutes` counts session time only, summed across attempts. A usage-limit wait doesn't count toward it and isn't an attempt; a crash resume is an attempt (2 max). A run waiting more than 24 h in total fails (`R3 failed: usage limit didn't reset within 24h`).
- **Progress.** On start, the runner upserts pulse item `core` / `run/R3` (`normal`, title `R3 still running · resonant · started 14:05`); the next hourly pulse mentions it (its state is the elapsed-time bucket `<30m` / `30m+`, so it's mentioned at most twice). Done, failed or cancelled clears it.
- **Replies.** Start (from `start_run`), done (`R3 done: PR #41 · <url> · 3 files · 12m`, plus `⚠ changes …` for flagged paths), no changes (`R3: no changes. <summary>`), failed (`R3 failed: timed out at 45m (work kept: /runs R3)` or the check that refused), cancelled. Deduped per run and kind.
- **Merge** (`merge_run`, L2): only a run in `ready`; `gh pr merge --squash --match-head-commit <head_sha>` with the runner token; refuses if CI isn't green (`gh pr checks`). Explicit `/merge R3` runs; the model choosing it asks `yes`. Both replies repeat the flagged paths.

### D5. Entry points (router)

Split in two so most of it doesn't wait for the NL-intents PR:

- **Tools and slash commands (P5A-4).** Builtin tools: `start_run{repo, ask}` (L3, owner only; `repo` is an enum of `runner.repos` names, so the model can't invent a path), `list_runs` (L0), `cancel_run{code}` (L3), `merge_run{code}` (L2). Slash commands: `/run <repo> <ask>`, `/runs [R3]`, `/cancel R3`, `/merge R3`, owner-only. When `runner.repos` is empty or a secret is missing, `start_run` replies `The runner isn't set up: <what's missing>. On the Mini: …` instead of failing silently. The `/kill` reply counts runs killed. `/resume` doesn't restart killed runs; they are `cancelled`. While paused, queued runs don't start; under autonomy off `start_run` (L3) asks first, like any L3. This touches `commands.py` and `fast_path.py`, which the NL-intents PR also changes, so it is built on whatever `main` has when wave 3 starts (that PR is expected to have merged long before).
- **Intent wiring (P5A-5), only after the NL-intents PR is merged.** `INTENT_TOOLS["run_project"] = ("start_run", "list_runs", "cancel_run", "merge_run")`; `ESCALATE_REPLY` goes away; the prefix hash golden test is updated. Labeler eval cases: "fix X in resonant", "open a PR that …", "how's R3 going", "merge it", "stop the run".

### D6. Checks and visibility

- **Probe suite**, split by where it can run:
  - **CI-safe** (GitHub macOS runners): file read and write denies and allows, realpath'd params and quote refusal, exec denies, `*:7777` and `*:11434` on loopback, a unix-socket connect to a test socket, the env contents, a `setsid` child surviving the group kill but not the reaper.
  - **Local-only** (the Mini, `resonant sandbox check`): Keychain (`security find-generic-password` and a copied binary), a launchd submit from Python (`launchctl submit` and an XPC client), `CGEventPost` and a window-server screenshot, `task_for_pid` on the daemon, the same ports on the Mini's **LAN IP**, the Docker and ssh-agent sockets if present, reads under `/Volumes`, and a real `claude -p` auth.
- `resonant sandbox check` runs every probe against the live profile and prints a table; exit 1 on any failure. A `--check` (go-live) row `sandbox`. Claude can run it on the Mini without the owner.
- **Fail closed on every run.** Before each run's session, the runner runs a cheap cached self-probe (a denied read of `~/.resonant`, a denied mach-lookup of `securityd`, a denied connect to `:7777`; about 50 ms), cached for 1 hour and keyed by the macOS build and the profile hash. A failure fails the run (`R3 failed: the sandbox check failed, see resonant sandbox check`) and alerts like a health alert.
- `--check` rows `runner secrets` (both present, not their values), `runner repos` (count, each mirror fetchable).
- `/status` shows runs running and the Claude slots in use.

---

## 4. Tickets

| Ticket | Title | Depends on | Wave | Owns (files) |
|---|---|---|---|---|
| P5A-1 | Sandbox: spike on the Mini (D1 pass/fail, written into As built), profile template + parameters, `wrap(argv, params)`, run-local `bin/`, the self-probe and its cache, probe suites (CI-safe in a macOS CI job, local-only behind a marker), `resonant sandbox check`, `--check` row `sandbox` | — | 1 | `resonant/sandbox/` (new: `__init__.py`, `profile.py`, `runner.sb`, `probes.py`), `cli.py` (the `sandbox` subcommand), `scripts/go_live_check.py` (sandbox row), `.github/workflows/` (macOS job), `tests/test_sandbox*.py` |
| P5A-2 | Runner config and workspace: `runner:` settings, core `runner/` secrets, mirror + per-repo lock, clone, fd-based bundle read + fsck fetch, refused checks and flagged paths, disk watchdog helper, pre-publish re-check hook, push, `gh pr create`, merge helper, no-follow cleanup + cache prune job. Fake remote (a local bare repo) and fake `gh` in tests | — | 1 | `config.py` (`RunnerConfig`), `secrets.py` (the `runner/` prefix, size cap check), `resonant/runner/` (new: `__init__.py`, `workspace.py`, `checks.py`, `cleanup.py`), `tests/test_runner_workspace.py`, `tests/test_runner_checks.py` |
| P5A-3 | `ClaudeRunner` + migration 006 `runs`: steps, session id save and resume, reaper (psutil walk + pgid, startup reap by start time), budget and usage-limit wait, transcript writer (line cap, two-token redaction), timeout, kill switch + cancel, pulse progress item, replies, runner registration | 1, 2 | 2 | `runners/claude.py` (new), `runners/reaper.py` (new), `store/migrations/006_runs.sql`, `store/runs.py` (new), `daemon.py`/`wiring.py` (register the runner, startup reap), `tests/test_runner_claude.py`, `tests/fakes/claude` (fake CLI) |
| P5A-4 | Run tools and slash commands: `start_run`, `list_runs`, `cancel_run`, `merge_run`, `/run` `/runs` `/cancel` `/merge`, setup-missing replies, `/kill` run count, `/status` runs line | 3 (and the NL PR merged, for `commands.py`/`fast_path.py`) | 3 | `tools/runs.py` (new), `tools/builtin.py` (registration), `router/commands.py`, `router/fast_path.py` (renders), `killswitch.py` (count), `tests/test_tools_runs.py` |
| P5A-5 | `run_project` intent wiring: `INTENT_TOOLS`, drop `ESCALATE_REPLY`, prefix-hash golden, labeler eval cases | 4, NL PR | 4 | `router/intents.py`, `router/prefixes.py`, `runners/local.py` (escalate path), the router eval cases (`evals/router_eval.py` and its fixtures), `tests/test_router*.py` goldens |
| P5A-6 | e2e (`tests/test_e2e_runner.py`, §5), go-live step 14, `--check` runner rows, `docs/test.md` rows, `interfaces.md`/`architecture.md`/`followups.md` updates (close "Runner sandboxing", narrow "Seatbelt sandbox" to extensions), README | 4 (R-tests through `/run`); the chat rows of R1 and R9 need 5 | 4 | `tests/test_e2e_runner.py`, `scripts/go_live_check.py` (runner rows), `docs/*`, `README.md` |

Waves 1 and 4 each run two tickets in parallel with no shared files (P5A-6's chat assertions are written against P5A-5's intent and rebase on it if it merges first; they don't share files). P5A-1 and P5A-6 both edit `scripts/go_live_check.py`, but in different waves.

## 5. Exit criteria (e2e, `tests/test_e2e_runner.py`, fake `claude`, real sandbox on macOS)

| # | Scenario | Pass condition |
|---|---|---|
| R1 | "fix X in resonant" (and `/run resonant …`) | `On it: R1 …` at once; one PR on the fake remote with the fake Claude's commit; `R1 done: PR #1 …`; pulse item opened then cleared; `/runs` shows `ready`. |
| R2 | Escape attempts from the fake Claude | Reading `~/.resonant/resonant.db`, running `resonant approvals approve`, `security find-generic-password`, `curl` to `:7777` on loopback, writing `~/Library/LaunchAgents/x.plist` and `~/.zshrc`, `launchctl submit` all fail; the run still finishes. (The local-only probes of D6 cover launchd XPC, CGEvent, `task_for_pid` and the LAN IP.) |
| R3 | Planted `.git/config` (`core.fsmonitor`, `credential.helper`) and `.git/hooks/pre-push` in the clone | Nothing runs outside the sandbox (a marker file outside stays absent); the push works. |
| R4 | `.github/` change, `.gitmodules` change, a token in a commit message | Each refused with `R1 failed: …` naming the reason; nothing pushed. |
| R5 | Sensitive paths: the fake Claude edits `Makefile` and `.claude/settings.json` | Pushed; the done reply and `/merge R1` both say `⚠ changes .claude/settings.json, Makefile`. |
| R6 | Daemon crash mid-session | On start, the old process tree is reaped before the resume; resumed with the same session id; one PR, not two. |
| R7 | `/kill` mid-session | The process tree is gone within 15 s, run `cancelled`, nothing pushed, `/kill` reply counts 1 run. |
| R8 | A `setsid` child and an `http.server` left running by the fake Claude | Both gone before harvest; nothing of the run alive after it ends. |
| R9 | `/kill` (and, separately, `/cancel R1`) during `publish`, between push and PR create | No push, or no PR after a push; the run is `cancelled`. |
| R10 | Timeout | Killed at `max_minutes` (shortened in the test); `failed`, work kept, no push. |
| R11 | Usage limit, then reset | `waiting`, slot and lock freed; resumes the same session; the wait isn't counted as an attempt or session time. |
| R12 | Third concurrent run; second run on the same repo | The third waits for a slot; the same-repo one waits for the repo lock. |
| R13 | `/merge R1` with CI red, then green | Refused, then merged with `--match-head-commit`; "merge it" in chat asks `yes`. |
| R14 | No repos / no token | `The runner isn't set up: …` naming exactly what's missing. |
| R15 | Planted symlinks and junk: `out.bundle` → `~/.resonant/resonant.db`, a run-dir file → `~/.zshrc`, a bundle with an extra ref, a 2 MB stream-json line | The bundle is refused (not a regular file / extra ref); nothing outside the run dir is written or deleted, including by cleanup; the long line is truncated, the daemon's memory stays bounded. |
| R16 | Token hygiene | Neither token appears in the DB, the logs, argv (`ps`), the clone's `.git/config`, the transcript (including after the fake Claude prints its `env`), or any reply. |
| R17 | Self-probe fails (a test profile with the `securityd` deny removed) | The run fails before the session with `the sandbox check failed`, and an alert goes out. |

## 6. Owner steps (terminal on the Mini, once)

1. `claude setup-token`, then `resonant secret set runner/claude_token` and paste it. (P5A-2 checks two things: the secret store caps values at 128 bytes and a setup token is about 108; and `secret set` accepts the core `runner/` prefix, not only installed extensions.) Or `runner/anthropic_key` for API billing.
2. A fine-grained GitHub PAT for **only** the runner repos: Contents read/write, Pull requests read/write, Commit statuses and Checks read, Metadata read. **No Workflows permission.** Then `resonant secret set runner/github_token`.
3. Name the repos. Claude adds them to `runner.repos` in `~/.resonant/config.yaml` (not a secret) and restarts.
4. For each runner repo, on GitHub: **no secrets available to `pull_request` runs from branches of the same repo** (a runner PR can change a test, a Makefile or `package.json` that CI executes, before any merge). Either the workflows that use secrets run only on `push` to `main`, or the secrets live in a GitHub environment that requires approval. Claude can check each repo's workflows and say which need a change.
5. Recommended: branch protection on each repo's `main` requiring CI, so `/merge` can never land red code.

Until steps 1–3 are done, everything else is deployable and testable: `resonant sandbox check` (Claude runs it), `/runs`, and "fix X in Y" → `The runner isn't set up: …`.

## 7. Risks

- **Allow-default runner profile.** Anything not enumerated is allowed. Mitigated by deny-by-default **writes** (no persistence), deny-default unix sockets, unconditional mach denies for every service known to act outside the sandbox (launchd, window server, HID, accessibility, Apple Events, LaunchServices, Keychain), and probes for each; `resonant sandbox check` re-runs after OS updates and a self-probe before every run. Residual: an unknown mach service that acts on the user's behalf.
- **Separate-user fallback (deferred 2026-10-15).** The owner chose option A (no secrets in env or argv) instead; this stays as hardening in `followups.md`. If the spike can't deny one of the D1 services without breaking `claude`, git or node, runs execute as a dedicated macOS user (`_resonant-runner`, no Keychain, no access to the owner's home) via a `sudo`-free launchd helper. That needs one more owner terminal step (creating the user) and is decided at the end of P5A-1, before P5A-3 starts.
- **Exfiltration.** The session has network and can send the repo's code or its own Claude token anywhere. Accepted (the owner's own repos, a revocable token). The GitHub token is never inside.
- **Prompt injection from the repo** steers Claude, within the wall; the result is a PR the owner reviews before `/merge`. Changes to files CI or tooling executes are flagged in iMessage (D3), and step 4 of §6 keeps repo secrets away from PR CI.
- **Shared runner cache** can be poisoned by one run for the next. Accepted: every run is owner-initiated, and nothing outside the sandbox uses that cache.
- **Remote Login.** ssh to the Mini isn't blocked by port; the sandbox can't read `~/.ssh` or reach an agent socket, so it has no credential. Keep Remote Login off or key-only.
- **`sandbox-exec` deprecation.** If a macOS update removes or weakens it, the self-probe fails closed and no run starts; the fallback is the separate user above.

## 8. Deferred (to `followups.md`)

- The deny-default **extension** profile: extension processes under the sandbox, opt-in per extension (`sandbox: true`), default later once the github and example extensions are proven under it.
- Follow-up instructions on a run ("also add tests" → resume the same session on the same branch) and PR review comments fed back.
- Runs on repos not in the allow-list, or creating new repos.
- Network allow-listing by domain (a filtering proxy, as Claude Code's own `/sandbox` does).
- Members (non-owner) starting runs.

## As built

### P5A-1 spike (macOS 27.0.1 / 26A434, the Mini)

Probes run under the real `runner.sb` (`resonant.sandbox.probes`). "Control" = the same call outside the sandbox succeeds, so a denial is a denial.

| Probe | Result |
|---|---|
| Read `~/.resonant`, `~/.claude`, `~/Library`, `~/.local/bin`, `$HOME`, `/Volumes`, `/Users/Shared`, `/private/tmp`, `/private/var/folders` | EPERM (control: readable) |
| Allow-list reads (`~/.local/share/claude`, `uv/python`), `realpath`/`getcwd` in run dir | OK (needs `file-read-metadata` on every ancestor of each allowed path, incl. the run dir's) |
| Write `~/Library/LaunchAgents`, `~/.zshrc*`, `~/.gitconfig*`, `~/.claude`, `$HOME`, `/Volumes`, `/Users/Shared`, `/private/tmp`, `/Library` (unique nonexistent names) | EPERM, nothing created. `~/.ssh` does not exist here (ENOENT, inconclusive; covered by the `$HOME` rule) |
| Exec `security`, `osascript`, `open`, `launchctl`, `sudo`, `crontab`, `screencapture`, the `resonant` entry point | EPERM. `/bin/ps` is setuid, so no sandboxed process can exec it at all |
| Keychain (throwaway keychain item, then `SecKeychainCopySearchList`) | Denied. Real `/usr/bin/security` with the exec deny lifted could not read the item; a denied call fails in about 20 ms, no hang. Copied system binaries are SIGKILLed by AMFI, so copies are not a valid probe |
| launchd job submit from Python (`launch_msg` SubmitJob) | Refused (errno 1). Control (outside) accepted a job. Refused even under a bare `(allow default)` profile: launchd rejects submits from any sandboxed caller. `com.apple.xpc.launchd*` mach-lookup is not what blocks it (denying all mach-lookup changes nothing) |
| CGEvent / HID | `IOServiceOpen(IOHIDSystem)` refused (control opened); window-server lookups denied; `CGDisplayCreateImage` none. `CGEventPost` itself is not observable here (no effect even outside, TCC), so it rests on the lookup and IOKit denials |
| Apple Events, `lsopen` | `osascript` runs and AppleScript works with every mach-lookup denied, so the mach deny alone does NOT stop Apple Events. Added `(deny appleevent-send)` and `(deny lsopen)`; `sandbox_check` now reports both denied. Not exercised end to end (a real Finder event hangs on a TCC prompt outside the sandbox too) |
| `task_for_pid` / `task_name_for_pid` on another pid | Denied (control: name port obtained) |
| Signals to other pids; own children | Other: EPERM. Own children killable. Needs `(require-not (target same-sandbox))`; `(target others)` blocked killing the session's own children |
| `*:7777`, `*:11434` connect on 127.0.0.1, ::1, LAN IP (192.168.4.50); bind | EPERM; another loopback port refused normally (connectable) |
| Unix sockets | Denied except mDNSResponder (ssh-agent socket denied; a run-dir socket denied) |
| git clone/commit/bundle, `uv run pytest` from the runner cache, `http.server` on loopback, HTTPS to github.com | OK. `/usr/bin/git` is an xcrun shim that tries to write `/var/folders/.../T`; use the real git (`xcrun --find git`) in the run-local bin. `npm`: not installed on the Mini, untested |
| `claude --version` | OK, 20 ms |
| `claude -p` with fresh `CLAUDE_CONFIG_DIR`, no token | `Not logged in`, about 430 ms: fails fast. Needs `CLAUDE_CODE_TMPDIR=<run>/tmp` (Claude hard-codes `/tmp/claude-<uid>`). Authenticated run is an owner step |
| Another process's argv/env via `sysctl KERN_PROCARGS2` | **Not deniable** (known limitation, shown as an informational row in `resonant sandbox check`). A sandboxed process reads a same-user, non-SIP process's environment (the control does too). Denying `process-info*`, `signal`, `mach-task-name`, `mach-priv-task-port`, `system-info` and `sysctl-read` (by name, prefix, or deny-all with an allowlist) changes nothing. SIP binaries hide their env; Python, `git`, `gh` and the daemon do not. Closed by **option A** below |
| Work dir: create/symlink/mkdir in `work_dir`, rename the run dir, write or symlink in a sibling run dir | All denied (only `work_dir/<code>` is writable), so the daemon's `O_NOFOLLOW` opens of a run dir are safe |
| `ps eww` equivalent | `ps` can't be exec'd in the sandbox (setuid); the sysctl behind it is the limitation above |

**Decision (owner, 2026-10-15): option A, no secrets in any process's env or argv.** The separate-user fallback (§7) is not taken; it is deferred to `followups.md` as hardening. Because `KERN_PROCARGS2` stays readable, the rule is that a secret never sits where it can be read. Other tickets own the plumbing: P5A-2 gives `git` and `gh` the token over an inherited fd or a 0600 file under `~/.resonant`, never env or argv; a new ticket covers extension secrets. The one deliberate exception is `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) in the session's own environment: Claude Code reads it only from there, and it is the session's own credential, visible only to other processes of the same sandbox and to same-user processes outside it. P5A-1 contributes the guard `daemon env holds no secrets` (a local probe in `resonant sandbox check`): neither this process, the LaunchAgent plist's `EnvironmentVariables` nor the running daemon's environment may hold a `GH_TOKEN`, `GITHUB_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `RESONANT_SECRET_*` or a token-shaped value (`ghp_`, `github_pat_`, `sk-ant-`, ...). It reports names only, never values, and cannot compare against `resonant/*` Keychain values (they are not read).

Built: `resonant/sandbox/` (`runner.sb` template; `profile.py` with `SandboxParams`, `render`, `wrap`, `scratch_env`, `link_tools`, `prepare_dirs`; `probes.py` with 26 probes, `self_check` and `cached_self_check`), `resonant sandbox check [--scope all|ci|local] [--json]`, the go-live row `runner sandbox` (advisory), `tests/test_sandbox.py` (CI-safe, runs on the existing macOS CI job) and `tests/test_sandbox_local.py` (skipped when `CI` is set).

Notes for the runner (P5A-3): use the real git (`resolve_tool` does `xcrun --find git`) in the run-local `bin/`; `scratch_env` sets `CLAUDE_CODE_TMPDIR=<run>/tmp`; the self-check cache file path is the caller's (`cached_self_check(params, path)`); the `setsid` reaper probe belongs to P5A-3 (here only "a new-session child stays sandboxed"); Claude's own `sandbox.enabled: false` run setting is P5A-3's. The authenticated `claude -p` is an owner step: `CLAUDE_CODE_OAUTH_TOKEN=... resonant sandbox check`. `npm` is not installed on the Mini and was not tested.

Addendum (P5A-2 review): runner tokens are briefly on disk under `runner.secrets_dir` (default `~/.resonant/run-secrets`). `SandboxParams.secrets_dir` is denied for read and write with its own rule, `resolved()` refuses a configuration where it overlaps the run dir, cache dir or any read path, and the probe `secrets_dir unreadable` lists, stats, reads and writes a canary in the configured dir (made 0700 and removed afterwards if missing; a nonexistent name under a denied dir answers ENOENT, so real entries are needed). `resonant sandbox check` takes the dir from `settings.runner.secrets_dir` once P5A-2's config lands, else the default.

Review fixes: `SandboxParams.secrets_dir` is required; `resolved()` refuses a run or cache dir that is, holds or sits inside `$HOME`, `~/Library`, `~/.claude`, `~/.ssh`, `~/.config`, `~/.resonant` or a shell or git startup file, and the profile ends with unconditional write denies for those paths and for `/dev/ttys*` and `/dev/ptmx`, then the secrets_dir deny, after every allow. `/dev/tty` is the only terminal device a session may write (headless `claude -p` with pipes needs no pty). `resonant sandbox check` and the go-live row are built from `settings.runner` (`work_dir`, `cache_dir`, `secrets_dir`) so they test the profile a real run uses; the probe run dir is a direct child of `work_dir`, as a real run's is, and the check removes any dir it had to create.

### P5A-3 (ClaudeRunner and migration 006)

Built: `store/migrations/006_runs.sql` + `store/runs.py`, `runners/claude.py` (`ClaudeRunner`, `TranscriptWriter`, `cleanup_job`), `runners/reaper.py`, registration in `wiring.py` and `daemon.py`, `tests/fakes/claude`, `tests/test_runner_claude.py`.

- **Public API** (what P5A-4's tools call): `await runner.start_run(repo, ask, principal) -> Started | Refused`, `runner.list_runs()`, `await runner.cancel_run(code) -> Cancelled | Refused`, `await runner.merge_run(code) -> Merged | Refused`, `await runner.setup_missing()`, `runner.last_killed` (runs the last `/kill` ended). The runner is `IMessageStack.claude` and `components.runners["claude"]`. `Started.text` is the "On it" reply. Merge is the workspace operation only (CI check + `--match-head-commit`); the L2 approval around it is P5A-4's.
- **`runs` table.** The ask is stored in the row (the P5A-3 spec), not only in the task checkpoint. `step` (prepare/session/harvest/publish/reply) is the resume point; `attempts` counts daemon crashes survived (a second crash fails the run); `sessions` picks `--session-id` (first) or `--resume`. Codes come from an AUTOINCREMENT key and are never reused.
- **One step per run**, holding the slot and the repo lock the whole time, so a run never queues behind other work between phases. A busy repo lock, a full runner (`claude.max_concurrent` minus the on-call slots) or a paused Resonant returns `Wait("usage_reset", +defer_s)` and holds no slot. The loop has no "resource" wait reason, so `usage_reset` is reused; the run's own status (`queued`) is what `/runs` shows.
- **The prompt is on stdin** (`claude -p` with no argument), not argv: no ask in `ps`, no leading-dash trouble. Claude's own sandbox is turned off with `--settings '{"sandbox":{"enabled":false}}'`, since `--setting-sources project` skips the run-local user settings file. `--max-budget-usd` is passed only with an API key.
- **Reaper** finds a run's processes four ways: the leader's tree, descendants sampled while it ran, the process group while it is provably the run's, and any own-user process with `RESONANT_RUN=<code>` in its environment or a cwd inside the run dir. The env marker is what catches a `setsid` grandchild whose parent died, including after a daemon crash. A pid is never signalled unless its start time matches the saved one.
- **Kill switch.** A pause hook cancels every active run and reaps live sessions at once; a pause set elsewhere (`resonant kill` from the CLI) is noticed by the session watcher within `poll_s` and by the pre-publish re-check. `cancel_run` cancels the loop task (`Loop.cancel`) and the watcher also notices the cancelled row, so either alone works.
- **Cleanup job** `core/run-cleanup` (daily 04:30, catch-up 12 h) is registered by the daemon (`core_jobs=(*CORE_JOBS, cleanup_job)`), not in `CORE_JOBS`, to keep `scheduler.py` untouched. Not scheduled when `runner.repos` is empty.
- **Unverified on the real CLI** (the authenticated `claude -p` is an owner step): the exact usage-limit result shape (`usage_limit()` reads an error result whose subtype or text names a limit, with a reset epoch in `resets_at`/`reset_at` or after a `|`; an unreadable reset waits one hour), and the inline-JSON form of `--settings`. Each is one function or one argv entry.
- **Known gaps.** A crash between the spawn and saving the pid leaves a process that only the env marker can find (the startup reap does look for it). A `--resume` of a session Claude never wrote fails the run. A failed self-check is logged and audited (`run.sandbox_failed`) and fails the run; a texted health alert for it is not wired.

P5A-3 review changes:

- The env marker the reaper matches is the run's **session id** (a uuid), not the run code, so a second daemon instance's `R1` is never touched. Matching by cwd skips processes with a controlling terminal (the owner's shell).
- **Tree kill is best effort inside the sandbox**: a process that clears its environment, changes directory and leaves the group is not found by marker, cwd or tree. It stays inside the sandbox (no `$HOME`, no secrets, no launchd), so the residue is a stray process, not an escape.
- `start()` failing leaves the runner blocked: every step defers (audit `run.startup_failed`) until a later `start()` succeeds, so nothing resumes before the old processes are reaped.
- `/kill` and `/cancel` use SIGTERM, then SIGKILL after 2 s; cancel also reaps anything marked to the run (a running harvest) before the run dir is deleted.
- A session counts (and is resumed with `--resume`) only after Claude's system message with the session id arrived; otherwise the next start is a fresh `--session-id`. The saved process is cleared only once the tree is really gone; if it won't die the run fails.
- The watchdog also covers the shared runner cache: over `runner.max_cache_gb`, the run is killed. The daily cleanup also runs with no repos configured when the work or cache dir exists.
- `pr list` uses `--state all`, so a PR merged or closed in between is reused, not duplicated. During a usage wait the pulse item reads "waiting for usage reset".
- Busy/full/paused keep `Wait("usage_reset")`; a `resource` reason is a follow-up ([followups.md](followups.md#a-resource-wait-reason-for-runner-runs)).
- `--max-turns` is hidden and undocumented in Claude Code's help; confirm on the first real run (test.md row 34). `--setting-sources project` makes Claude obey the repo's own `.claude/settings.json` inside the sandbox (accepted, as in D2). Whether `sandbox.enabled: false` takes effect is an owner step (test.md row 34). The redundant `autoUpdates` setting was dropped (`DISABLE_AUTOUPDATER=1` is in the env).

### P5A-4 and P5A-5 (run tools, commands and the `run_project` intent)

Built together: `tools/runs.py`, `router/commands.py` (`/run`, `/runs`, `/cancel`, `/merge`), `router/fast_path.py`, `router/render.py` (`render_runs`), `router/intents.py`, `router/prefixes.py`, `runners/local.py`, `status.py`, `evals/router.example.jsonl`, the prefix goldens, `tests/test_tools_runs.py`.

- **`start_run` repo enum.** Builtin specs are static, so `register_builtins` lets a module export `specs_for(ctx)`; `tools.runs` uses it to put the configured `runner.repos` names in `start_run`'s schema as an enum (and in its description). With no repos there is no enum and the description says so. The handler also refuses an unknown name before calling the runner.
- **Refusals are results, not failures.** `start_run`, `cancel_run` and `merge_run` return `{..., text}` for a refusal (not set up, unknown repo, CI red), and `runners.local.REPORTS` sends `text` as is, because `ToolFailed` details are clipped to 120 characters, which would cut the not-set-up sentence.
- **Merge approval.** `/merge R3` is explicit and runs (L2, owner). From chat it asks a plain yes; the prompt heading is `merge R7: PR #41 in dori <url>, changes Makefile`, built from the run's row (`tools.runs.summary_action`, registered in `components.summary_actions`).
- **ESCALATE_REPLY is gone.** `run_project` is an ordinary tool intent. A model asking for more than three tool calls still ends the task, now with `TOO_MANY_REPLY` and the same `escalate=true` result flag.
- **Labeler.** The labeler only labels; repo and ask are extracted by the runner model's `start_run` call, guided by a `run_project` rule in the runner system message. The labeler definition and two few-shots (`how's R3 going`, `merge R3`) changed, so the golden prefixes (both fixtures) were regenerated.
- **`/kill` count** comes from `FastPath.runs_killed`, which `wiring` points at `ClaudeRunner.last_killed`. `/status` and `daemon_status` carry `runs` (counts per status).
- **Not-set-up without a runner** (iMessage off): `The runner isn't set up: the Claude runner isn't running. On the Mini: set channels.imessage.enabled …`.
- **Run codes vs approval codes.** `R` was removed from `CODE_LETTERS` (approval codes never start with R), and `clean_external` masks only approval-alphabet look-alikes, so `R3`, `k8s`, `s3` and `src/a1.py` are no longer mangled.

### P5A-6 (runner e2e, go-live step 14, docs)

Built: `tests/test_e2e_runner.py`, `scripts/go_live_check.py` (`runner_rows`), `docs/go-live.md` step 14, `docs/test.md` (Phase 5a rows consolidated and renumbered: phone rows 14-15, Mini rows 16-36), `docs/architecture.md`/`.html`, `docs/interfaces.md`, `docs/followups.md`, `README.md`.

- **E2E harness.** The real `Daemon` (router, tools, gate, loop, `ClaudeRunner`) with a `FakeChatDB`, `RecordingOsaRunner`, a scripted model that knows the two run phrasings, `tests/fakes/claude`, a local bare repo and the fake `gh`. `wiring.ClaudeRunner` is monkeypatched to add the test seams (workspace, identity sandbox, fake CLI, short polls). Covered through iMessage: R1 (`/run` and chat, pulse item opened then cleared, `/runs`, flagged paths in the done and `/merge` replies), `/cancel` mid-session, `/kill` with escapees (reply counts 1 run, tree gone, nothing pushed, `/resume` doesn't restart it), R6 (daemon stops without reaping, the next daemon reaps the old tree and resumes the same session, one PR), R13 (`/merge` refused on red CI then merged with `--match-head-commit`; "merge it" asks yes, then merges), R14 (nothing set up, only a token missing), a repo without CI, R4 (`.github/` change and a token in the diff, never echoed), and one local-only run under the real `sandbox-exec`.
- **Left to `test_runner_claude.py`** (runner-level, no iMessage needed): R2/R3 escapes and planted git config (sandbox tests), R7/R8 reaping, R9 between push and PR, R10 timeout, R11 usage limit, R12 concurrency, R15 symlinks and long lines, R16 transcript token hygiene, R17 self-probe failure.
- **`--check` rows** (all advisory, read-only, names only): `runs table` (schema 6), `runner repos`, `runner secret runner/github_token`, `runner secret runner/claude_token (or runner/anthropic_key)`, and per repo `runner repo <name> has CI` (reads the existing mirror with `Workspace.repo_has_ci(refresh=False)`; `SKIP` until the first run has fetched it, since the check never touches the network). `runner sandbox` is P5A-1's row.
- **Observed in the e2e.** A chat request's first reply is the model's own text followed by the code-built `On it: …` line (`Working on it.` / `On it: R1 …`). The chat merge prompt is `[F6] merge R1: PR #41 in demo <url>, changes … Reply yes / no`: the model-chosen L2 is asked with a code-labelled prompt that a plain `yes` answers. The run's own `R1 cancelled.` text arrives after the `/cancel` reply, so a test counting texts must not assume order.

### RP-9 (secret-file denies, repos plan)

Built: the last blocks of `runner.sb`, `HOME_SECRET_DIRS`/`HOME_SECRET_FILES` and `SandboxParams.deny_read` in `profile.py`, `runner.deny_read` in `RunnerConfig`, three probes (`secret file names`, `credential files under $HOME`, `runner.deny_read paths`).

- **What is denied, for read and write.** Anywhere on the host outside the run dir, the cache and the toolchain read paths: files named `.env`, `.env.*` (not `.env.example`, `.env.sample`, `.env.template`), `.envrc`, `*.pem`, `*.p12`, `*.pfx`, `*.jks`, `*.key`, and `id_rsa`/`id_dsa`/`id_ecdsa`/`id_ed25519` (with optional `_sk`; `.pub` stays readable). Under `$HOME`, rendered one subpath each from `HOME_SECRETS`: `.ssh`, `.aws`, `.config/gh`, `.config/gcloud`, `.azure`, `.kube`, `.gnupg`, `.netrc`, `.npmrc`, `.pypirc`, `.pgpass`, `.git-credentials`, `.docker/config.json`. Plus every path in `runner.deny_read`. All of it sits after the last allow, so it wins even where a read path overlaps.
- **Why $HOME was not enough.** `$HOME` is denied wholesale, but secrets also live outside it (a work dir on another volume, `/opt`, a path in `runner.deny_read`) and a toolchain read path can sit on top of a credential dir.
- **Case.** The macOS filesystem is case-insensitive, so `.ENV` opens `.env`; every letter in the name rules is a `[xX]` class.
- **Exemptions.** The run dir and cache (a repo's own committed `.env.test` or dev cert), and the toolchain read paths (a package's bundled `cacert.pem`). The read-path exemption means a credential dir must never be added to the read paths; the `$HOME` list above still wins there. Also the CA-bundle locations only: `/private/etc/ssl`, `/System/Library/Security`, `/opt/homebrew/etc/ca-certificates`, `/opt/homebrew/etc/openssl@3`, `/opt/homebrew/share/ca-certificates`, `/opt/homebrew/Cellar/ca-certificates`. Checked inside the sandbox: curl, Apple git (`ls-remote` over https), gh and python urllib all reach github.com. A cert bundle elsewhere would need adding to the list.
- **SBPL notes.** `#"..."` is a raw string, so `\.` reaches the regex as an escaped dot; the dialect is POSIX extended with no lookahead, so the template allow-list is a `require-not` inside a `require-all`. A missing path answers ENOENT, not EPERM, before the sandbox is asked, so the probes use real files: the name probe makes a scratch dir under `/private/var/tmp` (outside `$HOME`, otherwise readable), and the `$HOME` probe plants every `HOME_SECRETS` entry in a fake home with a read path laid over each (only the final denies can close them; a sibling file is the control).
