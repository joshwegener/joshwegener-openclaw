#!/usr/bin/env bash
set -euo pipefail

# Stop OpenClaw gateway (tmux + launchd).

launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.openclaw.gateway.plist" 2>/dev/null || true

pkill -f 'openclaw gateway' 2>/dev/null || true
pkill -f 'openclaw.*gateway' 2>/dev/null || true

if tmux ls 2>/dev/null | rg -q '^openclaw:'; then
  tmux kill-session -t openclaw 2>/dev/null || true
fi

echo "openclaw down"

