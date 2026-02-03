#!/usr/bin/env bash
set -euo pipefail

# Simple notification hook for board_orchestrator.py.
# Expects:
# - CLAWD_NOTIFY_TELEGRAM_TARGET (e.g. 7998882588)
# - BOARD_ORCHESTRATOR_NOTIFY_MESSAGE (set by orchestrator)
#
# This script is intentionally best-effort and never fails the orchestrator loop.

TARGET="${CLAWD_NOTIFY_TELEGRAM_TARGET:-}"
MSG="${BOARD_ORCHESTRATOR_NOTIFY_MESSAGE:-}"

if [[ -z "$TARGET" || -z "$MSG" ]]; then
  exit 0
fi

{
  if command -v openclaw >/dev/null 2>&1; then
    openclaw message send \
      --channel telegram \
      --target "$TARGET" \
      --message "$MSG" \
      --json \
      >/dev/null
  else
    clawdbot message send \
      --channel telegram \
      --target "$TARGET" \
      --message "$MSG" \
      --json \
      >/dev/null
  fi
} || true
