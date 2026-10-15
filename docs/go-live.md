# Mac mini go-live checklist

This takes the Mac mini from fresh to "I can text Resonant". Work through it in order. Every step has a **verify** command and the **expected** result. Don't move on until it matches.

The iMessage channel stays **off** (`channels.imessage.enabled: false`, the default) until steps 1–5 pass. Step 6 turns it on. `dry_run` stays `true` until stage B of step 11.

Re-check the automated parts at any time. The check is read-only: it installs nothing and edits nothing.

```sh
scripts/bootstrap.sh --check
```

It prints `PASS`, `FAIL` or `SKIP` for each step and exits non-zero if a required step fails. Steps whose command hasn't shipped yet print `SKIP (available after Phase 1)`. A `FAIL ... (advisory)` line is worth fixing but doesn't fail the check.

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

FileVault disables auto-login. After a power loss or a crash reboot, the Mini waits at the login screen. Resonant and Messages don't start until someone logs in. That is the expected recovery path, not a bug. The dead-man switch (step 7) that would alert you isn't set up yet: it is planned as the status relay (see [`followups.md`](followups.md#status-relay-dead-man-switch-and-remote-dashboard)).

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

Not set up yet: planned as the status relay (see [`followups.md`](followups.md#status-relay-dead-man-switch-and-remote-dashboard)). Skip this step for now; `--check` reports `healthchecks URL set` as advisory.

## 8. Local model

Ollama runs as a LaunchAgent, and bootstrap pulls `model.name` (default `qwen3:30b-a3b-instruct-2507-q4_K_M`, ~19 GB). Use an instruct (non-thinking) build: plain `qwen3:30b-a3b` is the thinking-only Thinking-2507 build, and the probe's tool-call check fails on it.

- **Verify:** `resonant model probe`, then `resonant model bench`
- **Expected:** the probe reports the model is loaded with the configured context length, and exits 0. The bench prints latency and tokens/sec. **Record the numbers** (date, model, macOS version) in your notes so regressions are visible. `--check` runs the probe (required). Until it ships, `--check` prints `model probe  SKIP (available after Phase 1)`. In the meantime, `curl -s http://127.0.0.1:11434/api/tags` should list the model.

## 9. Router evals

*Deferred: needs real texts labelled by hand (see [`followups.md`](followups.md#router-evals-on-real-texts-go-live-step-9)).* The synthetic set in `evals/` only proves the pipeline. Gate on your own texts.

- Put real messages (anonymised as you like) in `~/.resonant/evals/router.jsonl`, in the same format as `evals/*.example.jsonl`. That directory is outside the repo and is never committed.
- **Verify:** `resonant eval router --set ~/.resonant/evals/router.jsonl`
- **Expected:** accuracy **≥ 90%**. If it is lower, look at the misroutes before going live. `--check` doesn't run evals. It prints `router eval  SKIP` with a pointer here.

### End-to-end latency (Phase 1 exit criterion)

- **Verify:** `resonant bench e2e --model real --n 20` (after step 8, with the model warm). It runs the real reader, router, local runner and channel against the configured Ollama model in a throwaway home, with a fake chat.db and a recording osascript runner, so it never texts anyone and is safe next to the live daemon.
- **Expected:** exit 0: `fast` p50 ≤ 1000 ms and `llm` p50 ≤ 3000 ms. **Record the numbers** (date, model, macOS version) in the latency table in `docs/architecture.md`, replacing "pending go-live checklist".

## 10. Remote dashboard access

Not set up yet: planned as the status relay (see [`followups.md`](followups.md#status-relay-dead-man-switch-and-remote-dashboard)). Skip this step for now. The API binds to `127.0.0.1` only (config validation enforces it), so the dashboard is reachable only on the Mini. iMessage doesn't need it.

## 11. Phase 2: approvals, then real ops

Two stages. Stage A checks every Phase 2 path with `dry_run: true`, so ops tools only say what they would do. Stage B turns `dry_run` off so `/restart` and `/update` act. Codes in the replies (`A7`, `D7`) vary: use the one you get. Times and commits are examples.

### Stage A: `dry_run: true`

Deploy Phase 2. The live checkout must be on `main`, not a detached HEAD (`/update` needs it in stage B):

```sh
cd ~/resonant && git switch main && git pull --ff-only && uv sync --frozen
resonant daemon install   # also installs the keep-awake agent (step 2)
```

`~/.resonant/logs/resonant.jsonl` shows `gate=policy dry_run=True`, and `scripts/bootstrap.sh --check` shows `store schema 5  PASS` and `dry_run  PASS  true`. Then text Resonant:

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

Stage B of step 11 is done (`dry_run: false`). Deploy Phase 3 with `/update` (or `git pull --ff-only && uv sync --frozen` and `resonant daemon install`). `~/.resonant/logs/resonant.jsonl` shows `extensions=0` on start, and `scripts/bootstrap.sh --check` shows `store schema 5  PASS`, `extensions dir  PASS  none installed yet` and `briefing scheduled  PASS  daily 08:00 …; next …`.

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
   - **Escalations** first: the secret `token`, the approver `owner`, `merge_pr` (L1), and the commands that run `gh`;
   - then the tools (`prs`, `pr`, `rerun_failed` L3, `merge_pr` L1), the jobs `watch` (every 10 minutes, L0) and `digest` (07:50, L0), notify on `ci_failed`, `ci_fixed`, `review_requested`, and the health check.

   Type `github`. It prints `Installed github repo:github@…` and `Restarting the daemon; it texts you when it's back.`, and over iMessage you get `Back up, down 4s` / `Installed github`.

3. **Optional: the example.** `resonant ext add repo:example`, type `example`. Its `morning-ping` texts `[example] ping` on weekday mornings from the second run on. Skip it if you don't want that.

4. `scripts/bootstrap.sh --check`: `extension github pin  PASS  enabled, <commit> digest <digest>, copy ok`, `secret github/token  PASS  present (value not read)`, `extension jobs scheduled  PASS  2 job(s); next github/watch …`.

### Over iMessage

1. **List.** `/ext list` → `Extensions (1):`, then `github · enabled · <commit> digest <digest> · 4 tools`, `- digest (L0) · next tomorrow 7:50am · never run`, `- watch (L0) · next …`, then `core · built in` and `- briefing (L0) · next tomorrow 8:00am · …`. Once the health check has run, the github line ends `· health ok`.
2. **Help.** `/help github` lists `/github prs`, `/github pr <repo> <number>`, `/github rerun_failed <repo> <number>` (L3) and `/github merge_pr <repo> <number> <head_sha>` (L1). `/gihub prs` → `Unknown command /gihub. Did you mean /github?`, and nothing runs.
3. **PRs, no prompt.** `/github prs` → `github prs: 3 open PRs, 1 failing CI, 0 awaiting your review`. This is also the daemon-side Keychain check: the daemon read the token with nobody at the screen. If no Keychain dialog appeared on the Mini and the reply isn't `Not done: secret github/token …`, the LaunchAgent reads it without a prompt.
4. **Chat.** "show my PRs" → an answer about your open PRs (the router labels it `extension:github`).
5. **No token anywhere.** On the Mini, `grep -c '<first 6 chars of the token>' ~/.resonant/logs/resonant.jsonl` and `sqlite3 ~/.resonant/resonant.db "SELECT COUNT(*) FROM audit_log WHERE detail LIKE '%<first 6 chars>%'"` both print `0`.
6. **Watch.** `/ext run github/watch` → `github/watch: done: 3 PRs watched, 2 events`. Nothing else arrives: the first run after install is a silent baseline, so PRs that were already red or green aren't announced.
7. **CI goes red.** Push a commit that fails CI to a scratch PR. Within about 10 minutes, once: `[github] ✗ resonant#41 CI failed (<workflow>)`. Push a fix: `[github] ✓ resonant#41 CI fixed`.
8. **Mute.** `/ext mute github 1h` → `Muted github until 3:45pm.`. Break CI again: nothing arrives, then or later. `/ext mute github off` → `Unmuted github.`.
9. **Briefing.** `/ext run core/briefing` → the briefing (one line per section with something in it, e.g. `▶ 1 running, 1 waiting (approval)`, or `Nothing on today.`), then `Briefing sent.`. The next morning it arrives by itself at 08:00, with the 07:50 github summary under `Updates:` (`[github] 3 open PRs, 1 failing CI, 2 awaiting your review`).
10. **Kill switch.** `/kill` → `kill switch engaged`. Due jobs are skipped: `sqlite3 ~/.resonant/resonant.db "SELECT at, extension, tool FROM audit_log WHERE action = 'job.skipped_paused' ORDER BY id DESC LIMIT 3"` shows the next `job_watch`. Any extension process still running is killed (`job.killed`), and no notifications or briefing are sent. `/ext run github/watch` → `Paused: nothing runs until /resume.`. `/resume` → `resumed: autonomy on, unpaused`; the skipped runs are not caught up, and the next `watch` runs on time.
11. **Tamper.** On the Mini: `chmod u+w ~/.resonant/extensions/github/README.md && echo x >> ~/.resonant/extensions/github/README.md`, then `/restart resonant`. After `Back up …` you get, once: `github changed on disk since you approved it; disabled. resonant ext update github to review`. `/ext list` shows `github · disabled_tampered · …`, and `/github prs` no longer exists. In the terminal, `resonant ext update github` shows the changed file and the full review; type `github`. `Back up …` / `Updated github`, and `/ext list` shows it `enabled` again.

### Optional: the writes

Only if you want them (see the github README). Edit the token: Actions **read and write** for `rerun_failed`; Contents and Pull requests **read and write** for `merge_pr`. The Keychain item doesn't change.

- `/github rerun_failed justinhe16/resonant 41` on a PR with a failed run: it runs without asking (L3) and replies `Done: github: rerun failed …`. With nothing to re-run it replies `Failed: github: rerun failed …` and changes nothing.
- `/github pr justinhe16/resonant 41` shows the head SHA. `/github merge_pr justinhe16/resonant 41 <sha>` → `[D7] … merge pr …? This can't be undone. Reply yes D7 / no`. `no` → `Not done.`. Only `yes D7` merges, and only if the head is still `<sha>` and checks are green.

- **Verify:** `scripts/bootstrap.sh --check`
- **Expected:** exit 0; `extension github pin`, `secret github/token`, `extension jobs scheduled` and `briefing scheduled` all `PASS`.
- **Rollback:** `resonant ext remove github`, type `github`: its tools, jobs and commands go after the restart. It keeps `~/.resonant/ext-data/github/` (`--purge` deletes it) and prints `resonant secret rm github/token` for the token it leaves. `/kill` stops everything at once in the meantime.

## 13. Phase 3.5: the pulse

Step 12 is done. Deploy with `/update` (or `git pull --ff-only && uv sync --frozen` and `resonant daemon install`). `scripts/bootstrap.sh --check` shows `store schema 5  PASS` and `pulse scheduled  PASS  hourly America/Los_Angeles; quiet 00:00-06:00; next …`. The pulse sends nothing until an installed extension with a `report:` grant reports something.

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

- Urgent items text immediately at any hour, at most 20 an hour per extension. Normal items go out in one `btw:` message at the top of each hour, never between 00:00 and 06:00 (the 06:00 pulse carries what was held), and never in the 08:00 hour (the briefing covers it). The briefing always sends, even as `Nothing on today.`.
- An item the owner has heard is not told again until its state changes (`A`, `B`, `A` is three tellings).
- An approval asked between :30 and :50 past an hour and still waiting at the top of the hour is mentioned once in that `btw:` (re-mentions need it 10 minutes old, and approvals expire after 30). Others expire before a pulse sees them.

- **Verify:** `scripts/bootstrap.sh --check`
- **Expected:** exit 0; `pulse scheduled  PASS`. The row is a `FAIL` (required) if `pulse.enabled` is false.
- **Rollback:** `pulse: {enabled: false}` in `~/.resonant/config.yaml` and `resonant daemon install` stops the hourly pulse (urgent reports and the briefing's Updates still work); `resonant ext remove <name>` removes a noisy reporter; `/ext mute <name> 8h` quiets one for now.

## 14. Phase 5a: the Claude runner

Step 13 is done. Deploy with `/update` (or `git pull --ff-only && uv sync --frozen` and `resonant daemon install`). `scripts/bootstrap.sh --check` shows `store schema 6  PASS`, `runs table  PASS`, `runner sandbox  PASS` and, until you finish the terminal steps, `runner repos  SKIP` and `FAIL` (advisory) rows for the two runner secrets. Until then everything else works, `/runs` and "fix X in Y" answer `The runner isn't set up: …`, and nothing can start a run.

What the runner is: "fix the typo in the README in demo" starts a Claude Code session in a throwaway clone, inside the macOS sandbox ([`phase5a-plan.md`](phase5a-plan.md)), and texts back a PR link. Merging is the one approval. Codes (`R1`), PR numbers and times below are examples.

### In the terminal on the Mini

1. **Claude's token.** `claude setup-token` prints a long-lived token for your subscription. Then:

   ```sh
   resonant secret set runner/claude_token    # paste it at the hidden prompt, twice
   ```

   For API billing instead, set `runner/anthropic_key` (and `runner.max_budget_usd` in config). The secret store takes values up to 128 bytes; a setup token is about 108.
2. **A GitHub token for the runner repos only.** Create a **fine-grained** personal access token, limited to the repos the runner may touch, with these repository permissions: Contents **read and write**, Pull requests **read and write**, Checks **read**, Commit statuses **read**, Metadata **read**. **No Workflows permission**: the runner is never allowed to edit `.github/`. Then:

   ```sh
   resonant secret set runner/github_token
   ```

3. **Name the repos.** Tell Claude which repos (for example "demo is github.com/you/demo"). Claude adds them to `runner.repos` in `~/.resonant/config.yaml` (it is not a secret) and restarts the daemon:

   ```yaml
   runner:
     repos:
       demo: {remote: github.com/you/demo, base: main}
   ```

4. **On GitHub, for each runner repo.** These keep a runner PR from doing harm before you merge it:
   - The repo has a workflow that runs on `pull_request`. The runner refuses a repo without one (`R1 failed: demo has no CI on pull_request …`).
   - No repo or organization secrets are available to `pull_request` runs from branches of the same repo. A runner PR can change a test, a `Makefile` or `package.json` that CI executes. Either the workflows that use secrets run only on `push` to `main`, or the secrets live in a GitHub environment that needs approval. Claude can read each repo's workflows and say which need a change.
   - Recommended: branch protection on `main` requiring CI, so `/merge` can never land red code.
5. **The authenticated probe.** The self-check proves the denies without a login; this proves a real `claude -p` authenticates inside the sandbox:

   ```sh
   read -rs T   # paste the token, then Enter; nothing is shown or saved to history
   CLAUDE_CODE_OAUTH_TOKEN="$T" resonant sandbox check; unset T
   ```

   Every row `PASS`, including `claude -p with CLAUDE_CODE_OAUTH_TOKEN`. The token is in that one command's environment only, and never in shell history: don't put it in a file or the LaunchAgent. The daemon's own environment must hold no secrets (`daemon env holds no secrets` is a row of the same check).
6. `scripts/bootstrap.sh --check`: `runner repos  PASS  1: demo`, `runner secret runner/github_token  PASS  present (value not read)`, `runner secret runner/claude_token (or runner/anthropic_key)  PASS …`. The row `runner repo demo has CI` is `SKIP` until the first run has fetched the mirror, then `PASS`. All runner rows are advisory.

### First real run (Claude can do this on the Mini)

A small ask on a repo with CI, such as "add a blank line at the end of the README", from iMessage (`/run demo …`). Check:

- **Claude's own sandbox is off, and ours is the only one.** The transcript `~/.resonant/runner/transcripts/R1.jsonl` has no `sandbox_apply` or permission errors, and the session wrote files. (A nested sandbox would fail; the run passes `--settings '{"sandbox":{"enabled":false}}'`.)
- **`--max-turns` is accepted** by the installed Claude Code (it is undocumented in `claude --help`): the run doesn't fail on an unknown flag. If it does, the flag is one entry in `runners/claude.py`.
- **The usage-limit text.** If the subscription's limit is ever hit, the run goes to `waiting` and resumes by itself. If it fails instead with the limit's text, send the text to Claude (the matcher is `usage_limit()` in `runners/claude.py`).
- **No token anywhere.** `grep -c '<first 6 chars of each token>' ~/.resonant/logs/resonant.jsonl` and the same query on `audit_log` (as in step 12) print `0`; so does the same `grep` on `~/.resonant/runner/transcripts/R1.jsonl`.
- **The sandbox held.** The session wrote only under `~/resonant-runs/R1/` (and the runner cache): `~/Library/LaunchAgents`, `~/.zshrc` and the `~/resonant` checkout are unchanged.

### Over iMessage

1. **Not set up (before the terminal steps).** `/runs` and "fix the typo in the README in demo" → `The runner isn't set up: no repos in runner.repos, secret runner/github_token, secret runner/claude_token (or runner/anthropic_key). On the Mini: …`.
2. **A run.** `/run demo fix the typo in the README` (or in plain words: "fix the typo in the README in demo") → at once `On it: R1 · demo. Up to 45m. /runs to check, /cancel R1 to stop.`. A few minutes later `R1 done: PR #41 · https://github.com/you/demo/pull/41 · 1 file · 4m` and Claude's one-line summary. A change to a file CI or your tooling executes adds `⚠ changes Makefile, package.json`.
3. **While it runs.** `/runs` → `Runs (1 active):` / `- R1 demo · running · 3m`. The next hourly `btw:` mentions `R1 still running · demo · started 14:05`, and stops once it's done.
4. **Cancel.** `/run demo add a README badge`, then `/cancel R2` → `R2 cancelled.` Nothing is pushed and no PR appears.
5. **Merge, explicit.** Once the PR's CI is green: `/merge R1` → `R1 merged: PR #41.` (plus the `⚠` line and the link). While CI is red or pending: `R1 not merged: CI isn't green`, and the run stays `ready`.
6. **Merge, in chat.** After another finished run, "merge it" → `[F6] merge R2: PR #42 in demo https://github.com/you/demo/pull/42, changes Makefile … Reply yes / no`. `no` → `Not done.`. `yes` → `R2 merged: PR #42.`
7. **Kill switch.** Start a run, then `/kill` → `kill switch engaged, 1 run ended`. On the Mini, `pgrep -fl claude` shows nothing of that run within a few seconds, and nothing was pushed. `/resume` does not restart it. `/runs` shows it `cancelled`.
8. **Refusals.** A repo without CI → `R1 failed: demo has no CI on pull_request; the runner only works in repos with CI`. A run that touches `.github/` → `R1 failed: …` naming it, with nothing pushed. A run that puts a token in the diff is refused the same way, and the token is never echoed back.
9. **Restart mid-run.** Start a run and `/restart resonant` once it's going. After `Back up …` the same run finishes (`R1 done: …`): one PR, not two.

### What to expect

- Two runs at once at most, and one per repo (a second on the same repo waits). A run gets 45 minutes of session time (`runner.max_minutes`), then it is stopped and fails with the work kept (`/runs R1`).
- The sandbox is a wall, not a prompt: starting a run asks nothing. The run can't read `~/.resonant`, the Keychain, your SSH keys or home, can't reach Resonant's API port, and can't start anything outside itself. It does have the network, so it can send the repo's code (or its own Claude token) anywhere; the GitHub token is never inside.
- Run directories (`~/resonant-runs/R1`) are deleted on merge, on `/cancel`, or 7 days after the run ends.

- **Verify:** `scripts/bootstrap.sh --check`, `resonant sandbox check`
- **Expected:** exit 0; `runner sandbox  PASS`, `runs table  PASS`, and the runner rows `PASS` once steps 1–3 are done.
- **Rollback:** empty `runner.repos` in `~/.resonant/config.yaml` and restart: no run can start (running ones finish, or `/kill` them). `resonant secret rm runner/claude_token` and `resonant secret rm runner/github_token` remove the credentials; revoke the GitHub token on github.com.

## Done

`scripts/bootstrap.sh --check` exits 0 with every required step `PASS`, and you have texted Resonant and got a reply.
