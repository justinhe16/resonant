# Mac mini go-live checklist

This takes the Mac mini from fresh to "I can text Resonant". Work through it in order. Every step has a **verify** command and the **expected** result. Don't move on until it matches.

The iMessage channel stays **off** (`channels.imessage.enabled: false`, the default) until steps 1–5 pass. Step 6 turns it on. `dry_run` stays `true` until stage B of step 11.

Re-check the automated parts at any time. The check is read-only: it installs nothing and edits nothing.

```sh
scripts/bootstrap.sh --check
```

It prints `PASS`, `FAIL` or `SKIP` for each step and exits non-zero if a required step fails. A `FAIL ... (advisory)` line is worth fixing but doesn't fail the check.

Paths below assume the defaults: the repo in `~/resonant` and runtime state in `~/.resonant`. `resonant` means `~/resonant/.venv/bin/resonant` if it isn't on your `PATH`.

## 1. Run bootstrap on the Mini

```sh
git clone https://github.com/justinhe16/resonant ~/resonant && cd ~/resonant
scripts/bootstrap.sh
```

The script is idempotent. It never overwrites existing files in `~/.resonant`, and it creates config with `install -m 600`. Note the **Full Disk Access** interpreter path it prints. You need it in step 5.

- **Verify:** `scripts/bootstrap.sh --check` and `ls -l ~/.resonant`
- **Expected:** `python env (.venv)  PASS` and `config.yaml loads  PASS`. `config.yaml` and `principals.yaml` are `-rw-------`.

## 2. Disk, power and reboots

- Turn **FileVault on** (System Settings → Privacy & Security → FileVault).
- Put the Mini on a **UPS**.
- Turn **automatic macOS updates off** (System Settings → General → Software Update → Automatic Updates: off for "Install macOS updates").
- **Never sleep**, and power back on after an outage: `sudo pmset -a sleep 0 disksleep 0 standby 0 autorestart 1 womp 1`. The display can still sleep. A sleeping Mini doesn't read `chat.db`, so texts would wait until it wakes. `autorestart 1` matters most without a UPS: after a power cut the Mini boots by itself (to the FileVault login screen, see below).
- **Keep-awake LaunchAgent.** `resonant daemon install` (and bootstrap) installs `com.resonant.caffeinate`, which runs `/usr/bin/caffeinate -i -m -s` under launchd (RunAtLoad, KeepAlive) and holds the sleep assertions while you're logged in. It is on by default (`power.keep_awake: true`); a hand-installed agent with that label is replaced, not duplicated. Setting it to `false` and reinstalling removes that agent, whoever installed it. Keep the `pmset` line above as well: caffeinate only runs in your login session.
- For planned reboots, use `sudo fdesetup authrestart`. It unlocks the disk once, so the Mini comes back up logged in.

FileVault disables auto-login. After a power loss or a crash reboot, the Mini waits at the login screen. Resonant and Messages don't start until someone logs in. That is the expected recovery path, not a bug. The dead-man switch (step 7, set up in step 15) is what alerts you: the Mini's snapshots stop, and your phone gets a push within about 5 minutes.

- **Verify:** `fdesetup status`, `defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates`, `pmset -g | grep -E ' sleep|autorestart'` and `pmset -g assertions | grep caffeinate`
- **Expected:** `FileVault is On.`, `0`, `sleep 0` and `autorestart 1`, and caffeinate holding `PreventSystemSleep` / `PreventUserIdleSystemSleep`. `--check` shows `keep-awake  PASS` (advisory). If the key doesn't exist, check System Settings directly. `--check` shows `FileVault  PASS  on`. Confirm the UPS by pulling its wall plug for a few seconds: the Mini stays on.

## 3. Resonant's Apple ID

Resonant has its own Apple ID so that it can text you, and you can text it.

- Create a dedicated Apple ID with an **email handle**, e.g. a new `@icloud.com` address. Do **not** attach a phone number. Resonant uses iMessage only, never SMS.
- Sign it into **Messages.app** on the Mini. In Messages → Settings → iMessage, enable only that email address under "You can be reached for messages at".
- Set the handle in `~/.resonant/config.yaml`. It must be the lowercase email.

  ```yaml
  channels:
    imessage:
      self_handle: resonant@example.com
  ```

- **Verify:** `scripts/bootstrap.sh --check`, then from your phone send an iMessage to the handle.
- **Expected:** `imessage self_handle set  PASS  <your handle>` (the example value fails). The message on your phone turns blue ("iMessage"), not green, and it appears in Messages.app on the Mini.

## 4. Principals

Resonant only reads from, and only ever messages, handles listed in `~/.resonant/principals.yaml`. Your messages can arrive from your phone number **or** your Apple ID email, depending on which device sends them, so list both:

```yaml
principals:
  owner:
    role: owner
    identities: ["imessage:+15551234567", "imessage:you@icloud.com"]
```

Phone numbers are E.164 (`+` and the country code, no spaces). Emails are lowercase.

- **Verify:** `scripts/bootstrap.sh --check`
- **Expected:** `owner has imessage identity  PASS  2 handle(s)`. The example handles from `principals.example.yaml` fail.

## 5. Privacy permissions

Apple doesn't let a script grant these. Do them by hand in System Settings → Privacy & Security.

- **Full Disk Access** for the exact interpreter path that bootstrap prints (also shown by `--check` as `Full Disk Access target`). It looks like `~/.local/share/uv/python/cpython-3.12.*/bin/python3.12`. Click `+`, press ⌘⇧G and paste the path. The daemon reads `~/Library/Messages/chat.db` through this grant. A Python upgrade changes the path and silently drops the grant, so re-run `scripts/bootstrap.sh` after upgrading Python and re-grant the new path.
- **Automation** (the daemon controls Messages). macOS prompts the first time Resonant sends. Click **Allow**. You can review it later under Privacy & Security → Automation.

macOS grants Full Disk Access per app. `--check` reads `chat.db` as your terminal, so to get a `PASS` there, the terminal app needs Full Disk Access too. The daemon's own access is what the self-test proves.

- **Verify:** `resonant selftest imessage`
- **Expected:** the self-test reports that it sent a message to `self_handle` and read it back from `chat.db`, and it exits 0. This also triggers the Automation prompt on the first run. `--check` never runs the self-test because it sends a real message; it prints a `SKIP` pointing here. The self-test is safe while the daemon is running. Run from a terminal, it proves the terminal's Automation grant; the daemon's own grant is proven by its startup self-test.

## 6. Turn the channel on

Only after steps 1–5 pass. In `~/.resonant/config.yaml`:

```yaml
channels:
  imessage:
    enabled: true
```

Then reload the daemon and check it:

```sh
resonant daemon install   # reloads the LaunchAgents with the new config
resonant status
```

- **Verify:** `resonant status`, then text "hi" to the Resonant handle from your phone.
- **Expected:** `daemon  up (launchd: True)`, `dry_run  True`, and exit code 0. `--check` shows `daemon up  PASS` and `imessage channel enabled  PASS  enabled: True`. You get a reply on your phone.

## 7. Dead-man switch

The dead-man switch is the status relay: an app on Fly alerts your phone when the Mini's snapshots stop. It needs Supabase and Fly, so it is set up in [step 15](#15-phase-4-the-remote-dashboard-and-the-dead-man-switch); skip this step until then. `--check` reports `dead-man switch` as advisory: `PASS` once the relay is on (or the older `health.healthchecks_url` is set).

## 8. Local model

Ollama runs as a LaunchAgent, and bootstrap pulls `model.name` (default `qwen3:30b-a3b-instruct-2507-q4_K_M`, ~19 GB). Use an instruct (non-thinking) build: plain `qwen3:30b-a3b` is the thinking-only Thinking-2507 build, and the probe's tool-call check fails on it.

- **Verify:** `resonant model probe`, then `resonant model bench`
- **Expected:** the probe reports the model is loaded with the configured context length, and exits 0. The bench prints latency and tokens/sec. **Record the numbers** (date, model, macOS version) in your notes so regressions are visible. `--check` runs the probe (required). `curl -s http://127.0.0.1:11434/api/tags` should also list the model.

