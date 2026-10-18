# Test tracker

What still needs a human to try, in one place. The full steps and expected replies are in [`go-live.md`](go-live.md). This file only tracks **status**. Deferred work (not tests) is in [`followups.md`](followups.md).

Legend:
- ✅ done.
- 📱 can be done from the phone, over iMessage, from anywhere.
- 🖥 needs the Mini's terminal.
- ❓ not confirmed yet.

Update a row when you try it, with the date and anything odd.

## Can do now, from the phone

| # | Test | Expect | Status |
|---|---|---|---|
| 1 | `/pulse` | `Nothing new since the last update.` | 📱 |
| 2 | `/pulse now` | `Nothing new to send.` | 📱 |
| 3 | `/ext list` | `core · built in` with `briefing` and `pulse`, each with a next time | 📱 |
| 4 | `/github prs` (before install) | `github isn't installed. On the Mini: resonant ext add repo:github` | 📱 |
| 5 | `/ext run core/briefing` | The briefing, or `Nothing on today.`, then `Briefing sent.` | 📱 |
| 6 | The 08:00 briefing arrives on its own | One message, every day, even if it's `Nothing on today.` | 📱 (wait for it) |
| 7 | Chat asks first (Stage B step 3): "please restart resonant" | `[A7] restart Resonant (yes always = don't ask before running restart_resonant). Reply yes / yes always / no`; `no` → `Not done.` | 📱 ❓ |
| 8 | Approval re-mention: around **:35–:45** past an hour, send "please restart resonant" and **don't answer** | At the top of the hour: `btw:` / `✋ A7 still waiting: restart Resonant`. Then reply `no` (or let it expire at 30 minutes). Only approvals asked :30–:50 are caught (follow-up: an expiry reminder). | 📱 |
| 9 | Kill switch and pulse: `/kill`, `/pulse now`, `/resume` | `Paused: pulse not sent.`; `/resume` → `resumed: autonomy on, unpaused` | 📱 |
| 10 | Urgent path with the example extension (go-live step 13.4): `/ext run example/incident`, then `/example incident failing`, then `/example incident resolved` | `[example] 🔴 Demo service failing…` within 5 min, once; then `[example] ✓ Demo service recovered` | 📱 after 🖥 `resonant ext add repo:example` |
| 11 | Hourly "btw" with the example (step 13.5): `/ext run example/pnl` once, then wait | At a top of the hour: `btw:` / `[example] PnL …` only in hours where it moved; nothing 00:00–06:00 | 📱 after 🖥 install |
| 12 | Plain words reach the pulse: "what's the latest update" | The same text as `/pulse`, through the `updates` intent (model reply, owner only) | 📱 after deploy |
| 13 | "what's running" | Tasks answer through chat (`job_control`), no longer a keyword | 📱 after deploy |
| 14 | Before the owner steps (no token and no `claude` login on the Mini): `/runs`, "spawn a claude to fix the typo in the README in resonant" | Both answer `The runner isn't set up: a Claude login (or secret runner/claude_token (or runner/anthropic_key)). …` and nothing about GitHub; `/runs` then lists the missing GitHub pieces as `No push or PRs yet: …` lines (go-live 14, iMessage 1) | 📱 after the employee-mode deploy |
| 14a | With only the Claude token (after 32): a scratch run, then "spawn a claude to fix the typo in the README in <repo checked out in ~/code>" | Scratch: `On it: R… · no repo (scratch dir).`, done as usual. Repo: `On it: R… · <repo>. … Heads up: no push yet (GitHub App not set up); the work stays on a local branch.`, then `R… done (…): work is on local branch resonant/r…-… in ~/code/<repo>; can't push yet: the GitHub App isn't set up.`; the branch has the commit, nothing on GitHub | 📱 after 32 |
| 14b | With only the Claude token: a private repo with no checkout in `~/code` | `I can't work on <owner>/<repo> yet: there's no checkout at …, and cloning it needs the daemon's GitHub token (runner/github_token). …`; "create a repo called x" → `Creating a repo needs the daemon's GitHub token …` | 📱 after 32 |
| 15 | Trust rules need no runner: "always ask before merging in trading", then `/rules`, then `/rules forget 1` | `Rule added: ask before merge in trading (rule 1).`; `Rules (1):` / `1. ask before merge in trading`; the rule is gone | 📱 after the employee-mode deploy |
| 16 | `/repos`, `/repo add <owner>/<name>` | The repo list (or `No repos yet.`); `Added <name> (<owner>/<name>, base main, push, CI): cloned to ~/code/<name>.`, with `Heads up: main isn't protected.` if it isn't | 📱 after the employee-mode deploy |
| 16i | RP-1 "add my mycelium repo" (registered before adds cloned), then "where's mycelium on the Mini?" and `/repos` | `Already have mycelium (justinhe16/mycelium, base main, push…): cloned to ~/code/mycelium.`; the answer names `~/code/mycelium`; `/repos` lines end `· ~/code/<name>` or `· not cloned`. `ls ~/code/mycelium` is a normal checkout and `git -C ~/code/mycelium remote get-url origin` is `https://github.com/justinhe16/mycelium` (no token) | 📱 + 🖥 |
| 16a | GT-1 standing allow: "please restart resonant", answer `yes always`; later "restart resonant" again; then `/rules` and `/rules forget <n>` | First: `Restarting Resonant…` then `Rule added: don't ask before running restart_resonant (rule N)`; second: restarts with no prompt; `/rules` lists it; after forget, the next ask prompts again | 📱 |
| 16b | GT-1 chained side effects: "note buy milk and remind me at 5pm to call mom" | Both report lines in one reply, no prompt | 📱 |
| 16c | GT-1 `/rules allow deploy in dori-staging` | `Rule added: don't ask before deploy in dori-staging (rule N).` | 📱 |
| 16d | GT-1 `/repo add <owner>/<public repo>` before the daemon token is set | `Added … Registered without the daemon's GitHub token, so push access and CI weren't checked; runs will find out.`; a private one says it needs `runner/github_token` | 📱 |
| 16e | GT-1 `/merge R<n>` while its CI is still running | `R<n>: CI is still running; I'll merge PR #… when it's green.`, then the merge in the digest (or `R<n> not merged: …` texted) | 📱 after a run |
| 16f | GT-1 a long note: `/note` with ~900 characters | `Saved note …` (up to 1000 characters) | 📱 |
| 16g | TR-1 during a run: "what's R<n> doing", "status on the <repo> run", `/runs R<n>` | `R<n> <repo>: running $ … · N tool calls in 10m · last event Ns ago`, then `Recent:` commands and `Said: …`; `/runs R<n>` adds `Now: …` under the run line | 📱 during a run |
| 16h | TR-1 detected side effects: a scratch run asked to `curl -X POST https://httpbin.org/post -d x=1` and a repo run asked to push a tag | The run's digest line ends `· did: POST httpbin.org (curl) (detected); pushed tag … (detected)`; no text for these (not high impact). A run that pushes to its base branch texts `R<n> detected: pushed to main` | 📱 after a run |
| 16i | RC-1 Remote Control: a scratch run ("spawn a claude to check disk usage") | After `On it: R<n> …`, one text `R<n> Remote Control: https://claude.ai/code/session_…`; the link opens the live session in the Claude app; `/runs R<n>` and "what's R<n> doing" show `Remote Control: …` while it runs; the session shows as ended in the app once the run is done (none left open) | 📱 after deploy ❓ |
| 16j | RC-1 live message: during a longer run, `tell R<n> also add a line to the README` | `→ R<n> (message)`; the worker acts on it in the same session (no new session in the app, transcript shows the message as a user turn) | 📱 after deploy ❓ |
| 16k | RC-1 with `runner/claude_token` set (token auth) | Probably `R<n>: Remote Control unavailable: …` once, and the run still finishes; `--check` row `runner remote control` FAILs with the reason (advisory) | 🖥 + 📱 optional |
| 16l | GH-2 rollup: two runs at once, merge one (`/merge R<n>` or a merge rule), leave the other running; wait for the top of the hour | One `btw:` / `Workers:` message: `Merged: R<n> <ask> (#…)` and `Still working on: R<m> <ask>`; no message in hours with nothing new (no "all quiet") | 📱 after deploy ❓ |
| 16m | GH-2 stall: a run whose PR sits unmerged (status ready) for 4h+ | Once: `Looks like R<n> <ask> (~4h quiet) got dropped. You should take a look.`; not repeated the next hours | 📱 after deploy ❓ |
| 16n | GH-2 override: "text me right away when CI fails in resonant", then break CI on one of your PRs | `Rule added: text right away on ci_failed in resonant (rule N).`; then `[github] ✗ resonant#… CI failed (…)` within ~10 min; other repos stay quiet. `/rules forget N` after | 📱 after `resonant ext update github` ❓ |
| 16o | GH-2 on demand: "any of my PRs failing CI?" | The open PRs with CI state (the `prs` tool); no texts arrived on their own for that failure | 📱 after `resonant ext update github` ❓ |

