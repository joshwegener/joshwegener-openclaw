#!/usr/bin/env bash
set -euo pipefail

# Stop the RecallDeck Kanboard orchestrator (tmux + launchd).
#
# Usage:
# - ./scripts/clawd_down.sh            # stop orchestrator only (keep worker/reviewer/docs windows for forensics)
# - ./scripts/clawd_down.sh --kill-jobs # also stop any in-flight worker/reviewer/docs jobs and close their tmux windows

PLIST="$HOME/Library/LaunchAgents/com.recalldeck.clawd.orchestrator.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
GUARD_PLIST="$HOME/Library/LaunchAgents/com.recalldeck.clawd.orchestrator-guardian.plist"
launchctl bootout "gui/$(id -u)" "$GUARD_PLIST" 2>/dev/null || true

# Prevent launchd from auto-restarting it until explicitly re-enabled.
launchctl disable "gui/$(id -u)/com.recalldeck.clawd.orchestrator" 2>/dev/null || true
launchctl disable "gui/$(id -u)/com.recalldeck.clawd.orchestrator-guardian" 2>/dev/null || true

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

if [[ "${1:-}" == "--kill-jobs" ]]; then
  # Kill in-flight Codex/Claude jobs spawned from clawd run directories.
  pkill -f '/Users/joshwegener/clawd/runs/worker/' 2>/dev/null || true
  pkill -f '/Users/joshwegener/clawd/runs/review/' 2>/dev/null || true
  pkill -f '/Users/joshwegener/clawd/runs/docs/' 2>/dev/null || true

  # Close per-task windows (worker-*, review-*, docs-*). Keep tail windows.
  tmux list-windows -t clawd -F '#{window_id}:#{window_name}' 2>/dev/null \
    | awk -F: '$2 ~ /^(worker|review|docs)-/ { print $1 }' \
    | while IFS= read -r wid; do
        [[ -n "$wid" ]] || continue
        tmux kill-window -t "$wid" 2>/dev/null || true
      done
fi

echo "clawd down"
