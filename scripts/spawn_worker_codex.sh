#!/usr/bin/env bash
set -euo pipefail

TASK_ID="${1:?task_id}"
REPO_KEY="${2:-}"       # already shell-escaped by caller
REPO_PATH="${3:-}"      # already shell-escaped by caller

LOG_DIR="/Users/joshwegener/clawd/memory/worker-logs"
mkdir -p "$LOG_DIR"
LOG_PATH="$LOG_DIR/task-${TASK_ID}.log"

PATCH_DIR="/Users/joshwegener/clawd/tmp/worker-patches"
mkdir -p "$PATCH_DIR"
PATCH_PATH="$PATCH_DIR/task-${TASK_ID}.patch"

COMMENT_PATH="/Users/joshwegener/clawd/tmp/kanboard-task-${TASK_ID}-comment.md"

read -r -d '' PROMPT <<EOF || true
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
EOF

# Run Codex in the background; write all output to the per-task worker log.
nohup codex exec \
  --dangerously-bypass-approvals-and-sandbox \
  --profile chigh \
  -C "$REPO_PATH" \
  "$PROMPT" \
  >>"$LOG_PATH" 2>&1 &

PID=$!

# Return a JSON object that the orchestrator can parse.
printf '{"execSessionId":"pid:%s","logPath":"%s"}\n' "$PID" "$LOG_PATH"