## Needs the Mini

| # | Test | Where | Status |
|---|---|---|---|
| 17 | Sandbox self-check: `resonant sandbox check` | With the default `runner.guard: off`: `guard: off …` and every row `SKIP`. With `thin` (or `--thin`): every deny and allow row passes, including Docker and Messages (Claude can run it; CI skips the guard e2e, which runs only on a Mac) | 🖥 (Claude) |
| 18 | GitHub token: `resonant secret set github/token`, then `check` | go-live 12, terminal 1 | 🖥 |
| 19 | `resonant ext add repo:github` (typed confirm) | go-live 12, terminal 2 | 🖥 |
| 20 | `scripts/bootstrap.sh --check` rows for the github pin, secret and jobs | go-live 12, terminal 4 | 🖥 |
| 21 | `/help github`, `/github prs`, "show my PRs" | go-live 12, iMessage 2–4 | 📱 after 19 |
| 22 | No token in logs or the DB (`grep`, `sqlite3`) | go-live 12, iMessage 5 | 🖥 |
| 23 | Watch: baseline silent, then a red CI that doesn't text (GH-2), a merge and a 4h-quiet PR in the hourly rollup | go-live 12, iMessage 6–7 | 📱 after 19 and `resonant ext update github` |
| 24 | `/ext mute github 1h`, then `off` | go-live 12, iMessage 8 | 📱 after 19 |
| 25 | Next 08:00 briefing shows `Updates:` / `[github] 3 open PRs, …` | go-live 13.3 | 📱 after 19 |
| 26 | `/kill` skips jobs and kills running extension processes | go-live 12, iMessage 10 | 📱 + 🖥 (sqlite check) |
| 27 | Tamper test: edit the installed copy, then `/restart resonant`, then `resonant ext update github` | go-live 12, iMessage 11 | 🖥 |
| 28 | Crash mid-approval: `kill -9` the daemon before answering | go-live 11, Stage B 5 | 🖥 ❓ |
| 29 | Optional: github writes (`rerun_failed`, `merge_pr` with `yes D7`) | go-live 12, the writes | 📱 after a token with write access |
| 30 | Remove the example extension after tests 10–11 | `resonant ext remove example` | 🖥 |
| 31 | Reinstall the LaunchAgents for the system-only PATH: `resonant daemon install` | go-live 14, terminal 1 | 🖥 |
| 32 | Claude: `claude` logged in on the Mini (`claude auth status`), enough with the guard off; optional token `claude setup-token` → `resonant secret set runner/claude_token` | go-live 14, terminal 2 | 🖥 |
| 33 | GitHub App: create (permissions as listed, no Administration), install on all repos, `resonant secret set runner/github_app_key --from-file <pem>`, delete the pem, tell Claude the app ID and installation ID | go-live 14, terminal 3 | 🖥 |
| 34 | Daemon token: fine-grained, all repos → `resonant secret set runner/github_token` | go-live 14, terminal 4 | 🖥 |
| 35 | Optional: classic `public_repo` token → `resonant secret set runner/github_public_token` | go-live 14, terminal 5 | 🖥 |
| 36 | Only with `runner.guard: thin`: authenticated probe `CLAUDE_CODE_OAUTH_TOKEN="$T" resonant sandbox check` (the `read -rs` form) | go-live 14, terminal 6 | 🖥 after 32 (skip while the guard is off) |
| 37 | `--check` runner rows `PASS` (repos and code dir may `SKIP`) | go-live 14, terminal 7 | 🖥 after 31–34 |
| 38 | Branch protection on each base you care about; "no CI edits in <repo>" where Actions secrets matter | go-live 14, on GitHub | 🖥 (Claude can check) |
| 39 | First real run: the worker's own `gh pr create` and `gh pr checks --watch` work with the App's installation token (PR by `<app>[bot]`); worktree under `~/code/.worktrees`, checkout untouched; no sandbox or permission errors in the transcript; no token in logs | go-live 14, first run | 🖥 (Claude) after 31–36 |
| 40 | Spawn in plain words: "spawn a claude to …" → `On it: R1 · <repo>.`; no done text; the next `btw:` has `Workers:` / `R1 done: PR #… (CI green) · <url>` | go-live 14, iMessage 2 | 📱 after 39 |
| 41 | A worker's question: `R1 asks: …`, a plain reply → `→ R1`, the run carries on; with two or three workers waiting at once, `R2 <answer>` and a reply by content go to the right one | go-live 14, iMessage 4 | 📱 after 39 |
| 42 | A net-new check: `[K4] R1 wants to … Reply yes / yes always / no`; `yes always` → `Rule added: don't ask before …`; the next one doesn't ask | go-live 14, iMessage 5 | 📱 after 39 |
| 43 | A rule taught over chat makes a merge ask: "always ask before merging in <area>", then "merge R<n>" on a run there asks; on another repo it merges | go-live 14, iMessage 6 | 📱 after 39 |
| 44 | A scratch run (no repo): `On it: R… · no repo (scratch dir).`, then `R… done: <summary>` in the digest; no PR | go-live 14, iMessage 7 | 📱 after 39 |
| 45 | `/cancel R1`, then `/kill` during a run → run cancelled, nothing left running; `/resume`; `/restart resonant` mid-run finishes with one PR | go-live 14, iMessage 10–11 | 📱 after 39 |
| 46 | First fork run: the fork appears, the App mints its token, `external_pr` asks, the upstream PR appears only after `yes` | go-live 14, first run | 📱 after 35 and 39 |
| 51 | Workers load your Claude setup (guard off): a run's transcript `init` line lists your MCP servers and skills; with no `runner/claude_token` set, `/runs` doesn't ask for one and a run works on your `claude` login | go-live 14, terminal 2 and first run | 🖥 (Claude) after 31–32 |
| 52 | Work on Resonant itself: "spawn a claude to fix the typo in the README in resonant" → `On it: R… · resonant.`, worktree `~/code/.worktrees/resonant/R…`, `~/resonant` untouched, a PR; "spawn a claude to restart resonant and check it came back" → `Back up (…)`, the same run resumes and finishes; without "restart" in the ask its `resonant restart` asks `[K…] R… wants to restart on resonant …` first | go-live 14, iMessage 12 | 📱 after 39 |
| 48 | Optional Linear: API key → `resonant secret set linear/token`; `resonant ext add repo:linear` asks before sending the token, lists every tool with a pin (`save_issue` and `save_comment` `L2: asks`), no `bad 'pattern' value` or `has no tool` refusal | go-live 12, Optional: Linear 1–3 | 🖥 |
| 49 | "what's on my Linear" answers with your issues (no asking); "make a Linear ticket for …" and "add a comment on KEY-123 saying …" each ask a plain yes first, `no` → `Not done.` | go-live 12, Optional: Linear 4 | 📱 after 48 |
| 50 | No Linear key in logs or the DB (`grep -r lin_api ~/.resonant/logs`, `sqlite3` on `audit_log`) | go-live 12, Optional: Linear | 🖥 after 49 |

