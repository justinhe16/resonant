#!/usr/bin/env bash
# Take a fresh Mac to a running Resonant daemon in one command:
#
#   git clone https://github.com/justinhe16/resonant && cd resonant && scripts/bootstrap.sh
#
# Idempotent: safe to re-run. It never overwrites existing config in ~/.resonant.
# Flags:
#   --no-model   skip pulling the local model (it is large)
#   --no-start   prepare everything but don't install or start the LaunchAgents
set -euo pipefail

PULL_MODEL=1
START=1
for arg in "$@"; do
  case "$arg" in
    --no-model) PULL_MODEL=0 ;;
    --no-start) START=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${RESONANT_HOME:-$HOME/.resonant}"
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[[ "$(uname -s)" == "Darwin" ]] || { echo "Resonant targets macOS." >&2; exit 1; }

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
mkdir -p "$HOME_DIR/logs" "$HOME_DIR/evals"
chmod 700 "$HOME_DIR"
seed() {  # seed <example> <target>: copy an example config unless the target exists
  if [[ -e "$2" ]]; then
    echo "keeping existing $2"
  else
    cp "$1" "$2" && chmod 600 "$2" && echo "created $2 (edit it)"
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
     - Create a separate Apple ID for Resonant and sign it into Messages.app on
       this Mac. Resonant texts you as a contact and can never send as you.
     - Add your handle to $HOME_DIR/principals.yaml, e.g. imessage:+15551234567

  3. Privacy permissions (System Settings > Privacy & Security)
     - Full Disk Access for the daemon's interpreter (needed to read chat.db):
         $PYTHON_REAL
       This grant covers every script that interpreter runs, and a Python upgrade
       changes the path, which silently drops the grant. Re-run this script after
       upgrading Python. 'resonant status' reports when chat.db is unreadable.
     - Automation: allow the daemon to control Messages (prompted on first send).

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
EOF
