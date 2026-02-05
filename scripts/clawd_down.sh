#!/usr/bin/env bash
set -euo pipefail

# Stop the RecallDeck Kanboard orchestrator (tmux + launchd).

PLIST="$HOME/Library/LaunchAgents/com.recalldeck.clawd.orchestrator.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
GUARD_PLIST="$HOME/Library/LaunchAgents/com.recalldeck.clawd.orchestrator-guardian.plist"
launchctl bootout "gui/$(id -u)" "$GUARD_PLIST" 2>/dev/null || true

pkill -f '/Users/joshwegener/clawd/scripts/run_orchestrator_loop.sh' 2>/dev/null || true
pkill -f '/Users/joshwegener/clawd/scripts/board_orchestrator.py' 2>/dev/null || true
pkill -f '/Users/joshwegener/clawd/scripts/run_orchestrator_guardian.sh' 2>/dev/null || true
pkill -f '/Users/joshwegener/clawd/scripts/orchestrator_guardian.py' 2>/dev/null || true

if tmux ls 2>/dev/null | rg -q '^clawd:'; then
  # Kill only the orchestrator window; keep workers for forensics.
  tmux list-windows -t clawd -F '#{window_id}:#{window_name}' \
    | awk -F: '$2 == "orchestrator" { print $1 }' \
    | while IFS= read -r wid; do
        [[ -n "$wid" ]] || continue
        tmux kill-window -t "$wid" 2>/dev/null || true
      done
fi

echo "clawd down"
