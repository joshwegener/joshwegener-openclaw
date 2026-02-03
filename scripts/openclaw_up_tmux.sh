#!/usr/bin/env bash
set -euo pipefail

# Run the OpenClaw gateway inside tmux for visible logs (instead of launchd).
#
# This does NOT enable cron jobs; it's just the gateway service loop.

TMUX_SESSION="${OPENCLAW_TMUX_SESSION:-openclaw}"
TMUX_WINDOW="gateway"
PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
BIND="${OPENCLAW_GATEWAY_BIND:-loopback}"

if ! command -v openclaw >/dev/null 2>&1; then
  echo "openclaw not found in PATH" >&2
  exit 1
fi

# Ensure the launchd gateway is not running (avoid double-bind)
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.openclaw.gateway.plist" 2>/dev/null || true

if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  # Create a neutral base window so dedupe logic below doesn't accidentally
  # kill the only window and delete the session.
  tmux new-session -d -s "$TMUX_SESSION" -n shell "bash"
fi

# Deduplicate gateway window by name
tmux list-windows -t "$TMUX_SESSION" -F '#{window_id}:#{window_name}' 2>/dev/null \
  | awk -F: -v n="$TMUX_WINDOW" '$2 == n { print $1 }' \
  | while IFS= read -r wid; do
      [[ -n "$wid" ]] || continue
      tmux kill-window -t "$wid" 2>/dev/null || true
    done

tmux new-window -t "$TMUX_SESSION" -n "$TMUX_WINDOW" "openclaw gateway --port \"$PORT\" --bind \"$BIND\""

echo "openclaw gateway up in tmux session: $TMUX_SESSION (window: $TMUX_WINDOW)"
echo "attach: tmux attach -t $TMUX_SESSION"
