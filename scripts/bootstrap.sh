#!/usr/bin/env bash
# Take a fresh Mac to a running Resonant daemon in one command:
#
#   git clone https://github.com/justinhe16/resonant && cd resonant && scripts/bootstrap.sh
#
# Idempotent: safe to re-run. It never overwrites existing config in ~/.resonant.
# Flags:
#   --no-model   skip pulling the local model (it is large)
#   --no-start   prepare everything but don't install or start the LaunchAgents
#   --check      verify the go-live checklist (docs/go-live.md) and change nothing.
#                Prints PASS/FAIL/SKIP per step; exits 1 if a required step fails.
set -euo pipefail

PULL_MODEL=1
START=1
CHECK=0
for arg in "$@"; do
  case "$arg" in
    --no-model) PULL_MODEL=0 ;;
    --no-start) START=0 ;;
    --check) CHECK=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${RESONANT_HOME:-$HOME/.resonant}"
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[[ "$(uname -s)" == "Darwin" ]] || { echo "Resonant targets macOS." >&2; exit 1; }

# --- --check: read-only go-live verification (docs/go-live.md) ----------------------
# Installs nothing and writes nothing. It never runs uv (uv run/sync can modify .venv)
# and runs Python with -B so no bytecode is written.
NOT_YET="SKIP (available after Phase 1)"
FAILED=0
report() {  # report <status> <required:1|0> <step> [detail]
  local status="$1" required="$2" name="$3" detail="${4:-}" tag=""
  if [[ "$status" == "FAIL" ]]; then
    if [[ "$required" == "1" ]]; then FAILED=$((FAILED + 1)); else tag=" (advisory)"; fi
  fi
  printf '  %-30s %s%s%s\n' "$name" "$status" "${detail:+  $detail}" "$tag"
}
has_cmd() {  # has_cmd <subcommand...>: true when `resonant <subcommand...>` exists
  "$RESONANT" "$@" --help >/dev/null 2>&1
}

run_check() {
  RESONANT="$REPO/.venv/bin/resonant"
  local py="$REPO/.venv/bin/python"
  export RESONANT_HOME="$HOME_DIR" PYTHONDONTWRITEBYTECODE=1

  printf '\033[1mResonant go-live check\033[0m  home: %s  (see docs/go-live.md)\n\n' "$HOME_DIR"

  if [[ ! -x "$py" || ! -x "$RESONANT" ]]; then
    report FAIL 1 "python env (.venv)" "missing; run scripts/bootstrap.sh first"
    printf '\n%d required step(s) failed.\n' "$FAILED"
    return 1
  fi
  report PASS 1 "python env (.venv)" "$RESONANT"
  echo "  (Full Disk Access target: $("$py" -B -c 'import os, sys; print(os.path.realpath(sys.executable))'))"

  if command -v fdesetup >/dev/null && fdesetup status 2>/dev/null | grep -q "FileVault is On"; then
    report PASS 0 "FileVault" "on"
  else
    report FAIL 0 "FileVault" "not on (step 2)"
  fi

  # Config, principals, Full Disk Access and health: checked by Python with the daemon's
  # own loaders. Note that macOS grants FDA per app: this shell checks with Terminal's
  # grant, while the daemon needs the interpreter path above.
  local status required name detail helper_out
  if ! helper_out="$("$py" -B "$REPO/scripts/go_live_check.py" 2>&1)"; then
    helper_out+=$'\n'$'FAIL\t1\tconfig checks\tgo_live_check.py crashed (output above)'
  fi
  while IFS=$'\t' read -r status required name detail; do
    if [[ -n "${name:-}" ]]; then
      report "$status" "$required" "$name" "${detail:-}"
    else
      echo "    $status"  # stray output (e.g. a traceback line)
    fi
  done <<< "$helper_out"

  if has_cmd model probe; then
    if "$RESONANT" model probe >/dev/null 2>&1; then
      report PASS 1 "model probe" "resonant model probe ok"
    else
      report FAIL 1 "model probe" "failed; run 'resonant model probe' for details (step 8)"
    fi
  else
    report "$NOT_YET" 1 "model probe"
  fi

  # These send a real iMessage or run the whole eval set, so --check only points at them.
  if has_cmd selftest imessage; then
    report SKIP 0 "imessage self-test" "run manually: resonant selftest imessage (step 5)"
  else
    report "$NOT_YET" 0 "imessage self-test"
  fi
  if has_cmd eval router; then
    report SKIP 0 "router eval" "run manually: resonant eval router (step 9)"
  else
    report "$NOT_YET" 0 "router eval"
  fi

  if "$RESONANT" status >/dev/null 2>&1; then
    report PASS 0 "daemon up" "resonant status ok"
  else
    report FAIL 0 "daemon up" "resonant status reports down (step 6)"
  fi

  echo
  if [[ $FAILED -gt 0 ]]; then
    printf '%d required step(s) failed. See docs/go-live.md.\n' "$FAILED"
    return 1
  fi
  echo "All required steps pass."
}

