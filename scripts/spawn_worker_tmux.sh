#!/usr/bin/env bash
set -euo pipefail

# Spawn a Codex worker inside tmux so you can watch live output.
#
# Orchestrator spawn contract:
# - MUST print one JSON object to stdout:
#   {"execSessionId":"...","logPath":"..."}
#
# We return a pid-based handle (pid:<pid>) so board_orchestrator can do
# best-effort liveness checks, plus a tmux hint for humans.

TASK_ID="${1:?task_id}"
REPO_KEY="${2:-}"       # already shell-escaped by caller
REPO_PATH="${3:-}"      # already shell-escaped by caller

TMUX_SESSION="${CLAWD_TMUX_SESSION:-clawd}"
TMUX_WINDOW="worker-${TASK_ID}"

LOG_DIR="/Users/joshwegener/clawd/memory/worker-logs"
mkdir -p "$LOG_DIR"
LOG_PATH="$LOG_DIR/task-${TASK_ID}.log"

PATCH_DIR="/Users/joshwegener/clawd/tmp/worker-patches"
mkdir -p "$PATCH_DIR"
PATCH_PATH="$PATCH_DIR/task-${TASK_ID}.patch"

RUN_DIR="/Users/joshwegener/clawd/tmp/worker-runs"
mkdir -p "$RUN_DIR"
RUN_PATH="$RUN_DIR/task-${TASK_ID}.sh"

PID_DIR="/Users/joshwegener/clawd/tmp/worker-pids"
mkdir -p "$PID_DIR"
PID_PATH="$PID_DIR/task-${TASK_ID}.pid"

COMMENT_PATH="/Users/joshwegener/clawd/tmp/kanboard-task-${TASK_ID}-comment.md"

cat >"$RUN_PATH" <<EOF
#!/usr/bin/env bash
set -euo pipefail

TASK_ID="${TASK_ID}"
REPO_KEY="${REPO_KEY}"
REPO_PATH="${REPO_PATH}"
LOG_PATH="${LOG_PATH}"
PID_PATH="${PID_PATH}"
PATCH_PATH="${PATCH_PATH}"
COMMENT_PATH="${COMMENT_PATH}"

mkdir -p "\$(dirname "\$LOG_PATH")" "\$(dirname "\$PID_PATH")"

read -r -d '' PROMPT <<'PROMPT' || true
You are the RecallDeck worker for Kanboard task #${TASK_ID}.

HARD SAFETY RULES (must follow):
- NEVER read, open, or print any of these assistant/user context files:
  - /Users/joshwegener/clawd/MEMORY.md
  - /Users/joshwegener/clawd/USER.md
  - /Users/joshwegener/clawd/SOUL.md
  - /Users/joshwegener/clawd/AGENTS.md
  - /Users/joshwegener/clawd/TOOLS.md
  - /Users/joshwegener/clawd/HEARTBEAT.md
  - /Users/joshwegener/clawd/IDENTITY.md
  - anything under /Users/joshwegener/clawd/memory/
- Do not search for secrets/keys/tokens or paste private content into logs/comments.
- Only read/edit code relevant to the task inside the repo (typically scripts/, tests/, src/).

Work in the repo at: ${REPO_PATH}
Repo key: ${REPO_KEY}

Steps:
1) Read the task title/description from Kanboard (UI: http://localhost:8401/).
2) Implement the work in this repo clone.
3) Commit changes with a clear message and push if origin is configured.

4) Export a patch to this exact path:
   ${PATCH_PATH}
   - If you created a commit, prefer:
     git format-patch -1 HEAD --stdout > ${PATCH_PATH}
   - Otherwise:
     git diff > ${PATCH_PATH}

   Then print EXACTLY this line (so automation can detect completion):
   Patch file: \`${PATCH_PATH}\`

5) Write a ready-to-paste Kanboard comment to:
   ${COMMENT_PATH}
   Mention that file path in your output.
   Then STOP (no extra chatter).

6) If you cannot commit/push in this environment, still produce the patch + comment file.
PROMPT

rm -f "\$PID_PATH"

nohup codex exec \\
  --dangerously-bypass-approvals-and-sandbox \\
  --profile "\${CODEX_PROFILE:-chigh}" \\
  -C "\$REPO_PATH" \\
  "\$PROMPT" \\
  >>"\$LOG_PATH" 2>&1 &

PID=\$!
echo "\$PID" >"\$PID_PATH"
wait "\$PID" || true
echo \"[worker \$TASK_ID] done\" >>\"\$LOG_PATH\" 2>&1 || true

# Keep the tmux window open for inspection.
exec bash
EOF

chmod +x "$RUN_PATH"

if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  tmux new-session -d -s "$TMUX_SESSION" -n orchestrator "bash"
fi

tmux new-window -k -t "$TMUX_SESSION" -n "$TMUX_WINDOW" "$RUN_PATH"

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

