#!/usr/bin/env bash
set -euo pipefail

# Bring up the RecallDeck Kanboard orchestrator in tmux.
# Optional: also enable the launchd LaunchAgent so it stays up across logins.

WITH_LAUNCHD="0"
if [[ "${1:-}" == "--launchd" ]]; then
  WITH_LAUNCHD="1"
fi

ENV_FILE="${CLAWD_ENV_FILE:-$HOME/.config/clawd/orchestrator.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "WARN: missing env file: $ENV_FILE" >&2
fi

/Users/joshwegener/clawd/scripts/tmux_up.sh

if [[ "$WITH_LAUNCHD" == "1" ]]; then
  PLIST="$HOME/Library/LaunchAgents/com.recalldeck.clawd.orchestrator.plist"
  if [[ -f "$PLIST" ]]; then
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl enable "gui/$(id -u)/com.recalldeck.clawd.orchestrator" 2>/dev/null || true
  else
    echo "WARN: missing launchd plist: $PLIST" >&2
  fi
fi

echo "clawd up"