## 9. Router evals

*Deferred: needs real texts labelled by hand (see [`followups.md`](followups.md#router-evals-on-real-texts-go-live-step-9)).* The synthetic set in `evals/` only proves the pipeline. Gate on your own texts.

- Put real messages (anonymised as you like) in `~/.resonant/evals/router.jsonl`, in the same format as `evals/*.example.jsonl`. That directory is outside the repo and is never committed.
- **Verify:** `resonant eval router --set ~/.resonant/evals/router.jsonl`
- **Expected:** accuracy **≥ 90%**. If it is lower, look at the misroutes before going live. `--check` doesn't run evals. It prints `router eval  SKIP` with a pointer here.

### End-to-end latency (Phase 1 exit criterion)

- **Verify:** `resonant bench e2e --model real --n 20` (after step 8, with the model warm). It runs the real reader, router, local runner and channel against the configured Ollama model in a throwaway home, with a fake chat.db and a recording osascript runner, so it never texts anyone and is safe next to the live daemon.
- **Expected:** exit 0: `fast` p50 ≤ 1000 ms, `llm` p50 ≤ 3000 ms and `loop` p50 ≤ 5000 ms (the brain loop, `brain.mode: loop`, in its own throwaway home: one read, then the answer). **Record the numbers** (date, model, macOS version) in the latency table in `docs/architecture.md`, replacing "pending go-live checklist".

### Task evals (the brain loop's bar, BR-6)

`brain.mode: loop` is the default since BR-8: on the repo's example set the live loop scored **91.0%** (`--repeat 3`, bar ≥ 85%, no unapproved write). Run the task eval on your own texts before you rely on it, and again after a prompt or model change. To go back to the labeler, set `brain: {mode: intents}` in `~/.resonant/config.yaml` and restart. Each text runs through the real router, brain loop, gate and channel in a fresh temp home with its own seeded world (repos, runs, files, notes, extensions): it never reads your state or `~/code`, never texts anyone, starts no Claude session (`ask_claude` is only recorded) and runs no side effect beyond the temp home's notes, reminders and facts. It is safe next to the live daemon, but it shares Ollama's one slot, so run it when you aren't texting Resonant.

- **Verify:** `resonant eval task --mode both` (after step 8, model warm; about 10–20 minutes for both modes). Without `--set` it uses `~/.resonant/evals/task.jsonl` when you have one, else the repo's `evals/task.example.jsonl`. `--only <id or category>` reruns a slice.
- **Expected:** `[loop]` accuracy ≥ 85% with `unapproved 0` and no timeouts (exit 0), and the loop ahead of `[intents]` on `assistant`, `research`, `followup` and `mini`. Escalations are counted apart (rate, precision, recall), so a loop that hands everything to Claude doesn't pass on `assistant` alone. Read the failed cases (text, tools, reply) printed under each mode. The summary goes to `~/.resonant/evals/results/task-<ts>.json` (no texts); compare runs there after a prompt or model change. Record the loop's accuracy and p50/p95 latency in `docs/brain-plan.md` §6. If `[loop]` is under 85% on your own set, switch to `brain: {mode: intents}` until it isn't, and note which rows miss.
- **Cold prompt.** The loop's first turn after anything else used the model (a restart, a fold, the pulse, an eval run) prefills about 5.2k tokens: about 4.3 s on the Mini before the first token, against about 0.05 s warm. Texting "hey" once after a restart warms it.
- **Routing (BR-10).** The report adds `route` (how many turns went to Claude, by hand-off), accuracy per route, `kept wrong` (turns the local model kept and got wrong: they should have gone to Claude), triage accuracy on rows with an expected route, and the ack's p50/p95. A live run never starts Claude: a handed-over turn is graded on routing, writes and forbidden tools only (`delegated`). `--route local` measures the loop alone (as before BR-10), `--route claude` checks every turn hands over cleanly. Record the BR-10 row in `docs/brain-plan.md` §6.

### Triage and Claude with Resonant's tools (BR-10)

Since BR-10 the local model's first call on each chat turn decides who answers it (`brain.route: triage`, the default). Easy turns (a status, a note or reminder, one obvious lookup, small talk) stay local: ~1–5 s, free. Everything else goes to Claude: an ack goes out first (~1 s warm, ~4–5 s on a cold prompt), then Claude's answer in ~5–20 s, longer when it uses tools. Claude now has Resonant's tools (runs, PRs, notes, reminders, logs, extensions…) through the same gate, approvals and trust rules as the local model; its own tools stay read-only, and it never sends email or changes calendar events.

- **Knobs** (`~/.resonant/config.yaml`, then `resonant restart`): `brain.route` (`triage` | `local` | `claude`), `brain.triage.bias` (`normal`, or `claude` to keep only status, notes, reminders and small talk local), `brain.triage.timeout_s` (8: past it the turn goes to Claude with a canned ack), `brain.ask_claude.resonant_tools` (true).
- **Usage.** Every handed-over turn is one message to Claude on your Claude subscription. Since BR-11 there is no daily cap by default (`brain.ask_claude.daily_cap: 0`; set a number to have one) and a runaway guard of 60 messages an hour (`brain.claude_session.max_per_hour`). Past either, you get one text saying so and the local model answers until midnight or the next hour. When Claude hits a usage limit you get one text (`Hit the Claude usage limit; answering locally until it resets at …`), the local model answers meanwhile, and Claude comes back on its own after the reset.
- **How to go back.** `brain: {route: local}` restores the BR-9 behavior (Claude only when the loop asks or gets stuck). `brain: {ask_claude: {resonant_tools: false}}` keeps triage but makes Claude read-only again, as in BR-5. Both take effect after `resonant restart`.
- **Over iMessage** (the phone test list for BR-10; check `/approvals` and the replies):
  1. "what's R4 doing" or "remind me in 10 minutes to stretch": answered locally, no ack, a few seconds.
  2. "why did the last resonant PR change the router?": an ack (a short lowercase line, no question) within a few seconds, then `Claude: …` naming the PR and what changed.
  3. A link ("summarise https://…"): an ack, then Claude's summary.
  4. "compare what R3 and R4 did and add a note with the differences": an ack, then Claude's answer with `Added note …` as a report line (or, if it read a page or a file first, an approval prompt after the answer: `yes` adds it once, `no` replies `Not done.`).
  5. With `brain: {route: claude}` for the test, "merge R3" (a run with a ready PR; L2 asks): Claude's answer says it's waiting on your yes, the approval prompt follows it (no code in Claude's text), `no` replies `Not done.` (`yes` merges once). Set `route` back after.
  6. A follow-up after (2): "and who merged it?": the local model or Claude answers using Claude's earlier answer.
  7. `/kill`, then a turn triage would hand over: no Claude session starts (asks are refused while paused) and nothing changes; `/resume` after.
  8. From a member's phone (if you have one): no ack and no Claude; the local loop answers as before.

### The Claude brain: one long-lived session (BR-11)

Since BR-11 the hard texts above all go to **one** Claude session that stays up (the "Claude brain"): it remembers the conversation itself, so a follow-up doesn't have to restate things, and it skips a cold start per text. It still has only the read-only built-ins plus Resonant's tools through the gate; nothing about approvals changes. It starts on the first hard text after a restart (resuming the same conversation when it can), restarts itself after a crash or a hang (primed with a short summary from Resonant's own records), and starts a fresh one after 50 turns or 12 hours. One text at a time: a second hard text while one is in flight gets an ack like `on it, checking (after "why is R4 slow", then this)`, and past 3 waiting you're told it's busy.

- **Knobs** (`~/.resonant/config.yaml`, then `resonant restart`): `brain.claude_session: {enabled: true, remote_control: false, rotate_turns: 50, rotate_hours: 12, turn_timeout_s: 240, queue_max: 3, max_per_hour: 60}`. `enabled: false` goes back to a separate `claude -p` per hard text (BR-10). `remote_control: true` asks the session for a Remote Control link when it starts: `/status` and the dashboard's Overview show it (`Claude brain: up · N turns · Remote Control: https://claude.ai/code/…`), so you can open the same conversation in the Claude app.
- **Over iMessage** (BR-11 phone tests, after the BR-10 list):
  1. "why is R4 slow?", then "and how do I fix it?": two acks and two `Claude: …` answers; the second clearly knows the first. `/status` shows `Claude brain: up · 2 turns`.
  2. Two hard texts back to back: the second ack says `(after "…", then this)`; the answers come in order.
  3. `resonant restart`, then a follow-up: the answer still knows the earlier question (the session resumed).
  4. `/kill`: `/status` shows `Claude brain: down`; `/resume`, then a hard text: it starts again.
  5. Optional, with `remote_control: true`: the link on `/status` opens the session in the Claude app.

## 10. Remote dashboard access

On the Mini, the dashboard is at `http://127.0.0.1:7777/` and needs no setup. The API binds to `127.0.0.1` only (config validation enforces it), so from anywhere else you use the remote copy, which the status relay feeds: set it up in [step 15](#15-phase-4-the-remote-dashboard-and-the-dead-man-switch). iMessage doesn't need it.

## 11. Phase 2: approvals, then real ops

Two stages. Stage A checks every Phase 2 path with `dry_run: true`, so ops tools only say what they would do. Stage B turns `dry_run` off so `/restart` and `/update` act. Codes in the replies (`A7`, `D7`) vary: use the one you get. Times and commits are examples.

### Stage A: `dry_run: true`

Deploy Phase 2. The live checkout must be on `main`, not a detached HEAD (`/update` needs it in stage B):

```sh
cd ~/resonant && git switch main && git pull --ff-only && uv sync --frozen
resonant daemon install   # also installs the keep-awake agent (step 2)
```

`~/.resonant/logs/resonant.jsonl` shows `gate=policy dry_run=True`, and `scripts/bootstrap.sh --check` shows `store schema <n>  PASS` (the latest migration; 11 today) and `dry_run  PASS  true`. Then text Resonant:

1. **Notes (no prompt).** `/note milk` → `Saved note 1: "milk"`. `/notes` → `Notes (1):` / `1. milk`. `/unnote 1` → `Deleted note 1.`. `/notes` → `No notes.`
2. **L1 needs the code.** `/note a`, then `/notes clear` → `[D7] delete all 1 note? This can't be undone. Reply yes D7 / no`. A plain `yes` → `Reply yes D7 to confirm.` and nothing is deleted. `yes D7` → `Cleared 1 note.`. Repeat with `no` → `Not done.`
3. **Reminders.** `/remind in 2 min test` → `⏰ Reminder 1 set for 3:02pm: "test"`, and `⏰ test` arrives on time. `/remind in 5 min x`, then `resonant daemon install` (a restart): it still fires, once. `/remind in 1 h y`, `/reminders` lists it, `/unremind 3` → `Cancelled reminder 3.` and it never fires. `/remind 3 x` → `say 3pm or 15:00. Usage: /remind <when> <text>`.
4. **Dry-run ops and reads.** `/restart ollama` → `Dry run: would restart Ollama`. `/restart resonant` → `Dry run: would restart Resonant`. `/update` → `Dry run: would update Resonant`. `/disk` and `/logs 5` answer.
5. **Discovery.** `/projects` lists recent work by state (`/note` items show as `command · /note`, never the text). `/help` lists every command, `/help rem` only the reminder ones, and `/remnd 3pm x` → `Unknown command /remnd. Did you mean /remind?` with nothing run.
6. **Chat phrasing.** "remind me in 10 minutes to stretch" → `⏰ Reminder N set for …: "stretch"`. "what can you do?" answers from the command list. "clear my notes" → `[D7] delete all … notes? …`; reply `no` → `Not done.`. "restart ollama" → `Dry run: would restart Ollama`.
7. **Kill switch.** `/kill` → `kill switch engaged`. `/note y` → `[A7] add note text="y"? Reply yes / no` (explicit commands ask now). Send `/kill` again: it is a command, not an answer, and the note stays pending. `yes` → `Saved note …: "y"`. `/resume` → `resumed: autonomy on, unpaused`, and `/note z` runs with no prompt again.

- **Verify:** `resonant approvals list` and `scripts/bootstrap.sh --check`
- **Expected:** `no pending approvals`; `autonomy on  PASS` and `approvals not stale  PASS`.

### Stage B: `dry_run: false`

Set both in `~/.resonant/config.yaml`. `ops.uv_path` is the absolute path from `which uv`, since launchd's `PATH` has no Homebrew:

```yaml
dry_run: false
ops:
  uv_path: /opt/homebrew/bin/uv
```

Then `resonant daemon install` and `resonant status` (`dry_run  False`). `--check` shows `dry_run  PASS  false (Phase 2 gate installed)`, `ops.uv_path set  PASS` (required now) and `repo on main and clean  PASS`. Then:

1. **Restart Ollama.** `/restart ollama` → `Restarted Ollama.`; `resonant model probe` still passes.
2. **Restart Resonant.** `/restart resonant` → `Restarting Resonant…`, then a few seconds later `Back up (a1b2c3d), down 6s`.
3. **Chat asks first.** "please restart resonant" → `[A7] restart Resonant? Reply yes / no`. `no` → `Not done.`. Ask again and reply `yes` → `Restarting Resonant…`, then `Back up …`. "please restart ollama" (L3) just runs → `Restarted Ollama.`
4. **Update.** On a clean `main`, `/update` → `Already up to date (a1b2c3d).`, or `Updated a1b2c3d → e4f5a6b, restarting…` then `Back up …`. Then `touch ~/resonant/x` and `/update` → `Failed: update Resonant (working tree has changes; not updating)`; `rm ~/resonant/x`. On a detached HEAD or another branch it refuses too (`not on main`).
5. **Crash mid-approval.** "please restart resonant", then before replying `kill -9 $(pgrep -f 'resonant daemon run')`. launchd restarts the daemon; reply `yes` → it restarts exactly once and says `Back up …`.
6. **Clean up.** `resonant approvals list` → `no pending approvals`.

- **Verify:** `scripts/bootstrap.sh --check` and `resonant status`
- **Expected:** exit 0, `dry_run  False`.
- **Rollback:** `/kill` (everything above L0 asks), or set `dry_run: true` and run `resonant daemon install`.

## 12. Phase 3: extensions, jobs and the briefing

Stage B of step 11 is done (`dry_run: false`). Deploy Phase 3 with `/update` (or `git pull --ff-only && uv sync --frozen` and `resonant daemon install`). `~/.resonant/logs/resonant.jsonl` shows `extensions=0` on start, and `scripts/bootstrap.sh --check` shows `store schema <n>  PASS` (the latest migration; 11 today), `extensions dir  PASS  none installed yet` and `briefing scheduled  PASS  daily 08:00 …; next …`.

The shipped manifests list the approver `owner`, so the owner in `~/.resonant/principals.yaml` must be named `owner`, as in step 4. Otherwise `ext add` refuses with `approver 'owner' is not a known principal`.

Codes (`A7`, `D7`), times, pins, PR numbers and counts below are examples.

### In the terminal on the Mini

1. **The GitHub token.** Create a fine-grained personal access token limited to the repos you want watched, with repository permissions Metadata, Contents, Pull requests, Checks and Actions, all **read** (see [`extensions/github/README.md`](../extensions/github/README.md#token)). Then:

   ```sh
   resonant secret set github/token     # paste it at the hidden prompt, twice
   resonant secret check github/token   # github/token: ok (no prompt)
   resonant secret list ~/resonant/extensions/github   # github/token ... present
   ```

   `set` says `stored github/token`. No Keychain dialog may appear for `check`. If one does, the item's access list doesn't trust `/usr/bin/security`: `resonant secret rm github/token` and set it again from this terminal.

2. **Install github.** `resonant ext add repo:github`. Check the screen:
   - the heading `github repo:github@<commit> digest <digest>`;
   - **Escalations** first: the secret `token`, the approver `owner`, `merge_pr` (L1), and `its code runs gh (network)` (the manifest's `bins: [gh]`: `gh` is pinned by its real path, so it must be the Homebrew one at `/opt/homebrew/bin/gh`; a `gh` a worker could replace is refused);
   - then the tools (`prs`, `pr`, `rerun_failed` L3, `merge_pr` L1), the jobs `watch` (every 10 minutes, L0) and `digest` (07:50, L0), no notify events (GH-2: nothing texts on its own; the hourly rollup reads the watch), and the health check.

   Type `github`. (If a later `brew upgrade gh` removes the pinned keg, the extension isn't loaded and `bootstrap.sh --check` shows `argv0 missing`; `resonant ext update github` pins the new one.) It prints `Installed github repo:github@…` and `Restarting the daemon; it texts you when it's back.`, and over iMessage you get `Back up, down 4s` / `Installed github`.

3. **Optional: the example.** `resonant ext add repo:example`, type `example`. Its `morning-ping` texts `[example] ping` on weekday mornings from the second run on. Skip it if you don't want that.

4. `scripts/bootstrap.sh --check`: `extension github pin  PASS  enabled, <commit> digest <digest>, copy ok`, `secret github/token  PASS  present (value not read)`, `extension jobs scheduled  PASS  2 job(s); next github/watch …`.

### Over iMessage

1. **List.** `/ext list` → `Extensions (1):`, then `github · enabled · <commit> digest <digest> · 4 tools`, `- digest (L0) · next tomorrow 7:50am · never run`, `- watch (L0) · next …`, then `core · built in` and `- briefing (L0) · next tomorrow 8:00am · …`. Once the health check has run, the github line ends `· health ok`.
2. **Help.** `/help github` lists `/github prs`, `/github pr <repo> <number>`, `/github rerun_failed <repo> <number>` (L3) and `/github merge_pr <repo> <number> <head_sha>` (L1). `/gihub prs` → `Unknown command /gihub. Did you mean /github?`, and nothing runs.
3. **PRs, no prompt.** `/github prs` → `github prs: 3 open PRs, 1 failing CI, 0 awaiting your review`. This is also the daemon-side Keychain check: the daemon read the token with nobody at the screen. If no Keychain dialog appeared on the Mini and the reply isn't `Not done: secret github/token …`, the LaunchAgent reads it without a prompt.
4. **Chat.** "show my PRs" → an answer about your open PRs (the router labels it `extension:github`).
5. **No token anywhere.** On the Mini, `grep -c '<first 6 chars of the token>' ~/.resonant/logs/resonant.jsonl` and `sqlite3 ~/.resonant/resonant.db "SELECT COUNT(*) FROM audit_log WHERE detail LIKE '%<first 6 chars>%'"` both print `0`.
6. **Watch.** `/ext run github/watch` → `github/watch: done: 3 PRs watched, 5 events` (CI events plus one `pr_active` per open PR). Nothing arrives: the watch never texts on its own (GH-2), and the first run after install is a baseline, so PRs that were already quiet or merged aren't announced by the rollup either.
7. **CI goes red, quietly.** Push a commit that fails CI to a scratch PR. No text. "any of my PRs failing CI?" → the scratch PR with its failing check. Merge another PR of yours: the next top of the hour, `btw:` / `Workers:` / `Merged: resonant#40 <title>`. Leave a PR untouched for 4 hours: once, `Looks like resonant#41 <title> (~4h quiet) got dropped. You should take a look.`
8. **A text for one repo, then mute.** "text me right away when CI fails in resonant" → `Rule added: text right away on ci_failed in resonant (rule N).`. Break CI again: within about 10 minutes, once, `[github] ✗ resonant#41 CI failed (<workflow>)`. `/ext mute github 1h` → `Muted github until 3:45pm.`; break it again: nothing arrives, then or later. `/ext mute github off` → `Unmuted github.`, and `/rules forget N`.
9. **Briefing.** `/ext run core/briefing` → the briefing (one line per section with something in it, e.g. `▶ 1 running, 1 waiting (approval)`, or one short line such as `Morning. Nothing for today.`), then `Briefing sent.`. The next morning it arrives by itself at 08:00, with the 07:50 github summary under `Updates:` (`[github] 3 open PRs, 1 failing CI, 2 awaiting your review`).
10. **Kill switch.** `/kill` → `kill switch engaged`. Due jobs are skipped: `sqlite3 ~/.resonant/resonant.db "SELECT at, extension, tool FROM audit_log WHERE action = 'job.skipped_paused' ORDER BY id DESC LIMIT 3"` shows the next `job_watch`. Any extension process still running is killed (`job.killed`), and no notifications or briefing are sent. `/ext run github/watch` → `Paused: nothing runs until /resume.`. `/resume` → `resumed: autonomy on, unpaused`; the skipped runs are not caught up, and the next `watch` runs on time.
11. **Tamper.** On the Mini: `chmod u+w ~/.resonant/extensions/github/README.md && echo x >> ~/.resonant/extensions/github/README.md`, then `/restart resonant`. After `Back up …` you get, once: `github changed on disk since you approved it; disabled. resonant ext update github to review`. `/ext list` shows `github · disabled_tampered · …`, and `/github prs` no longer exists. In the terminal, `resonant ext update github` shows the changed file and the full review; type `github`. `Back up …` / `Updated github`, and `/ext list` shows it `enabled` again.

### Optional: the writes

Only if you want them (see the github README). Edit the token: Actions **read and write** for `rerun_failed`; Contents and Pull requests **read and write** for `merge_pr`. The Keychain item doesn't change.

- `/github rerun_failed justinhe16/resonant 41` on a PR with a failed run: it runs without asking (L3) and replies `Done: github: rerun failed …`. With nothing to re-run it replies `Failed: github: rerun failed …` and changes nothing.
- `/github pr justinhe16/resonant 41` shows the head SHA. `/github merge_pr justinhe16/resonant 41 <sha>` → `[D7] … merge pr …? This can't be undone. Reply yes D7 / no`. `no` → `Not done.`. Only `yes D7` merges, and only if the head is still `<sha>` and checks are green.

### Optional: Linear (an MCP server over HTTP)

[`extensions/linear`](../extensions/linear/README.md) wires Linear's hosted MCP server (`https://mcp.linear.app/mcp`) with a personal API key as its bearer token. Reads are L0; `save_issue` and `save_comment` are L2 (a plain yes first).

1. In Linear's settings, create a **personal API key** (Read only, or Read + Write for the two writes).
2. `resonant secret set linear/token` (paste it at the hidden prompt), then `resonant secret check linear/token` → `linear/token: ok`.
3. `resonant ext add repo:linear`. It first asks `MCP server linear at https://mcp.linear.app/mcp needs linear/token to list its tools.` / `Send linear/token to mcp.linear.app now? [y/N]`; answer `y`. The screen shows `! mcp server "linear" calls "https://mcp.linear.app/mcp" (http, network), sends secret token as its bearer token`, one `! mcp tool … pin <12 hex>` line per tool (reads `read L0`, the writes `write L2: asks`), and a note listing the server tools it never exposes. Type `linear`. If it says `mcp server linear has no tool <name> (renamed? it has <names>)`, Linear renamed a tool: see the README. (Before LN-2 it also refused with `mcp tool save_issue: its schema has a bad 'pattern' value` or `… value under not or oneOf`; long or ECMA-only patterns are now dropped, rewriting `oneOf`/`not` over them so the check is only looser, so neither line should appear. If an `mcp tool …: its schema has a bad … value` line still does, note the tool and keyword for a ticket.)
4. Over iMessage: "what's on my Linear" → your issues (labeled `extension:linear`, no asking). "make a Linear ticket for <x>" → `[A…] … save issue …? Reply yes / no`; `no` → `Not done.` "add a comment on <KEY-123> saying <x>" → `… save comment …? Reply yes / no`.
5. `/kill` while a Linear call runs ends it (`maybe happened` for a write); `/resume` brings it back.

- **Verify:** `scripts/bootstrap.sh --check`
- **Expected:** exit 0; `extension github pin`, `secret github/token`, `extension jobs scheduled` and `briefing scheduled` all `PASS` (and `extension linear pin`, `secret linear/token` if you installed it).
- **Rollback:** `resonant ext remove github`, type `github`: its tools, jobs and commands go after the restart. It keeps `~/.resonant/ext-data/github/` (`--purge` deletes it) and prints `resonant secret rm github/token` for the token it leaves. `/kill` stops everything at once in the meantime.

## 13. Phase 3.5: the pulse

Step 12 is done. Deploy with `/update` (or `git pull --ff-only && uv sync --frozen` and `resonant daemon install`). `scripts/bootstrap.sh --check` shows `store schema <n>  PASS` (the latest migration; 11 today) and `pulse scheduled  PASS  hourly America/Los_Angeles; quiet 00:00-06:00; next …`. The pulse sends nothing until an installed extension with a `report:` grant reports something.

Times, codes and figures below are examples.

### Over iMessage

1. **Status.** `/pulse` → `Nothing new since the last update.` (plus `Open: …` per source once items exist). `/pulse now` → `Nothing new to send.`
2. **List.** `/ext list` shows `- pulse (L0) · next …` under `core · built in`, next to `briefing`.
3. **The github summary.** With github installed (step 12), the 07:50 `digest` reports a low item, so nothing is texted for it. The next 08:00 briefing shows it under `Updates:` (`[github] 3 open PRs, 1 failing CI, 2 awaiting your review`). A hard-coded line is no longer in the briefing.
4. **Try the urgent path (the example).** On the Mini: `resonant ext add repo:example`, type `example`. Then over iMessage:
   - `/ext run example/incident` once. This is the job's silent baseline, so nothing arrives.
   - `/example incident failing` → within 5 minutes, once, at any hour: `[example] 🔴 Demo service failing: checkout 5xx`. A second `/ext run example/incident` says nothing more (same state, told once).
   - `/example incident resolved` → within 5 minutes: `[example] ✓ Demo service recovered`.
   - `/example incident resolved` again → `no incident open`, and nothing is texted.
5. **Try the hourly "btw".** The example's fake `pnl` reports at `:55`, and a job's first run after install is a silent baseline, so run `/ext run example/pnl` once first. After that, at the top of an hour in which the rounded figure moved (it follows the hour), one message: `btw:` / `[example] PnL -$1.8k today`. Or `/pulse` to preview it and `/pulse now` to send it. Nothing is sent in an hour where nothing changed.
6. **Kill switch.** `/kill` pauses the scheduler, so no job runs and no report arrives while paused. With a `pnl` item pending, `/pulse now` → `Paused: pulse not sent.` Then `/resume`: the next pulse carries the pending `pnl` change, once. An urgent report is not held in a queue while paused: set `/example incident failing` after `/resume` and the `incident` job texts it within 5 minutes.
7. **Mute.** `/ext mute example 1h`: example items are skipped by the pulse and the briefing. `/pulse` shows `example 1 (muted until …)` under `Open:`.
8. **Clean up.** On the Mini: `resonant ext remove example`, type `example`.

### What to expect

- Urgent items text immediately at any hour, at most 20 an hour per extension. Normal items go out in one `btw:` message at the top of each hour, never between 00:00 and 06:00 (the 06:00 pulse carries what was held), and never in the 08:00 hour (the briefing covers it). The briefing always sends, even as one short line (`Morning. Nothing for today.`).
- An item the owner has heard is not told again until its state changes (`A`, `B`, `A` is three tellings).
- An approval asked between :30 and :50 past an hour and still waiting at the top of the hour is mentioned once in that `btw:` (re-mentions need it 10 minutes old, and approvals expire after 30). Others expire before a pulse sees them.

- **Verify:** `scripts/bootstrap.sh --check`
- **Expected:** exit 0; `pulse scheduled  PASS`. The row is a `FAIL` (required) if `pulse.enabled` is false.
- **Rollback:** `pulse: {enabled: false}` in `~/.resonant/config.yaml` and `resonant daemon install` stops the hourly pulse (urgent reports and the briefing's Updates still work); `resonant ext remove <name>` removes a noisy reporter; `/ext mute <name> 8h` quiets one for now.

## 14. Employee mode: Claude workers

Step 13 is done. Deploy with `/update` (or `git pull --ff-only && uv sync --frozen`), then do terminal step 1 below before anything else. Workers need a Claude credential: with the default `runner.guard: off` your own `claude` login on the Mini is enough (step 2); until one works, everything else keeps working, while `/runs` and "spawn a claude to …" answer `The runner isn't set up: a Claude login (or secret runner/claude_token …)` and nothing can start a run.

**You can start with just Claude** (steps 1 and 2). Workers run as soon as it is set: a scratch run works fully, and a repo run works in a worktree of your checkout and commits to its local branch, but can't push or open a PR yet. The start reply says so up front (`On it: R1 · dori. … Heads up: no push yet (GitHub App not set up); the work stays on a local branch.`) and the end reply names the branch (`R1 done (4m): work is on local branch resonant/r1-… in ~/code/dori; can't push yet: the GitHub App isn't set up.`). Steps 3 and 4 unlock the rest, in any order: the GitHub App lets workers push and open PRs (it needs step 4 too, since Resonant finds and checks the PR with its own token), and Resonant's own token unlocks cloning private repos (until then a private repo needs a checkout already in `~/code/<name>`; a public one is cloned anonymously), naming repos it hasn't seen, merging, creating repos, adding CI and fork runs. Each of those says what it needs when asked, and `/runs` lists what's still missing.

What you get ([`employee-plan.md`](employee-plan.md)): you text Resonant like an employee. "spawn a claude to build out the search page in dori" gets `On it: R4 · dori.`, and a Claude Code worker starts in a git worktree of your real checkout, `~/code/<repo>`, with your `.env` files, Docker and the network. An ask with no repo ("do a manual db update in dori-prod …") gets an empty scratch dir. Workers act on standing trust. They ask you only for something net new (a new system, a new kind of action, anything irreversible, anything that costs money), and they ask questions when they need you (`R4 asks: …`). Good news (a PR, done, merged) goes into the hourly digest; failures, questions and checks text you right away. **By default there is no sandbox** (`runner.guard: off`, GD-1): a worker runs like you at the keyboard of this dedicated Mini, with your real `HOME`, so it loads your `~/.claude` (skills such as `/robot-surf`, your `CLAUDE.md`, settings and MCP servers) and, with no token set, your `claude` login. It follows the trust rules because it is told to; nothing stops it reading the Keychain or `~/.resonant`. `runner.guard: thin` brings back the **thin guard** (a macOS sandbox that only protects Resonant itself: workers can't write `~/.resonant` or the installed daemon, read the Keychain or Messages, signal the daemon, add auto-start entries or reach the API port), at the cost of your Claude setup and of working on Resonant itself. Codes (`R1`, `K4`), PR numbers and times below are examples.

### In the terminal on the Mini

1. **Reinstall the LaunchAgents.** The daemon's PATH is now system-only, so it never runs a tool a worker could replace:

   ```sh
   resonant daemon install
   ```

   `resonant status` shows `daemon  up`. `scripts/bootstrap.sh --check` shows `store schema 12  PASS` and `runs table  PASS`.
2. **Claude Code, `gh` and a Claude login.** Claude Code must be installed (`claude --version`), and so must `gh` at `/opt/homebrew/bin/gh` (`runner.gh_path`; `brew install gh` if it isn't). With the default `runner.guard: off`, **your own login is enough**: run `claude` once in the Mini's terminal and log in (`claude auth status` says you are). Resonant checks it with `claude auth status` (cached for 10 minutes) and asks for a token only when no login works. Optional, and required with `runner.guard: thin`: a token. `claude setup-token` prints a long-lived token for your subscription, then:

   ```sh
   resonant secret set runner/claude_token    # paste it at the hidden prompt, twice
   ```

   A set token wins over your login. For API billing instead, set `runner/anthropic_key` (and `runner.max_budget_usd` in config); it is used only when no token is set, and it is the only time `ANTHROPIC_API_KEY` reaches a worker.
3. **The GitHub App** (workers' per-run tokens: one repo, one hour, PRs as `<app>[bot]`; [`repos-plan.md`](repos-plan.md) D10, kept by the employee plan). Without it workers still run and commit locally; this is what lets them push and open PRs:
   1. On github.com: Settings → Developer settings → GitHub Apps → New GitHub App. Any name and homepage URL; untick **Webhook → Active**.
   2. Repository permissions:
      - **Read and write:** Contents, Pull requests, Issues, Workflows, Actions.
      - **Read-only:** Checks, Commit statuses, Metadata.
      - **No Administration** (leave it at "No access"), and no organization or account permissions.
   3. Create it, then **Install App** on your account, for **All repositories** (this also covers forks made later).
   4. On the app's page, **Generate a private key**. It downloads a `.pem`. Store it and delete the file:

      ```sh
      resonant secret set runner/github_app_key --from-file ~/Downloads/<app>.<date>.private-key.pem
      rm ~/Downloads/<app>.<date>.private-key.pem
      ```

   5. Tell Claude the **App ID** (on the app's General page) and the **installation ID** (the number at the end of `github.com/settings/installations/<id>`). They aren't secret. Claude adds them to `~/.resonant/config.yaml` and restarts the daemon:

      ```yaml
      runner:
        github_app: {app_id: 123456, installation_id: 78901234}
      ```

4. **Resonant's own GitHub token** (`runner/github_token`: daemon-only, never in a worker; it reads repos and CI, keeps the mirrors, checks and merges PRs, creates repos and adds starter CI). Without it workers still run on repos you have checked out (or public ones); a push needs it as well as the App. A **fine-grained** personal access token, resource owner your account, **All repositories**:
   - **Read and write:** Contents, Pull requests, Administration (to create repos), Workflows (starter CI).
   - **Read-only:** Checks, Commit statuses, Metadata.

   ```sh
   resonant secret set runner/github_token
   ```

5. **Optional: the upstream-PR token** (`runner/github_public_token`, fork runs only: PRs to other people's public repos). A **classic** token with only the `public_repo` scope. Without it, fork runs are refused with a sentence naming it; nothing else needs it.

   ```sh
   resonant secret set runner/github_public_token
   ```

6. **Only with `runner.guard: thin`: the authenticated sandbox probe.** With the default `off`, `resonant sandbox check` prints `guard: off …` and skips every probe (`--thin` probes the profile anyway, before you switch). Under `thin` it proves the guard's denies and that a real `claude -p` authenticates inside it:

   ```sh
   read -rs T   # paste the Claude token, then Enter; nothing is shown or saved to history
   CLAUDE_CODE_OAUTH_TOKEN="$T" resonant sandbox check; unset T
   ```

   No row `FAIL`, and `claude -p with CLAUDE_CODE_OAUTH_TOKEN` and `daemon env holds no secrets` `PASS` (a `SKIP` whose control can't run here, such as no running Docker socket, is fine). The token is in that one command's environment only: don't put it in a file, a command line or the LaunchAgent. Run `resonant sandbox check` again after every macOS update.
7. `scripts/bootstrap.sh --check`, all advisory:
   - `runs table  PASS  schema v11` (or later);
   - `runner sandbox`: `SKIP  runner.guard is off: …` (the default), or `PASS` under `thin`;
   - `runner guard  PASS  off: workers run like you at the keyboard …` (or `thin: …`);
   - `runner repo resonant  PASS  /Users/<you>/resonant (registered at start)`: the daemon's own checkout (`ops.repo_path`) is the repo `resonant`, no config needed;
   - `runner github app  PASS  app 123456, installation 78901234`;
   - `runner secret runner/claude_token (or runner/anthropic_key)`: `PASS  present (value not read)`, or with the guard off and no token `PASS  not set: your own claude login works (guard off)`;
   - `runner secret runner/github_token` and `runner secret runner/github_app_key`: `PASS  present (value not read)`. Until steps 3–4 are done, their rows (and `runner github app`) say `FAIL  … workers still run; needed to push and open PRs: …`: advisory, not a blocker;
   - `runner secret runner/github_public_token (fork runs)`: `PASS`, or `SKIP` if you skipped step 5;
   - `runner code dir`: `SKIP` until adding a repo (or its first run) creates `~/code` by cloning the repo there, then `PASS`;
   - `runner repos`: `SKIP` is fine. Repos don't need listing: they are registered the first time you name one (or `/repo add`). An entry in `runner.repos` is only a per-repo override (`path` for a checkout outside `~/code`, `allow_github_changes: false`, `base`);
   - four `SKIP  owner step` rows: the first-run checks below and branch protection;
   - for each `runner.repos` entry, `runner repo <name> has CI`: `SKIP  no mirror yet` until its first run, then `PASS` (a `FAIL` means Resonant won't merge its runs until it has CI: `/repo ci <name>`).

### On GitHub, per repo you care about

- **Branch protection** (or a ruleset) on the base branch, requiring a pull request, with the app **not** in the bypass list. Without it a worker could push to `main` directly. Resonant warns at start (`main isn't protected — the session could push to it directly.`) and fails a run that did it, but the protection is what stops it.
- Require CI in that protection, so a merge can't land red code. Resonant merges only on green CI anyway, and a repo with no `pull_request` workflow can't be merged by Resonant (ask "add CI to <repo>").
- **Actions secrets.** A worker's token can push a workflow that runs on `pull_request` with the repo's secrets before anything is flagged. For a repo whose Actions secrets matter, tell Claude "no CI edits in <repo>": it adds `allow_github_changes: false` for it under `runner.repos`, so the token loses Workflows and `.github/` changes are refused at merge.

### First real run (Claude can do this on the Mini)

A small ask on a repo with CI, from iMessage: `/run <repo> add a blank line at the end of the README`. Check:

- **The worker drove its PR with the App's token.** The PR exists, is opened by `<app>[bot]`, and the transcript (`~/.resonant/runner/transcripts/R1.jsonl`) shows `gh pr create` and `gh pr checks --watch` succeeding. Those two are untested against the real `gh` with an installation token; if either fails, send the error to Claude.
- **The worktree and the checkout.** `~/code/<repo>` was cloned (or your existing checkout was used), the run worked in `~/code/.worktrees/<repo>/R1` on branch `resonant/r1-…`, and your checkout's own branch and files are unchanged. A checkout `.env` was linked into the worktree and not committed.
- **Your Claude setup loaded.** With the guard off the session runs with your `HOME`: the transcript's `init` line lists your MCP servers and skills, and there are no permission errors (`--permission-mode bypassPermissions`); `--max-turns` was accepted. (Under `thin` it has project settings only and passes `--settings '{"sandbox":{"enabled":false}}'`; no `sandbox_apply` errors.)
- **The usage-limit text.** If the subscription's limit is hit, the run goes to `waiting` and resumes by itself. If it fails instead with the limit's text, send the text to Claude (`usage_limit()` in `runners/claude.py`).
- **No token anywhere.** `grep -c '<first 6 chars of each token>' ~/.resonant/logs/resonant.jsonl ~/.resonant/runner/transcripts/R1.jsonl` and the `audit_log` query from step 12 all print `0`.
- **First fork run** (only with step 5): `/run github.com/<someone>/<public repo> fix <something small>`. The fork appears under your account, the App mints a token for it (the session pushes its branch there), Resonant asks `[K4] R1 wants to external_pr on <someone>/<repo>: …`, and only after `yes` does the upstream PR appear.

### Over iMessage

1. **Not set up (before the terminal steps).** `/runs` → `The runner isn't set up: secret runner/claude_token (or runner/anthropic_key). On the Mini: resonant secret set runner/claude_token.`, then one line per GitHub piece still missing (`No push or PRs yet: the GitHub App isn't set up, so workers commit to a local branch. On the Mini: …`). With only the Claude token set, the first line goes and the rest stay: "spawn a claude to fix the typo in the README in <repo>" (a repo checked out in `~/code`) → `On it: R1 · <repo>. … Heads up: no push yet (GitHub App not set up); the work stays on a local branch.`, and later `R1 done (…): work is on local branch resonant/r1-… in ~/code/<repo>; can't push yet: the GitHub App isn't set up.` A private repo with no checkout → `I can't work on <owner>/<repo> yet: there's no checkout at …, and cloning it needs the daemon's GitHub token (runner/github_token). …`.
2. **Spawn a worker in plain words.** "spawn a claude to fix the typo in the README in <repo>" → `On it: R1 · <repo>. Up to 45m. /runs to check, /cancel R1 to stop.` Nothing else is texted when it opens its PR: the next hourly `btw:` has `Workers:` / `R1 done: PR #41 (CI green) · <url>`. `/pulse now` sends it at once.
3. **`/run` texts the end.** `/run <repo> fix the typo in the README` → the same `On it`, then `R1 done: PR #41 (CI green) · <url> · 1 file · 4m` with `/merge R1 to merge it.` and Claude's summary.
4. **A question.** Ask for something with a real choice ("spawn a claude to add search to <repo>, ask me which backend"). You get `R1 asks: …`. Reply in plain words; you get `→ R1` and the worker carries on. With two workers waiting, `R2 <answer>` picks one, a reply about one question's content goes to it (`→ R2 (<question>)`), and an unclear one gets `For R1 (…) or R2 (…)?`. "tell R1 to also update the README" → `→ R1 (message)`.
5. **A net-new check.** A worker about to do something new asks `[K4] R1 wants to deploy on <system>: "…" (new system; yes always = don't ask before deploy in <system>). Reply yes / yes always / no`. `yes always` → `Rule added: don't ask before deploy in <system> (rule 1)`, and the next such deploy doesn't ask.
6. **Teach a rule.** "always ask before merging in trading" → `Rule added: ask before merge in trading (rule 2).` `/rules` lists the rules; `/rules forget 2` drops one. Then "merge R3" on a trading run asks `[F6] merge R3: PR #… Reply yes / no`, while "merge R1" on another repo just merges (`R1 merged: PR #41.`).
7. **A scratch run.** "spawn a claude to check what's using the disk on the Mini" (or anything with no repo) → `On it: R5 · no repo (scratch dir). …`, then in the digest `R5 done: <Claude's summary>`. No PR and no GitHub token.
8. **Several at once.** Start three runs; `/runs` lists them (up to 4 run at once; a waiting one frees its slot). Each one's questions and replies stay with it.
9. **Repos.** `/repos` lists what Resonant knows; `/repo add <owner>/<name>` registers one (`Heads up: main isn't protected.` if it isn't).
10. **Cancel and the kill switch.** `/cancel R1` → `R1 cancelled.` `/kill` during a run → `kill switch engaged, 1 run ended`; `pgrep -fl claude` on the Mini shows nothing of that run within seconds. `/resume` doesn't restart it.
11. **Restart mid-run.** `/restart resonant` while a run works: after `Back up …` the same run finishes, with one PR, not two.
12. **Work on Resonant itself.** "spawn a claude to fix the typo in the README in resonant" → `On it: R1 · resonant.`; it works in `~/code/.worktrees/resonant/R1` (never in `~/resonant`, the live daemon) and opens a PR. "spawn a claude to restart resonant and check it came back" → the worker runs `resonant restart` without asking (your ask said "restart resonant"; "restart it" or "redeploy it" work too), you get `Back up (…)`, and the run resumes and finishes. Without that in the ask the worker's `resonant restart` asks you first: `[K4] R1 wants to restart on resonant: … Reply yes / yes always / no`; `yes always` makes the next one go through. `resonant update` (pull `main`, `uv sync`, restart, as `/update`) works the same way; it deploys only what is merged.

### What to expect

- A run gets 45 minutes of session time (`runner.max_minutes`). Time waiting for your answer doesn't count. An unanswered question times out after 4 hours (`runner.ask_timeout_minutes`), and the worker uses its judgment or stops. The hourly digest repeats unanswered questions.
- Worktrees and run dirs (`~/resonant-runs/R1`) are deleted on merge, on `/cancel`, or 7 days after the run ends. Your checkouts in `~/code` are never deleted, and your own branches are never moved.
- Workers are trusted like you at the keyboard: they can read your `.env` files and the disk, use the network and Docker, change your checkouts' `.git`, and with the guard off (the default) also reach Resonant's own state and the Keychain. They follow the trust rules and ask before anything net new because they are told to; under `thin` the guard also protects Resonant.
- A worker restarts or deploys Resonant only with `resonant restart`/`update`, never `launchctl` by hand (its prompt says so): that way you get `Back up`, and its run resumes without spending one of its two crash attempts.

- **Verify:** `scripts/bootstrap.sh --check` and `resonant sandbox check`.
- **Expected:** exit 0; `runner sandbox  PASS`, `runs table  PASS`, `runner github app  PASS` and the runner secret rows `PASS` once steps 2–4 are done.
- **Rollback:** `/kill` stops every run at once. To put the sandbox back, set `runner: {guard: thin}` in `~/.resonant/config.yaml` and restart (then a token is required, and workers can't work on Resonant's own repo). `resonant secret rm runner/claude_token` keeps new runs from starting; `resonant secret rm runner/github_app_key` keeps new runs from pushing (they commit locally). Revoke `runner/github_token` on github.com and uninstall the App (Settings → Applications) to cut GitHub access.

## 15. Phase 4: the remote dashboard and the dead-man switch

**The local dashboard needs nothing.** On the Mini, open `http://127.0.0.1:7777/`: the daemon serves it with the full local view (runs, asks, approvals, pulse, rules, repos). It is read-only; every action stays on iMessage. This step makes a status-only copy reachable from any browser and adds the alert for when the Mini goes quiet ([`dashboard-plan.md`](dashboard-plan.md)).

How it fits together: every minute the daemon pushes the `remote` tier of the snapshot (health, counts, states and times; no message text, asks, repo names or URLs) to a Supabase project. Nothing connects into the Mini. A small app on Fly serves the dashboard against Supabase (you sign in with password and TOTP) and runs the dead-man checker: no snapshot for 5 minutes, or a health row `failing`, sends a push to your phone through ntfy, and another when it recovers. A power cut, a crash loop, a reboot waiting at the FileVault login or a frozen process all stop the snapshots, so this replaces the old `health.healthchecks_url` ping (which still works if you set it, but isn't needed).

Three credentials, and where each lives:

| What | Where | Who sees it |
|---|---|---|
| The **anon (publishable) key** and the project URL | `relay:` in `~/.resonant/config.yaml` and Fly secrets | public by design: the deployed dashboard ships it, and on its own it can read and write nothing |
| The **ingest token** (the daemon's write credential) | Keychain `relay/supabase_key` on the Mini | only its SHA-256 goes to Supabase |
| The **status token** (the checker's read credential) | Fly secret `RELAY_STATUS_TOKEN` | only its SHA-256 goes to Supabase |

Never use the `service_role` key or the JWT secret anywhere outside Supabase. Each token is made in a shell variable and piped to where it lives, so it never lands in shell history, a command line or on screen. The hashes the commands print aren't secret: paste them where step 6 says.

### Supabase (any browser)

1. **Create the project** at supabase.com (any name and region; the free plan is enough). Then, under **Authentication**:
   - **Sign In / Providers:** turn **Allow new users to sign up** off. Leave Email on.
   - **Multi-Factor:** **TOTP** enabled (the default).
   - **Users → Add user → Create new user:** your email and a strong password, **Auto Confirm User** ticked. This is the only user there will ever be.
   - Optional, if your plan has it: **Sessions** → a time-box or inactivity timeout (plan D5's short sessions).

   From **Project Settings → API Keys**, note the **project URL** (`https://<ref>.supabase.co`) and the **publishable key** (`sb_publishable_…`; the legacy `anon` JWT also works). Not secrets.
2. **Apply the migration.** **SQL Editor → New query**, paste all of [`supabase/migrations/20261016000000_relay.sql`](../supabase/migrations/20261016000000_relay.sql), **Run**. (Or with the Supabase CLI from the repo: `supabase link --project-ref <ref> && supabase db push`.) **Table Editor** now shows `snapshots`, empty.
3. **Enroll TOTP**, from a machine with Node and a checkout of the repo (your laptop is fine; the Mini doesn't need Node):

   ```sh
   cd dashboard && npm ci && cd ..
   node supabase/enroll_mfa.mjs --url https://<ref>.supabase.co --anon-key <publishable key> --email <you>
   ```

   It asks the password (hidden), shows a QR code (or the `otpauth://` URI), and asks a code from your authenticator app. It prints `Owner user id, for relay_private.config.owner_id: <uuid>`: keep it for step 6, then clear the terminal's scrollback (it showed the TOTP secret). The dashboard itself can't enroll a factor, on purpose: a stolen password can't add its own.

### The Mini's terminal

4. **The ingest token.** One block, pasted as is:

   ```sh
   T=$(openssl rand -hex 32)
   printf %s "$T" | shasum -a 256 | cut -d' ' -f1     # the ingest hash: keep it for step 6
   printf '%s\n' "$T" | resonant secret set relay/supabase_key
   unset T
   ```

   `resonant secret check relay/supabase_key` → `relay/supabase_key: ok (no prompt)` (the value is never shown).
5. **The Fly app, the status token and the alert topic.** Install the CLI and log in (`fly auth signup` the first time):

   ```sh
   brew install flyctl
   fly auth login
   ```

   Pick an app name (global on Fly; it becomes `https://<name>.fly.dev`), then from the repo root:

   ```sh
   cd ~/resonant
   APP=resonant-relay-<something>
   fly apps create "$APP"
   S=$(openssl rand -hex 32)
   printf %s "$S" | shasum -a 256 | cut -d' ' -f1     # the status hash: keep it for step 6
   TOPIC=resonant-$(openssl rand -hex 16)
   echo "ntfy topic: $TOPIC"                         # type it into the phone in step 9
   {
     echo "SUPABASE_URL=https://<ref>.supabase.co"
     echo "SUPABASE_ANON_KEY=<publishable key>"
     echo "RELAY_STATUS_TOKEN=$S"
     echo "NTFY_TOPIC=$TOPIC"
   } | fly secrets import --app "$APP" --stage
   unset S TOPIC
   fly secrets list --app "$APP"                     # names and digests only
   ```

   `fly apps create` (rather than `fly launch`) leaves `relay/fly.toml` untouched, so the Mini's checkout stays clean for `/update`; every later command names the app with `--app`, so in a new terminal set `APP=…` again first. The ntfy topic is the only thing between your alerts and anyone else: anyone who knows it can read and post to it, so keep it long and random and don't reuse it. Optional extras on the same pipe: `WEBHOOK_URL=https://…` (a JSON POST per alert, alongside or instead of ntfy), `NTFY_URL` and `NTFY_TOKEN` for your own or a protected ntfy server.

### Back in the SQL editor

6. **Tie it together** (paste the user id from step 3 and the two hashes from steps 4 and 5):

   ```sql
   update relay_private.config set
     owner_id            = '<user id>',
     ingest_token_sha256 = decode('<ingest hash>', 'hex'),
     status_token_sha256 = decode('<status hash>', 'hex');
   select owner_id, octet_length(ingest_token_sha256), octet_length(status_token_sha256)
     from relay_private.config;
   ```

   Expected: your user id, `32`, `32`. To rotate a token later, make a new one the same way (Keychain or `fly secrets import --app "$APP"`) and update its hash here; until both sides match, that side fails (the `relay` health row goes `failing`, or Fly sends a `read_error` alert).

### Turn the relay on

7. **Tell Claude the project URL and the publishable key** (not secret). Claude adds them to `~/.resonant/config.yaml` and restarts the daemon:

   ```yaml
   relay:
     enabled: true
     supabase_url: https://<ref>.supabase.co
     anon_key: sb_publishable_…
   ```

   Within a minute, `scripts/bootstrap.sh --check` shows `status relay enabled  PASS`, `relay secret relay/supabase_key  PASS  present (value not read)`, `relay pushing  PASS  ok since …` and `dead-man switch  PASS  relay on: …`, and **Table Editor → snapshots** gains a row a minute (it keeps 7 days). `relay pushing  FAIL  failing: HTTP 403: credentials rejected …` means the ingest hash doesn't match the Keychain item (step 4 or 6); `HTTP 404: relay_ingest not found …`, the migration isn't applied.
8. **Deploy the relay**, still from `~/resonant` (the build context is the repo root, because the image copies `dashboard/dist`):

   ```sh
   fly deploy --app "$APP" --config relay/fly.toml --dockerfile relay/Dockerfile --ha=false .
   fly scale count 1 --app "$APP" --yes
   fly status --app "$APP"                           # one machine, started, check passing
   ```

   Exactly one machine: the checker keeps its alert state in memory, so two would alert twice. `fly logs --app "$APP"` shows each failed check and every alert sent, never a secret.

### Your phone and the browser

9. **Alerts.** Install the **ntfy** app (App Store or Play Store), **Subscribe to topic**, enter the topic from step 5 (server `ntfy.sh`, the default), and allow notifications. Then clear the Mini's terminal scrollback (**Cmd-K** in Terminal; `clear` alone keeps it).
10. **The remote dashboard.** Open `https://<app>.fly.dev/` on your phone or any browser, sign in with the email, password and a TOTP code. You see health, autonomy, paused and dry-run, slots and counts, with "last seen" under a minute. There are no asks, message texts, repo names or rules: those stay on the Mini's local dashboard. Nobody else can sign in (sign-ups are off), and a signed-in session without TOTP reads nothing.
11. **The freeze test** (with Claude watching, if you like). It freezes the whole daemon, watchdog included, which is exactly what the dead-man switch is for:

    ```sh
    pgrep -f 'resonant daemon run$'                  # exactly one pid: the daemon
    kill -STOP $(pgrep -f 'resonant daemon run$')
    ```

    Expected: after about 3 minutes the remote dashboard shows the red **No fresh data for …** banner ("The Mini has stopped sending snapshots"), and about 4–6 minutes after the freeze (5 minutes from the last snapshot, plus up to one check period) your phone gets **Resonant: no snapshots** / `Snapshots stopped: none for …`. Exactly one alert, however long you wait. Then:

    ```sh
    kill -CONT $(pgrep -f 'resonant daemon run$')
    ```

    Expected: within a couple of minutes **Resonant: snapshots back**, and the banner clears. The daemon's watchdog may notice the frozen minutes and exit on resume; launchd restarts it, and `resonant status` shows `daemon  up`. Either way, one recovery alert.

- **Verify:** `scripts/bootstrap.sh --check`, `fly status --app "$APP"`, and the freeze test above.
- **Expected:** the relay rows and `dead-man switch` `PASS` (all advisory); one Fly machine; one alert on the freeze and one on recovery; the remote dashboard behind password + TOTP.
- **Rollback:** stop the checker first, or it alerts that snapshots stopped: `fly scale count 0 --app "$APP" --yes` (or `fly apps destroy "$APP"`). Then tell Claude to set `relay.enabled: false`, and `resonant secret rm relay/supabase_key`. Setting `ingest_token_sha256` to `null` (step 6) refuses every push at once. The local dashboard keeps working throughout.

## Done

`scripts/bootstrap.sh --check` exits 0 with every required step `PASS`, and you have texted Resonant and got a reply.
