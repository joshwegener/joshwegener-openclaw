#!/usr/bin/env bash
set -euo pipefail

# Spawn a Codex worker inside tmux so you can watch live output.
#
# Orchestrator spawn contract:
# - MUST print one JSON object to stdout:
#   {"execSessionId":"...","logPath":"...","runId":"...","runDir":"...","donePath":"...","patchPath":"...","commentPath":"...","startedAtMs":<int>}
#
# Design notes:
# - Each spawn creates a NEW per-run directory containing log/patch/comment/done.json.
# - The orchestrator should treat done.json as the canonical completion signal.

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

# Prefer a stable env file path over $HOME because tmux may not propagate HOME.
DEFAULT_ENV_FILE=""
if [[ -f "/Users/joshwegener/.config/clawd/orchestrator.env" ]]; then
  DEFAULT_ENV_FILE="/Users/joshwegener/.config/clawd/orchestrator.env"
elif [[ -f "${HOME:-}/.config/clawd/orchestrator.env" ]]; then
  DEFAULT_ENV_FILE="${HOME:-}/.config/clawd/orchestrator.env"
fi
ORCHESTRATOR_ENV_FILE="${CLAWD_ORCHESTRATOR_ENV_FILE:-${CLAWD_ENV_FILE:-$DEFAULT_ENV_FILE}}"

RUN_ROOT="${CLAWD_RUNS_ROOT:-/Users/joshwegener/clawd/runs}"
WORKER_RUN_ROOT="${CLAWD_WORKER_RUN_ROOT:-$RUN_ROOT/worker}"

RUN_ID="$(python3 - <<'PY'
import secrets, time
print(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3))
PY
)"
RUN_DIR="${WORKER_RUN_ROOT}/task-${TASK_ID}/${RUN_ID}"
mkdir -p "$RUN_DIR"

LOG_PATH="${RUN_DIR}/worker.log"
PATCH_PATH="${RUN_DIR}/patch.patch"
COMMENT_PATH="${RUN_DIR}/kanboard-comment.md"
META_PATH="${RUN_DIR}/meta.json"
DONE_PATH="${RUN_DIR}/done.json"
RUN_SCRIPT="${RUN_DIR}/run.sh"

cat >"$RUN_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

TASK_ID="${1:?task_id}"
REPO_KEY="${2:?repo_key}"
REPO_PATH="${3:?repo_path}"
CODEX_BIN="${4:?codex_bin}"
RUN_ID="${5:?run_id}"
RUN_DIR="${6:?run_dir}"
LOG_PATH="${7:?log_path}"
PATCH_PATH="${8:?patch_path}"
COMMENT_PATH="${9:?comment_path}"
META_PATH="${10:?meta_path}"
DONE_PATH="${11:?done_path}"

mkdir -p "$(dirname "$LOG_PATH")"

started_at_ms="$(python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
)"

export TASK_ID REPO_KEY REPO_PATH RUN_ID RUN_DIR LOG_PATH PATCH_PATH COMMENT_PATH META_PATH DONE_PATH STARTED_AT_MS="$started_at_ms"

python3 - <<'PY'
import json, os
payload = {
  "schemaVersion": 1,
  "taskId": int(os.environ.get("TASK_ID") or 0),
  "runId": os.environ.get("RUN_ID") or "",
  "repoKey": os.environ.get("REPO_KEY") or "",
  "repoPath": os.environ.get("REPO_PATH") or "",
  "runDir": os.environ.get("RUN_DIR") or "",
  "logPath": os.environ.get("LOG_PATH") or "",
  "patchPath": os.environ.get("PATCH_PATH") or "",
  "commentPath": os.environ.get("COMMENT_PATH") or "",
  "donePath": os.environ.get("DONE_PATH") or "",
  "startedAtMs": int(os.environ.get("STARTED_AT_MS") or 0),
}
with open(os.environ["META_PATH"], "w") as f:
  json.dump(payload, f, indent=2, sort_keys=True)
PY

