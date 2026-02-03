#!/usr/bin/env bash
set -euo pipefail

# Ensure the clawd tmux session exists with an orchestrator window running.
# Safe to run multiple times.

TMUX_SESSION="${CLAWD_TMUX_SESSION:-clawd}"

ensure_session() {
  if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    return 0
  fi
  tmux new-session -d -s "$TMUX_SESSION" -n orchestrator "bash"
}

window_exists() {
  local name="$1"
  tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | rg -q "^${name}$"
}

window_ids_by_name() {
  local name="$1"
  tmux list-windows -t "$TMUX_SESSION" -F '#{window_id}:#{window_name}' 2>/dev/null \
    | awk -F: -v n="$name" '$2 == n { print $1 }'
}

ensure_window_cmd() {
  local name="$1"
  local cmd="$2"

  if window_exists "$name"; then
    # If duplicates exist (shouldn't), keep the first and kill the rest.
    ids=($(window_ids_by_name "$name" || true))
    if [[ "${#ids[@]}" -gt 1 ]]; then
      for ((i=1; i<${#ids[@]}; i++)); do
        tmux kill-window -t "${ids[$i]}" 2>/dev/null || true
      done
    fi

    # Replace whatever is running in the first matching window.
    local target="${TMUX_SESSION}:${name}"
    if [[ "${#ids[@]}" -ge 1 ]]; then
      target="${ids[0]}.0"
    fi
    tmux respawn-pane -k -t "$target" "bash -lc $(printf %q "$cmd")"
  else
    tmux new-window -t "$TMUX_SESSION" -n "$name" "bash -lc $(printf %q "$cmd")"
  fi
}

ensure_session

chmod +x /Users/joshwegener/clawd/scripts/run_orchestrator_loop.sh
chmod +x /Users/joshwegener/clawd/scripts/spawn_worker_tmux.sh
chmod +x /Users/joshwegener/clawd/scripts/spawn_reviewer_tmux.sh

ensure_window_cmd "orchestrator" "/Users/joshwegener/clawd/scripts/run_orchestrator_loop.sh"

# Convenience: quick tails (optional, but helpful).
mkdir -p /Users/joshwegener/clawd/memory/worker-logs /Users/joshwegener/clawd/memory/review-logs
ensure_window_cmd "worker-logs" "tail -n 200 -F /Users/joshwegener/clawd/memory/worker-logs/*.log 2>/dev/null || bash"
ensure_window_cmd "review-logs" "tail -n 200 -F /Users/joshwegener/clawd/memory/review-logs/*.log 2>/dev/null || bash"

echo "tmux session ready: ${TMUX_SESSION}"
echo "attach: tmux attach -t ${TMUX_SESSION}"