if [[ $CHECK -eq 1 ]]; then
  if run_check; then exit 0; else exit 1; fi
fi

step "Homebrew packages"
if ! command -v brew >/dev/null; then
  echo "Homebrew is required: https://brew.sh" >&2
  exit 1
fi
for pkg in uv ollama gitleaks; do
  if command -v "$pkg" >/dev/null; then echo "$pkg: ok"; else brew install "$pkg"; fi
done

step "Python environment"
cd "$REPO"
uv python install 3.12
uv sync --frozen
RESONANT="$REPO/.venv/bin/resonant"
PYTHON_REAL="$(cd "$REPO" && uv run python -c 'import os, sys; print(os.path.realpath(sys.executable))')"
uv run pre-commit install >/dev/null 2>&1 || true

step "Runtime home: $HOME_DIR"
if [[ ! -d "$HOME_DIR" ]]; then
  mkdir -p "$HOME_DIR" && chmod 700 "$HOME_DIR"   # only when we create it
fi
mkdir -p "$HOME_DIR/logs" "$HOME_DIR/evals"
seed() {  # seed <example> <target>: copy an example config unless the target exists
  if [[ -e "$2" ]]; then
    echo "keeping existing $2"
  else
    install -m 600 "$1" "$2" && echo "created $2 (edit it)"
  fi
}
seed "$REPO/config/resonant.example.yaml" "$HOME_DIR/config.yaml"
seed "$REPO/config/principals.example.yaml" "$HOME_DIR/principals.yaml"
export RESONANT_HOME="$HOME_DIR"

if [[ $START -eq 1 ]]; then
  step "LaunchAgents (Ollama + daemon)"
  "$RESONANT" daemon install

  if [[ $PULL_MODEL -eq 1 ]]; then
    step "Local model"
    MODEL="$("$REPO/.venv/bin/python" -c 'from resonant.config import load_settings; print(load_settings().model.name)')"
    for _ in $(seq 1 30); do
      curl -sf http://127.0.0.1:11434/api/version >/dev/null && break
      sleep 1
    done
    ollama pull "$MODEL"
  fi

  step "Status"
  sleep 2
  "$RESONANT" status || true
fi

cat <<EOF

$(printf '\033[1m')Manual steps (macOS won't let a script do these):$(printf '\033[0m')

  1. Disk & reboots
     - Keep FileVault ON. Note: FileVault disables auto-login, so after an
       unplanned reboot someone must log in before Resonant (and Messages) start.
       The dead-man switch alerts you when that happens.
     - For planned reboots use:  sudo fdesetup authrestart
     - Put the Mini on a UPS, and turn off automatic macOS updates.

  2. Resonant's own Apple ID (iMessage channel, Phase 1)
     - Create a dedicated Apple ID with an EMAIL handle (e.g. resonant.agent@icloud.com).
       Don't attach a phone number. Resonant uses iMessage only, never SMS.
     - Sign it into Messages.app on this Mac (Settings > iMessage: enable that email only).
     - Set channels.imessage.self_handle in $HOME_DIR/config.yaml.
     - Add every handle you text from (phone number AND Apple ID email) to
       $HOME_DIR/principals.yaml, e.g. imessage:+15551234567, imessage:you@icloud.com.
       Resonant only ever messages handles listed there.

  3. Privacy permissions (System Settings > Privacy & Security)
     - Full Disk Access for the daemon's interpreter (needed to read chat.db):
         $PYTHON_REAL
       This grant covers every script that interpreter runs, and a Python upgrade
       changes the path, which silently drops the grant. Re-run this script after
       upgrading Python. 'resonant status' reports when chat.db is unreadable.
     - Automation: allow the daemon to control Messages (prompted on first send).
     - Then run the send/receive self-test (available once the iMessage channel ships):
         $RESONANT selftest imessage

  4. Secrets (only the executor reads these)
       security add-generic-password -s resonant -a <secret_name> -w
     Never put secrets in config files or .env.

  5. Network
     - Install Tailscale (https://tailscale.com/download/mac) and log in.
     - Expose the dashboard and API to your tailnet only:
         tailscale serve --bg 7777
     - Optional dead-man switch: create a check at healthchecks.io and set
       health.healthchecks_url in $HOME_DIR/config.yaml.

  6. Edit $HOME_DIR/config.yaml (dry_run stays true until Phase 2 ships), then:
       $RESONANT daemon install   # reload after config changes
       $RESONANT status

$(printf '\033[1m')Next:$(printf '\033[0m') work through docs/go-live.md step by step, then verify with
       scripts/bootstrap.sh --check
EOF
