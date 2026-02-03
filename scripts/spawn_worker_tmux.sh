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
CODEX_BIN="${CODEX_BIN:-$(command -v codex 2>/dev/null || true)}"
if [[ -z "$CODEX_BIN" && -x "/Users/joshwegener/Library/Application Support/Herd/config/nvm/versions/node/v22.11.0/bin/codex" ]]; then
  CODEX_BIN="/Users/joshwegener/Library/Application Support/Herd/config/nvm/versions/node/v22.11.0/bin/codex"
fi

TMUX_BIN="${TMUX_BIN:-$(command -v tmux 2>/dev/null || true)}"
if [[ -z "$TMUX_BIN" ]]; then
  for cand in /opt/homebrew/bin/tmux /usr/local/bin/tmux; do
    if [[ -x "$cand" ]]; then
      TMUX_BIN="$cand"
      break
    fi
  done
fi
if [[ -z "$TMUX_BIN" ]]; then
  echo "tmux not found in PATH and no fallback tmux binary found" >&2
  exit 1
fi

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

cat >"$RUN_PATH" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

TASK_ID="${1:?task_id}"
REPO_KEY="${2:?repo_key}"
REPO_PATH="${3:?repo_path}"
LOG_PATH="${4:?log_path}"
PID_PATH="${5:?pid_path}"
PATCH_PATH="${6:?patch_path}"
COMMENT_PATH="${7:?comment_path}"
CODEX_BIN="${8:?codex_bin}"

mkdir -p "$(dirname "$LOG_PATH")" "$(dirname "$PID_PATH")"

# Ensure Kanboard env is present even when spawned from launchd/tmux without a full shell env.
# Important: tmux sessions often retain partial env (e.g. KANBOARD_BASE) without creds.
if [[ -f "${HOME}/.config/clawd/orchestrator.env" ]]; then
  if [[ -z "${KANBOARD_BASE:-}" || -z "${KANBOARD_USER:-}" || -z "${KANBOARD_TOKEN:-}" ]]; then
    # shellcheck disable=SC1090
    source "${HOME}/.config/clawd/orchestrator.env" >/dev/null 2>&1 || true
  fi
fi

# Best-effort task context fetch; do not fail worker spawn if Kanboard is down.
KB_TITLE=""
KB_DESC=""
if [[ -n "${KANBOARD_BASE:-}" && -n "${KANBOARD_USER:-}" && -n "${KANBOARD_TOKEN:-}" ]]; then
  kb_json="$(python3 - <<'PY' "$TASK_ID" 2>>"$LOG_PATH" || true
import base64, json, os, sys, urllib.request

task_id = int(sys.argv[1])
base = os.environ.get("KANBOARD_BASE") or ""
user = os.environ.get("KANBOARD_USER") or ""
token = os.environ.get("KANBOARD_TOKEN") or ""
if not base or not user or not token:
    raise SystemExit(0)

payload = {"jsonrpc": "2.0", "method": "getTask", "id": 1, "params": {"task_id": task_id}}
auth = base64.b64encode(f"{user}:{token}".encode()).decode()
req = urllib.request.Request(
    base,
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
)
with urllib.request.urlopen(req, timeout=10) as resp:
    out = json.loads(resp.read().decode())
res = out.get("result") or {}
print(json.dumps({"title": res.get("title") or "", "description": res.get("description") or ""}))
PY
)"
  if [[ -n "$kb_json" ]]; then
    KB_TITLE="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get(\"title\", \"\"))' <<<"$kb_json" 2>/dev/null || true)"
    KB_DESC="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get(\"description\", \"\"))' <<<"$kb_json" 2>/dev/null || true)"
  fi
fi

KB_TITLE_B64="$(printf '%s' "$KB_TITLE" | python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())' 2>/dev/null || true)"
KB_DESC_B64="$(printf '%s' "$KB_DESC" | python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())' 2>/dev/null || true)"

export TASK_ID REPO_KEY REPO_PATH LOG_PATH PID_PATH PATCH_PATH COMMENT_PATH KB_TITLE_B64 KB_DESC_B64

PROMPT="$(python3 - <<'PY'
import base64
import os

