#!/usr/bin/env bash
set -euo pipefail

# Guardian for the tmux orchestrator: self-heals session/window drift and stale heartbeats.
# Intended to be run via launchd StartInterval (e.g. every 60s).

DEFAULT_ENV_FILE=""
if [[ -f "/Users/joshwegener/.config/clawd/orchestrator.env" ]]; then
  DEFAULT_ENV_FILE="/Users/joshwegener/.config/clawd/orchestrator.env"
elif [[ -f "${HOME:-}/.config/clawd/orchestrator.env" ]]; then
  DEFAULT_ENV_FILE="${HOME:-}/.config/clawd/orchestrator.env"
fi

ENV_FILE="${CLAWD_ORCHESTRATOR_ENV_FILE:-${CLAWD_ENV_FILE:-$DEFAULT_ENV_FILE}}"

mkdir -p /Users/joshwegener/clawd/memory
LOG_PATH="${CLAWD_ORCHESTRATOR_GUARDIAN_LOG:-/Users/joshwegener/clawd/memory/orchestrator-guardian.log}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

cd /Users/joshwegener/clawd

ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "=== guardian tick $ts ===" >>"$LOG_PATH"
python3 /Users/joshwegener/clawd/scripts/orchestrator_guardian.py >>"$LOG_PATH" 2>&1 || true
echo >>"$LOG_PATH"

