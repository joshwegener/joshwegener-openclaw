#!/usr/bin/env bash
set -euo pipefail

# Disable Telegram notifications for the Kanboard orchestrator.

ENV_FILE="${CLAWD_ENV_FILE:-$HOME/.config/clawd/orchestrator.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

perl -0777 -i -pe 's/^export\\s+BOARD_ORCHESTRATOR_NOTIFY_CMD=.*\\n//mg; s/^export\\s+CLAWD_NOTIFY_TELEGRAM_TARGET=.*\\n//mg; s/^export\\s+CLAWD_NOTIFY_DENY_TARGETS=.*\\n//mg' "$ENV_FILE"
cat >>"$ENV_FILE" <<'EOF'

# Notifications disabled
export BOARD_ORCHESTRATOR_NOTIFY_CMD=""
export CLAWD_NOTIFY_TELEGRAM_TARGET=""
EOF

echo "notifications disabled"

