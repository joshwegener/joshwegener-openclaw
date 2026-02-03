#!/usr/bin/env bash
set -euo pipefail

# Spawn a Claude reviewer inside tmux so you can watch live output.
#
# Orchestrator spawn contract:
# - MUST print one JSON object to stdout:
#   {"execSessionId":"...","logPath":"...","runId":"...","runDir":"...","resultPath":"...","startedAtMs":<int>}
#
# Design notes:
# - Each spawn creates a NEW per-run directory containing review.log + review.json.
# - The orchestrator should treat review.json as the canonical completion signal.

TASK_ID="${1:?task_id}"
REPO_KEY="${2:-}"
REPO_PATH="${3:-}"
PATCH_PATH="${4:-}"
# Back-compat: older orchestrator passed (patch_path, log_path, review_revision).
# Newer calls pass (patch_path, review_revision).
REVIEW_REVISION="${5:-}"
if [[ -n "${6:-}" ]]; then
  maybe_log="${5:-}"
  if [[ "$maybe_log" == *.log || "$maybe_log" == */review-task-*.log ]]; then
    REVIEW_REVISION="${6:-}"
  fi
fi

# Defensively strip accidental wrapping quotes if the caller already shell-escaped args.
for v in REPO_PATH PATCH_PATH; do
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

TMUX_SESSION="${CLAWD_TMUX_SESSION:-clawd}"
TMUX_WINDOW="review-${TASK_ID}"

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

# Prefer a stable env file path over $HOME because tmux may not propagate HOME.
DEFAULT_ENV_FILE=""
if [[ -f "/Users/joshwegener/.config/clawd/orchestrator.env" ]]; then
  DEFAULT_ENV_FILE="/Users/joshwegener/.config/clawd/orchestrator.env"
elif [[ -f "${HOME:-}/.config/clawd/orchestrator.env" ]]; then
  DEFAULT_ENV_FILE="${HOME:-}/.config/clawd/orchestrator.env"
fi
ORCHESTRATOR_ENV_FILE="${CLAWD_ORCHESTRATOR_ENV_FILE:-${CLAWD_ENV_FILE:-$DEFAULT_ENV_FILE}}"

RUN_ROOT="${CLAWD_RUNS_ROOT:-/Users/joshwegener/clawd/runs}"
REVIEW_RUN_ROOT="${CLAWD_REVIEW_RUN_ROOT:-$RUN_ROOT/review}"

RUN_ID="$(python3 - <<'PY'
import secrets, time
print(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3))
PY
)"
RUN_DIR="${REVIEW_RUN_ROOT}/task-${TASK_ID}/${RUN_ID}"
mkdir -p "$RUN_DIR"

LOG_PATH="${RUN_DIR}/review.log"
RESULT_PATH="${RUN_DIR}/review.json"
META_PATH="${RUN_DIR}/meta.json"
RUN_SCRIPT="${RUN_DIR}/run.sh"

cat >"$RUN_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

TASK_ID="${1:?task_id}"
REPO_KEY="${2:?repo_key}"
REPO_PATH="${3:?repo_path}"
PATCH_PATH="${4:?patch_path}"
LOG_PATH="${5:?log_path}"
RESULT_PATH="${6:?result_path}"
META_PATH="${7:?meta_path}"
REVIEW_REVISION="${8:-}"
RUN_ID="${9:?run_id}"
RUN_DIR="${10:?run_dir}"

started_at_ms="$(python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
)"

export TASK_ID REPO_KEY REPO_PATH PATCH_PATH LOG_PATH RESULT_PATH META_PATH REVIEW_REVISION RUN_ID RUN_DIR STARTED_AT_MS="$started_at_ms"

