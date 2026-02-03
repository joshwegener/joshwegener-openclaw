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

KB_TITLE=""
KB_DESC=""
if [[ -n "${KANBOARD_BASE:-}" && -n "${KANBOARD_USER:-}" && -n "${KANBOARD_TOKEN:-}" ]]; then
  # Best-effort fetch; do not fail worker spawn if Kanboard is down.
  kb_json="$(python3 - <<'PY' "$TASK_ID" 2>/dev/null || true
import base64, json, os, sys, urllib.request

task_id = int(sys.argv[1])
base = os.environ.get("KANBOARD_BASE") or ""
user = os.environ.get("KANBOARD_USER") or ""
token = os.environ.get("KANBOARD_TOKEN") or ""
if not base or not user or not token:
    raise SystemExit(0)

payload = {"jsonrpc": "2.0", "method": "getTask", "id": 1, "params": [task_id]}
auth = base64.b64encode(f"{user}:{token}".encode()).decode()
req = urllib.request.Request(
    base,
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
)
with urllib.request.urlopen(req, timeout=10) as resp:
    raw = resp.read().decode()
out = json.loads(raw)
res = out.get("result") or {}
print(json.dumps({"title": res.get("title") or "", "description": res.get("description") or ""}))
PY
)"
  if [[ -n "$kb_json" ]]; then
    KB_TITLE="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get(\"title\", \"\"))' <<<"$kb_json" 2>/dev/null || true)"
    KB_DESC="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get(\"description\", \"\"))' <<<"$kb_json" 2>/dev/null || true)"
  fi
fi

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

Task context (already fetched for you; do NOT attempt to log into Kanboard UI):
Title: ${KB_TITLE}
Description:
${KB_DESC}

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
1) Use the task context above (title/description). Do NOT try to log into Kanboard.
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

# Prevent stale PID reads before the new tmux window has a chance to overwrite it.
rm -f "$PID_PATH" 2>/dev/null || true
start_epoch="$(date +%s)"

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
    mtime="$(stat -f %m "$PID_PATH" 2>/dev/null || echo 0)"
    if [[ "$mtime" =~ ^[0-9]+$ ]] && (( mtime >= start_epoch )); then
      pid="$(cat "$PID_PATH" 2>/dev/null || true)"
      break
    fi
  fi
  sleep 0.05
done

handle="tmux:${TMUX_SESSION}:${TMUX_WINDOW}"
if [[ "$pid" =~ ^[0-9]+$ ]]; then
  handle="pid:${pid} ${handle}"
fi

printf '{"execSessionId":"%s","logPath":"%s"}\n' "$handle" "$LOG_PATH"
