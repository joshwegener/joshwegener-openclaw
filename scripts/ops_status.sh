#!/usr/bin/env bash
set -euo pipefail

echo "=== clawd ==="
if tmux ls 2>/dev/null | rg -q '^clawd:'; then
  tmux list-windows -t clawd -F '#{window_index}:#{window_name} panes=#{window_panes} active=#{window_active}' || true
else
  echo "tmux: no clawd session"
fi
pgrep -fl 'run_orchestrator_loop.sh|board_orchestrator.py' || echo "process: none"

echo
echo "=== openclaw ==="
if tmux ls 2>/dev/null | rg -q '^openclaw:'; then
  tmux list-windows -t openclaw -F '#{window_index}:#{window_name} panes=#{window_panes} active=#{window_active}' || true
else
  echo "tmux: no openclaw session"
fi

lsof -nP -iTCP:18789 -sTCP:LISTEN >/dev/null 2>&1 && echo "gateway: listening on 18789" || echo "gateway: not listening"
openclaw status | sed -n '1,70p' || true

