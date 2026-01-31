#!/usr/bin/env bash
set -euo pipefail

TASK_ID="${1:?task_id}"
REPO_KEY="${2:-}"
REPO_PATH="${3:-}"
PATCH_PATH="${4:-}"
LOG_PATH="${5:-/Users/joshwegener/clawd/memory/review-logs/review-task-${TASK_ID}.log}"
REVIEW_REVISION="${6:-}"

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
# Use a Python wrapper for Claude to avoid shell quoting issues and to enforce a timeout +
# always write a parseable `review_result: {...}` line.

if [[ -n "$REVIEW_REVISION" ]]; then
  nohup python3 /Users/joshwegener/clawd/scripts/run_claude_review.py \
    --repo-path "$REPO_PATH" \
    --log-path "$LOG_PATH" \
    --model "${CLAUDE_MODEL:-opus}" \
    --prompt "$PROMPT" \
    --revision "$REVIEW_REVISION" >/dev/null 2>&1 &
else
  nohup python3 /Users/joshwegener/clawd/scripts/run_claude_review.py \
    --repo-path "$REPO_PATH" \
    --log-path "$LOG_PATH" \
    --model "${CLAUDE_MODEL:-opus}" \
    --prompt "$PROMPT" >/dev/null 2>&1 &
fi

PID=$!

printf '{"execSessionId":"pid:%s","logPath":"%s"}\n' "$PID" "$LOG_PATH"
