#!/usr/bin/env bash
set -euo pipefail

TASK_ID="${1:?task_id}"
REPO_KEY="${2:-}"
REPO_PATH="${3:-}"
PATCH_PATH="${4:-}"
LOG_PATH="${5:-/Users/joshwegener/clawd/memory/review-logs/review-task-${TASK_ID}.log}"

mkdir -p "$(dirname "$LOG_PATH")"

# Build prompt. Reviewer must output STRICT JSON only.
read -r -d '' PROMPT <<EOF || true
You are the automated code reviewer for RecallDeck Kanban task #${TASK_ID}.

Context:
- Repo key: ${REPO_KEY}
- Repo path: ${REPO_PATH}
- Patch path (may be empty): ${PATCH_PATH}

Instructions:
1) If PATCH_PATH is non-empty and exists, review the patch file contents.
2) Otherwise, review based on the Kanboard task title/description and current repo state.
3) Output STRICT JSON only. No markdown, no prose.

JSON schema (you MUST output exactly this object; no wrapper fields):
{
  "score": <int 1-100>,
  "verdict": "PASS"|"REWORK"|"BLOCKER",
  "critical_items": ["..."],
  "notes": "short summary"
}

Policy:
- Use a high bar. Default passing threshold is 90.
- If there are ANY critical_items, the review MUST fail (verdict must be REWORK or BLOCKER) regardless of score.
- critical_items should be concrete, actionable, and limited to the truly blocking issues.
EOF

# IMPORTANT:
# - Use `--output-format text` so Claude prints ONLY the JSON object (as instructed).
# - Write to a temp file then append, to avoid tee/tail races writing to the same file.
# - Emit a single-line `review_result: {...}` marker for the orchestrator to parse.

nohup bash -lc "set -euo pipefail; \
  cd \"$REPO_PATH\"; \
  tmp=\"$(mktemp -t review-${TASK_ID}.XXXXXX)\"; \
  printf '### REVIEW START %s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" >> \"$LOG_PATH\"; \
  claude -p --model opus --dangerously-skip-permissions --output-format text \"$PROMPT\" > \"$tmp\"; \
  compact=\"$(python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin),separators=(\",\",\":\")))' < \"$tmp\")\"; \
  printf '%s\\n' \"$compact\" >> \"$LOG_PATH\"; \
  printf 'review_result: %s\\n' \"$compact\" >> \"$LOG_PATH\"; \
  rm -f \"$tmp\"" >/dev/null 2>&1 &

PID=$!

printf '{"execSessionId":"pid:%s","logPath":"%s"}\n' "$PID" "$LOG_PATH"
