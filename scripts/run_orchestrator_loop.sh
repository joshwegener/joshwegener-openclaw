#!/usr/bin/env bash
set -euo pipefail

# Run the board orchestrator on an interval. Intended to be run in tmux/launchd.
#
# Secrets are expected to come from a local env file, not committed to git.
# Default env file: ~/.config/clawd/orchestrator.env

ENV_FILE="${CLAWD_ENV_FILE:-$HOME/.config/clawd/orchestrator.env}"
TICK_SECONDS="${CLAWD_TICK_SECONDS:-20}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

cd /Users/joshwegener/clawd

mkdir -p /Users/joshwegener/clawd/memory
LOG_PATH="${CLAWD_ORCHESTRATOR_LOG:-/Users/joshwegener/clawd/memory/orchestrator.log}"

while true; do
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  {
    echo "=== orchestrator tick $ts ==="
    python3 /Users/joshwegener/clawd/scripts/board_orchestrator.py || true
    echo
  } 2>&1 | tee -a "$LOG_PATH"

  sleep "$TICK_SECONDS"
done
