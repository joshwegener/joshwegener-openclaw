#!/usr/bin/env bash
set -euo pipefail

# Spawn a Codex docs worker inside tmux so you can watch live output.
#
# Orchestrator spawn contract:
# - MUST print one JSON object to stdout:
#   {"execSessionId":"...","logPath":"...","runId":"...","runDir":"...","donePath":"...","patchPath":"...","commentPath":"...","startedAtMs":<int>}
#
# Design notes:
# - Each spawn creates a NEW per-run directory containing docs.log + patch + comment + done.json.
# - The orchestrator should treat done.json as the canonical completion signal.

TASK_ID="${1:?task_id}"
SOURCE_REPO_KEY="${2:-}"
SOURCE_REPO_PATH="${3:-}"
SOURCE_PATCH_PATH="${4:-}" # may be empty

# Defensively strip accidental wrapping quotes if the caller already shell-escaped args.
for v in SOURCE_REPO_PATH SOURCE_PATCH_PATH; do
  val="${!v}"
  if [[ "$val" == \"*\" && "$val" == *\" ]]; then
    val="${val#\"}"
    val="${val%\"}"
  fi
  if [[ "$val" == \'*\' && "$val" == *\' ]]; then
    val="${val#\'}"
    val="${val%\'}"
  fi
  printf -v "$v" '%s' "$val"
done

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
TMUX_WINDOW="docs-${TASK_ID}"

# Prefer a stable env file path over $HOME because tmux may not propagate HOME.
DEFAULT_ENV_FILE=""
if [[ -f "/Users/joshwegener/.config/clawd/orchestrator.env" ]]; then
  DEFAULT_ENV_FILE="/Users/joshwegener/.config/clawd/orchestrator.env"
elif [[ -f "${HOME:-}/.config/clawd/orchestrator.env" ]]; then
  DEFAULT_ENV_FILE="${HOME:-}/.config/clawd/orchestrator.env"
fi
ORCHESTRATOR_ENV_FILE="${CLAWD_ORCHESTRATOR_ENV_FILE:-${CLAWD_ENV_FILE:-$DEFAULT_ENV_FILE}}"

RECALLDECK_REPO_ROOT="${RECALLDECK_REPO_ROOT:-/Users/joshwegener/Projects/RecallDeck}"
DOCS_REPO_PATH="${RECALLDECK_DOCS_REPO:-}"
if [[ -z "$DOCS_REPO_PATH" ]]; then
  for cand in \
    "${RECALLDECK_REPO_ROOT}/RecallDeck-Docs" \
    "${RECALLDECK_REPO_ROOT}/recalldeck-docs" \
    "${RECALLDECK_REPO_ROOT}/docs" \
    "/Users/joshwegener/Projects/RecallDeck/RecallDeck-Docs"; do
    if [[ -d "$cand" ]]; then
      DOCS_REPO_PATH="$cand"
      break
    fi
  done
fi
if [[ -z "$DOCS_REPO_PATH" || ! -d "$DOCS_REPO_PATH" ]]; then
  echo "RecallDeck-Docs repo not found. Set RECALLDECK_DOCS_REPO or RECALLDECK_REPO_ROOT." >&2
  exit 1
fi

RUN_ROOT="${CLAWD_RUNS_ROOT:-/Users/joshwegener/clawd/runs}"
DOCS_RUN_ROOT="${CLAWD_DOCS_RUN_ROOT:-$RUN_ROOT/docs}"

RUN_ID="$(python3 - <<'PY'
import secrets, time
print(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3))
PY
)"
RUN_DIR="${DOCS_RUN_ROOT}/task-${TASK_ID}/${RUN_ID}"
mkdir -p "$RUN_DIR"

LOG_PATH="${RUN_DIR}/docs.log"
PATCH_PATH="${RUN_DIR}/patch.patch"
COMMENT_PATH="${RUN_DIR}/kanboard-comment.md"
META_PATH="${RUN_DIR}/meta.json"
DONE_PATH="${RUN_DIR}/done.json"
RUN_SCRIPT="${RUN_DIR}/run.sh"

cat >"$RUN_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

TASK_ID="${1:?task_id}"
DOCS_REPO_PATH="${2:?docs_repo_path}"
SOURCE_REPO_KEY="${3:-}"
SOURCE_REPO_PATH="${4:-}"
SOURCE_PATCH_PATH="${5:-}"
CODEX_BIN="${6:?codex_bin}"
RUN_ID="${7:?run_id}"
RUN_DIR="${8:?run_dir}"
LOG_PATH="${9:?log_path}"
PATCH_PATH="${10:?patch_path}"
COMMENT_PATH="${11:?comment_path}"
META_PATH="${12:?meta_path}"
DONE_PATH="${13:?done_path}"

mkdir -p "$(dirname "$LOG_PATH")"

started_at_ms="$(python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
)"

export TASK_ID DOCS_REPO_PATH SOURCE_REPO_KEY SOURCE_REPO_PATH SOURCE_PATCH_PATH RUN_ID RUN_DIR LOG_PATH PATCH_PATH COMMENT_PATH META_PATH DONE_PATH STARTED_AT_MS="$started_at_ms"

python3 - <<'PY'
import json, os
payload = {
  "schemaVersion": 1,
  "taskId": int(os.environ.get("TASK_ID") or 0),
  "runId": os.environ.get("RUN_ID") or "",
  "runDir": os.environ.get("RUN_DIR") or "",
  "logPath": os.environ.get("LOG_PATH") or "",
  "docsRepoPath": os.environ.get("DOCS_REPO_PATH") or "",
  "sourceRepoKey": os.environ.get("SOURCE_REPO_KEY") or "",
  "sourceRepoPath": os.environ.get("SOURCE_REPO_PATH") or "",
  "sourcePatchPath": os.environ.get("SOURCE_PATCH_PATH") or "",
  "patchPath": os.environ.get("PATCH_PATH") or "",
  "commentPath": os.environ.get("COMMENT_PATH") or "",
  "donePath": os.environ.get("DONE_PATH") or "",
  "startedAtMs": int(os.environ.get("STARTED_AT_MS") or 0),
}
with open(os.environ["META_PATH"], "w") as f:
  json.dump(payload, f, indent=2, sort_keys=True)
PY

# Load the orchestrator env file (best-effort) so PATH/CODEX_BIN/KANBOARD_* are available
# inside the tmux window. tmux does not reliably inherit the client process environment.
env_loaded_from=""
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

{
  echo "[kanboard-env] HOME=${HOME:-}"
  echo "[kanboard-env] loaded_from=${env_loaded_from:-}"
  echo "[kanboard-env] CLAWD_ORCHESTRATOR_ENV_FILE=${CLAWD_ORCHESTRATOR_ENV_FILE:-}"
  echo "[kanboard-env] KANBOARD_BASE=${KANBOARD_BASE:-}"
  echo "[kanboard-env] KANBOARD_USER=${KANBOARD_USER:-}"
  echo "[kanboard-env] KANBOARD_TOKEN_set=$([[ -n \"${KANBOARD_TOKEN:-}\" ]] && echo yes || echo no)"
} >>"$LOG_PATH" 2>&1 || true

# Best-effort task context fetch; do not fail docs run if Kanboard is down.
KB_TITLE=""
KB_DESC=""
if [[ -n "${KANBOARD_BASE:-}" && -n "${KANBOARD_USER:-}" && -n "${KANBOARD_TOKEN:-}" ]]; then
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
    snippet = body[:200].replace("\\n", "\\\\n") if body else ""
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
      KB_TITLE="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("title",""))' <<<"$kb_json" 2>>"$LOG_PATH" || true)"
      KB_DESC="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("description",""))' <<<"$kb_json" 2>>"$LOG_PATH" || true)"
      if [[ -n "$KB_TITLE" || -n "$KB_DESC" ]]; then
        break
      fi
    fi
    sleep 0.5
  done

  if [[ -z "$KB_TITLE" && -z "$KB_DESC" ]]; then
    KB_TITLE="(Kanboard task context unavailable)"
    KB_DESC="$(printf 'Error fetching task #%s from Kanboard JSON-RPC (getTask): empty title/description after retries\\n' \"$TASK_ID\")"
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
docs_repo_path = os.environ.get("DOCS_REPO_PATH", "")
source_repo_key = os.environ.get("SOURCE_REPO_KEY", "")
source_repo_path = os.environ.get("SOURCE_REPO_PATH", "")
source_patch_path = os.environ.get("SOURCE_PATCH_PATH", "")
patch_path = os.environ.get("PATCH_PATH", "")
comment_path = os.environ.get("COMMENT_PATH", "")
run_id = os.environ.get("RUN_ID", "")
run_dir = os.environ.get("RUN_DIR", "")
title = b64(os.environ.get("KB_TITLE_B64", ""))
desc = b64(os.environ.get("KB_DESC_B64", ""))

prompt = f"""You are the RecallDeck docs worker for Kanboard task #{task_id}.

Task context (already fetched for you; do NOT attempt to log into Kanboard UI):
Title: {title}
Description:
{desc}

Docs repo (where you will make changes):
- Path: {docs_repo_path}

Source repo (where the change happened):
- Repo key: {source_repo_key}
- Repo path: {source_repo_path}
- Patch path from the code worker (may be empty): {source_patch_path}

Run metadata:
- run_id: {run_id}
- run_dir: {run_dir}

HARD SAFETY RULES (must follow):
- Do not search for secrets/keys/tokens or paste private content into logs/comments.
- Only read/edit code relevant to the task inside the docs repo.

Instructions:
1) Use the task context above. Do NOT try to log into Kanboard UI.
2) If source_patch_path is non-empty and exists, read it to understand what changed.
3) Update RecallDeck-Docs appropriately for the change.
4) Commit changes with a clear message and push if origin is configured (docs repo).
5) Export a patch to this exact path:
   {patch_path}
   - If you created a commit, prefer:
     git format-patch -1 HEAD --stdout > {patch_path}
   - Otherwise:
     git diff > {patch_path}

   Note: an empty patch means "docs not needed" for this task; still create the file.

6) Write a ready-to-paste Kanboard comment to this exact path:
   {comment_path}
   - Summarize the docs change (or why docs were skipped).
   - Include the docs repo commit hash if you made one.

7) At the end, print EXACTLY these two lines (for human debugging):
   Patch file: `{patch_path}`
   Kanboard comment file: `{comment_path}`
"""

print(prompt)
PY
)"