## Brain: ask_claude and taint (BR-5)

Numbered B1–B5. Needs `brain.mode: loop` (BR-1, L1 below) to call `ask_claude` from chat.

| # | Test | Expect | Status |
|---|---|---|---|
| B1 | "look up the latest Claude Code release" (research) | `Looking into it…`, then within ~2 min one `Claude: …` reply, terse, under 1500 chars | 📱 after L1 |
| B2 | "what's on my calendar tomorrow" | Today: `Not done: Calendar isn't reachable from Claude's read-only mode (missing …)` (connectors don't load under `--restricted`, followups) | 📱 after L1 |
| B3 | A screenshot with "what's this?" | `Claude: …` about the image (attachment copied from Messages) | 📱 after BR-7 |
| B4 | `/kill` while an ask runs, then `/resume` | `Stopped: Resonant was paused (/kill).`; no `claude` process left (`pgrep -f -- --restricted`) | 📱 + 🖥 after BR-1 |
| B5 | A tainted write: after "read ~/code/x/README" ask it to "add a note …" | The note asks first (`[A7] add note … Reply yes / no`), no `yes always` | 📱 after L1 |

## Phase 4: the dashboard

Numbered D1–D6 so they don't collide with other sections' rows. Steps are [`go-live.md`](go-live.md#15-phase-4-the-remote-dashboard-and-the-dead-man-switch) step 15.

| # | Test | Expect | Status |
|---|---|---|---|
| D1 | Local dashboard: open `http://127.0.0.1:7777/` on the Mini | Overview with health, autonomy, slots and counts, "last seen" under a minute; Workers, Approvals, Pulse, Extensions and Rules & repos tabs with the local detail; no setup needed | 🖥 |
| D2 | `--check` after go-live 15.7 | `relay pushing  PASS  ok since …`, `relay secret relay/supabase_key  PASS`, `dead-man switch  PASS  relay on: …`; a row a minute in Supabase's `snapshots` | 🖥 after 15.1–15.7 |
| D3 | Remote sign-in at `https://<app>.fly.dev/` with password + TOTP | Status only (health, counts, states, times): no asks, texts, repo names or rules; a wrong TOTP code or no TOTP reads nothing | 📱 after 15.8 |
| D4 | Freeze: `kill -STOP $(pgrep -f 'resonant daemon run$')` | Remote dashboard: red `No fresh data for …` banner after ~3 min; phone: one `Resonant: no snapshots` push ~4–6 min after the freeze, and no repeats | 🖥 + 📱 after D3 and 15.9 |
| D5 | Recovery: `kill -CONT $(pgrep -f 'resonant daemon run$')` | One `Resonant: snapshots back` push within a couple of minutes; banner clears; `resonant status` shows `daemon  up` (a watchdog restart is fine) | 🖥 + 📱 after D4 |
| D6 | Paused shows from away: `/kill` over iMessage, watch the remote dashboard, then `/resume` | Within about a minute it shows paused and autonomy off (the relay keeps pushing while paused); after `/resume` it's back to on; no alert either way | 📱 after D3 |

## Phase 6a: the brain's read tools (BR-2)

Numbered F1–F4. The tools reach chat only through the brain loop (`brain.mode: loop`, L1 below).

| # | Test | Expect | Status |
|---|---|---|---|
| F1 | "where is mycelium" | A path under `~/code` (or wherever it is in the read roots), from `search` or `list_dir` | 📱 after L1 |
| F2 | "what's in ~/.ssh/config", "show me dori's .env" | Refused: the path is on the deny list; nothing from the file | 📱 after L1 |
| F3 | "what did R4 say last", "any errors in the log?" | Lines from `read_transcript` / `tail_log`, URLs and handles removed, no tokens | 📱 after L1 |
| F4 | Optional: `brew install ripgrep` (`brain.rg_path`, default `/opt/homebrew/bin/rg`); without it `search` uses the slower bounded Python walk | `/opt/homebrew/bin/rg --version` works | 🖥 |

## Brain (with `brain.mode: loop`)

| # | Test | Expect | Status |
|---|---|---|---|
| B1 | "remember that mycelium is checked out at ~/code/mycelium" | `Remembered mycelium-path: "…"` (or a similar name); `ls -l ~/.resonant/brain/facts` shows one `-rw-------` file | 📱 + 🖥 after BR-1 |
| B2 | "what do you remember?" then "forget mycelium-path" | The fact from B1; then `Forgot mycelium-path.` and the file is gone | 📱 after L1 |
| B3 | Text "where is mycelium?" with no fact saved | The repo's path from the briefing (`Repos:`), or a guess that says what it assumed | 📱 after L1 |
| B4 | Start a run, wait for it to finish without texting, then text "hey" | The reply can mention the run ended (the `since we last talked` notes) | 📱 after L1 |
| B5 | A long thread (16+ texts), then "what did I ask you earlier about X?" | Answers from the summary; `sqlite3 ~/.resonant/resonant.db "select folded, source from brain_summaries"` shows `source=model` | 📱 + 🖥 after BR-1 |

## Brain loop (BR-1)

Numbered L1–L7. `brain.mode` defaults to `intents`; L1 turns the loop on (BR-8 makes it the default once the evals pass).

| # | Test | Expect | Status |
|---|---|---|---|
| L1 | Add `brain: {mode: loop}` to `~/.resonant/config.yaml`, restart, text "hey" | A short reply; `sqlite3 ~/.resonant/resonant.db "select runner, kind from tasks order by rowid desc limit 1"` shows `brain|chat`; `ping` and `/status` still answer at once | 📱 + 🖥 |
| L2 | "what's R4 doing" while a run is open | From `run_activity` (a core tool), no labeler call | 📱 after L1 |
| L3 | "how much disk is free?" | The model loads `disk` with `load_tools` and answers; a `runner.brain.load` span with `loaded: ["disk"]` | 📱 + 🖥 after L1 |
| L4 | A question that needs a few reads ("which repo has the flaky test?") | `On it…` once if it takes over 6 s, then the answer; never more than 8 `runner.brain.round` spans for the task | 📱 after L1 |
| L5 | "remember that dori deploys on fridays"; then "read ~/code/dori/README.md and remember what it says about deploys" | The first runs (`Remembered …`); the second asks first (`[A7] … Reply yes / no`, no `yes always`) because the file read tainted the task | 📱 after L1 |
| L6 | Something the local model can't finish ("compare the last ten runs' failures and summarise the causes") | Either an answer, or `Looking into it…` and then `Claude: …` (auto-escalation) | 📱 after L1 |
| L7 | `brew services stop ollama`, text "hey", then `brew services start ollama` | `I'm having trouble thinking right now. …` (the intents fallback), no crash; after the restart replies come back | 📱 + 🖥 after L1 |

## Needs real data or effort

| # | Test | Where | Status |
|---|---|---|---|
| 47 | Router evals on 30–50 real texts (≥ 90%) | go-live 9, [`followups.md`](followups.md#router-evals-on-real-texts-go-live-step-9) | deferred |
| 53 | Task evals, real model: `resonant eval task --mode both` (BR-6). `[loop]` ≥ 85%, `unapproved 0`; record accuracy, escalation rate and p50/p95 for BR-8 | go-live 9 "Task evals", [`evals/README.md`](../evals/README.md#task-eval) | 🖥 |
| 54 | `resonant bench e2e --model real --n 20` with the `loop` path: `loop` p50 ≤ 5 s | go-live 9 | 🖥 |

## Done

| Test | When |
|---|---|
| Phase 2 Stage A (notes, L1 codes, reminders, dry-run ops, discovery, chat, kill switch) | 2026-10-14 |
| Stage B: `/restart ollama`, `/restart resonant` → `Back up`, `/update` | 2026-10-14 |
| `/ext list` works after the Phase 3 deploy | 2026-10-14 |
| End-to-end latency bench, real model (run by Claude on the Mini): fast p50 354 ms, llm p50 1384 ms, both under target; recorded in `architecture.md` | 2026-10-14 |
