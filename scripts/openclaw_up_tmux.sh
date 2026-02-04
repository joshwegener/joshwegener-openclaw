#!/usr/bin/env bash
set -euo pipefail

# Run the OpenClaw gateway inside tmux for visible logs (instead of launchd).
#
# This does NOT enable cron jobs; it's just the gateway service loop.

TMUX_SESSION="${OPENCLAW_TMUX_SESSION:-openclaw}"
TMUX_WINDOW="gateway"
PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
BIND="${OPENCLAW_GATEWAY_BIND:-loopback}"

# Ensure the gateway runs with a modern Node even when tmux/launchd PATH is stale.
# Josh's setup uses Herd-managed "nvm" installs under:
# /Users/joshwegener/Library/Application Support/Herd/config/nvm/versions/node/<ver>/bin
NODE_BIN_22_22="/Users/joshwegener/Library/Application Support/Herd/config/nvm/versions/node/v22.22.0/bin"
NODE_BIN_22_11="/Users/joshwegener/Library/Application Support/Herd/config/nvm/versions/node/v22.11.0/bin"
PATH_PREFIX=""
if [[ -x "${NODE_BIN_22_22}/node" ]]; then
  PATH_PREFIX="${NODE_BIN_22_22}"
fi
# Keep the older bin directory on PATH so openclaw itself (installed there) still resolves,
# while the `env node` shebang uses the newer node first.
if [[ -x "${NODE_BIN_22_11}/openclaw" ]]; then
  if [[ -n "${PATH_PREFIX}" ]]; then
    PATH_PREFIX="${PATH_PREFIX}:${NODE_BIN_22_11}"
  else
    PATH_PREFIX="${NODE_BIN_22_11}"
  fi
fi

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

if [[ -n "${PATH_PREFIX}" ]]; then
  tmux new-window -t "$TMUX_SESSION" -n "$TMUX_WINDOW" "export PATH=\"${PATH_PREFIX}:\$PATH\"; echo \"[openclaw] node=\$(node -v)\"; openclaw gateway --port \"$PORT\" --bind \"$BIND\""
else
  tmux new-window -t "$TMUX_SESSION" -n "$TMUX_WINDOW" "echo \"[openclaw] node=\$(node -v)\"; openclaw gateway --port \"$PORT\" --bind \"$BIND\""
fi

echo "openclaw gateway up in tmux session: $TMUX_SESSION (window: $TMUX_WINDOW)"
echo "attach: tmux attach -t $TMUX_SESSION"