echo "### DOCS START $(date -u '+%Y-%m-%dT%H:%M:%SZ')" | tee -a "$LOG_PATH"
echo "run_id=$RUN_ID" | tee -a "$LOG_PATH"
echo "docs_repo_path=$DOCS_REPO_PATH" | tee -a "$LOG_PATH"
echo "source_repo_path=$SOURCE_REPO_PATH" | tee -a "$LOG_PATH"
echo "source_patch_path=$SOURCE_PATCH_PATH" | tee -a "$LOG_PATH"

set +e
"$CODEX_BIN" exec \
  --dangerously-bypass-approvals-and-sandbox \
  --skip-git-repo-check \
  --profile "${CODEX_PROFILE:-chigh}" \
  -C "$DOCS_REPO_PATH" \
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
  "runDir": os.environ.get("RUN_DIR") or "",
  "startedAtMs": int(os.environ.get("STARTED_AT_MS") or 0),
  "finishedAtMs": int(os.environ.get("FINISHED_AT_MS") or 0),
  "exitCode": int(os.environ.get("CODEX_EXIT") or 0),
  "ok": (int(os.environ.get("CODEX_EXIT") or 0) == 0),
  "docsRepoPath": os.environ.get("DOCS_REPO_PATH") or "",
  "sourceRepoKey": os.environ.get("SOURCE_REPO_KEY") or "",
  "sourceRepoPath": os.environ.get("SOURCE_REPO_PATH") or "",
  "sourcePatchPath": os.environ.get("SOURCE_PATCH_PATH") or "",
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

