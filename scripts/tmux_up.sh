#!/usr/bin/env bash
set -euo pipefail

# Ensure the clawd tmux session exists with an orchestrator window running.
# Safe to run multiple times.

TMUX_SESSION="${CLAWD_TMUX_SESSION:-clawd}"
ORCHESTRATOR_WINDOW_CMD="${CLAWD_ORCHESTRATOR_WINDOW_CMD:-/Users/joshwegener/clawd/scripts/run_orchestrator_loop.sh}"

DEFAULT_ENV_FILE=""
if [[ -f "/Users/joshwegener/.config/clawd/orchestrator.env" ]]; then
  DEFAULT_ENV_FILE="/Users/joshwegener/.config/clawd/orchestrator.env"
elif [[ -f "${HOME:-}/.config/clawd/orchestrator.env" ]]; then
  DEFAULT_ENV_FILE="${HOME:-}/.config/clawd/orchestrator.env"
fi
ENV_FILE="${CLAWD_ORCHESTRATOR_ENV_FILE:-${CLAWD_ENV_FILE:-$DEFAULT_ENV_FILE}}"

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
      # Pane indices can be non-zero (user config). Target by pane_id.
      pane_id="$(tmux list-panes -t "${ids[0]}" -F '#{pane_id}' 2>/dev/null | head -n 1 || true)"
      if [[ -n "$pane_id" ]]; then
        target="$pane_id"
      fi
    fi
    tmux respawn-pane -k -t "$target" "bash -lc $(printf %q "$cmd")"
  else
    tmux new-window -t "$TMUX_SESSION" -n "$name" "bash -lc $(printf %q "$cmd")"
  fi
}

ensure_session

# Ensure a stable pointer to the orchestrator env file is available inside tmux.
if [[ -n "${ENV_FILE:-}" ]]; then
  tmux set-environment -t "$TMUX_SESSION" "CLAWD_ORCHESTRATOR_ENV_FILE" "$ENV_FILE" 2>/dev/null || true
fi
if [[ -n "${CLAWD_TICK_SECONDS:-}" ]]; then
  tmux set-environment -t "$TMUX_SESSION" "CLAWD_TICK_SECONDS" "$CLAWD_TICK_SECONDS" 2>/dev/null || true
fi
if [[ -n "${CLAWD_ORCHESTRATOR_HEARTBEAT_PATH:-}" ]]; then
  tmux set-environment -t "$TMUX_SESSION" "CLAWD_ORCHESTRATOR_HEARTBEAT_PATH" "$CLAWD_ORCHESTRATOR_HEARTBEAT_PATH" 2>/dev/null || true
fi
if [[ -n "${CLAWD_ORCHESTRATOR_WINDOW_CMD:-}" ]]; then
  tmux set-environment -t "$TMUX_SESSION" "CLAWD_ORCHESTRATOR_WINDOW_CMD" "$CLAWD_ORCHESTRATOR_WINDOW_CMD" 2>/dev/null || true
fi

chmod +x /Users/joshwegener/clawd/scripts/run_orchestrator_loop.sh
chmod +x /Users/joshwegener/clawd/scripts/spawn_worker_tmux.sh
chmod +x /Users/joshwegener/clawd/scripts/spawn_reviewer_tmux.sh
chmod +x /Users/joshwegener/clawd/scripts/tail_latest_logs.sh

ensure_window_cmd "orchestrator" "$ORCHESTRATOR_WINDOW_CMD"

# Convenience: quick tails (optional, but helpful).
ensure_window_cmd "worker-logs" "/Users/joshwegener/clawd/scripts/tail_latest_logs.sh /Users/joshwegener/clawd/runs/worker worker.log 20 200"
ensure_window_cmd "review-logs" "/Users/joshwegener/clawd/scripts/tail_latest_logs.sh /Users/joshwegener/clawd/runs/review review.log 20 200"
ensure_window_cmd "orchestrator-logs" "tail -n 300 -F /Users/joshwegener/clawd/memory/orchestrator.log 2>/dev/null || bash"

echo "tmux session ready: ${TMUX_SESSION}"
echo "attach: tmux attach -t ${TMUX_SESSION}"