# Ensure Kanboard env is present even when spawned from launchd/tmux without a full shell env.
env_loaded_from=""
if [[ -z "${KANBOARD_BASE:-}" || -z "${KANBOARD_USER:-}" || -z "${KANBOARD_TOKEN:-}" ]]; then
  for cand in "${CLAWD_ORCHESTRATOR_ENV_FILE:-}" "${CLAWD_ENV_FILE:-}" "${HOME:-}/.config/clawd/orchestrator.env" "/Users/joshwegener/.config/clawd/orchestrator.env"; do
    [[ -n "$cand" ]] || continue
    if [[ -f "$cand" ]]; then
      # shellcheck disable=SC1090
      set +u
      source "$cand" >>"$LOG_PATH" 2>&1 || true
      set -u
      env_loaded_from="$cand"
      break
    fi
  done
fi

{
  echo "[kanboard-env] HOME=${HOME:-}"
  echo "[kanboard-env] loaded_from=${env_loaded_from:-}"
  echo "[kanboard-env] CLAWD_ORCHESTRATOR_ENV_FILE=${CLAWD_ORCHESTRATOR_ENV_FILE:-}"
  echo "[kanboard-env] KANBOARD_BASE=${KANBOARD_BASE:-}"
  echo "[kanboard-env] KANBOARD_USER=${KANBOARD_USER:-}"
  echo "[kanboard-env] KANBOARD_TOKEN_set=$([[ -n \"${KANBOARD_TOKEN:-}\" ]] && echo yes || echo no)"
} >>"$LOG_PATH" 2>&1 || true

# Best-effort task context fetch; do not fail worker run if Kanboard is down.
KB_TITLE=""
KB_DESC=""
if [[ -n "${KANBOARD_BASE:-}" && -n "${KANBOARD_USER:-}" && -n "${KANBOARD_TOKEN:-}" ]]; then
  # Retry a few times: we've seen rare cases where Kanboard returns empty title/description
  # transiently (race during migrations/restarts). An empty title/description causes the
  # worker to produce no-op "missing context" patches.
  kb_json=""
  for attempt in 1 2 3; do
    kb_json="$(python3 - <<'PY' "$TASK_ID" 2>>"$LOG_PATH" || true
import base64, json, os, sys, urllib.error, urllib.request

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
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode()
except urllib.error.HTTPError as e:
    body = ""
    try:
        body = e.read().decode(errors="replace")
    except Exception:
        body = ""
    snippet = body[:200].replace("\n", "\\n") if body else ""
    print(json.dumps({"title": "", "description": "", "error": f"HTTP {e.code}: {e.reason}; body={snippet!r}"}))
    raise SystemExit(0)
except Exception as e:
    print(json.dumps({"title": "", "description": "", "error": f"{type(e).__name__}: {e}"}))
    raise SystemExit(0)

try:
    out = json.loads(raw)
except Exception:
    print(json.dumps({"title": "", "description": "", "error": f"Non-JSON response: {raw[:200]!r}"}))
    raise SystemExit(0)

if out.get("error"):
    print(json.dumps({"title": "", "description": "", "error": out.get("error")}))
    raise SystemExit(0)

