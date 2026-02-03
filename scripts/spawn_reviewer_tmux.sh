#!/usr/bin/env bash
set -euo pipefail

# Spawn a Claude reviewer inside tmux so you can watch live output.
#
# Orchestrator spawn contract:
# - MUST print one JSON object to stdout:
#   {"execSessionId":"...","logPath":"..."}
#
# We return a pid-based handle (pid:<pid>) so board_orchestrator can do
# best-effort liveness checks, plus a tmux hint for humans.

TASK_ID="${1:?task_id}"
REPO_KEY="${2:-}"
REPO_PATH="${3:-}"
PATCH_PATH="${4:-}"
LOG_PATH="${5:-/Users/joshwegener/clawd/memory/review-logs/review-task-${TASK_ID}.log}"
REVIEW_REVISION="${6:-}"

TMUX_SESSION="${CLAWD_TMUX_SESSION:-clawd}"
TMUX_WINDOW="review-${TASK_ID}"

mkdir -p "$(dirname "$LOG_PATH")"

RUN_DIR="/Users/joshwegener/clawd/tmp/reviewer-runs"
mkdir -p "$RUN_DIR"
RUN_PATH="$RUN_DIR/task-${TASK_ID}.sh"

PID_DIR="/Users/joshwegener/clawd/tmp/reviewer-pids"
mkdir -p "$PID_DIR"
PID_PATH="$PID_DIR/task-${TASK_ID}.pid"

cat >"$RUN_PATH" <<EOF
#!/usr/bin/env bash
set -euo pipefail

TASK_ID="${TASK_ID}"
REPO_KEY="${REPO_KEY}"
REPO_PATH="${REPO_PATH}"
PATCH_PATH="${PATCH_PATH}"
LOG_PATH="${LOG_PATH}"
PID_PATH="${PID_PATH}"
REVIEW_REVISION="${REVIEW_REVISION}"

mkdir -p "\$(dirname "\$LOG_PATH")" "\$(dirname "\$PID_PATH")"

read -r -d '' PROMPT <<'PROMPT' || true
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
- Use a high bar. Default passing threshold is 85.
- If there are ANY critical_items, the review MUST fail (verdict must be REWORK or BLOCKER) regardless of score.
- critical_items should be concrete, actionable, and limited to the truly blocking issues.
PROMPT

rm -f "\$PID_PATH"

if [[ -n "\$REVIEW_REVISION" ]]; then
  nohup python3 /Users/joshwegener/clawd/scripts/run_claude_review.py \\
    --repo-path "\$REPO_PATH" \\
    --log-path "\$LOG_PATH" \\
    --model "\${CLAUDE_MODEL:-opus}" \\
    --prompt "\$PROMPT" \\
    --revision "\$REVIEW_REVISION" >>"\$LOG_PATH" 2>&1 &
else
  nohup python3 /Users/joshwegener/clawd/scripts/run_claude_review.py \\
    --repo-path "\$REPO_PATH" \\
    --log-path "\$LOG_PATH" \\
    --model "\${CLAUDE_MODEL:-opus}" \\
    --prompt "\$PROMPT" >>"\$LOG_PATH" 2>&1 &
fi

PID=\$!
echo "\$PID" >"\$PID_PATH"
wait "\$PID" || true
echo \"[review \$TASK_ID] done\" >>\"\$LOG_PATH\" 2>&1 || true

exec bash
EOF

chmod +x "$RUN_PATH"

if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  tmux new-session -d -s "$TMUX_SESSION" -n orchestrator "bash"
fi

# Deduplicate by name (tmux allows duplicate window names).
tmux list-windows -t "$TMUX_SESSION" -F '#{window_id}:#{window_name}' 2>/dev/null \
  | awk -F: -v n="$TMUX_WINDOW" '$2 == n { print $1 }' \
  | while IFS= read -r wid; do
      [[ -n "$wid" ]] || continue
      tmux kill-window -t "$wid" 2>/dev/null || true
    done

tmux new-window -t "$TMUX_SESSION" -n "$TMUX_WINDOW" "$RUN_PATH"

pid=""
for _ in $(seq 1 50); do
  if [[ -f "$PID_PATH" ]]; then
    pid="$(cat "$PID_PATH" 2>/dev/null || true)"
    break
  fi
  sleep 0.05
done

handle="tmux:${TMUX_SESSION}:${TMUX_WINDOW}"
if [[ "$pid" =~ ^[0-9]+$ ]]; then
  handle="pid:${pid} ${handle}"
fi

printf '{"execSessionId":"%s","logPath":"%s"}\n' "$handle" "$LOG_PATH"
