#!/usr/bin/env bash
set -euo pipefail

TASK_ID="${1:?task_id}"
REPO_PATH="${2:-}"
PATCH_PATH="${3:-}"

LOG_DIR="/Users/joshwegener/clawd/memory/worker-logs"
mkdir -p "$LOG_DIR"
LOG_PATH="$LOG_DIR/review-task-${TASK_ID}.log"

PROMPT=$(cat <<EOF
You are the RecallDeck reviewer for Kanboard task #${TASK_ID}.

Review the work and output a single line with the sentinel:
REVIEW_RESULT: {"score": <1-100>, "verdict": "PASS"|"REWORK"|"BLOCKER", "notes": "..."}

Requirements:
- Give a score 1-100 and verdict PASS, REWORK, or BLOCKER.
- Include brief notes.
- Do not modify files or run destructive commands.

Context:
- Kanboard UI: http://localhost:8401/
- Repo: ${REPO_PATH}
- Patch: ${PATCH_PATH}
EOF
)

if command -v claude >/dev/null 2>&1; then
  if [ -n "${CLAUDE_MODEL:-}" ]; then
    nohup claude --model "${CLAUDE_MODEL}" "$PROMPT" >>"$LOG_PATH" 2>&1 &
  else
    nohup claude "$PROMPT" >>"$LOG_PATH" 2>&1 &
  fi
elif command -v codex >/dev/null 2>&1; then
  nohup codex exec \
    --dangerously-bypass-approvals-and-sandbox \
    --profile ghigh \
    -C "$REPO_PATH" \
    "$PROMPT" \
    >>"$LOG_PATH" 2>&1 &
else
  echo "No claude or codex found for reviewer" >>"$LOG_PATH"
  exit 1
fi

PID=$!

printf '{"execSessionId":"pid:%s","logPath":"%s"}\n' "$PID" "$LOG_PATH"
