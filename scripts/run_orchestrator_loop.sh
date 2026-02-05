#!/usr/bin/env bash
set -euo pipefail

# Run the board orchestrator on an interval. Intended to be run in tmux/launchd.
#
# Secrets are expected to come from a local env file, not committed to git.
# Default env file: ~/.config/clawd/orchestrator.env

DEFAULT_ENV_FILE=""
if [[ -f "/Users/joshwegener/.config/clawd/orchestrator.env" ]]; then
  DEFAULT_ENV_FILE="/Users/joshwegener/.config/clawd/orchestrator.env"
elif [[ -f "${HOME:-}/.config/clawd/orchestrator.env" ]]; then
  DEFAULT_ENV_FILE="${HOME:-}/.config/clawd/orchestrator.env"
fi

# Prefer a stable env file path over $HOME because tmux/launchd may not propagate HOME.
ENV_FILE="${CLAWD_ORCHESTRATOR_ENV_FILE:-${CLAWD_ENV_FILE:-$DEFAULT_ENV_FILE}}"
TICK_SECONDS="${CLAWD_TICK_SECONDS:-20}"
HEARTBEAT_PATH="${CLAWD_ORCHESTRATOR_HEARTBEAT_PATH:-/Users/joshwegener/clawd/memory/orchestrator-heartbeat.json}"

detect_version() {
  if [[ -n "${CLAWD_VERSION:-}" ]]; then
    echo "$CLAWD_VERSION"
    return 0
  fi
  git -C /Users/joshwegener/clawd rev-parse --short HEAD 2>/dev/null || echo "unknown"
}

write_heartbeat() {
  local phase="${1:-tick}"
  local ts_iso
  local ts_epoch
  ts_iso="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  ts_epoch="$(date -u '+%s')"
  mkdir -p "$(dirname "$HEARTBEAT_PATH")"
  local tmp
  tmp="$(mktemp "${HEARTBEAT_PATH}.tmp.XXXXXX")"
  cat >"$tmp" <<EOF
{"ts":"$ts_iso","tsEpochS":$ts_epoch,"pid":$$,"version":"$VERSION","tickSeconds":$TICK_SECONDS,"phase":"$phase"}
EOF
  mv -f "$tmp" "$HEARTBEAT_PATH"
}

if [[ -f "$ENV_FILE" ]]; then
  echo "Using env file: $ENV_FILE" | tee -a "${CLAWD_ORCHESTRATOR_LOG:-/Users/joshwegener/clawd/memory/orchestrator.log}"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
else
  echo "WARN: env file not found (KANBOARD_* may be missing): ${ENV_FILE:-<empty>}" | tee -a "${CLAWD_ORCHESTRATOR_LOG:-/Users/joshwegener/clawd/memory/orchestrator.log}"
fi

cd /Users/joshwegener/clawd

mkdir -p /Users/joshwegener/clawd/memory
LOG_PATH="${CLAWD_ORCHESTRATOR_LOG:-/Users/joshwegener/clawd/memory/orchestrator.log}"
VERSION="$(detect_version)"

while true; do
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "=== orchestrator tick $ts ===" | tee -a "$LOG_PATH"
  write_heartbeat "tick-start" || true
  # Avoid running the whole loop body inside a pipeline subshell; keeps ticks from duplicating.
  python3 /Users/joshwegener/clawd/scripts/board_orchestrator.py 2>&1 | tee -a "$LOG_PATH" || true
  write_heartbeat "tick-end" || true
  echo | tee -a "$LOG_PATH"

  sleep "$TICK_SECONDS"
done