def b64(s: str) -> str:
    if not s:
        return ""
    try:
        return base64.b64decode(s.encode("ascii")).decode("utf-8", "replace")
    except Exception:
        return ""

task_id = os.environ.get("TASK_ID", "")
repo_key = os.environ.get("REPO_KEY", "")
repo_path = os.environ.get("REPO_PATH", "")
patch_path = os.environ.get("PATCH_PATH", "")
comment_path = os.environ.get("COMMENT_PATH", "")
title = b64(os.environ.get("KB_TITLE_B64", ""))
desc = b64(os.environ.get("KB_DESC_B64", ""))

prompt = f"""You are the RecallDeck worker for Kanboard task #{task_id}.

Task context (already fetched for you; do NOT attempt to log into Kanboard UI):
Title: {title}
Description:
{desc}

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

Work in the repo at: {repo_path}
Repo key: {repo_key}

Steps:
1) Use the task context above (title/description). Do NOT try to log into Kanboard.
2) Implement the work in this repo clone.
3) Commit changes with a clear message and push if origin is configured.

4) Export a patch to this exact path:
   {patch_path}
   - If you created a commit, prefer:
     git format-patch -1 HEAD --stdout > {patch_path}
   - Otherwise:
     git diff > {patch_path}

   Then print EXACTLY this line (so automation can detect completion):
   Patch file: `{patch_path}`

5) Write a ready-to-paste Kanboard comment to:
   {comment_path}
   Mention that file path in your output.
   Then STOP (no extra chatter).

6) If you cannot commit/push in this environment, still produce the patch + comment file.
"""

print(prompt)
PY
)"

rm -f "$PID_PATH"

nohup "$CODEX_BIN" exec \
  --dangerously-bypass-approvals-and-sandbox \
  --skip-git-repo-check \
  --profile "${CODEX_PROFILE:-chigh}" \
  -C "$REPO_PATH" \
  "$PROMPT" \
  >>"$LOG_PATH" 2>&1 &

PID=$!
echo "$PID" >"$PID_PATH"
wait "$PID" || true
echo "[worker $TASK_ID] done" >>"$LOG_PATH" 2>&1 || true

# Keep the tmux window open for inspection.
exec bash
EOF

chmod +x "$RUN_PATH"

if ! "$TMUX_BIN" has-session -t "$TMUX_SESSION" 2>/dev/null; then
  "$TMUX_BIN" new-session -d -s "$TMUX_SESSION" -n orchestrator "bash"
fi

# Prevent stale PID reads before the new tmux window has a chance to overwrite it.
rm -f "$PID_PATH" 2>/dev/null || true

# Deduplicate by name (tmux allows duplicate window names).
"$TMUX_BIN" list-windows -t "$TMUX_SESSION" -F '#{window_id}:#{window_name}' 2>/dev/null \
  | awk -F: -v n="$TMUX_WINDOW" '$2 == n { print $1 }' \
  | while IFS= read -r wid; do
      [[ -n "$wid" ]] || continue
      "$TMUX_BIN" kill-window -t "$wid" 2>/dev/null || true
    done

cmd="$(printf '%q ' "$RUN_PATH" "$TASK_ID" "$REPO_KEY" "$REPO_PATH" "$LOG_PATH" "$PID_PATH" "$PATCH_PATH" "$COMMENT_PATH" "$CODEX_BIN")"
"$TMUX_BIN" new-window -t "$TMUX_SESSION" -n "$TMUX_WINDOW" "$cmd"

handle="tmux:${TMUX_SESSION}:${TMUX_WINDOW}"
pid=""
# The lease system needs a pid at spawn-time; wait briefly for the tmux window to write it.
wait_ms="${CLAWD_WORKER_PID_WAIT_MS:-1200}"
step_ms=25
elapsed=0
while [[ $elapsed -lt $wait_ms ]]; do
  if [[ -f "$PID_PATH" ]]; then
    pid="$(cat "$PID_PATH" 2>/dev/null || true)"
    break
  fi
  sleep 0.025
  elapsed=$((elapsed + step_ms))
done
if [[ "$pid" =~ ^[0-9]+$ ]]; then
  handle="pid:${pid} ${handle}"
fi

printf '{"execSessionId":"%s","logPath":"%s"}\n' "$handle" "$LOG_PATH"