res = out.get("result") or {}
print(json.dumps({"title": res.get("title") or "", "description": res.get("description") or ""}))
PY
)"
    if [[ -n "$kb_json" ]]; then
      # NOTE: capture parse errors to the worker log (do not silence), otherwise we can't diagnose
      # intermittent issues where title/description appear empty.
      KB_TITLE="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("title",""))' <<<"$kb_json" 2>>"$LOG_PATH" || true)"
      KB_DESC="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("description",""))' <<<"$kb_json" 2>>"$LOG_PATH" || true)"
      KB_ERR="$(python3 -c 'import json,sys; v=json.loads(sys.stdin.read()).get("error"); import json as _j; print("" if v in (None,"") else (v if isinstance(v,str) else _j.dumps(v)))' <<<"$kb_json" 2>>"$LOG_PATH" || true)"
      if [[ -n "$KB_TITLE" || -n "$KB_DESC" ]]; then
        break
      fi
      if [[ -n "$KB_ERR" ]]; then
        echo "[kanboard-task] fetch_error(attempt=$attempt)=$KB_ERR" >>"$LOG_PATH" 2>&1 || true
      else
        echo "[kanboard-task] empty title/desc (attempt=$attempt)" >>"$LOG_PATH" 2>&1 || true
      fi
    fi
    sleep 0.5
  done

  # If we still have no context, inject an explicit error so the model doesn't
  # confidently fabricate a task.
  if [[ -z "$KB_TITLE" && -z "$KB_DESC" ]]; then
    KB_TITLE="(Kanboard task context unavailable)"
    KB_DESC="$(printf 'Error fetching task #%s from Kanboard JSON-RPC (getTask): empty title/description after retries\n' \"$TASK_ID\")"
  fi
fi

KB_TITLE_B64="$(printf '%s' "$KB_TITLE" | python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())' 2>/dev/null || true)"
KB_DESC_B64="$(printf '%s' "$KB_DESC" | python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())' 2>/dev/null || true)"

export KB_TITLE_B64 KB_DESC_B64

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
run_id = os.environ.get("RUN_ID", "")
run_dir = os.environ.get("RUN_DIR", "")
title = b64(os.environ.get("KB_TITLE_B64", ""))
desc = b64(os.environ.get("KB_DESC_B64", ""))

prompt = f"""You are the RecallDeck worker for Kanboard task #{task_id}.

Task context (already fetched for you; do NOT attempt to log into Kanboard UI):
Title: {title}
Description:
{desc}

Work in the repo at: {repo_path}
Repo key: {repo_key}

Run metadata:
- run_id: {run_id}
- run_dir: {run_dir}

HARD SAFETY RULES (must follow):
- Do not search for secrets/keys/tokens or paste private content into logs/comments.
- Only read/edit code relevant to the task inside the repo.

Steps:
1) Use the task context above (title/description). Do NOT try to log into Kanboard UI.
2) Implement the work in this repo clone.
3) Commit changes with a clear message and push if origin is configured.

4) Export a patch to this exact path:
   {patch_path}
   - If you created a commit, prefer:
     git format-patch -1 HEAD --stdout > {patch_path}
   - Otherwise:
     git diff > {patch_path}

5) Write a ready-to-paste Kanboard comment to this exact path:
   {comment_path}

6) At the end, print EXACTLY these two lines (for human debugging):
   Patch file: `{patch_path}`
   Kanboard comment file: `{comment_path}`
"""

print(prompt)
PY
)"

echo "### WORKER START $(date -u '+%Y-%m-%dT%H:%M:%SZ')" | tee -a "$LOG_PATH"
echo "run_id=$RUN_ID" | tee -a "$LOG_PATH"
echo "repo_path=$REPO_PATH" | tee -a "$LOG_PATH"

set +e
"$CODEX_BIN" exec \
  --dangerously-bypass-approvals-and-sandbox \
  --skip-git-repo-check \
  --profile "${CODEX_PROFILE:-chigh}" \
  -C "$REPO_PATH" \
  "$PROMPT" 2>&1 | tee -a "$LOG_PATH"
CODEX_EXIT="${PIPESTATUS[0]}"
set -e

patch_exists="false"
comment_exists="false"
patch_bytes=0
comment_bytes=0
if [[ -f "$PATCH_PATH" ]]; then
  patch_exists="true"
  patch_bytes="$(wc -c <"$PATCH_PATH" | tr -d ' ')"
fi
if [[ -f "$COMMENT_PATH" ]]; then
  comment_exists="true"
  comment_bytes="$(wc -c <"$COMMENT_PATH" | tr -d ' ')"
fi

finished_at_ms="$(python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
)"