python3 - <<'PY'
import json, os
payload = {
  "schemaVersion": 1,
  "taskId": int(os.environ.get("TASK_ID") or 0),
  "runId": os.environ.get("RUN_ID") or "",
  "repoKey": os.environ.get("REPO_KEY") or "",
  "repoPath": os.environ.get("REPO_PATH") or "",
  "patchPath": os.environ.get("PATCH_PATH") or "",
  "logPath": os.environ.get("LOG_PATH") or "",
  "resultPath": os.environ.get("RESULT_PATH") or "",
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

# Best-effort task context fetch; do not fail reviewer run if Kanboard is down.
KB_TITLE=""
KB_DESC=""
if [[ -n "${KANBOARD_BASE:-}" && -n "${KANBOARD_USER:-}" && -n "${KANBOARD_TOKEN:-}" ]]; then
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
    KB_TITLE="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get(\"title\", \"\"))' <<<"$kb_json" 2>/dev/null || true)"
    KB_DESC="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get(\"description\", \"\"))' <<<"$kb_json" 2>/dev/null || true)"
    KB_ERR="$(python3 -c 'import json,sys; v=json.loads(sys.stdin.read()).get(\"error\"); import json as _j; print(\"\" if v in (None, \"\") else (v if isinstance(v,str) else _j.dumps(v)))' <<<"$kb_json" 2>/dev/null || true)"
    if [[ -z "$KB_TITLE" && -z "$KB_DESC" && -n "$KB_ERR" ]]; then
      KB_TITLE="(Kanboard task context unavailable)"
      KB_DESC="$(printf 'Error fetching task #%s from Kanboard JSON-RPC (getTask):\n%s\n' "$TASK_ID" "$KB_ERR")"
      echo "[kanboard-task] fetch_error=$KB_ERR" >>"$LOG_PATH" 2>&1 || true
    fi
  fi
fi

PASS_THRESHOLD="${BOARD_ORCHESTRATOR_REVIEW_THRESHOLD:-91}"

KB_TITLE_B64="$(printf '%s' "$KB_TITLE" | python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())' 2>/dev/null || true)"
KB_DESC_B64="$(printf '%s' "$KB_DESC" | python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())' 2>/dev/null || true)"

export PASS_THRESHOLD KB_TITLE_B64 KB_DESC_B64

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
title = b64(os.environ.get("KB_TITLE_B64", ""))
desc = b64(os.environ.get("KB_DESC_B64", ""))
try:
    pass_threshold = int(os.environ.get("PASS_THRESHOLD", "91"))
except Exception:
    pass_threshold = 91

prompt = f"""You are the automated code reviewer for RecallDeck Kanban task #{task_id}.

Context:
- Repo key: {repo_key}
- Repo path: {repo_path}
- Patch path (may be empty): {patch_path}
- Task title (from Kanboard): {title}
- Task description (from Kanboard):
{desc}

Instructions:
1) If patch_path is non-empty and exists, review the patch file contents.
2) Otherwise, review based on the Kanboard task title/description and current repo state.
3) Output STRICT JSON only. No markdown, no prose.

JSON schema (you MUST output exactly this object; no wrapper fields):
{{
  "score": <int 1-100>,
  "verdict": "PASS"|"REWORK"|"BLOCKER",
  "critical_items": ["..."],
  "notes": "short summary"
}}

Policy:
- Use a high bar. Passing threshold is {pass_threshold} (must be 90+).
- If you would PASS the change, set verdict="PASS" AND score >= {pass_threshold}.
- If score < {pass_threshold}, verdict MUST NOT be PASS.
- If there are ANY critical_items, the review MUST fail (verdict must be REWORK or BLOCKER) regardless of score.
"""

print(prompt)
PY
)"

echo "### REVIEW START $(date -u '+%Y-%m-%dT%H:%M:%SZ')" | tee -a "$LOG_PATH"
echo "run_id=$RUN_ID" | tee -a "$LOG_PATH"

args=(--repo-path "$REPO_PATH" --log-path "$LOG_PATH" --result-path "$RESULT_PATH" --model "${CLAUDE_MODEL:-opus}" --prompt "$PROMPT")
if [[ -n "$REVIEW_REVISION" ]]; then
  args+=(--revision "$REVIEW_REVISION")
fi

python3 /Users/joshwegener/clawd/scripts/run_claude_review.py "${args[@]}" 2>&1 | tee -a "$LOG_PATH"

echo "[review $TASK_ID] done result=$RESULT_PATH" | tee -a "$LOG_PATH"

if [[ "${CLAWD_KEEP_REVIEWER_WINDOW_OPEN:-0}" == "1" ]]; then
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

# Deduplicate by name: keep one active review window per task.
"$TMUX_BIN" list-windows -t "$TMUX_SESSION" -F '#{window_id}:#{window_name}' 2>/dev/null \
  | awk -F: -v n="$TMUX_WINDOW" '$2 == n { print $1 }' \
  | while IFS= read -r wid; do
      [[ -n "$wid" ]] || continue
      "$TMUX_BIN" kill-window -t "$wid" 2>/dev/null || true
    done

cmd="$(printf '%q ' "$RUN_SCRIPT" "$TASK_ID" "$REPO_KEY" "$REPO_PATH" "$PATCH_PATH" "$LOG_PATH" "$RESULT_PATH" "$META_PATH" "$REVIEW_REVISION" "$RUN_ID" "$RUN_DIR")"
"$TMUX_BIN" new-window -t "$TMUX_SESSION" -n "$TMUX_WINDOW" "$cmd"

started_at_ms="$(python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
)"

handle="tmux:${TMUX_SESSION}:${TMUX_WINDOW}"
printf '{"execSessionId":"%s","logPath":"%s","runId":"%s","runDir":"%s","resultPath":"%s","startedAtMs":%s}\n' \
  "$handle" "$LOG_PATH" "$RUN_ID" "$RUN_DIR" "$RESULT_PATH" "$started_at_ms"
