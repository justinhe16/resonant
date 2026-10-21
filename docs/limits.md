# Known limits and trade-offs

What Resonant does not do, or does on purpose in a way that has a cost. Each was accepted when it was built. Most say what to watch for and what to do if it shows up. Planned work is in Linear (Resonant project, epic [PER-257](https://linear.app/doriapp/issue/PER-257)); designs that wait for a consumer are in [`extensions.md`](extensions.md#11-deferred-designs).

## Brain and Claude session

**qwen and the loop**
- **Cold prompt.** A first turn is about 5.2k tokens. Ollama keeps one slot, so after a fold, a pulse, an eval or a restart the next turn pays about 4.3 s of prefill instead of 0.05 s. Watch the `runner.brain.answer` spans; if it shows, give the brain its own slot (`OLLAMA_NUM_PARALLEL=2`, costs memory) or trim stubs a principal never uses.
- **Triage costs one call.** On a busy model a turn that would have been local pays the triage call first (80 tokens out, cached prefix). Measure in the BR-10 eval row (ack p50, local p50).
- **`looks_hard` is a regex floor.** English, shape-only ("is there a reason X broke?" isn't caught; the model's triage still can be). Widen from real misses; each widening costs Claude usage on small talk.
- **`asks_live_state` is a word list.** A live-state question in other words ("anything new on resonant?") answered with no tool isn't sent back. Widen from real misses, not guesses.
- **The ack is a model line, filtered.** `clean_ack` drops questions, links and result words, but an ack can still be odd. If model acks look wrong in practice, use the canned ones only.
- **Stub args.** A stub's schema has no descriptions, limits or formats, so a `minimum` or `pattern` is checked in code (`args_error`, one extra round).
- **The claim check is English and pattern-based.** Other phrasings of "I've saved …" get through. Report lines are code-built, so a missing report line is the owner's tell.
- **The target check costs a round** when the owner names the target in other words ("stop the dori run" for R1). It looks only at `code`, `id`, `number` and `*_id` args.
- **The guess check fires once per task.** A second guess in the same task runs.
- **The number guard only logs.** The task eval counts its hits (`number_guard_hits`). Bring back a hard guard if real misses show up.
- **A crash retry starts the turn over.** Tool results aren't checkpointed; reads replay from `executions` and a pinned side effect resumes. Fine at today's scale.
- **Briefing paths are absolute.** Rendering them as `~/code/…` made the model invent a home and fail every read.
- **`recent_changes` never fetches.** On a checkout nobody pulls it shows what was last fetched (`behind` and `fetched_at` say so). A periodic fetch would write to the owner's checkouts, so it isn't done.
- **`recent_changes` checks `.git` itself, not every object file.** A worker could symlink loose objects from another repo in, but that needs write access it could use to read that repo directly.
- **Eval noise and fixtures.** At temperature 0 the live loop still moves 2–4 cases between runs, so the bar is the mean of `--repeat 3`. The eval's `HOME` is the world root; the linear fixture copies the extension's no-optional-args contract (give it the MCP filters back if manifests get optional args).
- The intents path's limits go away with [PER-256](https://linear.app/doriapp/issue/PER-256).

**The Claude session**
- **A fresh session after outside reads.** A session that read outside content is restarted (primed) once that taint lifts. That costs a cold start a couple of turns after any web, file or log read. If that's too often, lower `brain.taint_hops`.
- **The tool list is fixed per session.** An extension installed or updated mid-session is offered from the next start (a rotation, a restart or a crash).
- **Stray `result` lines.** With Remote Control on, a message typed in the Claude app joins the same conversation and could finish a turn. Remote Control is off by default; treat it as the owner's own seat.
- **A limit hit isn't re-answered locally.** A usage limit shows only in the session's `result`, after the ack, so that turn's reply is the limit notice and the owner asks again.
- **One owner-only session.** Members can't escalate (`ask_claude` has no grant), and their media attachments are ignored. A per-member session would need its own server, kv and queue.
- **One-shot asks.** Images, and the media and watch asks, start their own `claude -p` and stay read-only. Email and calendar asks get "can't reach it". One-shot asks keep no transcript; add `transcripts/ask-<id>.jsonl` if debugging needs it.
- **Images always go to Claude.** qwen3 has no vision. A local vision model could answer simple ones.
- **A media ask's answer isn't replayed** in later turns; only answers to brain tasks are (`<claude_answer>`). Those are noted after the send and retried for about 10 s; past that they're missing from history.
- **No daily cap by default.** `daily_cap: 0`; the runaway guard (60 an hour across the brain, one-shot asks, media and watch checks) is the only automatic stop besides Claude's own limits. Watch `ask.queued` and `ask.notice` audit counts.
- **Live CLI behavior not checked:** whether auto-compaction keeps `--append-system-prompt`. Re-check after CLI updates; `scripts/probe_claude_rules.py` re-checks the deny rules and `--max-turns` (PER-262).
- **Deny globs by case** (PER-262). Claude Code matches glob rules by case and takes no `[eE]` classes, so each glob is written as given, lower, upper and capitalized. Another mix (`.eNv`) gets past Claude's own Read, Grep and Glob; path rules ignore case, the read tools fold case, and `--add-dir` keeps Claude to the roots.
- **An outside pause lands within `pause_check_s`** (PER-262, 5 s) for an idle session; a `/kill` in the daemon is at once.
- **Transcript sweep** (PER-262). Only the brain's own `~/.claude/projects/<cwd>` dir, for the `HOME` the CLI runs with. The dir name follows Claude Code's naming as seen on 2.1.305; if a later CLI names it differently, the sweep finds nothing (it never guesses).
- **Gmail tool names are a guess** (`brain.ask_claude.connector_tools.email`). Check them against the init event once Gmail reaches the session (Phase 7).
- **Voice transcripts are chat only.** Never a command, approval or worker reply, so `/restart` or `R4 yes` must be typed. Voice approvals would need a confirmation step.
- **Watches.** A watch's tool and args are fixed at creation, so "watch it" after a long thread depends on the model; the approval prompt shows exactly what will be checked. Checks run at most every 5 minutes, behind owner chat. Brain notices have their own hourly cap (`extensions.notify.max_per_hour`), not shared with extensions.

**The Claude brain's shell (PER-258)**
- **Most shell changes ask.** A Bash read taints the turn, so "commit and push" after a `git status` asks. If that gets noisy, leave outputs with no outside text (`rev-parse`, `rev-list --count`, `branch --show-current`) out of taint.
- **A repo's own config runs on reads.** `git status` in a repo whose `.git/config` sets `core.fsmonitor`, a pager or an external diff runs it. The brain's repos are the owner's own.
- **The tracked directory is a heuristic.** It follows `cd` and the CLI's cwd-reset line, not `pushd` or a `cd` inside `||`. A wrong directory only changes the approval line and asks again; it never runs.
- **Compound changes are always L2,** and a heredoc commit message (a substitution) asks. Splitting compounds into segments is a later option.
- **Grants** last 30 minutes, keyed on the exact command and directory (an edit's on its digest).
- **Residuals from review.** `ps aux` (pre-allowed) shows other processes' arguments. A submodule counts as a linked worktree. The scratch dirs include the whole system temp dir. A script Claude runs asks with only its path shown.

- **System texts the brains see** (PER-268). The Claude brain gets at most 6 of the last 24 h at the top of its next turn and a fresh session starts without the older ones; qwen shows at most 3 between two exchanges. Results and end texts that never went out (a refused send, a digest-level end) aren't recorded. Approval codes in them are masked, as in any forwarded text.

## Taint

- **Laundering through a note or a fact.** A tainted turn can save a note or a fact without asking, and a later clean turn sees it (the facts index is in every prompt). The report line shows what was saved. If it shows up, make `list_notes` taint, or mark facts saved in a tainted turn and taint the turns that load them.
- **Laundering past the window.** Taint lasts `brain.taint_hops` (3) turns from the source, so a model that keeps repeating injected text reads clean after that. Raise `brain.taint_hops` if it shows up.
- **The summary drops what tainted replies said.** "What did that file say?" past the window has nothing to go on; the model can read it again. Keeping the text would taint for good.
- **Deleting a note and cancelling a reminder still ask.** Exempt them too if those asks get noisy.
- **Taint relies on the stream's order.** A change waits up to 3 s for its own `tool_use` so every earlier read counts. If a CLI update runs the MCP call first, every change asks (fails closed). Re-check after CLI updates.
- **WebFetch can carry data out** in a query string. Accepted: secrets are out of reach and the roots are the owner's code. A domain allowlist would narrow it.

## Approvals and gate

- **Standing allows are per tool and scope.** A `yes always` on `github.merge_pr` in a repo covers every PR there. Narrow it with an `ask` rule.
- **The injection budget is five effects.** One injected read in a chat task can steer up to five unasked changes (L3, or a standing allow); a tainted task's changes all ask. A per-task cap after an extension read is a cheap tightening if extension reads grow.
- **Restart and update take `yes always`.** With one, a steered model can restart or update the daemon unasked (update only pulls `main`). Forget the rule, or add them to `trust.tools.NEVER`.
- **No cumulative spend cap.** Below `trust.cost_ask_over_usd` a seen action never asks, however often. A negative `cost_usd` never asks.
- **Caps are per task.** A brain turn can run 5 changes, hand off, and the Claude task 5 more. The escalation note tells Claude what ran; nothing enforces it.
- **The owner answers every batch,** even when a member could approve every change in it.
- **A lone change's TTL starts in the session.** A Claude session longer than `gate.approval_ttl_s` would see it expire before the prompt (safe, and not reachable with `timeout_s`). A batch's expiry restarts when it is prompted.
- **The kill switch blocks on `paused` only.** An approved batch runs with autonomy off. `/kill` sets both.
- **A template's own words are trusted** (PER-268). Args it doesn't name are shown after it, but `{~arg}` hides one on purpose (`head_sha` in `merge_pr`), and the phrase is the manifest author's (cleaned, owner-approved at install); a misleading one would under-describe its tool. `/approvals <code>` shows the full args.
- **The "approve it?" guard is a phrase list** (PER-268). A model that asks for a yes in other words still gets through; the prompt rule is the backup, and a real ask is never dropped (it only runs with nothing pending).
- **An extension's stderr reason is replayed untainted** (PER-268). The failure text's reason is the extension's (or GitHub's) last stderr line, cleaned and clipped to 200, and it reaches both brains as a system text. Accepted: it is one short line from Resonant's own extensions.

- **"Claude answer" is read off the text** (PER-261). The iMessage channel marks a send as a Claude answer when it starts with `Claude: `. Anything else that starts that way also asks a bare yes for its code (fails safe); a Claude answer sent through another channel isn't seen (only iMessage replies bind).
- **The expiry reminder runs once a minute** (PER-261), so it lands up to a minute after half the TTL; a prompt with under a minute left gets none. One a prompt, never repeated.
- **One request per action covers PR merges and reruns** (PER-269). Only `github.merge_pr`, `github.rerun_failed`, `merge_run` and a worker's `--action merge` check carry an identity; other duplicate asks (two paths deploying one thing) still each ask. A merge check whose `what` names no PR, from a run with no PR yet, has none.
- **A batched ask isn't made obsolete** (PER-269). A batch is answered as one unit, so a member whose PR merged meanwhile stays; its merge then fails as already merged.
- **A worker told `handled` isn't told the outcome** (PER-269). Its check isn't asked because Resonant (or another run) already asks for the same merge; if the owner says no there, the worker only learns it from the PR.

## Workers

- **Workers run unguarded** by default, as the owner at the keyboard. The checkout's `.git` is worker-writable, so a planted hook runs on the owner's next `git commit` there. Accepted.
- **Thin guard gaps** (only with `runner.guard: thin`). Existing hardlinks into a protected file aren't covered; port 7777 is fixed in the profile (derive it from `api.port` if that changes); `~/Library` and `~/.config` are write-denied whole.
- **The per-run App token covers one repo.** An ask that spans repos can't push to the others; widen through `resonant check` if it comes up.
- **Fork runs.** A fork run pushes before its checks, so a token they find is already public on the fork; rotate it. The PR-open race is caught after the fact (read back, closed). The App covering a new fork is confirmed only on the first real fork run.
- **Anonymous repo registration.** Without the daemon token (or through an extension's `via` link) a public repo is registered `push`, and its runs fail to push; re-add with the token for fork mode. The lookups share GitHub's 60/h per-IP limit.
- **Narrowed flagged paths.** Makefiles, Dockerfiles, `conftest.py`, `tox.ini` and new executables merge without asking; lifecycle scripts are listed as `deps changed`. Add any back to `runner/checks.py` if that proves too loose.
- **Side-effect reporting.** `did:` notes are honor-system. Detection reads command text only: it misses what a script, a program, a subagent or a neutrally named MCP tool does, skips refs in variables, and lists failed commands too. Widen `runners/side_effects.py` from real transcripts. A detected merge is said only in the run's end text (PER-268), so a long run that merges early is heard about when it ends.
- **A merge-only run is told by the activity feed** (PER-268). A run that pushed nothing ends `done` only when it ran `gh pr merge` itself and the feed shows a PR merge by the app's bot onto its base; a merge by other means (`gh api`, an MCP tool) or with no GitHub App (no feed) ends `no changes`, with the merge on its `did:` list when detected. A `branch_deletion` of the base still counts as a direct push, past the ticket's "push and force_push".
- **Model-chosen `answer_worker`/`message_worker` run unasked.** A worker's summary could nudge the model into messaging another run; the deterministic paths cover the usual replies.
- **Small gaps.** Time blocked in `resonant ask` (up to 8 minutes) counts as session time. A renamed repo's checkout isn't moved. `/run blog fix x` with a repo only GitHub knows is a scratch run unless written `/run blog: fix x`. The relay's "which one?" stash and the brain's pick aren't in `/approvals` or `/runs`. No default trust rules and no per-repo auto-merge default ship (owner decisions).
- **A `runner.repos` entry resets per-repo settings.** It sets `allow_github_changes` and `quote_ask` back to the config defaults.
- **`gh` only from Homebrew.** The github extension finds `gh` only through `/opt/homebrew/bin` on the extensions' PATH.
- **Remote Control uses an undocumented control request.** A CLI update can break it: runs then say `Remote Control unavailable` once and the `--check` row fails. Re-check after each CLI update; `runner.remote_control: off` stops asking. Token-only logins likely get that error by design.
- **Remote sessions.** A process killed without Resonant's stop (a crash, `kill -9`) leaves its remote session unarchived; archive it in the app by hand. Each `--resume` opens a new remote session; `/runs R<n>` shows the current URL. The session ends with Resonant's turn, and a very short one may say nothing about Remote Control. The end grace is a fixed 15 s.
- **The disk watchdog is a net, not a wall** (PER-267). What it can't read (a `chmod 0` folder, a tree deeper than 256 levels, a folder gone mid-walk) is skipped, so a count can be low; it no longer fails closed. A run is stopped only when what it counted is over its cap (`max_run_dir_mb` per run folder and worktree, `max_cache_gb` for the cache), or when the volume has under `min_free_disk_mb` free and the count grew by 1 MiB or more since the last check. On a Mac that sits under the floor, every run that keeps writing is stopped, the cache's watcher included (one run filling the cache stops the others); lower the floor if that bites. A count that stays partial for 3 checks is logged and audited (`run.disk_partial`) once, never texted. A fill inside a folder it can't read is missed by both; accepted, since workers are unguarded and the watchdog is there for a runaway fill, not a hostile session.
- **A live message written as the process dies can be lost.** Rare; widen `_after_exit` to keep live texts after an unclean exit if it shows up.

## Extensions

- **`gh` and `git` take arbitrary arguments** until the extension sandbox lands. Only first-party, reviewed manifests are installed.
- **Vendored packages aren't detected.** The stdlib-only check looks for dependency files only. Review catches it.
- **`gh` needs its token on disk.** The github extension writes a 0600 `hosts.yml` in a 0700 dir for the run; one ended by SIGKILL leaves it until a later run sweeps `resonant-gh-*` dirs older than an hour. A descendant started through a shell could inherit fd 4; first-party extensions don't.
- **The notify cap after a crash.** A crash between a send and its audit row leaves that send uncounted, so an hour can carry one over its 20.
- **L0 jobs are at least once within catch-up.** They're reads, and events dedupe by key.
- **A gated job's events can be lost** if the daemon crashes between recording the execution and finishing the step: the retry returns the recorded result without the events.
- **MCP tool names.** Linear's names come from public listings. If `ext add repo:linear` reports `has no tool <name>`, rename it in `extensions/linear/resonant.yaml` (the refusal names the closest tools).
- **A drifted MCP tool stays disabled** until a restart or `ext update`. There's no `/ext` re-approve from iMessage (see [iMessage `/ext`](extensions.md#imessage-ext-addupdateremove)).

## Pulse and briefing

- **Only job runs store events.** A reporter must be a job; a tool that calls `report()` is dropped silently.
- **The first job run after install is a silent baseline.** Run the job once (`/ext run <ext>/<job>`), then expect news.
- **An urgent send races the pulse.** A pulse in the window between `channel.send` and marking it told sends it again. Narrow (top of the hour); add an in-flight skip if it shows up.
- **A failed pulse job isn't rerun.** The next delivery (or `/pulse now`, or the 08:00 briefing) first finishes an unsent record under 2 hours old with unchanged items; older or changed ones are recomposed.
- **Muted urgent items ride the pulse after the mute.** They stay pending, so the first pulse after carries the open ones; a short `ttl` lets one expire.
- **Briefing Updates exclude `core` and muted sources.** If every recipient refuses an urgent send, the item stays pending for the pulse.
- **`/pulse` doesn't refresh.** A stall since the last pulse run shows only after the next one.
- **Stall clocks.** `pulse.stall_after` decides both, but a PR's activity misses some changes (a same-tree force-push), and a run's PR by the GitHub App is measured from the run's end.
- **A long outage can miss a merge.** The watch looks back two days for closed PRs and the rollup reads seven days of events.
- **The watch's event cap is 100 a run,** with `pr_active` last, so with very many open PRs some wait a run. Fine at personal scale.
- **Review requests are said once per PR** within a week, not per push.

## Relay and dashboard

- **Relay retention is fixed** at 7 days and 12000 rows, inside `relay_ingest`. Changing it is a new migration.
- **A Fly outage is silent.** The checker can't alert on its own host (a Supabase outage shows as `read_error`). An external uptime check on `/healthz` covers it ([`followups.md`](followups.md#optional)).
- **The alert channel is a public ntfy topic.** The long random topic is the password. Moving to a protected topic, an own server or a webhook needs no code ([`followups.md`](followups.md#optional)).

## Ops

- **Health is not a brain notice.** `HealthMonitor` texts on a degrade and a recovery, outside the daily cap on purpose.
- **Usage-limit backoff is per side.** The brain and the workers each learn a Claude limit on their own.
- **The MCP bridge runs the daemon's Python** (`sys.executable -I`). A venv that moves mid-session (an update) breaks that session's tools only.
