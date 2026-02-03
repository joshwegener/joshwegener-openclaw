#!/usr/bin/env bash
set -euo pipefail

# Enable Telegram notifications for the Kanboard orchestrator.
# Edits ~/.config/clawd/orchestrator.env (local-only).

TARGET="${1:-7998882588}"
ENV_FILE="${CLAWD_ENV_FILE:-$HOME/.config/clawd/orchestrator.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

# Remove any existing notify lines and re-add.
perl -0777 -i -pe 's/^export\\s+BOARD_ORCHESTRATOR_NOTIFY_CMD=.*\\n//mg; s/^export\\s+CLAWD_NOTIFY_TELEGRAM_TARGET=.*\\n//mg; s/^export\\s+CLAWD_NOTIFY_DENY_TARGETS=.*\\n//mg' "$ENV_FILE"

cat >>"$ENV_FILE" <<EOF

# Notifications (Telegram)
export BOARD_ORCHESTRATOR_NOTIFY_CMD=\"/Users/joshwegener/clawd/scripts/notify_telegram.sh\"
export CLAWD_NOTIFY_TELEGRAM_TARGET=\"${TARGET}\"
# Safety denylist (comma-separated)
export CLAWD_NOTIFY_DENY_TARGETS=\"3202660002,+13202660002\"
EOF

echo "notifications enabled -> telegram target: $TARGET"

