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
| 7 | Chat asks first (Stage B step 3): "please restart resonant" | `[A7] restart Resonant? Reply yes / no`; `no` → `Not done.` | 📱 ❓ |
| 8 | Approval re-mention: around **:35–:45** past an hour, send "please restart resonant" and **don't answer** | At the top of the hour: `btw:` / `✋ A7 still waiting: restart Resonant`. Then reply `no` (or let it expire at 30 minutes). Only approvals asked :30–:50 are caught (follow-up: an expiry reminder). | 📱 |
| 9 | Kill switch and pulse: `/kill`, `/pulse now`, `/resume` | `Paused: pulse not sent.`; `/resume` → `resumed: autonomy on, unpaused` | 📱 |
| 10 | Urgent path with the example extension (go-live step 13.4): `/ext run example/incident`, then `/example incident failing`, then `/example incident resolved` | `[example] 🔴 Demo service failing…` within 5 min, once; then `[example] ✓ Demo service recovered` | 📱 after 🖥 `resonant ext add repo:example` |
| 11 | Hourly "btw" with the example (step 13.5): `/ext run example/pnl` once, then wait | At a top of the hour: `btw:` / `[example] PnL …` only in hours where it moved; nothing 00:00–06:00 | 📱 after 🖥 install |
| 12 | Plain words reach the pulse: "what's the latest update" | The same text as `/pulse`, through the `updates` intent (model reply, owner only) | 📱 after deploy |
| 13 | "what's running" | Tasks answer through chat (`job_control`), no longer a keyword | 📱 after deploy |
| 14 | Before the owner steps: `/runs`, "spawn a claude to fix the typo in the README in resonant" | Both answer `The runner isn't set up: secret runner/claude_token (or runner/anthropic_key). …` and nothing about GitHub; `/runs` then lists the missing GitHub pieces as `No push or PRs yet: …` lines (go-live 14, iMessage 1) | 📱 after the employee-mode deploy |
| 14a | With only the Claude token (after 32): a scratch run, then "spawn a claude to fix the typo in the README in <repo checked out in ~/code>" | Scratch: `On it: R… · no repo (scratch dir).`, done as usual. Repo: `On it: R… · <repo>. … Heads up: no push yet (GitHub App not set up); the work stays on a local branch.`, then `R… done (…): work is on local branch resonant/r…-… in ~/code/<repo>; can't push yet: the GitHub App isn't set up.`; the branch has the commit, nothing on GitHub | 📱 after 32 |
| 14b | With only the Claude token: a private repo with no checkout in `~/code` | `I can't work on <owner>/<repo> yet: there's no checkout at …, and cloning it needs the daemon's GitHub token (runner/github_token). …`; "create a repo called x" → `Creating a repo needs the daemon's GitHub token …` | 📱 after 32 |
| 15 | Trust rules need no runner: "always ask before merging in trading", then `/rules`, then `/rules forget 1` | `Rule added: ask before merge in trading (rule 1).`; `Rules (1):` / `1. ask before merge in trading`; the rule is gone | 📱 after the employee-mode deploy |
| 16 | `/repos`, `/repo add <owner>/<name>` | The repo list (or `No repos yet.`); `Added <owner>/<name> as <name> …`, with `Heads up: main isn't protected.` if it isn't | 📱 after the employee-mode deploy |

## Needs the Mini

| # | Test | Where | Status |
|---|---|---|---|
| 17 | Sandbox self-check: `resonant sandbox check` | every deny and allow row passes, including Docker and Messages (Claude can run it; CI skips the guard e2e, which runs only on a Mac) | 🖥 (Claude) |
| 18 | GitHub token: `resonant secret set github/token`, then `check` | go-live 12, terminal 1 | 🖥 |
| 19 | `resonant ext add repo:github` (typed confirm) | go-live 12, terminal 2 | 🖥 |
| 20 | `scripts/bootstrap.sh --check` rows for the github pin, secret and jobs | go-live 12, terminal 4 | 🖥 |
| 21 | `/help github`, `/github prs`, "show my PRs" | go-live 12, iMessage 2–4 | 📱 after 19 |
| 22 | No token in logs or the DB (`grep`, `sqlite3`) | go-live 12, iMessage 5 | 🖥 |
| 23 | Watch: baseline silent, then a red and a green CI | go-live 12, iMessage 6–7 | 📱 after 19 |
| 24 | `/ext mute github 1h`, then `off` | go-live 12, iMessage 8 | 📱 after 19 |
| 25 | Next 08:00 briefing shows `Updates:` / `[github] 3 open PRs, …` | go-live 13.3 | 📱 after 19 |
| 26 | `/kill` skips jobs and kills running extension processes | go-live 12, iMessage 10 | 📱 + 🖥 (sqlite check) |
| 27 | Tamper test: edit the installed copy, then `/restart resonant`, then `resonant ext update github` | go-live 12, iMessage 11 | 🖥 |
| 28 | Crash mid-approval: `kill -9` the daemon before answering | go-live 11, Stage B 5 | 🖥 ❓ |
| 29 | Optional: github writes (`rerun_failed`, `merge_pr` with `yes D7`) | go-live 12, the writes | 📱 after a token with write access |
| 30 | Remove the example extension after tests 10–11 | `resonant ext remove example` | 🖥 |
| 31 | Reinstall the LaunchAgents for the system-only PATH: `resonant daemon install` | go-live 14, terminal 1 | 🖥 |
| 32 | Claude token: `claude setup-token` → `resonant secret set runner/claude_token` | go-live 14, terminal 2 | 🖥 |
| 33 | GitHub App: create (permissions as listed, no Administration), install on all repos, `resonant secret set runner/github_app_key --from-file <pem>`, delete the pem, tell Claude the app ID and installation ID | go-live 14, terminal 3 | 🖥 |
| 34 | Daemon token: fine-grained, all repos → `resonant secret set runner/github_token` | go-live 14, terminal 4 | 🖥 |
| 35 | Optional: classic `public_repo` token → `resonant secret set runner/github_public_token` | go-live 14, terminal 5 | 🖥 |
| 36 | Authenticated probe: `CLAUDE_CODE_OAUTH_TOKEN="$T" resonant sandbox check` (the `read -rs` form) | go-live 14, terminal 6 | 🖥 after 32 |
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

## Needs real data or effort

| # | Test | Where | Status |
|---|---|---|---|
| 47 | Router evals on 30–50 real texts (≥ 90%) | go-live 9, [`followups.md`](followups.md#router-evals-on-real-texts-go-live-step-9) | deferred |

## Done

| Test | When |
|---|---|
| Phase 2 Stage A (notes, L1 codes, reminders, dry-run ops, discovery, chat, kill switch) | 2026-10-14 |
| Stage B: `/restart ollama`, `/restart resonant` → `Back up`, `/update` | 2026-10-14 |
| `/ext list` works after the Phase 3 deploy | 2026-10-14 |
| End-to-end latency bench, real model (run by Claude on the Mini): fast p50 354 ms, llm p50 1384 ms, both under target; recorded in `architecture.md` | 2026-10-14 |
