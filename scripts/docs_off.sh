#!/usr/bin/env bash
set -euo pipefail

# Disable docs automation for the Kanboard orchestrator.
# Edits ~/.config/clawd/orchestrator.env (local-only).

ENV_FILE="${CLAWD_ENV_FILE:-$HOME/.config/clawd/orchestrator.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

perl -0777 -i -pe 's/^export\\s+BOARD_ORCHESTRATOR_DOCS_SPAWN_CMD=.*\\n//mg; s/^export\\s+BOARD_ORCHESTRATOR_DOCS_WIP_LIMIT=.*\\n//mg' "$ENV_FILE"
cat >>"$ENV_FILE" <<'EOF'

# Docs automation disabled
export BOARD_ORCHESTRATOR_DOCS_SPAWN_CMD=""
export BOARD_ORCHESTRATOR_DOCS_WIP_LIMIT="1"
EOF

echo "docs automation disabled"

