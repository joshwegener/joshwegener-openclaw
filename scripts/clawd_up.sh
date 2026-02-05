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
  ORCH_PLIST="$HOME/Library/LaunchAgents/com.recalldeck.clawd.orchestrator.plist"
  if [[ -f "$ORCH_PLIST" ]]; then
    launchctl bootstrap "gui/$(id -u)" "$ORCH_PLIST" 2>/dev/null || true
    launchctl enable "gui/$(id -u)/com.recalldeck.clawd.orchestrator" 2>/dev/null || true
  else
    echo "WARN: missing launchd plist: $ORCH_PLIST" >&2
  fi

  GUARD_LABEL="com.recalldeck.clawd.orchestrator-guardian"
  GUARD_SRC="/Users/joshwegener/clawd/launchd/${GUARD_LABEL}.plist"
  GUARD_PLIST="$HOME/Library/LaunchAgents/${GUARD_LABEL}.plist"
  if [[ ! -f "$GUARD_PLIST" && -f "$GUARD_SRC" ]]; then
    mkdir -p "$HOME/Library/LaunchAgents"
    cp -f "$GUARD_SRC" "$GUARD_PLIST"
  fi
  if [[ -f "$GUARD_PLIST" ]]; then
    launchctl bootstrap "gui/$(id -u)" "$GUARD_PLIST" 2>/dev/null || true
    launchctl enable "gui/$(id -u)/${GUARD_LABEL}" 2>/dev/null || true
  else
    echo "WARN: missing guardian launchd plist: $GUARD_PLIST" >&2
  fi
fi

echo "clawd up"
