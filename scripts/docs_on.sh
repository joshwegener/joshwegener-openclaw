#!/usr/bin/env bash
set -euo pipefail

# Enable docs automation for the Kanboard orchestrator.
# Edits ~/.config/clawd/orchestrator.env (local-only).
#
# This config causes cards in Documentation with docs:auto + docs:pending to spawn
# a Codex docs worker in tmux via scripts/spawn_docs_tmux.sh.

ENV_FILE="${CLAWD_ENV_FILE:-$HOME/.config/clawd/orchestrator.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

# Remove any existing docs spawn config lines and re-add.
perl -0777 -i -pe 's/^export\\s+BOARD_ORCHESTRATOR_DOCS_SPAWN_CMD=.*\\n//mg; s/^export\\s+BOARD_ORCHESTRATOR_DOCS_WIP_LIMIT=.*\\n//mg' "$ENV_FILE"

cat >>"$ENV_FILE" <<'EOF'

# Docs automation (Documentation column)
export BOARD_ORCHESTRATOR_DOCS_SPAWN_CMD="/Users/joshwegener/clawd/scripts/spawn_docs_tmux.sh {task_id} {repo_key} {repo_path} {patch_path}"
# Global docs concurrency (default 1)
export BOARD_ORCHESTRATOR_DOCS_WIP_LIMIT="1"
EOF

echo "docs automation enabled"

