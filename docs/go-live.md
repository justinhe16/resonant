# Mac mini go-live checklist

This takes the Mac mini from fresh to "I can text Resonant". Work through it in order. Every step has a **verify** command and the **expected** result. Don't move on until it matches.

The iMessage channel stays **off** (`channels.imessage.enabled: false`, the default) until steps 1–5 pass. Step 6 turns it on. `dry_run` stays `true` for all of Phase 1.

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
- For planned reboots, use `sudo fdesetup authrestart`. It unlocks the disk once, so the Mini comes back up logged in.

FileVault disables auto-login. After a power loss or a crash reboot, the Mini waits at the login screen. Resonant and Messages don't start until someone logs in. The dead-man switch (step 7) alerts you when that happens. That is the expected recovery path, not a bug.

- **Verify:** `fdesetup status` and `softwareupdate --schedule`
- **Expected:** `FileVault is On.` and `Automatic checking for updates is turned off` (or updates set to download-only). `--check` shows `FileVault  PASS  on`. Confirm the UPS by pulling its wall plug for a few seconds: the Mini stays on.

## 3. Resonant's Apple ID

Resonant has its own Apple ID so that it can text you, and you can text it.

- Create a dedicated Apple ID with an **email handle**, e.g. `resonant.agent@icloud.com`. Do **not** attach a phone number. Resonant uses iMessage only, never SMS.
- Sign it into **Messages.app** on the Mini. In Messages → Settings → iMessage, enable only that email address under "You can be reached for messages at".
- Set the handle in `~/.resonant/config.yaml`. It must be the lowercase email.

  ```yaml
  channels:
    imessage:
      self_handle: resonant.agent@icloud.com
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
- **Expected:** the self-test reports that it sent a message to `self_handle` and read it back from `chat.db`, and it exits 0. This also triggers the Automation prompt on the first run. Until the iMessage channel ships, `--check` prints `imessage self-test  SKIP (available after Phase 1)`. `--check` never runs the self-test because it sends a real message.

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

## 7. Dead-man switch (healthchecks.io)

- Create a check at [healthchecks.io](https://healthchecks.io). Set its period to 1 minute (the daemon pings every `health.ping_every_s`, 60s by default) and a grace time of a few minutes. Add your phone or email as an integration.
- Set the ping URL in `~/.resonant/config.yaml`, then reload with `resonant daemon install`.

  ```yaml
  health:
    healthchecks_url: https://hc-ping.com/<uuid>
  ```

- **Verify:** `scripts/bootstrap.sh --check`, then freeze the daemon for longer than the check's period plus grace:

  ```sh
  pid=$(launchctl list | awk '/com.resonant.daemon/ {print $1}')
  kill -STOP "$pid"; sleep 400; kill -CONT "$pid"   # adjust sleep to period + grace
  ```

- **Expected:** `healthchecks URL set  PASS`. On healthchecks.io the check shows recent pings. While the daemon is frozen, the check goes **down** and you get the alert. After `kill -CONT` it recovers. If the watchdog sees a stale loop first, the event log shows a `/fail` ping with the reason.

## 8. Local model

Ollama runs as a LaunchAgent, and bootstrap pulls `model.name` (default `qwen3:30b-a3b`).

- **Verify:** `resonant model probe`, then `resonant model bench`
- **Expected:** the probe reports the model is loaded with the configured context length, and exits 0. The bench prints latency and tokens/sec. **Record the numbers** (date, model, macOS version) in your notes so regressions are visible. `--check` runs the probe (required). Until it ships, `--check` prints `model probe  SKIP (available after Phase 1)`. In the meantime, `curl -s http://127.0.0.1:11434/api/tags` should list the model.

## 9. Router evals

The synthetic set in `evals/` only proves the pipeline. Gate on your own texts.

- Put real messages (anonymised as you like) in `~/.resonant/evals/router.jsonl`, in the same format as `evals/*.example.jsonl`. That directory is outside the repo and is never committed.
- **Verify:** `resonant eval router --set ~/.resonant/evals/router.jsonl`
- **Expected:** accuracy **≥ 90%**. If it is lower, look at the misroutes before going live. `--check` doesn't run evals. It prints `router eval  SKIP` with a pointer here.

## 10. Tailnet access to the dashboard

The API binds to `127.0.0.1` only (config validation enforces it). Tailscale is the only way in.

- Install Tailscale (https://tailscale.com/download/mac), log in, then:

  ```sh
  tailscale serve --bg 7777
  ```

- **Verify:** `tailscale serve status` on the Mini, then from another device on your tailnet: `curl -s https://<mini-name>.<tailnet>.ts.net/api/status`
- **Expected:** `serve status` shows `https://<mini-name>...` proxying to `http://127.0.0.1:7777`. The remote `curl` returns JSON with `"dry_run": true`. Off the tailnet the name doesn't resolve.

## 11. Keep `dry_run: true`

Phase 1 is read-only. The gate logs side effects instead of executing them. Leave `dry_run: true` until Phase 2 ships approvals.

- **Verify:** `scripts/bootstrap.sh --check` and `resonant status`
- **Expected:** `dry_run is true  PASS` and `dry_run  True`. `--check` fails if `dry_run` is off.

## Done

`scripts/bootstrap.sh --check` exits 0 with every required step `PASS`, and you have texted Resonant and got a reply.