echo "[docs $TASK_ID] done exit=$CODEX_EXIT patch=$PATCH_PATH comment=$COMMENT_PATH" | tee -a "$LOG_PATH"

if [[ "${CLAWD_KEEP_DOCS_WINDOW_OPEN:-0}" == "1" ]]; then
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

# Deduplicate by name: keep one active docs window per task.
"$TMUX_BIN" list-windows -t "$TMUX_SESSION" -F '#{window_id}:#{window_name}' 2>/dev/null \
  | awk -F: -v n="$TMUX_WINDOW" '$2 == n { print $1 }' \
  | while IFS= read -r wid; do
      [[ -n "$wid" ]] || continue
      "$TMUX_BIN" kill-window -t "$wid" 2>/dev/null || true
    done

cmd="$(printf '%q ' "$RUN_SCRIPT" "$TASK_ID" "$DOCS_REPO_PATH" "$SOURCE_REPO_KEY" "$SOURCE_REPO_PATH" "$SOURCE_PATCH_PATH" "$CODEX_BIN" "$RUN_ID" "$RUN_DIR" "$LOG_PATH" "$PATCH_PATH" "$COMMENT_PATH" "$META_PATH" "$DONE_PATH")"
"$TMUX_BIN" new-window -t "$TMUX_SESSION" -n "$TMUX_WINDOW" "$cmd"

started_at_ms="$(python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
)"

handle="tmux:${TMUX_SESSION}:${TMUX_WINDOW}"
printf '{"execSessionId":"%s","logPath":"%s","runId":"%s","runDir":"%s","donePath":"%s","patchPath":"%s","commentPath":"%s","startedAtMs":%s}\n' \
  "$handle" "$LOG_PATH" "$RUN_ID" "$RUN_DIR" "$DONE_PATH" "$PATCH_PATH" "$COMMENT_PATH" "$started_at_ms"