export FINISHED_AT_MS="$finished_at_ms" CODEX_EXIT="$CODEX_EXIT" PATCH_EXISTS="$patch_exists" COMMENT_EXISTS="$comment_exists" PATCH_BYTES="$patch_bytes" COMMENT_BYTES="$comment_bytes"

python3 - <<'PY'
import json, os
payload = {
  "schemaVersion": 1,
  "taskId": int(os.environ.get("TASK_ID") or 0),
  "runId": os.environ.get("RUN_ID") or "",
  "repoKey": os.environ.get("REPO_KEY") or "",
  "repoPath": os.environ.get("REPO_PATH") or "",
  "runDir": os.environ.get("RUN_DIR") or "",
  "startedAtMs": int(os.environ.get("STARTED_AT_MS") or 0),
  "finishedAtMs": int(os.environ.get("FINISHED_AT_MS") or 0),
  "exitCode": int(os.environ.get("CODEX_EXIT") or 0),
  "ok": (int(os.environ.get("CODEX_EXIT") or 0) == 0),
  "patchPath": os.environ.get("PATCH_PATH") or "",
  "commentPath": os.environ.get("COMMENT_PATH") or "",
  "patchExists": (os.environ.get("PATCH_EXISTS") == "true"),
  "commentExists": (os.environ.get("COMMENT_EXISTS") == "true"),
  "patchBytes": int(os.environ.get("PATCH_BYTES") or 0),
  "commentBytes": int(os.environ.get("COMMENT_BYTES") or 0),
}
with open(os.environ["DONE_PATH"], "w") as f:
  json.dump(payload, f, indent=2, sort_keys=True)
PY

echo "[worker $TASK_ID] done exit=$CODEX_EXIT patch=$PATCH_PATH comment=$COMMENT_PATH" | tee -a "$LOG_PATH"

if [[ "${CLAWD_KEEP_WORKER_WINDOW_OPEN:-0}" == "1" ]]; then
  exec bash
fi
exit 0
EOF

chmod +x "$RUN_SCRIPT"

if ! "$TMUX_BIN" has-session -t "$TMUX_SESSION" 2>/dev/null; then
  "$TMUX_BIN" new-session -d -s "$TMUX_SESSION" -n orchestrator "bash"
fi

# Ensure the tmux server environment has a stable pointer to the orchestrator env file.
if [[ -n "${ORCHESTRATOR_ENV_FILE:-}" ]]; then
  "$TMUX_BIN" set-environment -t "$TMUX_SESSION" "CLAWD_ORCHESTRATOR_ENV_FILE" "$ORCHESTRATOR_ENV_FILE" 2>/dev/null || true
fi

# Deduplicate by name: keep one active worker window per task.
"$TMUX_BIN" list-windows -t "$TMUX_SESSION" -F '#{window_id}:#{window_name}' 2>/dev/null \
  | awk -F: -v n="$TMUX_WINDOW" '$2 == n { print $1 }' \
  | while IFS= read -r wid; do
      [[ -n "$wid" ]] || continue
      "$TMUX_BIN" kill-window -t "$wid" 2>/dev/null || true
    done

cmd="$(printf '%q ' "$RUN_SCRIPT" "$TASK_ID" "$REPO_KEY" "$REPO_PATH" "$CODEX_BIN" "$RUN_ID" "$RUN_DIR" "$LOG_PATH" "$PATCH_PATH" "$COMMENT_PATH" "$META_PATH" "$DONE_PATH")"
"$TMUX_BIN" new-window -t "$TMUX_SESSION" -n "$TMUX_WINDOW" "$cmd"

started_at_ms="$(python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
)"

handle="tmux:${TMUX_SESSION}:${TMUX_WINDOW}"
printf '{"execSessionId":"%s","logPath":"%s","runId":"%s","runDir":"%s","donePath":"%s","patchPath":"%s","commentPath":"%s","startedAtMs":%s}\n' \
  "$handle" "$LOG_PATH" "$RUN_ID" "$RUN_DIR" "$DONE_PATH" "$PATCH_PATH" "$COMMENT_PATH" "$started_at_ms"
