#!/usr/bin/env bash
set -euo pipefail

# Simple notification hook for board_orchestrator.py.
# Expects:
# - CLAWD_NOTIFY_TELEGRAM_TARGET (e.g. 7998882588)
# - BOARD_ORCHESTRATOR_NOTIFY_MESSAGE (set by orchestrator)
#
# This script is intentionally best-effort and never fails the orchestrator loop.
# Safety:
# - CLAWD_NOTIFY_DENY_TARGETS: comma-separated list of targets to never message.

TARGET="${CLAWD_NOTIFY_TELEGRAM_TARGET:-}"
MSG="${BOARD_ORCHESTRATOR_NOTIFY_MESSAGE:-}"
DENY="${CLAWD_NOTIFY_DENY_TARGETS:-}"

if [[ -z "$TARGET" || -z "$MSG" ]]; then
  exit 0
fi

deny_hit="0"
if [[ -n "$DENY" ]]; then
  IFS=',' read -r -a deny_list <<<"$DENY"
  for x in "${deny_list[@]}"; do
    x="$(echo "$x" | tr -d '[:space:]')"
    [[ -n "$x" ]] || continue
    if [[ "$TARGET" == "$x" ]]; then
      deny_hit="1"
      break
    fi
  done
fi
# Hard safety block (prevents accidental spamming of Josh's iMessage number if miswired).
if [[ "$TARGET" == "3202660002" || "$TARGET" == "+13202660002" ]]; then
  deny_hit="1"
fi
if [[ "$deny_hit" == "1" ]]; then
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
