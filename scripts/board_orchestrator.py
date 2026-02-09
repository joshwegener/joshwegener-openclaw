#!/usr/bin/env python3
"""RecallDeck board orchestrator (Kanboard JSON-RPC).

Designed for cron-driven execution.

Behavior:
- Uses a state file for dry-run arming + cooldown.
- Uses a lock file to prevent overlapping runs.
- Minimal MVP: only board moves/creation; no artifact inference.

Env:
- KANBOARD_BASE (default http://localhost:8401/jsonrpc.php)
- KANBOARD_USER
- KANBOARD_TOKEN
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
import urllib.request
import urllib.error
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import fcntl
except Exception:  # pragma: no cover - platform dependent
    fcntl = None

STATE_PATH = (
    os.environ.get("BOARD_ORCHESTRATOR_STATE")
    or os.environ.get("RECALLDECK_STATE_PATH")
    or os.environ.get("STATE_PATH")
    or "/Users/joshwegener/clawd/memory/board-orchestrator-state.json"
)
LOCK_PATH = os.environ.get("BOARD_ORCHESTRATOR_LOCK", "/tmp/board-orchestrator.lock")
LOCK_STRATEGY = os.environ.get("BOARD_ORCHESTRATOR_LOCK_STRATEGY", "flock").strip().lower()
LOCK_WAIT_MS = int(os.environ.get("BOARD_ORCHESTRATOR_LOCK_WAIT_MS", "0"))

PROJECT_NAME = os.environ.get("RECALLDECK_PROJECT", "RecallDeck")
KANBOARD_BASE = os.environ.get("KANBOARD_BASE", "http://localhost:8401/jsonrpc.php")
KANBOARD_USER = os.environ.get("KANBOARD_USER")
KANBOARD_TOKEN = os.environ.get("KANBOARD_TOKEN")

WIP_LIMIT = 2
ACTION_BUDGET = 3
ACTION_BUDGET_CRITICAL = 12  # allow multi-move pause+preempt in a single run
TASK_COOLDOWN_MIN = 30
FIRST_RUN_DRYRUNS = 1  # first run only

TAG_EPIC = "epic"
TAG_HOLD = "hold"
TAG_HOLD_QUEUED_CRITICAL = "hold:queued-critical"
TAG_HOLD_DEPS = "hold:deps"
TAG_HOLD_NEEDS_REPO = "hold:needs-repo"
TAG_HOLD_MANUAL = "hold:manual"
TAG_NOAUTO = "no-auto"
TAG_STORY = "story"
TAG_EPIC_CHILD = "epic-child"
TAG_DOCS_REQUIRED = "docs-required"
TAG_DOC_PENDING = "docs:pending"
TAG_DOC_INFLIGHT = "docs:inflight"
TAG_DOC_COMPLETED = "docs:completed"
TAG_DOC_SKIP = "docs:skip"
TAG_DOC_ERROR = "docs:error"
TAG_DOC_RETRY = "docs:retry"
TAG_DOC_AUTO = "docs:auto"
TAG_CRITICAL = "critical"
TAG_PAUSED = "paused"  # generic/manual pause
TAG_PAUSED_CRITICAL = "paused:critical"
TAG_PAUSED_MISSING_WORKER = "paused:missing-worker"
TAG_PAUSED_STALE_WORKER = "paused:stale-worker"
TAG_PAUSED_DEPS = "paused:deps"
TAG_PAUSED_EXCLUSIVE = "paused:exclusive"
TAG_PAUSED_ARTIFACT = "paused:artifact"
TAG_AUTO_BLOCKED = "auto-blocked"
TAG_BLOCKED_DEPS = "blocked:deps"
TAG_BLOCKED_EXCLUSIVE = "blocked:exclusive"
TAG_BLOCKED_REPO = "blocked:repo"
TAG_BLOCKED_CONTEXT = "blocked:context"
TAG_BLOCKED_ARTIFACT = "blocked:artifact"
TAG_BLOCKED_THRASH = "blocked:thrash"
TAG_BLOCKED_AUTH = "blocked:auth"
TAG_BLOCKED_QUOTA = "blocked:quota"
TAG_NO_REPO = "no-repo"
TAG_NEEDS_REWORK = "needs-rework"  # legacy

TAG_REVIEW_AUTO = "review:auto"
TAG_REVIEW_PENDING = "review:pending"
TAG_REVIEW_INFLIGHT = "review:inflight"
TAG_REVIEW_PASS = "review:pass"
TAG_REVIEW_REWORK = "review:rework"
TAG_REVIEW_BLOCKED_WIP = "review:blocked:wip"
TAG_REVIEW_ERROR = "review:error"
TAG_REVIEW_SKIP = "review:skip"
TAG_REVIEW_RERUN = "review:rerun"
TAG_REVIEW_RETRY = "review:retry"  # alias for review:rerun (human muscle memory)

# When enabled, any active critical task freezes normal throughput until the critical reaches Done.
# This prevents the system from burning tokens on non-critical work while a critical is waiting in Review/Docs.
CRITICAL_FREEZE_ALL = os.environ.get("BOARD_ORCHESTRATOR_CRITICAL_FREEZE_ALL", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Accept both "Depends on:" and "Dependencies:" prefixes (we've seen both in task descriptions).
DEPENDS_RE = re.compile(r"^(?:depends on|dependency|dependencies)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
EXCLUSIVE_RE = re.compile(r"^exclusive\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
REPO_RE = re.compile(r"^repo\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
PATCH_MARKER_RE = re.compile(
    r"(?:patch file|patch to apply|generated patch)\s*:\s*`?([^\s`]+)`?",
    re.IGNORECASE,
)
REVIEW_RESULT_RE = re.compile(r"review[_ ]result\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

REPO_ROOT = os.environ.get("RECALLDECK_REPO_ROOT", "/Users/joshwegener/Projects/RecallDeck")
REPO_MAP_PATH = os.environ.get("BOARD_ORCHESTRATOR_REPO_MAP", "")
WORKER_LOG_DIR = os.environ.get("BOARD_ORCHESTRATOR_WORKER_LOG_DIR", "/Users/joshwegener/clawd/memory/worker-logs")
WORKER_LOG_TAIL_BYTES = int(os.environ.get("BOARD_ORCHESTRATOR_WORKER_LOG_TAIL_BYTES", "20000"))
WORKER_SPAWN_CMD = os.environ.get("BOARD_ORCHESTRATOR_WORKER_SPAWN_CMD", "")
# Cron tick safety: spawning a worker should return quickly (worker runs in the background).
WORKER_SPAWN_TIMEOUT_SEC = int(os.environ.get("BOARD_ORCHESTRATOR_WORKER_SPAWN_TIMEOUT_SEC", "2"))
DOCS_SPAWN_CMD = os.environ.get("BOARD_ORCHESTRATOR_DOCS_SPAWN_CMD", "")
# Cron tick safety: docs spawns should return quickly (docs worker runs in the background).
DOCS_SPAWN_TIMEOUT_SEC = int(os.environ.get("BOARD_ORCHESTRATOR_DOCS_SPAWN_TIMEOUT_SEC", "2"))
DOCS_WIP_LIMIT = int(os.environ.get("BOARD_ORCHESTRATOR_DOCS_WIP_LIMIT", "1"))
DOCS_RUN_TIMEOUT_MIN = int(os.environ.get("BOARD_ORCHESTRATOR_DOCS_RUN_TIMEOUT_MIN", "60"))
WORKER_LEASES_ENABLED = os.environ.get("BOARD_ORCHESTRATOR_USE_LEASES", "0").strip().lower() not in (
    "0",
    "false",
    "no",
)
WORKER_LEASE_ROOT = os.environ.get("RECALLDECK_WORKER_LEASE_ROOT", "/tmp/recalldeck-workers")
WORKER_LEASE_ARCHIVE_TTL_HOURS = int(os.environ.get("RECALLDECK_WORKER_LEASE_ARCHIVE_TTL_HOURS", "72"))
LEASE_STALE_GRACE_MS = int(os.environ.get("RECALLDECK_WORKER_LEASE_GRACE_MS", "2000"))
WORKER_LOG_STALE_MS = int(os.environ.get("BOARD_ORCHESTRATOR_WORKER_LOG_STALE_MS", "0"))
WORKER_LOG_STALE_ACTION = os.environ.get("BOARD_ORCHESTRATOR_WORKER_LOG_STALE_ACTION", "pause").strip().lower()
THRASH_WINDOW_MIN = int(os.environ.get("BOARD_ORCHESTRATOR_THRASH_WINDOW_MIN", "30"))
THRASH_MAX_RESPAWNS = int(os.environ.get("BOARD_ORCHESTRATOR_THRASH_MAX_RESPAWNS", "3"))
THRASH_PAUSE_TAG = os.environ.get("BOARD_ORCHESTRATOR_THRASH_PAUSE_TAG", "paused:thrash")
WORKER_RUN_TIMEOUT_MIN = int(os.environ.get("BOARD_ORCHESTRATOR_WORKER_RUN_TIMEOUT_MIN", "180"))
REVIEW_RUN_TIMEOUT_MIN = int(os.environ.get("BOARD_ORCHESTRATOR_REVIEW_RUN_TIMEOUT_MIN", "60"))
REVIEWER_LOG_DIR = os.environ.get(
    "BOARD_ORCHESTRATOR_REVIEWER_LOG_DIR",
    "/Users/joshwegener/clawd/memory/review-logs",
)
REVIEWER_LOG_TAIL_BYTES = int(os.environ.get("BOARD_ORCHESTRATOR_REVIEWER_LOG_TAIL_BYTES", "20000"))
REVIEWER_SPAWN_CMD = os.environ.get("BOARD_ORCHESTRATOR_REVIEWER_SPAWN_CMD", "")
# Cron tick safety: reviewer spawns should return quickly (reviewer runs in the background).
REVIEWER_SPAWN_TIMEOUT_SEC = int(os.environ.get("BOARD_ORCHESTRATOR_REVIEWER_SPAWN_TIMEOUT_SEC", "2"))
TMUX_SESSION = os.environ.get("CLAWD_TMUX_SESSION", "clawd")
TMUX_CLEANUP_WINDOWS = os.environ.get("CLAWD_CLEANUP_TMUX_WINDOWS", "1").strip().lower() not in ("0", "false", "no")
# Reviewer run directories (for recovery after orchestrator restarts).
CLAWD_HOME = os.environ.get("CLAWD_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAWD_RUNS_ROOT = os.environ.get("CLAWD_RUNS_ROOT") or os.path.join(CLAWD_HOME, "runs")
CLAWD_REVIEW_RUN_ROOT = os.environ.get("CLAWD_REVIEW_RUN_ROOT") or os.path.join(CLAWD_RUNS_ROOT, "review")
# Review PASS threshold (score must be >= this integer).
REVIEW_THRESHOLD = int(os.environ.get("BOARD_ORCHESTRATOR_REVIEW_THRESHOLD", "90"))
REVIEW_AUTO_DONE = os.environ.get("BOARD_ORCHESTRATOR_REVIEW_AUTO_DONE", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
# NorthStar policy: if any critical_items are present, the review MUST fail regardless of score.
# Keep the env var for backwards compatibility, but do not allow disabling this rule in automation.
REVIEW_FAIL_ON_CRITICAL_ITEMS_ENV = os.environ.get("BOARD_ORCHESTRATOR_REVIEW_FAIL_ON_CRITICAL_ITEMS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
REVIEW_FAIL_ON_CRITICAL_ITEMS = True

MISSING_WORKER_POLICY = os.environ.get("BOARD_ORCHESTRATOR_MISSING_WORKER_POLICY", "pause").strip().lower()
ALLOW_TITLE_REPO_HINT = os.environ.get("BOARD_ORCHESTRATOR_ALLOW_TITLE_REPO_HINT", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

COL_BACKLOG = "Backlog"
COL_READY = "Ready"
COL_WIP = "Work in progress"
COL_REVIEW = "Review"
COL_DOCUMENTATION = "Documentation"
COL_BLOCKED = "Blocked"
COL_DONE = "Done"

# Optional notification hook (best-effort).
# If BOARD_ORCHESTRATOR_NOTIFY_CMD is set, the orchestrator will invoke it when
# there are actions/errors to surface. Message is passed via env var:
# - BOARD_ORCHESTRATOR_NOTIFY_MESSAGE
NOTIFY_CMD = os.environ.get("BOARD_ORCHESTRATOR_NOTIFY_CMD", "").strip()
NOTIFY_DEDUP_SECONDS = int(os.environ.get("BOARD_ORCHESTRATOR_NOTIFY_DEDUP_SECONDS", "60"))
DEBUG_RPC = os.environ.get("BOARD_ORCHESTRATOR_DEBUG_RPC", "0").strip().lower() in ("1", "true", "yes", "on")


def now_ms() -> int:
    return int(time.time() * 1000)


def make_run_id() -> str:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{ts}-p{os.getpid()}"


def critical_column_priority(column_id: int, col_wip_id: int, col_review_id: int, col_ready_id: int) -> int:
    if column_id == col_wip_id:
        return 0
    if column_id == col_review_id:
        return 1
    if column_id == col_ready_id:
        return 2
    return 3


def critical_sort_key(
    column_id: int,
    col_wip_id: int,
    col_review_id: int,
    col_ready_id: int,
    base_sort: Tuple[int, int],
) -> Tuple[int, int, int]:
    return (critical_column_priority(column_id, col_wip_id, col_review_id, col_ready_id),) + tuple(base_sort)


def pick_critical_queue(
    critical_candidates: List[Tuple[Dict[str, Any], int, int]],
    col_wip_id: int,
    col_review_id: int,
    col_ready_id: int,
    sort_key_fn: Callable[[Tuple[Any, ...]], Tuple[int, int]],
) -> Tuple[Optional[Tuple[Dict[str, Any], int, int]], List[Tuple[Dict[str, Any], int, int]]]:
    if not critical_candidates:
        return None, []
    critical_sorted = sorted(
        critical_candidates,
        key=lambda item: critical_sort_key(item[2], col_wip_id, col_review_id, col_ready_id, sort_key_fn(item)),
    )
    return critical_sorted[0], critical_sorted[1:]


def plan_pause_wip(
    wip_task_ids: List[int],
    critical_task_ids: set[int],
    paused_by_critical: Dict[str, Any],
) -> List[int]:
    pause_ids: List[int] = []
    for tid in wip_task_ids:
        if tid in critical_task_ids:
            continue
        if str(tid) in paused_by_critical:
            continue
        pause_ids.append(tid)
    return pause_ids


def sorted_paused_ids(paused_by_critical: Dict[str, Any]) -> List[int]:
    paused_ids = [int(k) for k in paused_by_critical.keys()]
    return sorted(
        paused_ids,
        key=lambda tid: (
            int(paused_by_critical.get(str(tid), {}).get("pausedAtMs", 0) or 0),
            tid,
        ),
    )


def plan_resume_from_state(
    paused_by_critical: Dict[str, Any],
    paused_task_ids: set[int],
    wip_count: int,
    wip_limit: int,
) -> Tuple[List[int], List[int], List[int]]:
    resume_to_wip: List[int] = []
    resume_to_ready: List[int] = []
    drop_ids: List[int] = []
    for tid in sorted_paused_ids(paused_by_critical):
        if tid not in paused_task_ids:
            drop_ids.append(tid)
            continue
        if wip_count < wip_limit:
            resume_to_wip.append(tid)
            wip_count += 1
        else:
            resume_to_ready.append(tid)
    return resume_to_wip, resume_to_ready, drop_ids


def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "dryRun": True,
        "dryRunRunsRemaining": FIRST_RUN_DRYRUNS,
        "lastActionsByTaskId": {},
        "swimlanePriority": ["Default swimlane"],
        # critical-mode bookkeeping (optional, self-healing)
        "critical": {},
        "pausedByCritical": {},
        # repo mapping + auto-block bookkeeping (optional, self-healing)
        "repoMap": {},
        "repoByTaskId": {},
        "autoBlockedByOrchestrator": {},
        # provider health/backoff bookkeeping (optional, self-healing)
        "providerHealth": {},
        # review automation bookkeeping (optional, self-healing)
        "reviewersByTaskId": {},
        "reviewResultsByTaskId": {},
        "reviewReworkHistoryByTaskId": {},
        # docs automation bookkeeping (optional, self-healing)
        "docsWorkersByTaskId": {},
        "docsSpawnFailuresByTaskId": {},
    }


def save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def _notify_digest(message: str) -> str:
    try:
        return hashlib.sha256(message.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def maybe_notify(state: Dict[str, Any], *, actions: List[str], errors: List[str]) -> None:
    """Best-effort notification for humans.

    Guardrails:
    - Only fires when there are actions or errors.
    - Dedupes repeats for a short window to avoid spam.
    - Never raises (orchestrator must keep running).
    """
    if not NOTIFY_CMD:
        return
    if not actions and not errors:
        return

    msg_lines: List[str] = []
    if errors:
        msg_lines.append("RecallDeck board: errors")
        msg_lines.extend(f"- {e}" for e in errors[:10])
    if actions:
        if msg_lines:
            msg_lines.append("")
        msg_lines.append("RecallDeck board: actions")
        msg_lines.extend(f"- {a}" for a in actions[:12])
        extra = max(0, len(actions) - 12)
        if extra:
            msg_lines.append(f"- …and {extra} more")

    message = "\n".join(msg_lines).strip()
    if not message:
        return

    now_s = int(time.time())
    dedup = state.get("notify") if isinstance(state.get("notify"), dict) else {}
    last_digest = str(dedup.get("lastDigest") or "")
    last_at_s = 0
    try:
        last_at_s = int(dedup.get("lastAtS") or 0)
    except Exception:
        last_at_s = 0

    digest = _notify_digest(message)
    if digest and digest == last_digest and last_at_s and (now_s - last_at_s) < NOTIFY_DEDUP_SECONDS:
        return

    env = dict(os.environ)
    env["BOARD_ORCHESTRATOR_NOTIFY_MESSAGE"] = message
    try:
        subprocess.run(
            NOTIFY_CMD,
            shell=True,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=10,
        )
    except Exception:
        pass

    try:
        state["notify"] = {"lastDigest": digest, "lastAtS": now_s}
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Provider preflight + exponential backoff (anti-thrash)
# -----------------------------------------------------------------------------

PREFLIGHT_ENABLED = os.environ.get("BOARD_ORCHESTRATOR_PREFLIGHT_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
PREFLIGHT_TIMEOUT_SEC = int(os.environ.get("BOARD_ORCHESTRATOR_PREFLIGHT_TIMEOUT_SEC", "4"))
PREFLIGHT_OK_TTL_SEC = int(os.environ.get("BOARD_ORCHESTRATOR_PREFLIGHT_OK_TTL_SEC", "300"))
PREFLIGHT_BACKOFF_BASE_SEC = int(os.environ.get("BOARD_ORCHESTRATOR_PREFLIGHT_BACKOFF_BASE_SEC", "60"))
PREFLIGHT_BACKOFF_MAX_SEC = int(os.environ.get("BOARD_ORCHESTRATOR_PREFLIGHT_BACKOFF_MAX_SEC", "3600"))
# Add a small jitter to avoid herd effects if multiple cron jobs run at once.
PREFLIGHT_BACKOFF_JITTER_PCT = float(os.environ.get("BOARD_ORCHESTRATOR_PREFLIGHT_BACKOFF_JITTER_PCT", "0.10"))


def _classify_provider_failure(output: str) -> str:
    """Return 'quota' or 'auth' or 'unknown' based on common CLI error strings."""
    s = (output or "").lower()
    if any(x in s for x in ("insufficient_quota", "rate limit", "rate_limit", "quota", "too many requests", "http 429")):
        return "quota"
    if any(
        x in s
        for x in (
            "not logged",
            "login required",
            "unauthorized",
            "forbidden",
            "expired token",
            "invalid api key",
            "no api key",
            "http 401",
            "http 403",
        )
    ):
        return "auth"
    return "unknown"


def _preflight_override(kind: str) -> str:
    env_key = f"BOARD_ORCHESTRATOR_PREFLIGHT_{kind.upper()}_PROVIDER"
    return str(os.environ.get(env_key, "auto") or "auto").strip().lower()


def infer_preflight_provider(kind: str, spawn_cmd: str) -> str:
    """Infer which provider a spawn command depends on.

    We keep this heuristic intentionally conservative so unit tests using stub/echo
    spawn commands don't depend on local Codex/Claude installs.

    Override via:
      BOARD_ORCHESTRATOR_PREFLIGHT_WORKER_PROVIDER=codex|none|auto
      BOARD_ORCHESTRATOR_PREFLIGHT_DOCS_PROVIDER=codex|none|auto
      BOARD_ORCHESTRATOR_PREFLIGHT_REVIEWER_PROVIDER=claude|none|auto
    """
    override = _preflight_override(kind)
    if override and override != "auto":
        if override in ("none", "off", "disabled", "0", "false", "no"):
            return ""
        return override

    if not spawn_cmd:
        return ""
    s = spawn_cmd.lower()
    if kind in ("worker", "docs"):
        # Typical: /.../spawn_worker_* or /.../spawn_docs_* wrappers that invoke `codex exec`.
        if any(x in s for x in ("spawn_worker", "spawn_docs", "codex exec", " codex ", "/codex", "openai")):
            return "codex"
        return ""
    if kind == "reviewer":
        # Typical: /.../spawn_reviewer_* wrappers that invoke `claude` via run_claude_review.py.
        if any(x in s for x in ("spawn_reviewer", "run_claude_review.py", " claude ", "/claude", "anthropic")):
            return "claude"
        return ""
    return ""


def preflight_codex(*, timeout_sec: int) -> Dict[str, Any]:
    codex_bin = shutil.which("codex") or ""
    if not codex_bin:
        return {"ok": False, "category": "auth", "message": "codex not found in PATH"}
    try:
        out = subprocess.run(
            [codex_bin, "login", "status"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=max(1, int(timeout_sec)),
        )
    except Exception:
        return {"ok": False, "category": "unknown", "message": "codex login status failed to run"}

    raw = (out.stdout or "").strip()
    if out.returncode == 0 and "logged in" in raw.lower():
        return {"ok": True, "message": raw.splitlines()[0] if raw else "Logged in"}

    category = _classify_provider_failure(raw)
    msg = raw.splitlines()[0].strip() if raw else "codex login status failed"
    if not msg:
        msg = "codex login status failed"
    return {"ok": False, "category": category, "message": msg}


def preflight_claude(*, timeout_sec: int) -> Dict[str, Any]:
    # Quick sanity: reviewer scripts rely on a `claude` CLI.
    claude_bin = shutil.which("claude") or ""
    if not claude_bin:
        return {"ok": False, "category": "auth", "message": "claude not found in PATH"}

    script_dir = os.path.dirname(os.path.abspath(__file__))
    status_sh = os.path.join(script_dir, "claude-auth-status.sh")
    if not os.path.isfile(status_sh):
        return {"ok": False, "category": "unknown", "message": "claude-auth-status.sh missing"}

    try:
        out = subprocess.run(
            [status_sh, "simple"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=max(1, int(timeout_sec)),
        )
    except Exception:
        return {"ok": False, "category": "unknown", "message": "claude auth status check failed to run"}

    raw = (out.stdout or "").strip()
    if raw == "OK" or raw.endswith("_EXPIRING"):
        # EXPIRING is still usable; treat as OK for preflight gating.
        return {"ok": True, "message": raw}

    category = _classify_provider_failure(raw)
    if "expired" in raw.lower() or raw.endswith("_EXPIRED"):
        category = "auth"
    msg = raw.splitlines()[0].strip() if raw else "claude auth not OK"
    if not msg:
        msg = "claude auth not OK"
    return {"ok": False, "category": category, "message": msg}


def _provider_health(state: Dict[str, Any]) -> Dict[str, Any]:
    ph = state.get("providerHealth")
    if isinstance(ph, dict):
        return ph
    ph = {}
    state["providerHealth"] = ph
    return ph


def provider_preflight_gate(
    state: Dict[str, Any],
    *,
    provider: str,
    errors: List[str],
) -> Tuple[bool, Optional[str], str]:
    """Return (ok, category, message). On failure, records an exponential backoff window in state."""
    if not PREFLIGHT_ENABLED:
        return True, None, "disabled"
    provider = (provider or "").strip().lower()
    if not provider:
        return True, None, "no-provider"

    nowm = now_ms()
    now_s = int(time.time())

    ph = _provider_health(state)
    entry = ph.get(provider)
    if not isinstance(entry, dict):
        entry = {}

    try:
        blocked_until = int(entry.get("blockedUntilMs") or 0)
    except Exception:
        blocked_until = 0
    if blocked_until and nowm < blocked_until:
        cat = str(entry.get("lastErrorCategory") or "unknown")
        msg = str(entry.get("lastErrorMessage") or "provider unavailable")
        return False, cat, msg

    # Positive cache: if we checked recently and it was OK, don't re-run external checks.
    try:
        last_ok = int(entry.get("lastOkAtMs") or 0)
    except Exception:
        last_ok = 0
    if last_ok and (nowm - last_ok) < (max(1, PREFLIGHT_OK_TTL_SEC) * 1000):
        return True, None, "ok(cached)"

    # Run an actual preflight check.
    entry["lastCheckAtMs"] = nowm
    check: Dict[str, Any]
    if provider == "codex":
        check = preflight_codex(timeout_sec=PREFLIGHT_TIMEOUT_SEC)
    elif provider == "claude":
        check = preflight_claude(timeout_sec=PREFLIGHT_TIMEOUT_SEC)
    else:
        check = {"ok": False, "category": "unknown", "message": f"unknown provider {provider!r}"}

    ok = bool(check.get("ok"))
    if ok:
        entry["status"] = "ok"
        entry["lastOkAtMs"] = nowm
        entry["blockedUntilMs"] = 0
        entry["consecutiveFailures"] = 0
        entry["lastErrorCategory"] = ""
        entry["lastErrorMessage"] = ""
        # Reset outage notification dedupe.
        entry["lastNotifiedKey"] = ""
        entry["lastNotifiedAtS"] = 0
        ph[provider] = entry
        state["providerHealth"] = ph
        return True, None, str(check.get("message") or "ok")

    category = str(check.get("category") or "unknown")
    message = str(check.get("message") or "provider unavailable")

    # Soft-fail "unknown" preflight errors.
    #
    # We only want to *block* spawns when we have a clear auth/quota signal. In practice,
    # Claude/Codex CLIs can sometimes be slow to start or temporarily fail to execute
    # (PATH drift, cold start, transient CLI issues). Treating those as hard auth failures
    # causes the pipeline to deadlock in Review/WIP.
    #
    # If the provider is truly unusable, the spawned worker/reviewer will fail and we will
    # later force-block with a concrete (auth/quota) reason based on its logs.
    if category == "unknown":
        entry["status"] = "degraded"
        entry["lastCheckAtMs"] = nowm
        entry["blockedUntilMs"] = 0
        entry["lastErrorCategory"] = category
        entry["lastErrorMessage"] = message
        # Positive cache this degraded status briefly so we don't keep re-running slow checks.
        entry["lastOkAtMs"] = nowm
        entry["consecutiveFailures"] = 0
        ph[provider] = entry
        state["providerHealth"] = ph
        # Best-effort single notification for operators; do not block the pipeline.
        outage_key = f"degraded:{provider}:{message}"
        last_key = str(entry.get("lastNotifiedKey") or "")
        try:
            last_at = int(entry.get("lastNotifiedAtS") or 0)
        except Exception:
            last_at = 0
        if outage_key and outage_key != last_key and (not last_at or (now_s - last_at) > 5):
            entry["lastNotifiedKey"] = outage_key
            entry["lastNotifiedAtS"] = now_s
            errors.append(f"warn: provider {provider} preflight degraded (unknown): {message}. Proceeding.")
        return True, None, f"ok(degraded:{message})"

    try:
        fail_count = int(entry.get("consecutiveFailures") or 0)
    except Exception:
        fail_count = 0
    fail_count += 1
    entry["consecutiveFailures"] = fail_count
    entry["status"] = "blocked"
    entry["lastErrorCategory"] = category
    entry["lastErrorMessage"] = message

    base = max(1, int(PREFLIGHT_BACKOFF_BASE_SEC))
    cap = max(base, int(PREFLIGHT_BACKOFF_MAX_SEC))
    backoff = min(cap, base * (2 ** (max(0, fail_count - 1))))
    # jitter in [1-j, 1+j]
    jitter_pct = max(0.0, min(0.9, float(PREFLIGHT_BACKOFF_JITTER_PCT)))
    if jitter_pct > 0:
        # secrets.randbelow gives a stable-ish jitter without importing random.
        r = secrets.randbelow(10_000) / 10_000.0
        factor = (1.0 - jitter_pct) + (2.0 * jitter_pct * r)
        backoff = int(max(1, backoff * factor))

    entry["blockedUntilMs"] = nowm + (backoff * 1000)
    entry["lastBackoffSec"] = backoff

    # Single-notification guard: only emit once per outage "key".
    outage_key = f"{category}:{message}"
    last_key = str(entry.get("lastNotifiedKey") or "")
    try:
        last_at = int(entry.get("lastNotifiedAtS") or 0)
    except Exception:
        last_at = 0
    if outage_key and outage_key != last_key and (not last_at or (now_s - last_at) > 5):
        entry["lastNotifiedKey"] = outage_key
        entry["lastNotifiedAtS"] = now_s
        errors.append(
            f"manual-fix: provider {provider} unavailable ({category}): {message}. Backing off for {backoff}s."
        )

    ph[provider] = entry
    state["providerHealth"] = ph
    return False, category, message


def provider_force_block(
    state: Dict[str, Any],
    *,
    provider: str,
    category: str,
    message: str,
    errors: List[str],
) -> None:
    """Mark a provider as unavailable without running any external preflight checks.

    Intended for cases where a worker/reviewer already ran and emitted a clear
    auth/quota signal in its logs. This helps prevent further spawns from
    thrashing while the outage persists.
    """
    if not PREFLIGHT_ENABLED:
        return
    provider = (provider or "").strip().lower()
    if not provider:
        return
    category = (category or "unknown").strip().lower()
    message = (message or "provider unavailable").strip()

    nowm = now_ms()
    now_s = int(time.time())
    ph = _provider_health(state)
    entry = ph.get(provider)
    if not isinstance(entry, dict):
        entry = {}

    try:
        fail_count = int(entry.get("consecutiveFailures") or 0)
    except Exception:
        fail_count = 0
    fail_count += 1
    entry["consecutiveFailures"] = fail_count
    entry["status"] = "blocked"
    entry["lastErrorCategory"] = category
    entry["lastErrorMessage"] = message
    entry["lastCheckAtMs"] = nowm

    base = max(1, int(PREFLIGHT_BACKOFF_BASE_SEC))
    cap = max(base, int(PREFLIGHT_BACKOFF_MAX_SEC))
    backoff = min(cap, base * (2 ** (max(0, fail_count - 1))))
    jitter_pct = max(0.0, min(0.9, float(PREFLIGHT_BACKOFF_JITTER_PCT)))
    if jitter_pct > 0:
        r = secrets.randbelow(10_000) / 10_000.0
        factor = (1.0 - jitter_pct) + (2.0 * jitter_pct * r)
        backoff = int(max(1, backoff * factor))

    try:
        blocked_until = int(entry.get("blockedUntilMs") or 0)
    except Exception:
        blocked_until = 0
    entry["blockedUntilMs"] = max(blocked_until, nowm + (backoff * 1000))
    entry["lastBackoffSec"] = backoff

    outage_key = f"{category}:{message}"
    last_key = str(entry.get("lastNotifiedKey") or "")
    try:
        last_at = int(entry.get("lastNotifiedAtS") or 0)
    except Exception:
        last_at = 0
    if outage_key and outage_key != last_key and (not last_at or (now_s - last_at) > 5):
        entry["lastNotifiedKey"] = outage_key
        entry["lastNotifiedAtS"] = now_s
        errors.append(
            f"manual-fix: provider {provider} unavailable ({category}): {message}. Backing off for {backoff}s."
        )

    ph[provider] = entry
    state["providerHealth"] = ph


def tmux_kill_window(window_name: str) -> None:
    """Best-effort cleanup for per-task tmux windows (avoids accumulating dead panes)."""
    if not TMUX_CLEANUP_WINDOWS:
        return
    if not window_name:
        return
    tmux_bin = shutil.which("tmux") or ""
    if not tmux_bin:
        return
    target = f"{TMUX_SESSION}:{window_name}"
    try:
        subprocess.run(
            [tmux_bin, "kill-window", "-t", target],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except Exception:
        pass


def acquire_lock_legacy(run_id: str) -> Optional[Dict[str, Any]]:
    # stale after 10 minutes
    stale_ms = 10 * 60 * 1000
    deadline_ms = now_ms() + max(0, LOCK_WAIT_MS)
    while True:
        if os.path.exists(LOCK_PATH):
            try:
                with open(LOCK_PATH, "r") as f:
                    lock = json.load(f)
                if now_ms() - int(lock.get("createdAtMs", 0)) < stale_ms:
                    if LOCK_WAIT_MS <= 0 or now_ms() >= deadline_ms:
                        return None
                    time.sleep(0.05)
                    continue
            except Exception:
                # if unreadable, treat as stale
                pass
        try:
            fh = open(LOCK_PATH, "w")
            json.dump({"pid": os.getpid(), "createdAtMs": now_ms(), "runId": run_id}, fh)
            fh.flush()
            return {"fh": fh, "strategy": "legacy-stale-file"}
        except Exception:
            return None


def acquire_lock_flock(run_id: str) -> Optional[Dict[str, Any]]:
    if fcntl is None:
        return None
    deadline_ms = now_ms() + max(0, LOCK_WAIT_MS)
    while True:
        try:
            fh = open(LOCK_PATH, "a+")
        except Exception:
            return None
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            if LOCK_WAIT_MS <= 0 or now_ms() >= deadline_ms:
                return None
            time.sleep(0.05)
            continue
        except Exception:
            fh.close()
            return None
        # Write diagnostics for humans; not used for correctness.
        try:
            fh.seek(0)
            fh.truncate()
            json.dump({"pid": os.getpid(), "createdAtMs": now_ms(), "runId": run_id}, fh)
            fh.flush()
        except Exception:
            pass
        return {"fh": fh, "strategy": "flock"}


def acquire_lock(run_id: str) -> Optional[Dict[str, Any]]:
    strategy = LOCK_STRATEGY or "flock"
    if strategy == "legacy-stale-file":
        return acquire_lock_legacy(run_id)
    if strategy == "flock":
        # No implicit fallback: explicit config switch required.
        return acquire_lock_flock(run_id)
    # Unknown strategy: refuse to run (avoid implicit stale-file behavior).
    return None


def release_lock(lock: Optional[Dict[str, Any]]) -> None:
    if not lock:
        return
    strategy = lock.get("strategy")
    fh = lock.get("fh")
    try:
        if fh:
            fh.close()
    except Exception:
        pass
    if strategy == "legacy-stale-file":
        try:
            if os.path.exists(LOCK_PATH):
                os.remove(LOCK_PATH)
        except Exception:
            pass


def rpc(method: str, params: Any = None) -> Any:
    if not KANBOARD_USER or not KANBOARD_TOKEN:
        raise RuntimeError("KANBOARD_USER/KANBOARD_TOKEN not set")

    if DEBUG_RPC:
        if method in ("moveTaskPosition", "setTaskTags"):
            print(f"[rpc] {method} params={params!r}", flush=True)
        else:
            print(f"[rpc] {method}", flush=True)

    payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params is not None:
        payload["params"] = params

    auth = base64.b64encode(f"{KANBOARD_USER}:{KANBOARD_TOKEN}".encode()).decode()
    req = urllib.request.Request(
        KANBOARD_BASE,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")
        except Exception:
            body = ""
        snippet = body[:200].replace("\n", "\\n") if body else ""
        raise RuntimeError(f"Kanboard HTTP {e.code} for {method}: {e.reason}; body={snippet!r}")

    # Kanboard can emit PHP fatals as HTML; guard
    try:
        out = json.loads(raw)
    except Exception:
        raise RuntimeError(f"Non-JSON response from Kanboard: {raw[:200]}")

    if out.get("error"):
        raise RuntimeError(str(out["error"]))

    return out.get("result")


def get_project_id() -> int:
    res = rpc("getProjectByName", {"name": PROJECT_NAME})
    return int(res["id"])


def get_board(pid: int) -> List[Dict[str, Any]]:
    return rpc("getBoard", {"project_id": pid})


def get_task(task_id: int) -> Dict[str, Any]:
    return rpc("getTask", [task_id])


def get_task_tags(task_id: int) -> List[str]:
    # returns dict {tag_id: tag_name}
    res = rpc("getTaskTags", {"task_id": task_id}) or {}
    return list(res.values())


def parse_depends_on(description: str) -> List[int]:
    if not description:
        return []
    m = DEPENDS_RE.search(description)
    if not m:
        return []
    raw = m.group(1)
    ids: List[int] = []
    # allow comma- or whitespace-separated lists
    for part in re.split(r"[\s,]+", raw.strip()):
        part = part.strip()
        if not part:
            continue
        if part.startswith('#'):
            part = part[1:]
        if part.isdigit():
            ids.append(int(part))
    return ids


def parse_exclusive_keys(tags: List[str], description: str) -> List[str]:
    keys: List[str] = []
    for t in tags:
        if ':' in t:
            a, b = t.split(':', 1)
            if a.strip().lower() == 'exclusive' and b.strip():
                keys.append(b.strip().lower())
    if description:
        m = EXCLUSIVE_RE.search(description)
        if m:
            raw = m.group(1)
            for part in raw.split(','):
                k = part.strip().lower()
                if k:
                    keys.append(k)
    # dedupe
    out: List[str] = []
    seen = set()
    for k in keys:
        if k not in seen:
            out.append(k)
            seen.add(k)
    return out


def normalize_repo_key(key: str) -> str:
    k = (key or "").strip().lower()
    k = re.sub(r"[^a-z0-9]+", "-", k).strip("-")
    return k


def load_repo_map_from_file(path: str) -> Dict[str, str]:
    if not path:
        return {}
    try:
        with open(path, "r") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            out: Dict[str, str] = {}
            for k, v in raw.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    continue
                out[normalize_repo_key(k)] = os.path.expanduser(v)
            return out
    except Exception:
        return {}
    return {}


def discover_repo_map(repo_root: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not repo_root:
        return out
    root = os.path.expanduser(repo_root)
    if not os.path.isdir(root):
        return out
    try:
        entries = os.listdir(root)
    except Exception:
        return out

    for name in entries:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        full_key = normalize_repo_key(name)
        if not full_key:
            continue
        out[full_key] = path
        if full_key.startswith("recalldeck-"):
            out[full_key[len("recalldeck-") :]] = path

    # common aliases
    if "server" in out:
        out.setdefault("api", out["server"])
        out.setdefault("backend", out["server"])
    if "web" in out:
        out.setdefault("frontend", out["web"])
        out.setdefault("ui", out["web"])

    # local orchestrator repo convenience alias
    if os.path.isdir("/Users/joshwegener/clawd"):
        out.setdefault("clawd", "/Users/joshwegener/clawd")

    return out


def merge_repo_maps(*maps: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in maps:
        for k, v in (m or {}).items():
            kk = normalize_repo_key(k)
            if not kk or not isinstance(v, str):
                continue
            out[kk] = os.path.expanduser(v)
    # prune obvious non-dirs; keep if empty (caller can decide)
    pruned: Dict[str, str] = {}
    for k, p in out.items():
        if os.path.isdir(p):
            pruned[k] = p
    return pruned


def parse_repo_hint_with_source(
    tags: List[str],
    description: str,
    title: str,
    *,
    allow_title_prefix: bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    for t in tags:
        if ":" in t:
            a, b = t.split(":", 1)
            if a.strip().lower() == "repo" and b.strip():
                return b.strip(), "tag"
    if description:
        m = REPO_RE.search(description)
        if m:
            return m.group(1).strip(), "description"
    if allow_title_prefix and title:
        # Accept multi-segment prefixes like "Web/Playground:" by taking the
        # first segment as the repo hint.
        m = re.match(r"^\s*([A-Za-z0-9_/-]+)\s*:\s*", title)
        if m:
            raw = m.group(1).strip()
            hint = raw.split("/", 1)[0].strip()
            return hint, "title"
    return None, None


def parse_repo_hint(tags: List[str], description: str, title: str) -> Optional[str]:
    hint, _source = parse_repo_hint_with_source(
        tags, description, title, allow_title_prefix=ALLOW_TITLE_REPO_HINT
    )
    return hint


def resolve_repo_path(repo_hint: Optional[str], repo_map: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    if not repo_hint:
        return None, None
    hint = repo_hint.strip()

    # Direct path hint (Repo: /path/to/repo)
    if ("/" in hint or "\\" in hint) and os.path.isdir(os.path.expanduser(hint)):
        p = os.path.expanduser(hint)
        return normalize_repo_key(os.path.basename(p)), p

    key = normalize_repo_key(hint)
    if not key:
        return None, None
    path = (repo_map or {}).get(key)
    if path and os.path.isdir(path):
        return key, path
    return key, None


def worker_entry_for(task_id: int, workers_by_task: Dict[str, Any]) -> Any:
    if not workers_by_task:
        return None
    return workers_by_task.get(str(task_id)) or workers_by_task.get(task_id)


def worker_handle(entry: Any) -> Optional[str]:
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry.strip() or None
    if not isinstance(entry, dict):
        return None
    for key in ("execSessionId", "handle", "sessionId", "session_id"):
        val = entry.get(key)
        if val:
            return str(val)
    return None

PID_HANDLE_RE = re.compile(r"\bpid:(\d+)\b")


def extract_pid(handle: Optional[str]) -> Optional[int]:
    if not handle:
        return None
    m = PID_HANDLE_RE.search(str(handle))
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        # Signal 0: check existence without sending a signal.
        os.kill(pid, 0)
        return True
    except PermissionError:
        # Exists but we may not own it; treat as alive.
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return False


def worker_is_alive(handle: Optional[str]) -> bool:
    """Best-effort liveness check.

    Today we can reliably validate pid-based handles emitted by spawn_worker_codex.sh
    (e.g. 'pid:12345'). For non-pid handles (exec sessions, opaque IDs),
    treat as alive (unknown).
    """
    # tmux handles (tmux:session:window) are first-class for our pipeline because we
    # spawn workers in per-task windows and need to detect orphaned/stale runs.
    if handle and str(handle).startswith("tmux:"):
        try:
            _pfx, session, window = str(handle).split(":", 2)
        except Exception:
            session = ""
            window = ""
        if session and window:
            tmux_bin = shutil.which("tmux")
            if not tmux_bin:
                return True  # can't check; treat unknown as alive
            try:
                out = subprocess.run(
                    [tmux_bin, "list-windows", "-t", session, "-F", "#{window_name}"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=2,
                ).stdout
                names = {line.strip() for line in out.splitlines() if line.strip()}
                return window in names
            except Exception:
                return True  # can't check; treat unknown as alive

    pid = extract_pid(handle)
    if pid is None:
        return True
    return pid_alive(pid)

def reviewer_is_alive(handle: Optional[str]) -> bool:
    """Best-effort liveness check for reviewers.

    Reviewers are usually spawned in tmux windows (tmux:session:window). For
    pid-based handles we can treat dead PIDs as stale and respawn. For unknown
    handles, treat as alive (unknown).
    """
    if handle and str(handle).startswith("tmux:"):
        return worker_is_alive(handle)
    pid = extract_pid(handle)
    if pid is None:
        return True
    return pid_alive(pid)


def review_needs_rework(score: int, verdict: str, critical_items: List[str], threshold: int) -> bool:
    """Centralize the pass/fail policy so it can't drift.

    Pass requires:
    - score >= threshold
    - verdict == PASS
    - no critical items
    """
    if score < threshold:
        return True
    if str(verdict or "").upper() != "PASS":
        return True
    if critical_items:
        return True
    return False


# -----------------------------------------------------------------------------
# Worker leases + thrash guard
# -----------------------------------------------------------------------------

LEASE_SCHEMA_VERSION = 1
HISTORY_SCHEMA_VERSION = 1
LEASE_PENDING_NOTE = "awaiting worker pid"


def lease_task_dir(task_id: int) -> str:
    return os.path.join(WORKER_LEASE_ROOT, f"task-{task_id}")


def lease_dir(task_id: int) -> str:
    return os.path.join(lease_task_dir(task_id), "lease")


def lease_json_path(task_id: int) -> str:
    return os.path.join(lease_dir(task_id), "lease.json")


def lease_archive_root(task_id: int) -> str:
    return os.path.join(lease_task_dir(task_id), "archive")


def lease_history_path(task_id: int) -> str:
    return os.path.join(lease_task_dir(task_id), "history.json")


def ensure_dir(path: str) -> None:
    if not path:
        return
    os.makedirs(path, exist_ok=True)


def safe_read_json(path: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            return raw
    except Exception:
        return None
    return None


def safe_write_json(path: str, payload: Dict[str, Any]) -> None:
    if not path:
        return
    ensure_dir(os.path.dirname(path))
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)

def json_file(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    return safe_read_json(path)


def is_done_payload(payload: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(payload, dict):
        return False
    # For worker done.json and reviewer review.json we use schemaVersion=1.
    try:
        return int(payload.get("schemaVersion") or 0) == 1
    except Exception:
        return False


def worker_done_from_entry(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    done_path = entry.get("donePath") or entry.get("done_path")
    if not done_path or not os.path.isfile(str(done_path)):
        return None
    payload = json_file(str(done_path))
    if not is_done_payload(payload):
        return None
    payload = dict(payload)
    payload["donePath"] = str(done_path)
    return payload


def reviewer_result_from_entry(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    result_path = entry.get("resultPath") or entry.get("result_path")
    if not result_path or not os.path.isfile(str(result_path)):
        return None
    payload = json_file(str(result_path))
    if not isinstance(payload, dict):
        return None
    # run_claude_review writes {score, verdict, ...} with optional schemaVersion omitted.
    # Accept either.
    if "score" not in payload or "verdict" not in payload:
        return None
    out = dict(payload)
    out["resultPath"] = str(result_path)
    return out


def latest_reviewer_result_for_task(task_id: int) -> Optional[Dict[str, Any]]:
    """Best-effort recovery: find the most recent review.json for task_id.

    This allows the orchestrator to consume review results even if it restarted and
    lost the in-memory reviewer handle (reviewersByTaskId).
    """
    try:
        task_dir = os.path.join(CLAWD_REVIEW_RUN_ROOT, f"task-{task_id}")
        if not os.path.isdir(task_dir):
            return None
        best_path: Optional[str] = None
        best_mtime: float = -1.0
        with os.scandir(task_dir) as it:
            for ent in it:
                if not ent.is_dir():
                    continue
                p = os.path.join(ent.path, "review.json")
                if not os.path.isfile(p):
                    continue
                try:
                    m = os.path.getmtime(p)
                except Exception:
                    m = 0.0
                if m > best_mtime:
                    best_mtime = m
                    best_path = p
        if not best_path:
            return None
        payload = json_file(best_path)
        if not isinstance(payload, dict):
            return None
        if "score" not in payload or "verdict" not in payload:
            return None
        out = dict(payload)
        out["resultPath"] = best_path
        return out
    except Exception:
        return None


def lease_is_valid(task_id: int, lease: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(lease, dict):
        return False
    try:
        if int(lease.get("taskId") or 0) != int(task_id):
            return False
    except Exception:
        return False
    if lease.get("schemaVersion") != LEASE_SCHEMA_VERSION:
        return False
    if not lease.get("leaseId"):
        return False
    return True


def generate_lease_id() -> str:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{ts}-{secrets.token_hex(4)}"


def default_history(task_id: int) -> Dict[str, Any]:
    return {"schemaVersion": HISTORY_SCHEMA_VERSION, "taskId": task_id, "spawnAttempts": []}


def load_history(task_id: int) -> Dict[str, Any]:
    raw = safe_read_json(lease_history_path(task_id))
    if not isinstance(raw, dict):
        return default_history(task_id)
    if raw.get("schemaVersion") != HISTORY_SCHEMA_VERSION:
        return default_history(task_id)
    if not isinstance(raw.get("spawnAttempts"), list):
        raw["spawnAttempts"] = []
    return raw


def record_spawn_attempt(
    task_id: int,
    lease_id: Optional[str],
    run_id: str,
    result: str,
    reason: Optional[str] = None,
) -> None:
    history = load_history(task_id)
    entry: Dict[str, Any] = {
        "atMs": now_ms(),
        "leaseId": lease_id or "",
        "orchestratorRunId": run_id,
        "result": result,
    }
    if reason:
        entry["reason"] = reason
    history.setdefault("spawnAttempts", []).append(entry)
    safe_write_json(lease_history_path(task_id), history)


def thrash_guard_allows(task_id: int, nowm: int) -> bool:
    if THRASH_MAX_RESPAWNS <= 0 or THRASH_WINDOW_MIN <= 0:
        return True
    history = load_history(task_id)
    window_ms = THRASH_WINDOW_MIN * 60 * 1000
    count = 0
    for attempt in history.get("spawnAttempts", []):
        try:
            if attempt.get("result") != "spawned":
                continue
            at_ms = int(attempt.get("atMs") or 0)
        except Exception:
            continue
        if (nowm - at_ms) <= window_ms:
            count += 1
    return count < THRASH_MAX_RESPAWNS


def init_lease_payload(
    task_id: int,
    run_id: str,
    repo_key: str,
    repo_path: str,
    log_path: str,
    patch_path: str,
    comment_path: str,
    spawn_cmd: str,
    spawn_timeout_sec: int,
) -> Dict[str, Any]:
    nowm = now_ms()
    return {
        "schemaVersion": LEASE_SCHEMA_VERSION,
        "leaseId": generate_lease_id(),
        "taskId": task_id,
        "createdAtMs": nowm,
        "updatedAtMs": nowm,
        "orchestrator": {"runId": run_id, "pid": os.getpid()},
        "worker": {
            "kind": "codex",
            "pid": None,
            "startedAtMs": None,
            "repoKey": repo_key,
            "repoPath": repo_path,
            "logPath": log_path,
            "patchPath": patch_path,
            "commentPath": comment_path,
            "spawn": {"cmd": spawn_cmd, "timeoutSec": spawn_timeout_sec},
        },
        "liveness": {
            "lastSeenAliveAtMs": None,
            "lastCheckedAtMs": None,
            "lastVerdict": "unknown",
            "notes": "",
        },
    }


def write_lease_files(task_id: int, lease: Dict[str, Any]) -> None:
    lease["updatedAtMs"] = now_ms()
    safe_write_json(lease_json_path(task_id), lease)
    # Optional convenience PID files.
    try:
        worker_pid = lease.get("worker", {}).get("pid")
        if worker_pid:
            with open(os.path.join(lease_dir(task_id), "worker.pid"), "w") as f:
                f.write(f"{int(worker_pid)}\n")
        with open(os.path.join(lease_dir(task_id), "orchestrator.pid"), "w") as f:
            f.write(f"{os.getpid()}\n")
    except Exception:
        pass


def load_lease(task_id: int) -> Optional[Dict[str, Any]]:
    lease = safe_read_json(lease_json_path(task_id))
    return lease if lease_is_valid(task_id, lease) else None


def lease_worker_pid(lease: Optional[Dict[str, Any]]) -> Optional[int]:
    if not lease:
        return None
    worker = lease.get("worker")
    if not isinstance(worker, dict):
        worker = {}
    try:
        pid = int(worker.get("pid") or 0)
    except Exception:
        pid = 0
    return pid if pid > 0 else None


def lease_log_path(lease: Optional[Dict[str, Any]], task_id: int) -> str:
    if not lease:
        return default_worker_log_path(task_id)
    worker = lease.get("worker")
    if not isinstance(worker, dict):
        worker = {}
    log_path = worker.get("logPath")
    return str(log_path) if log_path else default_worker_log_path(task_id)


def lease_worker_entry(task_id: int, lease: Dict[str, Any]) -> Dict[str, Any]:
    worker = lease.get("worker")
    if not isinstance(worker, dict):
        worker = {}
    handle = worker.get("execSessionId") or worker.get("handle")
    pid = lease_worker_pid(lease)
    if not handle and pid:
        handle = f"pid:{pid}"
    return {
        "kind": worker.get("kind") or "codex",
        "execSessionId": handle,
        "logPath": worker.get("logPath") or default_worker_log_path(task_id),
        "patchPath": worker.get("patchPath") or default_worker_patch_path(task_id),
        "commentPath": worker.get("commentPath") or default_worker_comment_path(task_id),
        "startedAtMs": worker.get("startedAtMs"),
        "repoKey": worker.get("repoKey") or "",
        "repoPath": worker.get("repoPath") or "",
        "leaseId": lease.get("leaseId"),
    }


def evaluate_lease_liveness(task_id: int, lease: Optional[Dict[str, Any]]) -> Tuple[str, Optional[int], str]:
    if not lease:
        return "unknown", None, "missing lease metadata"
    pid = lease_worker_pid(lease)
    if not pid:
        if LEASE_STALE_GRACE_MS > 0:
            nowm = now_ms()
            for key in ("updatedAtMs", "createdAtMs"):
                try:
                    ts = int(lease.get(key) or 0)
                except Exception:
                    ts = 0
                if ts and (nowm - ts) <= LEASE_STALE_GRACE_MS:
                    return "unknown", None, LEASE_PENDING_NOTE
        return "dead", None, "missing worker pid"
    verdict = "alive" if pid_alive(pid) else "dead"
    log_path = lease_log_path(lease, task_id)
    if verdict == "alive" and WORKER_LOG_STALE_MS > 0:
        try:
            mtime_ms = int(os.path.getmtime(log_path) * 1000)
            if now_ms() - mtime_ms > WORKER_LOG_STALE_MS:
                return "unknown", pid, "worker log stale"
        except Exception:
            return "unknown", pid, "worker log mtime unavailable"
    return verdict, pid, ""


def update_lease_liveness(task_id: int, lease: Dict[str, Any], verdict: str, notes: str = "") -> None:
    liveness = lease.get("liveness") or {}
    nowm = now_ms()
    liveness["lastCheckedAtMs"] = nowm
    liveness["lastVerdict"] = verdict
    if verdict == "alive":
        liveness["lastSeenAliveAtMs"] = nowm
    if notes:
        liveness["notes"] = notes
    lease["liveness"] = liveness
    write_lease_files(task_id, lease)


def archive_lease_dir(task_id: int, lease_id: Optional[str] = None) -> Optional[str]:
    src = lease_dir(task_id)
    if not os.path.isdir(src):
        return None
    ensure_dir(lease_archive_root(task_id))
    if lease_id:
        name = lease_id
    else:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        name = f"stale-{ts}"
    dest = os.path.join(lease_archive_root(task_id), name)
    # Avoid collisions.
    if os.path.exists(dest):
        dest = f"{dest}-{secrets.token_hex(2)}"
    try:
        shutil.move(src, dest)
        return dest
    except Exception:
        return None


def acquire_lease_dir(task_id: int) -> bool:
    ensure_dir(lease_task_dir(task_id))
    try:
        os.mkdir(lease_dir(task_id))
        return True
    except FileExistsError:
        return False
    except OSError as e:
        if e.errno == errno.EEXIST:
            return False
        return False


def recover_stale_lease_dir(task_id: int) -> bool:
    """Archive a lease dir when lease.json is missing/unreadable.

    Returns True if a stale lease dir was archived.
    """
    if not os.path.isdir(lease_dir(task_id)):
        return False
    lease = load_lease(task_id)
    if lease_is_valid(task_id, lease):
        return False
    if LEASE_STALE_GRACE_MS > 0:
        try:
            mtime_ms = int(os.path.getmtime(lease_dir(task_id)) * 1000)
            if now_ms() - mtime_ms < LEASE_STALE_GRACE_MS:
                return False
        except Exception:
            pass
    archived = archive_lease_dir(task_id)
    return archived is not None


def gc_worker_leases() -> None:
    if not WORKER_LEASE_ROOT or not os.path.isdir(WORKER_LEASE_ROOT):
        return
    ttl_ms = WORKER_LEASE_ARCHIVE_TTL_HOURS * 60 * 60 * 1000
    nowm = now_ms()
    try:
        entries = os.listdir(WORKER_LEASE_ROOT)
    except Exception:
        return
    for name in entries:
        if not name.startswith("task-"):
            continue
        tdir = os.path.join(WORKER_LEASE_ROOT, name)
        if not os.path.isdir(tdir):
            continue
        lease_path = os.path.join(tdir, "lease")
        archive_root = os.path.join(tdir, "archive")
        if os.path.isdir(archive_root):
            for aname in os.listdir(archive_root):
                apath = os.path.join(archive_root, aname)
                try:
                    mtime_ms = int(os.path.getmtime(apath) * 1000)
                except Exception:
                    continue
                if nowm - mtime_ms > ttl_ms:
                    try:
                        shutil.rmtree(apath)
                    except Exception:
                        pass
        # Remove empty task dir if no active lease and no recent history.
        if os.path.isdir(lease_path):
            continue
        try:
            if os.path.isdir(archive_root) and os.listdir(archive_root):
                continue
        except Exception:
            continue
        history_path = os.path.join(tdir, "history.json")
        if os.path.isfile(history_path):
            try:
                mtime_ms = int(os.path.getmtime(history_path) * 1000)
                if nowm - mtime_ms <= ttl_ms:
                    continue
            except Exception:
                continue
        try:
            if not os.listdir(tdir):
                os.rmdir(tdir)
        except Exception:
            pass


def scan_orphan_leases(wip_ids: set[int]) -> List[str]:
    alerts: List[str] = []
    if not WORKER_LEASE_ROOT or not os.path.isdir(WORKER_LEASE_ROOT):
        return alerts
    try:
        entries = os.listdir(WORKER_LEASE_ROOT)
    except Exception:
        return alerts
    for name in entries:
        if not name.startswith("task-"):
            continue
        try:
            task_id = int(name.split("-", 1)[1])
        except Exception:
            continue
        if task_id in wip_ids:
            continue
        lease = load_lease(task_id)
        if not lease:
            continue
        verdict, worker_pid, note = evaluate_lease_liveness(task_id, lease)
        update_lease_liveness(task_id, lease, verdict, note)
        if verdict == "alive":
            alerts.append(
                f"manual-fix: task #{task_id} has live worker pid {worker_pid} but is not in WIP (orphan)."
            )
    return alerts





# -----------------------------------------------------------------------------
# Worker diagnostics (pid-based)
# -----------------------------------------------------------------------------

WORKER_LOG_TAIL_LINES = int(os.environ.get("BOARD_ORCHESTRATOR_WORKER_LOG_TAIL_LINES", "10"))
WORKER_LOG_EXPAND_LINES = int(os.environ.get("BOARD_ORCHESTRATOR_WORKER_LOG_EXPAND_LINES", "80"))

# Very small heuristic classifier. These strings are intentionally broad.
_DIAG_PATTERNS = [
    # NOTE: Avoid matching bare HTTP status codes like "401" / "403" / "429".
    # Those appear frequently in *documentation text* that workers may print while
    # reading API docs, and we do not want false "auth/quota" diagnoses.
    (
        "quota",
        re.compile(
            r"\b(quota|rate[\s_-]?limit|insufficient[_-]?quota|HTTP\s*429|429\s+too\s+many\s+requests)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "auth",
        re.compile(
            r"\b(unauthorized|forbidden|invalid api key|expired token|no api key|not logged|login required|HTTP\s*401|HTTP\s*403|401\s+unauthorized|403\s+forbidden)\b",
            re.IGNORECASE,
        ),
    ),
    ("permissions", re.compile(r"\b(permission denied|operation not permitted|EPERM|EACCES)\b", re.IGNORECASE)),
    ("git", re.compile(r"\b(fatal:|could not read from remote repository|not a git repository|merge conflict)\b", re.IGNORECASE)),
    ("network", re.compile(r"\b(network is unreachable|timed out|timeout|ENOTFOUND|ECONNRESET|EAI_AGAIN|Temporary failure in name resolution)\b", re.IGNORECASE)),
    ("tooling", re.compile(r"\b(traceback|exception|panic|segmentation fault)\b", re.IGNORECASE)),
]


def tail_lines(path: str, n: int) -> list[str]:
    if not path or not os.path.isfile(path) or n <= 0:
        return []
    try:
        raw = read_tail(path, WORKER_LOG_TAIL_BYTES)
        lines = raw.splitlines()
        return lines[-n:] if len(lines) > n else lines
    except Exception:
        return []


def diagnose_worker_failure(task_id: int, log_path: str) -> Dict[str, Any]:
    # Return a small diagnosis payload for a dead/stale worker.
    lines = tail_lines(log_path, WORKER_LOG_TAIL_LINES)
    joined = "\n".join(lines)

    category = "unknown"
    for name, pat in _DIAG_PATTERNS:
        if pat.search(joined or ""):
            category = name
            break

    expanded = False
    if category == "unknown":
        more = tail_lines(log_path, WORKER_LOG_EXPAND_LINES)
        if more and more != lines:
            expanded = True
            lines = more
            joined = "\n".join(lines)
            for name, pat in _DIAG_PATTERNS:
                if pat.search(joined or ""):
                    category = name
                    break

    suggestion = None
    if category in ("quota", "auth"):
        suggestion = "Manual intervention likely (quota/auth)."
    elif category == "permissions":
        suggestion = "Likely filesystem/permission issue; may need manual fix."
    elif category == "git":
        suggestion = "Likely git/config issue; may need manual fix."
    elif category == "network":
        suggestion = "Transient network; safe to retry once."
    elif category == "tooling":
        suggestion = "Worker/tool crashed; safe to retry once, then pause+alert."

    return {
        "taskId": task_id,
        "category": category,
        "expanded": expanded,
        "tail": (lines[-20:] if lines else []),
        "suggestion": suggestion,
    }
def default_worker_log_path(task_id: int) -> str:
    return os.path.join(WORKER_LOG_DIR, f"task-{task_id}.log")

def default_worker_patch_path(task_id: int) -> str:
    # Must match scripts/spawn_worker_codex.sh
    return os.path.join("/Users/joshwegener/clawd/tmp/worker-patches", f"task-{task_id}.patch")


def default_worker_comment_path(task_id: int) -> str:
    # Must match scripts/spawn_worker_codex.sh
    return os.path.join("/Users/joshwegener/clawd/tmp", f"kanboard-task-{task_id}-comment.md")



def default_reviewer_log_path(task_id: int) -> str:
    return os.path.join(REVIEWER_LOG_DIR, f"review-task-{task_id}.log")


def compute_patch_revision(path: Optional[str]) -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 64)
                if not chunk:
                    break
                h.update(chunk)
    except Exception:
        return None
    return h.hexdigest()


def patch_has_diff(path: Optional[str]) -> bool:
    """Return True if a git-format patch appears to contain an actual diff.

    Empty/no-op commits produced by some agents can generate a format-patch header with
    no `diff --git` sections. Treat those as non-actionable artifacts.
    """
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            raw = f.read(200_000)
        s = raw.decode(errors="ignore")
        if "diff --git " in s:
            return True
        if "\n+++ b/" in s or "\n--- a/" in s:
            return True
        return False
    except Exception:
        return False


def extract_review_revision(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("reviewRevision", "review_revision", "revision"):
        val = payload.get(key)
        if val:
            return str(val)
    return None


def review_revision_matches(current: Optional[str], recorded: Optional[str]) -> bool:
    if not current:
        return True
    if not recorded:
        return False
    return current == recorded


def read_tail(path: str, max_bytes: int) -> str:
    try:
        with open(path, "rb") as f:
            if max_bytes > 0:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(size - max_bytes, 0), os.SEEK_SET)
            raw = f.read()
        return raw.decode(errors="ignore")
    except Exception:
        return ""

def read_text(path: str, max_bytes: int = 20000) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as f:
            raw = f.read(max_bytes if max_bytes > 0 else None)
        return raw.decode(errors="ignore")
    except Exception:
        return ""


def detect_worker_completion(
    task_id: int,
    log_path: str,
    *,
    patch_path: Optional[str] = None,
    comment_path: Optional[str] = None,
    started_at_ms: Optional[int] = None,
) -> Optional[Dict[str, str]]:
    # Prefer log marker detection, but fall back to artifact detection because
    # Codex may keep logging after printing the marker, pushing it out of the tail window.
    if not log_path or not os.path.isfile(log_path):
        return None

    # Only trust completion signals when we can bind them to a specific worker run.
    # Without started_at_ms, stale log markers can cause WIP <-> Review ping-pong.
    if started_at_ms is None:
        return None

    # Defense-in-depth: only trust the log marker if the log file itself was touched
    # after the worker started (with slack for clock skew / buffered writes).
    log_mtime_ok = False
    try:
        log_mtime_ms = int(os.path.getmtime(log_path) * 1000)
        slack_ms = 60 * 1000
        log_mtime_ok = log_mtime_ms + slack_ms >= int(started_at_ms)
    except Exception:
        log_mtime_ok = False

    tail = read_tail(log_path, WORKER_LOG_TAIL_BYTES)
    if tail and log_mtime_ok:
        patch_match = PATCH_MARKER_RE.search(tail)
        if patch_match:
            p = patch_match.group(1)
            if p and os.path.isfile(p):
                return {"logPath": log_path, "patchPath": p}

    if patch_path is None:
        patch_path = default_worker_patch_path(task_id)
    if comment_path is None:
        comment_path = default_worker_comment_path(task_id)

    if not (patch_path and comment_path):
        return None
    if not (os.path.isfile(patch_path) and os.path.isfile(comment_path)):
        return None

    try:
        patch_mtime_ms = int(os.path.getmtime(patch_path) * 1000)
        comment_mtime_ms = int(os.path.getmtime(comment_path) * 1000)
    except Exception:
        return None

    if started_at_ms is not None:
        # allow 60s clock/flush slack
        slack_ms = 60 * 1000
        if patch_mtime_ms + slack_ms < started_at_ms or comment_mtime_ms + slack_ms < started_at_ms:
            return None

    return {"logPath": log_path, "patchPath": patch_path}


def archive_file(path: str, archive_dir: str, *, prefix: str) -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    ensure_dir(archive_dir)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = os.path.basename(path)
    dest = os.path.join(archive_dir, f"{prefix}-{ts}-{base}")
    if os.path.exists(dest):
        dest = f"{dest}-{secrets.token_hex(2)}"
    try:
        shutil.move(path, dest)
        return dest
    except Exception:
        return None


def reset_worker_state(task_id: int) -> None:
    """Clear worker artifacts/lease so rework doesn't immediately auto-complete.

    Called when a task is sent back to WIP from Review (rework). Without this,
    stale patch/comment artifacts and a stale lease can cause instant WIP->Review
    transitions even though no new worker ran.
    """
    # Archive patch + comment artifacts.
    patch_path = default_worker_patch_path(task_id)
    comment_path = default_worker_comment_path(task_id)
    archive_file(patch_path, "/Users/joshwegener/clawd/tmp/worker-patches/archive", prefix=f"task-{task_id}")
    archive_file(comment_path, "/Users/joshwegener/clawd/tmp/worker-comments/archive", prefix=f"task-{task_id}")

    # Archive the worker log too. Otherwise, a stale "Patch file:" marker can
    # cause immediate false completion on the next tick.
    log_path = default_worker_log_path(task_id)
    archive_file(log_path, "/Users/joshwegener/clawd/memory/worker-logs/archive", prefix=f"task-{task_id}")

    # Archive any active lease so the next WIP tick spawns a fresh worker with a new leaseId/startedAtMs.
    if WORKER_LEASES_ENABLED:
        lease = load_lease(task_id)
        lease_id = None
        if isinstance(lease, dict):
            lease_id = str(lease.get("leaseId") or "") or None
        archive_lease_dir(task_id, lease_id)


def parse_review_result(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    match = None
    for m in REVIEW_RESULT_RE.finditer(text):
        match = m
    if not match:
        return None
    raw = (match.group(1) or "").strip()
    tail = text[match.end():]
    parse_text = raw
    if tail:
        parse_text = (raw + "\n" + tail) if raw else tail.lstrip()
    if not parse_text.strip():
        return None
    if not raw:
        raw = parse_text.strip()

    def extract_review_json_from_string(s: str) -> Optional[Dict[str, Any]]:
        """Best-effort extraction of a {score, verdict, ...} object from a string.

        This is primarily to recover when the reviewer wrote a JSON envelope where
        the actual review JSON was embedded inside a text field.
        """
        if not s:
            return None
        # Unescape common sequences (Claude json output often embeds {\"score\":...}).
        s2 = s.replace('\\"', '"')
        idx = s2.find('{"score"')
        if idx < 0:
            idx = s2.find("{\"score\"")
        if idx < 0:
            return None
        # Brace scan to extract one JSON object.
        depth = 0
        end = None
        for i, ch in enumerate(s2[idx:], start=idx):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            return None
        frag = s2[idx:end]
        try:
            out = json.loads(frag)
            return out if isinstance(out, dict) else None
        except Exception:
            return None

    payload: Any = None
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
        except Exception:
            payload = None

    if payload is None:
        embedded = extract_review_json_from_string(parse_text)
        if embedded and isinstance(embedded, dict):
            payload = embedded

    # If the reviewer wrote a Claude JSON envelope (e.g. {type:'result', result:'...{score...}'})
    # attempt to recover the embedded review JSON.
    if isinstance(payload, dict) and ("score" not in payload or "verdict" not in payload):
        embedded = None
        if isinstance(payload.get("result"), str):
            embedded = extract_review_json_from_string(payload.get("result") or "")
        if embedded and isinstance(embedded, dict):
            payload = embedded

    score = None
    verdict = None
    notes = None
    critical_items: list[str] = []
    minor_items: list[str] = []
    fix_plan: list[str] = []
    review_revision = None
    if isinstance(payload, dict):
        score = payload.get("score") if score is None else score
        verdict = payload.get("verdict") if verdict is None else verdict
        notes = payload.get("notes") or payload.get("comment") or payload.get("summary")
        review_revision = extract_review_revision(payload)
        ci = (
            payload.get("critical_items")
            or payload.get("criticalItems")
            or payload.get("criticalIssues")
            or payload.get("critical")
        )
        if isinstance(ci, list):
            critical_items = [str(x) for x in ci if str(x).strip()]
        elif isinstance(ci, str):
            item = ci.strip()
            if item:
                critical_items = [item]

        mi = payload.get("minor_items") or payload.get("minorItems") or payload.get("minorIssues") or payload.get("nits")
        if isinstance(mi, list):
            minor_items = [str(x) for x in mi if str(x).strip()]

        fp = payload.get("fix_plan") or payload.get("fixPlan") or payload.get("plan") or payload.get("next_steps")
        if isinstance(fp, list):
            fix_plan = [str(x) for x in fp if str(x).strip()]

    if score is None:
        score_match = re.search(r"score\s*[:=]\s*(\d{1,3})", parse_text, re.IGNORECASE)
        if score_match:
            score = score_match.group(1)
    if verdict is None:
        verdict_match = re.search(r"verdict\s*[:=]\s*([A-Za-z]+)", parse_text, re.IGNORECASE)
        if verdict_match:
            verdict = verdict_match.group(1)

    try:
        score_int = int(score)
    except Exception:
        return None
    if score_int < 1 or score_int > 100:
        return None

    verdict_norm = str(verdict or "").strip().upper()
    if verdict_norm not in ("PASS", "REWORK", "BLOCKER"):
        return None

    result: Dict[str, Any] = {"score": score_int, "verdict": verdict_norm, "raw": raw}
    if notes:
        result["notes"] = str(notes)
    if critical_items:
        result["critical_items"] = critical_items
    if minor_items:
        result["minor_items"] = minor_items
    if fix_plan:
        result["fix_plan"] = fix_plan
    if review_revision:
        result["reviewRevision"] = review_revision
    return result


def detect_review_result(task_id: int, log_path: str) -> Optional[Dict[str, Any]]:
    if not log_path or not os.path.isfile(log_path):
        return None
    tail = read_tail(log_path, REVIEWER_LOG_TAIL_BYTES)
    if not tail:
        return None
    marker = "### REVIEW START"
    idx = tail.rfind(marker)
    if idx >= 0:
        tail = tail[idx:]
    result = parse_review_result(tail)
    if not result:
        return None
    result["logPath"] = log_path
    return result


def format_worker_spawn_cmd(task_id: int, repo_key: Optional[str], repo_path: Optional[str]) -> Tuple[str, str, str]:
    safe_repo_key = repo_key or ""
    safe_repo_path = repo_path or ""
    try:
        cmd = WORKER_SPAWN_CMD.format(
            task_id=task_id,
            repo_key=shlex.quote(safe_repo_key),
            repo_path=shlex.quote(safe_repo_path),
        )
    except Exception:
        cmd = WORKER_SPAWN_CMD
    return cmd, safe_repo_key, safe_repo_path


def spawn_worker(task_id: int, repo_key: Optional[str], repo_path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not WORKER_SPAWN_CMD:
        return None
    cmd, safe_repo_key, safe_repo_path = format_worker_spawn_cmd(task_id, repo_key, repo_path)
    try:
        out = subprocess.run(
            cmd,
            shell=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=WORKER_SPAWN_TIMEOUT_SEC,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    raw = (out.stdout or "").strip()
    if not raw:
        return None
    handle: Optional[str] = None
    log_path: Optional[str] = None
    run_id: Optional[str] = None
    run_dir: Optional[str] = None
    done_path: Optional[str] = None
    patch_path: Optional[str] = None
    comment_path: Optional[str] = None
    started_at_ms: Optional[int] = None
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                handle = payload.get("execSessionId") or payload.get("handle") or payload.get("sessionId")
                log_path = payload.get("logPath")
                run_id = payload.get("runId") or payload.get("run_id")
                run_dir = payload.get("runDir") or payload.get("run_dir")
                done_path = payload.get("donePath") or payload.get("done_path")
                patch_path = payload.get("patchPath") or payload.get("patch_path")
                comment_path = payload.get("commentPath") or payload.get("comment_path")
                try:
                    if payload.get("startedAtMs") is not None:
                        started_at_ms = int(payload.get("startedAtMs") or 0) or None
                except Exception:
                    started_at_ms = None
        except Exception:
            handle = None
    if not handle:
        handle = raw.splitlines()[-1].strip()
    if not log_path:
        log_path = default_worker_log_path(task_id)
    if not patch_path:
        patch_path = default_worker_patch_path(task_id)
    if not comment_path:
        comment_path = default_worker_comment_path(task_id)
    if not started_at_ms:
        started_at_ms = now_ms()
    return {
        "kind": "codex",
        "execSessionId": handle,
        "logPath": log_path,
        "runId": run_id or "",
        "runDir": run_dir or "",
        "donePath": done_path or "",
        "patchPath": patch_path,
        "commentPath": comment_path,
        "startedAtMs": started_at_ms,
        "repoKey": safe_repo_key,
        "repoPath": safe_repo_path,
    }


def format_docs_spawn_cmd(
    task_id: int,
    source_repo_key: Optional[str],
    source_repo_path: Optional[str],
    source_patch_path: Optional[str],
) -> Tuple[str, str, str, str]:
    safe_repo_key = source_repo_key or ""
    safe_repo_path = source_repo_path or ""
    safe_patch_path = source_patch_path or ""
    try:
        cmd = DOCS_SPAWN_CMD.format(
            task_id=task_id,
            repo_key=shlex.quote(safe_repo_key),
            repo_path=shlex.quote(safe_repo_path),
            patch_path=shlex.quote(safe_patch_path),
        )
    except Exception:
        cmd = DOCS_SPAWN_CMD
    return cmd, safe_repo_key, safe_repo_path, safe_patch_path


def spawn_docs_worker(
    task_id: int,
    source_repo_key: Optional[str],
    source_repo_path: Optional[str],
    source_patch_path: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not DOCS_SPAWN_CMD:
        return None
    cmd, safe_repo_key, safe_repo_path, safe_patch_path = format_docs_spawn_cmd(
        task_id, source_repo_key, source_repo_path, source_patch_path
    )
    try:
        out = subprocess.run(
            cmd,
            shell=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=DOCS_SPAWN_TIMEOUT_SEC,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    raw = (out.stdout or "").strip()
    if not raw:
        return None

    handle: Optional[str] = None
    log_path: Optional[str] = None
    run_id: Optional[str] = None
    run_dir: Optional[str] = None
    done_path: Optional[str] = None
    patch_path: Optional[str] = None
    comment_path: Optional[str] = None
    started_at_ms: Optional[int] = None
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                handle = payload.get("execSessionId") or payload.get("handle") or payload.get("sessionId")
                log_path = payload.get("logPath")
                run_id = payload.get("runId") or payload.get("run_id")
                run_dir = payload.get("runDir") or payload.get("run_dir")
                done_path = payload.get("donePath") or payload.get("done_path")
                patch_path = payload.get("patchPath") or payload.get("patch_path")
                comment_path = payload.get("commentPath") or payload.get("comment_path")
                try:
                    if payload.get("startedAtMs") is not None:
                        started_at_ms = int(payload.get("startedAtMs") or 0) or None
                except Exception:
                    started_at_ms = None
        except Exception:
            handle = None
    if not handle:
        handle = raw.splitlines()[-1].strip()
    if not started_at_ms:
        started_at_ms = now_ms()
    return {
        "kind": "docs",
        "execSessionId": handle,
        "logPath": log_path or "",
        "runId": run_id or "",
        "runDir": run_dir or "",
        "donePath": done_path or "",
        "patchPath": patch_path or "",
        "commentPath": comment_path or "",
        "startedAtMs": started_at_ms,
        "sourceRepoKey": safe_repo_key,
        "sourceRepoPath": safe_repo_path,
        "sourcePatchPath": safe_patch_path,
    }


def spawn_reviewer(
    task_id: int,
    repo_key: Optional[str],
    repo_path: Optional[str],
    patch_path: Optional[str],
    review_revision: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not REVIEWER_SPAWN_CMD:
        return None
    safe_repo_key = repo_key or ""
    safe_repo_path = repo_path or ""
    safe_patch_path = patch_path or ""
    safe_review_revision = review_revision or ""
    try:
        cmd = REVIEWER_SPAWN_CMD.format(
            task_id=task_id,
            repo_key=shlex.quote(safe_repo_key),
            repo_path=shlex.quote(safe_repo_path),
            patch_path=shlex.quote(safe_patch_path),
            log_path=shlex.quote(default_reviewer_log_path(task_id)),
            review_revision=shlex.quote(safe_review_revision),
        )
    except Exception:
        cmd = REVIEWER_SPAWN_CMD
    try:
        out = subprocess.run(
            cmd,
            shell=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=REVIEWER_SPAWN_TIMEOUT_SEC,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    raw = (out.stdout or "").strip()
    if not raw:
        return None
    handle: Optional[str] = None
    log_path: Optional[str] = None
    run_id: Optional[str] = None
    run_dir: Optional[str] = None
    result_path: Optional[str] = None
    started_at_ms: Optional[int] = None
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                handle = payload.get("execSessionId") or payload.get("handle") or payload.get("sessionId")
                log_path = payload.get("logPath")
                run_id = payload.get("runId") or payload.get("run_id")
                run_dir = payload.get("runDir") or payload.get("run_dir")
                result_path = payload.get("resultPath") or payload.get("result_path")
                try:
                    if payload.get("startedAtMs") is not None:
                        started_at_ms = int(payload.get("startedAtMs") or 0) or None
                except Exception:
                    started_at_ms = None
        except Exception:
            handle = None
    if not handle:
        handle = raw.splitlines()[-1].strip()
    if not log_path:
        log_path = default_reviewer_log_path(task_id)
    if not started_at_ms:
        started_at_ms = now_ms()
    return {
        "kind": "reviewer",
        "execSessionId": handle,
        "logPath": log_path,
        "runId": run_id or "",
        "runDir": run_dir or "",
        "resultPath": result_path or "",
        "startedAtMs": started_at_ms,
        "repoKey": safe_repo_key,
        "repoPath": safe_repo_path,
        "patchPath": safe_patch_path,
        "reviewRevision": safe_review_revision,
    }


def set_task_tags(pid: int, task_id: int, tags: List[str]) -> None:
    rpc("setTaskTags", [pid, task_id, tags])


def move_task(pid: int, task_id: int, column_id: int, position: int, swimlane_id: int) -> None:
    rpc(
        "moveTaskPosition",
        {
            "project_id": pid,
            "task_id": task_id,
            "column_id": column_id,
            "position": position,
            "swimlane_id": swimlane_id,
        },
    )


def create_task(
    pid: int,
    title: str,
    description: str,
    column_id: int,
    swimlane_id: Optional[int] = None,
) -> int:
    params: Dict[str, Any] = {"title": title, "project_id": pid, "column_id": column_id, "description": description}
    if swimlane_id is not None:
        params["swimlane_id"] = swimlane_id
    return int(rpc("createTask", params))


def best_swimlanes(board: List[Dict[str, Any]], priority: List[str]) -> List[Dict[str, Any]]:
    by_name = {b.get("name"): b for b in board}
    out: List[Dict[str, Any]] = []
    for name in priority:
        if name in by_name:
            out.append(by_name[name])
    # append any remaining swimlanes
    for b in board:
        if b.get("name") not in priority:
            out.append(b)
    return out


def pick_top_task(col: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tasks = col.get("tasks") or []
    if not tasks:
        return None
    # Kanboard 'position' is 1..n; smaller = higher
    return sorted(tasks, key=lambda t: int(t.get("position") or 10**9))[0]


def find_column(columns: List[Dict[str, Any]], title: str) -> Optional[Dict[str, Any]]:
    for c in columns:
        if (c.get("title") or "").strip() == title:
            return c
    return None


def task_title(t: Dict[str, Any]) -> str:
    return (t.get("title") or "").strip()


def is_held(tags: List[str]) -> bool:
    lower = {x.lower() for x in tags}
    if TAG_HOLD in lower or TAG_NOAUTO in lower or any(t.startswith("hold:") for t in lower):
        return True
    # Treat any paused tag as an explicit "do not advance/start" escape hatch.
    if TAG_PAUSED in lower:
        return True
    if any(t.startswith("paused:") for t in lower):
        return True
    # Treat "blocked:*" tags as non-actionable until manually cleared or auto-healed.
    if any(t.startswith("blocked:") for t in lower):
        return True
    return False


def is_epic(tags: List[str]) -> bool:
    return TAG_EPIC in {x.lower() for x in tags}


def is_critical(tags: List[str]) -> bool:
    return TAG_CRITICAL in {x.lower() for x in tags}

def is_hard_hold(tags: List[str]) -> bool:
    """Hard holds are human intent to stop automation.

    We intentionally *do not* treat paused/blocked tags as hard holds for purposes
    of critical selection. A critical task may be paused/blocked and should still
    freeze throughput until it is resolved.
    """
    lower = {x.lower() for x in tags}
    orchestrator_holds = {TAG_HOLD_QUEUED_CRITICAL, TAG_HOLD_DEPS, TAG_HOLD_NEEDS_REPO}
    # Legacy: some older runs incorrectly added plain `hold` alongside `hold:queued-critical`.
    # In that case, treat it as orchestrator-managed and allow selection so we can unqueue.
    if TAG_HOLD in lower:
        if TAG_HOLD_QUEUED_CRITICAL in lower:
            return False
        # Plain `hold` is ambiguous; treat as human intent (manual hold).
        return True
    if TAG_NOAUTO in lower:
        return True
    for t in lower:
        if t.startswith("hold:") and t not in orchestrator_holds:
            return True
    return False


def has_tag(tags: List[str], tag: str) -> bool:
    return tag.lower() in {x.lower() for x in tags}


def breakdown_title(epic_id: int, epic_title: str) -> str:
    return f"Break down epic #{epic_id}: {epic_title}".strip()


def find_existing_breakdown(all_tasks: List[Tuple[Dict[str, Any], int]], target_title: str) -> Optional[int]:
    # all_tasks: (task, swimlane_id)
    for t, _sw in all_tasks:
        if task_title(t) == target_title:
            return int(t.get("id"))
    return None


def emit_json(
    *,
    mode: str,
    actions: List[str],
    promoted_to_ready: List[int],
    moved_to_wip: List[int],
    created_tasks: List[int],
    errors: List[str],
) -> None:
    # If truly nothing happened, stay silent for cron.
    if not actions and not errors:
        print("NO_REPLY")
        return

    payload = {
        "mode": mode,
        "actions": actions,
        "promotedToReady": promoted_to_ready,
        "movedToWip": moved_to_wip,
        "createdTasks": created_tasks,
        "errors": errors,
    }
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def main() -> int:
    run_id = make_run_id()
    lock = acquire_lock(run_id)
    if not lock:
        print("NO_REPLY")
        return 0

    try:
        state = load_state()
        if WORKER_LEASES_ENABLED:
            try:
                gc_worker_leases()
            except Exception:
                pass

        # Repo mapping (self-healing):
        # - Merge any persisted mapping with optional JSON mapping file and
        #   auto-discovered repos under REPO_ROOT.
        existing_repo_map = state.get("repoMap") or {}
        file_repo_map = load_repo_map_from_file(REPO_MAP_PATH)
        discovered_repo_map = discover_repo_map(REPO_ROOT)
        if file_repo_map or discovered_repo_map:
            merged_repo_map = merge_repo_maps(existing_repo_map, file_repo_map, discovered_repo_map)
            if merged_repo_map:
                state["repoMap"] = merged_repo_map
        repo_map: Dict[str, str] = (state.get("repoMap") or {})
        repo_by_task: Dict[str, Any] = (state.get("repoByTaskId") or {})
        auto_blocked: Dict[str, Any] = (state.get("autoBlockedByOrchestrator") or {})
        repo_hold_commented_by_task_id: Dict[str, Any] = (state.get("repoHoldCommentedByTaskId") or {})
        if not isinstance(repo_hold_commented_by_task_id, dict):
            repo_hold_commented_by_task_id = {}
        reviewers_by_task: Dict[str, Any] = (state.get("reviewersByTaskId") or {})
        review_results_by_task: Dict[str, Any] = (state.get("reviewResultsByTaskId") or {})
        review_rework_history_by_task: Dict[str, Any] = (state.get("reviewReworkHistoryByTaskId") or {})
        reviewer_spawn_failures_by_task: Dict[str, Any] = (state.get("reviewerSpawnFailuresByTaskId") or {})
        if not isinstance(reviewer_spawn_failures_by_task, dict):
            reviewer_spawn_failures_by_task = {}
        docs_workers_by_task: Dict[str, Any] = (state.get("docsWorkersByTaskId") or {})
        if not isinstance(docs_workers_by_task, dict):
            docs_workers_by_task = {}
        docs_spawn_failures_by_task: Dict[str, Any] = (state.get("docsSpawnFailuresByTaskId") or {})
        if not isinstance(docs_spawn_failures_by_task, dict):
            docs_spawn_failures_by_task = {}
        # Track docs runs that hang (tmux window alive but no done.json) so we can
        # safely restart without infinite loops.
        docs_timeout_restarts_by_task: Dict[str, Any] = (state.get("docsTimeoutRestartsByTaskId") or {})
        if not isinstance(docs_timeout_restarts_by_task, dict):
            docs_timeout_restarts_by_task = {}

        pid = get_project_id()
        board = get_board(pid)
        swimlanes = best_swimlanes(board, state.get("swimlanePriority") or ["Default swimlane"])

        # Use first swimlane's columns as canonical for id mapping
        if not swimlanes:
            print("NO_REPLY")
            return 0

        columns = swimlanes[0].get("columns") or []
        col_backlog = find_column(columns, COL_BACKLOG)
        col_ready = find_column(columns, COL_READY)
        col_wip = find_column(columns, COL_WIP)
        col_review = find_column(columns, COL_REVIEW)
        col_docs = find_column(columns, COL_DOCUMENTATION)
        col_blocked = find_column(columns, COL_BLOCKED)
        col_done = find_column(columns, COL_DONE)

        missing = [
            name
            for name, col in [
                (COL_BACKLOG, col_backlog),
                (COL_READY, col_ready),
                (COL_WIP, col_wip),
                (COL_REVIEW, col_review),
                # Documentation is optional; if present we enforce Review -> Documentation -> Done.
                # NOTE: Paused is now tag-based; the Paused column is optional/legacy.
                (COL_BLOCKED, col_blocked),
                (COL_DONE, col_done),
            ]
            if col is None
        ]
        if missing:
            print("RecallDeck board orchestrator: missing columns: " + ", ".join(missing))
            return 0

        # Gather tasks across swimlanes
        def tasks_for_column(col_id: int) -> List[Tuple[Dict[str, Any], int]]:
            out: List[Tuple[Dict[str, Any], int]] = []
            for sl in swimlanes:
                for c in (sl.get("columns") or []):
                    if int(c.get("id")) == int(col_id):
                        for t in (c.get("tasks") or []):
                            out.append((t, int(sl.get("id") or 0)))
            return out

        done_column_id = int(col_done["id"])

        def is_done(task_id: int) -> bool:
            try:
                t = get_task(task_id)
                return int(t.get("column_id") or 0) == done_column_id
            except Exception:
                return False

        # Drift check: if a task is in WIP but we have no recorded worker handle, flag it.
        workers_by_task = (state.get("workersByTaskId") or {})

        wip_tasks = tasks_for_column(int(col_wip["id"]))
        ready_tasks = tasks_for_column(int(col_ready["id"]))
        backlog_tasks = tasks_for_column(int(col_backlog["id"]))
        review_tasks = tasks_for_column(int(col_review["id"]))
        docs_tasks: List[Tuple[Dict[str, Any], int]] = []
        if col_docs is not None:
            docs_tasks = tasks_for_column(int(col_docs["id"]))
        # Paused is now tag-based; the Paused column is optional/legacy.
        blocked_tasks = tasks_for_column(int(col_blocked["id"]))
        done_tasks = tasks_for_column(int(col_done["id"]))

        review_ids = {int(t.get("id")) for t, _sl in review_tasks}
        docs_ids = {int(t.get("id")) for t, _sl in docs_tasks}
        lease_warnings: List[str] = []
        lease_pending_ids: set[int] = set()
        stale_worker_ids: set[int] = set()

        # Prefer leases as the canonical source for active workers.
        if WORKER_LEASES_ENABLED and wip_tasks:
            for wt, _wsl_id in wip_tasks:
                tid = int(wt.get("id"))
                lease = load_lease(tid)
                if lease:
                    verdict, _worker_pid, note = evaluate_lease_liveness(tid, lease)
                    update_lease_liveness(tid, lease, verdict, note)
                    if verdict == "dead":
                        workers_by_task.pop(str(tid), None)
                        workers_by_task.pop(tid, None)
                    else:
                        workers_by_task[str(tid)] = lease_worker_entry(tid, lease)
                        if verdict == "unknown":
                            if note == LEASE_PENDING_NOTE:
                                lease_pending_ids.add(tid)
                            elif (note or "").startswith("worker log") and WORKER_LOG_STALE_ACTION == "pause":
                                stale_worker_ids.add(tid)
                            else:
                                lease_warnings.append(
                                    f"manual-fix: WIP #{tid} worker liveness unknown ({note or 'unknown'})."
                                )
                else:
                    workers_by_task.pop(str(tid), None)
                    workers_by_task.pop(tid, None)

        # Self-heal state: drop stale bookkeeping for tasks no longer in those columns.
        blocked_ids = {int(t.get("id")) for t, _sl in blocked_tasks}
        done_ids = {int(t.get("id")) for t, _sl in done_tasks}
        for k in list(auto_blocked.keys()):
            try:
                if int(k) not in blocked_ids:
                    auto_blocked.pop(k, None)
            except Exception:
                auto_blocked.pop(k, None)
        for k in list(repo_by_task.keys()):
            try:
                if int(k) in done_ids:
                    repo_by_task.pop(k, None)
            except Exception:
                repo_by_task.pop(k, None)
        for k in list(repo_hold_commented_by_task_id.keys()):
            try:
                if int(k) in done_ids:
                    repo_hold_commented_by_task_id.pop(k, None)
            except Exception:
                repo_hold_commented_by_task_id.pop(k, None)
        for k in list(reviewers_by_task.keys()):
            try:
                if int(k) not in review_ids:
                    reviewers_by_task.pop(k, None)
            except Exception:
                reviewers_by_task.pop(k, None)
        for k in list(review_results_by_task.keys()):
            try:
                if int(k) not in review_ids:
                    review_results_by_task.pop(k, None)
            except Exception:
                review_results_by_task.pop(k, None)
        for k in list(docs_workers_by_task.keys()):
            try:
                if int(k) not in docs_ids:
                    docs_workers_by_task.pop(k, None)
            except Exception:
                docs_workers_by_task.pop(k, None)
        for k in list(docs_spawn_failures_by_task.keys()):
            try:
                if int(k) not in docs_ids:
                    docs_spawn_failures_by_task.pop(k, None)
            except Exception:
                docs_spawn_failures_by_task.pop(k, None)

        # Sort helper
        def sort_key(item: Tuple[Any, ...]) -> Tuple[int, int]:
            t = item[0]
            sl_id = item[1]
            # swimlane priority index
            sl_name = None
            for sl in swimlanes:
                if int(sl.get("id") or 0) == sl_id:
                    sl_name = sl.get("name")
                    break
            pri_list = state.get("swimlanePriority") or ["Default swimlane"]
            pri = pri_list.index(sl_name) if sl_name in pri_list else len(pri_list)
            return (pri, int(t.get("position") or 10**9))

        # Determine critical queue early so drift checks don't flag queued criticals.
        all_open: List[Tuple[Dict[str, Any], int, int]] = []
        for sl in swimlanes:
            for c in (sl.get("columns") or []):
                col_id = int(c.get("id") or 0)
                if col_id == int(col_done["id"]):
                    continue
                for t in (c.get("tasks") or []):
                    all_open.append((t, int(sl.get("id") or 0), col_id))

        critical_candidates: List[Tuple[Dict[str, Any], int, int]] = []
        critical_task_ids: set[int] = set()
        for t, sl_id, col_id in all_open:
            tid = int(t.get("id"))
            try:
                tags = get_task_tags(tid)
            except Exception:
                tags = []
            # Critical queueing uses `hold:queued-critical` as an orchestrator-managed fence.
            # Those cards are "held" for normal flow, but MUST still be considered for
            # critical selection so we can unqueue them when they become active.
            if is_critical(tags) and (not is_hard_hold(tags)):
                critical_candidates.append((t, sl_id, col_id))
                critical_task_ids.add(tid)

        active_critical, queued_critical = pick_critical_queue(
            critical_candidates,
            int(col_wip["id"]),
            int(col_review["id"]),
            int(col_ready["id"]),
            sort_key,
        )
        queued_critical_ids = {int(t.get("id")) for t, _sl_id, _col_id in queued_critical}
        active_critical_id: Optional[int] = None
        active_critical_col_id: Optional[int] = None
        # When CRITICAL_FREEZE_ALL=1, any active critical freezes normal pulling until the critical reaches Done.
        # Otherwise, we only freeze when a critical is actively in WIP.
        critical_exclusive = False
        if active_critical is not None:
            try:
                active_critical_id = int(active_critical[0].get("id") or 0)
            except Exception:
                active_critical_id = None
            try:
                active_critical_col_id = int(active_critical[2])
            except Exception:
                active_critical_col_id = None
            if active_critical_col_id is not None:
                if CRITICAL_FREEZE_ALL:
                    critical_exclusive = active_critical_col_id != int(col_done["id"])
                else:
                    critical_exclusive = active_critical_col_id == int(col_wip["id"])

        wip_count = len(wip_tasks)
        wip_active_count_cache: Optional[int] = None

        def wip_active_count() -> int:
            nonlocal wip_active_count_cache
            if wip_active_count_cache is not None:
                return wip_active_count_cache
            cnt = 0
            for wt, _wsl in wip_tasks:
                tid = int(wt.get("id"))
                try:
                    tags = get_task_tags(tid)
                except Exception:
                    tags = []
                if not is_held(tags):
                    cnt += 1
            wip_active_count_cache = cnt
            return cnt

        def invalidate_wip_active_count() -> None:
            nonlocal wip_active_count_cache
            wip_active_count_cache = None

        actions: List[str] = []
        promoted_to_ready: List[int] = []
        moved_to_wip: List[int] = []
        created_tasks: List[int] = []
        errors: List[str] = []
        wip_ids = {int(t.get("id")) for t, _sl_id in wip_tasks}
        if lease_warnings:
            errors.extend(lease_warnings)
        if WORKER_LEASES_ENABLED:
            try:
                errors.extend(scan_orphan_leases(wip_ids))
            except Exception:
                pass

        missing_worker_tasks: List[Tuple[Dict[str, Any], int]] = []

        # Drift: WIP tasks missing worker handle and/or repo mapping
        for t, sl_id in wip_tasks:
            tid = int(t.get("id"))
            title = task_title(t)
            if tid in queued_critical_ids:
                continue
            tags: List[str] = []
            desc = ""
            try:
                tags = get_task_tags(tid)
                full = get_task(tid)
                desc = (full.get("description") or "")
            except Exception:
                tags = []
                desc = ""

            # If a card is explicitly paused/held, don't keep trying to respawn workers every tick.
            # Exception: critical cards can still be reconciled.
            reconcile_worker = (not is_held(tags)) or is_critical(tags)
            if reconcile_worker:
                entry = worker_entry_for(tid, workers_by_task)

                # Greenfield rule: treat done.json as the canonical completion signal.
                # If we have an inflight run (donePath present but missing), do NOT respawn.
                inflight = False
                done_path = None
                started_at_ms = None
                if isinstance(entry, dict):
                    done_path = entry.get("donePath") or entry.get("done_path") or ""
                    try:
                        started_at_ms = int(entry.get("startedAtMs") or 0) or None
                    except Exception:
                        started_at_ms = None
                    if done_path:
                        if os.path.isfile(str(done_path)):
                            inflight = False
                        else:
                            inflight = True

                if inflight:
                    # If the tmux window/handle is gone, treat the run as stale immediately
                    # (otherwise we can get stuck waiting for done.json until the timeout).
                    h = worker_handle(entry)
                    if h and not worker_is_alive(h):
                        workers_by_task.pop(str(tid), None)
                        workers_by_task.pop(tid, None)
                        missing_worker_tasks.append((t, sl_id))
                        continue

                    # If the run is taking too long, treat it as stale and allow a respawn.
                    if started_at_ms and WORKER_RUN_TIMEOUT_MIN > 0:
                        timeout_ms = WORKER_RUN_TIMEOUT_MIN * 60 * 1000
                        if now_ms() - started_at_ms > timeout_ms:
                            workers_by_task.pop(str(tid), None)
                            workers_by_task.pop(tid, None)
                            missing_worker_tasks.append((t, sl_id))
                    continue

                # No inflight run recorded => needs a worker.
                # A runId without a donePath is treated as missing (incomplete spawn payload).
                if entry is None or (
                    isinstance(entry, dict)
                    and not (entry.get("donePath") or entry.get("done_path"))
                ):
                    missing_worker_tasks.append((t, sl_id))

            try:
                if str(tid) in repo_by_task and os.path.isdir(str(repo_by_task.get(str(tid), {}).get("path") or "")):
                    continue
                if not has_repo_mapping(tid, title, tags, desc):
                    errors.append(
                        f"drift: WIP #{tid} ({title}) has no repo mapping (add 'Repo:' in description or tag repo:<key> or tag no-repo)"
                    )
            except Exception:
                pass

        # Docs drift is handled after dry-run mode is computed.

        # Cooldown is meant to prevent repeating the same move decision across runs.
        # Snapshot the last-actions map at the start of the run so a single tick can
        # still perform multiple transitions (e.g., Backlog -> Ready -> WIP).
        last_actions_prev = state.get("lastActionsByTaskId") or {}
        last_actions = dict(last_actions_prev)
        cooldown_ms = TASK_COOLDOWN_MIN * 60 * 1000

        def cooled(task_id: int) -> bool:
            last = int(last_actions_prev.get(str(task_id), 0) or 0)
            return (now_ms() - last) >= cooldown_ms

        def record_action(task_id: int) -> None:
            last_actions[str(task_id)] = now_ms()

        comment_user_id: Optional[int] = None

        def ensure_comment_user_id() -> int:
            nonlocal comment_user_id
            if comment_user_id is not None:
                return comment_user_id
            try:
                me = rpc("getMe")
                comment_user_id = int(me.get("id") or 0)
            except Exception:
                comment_user_id = 0
            return comment_user_id

        def add_tag(task_id: int, tag: str) -> None:
            try:
                tags = get_task_tags(task_id)
                if has_tag(tags, tag):
                    return
                # Kanboard expects full tag list
                set_task_tags(pid, task_id, tags + [tag])
            except Exception:
                pass

        def remove_tag(task_id: int, tag: str) -> None:
            try:
                tags = get_task_tags(task_id)
                new_tags = [t for t in tags if t.strip().lower() != tag.strip().lower()]
                if new_tags != tags:
                    set_task_tags(pid, task_id, new_tags)
            except Exception:
                pass

        def add_tags(task_id: int, tags_to_add: List[str]) -> None:
            try:
                existing = get_task_tags(task_id)
                lower = {t.lower() for t in existing}
                merged = existing[:]
                for t in tags_to_add:
                    if t and t.lower() not in lower:
                        merged.append(t)
                        lower.add(t.lower())
                if merged != existing:
                    set_task_tags(pid, task_id, merged)
            except Exception:
                pass

        def remove_tags(task_id: int, tags_to_remove: List[str]) -> None:
            try:
                existing = get_task_tags(task_id)
                remove_lower = {t.lower() for t in tags_to_remove if t}
                new_tags = [t for t in existing if t.strip().lower() not in remove_lower]
                if new_tags != existing:
                    set_task_tags(pid, task_id, new_tags)
            except Exception:
                pass

        def clear_paused_tags(task_id: int) -> None:
            """Remove paused tags that prevent automation from advancing cards.

            We treat any of:
            - paused
            - paused:*
            as "runtime holds" rather than durable metadata.
            """
            try:
                existing = get_task_tags(task_id)
            except Exception:
                return
            if not existing:
                return
            new_tags = [t for t in existing if not (t.strip().lower() == TAG_PAUSED or t.strip().lower().startswith("paused:"))]
            # Kanboard expects the full tag list on set; only write when changed.
            if new_tags != existing:
                try:
                    set_task_tags(pid, task_id, new_tags)
                except Exception:
                    pass

        def add_comment(task_id: int, comment: str) -> None:
            if not comment:
                return
            try:
                user_id = ensure_comment_user_id()
                rpc("createComment", {"task_id": task_id, "user_id": user_id, "content": comment})
            except Exception:
                pass

        def maybe_comment_needs_repo(task_id: int) -> None:
            """Post a one-time comment explaining how to add a repo mapping.

            Uses state to avoid re-posting every tick.
            """
            if str(task_id) in repo_hold_commented_by_task_id:
                return
            msg = (
                "Automation is paused: this card needs an explicit repo mapping.\n"
                "Add ONE of:\n"
                "- `Repo: /absolute/path/to/repo` (in the description)\n"
                "- tag `repo:<key>` (e.g. `repo:clawd`)\n"
                "- tag `no-repo` (explicit opt-out)\n"
                "\n"
                "Once fixed, the orchestrator will clear `hold:needs-repo`/`blocked:repo` automatically."
            )
            if dry_run:
                actions.append(f"Would comment on #{task_id}: needs repo mapping (Repo:/repo:*/no-repo)")
                return
            add_comment(task_id, msg)
            repo_hold_commented_by_task_id[str(task_id)] = now_ms()

        def ensure_worker_handle_for_task_legacy(
            task_id: int,
            repo_key: Optional[str],
            repo_path: Optional[str],
        ) -> Tuple[bool, Optional[Dict[str, Any]]]:
            entry = worker_entry_for(task_id, workers_by_task)
            # Greenfield: a worker entry without a donePath is treated as incomplete/stale.
            # Clear it so reconciliation can spawn a fresh worker run.
            if isinstance(entry, dict) and not (entry.get("donePath") or entry.get("done_path")):
                workers_by_task.pop(str(task_id), None)
                workers_by_task.pop(task_id, None)
                entry = None
            handle = worker_handle(entry)
            if handle and worker_is_alive(handle):
                return True, None

            # If we have a pid-based handle but the process is dead, diagnose + decide.
            if handle and not worker_is_alive(handle):
                log_path = None
                if isinstance(entry, dict):
                    log_path = entry.get("logPath")
                if not log_path:
                    log_path = default_worker_log_path(task_id)

                diag = diagnose_worker_failure(task_id, log_path)
                category = str(diag.get("category") or "unknown")

                # Persist some breadcrumbs for later debugging.
                if isinstance(entry, dict):
                    entry["lastDeadAtMs"] = now_ms()
                    entry["lastDiagnosis"] = diag

                # If the failure smells like a manual issue, pause + alert.
                manual_categories = {"quota", "auth", "permissions", "git"}
                if category in manual_categories:
                    try:
                        t = get_task(task_id)
                        title = task_title(t)
                        sl_id = int(t.get("swimlane_id") or 0)
                    except Exception:
                        title = str(task_id)
                        sl_id = 0

                    reason = f"worker failed ({category})"
                    pause_missing_worker(task_id, sl_id, title, reason, force=True, label="WIP")
                    add_tag(task_id, f"paused:{category}")

                    tail = diag.get("tail") or []
                    tail_text = "\n".join([str(x) for x in tail][-10:])
                    msg = (
                        "Auto-pause: worker died and needs manual attention.\n"
                        + f"Category: {category}\n"
                        + (f"Suggestion: {diag.get('suggestion')}\n" if diag.get('suggestion') else "")
                        + ("Last log lines:\n" + tail_text if tail_text else "")
                    )
                    add_comment(task_id, msg)

                    if category in ("auth", "quota"):
                        provider_force_block(
                            state,
                            provider="codex",
                            category=category,
                            message=f"Detected from worker log (WIP #{task_id})",
                            errors=errors,
                        )
                    else:
                        # Ensure this results in a Telegram alert via the cron wrapper.
                        errors.append(
                            f"manual-fix: WIP #{task_id} ({title}) worker died ({category}); auto-paused."
                        )

                    # Drop stale handle so it doesn't get treated as alive.
                    workers_by_task.pop(str(task_id), None)
                    workers_by_task.pop(task_id, None)
                    return False, {"kind": "worker-dead", "category": category}

                # Otherwise, attempt a controlled respawn (anti-thrash).
                restart_count = 0
                last_restart = 0
                started_at = 0
                if isinstance(entry, dict):
                    restart_count = int(entry.get("restartCount") or 0)
                    last_restart = int(entry.get("lastRestartAtMs") or 0)
                    started_at = int(entry.get("startedAtMs") or 0)

                nowm = now_ms()
                # Within 30 minutes, allow at most 2 restarts.
                window_ms = 30 * 60 * 1000
                if last_restart and (nowm - last_restart) < window_ms and restart_count >= 2:
                    try:
                        t = get_task(task_id)
                        title = task_title(t)
                        sl_id = int(t.get("swimlane_id") or 0)
                    except Exception:
                        title = str(task_id)
                        sl_id = 0

                    pause_missing_worker(task_id, sl_id, title, "worker restart thrash", force=True, label="WIP")
                    add_tag(task_id, "paused:thrash")
                    errors.append(
                        f"manual-fix: WIP #{task_id} ({title}) worker keeps dying; paused (thrash guard)."
                    )
                    workers_by_task.pop(str(task_id), None)
                    workers_by_task.pop(task_id, None)
                    return False, {"kind": "thrash"}

                # Never spawn workers during dry-run.
                if dry_run:
                    return False, {"kind": "dry-run"}

                # Drop stale entry and respawn.
                workers_by_task.pop(str(task_id), None)
                workers_by_task.pop(task_id, None)
                
                if WORKER_SPAWN_CMD and repo_path is not None:
                    provider = infer_preflight_provider("worker", WORKER_SPAWN_CMD)
                    if provider:
                        ok, category2, msg2 = provider_preflight_gate(state, provider=provider, errors=errors)
                        if not ok:
                            reason_tag = TAG_BLOCKED_QUOTA if str(category2) == "quota" else TAG_BLOCKED_AUTH
                            if dry_run:
                                actions.append(
                                    f"Would tag WIP #{task_id} as {reason_tag} (provider {provider} {category2}: {msg2})"
                                )
                            else:
                                try:
                                    existing = get_task_tags(task_id)
                                except Exception:
                                    existing = []
                                lower = {t.lower() for t in (existing or [])}
                                if reason_tag.lower() not in lower or TAG_AUTO_BLOCKED.lower() not in lower:
                                    add_tags(task_id, [reason_tag, TAG_AUTO_BLOCKED])
                                    record_action(task_id)
                                    actions.append(
                                        f"Tagged WIP #{task_id} as {reason_tag} (provider {provider} {category2}: {msg2})"
                                    )
                            return False, {
                                "kind": "provider-blocked",
                                "provider": provider,
                                "category": category2,
                                "message": msg2,
                            }
                spawned = spawn_worker(task_id, repo_key, repo_path)
                if spawned:
                    spawned["restartCount"] = restart_count + 1
                    spawned["lastRestartAtMs"] = nowm
                    workers_by_task[str(task_id)] = spawned
                    actions.append(f"Respawned worker for WIP #{task_id} (diagnosed dead pid; category {category})")
                    return True, None
                return False, None

            # No handle: spawn if allowed.
            if dry_run:
                return False, {"kind": "dry-run"}
            if WORKER_SPAWN_CMD and repo_path is not None:
                provider = infer_preflight_provider("worker", WORKER_SPAWN_CMD)
                if provider:
                    ok, category2, msg2 = provider_preflight_gate(state, provider=provider, errors=errors)
                    if not ok:
                        reason_tag = TAG_BLOCKED_QUOTA if str(category2) == "quota" else TAG_BLOCKED_AUTH
                        if dry_run:
                            actions.append(
                                f"Would tag WIP #{task_id} as {reason_tag} (provider {provider} {category2}: {msg2})"
                            )
                        else:
                            try:
                                existing = get_task_tags(task_id)
                            except Exception:
                                existing = []
                            lower = {t.lower() for t in (existing or [])}
                            if reason_tag.lower() not in lower or TAG_AUTO_BLOCKED.lower() not in lower:
                                add_tags(task_id, [reason_tag, TAG_AUTO_BLOCKED])
                                record_action(task_id)
                                actions.append(
                                    f"Tagged WIP #{task_id} as {reason_tag} (provider {provider} {category2}: {msg2})"
                                )
                        return False, {
                            "kind": "provider-blocked",
                            "provider": provider,
                            "category": category2,
                            "message": msg2,
                        }
                spawned = spawn_worker(task_id, repo_key, repo_path)
                if spawned:
                    workers_by_task[str(task_id)] = spawned
                    return True, None
            return False, None

        def ensure_worker_handle_for_task(
            task_id: int,
            repo_key: Optional[str],
            repo_path: Optional[str],
        ) -> Tuple[bool, Optional[Dict[str, Any]]]:
            if not WORKER_LEASES_ENABLED:
                return ensure_worker_handle_for_task_legacy(task_id, repo_key, repo_path)

            entry = worker_entry_for(task_id, workers_by_task)
            lease = load_lease(task_id)
            lease_id = None
            if lease:
                lease_id = lease.get("leaseId")
                verdict, _worker_pid, note = evaluate_lease_liveness(task_id, lease)
                update_lease_liveness(task_id, lease, verdict, note)
                if verdict == "alive":
                    workers_by_task[str(task_id)] = lease_worker_entry(task_id, lease)
                    return True, None
                if verdict == "unknown":
                    if note == LEASE_PENDING_NOTE:
                        workers_by_task[str(task_id)] = lease_worker_entry(task_id, lease)
                        return True, None
                    errors.append(
                        f"manual-fix: WIP #{task_id} worker liveness unknown ({note or 'unknown'})."
                    )
                    workers_by_task[str(task_id)] = lease_worker_entry(task_id, lease)
                    return False, {"kind": "lease-unknown", "note": note}

            # If we have a dead lease or a dead pid-based handle, diagnose + decide.
            handle = worker_handle(entry)
            pid = extract_pid(handle)
            if lease or (pid and not pid_alive(pid)):
                log_path = lease_log_path(lease, task_id) if lease else None
                if not log_path and isinstance(entry, dict):
                    log_path = entry.get("logPath")
                if not log_path:
                    log_path = default_worker_log_path(task_id)

                diag = diagnose_worker_failure(task_id, log_path)
                category = str(diag.get("category") or "unknown")

                # Persist some breadcrumbs for later debugging.
                if isinstance(entry, dict):
                    entry["lastDeadAtMs"] = now_ms()
                    entry["lastDiagnosis"] = diag

                # If the failure smells like a manual issue, pause + alert.
                manual_categories = {"quota", "auth", "permissions", "git"}
                if category in manual_categories:
                    try:
                        t = get_task(task_id)
                        title = task_title(t)
                        sl_id = int(t.get("swimlane_id") or 0)
                    except Exception:
                        title = str(task_id)
                        sl_id = 0

                    reason = f"worker failed ({category})"
                    pause_missing_worker(task_id, sl_id, title, reason, force=True, label="WIP")
                    add_tag(task_id, f"paused:{category}")

                    tail = diag.get("tail") or []
                    tail_text = "\n".join([str(x) for x in tail][-10:])
                    msg = (
                        "Auto-pause: worker died and needs manual attention.\n"
                        + f"Category: {category}\n"
                        + (f"Suggestion: {diag.get('suggestion')}\n" if diag.get('suggestion') else "")
                        + ("Last log lines:\n" + tail_text if tail_text else "")
                    )
                    add_comment(task_id, msg)

                    if category in ("auth", "quota"):
                        provider_force_block(
                            state,
                            provider="codex",
                            category=category,
                            message=f"Detected from worker log (WIP #{task_id})",
                            errors=errors,
                        )
                    else:
                        errors.append(
                            f"manual-fix: WIP #{task_id} ({title}) worker died ({category}); auto-paused."
                        )
                    workers_by_task.pop(str(task_id), None)
                    workers_by_task.pop(task_id, None)
                    if lease:
                        archive_lease_dir(task_id, lease_id or lease.get("leaseId"))
                    record_spawn_attempt(task_id, lease_id, run_id, "refused", f"manual-{category}")
                    return False, {"kind": "worker-dead", "category": category}

                # Otherwise, attempt a controlled respawn (anti-thrash).
                nowm = now_ms()
                if not thrash_guard_allows(task_id, nowm):
                    try:
                        t = get_task(task_id)
                        title = task_title(t)
                        sl_id = int(t.get("swimlane_id") or 0)
                    except Exception:
                        title = str(task_id)
                        sl_id = 0

                    pause_missing_worker(task_id, sl_id, title, "worker restart thrash", force=True, label="WIP")
                    add_tag(task_id, THRASH_PAUSE_TAG)
                    errors.append(
                        f"manual-fix: WIP #{task_id} ({title}) worker keeps dying; paused (thrash guard)."
                    )
                    workers_by_task.pop(str(task_id), None)
                    workers_by_task.pop(task_id, None)
                    if lease:
                        archive_lease_dir(task_id, lease_id or lease.get("leaseId"))
                    record_spawn_attempt(task_id, lease_id, run_id, "refused", "thrash")
                    return False, {"kind": "thrash"}

                # Never spawn workers during dry-run.
                if dry_run:
                    record_spawn_attempt(task_id, lease_id, run_id, "refused", "dry-run")
                    return False, {"kind": "dry-run"}

                # Drop stale entry and stale lease before respawn.
                workers_by_task.pop(str(task_id), None)
                workers_by_task.pop(task_id, None)
                if lease:
                    archive_lease_dir(task_id, lease_id or lease.get("leaseId"))

            # If we have a live pid-based handle but no lease, seed one for recovery.
            if not lease and pid and pid_alive(pid):
                if not acquire_lease_dir(task_id):
                    existing = load_lease(task_id)
                    if existing:
                        verdict, _pid, note = evaluate_lease_liveness(task_id, existing)
                        update_lease_liveness(task_id, existing, verdict, note)
                        workers_by_task[str(task_id)] = lease_worker_entry(task_id, existing)
                        if verdict == "alive":
                            return True, None
                        return False, {"kind": "lease-unknown", "note": note}
                    if recover_stale_lease_dir(task_id):
                        if not acquire_lease_dir(task_id):
                            return False, {"kind": "lease-race"}
                    else:
                        return False, {"kind": "lease-race"}
                cmd, safe_repo_key, safe_repo_path = format_worker_spawn_cmd(task_id, repo_key, repo_path)
                lease_payload = init_lease_payload(
                    task_id,
                    run_id,
                    safe_repo_key or (entry.get("repoKey") if isinstance(entry, dict) else ""),
                    safe_repo_path or (entry.get("repoPath") if isinstance(entry, dict) else ""),
                    (entry.get("logPath") if isinstance(entry, dict) else None) or default_worker_log_path(task_id),
                    (entry.get("patchPath") if isinstance(entry, dict) else None) or default_worker_patch_path(task_id),
                    (entry.get("commentPath") if isinstance(entry, dict) else None)
                    or default_worker_comment_path(task_id),
                    cmd,
                    WORKER_SPAWN_TIMEOUT_SEC,
                )
                lease_payload["worker"]["pid"] = pid
                lease_payload["worker"]["startedAtMs"] = (
                    entry.get("startedAtMs") if isinstance(entry, dict) else None
                ) or now_ms()
                lease_payload["worker"]["execSessionId"] = handle
                write_lease_files(task_id, lease_payload)
                workers_by_task[str(task_id)] = lease_worker_entry(task_id, lease_payload)
                return True, None

            # No lease or lease cleaned up: attempt to acquire + spawn.
            if dry_run:
                record_spawn_attempt(task_id, lease_id, run_id, "refused", "dry-run")
                return False, {"kind": "dry-run"}
            if not WORKER_SPAWN_CMD or not repo_path:
                reason = "no-spawn-cmd" if not WORKER_SPAWN_CMD else "missing-repo-path"
                record_spawn_attempt(task_id, lease_id, run_id, "refused", reason)
                return False, {"kind": "misconfig", "reason": reason}

            provider = infer_preflight_provider("worker", WORKER_SPAWN_CMD)
            if provider:
                ok, category2, msg2 = provider_preflight_gate(state, provider=provider, errors=errors)
                if not ok:
                    reason_tag = TAG_BLOCKED_QUOTA if str(category2) == "quota" else TAG_BLOCKED_AUTH
                    if dry_run:
                        actions.append(
                            f"Would tag WIP #{task_id} as {reason_tag} (provider {provider} {category2}: {msg2})"
                        )
                    else:
                        try:
                            existing = get_task_tags(task_id)
                        except Exception:
                            existing = []
                        lower = {t.lower() for t in (existing or [])}
                        if reason_tag.lower() not in lower or TAG_AUTO_BLOCKED.lower() not in lower:
                            add_tags(task_id, [reason_tag, TAG_AUTO_BLOCKED])
                            record_action(task_id)
                            actions.append(
                                f"Tagged WIP #{task_id} as {reason_tag} (provider {provider} {category2}: {msg2})"
                            )
                    record_spawn_attempt(task_id, lease_id, run_id, "refused", f"provider-{category2}")
                    return False, {
                        "kind": "provider-blocked",
                        "provider": provider,
                        "category": category2,
                        "message": msg2,
                    }

            if not thrash_guard_allows(task_id, now_ms()):
                try:
                    t = get_task(task_id)
                    title = task_title(t)
                    sl_id = int(t.get("swimlane_id") or 0)
                except Exception:
                    title = str(task_id)
                    sl_id = 0
                pause_missing_worker(task_id, sl_id, title, "worker restart thrash", force=True, label="WIP")
                add_tag(task_id, THRASH_PAUSE_TAG)
                errors.append(
                    f"manual-fix: WIP #{task_id} ({title}) worker keeps dying; paused (thrash guard)."
                )
                record_spawn_attempt(task_id, lease_id, run_id, "refused", "thrash")
                return False, {"kind": "thrash"}

            if not acquire_lease_dir(task_id):
                # Another process owns/created the lease; read + reconcile.
                lease = load_lease(task_id)
                if lease:
                    verdict, _worker_pid, note = evaluate_lease_liveness(task_id, lease)
                    update_lease_liveness(task_id, lease, verdict, note)
                    if verdict == "alive":
                        workers_by_task[str(task_id)] = lease_worker_entry(task_id, lease)
                        return True, None
                    if verdict == "unknown":
                        errors.append(
                            f"manual-fix: WIP #{task_id} worker liveness unknown ({note or 'unknown'})."
                        )
                        workers_by_task[str(task_id)] = lease_worker_entry(task_id, lease)
                        return False, {"kind": "lease-unknown", "note": note}
                    # Dead/invalid lease: archive and retry once.
                    archive_lease_dir(task_id, lease.get("leaseId"))
                    if not acquire_lease_dir(task_id):
                        record_spawn_attempt(task_id, lease.get("leaseId"), run_id, "refused", "lease-race")
                        return False, {"kind": "lease-race"}
                else:
                    if recover_stale_lease_dir(task_id) and acquire_lease_dir(task_id):
                        pass
                    else:
                        record_spawn_attempt(task_id, lease_id, run_id, "refused", "lease-race")
                        return False, {"kind": "lease-race"}

            cmd, safe_repo_key, safe_repo_path = format_worker_spawn_cmd(task_id, repo_key, repo_path)
            lease_payload = init_lease_payload(
                task_id,
                run_id,
                safe_repo_key,
                safe_repo_path,
                default_worker_log_path(task_id),
                default_worker_patch_path(task_id),
                default_worker_comment_path(task_id),
                cmd,
                WORKER_SPAWN_TIMEOUT_SEC,
            )
            write_lease_files(task_id, lease_payload)
            lease_id = lease_payload.get("leaseId")

            spawned = spawn_worker(task_id, repo_key, repo_path)
            if not spawned:
                record_spawn_attempt(task_id, lease_id, run_id, "failed", "spawn-failed")
                archive_lease_dir(task_id, lease_id)
                return False, {"kind": "spawn-failed"}

            worker_pid = extract_pid(worker_handle(spawned))
            lease_payload["worker"]["pid"] = worker_pid
            lease_payload["worker"]["startedAtMs"] = spawned.get("startedAtMs") or now_ms()
            lease_payload["worker"]["execSessionId"] = spawned.get("execSessionId")
            lease_payload["worker"]["logPath"] = spawned.get("logPath") or default_worker_log_path(task_id)
            lease_payload["worker"]["patchPath"] = spawned.get("patchPath") or default_worker_patch_path(task_id)
            lease_payload["worker"]["commentPath"] = spawned.get("commentPath") or default_worker_comment_path(task_id)
            write_lease_files(task_id, lease_payload)

            record_spawn_attempt(task_id, lease_id, run_id, "spawned")
            workers_by_task[str(task_id)] = lease_worker_entry(task_id, lease_payload)
            return True, None

        def resolve_patch_path_for_task(task_id: int) -> Optional[str]:
            entry = worker_entry_for(task_id, workers_by_task)
            if isinstance(entry, dict):
                p = entry.get("patchPath")
                if p and os.path.isfile(str(p)):
                    return str(p)
                done = worker_done_from_entry(entry)
                if done and done.get("patchPath") and os.path.isfile(str(done.get("patchPath"))):
                    entry["patchPath"] = str(done.get("patchPath"))
                    return str(done.get("patchPath"))
            # Fallback: allow manual injection of a patch artifact without a worker handle.
            # This supports cases where a patch was produced out-of-band but we still want
            # to run the standard review automation.
            p = default_worker_patch_path(task_id)
            if p and os.path.isfile(str(p)):
                return str(p)
            return None

        def docs_inflight_count() -> int:
            count = 0
            for entry in (docs_workers_by_task or {}).values():
                if not isinstance(entry, dict):
                    continue
                if worker_done_from_entry(entry):
                    continue
                h = worker_handle(entry)
                if not h:
                    continue
                # worker_is_alive treats unknown handles as alive.
                if worker_is_alive(h):
                    count += 1
            return count

        def ensure_docs_worker_handle_for_task(
            task_id: int,
            source_repo_key: Optional[str],
            source_repo_path: Optional[str],
            source_patch_path: Optional[str],
        ) -> Tuple[bool, Optional[Dict[str, Any]]]:
            entry = worker_entry_for(task_id, docs_workers_by_task)
            # Require a donePath so we can deterministically reconcile completion.
            if isinstance(entry, dict) and not (entry.get("donePath") or entry.get("done_path")):
                docs_workers_by_task.pop(str(task_id), None)
                docs_workers_by_task.pop(task_id, None)
                entry = None

            handle = worker_handle(entry)
            if handle and worker_is_alive(handle):
                return True, None
            if handle and not worker_is_alive(handle):
                docs_workers_by_task.pop(str(task_id), None)
                docs_workers_by_task.pop(task_id, None)
            if DOCS_SPAWN_CMD:
                provider = infer_preflight_provider("docs", DOCS_SPAWN_CMD)
                if provider:
                    ok, category, msg = provider_preflight_gate(state, provider=provider, errors=errors)
                    if not ok:
                        reason_tag = TAG_BLOCKED_QUOTA if str(category) == "quota" else TAG_BLOCKED_AUTH
                        if dry_run:
                            actions.append(
                                f"Would tag Documentation #{task_id} as {reason_tag} (provider {provider} {category}: {msg})"
                            )
                        else:
                            try:
                                existing = get_task_tags(task_id)
                            except Exception:
                                existing = []
                            lower = {t.lower() for t in (existing or [])}
                            if reason_tag.lower() not in lower or TAG_AUTO_BLOCKED.lower() not in lower:
                                add_tags(task_id, [reason_tag, TAG_AUTO_BLOCKED])
                                record_action(task_id)
                                actions.append(
                                    f"Tagged Documentation #{task_id} as {reason_tag} (provider {provider} {category}: {msg})"
                                )
                        return False, {"kind": "provider-blocked", "provider": provider, "category": category, "message": msg}
                spawned = spawn_docs_worker(task_id, source_repo_key, source_repo_path, source_patch_path)
                if spawned and spawned.get("donePath"):
                    docs_workers_by_task[str(task_id)] = spawned
                    return True, None
            return False, None

        def ensure_reviewer_handle_for_task(
            task_id: int,
            repo_key: Optional[str],
            repo_path: Optional[str],
            patch_path: Optional[str],
            review_revision: Optional[str],
        ) -> Tuple[bool, Optional[Dict[str, Any]]]:
            entry = worker_entry_for(task_id, reviewers_by_task)
            # Greenfield: a reviewer entry without a resultPath is treated as incomplete/stale.
            if isinstance(entry, dict) and not (entry.get("resultPath") or entry.get("result_path")):
                reviewers_by_task.pop(str(task_id), None)
                reviewers_by_task.pop(task_id, None)
                entry = None
            handle = worker_handle(entry)
            if handle and reviewer_is_alive(handle):
                return True, None
            if handle and not reviewer_is_alive(handle):
                # Drop stale reviewer bookkeeping so we can respawn deterministically.
                reviewers_by_task.pop(str(task_id), None)
                reviewers_by_task.pop(task_id, None)
            if REVIEWER_SPAWN_CMD:
                provider = infer_preflight_provider("reviewer", REVIEWER_SPAWN_CMD)
                if provider:
                    ok, category, msg = provider_preflight_gate(state, provider=provider, errors=errors)
                    if not ok:
                        reason_tag = TAG_BLOCKED_QUOTA if str(category) == "quota" else TAG_BLOCKED_AUTH
                        if dry_run:
                            actions.append(
                                f"Would tag Review #{task_id} as {reason_tag} (provider {provider} {category}: {msg})"
                            )
                        else:
                            try:
                                existing = get_task_tags(task_id)
                            except Exception:
                                existing = []
                            lower = {t.lower() for t in (existing or [])}
                            if reason_tag.lower() not in lower or TAG_AUTO_BLOCKED.lower() not in lower:
                                add_tags(task_id, [reason_tag, TAG_AUTO_BLOCKED])
                                record_action(task_id)
                                actions.append(
                                    f"Tagged Review #{task_id} as {reason_tag} (provider {provider} {category}: {msg})"
                                )
                        return False, {"kind": "provider-blocked", "provider": provider, "category": category, "message": msg}
                spawned = spawn_reviewer(task_id, repo_key, repo_path, patch_path, review_revision)
                if spawned:
                    reviewers_by_task[str(task_id)] = spawned
                    return True, None
            return False, None

        def pause_missing_worker(
            task_id: int,
            sl_id: int,
            title: str,
            reason: str,
            *,
            force: bool = False,
            label: str = "WIP",
        ) -> bool:
            # Budget accounting is handled by callers to avoid double-decrement bugs.
            if budget <= 0 and not force:
                return False
            if dry_run:
                actions.append(f"Would tag {label} #{task_id} ({title}) as paused:missing-worker ({reason})")
            else:
                record_action(task_id)
                add_tags(task_id, [TAG_PAUSED, TAG_PAUSED_MISSING_WORKER])
                # If the card can't run because we can't resolve its repo mapping, attach an explicit hold reason
                # and a one-time comment so the fix is obvious in Kanboard.
                if "repo" in (reason or "").lower():
                    add_tags(task_id, [TAG_AUTO_BLOCKED, TAG_BLOCKED_REPO, TAG_HOLD_NEEDS_REPO])
                    remove_tags(task_id, [TAG_NO_REPO, TAG_HOLD])
                    maybe_comment_needs_repo(task_id)
                actions.append(f"Tagged {label} #{task_id} ({title}) as paused:missing-worker ({reason})")
                # Keep WIP/Ready clean: paused cards shouldn't sit in active columns.
                try:
                    move_task(pid, task_id, int(col_blocked["id"]), 1, int(sl_id))
                except Exception:
                    pass
            return True

        def tag_blocked_and_keep_in_backlog(
            task_id: int,
            sl_id: int,
            title: str,
            reason: str,
            reason_tag: str,
            *,
            from_label: str,
            auto_blocked: bool = False,
        ) -> None:
            """Backlog+tag policy for non-actionable cards.

            We avoid filling the Blocked column with "missing repo"/deps/exclusive.
            Instead we keep the card in Backlog and attach a durable tag so a human
            can fix/triage without automation thrashing.
            """
            record_action(task_id)
            tags_to_add = [reason_tag]
            # Certain blocked reasons are transient (deps/exclusive/repo mapping) and should auto-heal
            # once the condition clears. Mark these as orchestrator-owned so we can safely clear them.
            if auto_blocked:
                tags_to_add.append(TAG_AUTO_BLOCKED)
            if reason_tag == TAG_BLOCKED_DEPS:
                tags_to_add.append(TAG_HOLD_DEPS)
            if reason_tag == TAG_BLOCKED_REPO:
                tags_to_add.append(TAG_HOLD_NEEDS_REPO)
            add_tags(task_id, tags_to_add)
            if reason_tag == TAG_BLOCKED_REPO:
                # Clean up legacy/incorrect auto-tags that made missing mappings ambiguous.
                remove_tags(task_id, [TAG_NO_REPO, TAG_HOLD])
                maybe_comment_needs_repo(task_id)
            try:
                move_task(pid, task_id, int(col_backlog["id"]), 1, int(sl_id))
            except Exception:
                pass
            actions.append(f"Kept {from_label} #{task_id} ({title}) in Backlog; tagged {reason_tag}: {reason}")

        def record_repo(task_id: int, repo_key: Optional[str], repo_path: Optional[str], source: Optional[str]) -> None:
            if not repo_key or not repo_path:
                return
            payload: Dict[str, Any] = {"key": repo_key, "path": repo_path, "resolvedAtMs": now_ms()}
            if source:
                payload["source"] = source
            repo_by_task[str(task_id)] = payload

        def resolve_repo_for_task(
            task_id: int,
            title: str,
            tags: List[str],
            description: str,
            *,
            require_explicit: bool = False,
        ) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
            if has_tag(tags, TAG_NO_REPO):
                # Explicit opt-out: allow automation to proceed without a repo path.
                # Downstream spawn scripts receive an empty repo path.
                return True, None, "", "tag"
            hint, source = parse_repo_hint_with_source(
                tags,
                description,
                title,
                # Title-prefix mapping is legacy; when enforcing repo hygiene, require explicit
                # tag/description hints so cards are actionable.
                allow_title_prefix=(False if require_explicit else ALLOW_TITLE_REPO_HINT),
            )
            repo_key, repo_path = resolve_repo_path(hint, repo_map)
            if repo_path:
                record_repo(task_id, repo_key, repo_path, source)
                return True, repo_key, repo_path, source
            return False, repo_key, None, source

        def has_repo_mapping(task_id: int, title: str, tags: List[str], description: str) -> bool:
            ok, _key, _path, _source = resolve_repo_for_task(
                task_id, title, tags, description, require_explicit=True
            )
            return ok

        # Determine dry-run
        dry_runs_remaining = int(state.get("dryRunRunsRemaining") or 0)
        dry_run = bool(state.get("dryRun", True))
        auto_arm = False
        if dry_run and dry_runs_remaining <= 0:
            dry_run = False
        if dry_run and dry_runs_remaining == 1:
            auto_arm = True

        mode = "DRY_RUN" if dry_run else "LIVE"

        budget = ACTION_BUDGET

        # Hygiene: replace ambiguous plain `hold` with explicit hold reasons.
        # Best-effort; do not scan endlessly in a single tick.
        def normalize_plain_hold(task_id: int, tags: List[str]) -> bool:
            lower = {t.lower() for t in (tags or [])}
            if TAG_HOLD not in lower:
                return False
            has_reason = any(t.startswith("hold:") for t in lower)
            if dry_run:
                actions.append(f"Would normalize hold tag on #{task_id}")
                return True
            # Always remove plain `hold`; keep any existing reason tag.
            if not has_reason:
                if TAG_BLOCKED_DEPS in lower:
                    add_tags(task_id, [TAG_HOLD_DEPS])
                elif TAG_BLOCKED_REPO in lower:
                    add_tags(task_id, [TAG_HOLD_NEEDS_REPO])
                    maybe_comment_needs_repo(task_id)
                else:
                    add_tags(task_id, [TAG_HOLD_MANUAL])
            remove_tag(task_id, TAG_HOLD)
            return True

        hold_fix_budget = 10
        if all_open and hold_fix_budget > 0:
            for t, _sl_id, _col_id in all_open:
                if hold_fix_budget <= 0:
                    break
                tid = int(t.get("id") or 0)
                if tid <= 0:
                    continue
                try:
                    ttags = get_task_tags(tid)
                except Exception:
                    continue
                if normalize_plain_hold(tid, ttags):
                    hold_fix_budget -= 1

        # If a worker finished and produced artifacts but the card was moved to Blocked
        # (e.g. missing-worker/thrash), promote it to Review so the pipeline can continue.
        completed_blocked_ids: List[int] = []
        if budget > 0 and blocked_tasks:
            for bt, bsl_id in sorted(blocked_tasks, key=sort_key):
                if budget <= 0:
                    break
                bid = int(bt.get("id"))
                btitle = task_title(bt)
                try:
                    btags = get_task_tags(bid)
                except Exception:
                    btags = []

                lower = {t.lower() for t in (btags or [])}
                if not (
                    TAG_PAUSED_MISSING_WORKER in lower
                    or THRASH_PAUSE_TAG in lower
                    or TAG_PAUSED_STALE_WORKER in lower
                ):
                    continue

                entry = worker_entry_for(bid, workers_by_task)
                done_payload = worker_done_from_entry(entry)
                if not done_payload:
                    continue
                if not (done_payload.get("ok") and done_payload.get("patchExists") and done_payload.get("commentExists")):
                    continue

                if dry_run:
                    actions.append(f"Would move Blocked #{bid} ({btitle}) -> Review (worker output complete)")
                else:
                    move_task(pid, bid, int(col_review["id"]), 1, int(bsl_id))
                    record_action(bid)
                    remove_tags(
                        bid,
                        [
                            TAG_REVIEW_PASS,
                            TAG_REVIEW_REWORK,
                            TAG_REVIEW_BLOCKED_WIP,
                            TAG_REVIEW_ERROR,
                            TAG_REVIEW_INFLIGHT,
                        ],
                    )
                    add_tags(bid, [TAG_REVIEW_AUTO, TAG_REVIEW_PENDING])
                    clear_paused_tags(bid)
                    comment_path = str(done_payload.get("commentPath") or "")
                    comment_text = read_text(comment_path, 20000).strip() if comment_path else ""
                    if comment_text:
                        add_comment(bid, comment_text)
                    if WORKER_LEASES_ENABLED:
                        try:
                            lease = load_lease(bid)
                            if lease:
                                archive_lease_dir(bid, lease.get("leaseId"))
                        except Exception:
                            pass
                    actions.append(f"Moved Blocked #{bid} ({btitle}) -> Review (worker output complete)")
                    tmux_kill_window(f"worker-{bid}")

                completed_blocked_ids.append(bid)
                budget -= 1

        # Auto-advance WIP tasks when a worker run writes done.json.
        completed_wip_ids: List[int] = []
        if budget > 0 and wip_tasks:
            for wt, wsl_id in sorted(wip_tasks, key=sort_key):
                if budget <= 0:
                    break
                wid = int(wt.get("id"))
                wtitle = task_title(wt)
                entry = worker_entry_for(wid, workers_by_task)
                done_payload = worker_done_from_entry(entry)
                if not done_payload:
                    continue

                ok = bool(done_payload.get("ok"))
                patch_exists = bool(done_payload.get("patchExists"))
                comment_exists = bool(done_payload.get("commentExists"))
                try:
                    patch_bytes = int(done_payload.get("patchBytes") or 0)
                except Exception:
                    patch_bytes = 0
                patch_path = str(done_payload.get("patchPath") or "") if patch_exists else ""
                comment_path = str(done_payload.get("commentPath") or "") if comment_exists else ""

                if not (ok and patch_exists and comment_exists and patch_bytes > 0 and patch_path and comment_path):
                    # Worker finished but did not produce usable artifacts; keep it out of Review to avoid thrash.
                    reason = "worker finished without usable artifacts"
                    if not ok:
                        reason = f"worker exited non-zero (see done.json)"
                    if patch_exists and patch_bytes == 0:
                        reason = "worker produced empty patch"
                    try:
                        wtags = get_task_tags(wid)
                    except Exception:
                        wtags = []
                    critical_wip = is_critical(wtags)
                    if dry_run:
                        if critical_wip:
                            actions.append(
                                f"Would keep CRITICAL WIP #{wid} ({wtitle}) in WIP; tagged paused:artifact: {reason}"
                            )
                        else:
                            actions.append(
                                f"Would keep WIP #{wid} ({wtitle}) in Backlog; tagged blocked:artifact: {reason}"
                            )
                    else:
                        if critical_wip:
                            # Do not demote critical cards out of WIP on worker failure. Pause them in-place and
                            # require explicit human intervention (unpause / rerun) to avoid burn+thrash.
                            add_tags(wid, [TAG_PAUSED, TAG_PAUSED_ARTIFACT])
                            remove_tags(wid, [TAG_REVIEW_PENDING, TAG_REVIEW_INFLIGHT])
                            add_comment(
                                wid,
                                "Worker finished without usable artifacts. This CRITICAL card has been paused in WIP "
                                f"(tagged {TAG_PAUSED_ARTIFACT}) to prevent respawn thrash.\n"
                                f"Reason: {reason}\n"
                                "Next: inspect the worker log/done.json, fix env/tooling, then remove paused tags to rerun.",
                            )
                        else:
                            tag_blocked_and_keep_in_backlog(
                                wid,
                                int(wsl_id),
                                wtitle,
                                reason,
                                TAG_BLOCKED_ARTIFACT,
                                from_label="WIP",
                            )
                        workers_by_task.pop(str(wid), None)
                        workers_by_task.pop(wid, None)
                        tmux_kill_window(f"worker-{wid}")
                    budget -= 1
                    continue
                if dry_run:
                    actions.append(f"Would move WIP #{wid} ({wtitle}) -> Review (worker output complete)")
                else:
                    move_task(pid, wid, int(col_review["id"]), 1, wsl_id)
                    record_action(wid)
                    # Mark this review as auto-managed.
                    remove_tags(
                        wid,
                        [
                            TAG_REVIEW_PASS,
                            TAG_REVIEW_REWORK,
                            TAG_REVIEW_BLOCKED_WIP,
                            TAG_REVIEW_ERROR,
                            TAG_REVIEW_INFLIGHT,
                        ],
                    )
                    add_tags(wid, [TAG_REVIEW_AUTO, TAG_REVIEW_PENDING])
                    clear_paused_tags(wid)
                    # Post the worker-prepared Kanboard comment (best-effort; avoids manual copy/paste).
                    comment_text = read_text(comment_path, 20000).strip()
                    if comment_text:
                        add_comment(wid, comment_text)
                    if isinstance(entry, dict):
                        entry["completedAtMs"] = now_ms()
                        entry["patchPath"] = patch_path
                        entry["commentPath"] = comment_path
                    actions.append(f"Moved WIP #{wid} ({wtitle}) -> Review (worker output complete)")
                    tmux_kill_window(f"worker-{wid}")
                completed_wip_ids.append(wid)
                budget -= 1

        if completed_wip_ids:
            wip_tasks = [(t, sl_id) for t, sl_id in wip_tasks if int(t.get("id")) not in completed_wip_ids]
            wip_count = len(wip_tasks)
            missing_worker_tasks = [
                (t, sl_id) for t, sl_id in missing_worker_tasks if int(t.get("id")) not in completed_wip_ids
            ]
            stale_worker_ids.difference_update(set(completed_wip_ids))
            invalidate_wip_active_count()

        # Reconcile WIP tasks missing worker handles: spawn or pause deterministically.
        paused_missing_worker_ids: List[int] = []
        if budget > 0 and missing_worker_tasks:
            for wt, wsl_id in sorted(missing_worker_tasks, key=sort_key):
                if budget <= 0:
                    break
                wid = int(wt.get("id"))
                wtitle = task_title(wt)
                try:
                    wtags = get_task_tags(wid)
                    wfull = get_task(wid)
                    wdesc = (wfull.get("description") or "")
                except Exception:
                    wtags = []
                    wdesc = ""

                repo_ok, repo_key, repo_path, _source = resolve_repo_for_task(
                    wid, wtitle, wtags, wdesc, require_explicit=True
                )
                is_critical_wip = is_critical(wtags) and not is_held(wtags)
                spawn_allowed = MISSING_WORKER_POLICY == "spawn" or is_critical_wip
                label = "critical WIP" if is_critical_wip else "WIP"
                spawned = False
                if spawn_allowed and repo_path is not None:
                    if dry_run:
                        actions.append(f"Would spawn worker for {label} #{wid} ({wtitle})")
                        if not WORKER_SPAWN_CMD:
                            actions.append(f"Would pause {label} #{wid} ({wtitle}) -> Paused (missing worker handle)")
                            paused_missing_worker_ids.append(wid)
                        budget -= 1
                        continue
                    ok, reason = ensure_worker_handle_for_task(wid, repo_key, repo_path)
                    if ok:
                        actions.append(f"Spawned worker for {label} #{wid} ({wtitle})")
                        budget -= 1
                        spawned = True
                    else:
                        # If the provider is unavailable, don't auto-pause/move; just tag and stop spawning.
                        if isinstance(reason, dict) and reason.get("kind") == "provider-blocked":
                            budget -= 1
                            continue

                if spawned:
                    continue

                reason = "missing worker handle"
                if not repo_ok and not repo_path:
                    reason = "missing worker handle + repo mapping"
                if pause_missing_worker(wid, wsl_id, wtitle, reason, label=label):
                    budget -= 1
                paused_missing_worker_ids.append(wid)
                if budget <= 0:
                    break

        # NOTE: pause is tag-based; tasks remain in WIP, so do not remove from wip_tasks/wip_count.
        if paused_missing_worker_ids:
            invalidate_wip_active_count()

        if missing_worker_tasks and budget <= 0:
            remaining = []
            for t, _sl_id in missing_worker_tasks:
                tid = int(t.get("id"))
                if tid in paused_missing_worker_ids:
                    continue
                entry = worker_entry_for(tid, workers_by_task)
                if worker_handle(entry):
                    continue
                remaining.append(tid)
            if remaining:
                tail_ids = ", ".join("#" + str(x) for x in sorted(remaining)[:5])
                errors.append(f"drift: WIP tasks missing worker handle (action budget exhausted): {tail_ids}")

        # Watchdog: if a WIP worker pid is alive but its log has been stale for too long, pause the card
        # to prevent WIP deadlocks. (We avoid auto-respawning when the pid is alive to prevent duplicate workers.)
        paused_stale_worker_ids: List[int] = []
        if budget > 0 and stale_worker_ids and WORKER_LOG_STALE_ACTION == "pause":
            wip_by_id = {int(t.get("id")): (t, sl_id) for t, sl_id in wip_tasks}
            for wid in sorted(stale_worker_ids):
                if budget <= 0:
                    break
                if wid not in wip_by_id:
                    continue
                wt, _wsl_id = wip_by_id[wid]
                wtitle = task_title(wt)
                try:
                    wtags = get_task_tags(wid)
                except Exception:
                    wtags = []
                if has_tag(wtags, TAG_PAUSED_STALE_WORKER):
                    continue
                if dry_run:
                    actions.append(f"Would tag WIP #{wid} ({wtitle}) as paused:stale-worker (worker log stale)")
                else:
                    record_action(wid)
                    add_tags(wid, [TAG_PAUSED, TAG_PAUSED_STALE_WORKER])
                    actions.append(f"Tagged WIP #{wid} ({wtitle}) as paused:stale-worker (worker log stale)")
                paused_stale_worker_ids.append(wid)
                budget -= 1
        if paused_stale_worker_ids:
            invalidate_wip_active_count()
        if stale_worker_ids and budget <= 0 and not paused_stale_worker_ids:
            tail_ids = ", ".join("#" + str(x) for x in sorted(stale_worker_ids)[:5])
            errors.append(f"watchdog: WIP worker log stale (action budget exhausted): {tail_ids}")

        # ---------------------------------------------------------------------
        # CRITICAL MODE (preemptive)
        # ---------------------------------------------------------------------
        # If any non-Done task is tagged `critical`, it takes absolute priority.
        # While a critical is actively in WIP, pause all non-critical WIP work and do not
        # pull/start other work. Once the critical reaches Review (waiting on human),
        # resume normal throughput so the pipeline can't deadlock on a single critical card.
        #
        # Note: We still respect dependencies/exclusive constraints; if the critical
        # task cannot start, we do NOT pause everything (avoids deadlock) and we
        # emit an error instead.

        # Critical queue (active + queued) computed earlier to keep drift checks consistent.

        # If multiple critical tasks exist, auto-queue the non-active ones.
        # This prevents the monitor from treating multiple critical tags as an invariant violation,
        # while still preserving the priority order on the board.
        if active_critical is not None:
            active_id = int(active_critical[0].get("id") or 0)

            # Ensure the active critical isn't fenced by our own queue tag.
            # Only `hold:queued-critical` is orchestrator-owned; do NOT override a manual `hold`.
            try:
                atags = get_task_tags(active_id)
            except Exception:
                atags = []
            if has_tag(atags, TAG_HOLD_QUEUED_CRITICAL):
                if dry_run:
                    actions.append(f"Would untag hold/hold:queued-critical for active critical #{active_id}")
                else:
                    remove_tags(active_id, [TAG_HOLD, TAG_HOLD_QUEUED_CRITICAL])
                    record_action(active_id)
                    actions.append(f"Unqueued active critical #{active_id} (removed hold:queued-critical)")

        # If multiple critical tasks exist, auto-queue the non-active ones.
        if active_critical is not None and len(critical_candidates) > 1:
            active_id = int(active_critical[0].get("id") or 0)
            for t, _sl_id, _col_id in critical_candidates:
                tid = int(t.get("id"))
                if tid == active_id:
                    continue
                if budget <= 0:
                    break
                ttitle = task_title(t)
                try:
                    ttags = get_task_tags(tid)
                except Exception:
                    ttags = []
                if has_tag(ttags, TAG_HOLD_QUEUED_CRITICAL):
                    continue
                if is_held(ttags):
                    continue
                if dry_run:
                    actions.append(f"Would tag queued critical #{tid} ({ttitle}) as hold:queued-critical")
                else:
                    # Queue fence is hold:queued-critical (do not add plain `hold`; keep it human-only).
                    add_tags(tid, [TAG_HOLD_QUEUED_CRITICAL])
                    record_action(tid)
                    actions.append(f"Queued critical #{tid} ({ttitle}) (tagged hold:queued-critical)")
                budget -= 1

        # ---------------------------------------------------------------------
        # REVIEW AUTOMATION
        # ---------------------------------------------------------------------
        review_rework_queue: List[Tuple[Dict[str, Any], int]] = []
        for rt, rsl_id in sorted(review_tasks, key=sort_key):
            rid = int(rt.get("id"))
            # Only freeze non-critical reviews when a critical is actively exclusive (normally: in WIP).
            # If a critical is blocked in Backlog/Ready/Review, we must continue normal review throughput
            # or the pipeline can deadlock (e.g., critical depends on another card that needs review).
            if critical_exclusive and active_critical is not None:
                active_id = int(active_critical[0].get("id") or 0)
                if rid != active_id:
                    continue
            rtitle = task_title(rt)
            try:
                rtags = get_task_tags(rid)
            except Exception:
                rtags = []

            # Auto-heal provider blocks: if a card was auto-blocked due to auth/quota
            # and the provider is healthy again, clear the blocked tags so review can resume.
            lower_rt = {t.lower() for t in (rtags or [])}
            if TAG_AUTO_BLOCKED in lower_rt and (TAG_BLOCKED_AUTH in lower_rt or TAG_BLOCKED_QUOTA in lower_rt):
                provider = infer_preflight_provider("reviewer", REVIEWER_SPAWN_CMD) if REVIEWER_SPAWN_CMD else "claude"
                if provider:
                    ok, _cat, _msg = provider_preflight_gate(state, provider=provider, errors=errors)
                    if ok:
                        if dry_run:
                            actions.append(f"Would clear blocked auth/quota tags for Review #{rid} ({rtitle})")
                        else:
                            remove_tags(rid, [TAG_AUTO_BLOCKED, TAG_BLOCKED_AUTH, TAG_BLOCKED_QUOTA])
                            try:
                                rtags = get_task_tags(rid)
                            except Exception:
                                rtags = [t for t in rtags if str(t).lower() not in (TAG_AUTO_BLOCKED, TAG_BLOCKED_AUTH, TAG_BLOCKED_QUOTA)]

            if is_held(rtags):
                continue
            if has_tag(rtags, TAG_REVIEW_SKIP):
                continue

            patch_path = resolve_patch_path_for_task(rid)
            current_revision = compute_patch_revision(patch_path)
            stored_result = review_results_by_task.get(str(rid))
            stored_revision = extract_review_revision(stored_result)
            rerun_requested = has_tag(rtags, TAG_REVIEW_RERUN) or has_tag(rtags, TAG_REVIEW_RETRY)
            stored_matches = review_revision_matches(current_revision, stored_revision)

            stale_result = stored_result is not None and (rerun_requested or not stored_matches)
            if stale_result:
                if dry_run:
                    actions.append(f"Would clear stale review result for Review #{rid} ({rtitle})")
                else:
                    review_results_by_task.pop(str(rid), None)
                stored_result = None

            entry = worker_entry_for(rid, reviewers_by_task)
            entry_revision = extract_review_revision(entry)
            entry_matches = review_revision_matches(current_revision, entry_revision)
            entry_mismatch = entry is not None and (rerun_requested or not entry_matches)
            if entry_mismatch:
                if dry_run:
                    actions.append(f"Would reset reviewer handle for Review #{rid} ({rtitle})")
                else:
                    reviewers_by_task.pop(str(rid), None)
                entry = None

            if rerun_requested or stale_result:
                if dry_run:
                    actions.append(f"Would reset review state for Review #{rid} ({rtitle})")
                else:
                    remove_tags(
                        rid,
                        [
                            TAG_REVIEW_PASS,
                            TAG_REVIEW_REWORK,
                            TAG_NEEDS_REWORK,
                            TAG_REVIEW_ERROR,
                            TAG_REVIEW_INFLIGHT,
                            TAG_REVIEW_PENDING,
                            TAG_REVIEW_RERUN,
                            TAG_REVIEW_RETRY,
                        ],
                    )
                    add_tag(rid, TAG_REVIEW_PENDING)

            result_payload: Optional[Dict[str, Any]] = None
            if stored_result:
                result_payload = stored_result
            else:
                # Preferred: read structured JSON result emitted by the reviewer run.
                result_payload = reviewer_result_from_entry(entry)

                # Recovery: allow consuming a completed review even if we lost the reviewer handle
                # (e.g., orchestrator restart) by scanning the per-run review.json directory.
                #
                # IMPORTANT: If a human explicitly requested a rerun (review:retry/review:rerun),
                # we must NOT consume stale on-disk review.json/log markers. Otherwise a previous
                # BLOCKER result can immediately re-assert review:error and prevent reruns.
                if not result_payload and not rerun_requested:
                    result_payload = latest_reviewer_result_for_task(rid)

                # Back-compat: fall back to parsing the legacy review log marker.
                # Same rule as above: never consume legacy results when a rerun is requested.
                if not result_payload and not rerun_requested:
                    log_path = None
                    if isinstance(entry, dict):
                        log_path = entry.get("logPath")
                    if not log_path:
                        log_path = default_reviewer_log_path(rid)
                    result_payload = detect_review_result(rid, log_path)

                if result_payload:
                    result_revision = extract_review_revision(result_payload)
                    if not result_revision:
                        result_revision = extract_review_revision(entry)
                    if not review_revision_matches(current_revision, result_revision):
                        result_payload = None
                    else:
                        # Ignore obviously broken review runs caused by missing Kanboard task context
                        # (e.g., reviewer was launched without auth/env). These runs tend to claim
                        # the task has "no title/description" even when we clearly have a title.
                        if result_payload and (rtitle or "").strip():
                            notes = str(result_payload.get("notes") or "")
                            critical_items = result_payload.get("critical_items") or []
                            if not isinstance(critical_items, list):
                                critical_items = []
                            blob = (notes + "\n" + "\n".join([str(x) for x in critical_items])).lower()
                            if any(
                                needle in blob
                                for needle in (
                                    "no title or description",
                                    "has no title or description",
                                    "task context missing",
                                    "missing task context",
                                    "context unavailable",
                                )
                            ):
                                # Only ignore these if the patch actually contains a diff.
                                # If the patch is empty/no-op, we want to process the BLOCKER
                                # and park the card as blocked:artifact.
                                if patch_has_diff(patch_path):
                                    result_payload = None

                # If the reviewer run appears stuck/crashed (no result for too long), clear it so we can respawn.
                if not result_payload and isinstance(entry, dict) and REVIEW_RUN_TIMEOUT_MIN > 0:
                    try:
                        started_at_ms = int(entry.get("startedAtMs") or 0) or None
                    except Exception:
                        started_at_ms = None
                    if started_at_ms:
                        timeout_ms = REVIEW_RUN_TIMEOUT_MIN * 60 * 1000
                        if now_ms() - started_at_ms > timeout_ms:
                            if not dry_run:
                                reviewers_by_task.pop(str(rid), None)
                                reviewers_by_task.pop(rid, None)
                            entry = None

            # If the reviewer is broken (auth/quota) we mark review:error and only retry
            # when a human explicitly asks (review:rerun) to avoid infinite loops.
            # Still allow consuming an already-written result_payload to unblock the pipeline.
            if has_tag(rtags, TAG_REVIEW_ERROR) and not rerun_requested and not stored_result and not result_payload:
                # ensure we don't leave it stuck "inflight"
                if not dry_run:
                    remove_tags(rid, [TAG_REVIEW_INFLIGHT, TAG_REVIEW_PENDING])
                continue

            # If the reviewer exited and produced no result payload, treat this as a manual-review blocker.
            # We do NOT automatically respawn in a tight loop; instead we park with review:error so a human
            # can inspect the log/run directory and request an explicit retry.
            if not result_payload and isinstance(entry, dict):
                h = worker_handle(entry)
                if h and not reviewer_is_alive(h):
                    # Small grace window: allow the reviewer to start + flush output even if the tmux window closes fast.
                    try:
                        started_at_ms = int(entry.get("startedAtMs") or 0) or 0
                    except Exception:
                        started_at_ms = 0
                    if started_at_ms and (now_ms() - started_at_ms) < 15_000:
                        pass
                    else:
                        if dry_run:
                            actions.append(f"Would tag Review #{rid} ({rtitle}) as review:error (reviewer exited w/out result)")
                        else:
                            # If the reviewer died due to auth/quota, treat it as a provider outage so we don't
                            # keep spawning new reviewers and burning usage.
                            try:
                                rlog_path = str(entry.get("logPath") or "") if isinstance(entry, dict) else ""
                            except Exception:
                                rlog_path = ""
                            if not rlog_path:
                                rlog_path = default_reviewer_log_path(rid)
                            diag = diagnose_worker_failure(rid, rlog_path)
                            category = str(diag.get("category") or "")
                            if category in ("auth", "quota"):
                                provider_force_block(
                                    state,
                                    provider="claude",
                                    category=category,
                                    message=f"Detected from reviewer log (Review #{rid})",
                                    errors=errors,
                                )
                                reason_tag = TAG_BLOCKED_QUOTA if category == "quota" else TAG_BLOCKED_AUTH
                                try:
                                    existing = get_task_tags(rid)
                                except Exception:
                                    existing = []
                                lower = {t.lower() for t in (existing or [])}
                                if reason_tag.lower() not in lower or TAG_AUTO_BLOCKED.lower() not in lower:
                                    add_tags(rid, [reason_tag, TAG_AUTO_BLOCKED])
                                    record_action(rid)

                            add_tag(rid, TAG_REVIEW_ERROR)
                            remove_tags(rid, [TAG_REVIEW_INFLIGHT, TAG_REVIEW_PENDING])
                            msg = (
                                "Reviewer exited without producing a review result.\n"
                                "This card is parked with review:error to avoid thrash.\n"
                                f"- handle: {h}\n"
                                f"- logPath: {entry.get('logPath')}\n"
                                f"- runDir: {entry.get('runDir')}\n"
                                f"- expected resultPath: {entry.get('resultPath')}\n"
                                "To retry after fixing the reviewer environment, add tag review:rerun (or review:retry)."
                            )
                            add_comment(rid, msg)
                            reviewers_by_task.pop(str(rid), None)
                            reviewers_by_task.pop(rid, None)
                        continue

            # Only spawn if we don't already have a usable result in the log.
            if not result_payload and not worker_handle(entry) and not stored_result:
                if dry_run:
                    actions.append(f"Would spawn reviewer for Review #{rid} ({rtitle})")
                else:
                    # Mark this Review card as auto-reviewed by default.
                    if not has_tag(rtags, TAG_REVIEW_AUTO):
                        add_tag(rid, TAG_REVIEW_AUTO)
                    add_tag(rid, TAG_REVIEW_PENDING)
                    remove_tag(rid, TAG_REVIEW_ERROR)

                    try:
                        full = get_task(rid)
                        rdesc = (full.get("description") or "")
                    except Exception:
                        rdesc = ""
                    _repo_ok, repo_key, repo_path, _source = resolve_repo_for_task(
                        rid, rtitle, rtags, rdesc, require_explicit=True
                    )
                    spawned_ok, spawned_reason = ensure_reviewer_handle_for_task(
                        rid, repo_key, repo_path, patch_path, current_revision
                    )
                    if spawned_ok:
                        # Reset spawn failure counter on success.
                        reviewer_spawn_failures_by_task.pop(str(rid), None)
                        # Atomically transition tags to inflight (avoid brief pending+inflight overlap).
                        try:
                            current_tags = get_task_tags(rid)
                        except Exception:
                            current_tags = list(rtags)
                        new_tags: List[str] = []
                        seen: set[str] = set()
                        for t in current_tags:
                            tl = str(t).lower()
                            if tl == TAG_REVIEW_PENDING.lower():
                                continue
                            if tl in seen:
                                continue
                            seen.add(tl)
                            new_tags.append(str(t))
                        if TAG_REVIEW_INFLIGHT.lower() not in seen:
                            new_tags.append(TAG_REVIEW_INFLIGHT)
                        set_task_tags(pid, rid, new_tags)
                        actions.append(f"Spawned reviewer for Review #{rid} ({rtitle})")
                    else:
                        # Provider preflight failures are global and should not escalate per-card to review:error.
                        if isinstance(spawned_reason, dict) and spawned_reason.get("kind") == "provider-blocked":
                            continue
                        # Escalate repeated spawn failures to review:error so we don't sit in review:pending forever.
                        rec = reviewer_spawn_failures_by_task.get(str(rid)) or {}
                        if not isinstance(rec, dict):
                            rec = {}
                        try:
                            fail_count = int(rec.get("count") or 0)
                        except Exception:
                            fail_count = 0
                        fail_count += 1
                        rec["count"] = fail_count
                        rec["lastFailedAtMs"] = now_ms()
                        reviewer_spawn_failures_by_task[str(rid)] = rec
                        if fail_count >= 3:
                            add_tag(rid, TAG_REVIEW_ERROR)
                            remove_tags(rid, [TAG_REVIEW_INFLIGHT, TAG_REVIEW_PENDING])
                            msg = (
                                "Reviewer spawn repeatedly failed.\n"
                                f"- attempts: {fail_count}\n"
                                "This card is parked with review:error to avoid thrash.\n"
                                "After fixing the reviewer environment, add tag review:rerun (or review:retry) to retry."
                            )
                            add_comment(rid, msg)
                            actions.append(f"Reviewer spawn failed {fail_count}x for Review #{rid} ({rtitle}); tagged review:error")

            if result_payload and not stored_result:
                score = int(result_payload.get("score") or 0)
                verdict = str(result_payload.get("verdict") or "").upper()
                notes = result_payload.get("notes")
                if dry_run:
                    actions.append(f"Would comment review results on Review #{rid} ({rtitle})")
                else:
                    critical_items = result_payload.get("critical_items") or []
                    if not isinstance(critical_items, list):
                        critical_items = []

                    review_revision = extract_review_revision(result_payload) or current_revision or extract_review_revision(entry)
                    short_rev = review_revision[:12] if review_revision else None
                    header = "Opus review checklist"
                    if short_rev:
                        header += f" (rev {short_rev})"

                    score_ok = score >= REVIEW_THRESHOLD
                    verdict_ok = verdict == "PASS"

                    comment_lines = [
                        header,
                        "- [x] Review completed",
                        f"- [{'x' if score_ok else ' '}] Score >= {REVIEW_THRESHOLD} (score {score})",
                        f"- [{'x' if verdict_ok else ' '}] Verdict PASS (verdict {verdict})",
                    ]
                    if critical_items:
                        comment_lines.append(f"- [ ] Critical items found ({len(critical_items)})")
                        for item in critical_items[:10]:
                            comment_lines.append(f"  - {item}")
                    else:
                        comment_lines.append("- [x] No critical items found")
                    decision = "approve" if (score_ok and verdict_ok and not critical_items) else "request-changes"
                    comment_lines.append(f"- Recommendation: {decision}")
                    comment_lines.append("- Risks:")
                    if critical_items:
                        for item in critical_items[:10]:
                            comment_lines.append(f"  - {item}")
                    else:
                        comment_lines.append("  - None noted")
                    comment_lines.append("- Correctness:")
                    if notes:
                        comment_lines.append(f"  - {notes}")
                    else:
                        comment_lines.append("  - No correctness notes provided")
                    minor_items = result_payload.get("minor_items") or []
                    if isinstance(minor_items, list) and minor_items:
                        comment_lines.append("- Minor items:")
                        for item in minor_items[:10]:
                            comment_lines.append(f"  - {item}")
                    fix_plan = result_payload.get("fix_plan") or []
                    if isinstance(fix_plan, list) and fix_plan:
                        comment_lines.append("- Fix plan:")
                        for item in fix_plan[:10]:
                            comment_lines.append(f"  - {item}")
                    comment_lines.append("- Tests to run/add:")
                    comment_lines.append("  - Run: python3 -m unittest discover -s tests")
                    if review_revision:
                        comment_lines.append(f"- Review revision: `{review_revision}`")
                    add_comment(rid, "\n".join(comment_lines))
                    review_results_by_task[str(rid)] = {
                        "score": score,
                        "verdict": verdict,
                        "notes": notes,
                        "critical_items": critical_items,
                        "minor_items": result_payload.get("minor_items") or [],
                        "fix_plan": result_payload.get("fix_plan") or [],
                        "commentedAtMs": now_ms(),
                        "logPath": result_payload.get("logPath"),
                        "reviewRevision": review_revision,
                        "patchPath": patch_path,
                    }

            if result_payload:
                score = int(result_payload.get("score") or 0)
                verdict = str(result_payload.get("verdict") or "").upper()
                critical_items = result_payload.get("critical_items") or []
                if not isinstance(critical_items, list):
                    critical_items = []

                needs_rework = review_needs_rework(score, verdict, critical_items, REVIEW_THRESHOLD)

                # If the reviewer itself is broken (auth/quota), don't thrash the card back into WIP.
                # Keep it in Review with review:error so a human can fix the reviewer environment.
                manual_review_blocker = False
                if verdict == "BLOCKER":
                    notes = str(result_payload.get("notes") or "")
                    blob = (notes + "\n" + "\n".join([str(x) for x in critical_items])).lower()
                    if any(
                        needle in blob
                        for needle in (
                            "invalid api key",
                            "please run /login",
                            "unauthorized",
                            "forbidden",
                            "authentication",
                            "quota",
                            "rate limit",
                        )
                    ):
                        manual_review_blocker = True

                # Clear inflight/pending once we have a result.
                if dry_run:
                    actions.append(f"Would clear review:inflight/review:pending for Review #{rid} ({rtitle})")
                else:
                    remove_tags(rid, [TAG_REVIEW_INFLIGHT, TAG_REVIEW_PENDING])

                if manual_review_blocker:
                    if dry_run:
                        actions.append(f"Would tag Review #{rid} ({rtitle}) as review:error (reviewer auth/quota)")
                    else:
                        add_tag(rid, TAG_REVIEW_ERROR)
                        remove_tags(rid, [TAG_REVIEW_PASS, TAG_REVIEW_REWORK, TAG_NEEDS_REWORK, TAG_REVIEW_BLOCKED_WIP])
                        tmux_kill_window(f"review-{rid}")
                    continue

                # Certain BLOCKER outcomes are not actionable by "rerun worker" and will just
                # cause Review <-> WIP ping-pong. Park them in Backlog with a durable blocked tag.
                if verdict == "BLOCKER":
                    non_actionable_tag = None
                    non_actionable_reason = None

                    empty_patch = not patch_has_diff(patch_path)

                    notes = str(result_payload.get("notes") or "")
                    blob = (notes + "\n" + "\n".join([str(x) for x in critical_items])).lower()

                    if empty_patch or "patch file is empty" in blob or "no patch content" in blob:
                        non_actionable_tag = TAG_BLOCKED_ARTIFACT
                        non_actionable_reason = "empty/missing patch artifact (no changes to implement/review)"
                    elif "task context missing" in blob or "missing task context" in blob or "no title or description" in blob:
                        non_actionable_tag = TAG_BLOCKED_CONTEXT
                        non_actionable_reason = "task context missing (title/description required)"

                    if non_actionable_tag and non_actionable_reason:
                        if dry_run:
                            actions.append(
                                f"Would keep Review #{rid} ({rtitle}) in Backlog; tagged {non_actionable_tag}: {non_actionable_reason}"
                            )
                        else:
                            remove_tags(
                                rid,
                                [
                                    TAG_REVIEW_INFLIGHT,
                                    TAG_REVIEW_PENDING,
                                    TAG_REVIEW_ERROR,
                                    TAG_REVIEW_PASS,
                                    TAG_REVIEW_REWORK,
                                    TAG_NEEDS_REWORK,
                                    TAG_REVIEW_BLOCKED_WIP,
                                    TAG_REVIEW_RERUN,
                                    TAG_REVIEW_RETRY,
                                ],
                            )
                            tag_blocked_and_keep_in_backlog(
                                rid,
                                int(rsl_id),
                                rtitle,
                                non_actionable_reason,
                                non_actionable_tag,
                                from_label="Review",
                            )
                            # Drop any cached result/handle; the card is no longer in Review.
                            review_results_by_task.pop(str(rid), None)
                            reviewers_by_task.pop(str(rid), None)
                        continue

                if needs_rework:
                    if dry_run:
                        actions.append(
                            f"Would tag Review #{rid} ({rtitle}) as review:rework (score {score}, verdict {verdict})"
                        )
                    else:
                        add_tags(rid, [TAG_REVIEW_REWORK, TAG_NEEDS_REWORK])
                        remove_tags(rid, [TAG_REVIEW_PASS, TAG_REVIEW_BLOCKED_WIP, TAG_REVIEW_ERROR])
                    review_rework_queue.append((rt, rsl_id))
                    tmux_kill_window(f"review-{rid}")
                else:
                    if dry_run:
                        actions.append(f"Would tag Review #{rid} ({rtitle}) as review:pass")
                    else:
                        add_tag(rid, TAG_REVIEW_PASS)
                        remove_tags(rid, [TAG_REVIEW_REWORK, TAG_NEEDS_REWORK, TAG_REVIEW_BLOCKED_WIP, TAG_REVIEW_ERROR])

                    # Auto-advance Review -> Documentation (preferred) or -> Done on pass (configurable).
                    if REVIEW_AUTO_DONE and budget > 0:
                        if col_docs is not None:
                            if dry_run:
                                actions.append(f"Would move Review #{rid} ({rtitle}) -> Documentation (review pass)")
                            else:
                                move_task(pid, rid, int(col_docs["id"]), 1, int(rsl_id))
                                record_action(rid)
                                # Docs flow tags are orchestrator-owned. Clear any stale docs state and mark pending.
                                remove_tags(rid, [TAG_DOC_COMPLETED, TAG_DOC_SKIP, TAG_DOC_INFLIGHT])
                                add_tags(rid, [TAG_DOC_AUTO, TAG_DOC_PENDING])
                                actions.append(f"Moved Review #{rid} ({rtitle}) -> Documentation (review pass)")
                        else:
                            if dry_run:
                                actions.append(f"Would move Review #{rid} ({rtitle}) -> Done (review pass)")
                            else:
                                move_task(pid, rid, int(col_done["id"]), 1, int(rsl_id))
                                record_action(rid)
                                actions.append(f"Moved Review #{rid} ({rtitle}) -> Done (review pass)")
                        budget -= 1
                    tmux_kill_window(f"review-{rid}")

        # Move rework items back to WIP before pulling new Ready work.
        if review_rework_queue and budget > 0:
            for rt, rsl_id in sorted(review_rework_queue, key=sort_key):
                if budget <= 0:
                    break
                rid = int(rt.get("id"))
                try:
                    rtags = get_task_tags(rid)
                except Exception:
                    rtags = []
                is_critical_review = is_critical(rtags)
                if wip_active_count() >= WIP_LIMIT and not is_critical_review:
                    # Can't move yet; mark it so we keep prioritizing it.
                    if not has_tag(rtags, TAG_REVIEW_BLOCKED_WIP):
                        if dry_run:
                            actions.append(f"Would tag Review #{rid} as review:blocked:wip (waiting for WIP capacity)")
                        else:
                            add_tag(rid, TAG_REVIEW_BLOCKED_WIP)
                    continue

                if critical_exclusive and active_critical_id is not None:
                    if rid != int(active_critical_id):
                        continue
                rtitle = task_title(rt)
                if is_held(rtags):
                    continue

                # Thrash guard: if the same patch revision keeps re-failing review, stop looping.
                patch_path = resolve_patch_path_for_task(rid)
                current_revision = compute_patch_revision(patch_path) or ""
                hist = review_rework_history_by_task.get(str(rid))
                if not isinstance(hist, list):
                    hist = []
                window_ms = THRASH_WINDOW_MIN * 60 * 1000
                nowm = now_ms()
                pruned: list[dict[str, Any]] = []
                for e in hist:
                    if not isinstance(e, dict):
                        continue
                    try:
                        at = int(e.get("atMs") or 0)
                    except Exception:
                        at = 0
                    if at and (nowm - at) <= window_ms:
                        pruned.append(e)
                hist = pruned
                same_rev = 0
                for e in hist:
                    if not isinstance(e, dict):
                        continue
                    if str(e.get("reviewRevision") or "") == current_revision:
                        same_rev += 1
                if THRASH_MAX_RESPAWNS > 0 and same_rev >= THRASH_MAX_RESPAWNS:
                    if dry_run:
                        actions.append(
                            f"Would keep Review #{rid} ({rtitle}) in Backlog; tagged {TAG_BLOCKED_THRASH} (review thrash guard)"
                        )
                    else:
                        remove_tags(
                            rid,
                            [
                                TAG_REVIEW_INFLIGHT,
                                TAG_REVIEW_PENDING,
                                TAG_REVIEW_ERROR,
                                TAG_REVIEW_PASS,
                                TAG_REVIEW_REWORK,
                                TAG_NEEDS_REWORK,
                                TAG_REVIEW_BLOCKED_WIP,
                                TAG_REVIEW_RERUN,
                                TAG_REVIEW_RETRY,
                            ],
                        )
                        tag_blocked_and_keep_in_backlog(
                            rid,
                            int(rsl_id),
                            rtitle,
                            "review thrash guard: same patch keeps failing review",
                            TAG_BLOCKED_THRASH,
                            from_label="Review",
                        )
                        review_results_by_task.pop(str(rid), None)
                        reviewers_by_task.pop(str(rid), None)
                        review_rework_history_by_task[str(rid)] = hist
                        record_action(rid)
                    budget -= 1
                    continue

                if dry_run:
                    actions.append(f"Would move Review #{rid} ({rtitle}) -> WIP (rework)")
                else:
                    reset_worker_state(rid)
                    # Record this rework attempt (for thrash guard + debugging).
                    last_result = review_results_by_task.get(str(rid)) if isinstance(review_results_by_task, dict) else None
                    entry: Dict[str, Any] = {"atMs": now_ms(), "reviewRevision": current_revision}
                    if isinstance(last_result, dict):
                        try:
                            entry["score"] = int(last_result.get("score") or 0)
                        except Exception:
                            pass
                        if last_result.get("verdict"):
                            entry["verdict"] = str(last_result.get("verdict"))
                    hist.append(entry)
                    review_rework_history_by_task[str(rid)] = hist

                    move_task(pid, rid, int(col_wip["id"]), 1, int(rsl_id))
                    record_action(rid)
                    remove_tags(rid, [TAG_REVIEW_BLOCKED_WIP, TAG_REVIEW_PASS, TAG_REVIEW_PENDING, TAG_REVIEW_INFLIGHT])
                    # Keep review:rework tag as a breadcrumb is optional; for now we clear it once it re-enters WIP.
                    remove_tags(rid, [TAG_REVIEW_REWORK, TAG_NEEDS_REWORK])
                    actions.append(f"Moved Review #{rid} ({rtitle}) -> WIP (rework)")
                wip_tasks.append((rt, rsl_id))
                wip_count += 1
                invalidate_wip_active_count()
                review_results_by_task.pop(str(rid), None)
                reviewers_by_task.pop(str(rid), None)
                workers_by_task.pop(str(rid), None)
                workers_by_task.pop(rid, None)
                budget -= 1

        # ---------------------------------------------------------------------
        # DOCUMENTATION FLOW (Review -> Documentation -> Done)
        # ---------------------------------------------------------------------
        # We no longer auto-create separate "Docs: (from #X)" companion cards.
        #
        # Instead, when the board has a Documentation column, a card must be marked as
        # docs:completed (or docs:skip) before it is allowed to move to Done.
        #
        # Tag policy:
        # - docs:pending is the default state when a card enters Documentation.
        # - docs:inflight is optional/human-driven (used as a signal that docs work started).
        # - docs:completed (or docs:skip) is the gate to Done.
        if col_docs is not None and budget > 0 and docs_tasks:
            active_critical_id = None
            if active_critical is not None:
                try:
                    active_critical_id = int(active_critical[0].get("id") or 0) or None
                except Exception:
                    active_critical_id = None

            for dt, dsl_id in sorted(docs_tasks, key=sort_key):
                if budget <= 0:
                    break
                did = int(dt.get("id"))
                dtitle = task_title(dt)

                # Same rule as review automation: only freeze docs throughput when a critical is
                # actively exclusive (normally: in WIP). Do not deadlock on a backlog-blocked critical.
                if critical_exclusive and active_critical_id is not None and did != int(active_critical_id):
                    continue

                try:
                    dtags = get_task_tags(did)
                except Exception:
                    dtags = []
                if is_held(dtags):
                    continue

                lower = {t.lower() for t in (dtags or [])}
                done_ready = (TAG_DOC_COMPLETED in lower) or (TAG_DOC_SKIP in lower)
                retry_requested = TAG_DOC_RETRY in lower

                # Docs retry: clear docs:error and allow respawn (explicit human intent).
                if retry_requested:
                    if dry_run:
                        actions.append(f"Would clear docs:error for Documentation #{did} ({dtitle}) (docs:retry)")
                    else:
                        # On retry we need to re-enter the normal docs spawn state machine.
                        # Ensure docs:pending is present so docs:auto can trigger a new docs worker.
                        remove_tags(did, [TAG_DOC_ERROR, TAG_DOC_RETRY])
                        add_tag(did, TAG_DOC_PENDING)
                        try:
                            dtags = get_task_tags(did)
                        except Exception:
                            dtags = list(dtags)
                        lower = {t.lower() for t in (dtags or [])}
                        done_ready = (TAG_DOC_COMPLETED in lower) or (TAG_DOC_SKIP in lower)

                if done_ready:
                    if dry_run:
                        actions.append(f"Would move Documentation #{did} ({dtitle}) -> Done (docs complete)")
                    else:
                        move_task(pid, did, int(col_done["id"]), 1, int(dsl_id))
                        record_action(did)
                        # Keep docs:completed/docs:skip as a durable breadcrumb; clear transitional tags.
                        remove_tags(did, [TAG_DOC_PENDING, TAG_DOC_INFLIGHT])
                        # Best-effort docs worker cleanup (if one was running or left stale state).
                        docs_workers_by_task.pop(str(did), None)
                        docs_workers_by_task.pop(did, None)
                        tmux_kill_window(f"docs-{did}")
                        actions.append(f"Moved Documentation #{did} ({dtitle}) -> Done (docs complete)")
                    budget -= 1
                    continue

                # If a docs worker run completed, consume it and advance deterministically.
                entry = worker_entry_for(did, docs_workers_by_task)

                # Docs can hang indefinitely (Codex CLI stalled, network issues, etc.). If the
                # tmux window is still alive but the run has exceeded DOCS_RUN_TIMEOUT_MIN and
                # done.json never appeared, treat the run as stale and respawn (anti-deadlock).
                #
                # We keep this restart bounded: after repeated timeouts we park the card in
                # docs:error to avoid burning usage.
                if isinstance(entry, dict):
                    done_path = entry.get("donePath") or entry.get("done_path") or ""
                    if done_path and not os.path.isfile(str(done_path)):
                        try:
                            started_at_ms = int(entry.get("startedAtMs") or 0) or None
                        except Exception:
                            started_at_ms = None
                        if started_at_ms and DOCS_RUN_TIMEOUT_MIN > 0:
                            timeout_ms = DOCS_RUN_TIMEOUT_MIN * 60 * 1000
                            if now_ms() - started_at_ms > timeout_ms:
                                if dry_run:
                                    actions.append(
                                        f"Would restart stale docs worker for Documentation #{did} ({dtitle}) "
                                        f"(run > {DOCS_RUN_TIMEOUT_MIN}m without done.json)"
                                    )
                                else:
                                    rec = docs_timeout_restarts_by_task.get(str(did)) or {}
                                    if not isinstance(rec, dict):
                                        rec = {}
                                    try:
                                        count = int(rec.get("count") or 0)
                                    except Exception:
                                        count = 0
                                    count += 1
                                    rec["count"] = count
                                    rec["lastAtMs"] = now_ms()
                                    docs_timeout_restarts_by_task[str(did)] = rec

                                    # Best-effort kill the hung window and drop bookkeeping.
                                    tmux_kill_window(f"docs-{did}")
                                    docs_workers_by_task.pop(str(did), None)
                                    docs_workers_by_task.pop(did, None)

                                    # Re-enter the spawn state machine.
                                    remove_tag(did, TAG_DOC_INFLIGHT)
                                    add_tag(did, TAG_DOC_PENDING)
                                    record_action(did)

                                    actions.append(
                                        f"Restarted stale docs worker for Documentation #{did} ({dtitle}) "
                                        f"(timeout {DOCS_RUN_TIMEOUT_MIN}m; attempt {count})"
                                    )

                                    if count >= 3:
                                        add_tag(did, TAG_DOC_ERROR)
                                        remove_tags(did, [TAG_DOC_PENDING, TAG_DOC_INFLIGHT])
                                        add_comment(
                                            did,
                                            "Docs worker appears to be hung (no done.json after timeout).\n"
                                            f"- attempts: {count}\n"
                                            "This card is parked with docs:error to avoid thrash.\n"
                                            "After fixing the docs worker environment, add tag docs:retry.",
                                        )
                                        actions.append(
                                            f"Docs worker timed out {count}x for Documentation #{did} ({dtitle}); tagged docs:error"
                                        )
                                budget -= 1
                                if budget <= 0:
                                    break
                                continue

                done_payload = worker_done_from_entry(entry) if isinstance(entry, dict) else None
                if done_payload:
                    ok = bool(done_payload.get("ok"))
                    patch_exists = bool(done_payload.get("patchExists"))
                    comment_exists = bool(done_payload.get("commentExists"))
                    try:
                        patch_bytes = int(done_payload.get("patchBytes") or 0)
                    except Exception:
                        patch_bytes = 0
                    try:
                        comment_bytes = int(done_payload.get("commentBytes") or 0)
                    except Exception:
                        comment_bytes = 0
                    patch_path = str(done_payload.get("patchPath") or "") if patch_exists else ""
                    comment_path = str(done_payload.get("commentPath") or "") if comment_exists else ""

                    # Docs completion requires a comment even if the patch is intentionally empty (docs:skip).
                    usable = bool(ok and patch_exists and comment_exists and patch_path and comment_path and comment_bytes > 0)
                    if not usable:
                        if dry_run:
                            actions.append(
                                f"Would tag Documentation #{did} ({dtitle}) as docs:error (docs worker output unusable)"
                            )
                        else:
                            add_tag(did, TAG_DOC_ERROR)
                            remove_tags(did, [TAG_DOC_PENDING, TAG_DOC_INFLIGHT, TAG_DOC_RETRY])
                            msg = (
                                "Docs worker finished without usable artifacts.\n"
                                "This card is parked with docs:error to avoid thrash.\n"
                                f"- handle: {worker_handle(entry)}\n"
                                f"- logPath: {entry.get('logPath') if isinstance(entry, dict) else ''}\n"
                                f"- runDir: {entry.get('runDir') if isinstance(entry, dict) else ''}\n"
                                f"- donePath: {done_payload.get('donePath')}\n"
                                "To retry after fixing the docs worker environment, add tag docs:retry."
                            )
                            add_comment(did, msg)
                            docs_workers_by_task.pop(str(did), None)
                            docs_workers_by_task.pop(did, None)
                            tmux_kill_window(f"docs-{did}")
                            record_action(did)
                            actions.append(
                                f"Tagged Documentation #{did} ({dtitle}) as docs:error (docs worker output unusable)"
                            )
                        budget -= 1
                        continue

                    result_tag = TAG_DOC_COMPLETED if patch_bytes > 0 else TAG_DOC_SKIP
                    if dry_run:
                        actions.append(
                            f"Would move Documentation #{did} ({dtitle}) -> Done ({result_tag}; docs worker complete)"
                        )
                    else:
                        add_tag(did, result_tag)
                        remove_tags(did, [TAG_DOC_PENDING, TAG_DOC_INFLIGHT, TAG_DOC_ERROR, TAG_DOC_RETRY])
                        comment_text = read_text(comment_path, 20000).strip()
                        if comment_text:
                            add_comment(did, comment_text)
                        move_task(pid, did, int(col_done["id"]), 1, int(dsl_id))
                        record_action(did)
                        docs_workers_by_task.pop(str(did), None)
                        docs_workers_by_task.pop(did, None)
                        tmux_kill_window(f"docs-{did}")
                        actions.append(
                            f"Moved Documentation #{did} ({dtitle}) -> Done ({result_tag}; docs worker complete)"
                        )
                    budget -= 1
                    continue

                # If a docs worker handle exists but exited without producing done.json, park with docs:error.
                if isinstance(entry, dict):
                    h = worker_handle(entry)
                    if h and not worker_is_alive(h):
                        try:
                            started_at_ms = int(entry.get("startedAtMs") or 0) or 0
                        except Exception:
                            started_at_ms = 0
                        # Grace window for fast tmux startup/exit.
                        if started_at_ms and (now_ms() - started_at_ms) < 15_000:
                            pass
                        else:
                            if dry_run:
                                actions.append(
                                    f"Would tag Documentation #{did} ({dtitle}) as docs:error (docs worker exited without done.json)"
                                )
                            else:
                                add_tag(did, TAG_DOC_ERROR)
                                remove_tags(did, [TAG_DOC_PENDING, TAG_DOC_INFLIGHT, TAG_DOC_RETRY])
                                msg = (
                                    "Docs worker exited without producing done.json.\n"
                                    "This card is parked with docs:error to avoid thrash.\n"
                                    f"- handle: {h}\n"
                                    f"- logPath: {entry.get('logPath')}\n"
                                    f"- runDir: {entry.get('runDir')}\n"
                                    f"- expected donePath: {entry.get('donePath')}\n"
                                    "To retry after fixing the docs worker environment, add tag docs:retry."
                                )
                                add_comment(did, msg)
                                docs_workers_by_task.pop(str(did), None)
                                docs_workers_by_task.pop(did, None)
                                tmux_kill_window(f"docs-{did}")
                                record_action(did)
                                actions.append(
                                    f"Tagged Documentation #{did} ({dtitle}) as docs:error (docs worker exited without done.json)"
                                )
                            budget -= 1
                            continue

                # Docs spawn: when docs:auto + docs:pending, spawn a docs worker (respect global docs WIP limit).
                if (
                    DOCS_SPAWN_CMD
                    and TAG_DOC_AUTO in lower
                    and TAG_DOC_PENDING in lower
                    and (TAG_DOC_ERROR not in lower)
                ):
                    if DOCS_WIP_LIMIT > 0 and docs_inflight_count() >= DOCS_WIP_LIMIT:
                        continue
                    if dry_run:
                        actions.append(f"Would spawn docs worker for Documentation #{did} ({dtitle})")
                    else:
                        try:
                            full = get_task(did)
                            ddesc = (full.get("description") or "")
                        except Exception:
                            ddesc = ""
                        repo_ok, repo_key, repo_path, _source = resolve_repo_for_task(
                            did, dtitle, dtags, ddesc, require_explicit=True
                        )
                        patch_path = resolve_patch_path_for_task(did) or ""
                        if not repo_ok:
                            add_tag(did, TAG_DOC_ERROR)
                            remove_tags(did, [TAG_DOC_PENDING, TAG_DOC_INFLIGHT, TAG_DOC_RETRY])
                            add_comment(
                                did,
                                "Docs automation cannot resolve the source repo mapping for this card.\n"
                                "This card is parked with docs:error to avoid thrash.\n"
                                "Fix the repo mapping (repo:<key> tag or Repo: ... in description), then add docs:retry.",
                            )
                            record_action(did)
                            actions.append(
                                f"Tagged Documentation #{did} ({dtitle}) as docs:error (no source repo mapping)"
                            )
                            budget -= 1
                            continue
                        spawned_ok, spawned_reason = ensure_docs_worker_handle_for_task(did, repo_key, repo_path, patch_path)
                        if spawned_ok:
                            docs_spawn_failures_by_task.pop(str(did), None)
                            # Atomically transition pending -> inflight (avoid overlap).
                            try:
                                current_tags = get_task_tags(did)
                            except Exception:
                                current_tags = list(dtags)
                            new_tags: List[str] = []
                            seen: set[str] = set()
                            for t in current_tags:
                                tl = str(t).lower()
                                if tl in (TAG_DOC_PENDING.lower(), TAG_DOC_ERROR.lower(), TAG_DOC_RETRY.lower()):
                                    continue
                                if tl in seen:
                                    continue
                                seen.add(tl)
                                new_tags.append(str(t))
                            if TAG_DOC_AUTO.lower() not in seen:
                                new_tags.append(TAG_DOC_AUTO)
                            if TAG_DOC_INFLIGHT.lower() not in seen:
                                new_tags.append(TAG_DOC_INFLIGHT)
                            set_task_tags(pid, did, new_tags)
                            record_action(did)
                            actions.append(f"Spawned docs worker for Documentation #{did} ({dtitle})")
                        else:
                            # Provider preflight failures are global and should not escalate per-card to docs:error.
                            if isinstance(spawned_reason, dict) and spawned_reason.get("kind") == "provider-blocked":
                                budget -= 1
                                continue
                            rec = docs_spawn_failures_by_task.get(str(did)) or {}
                            if not isinstance(rec, dict):
                                rec = {}
                            try:
                                fail_count = int(rec.get("count") or 0)
                            except Exception:
                                fail_count = 0
                            fail_count += 1
                            rec["count"] = fail_count
                            rec["lastFailedAtMs"] = now_ms()
                            docs_spawn_failures_by_task[str(did)] = rec
                            record_action(did)
                            if fail_count >= 3:
                                add_tag(did, TAG_DOC_ERROR)
                                remove_tags(did, [TAG_DOC_PENDING, TAG_DOC_INFLIGHT])
                                msg = (
                                    "Docs worker spawn repeatedly failed.\n"
                                    f"- attempts: {fail_count}\n"
                                    "This card is parked with docs:error to avoid thrash.\n"
                                    "After fixing the docs worker environment, add tag docs:retry to retry."
                                )
                                add_comment(did, msg)
                                actions.append(
                                    f"Docs worker spawn failed {fail_count}x for Documentation #{did} ({dtitle}); tagged docs:error"
                                )
                        budget -= 1
                    if budget <= 0:
                        break
                    continue

                # Ensure docs:pending is present unless docs:inflight is already set.
                # If docs:error is present, do not auto-add docs:pending (avoid respawn loops).
                if (TAG_DOC_ERROR not in lower) and (TAG_DOC_INFLIGHT not in lower) and (TAG_DOC_PENDING not in lower):
                    if dry_run:
                        actions.append(f"Would tag Documentation #{did} ({dtitle}) as docs:pending")
                    else:
                        add_tags(did, [TAG_DOC_AUTO, TAG_DOC_PENDING])
                        record_action(did)
                        actions.append(f"Tagged Documentation #{did} ({dtitle}) as docs:pending")
                    budget -= 1

        # Resume tasks paused by a prior critical when the critical no longer enforces exclusivity.
        paused_by_critical: Dict[str, Any] = state.get("pausedByCritical") or {}

        if (active_critical is None or not critical_exclusive) and paused_by_critical:
            budget = max(budget, ACTION_BUDGET_CRITICAL)
            cleared_any = False

            # Clear paused:critical tags when no critical remains.
            def paused_reason_tags(tags: list[str]) -> set[str]:
                lower = {t.lower() for t in tags}
                return {t for t in lower if t.startswith('paused:')}

            for tid_s, info in list(paused_by_critical.items()):
                try:
                    tid = int(tid_s)
                except Exception:
                    paused_by_critical.pop(tid_s, None)
                    continue
                if budget <= 0:
                    break
                if dry_run:
                    actions.append(f'Would untag paused:critical for #{tid} (critical cleared)')
                else:
                    try:
                        tags = get_task_tags(tid)
                    except Exception:
                        tags = []
                    lower = {t.lower() for t in tags}
                    # Always remove the critical reason tag.
                    if TAG_PAUSED_CRITICAL in lower:
                        remove_tag(tid, TAG_PAUSED_CRITICAL)
                    # If we added the generic paused tag solely for critical, remove it when no other pause reasons remain.
                    added_paused = bool(info.get('addedPaused'))
                    try:
                        tags2 = get_task_tags(tid)
                    except Exception:
                        tags2 = []
                    reasons = paused_reason_tags(tags2)
                    if added_paused and (not reasons) and (TAG_PAUSED in {t.lower() for t in tags2}):
                        remove_tag(tid, TAG_PAUSED)
                    record_action(tid)
                    actions.append(f'Cleared paused:critical for #{tid} (critical cleared)')
                    paused_by_critical.pop(str(tid), None)
                    cleared_any = True
                budget -= 1
            if cleared_any:
                invalidate_wip_active_count()

            if not dry_run:
                state["pausedByCritical"] = paused_by_critical
        if active_critical is not None:
            ct, csl_id, c_col_id = active_critical
            cid = int(ct.get("id"))
            ctitle = task_title(ct)

            critical_in_wip = int(c_col_id) == int(col_wip["id"])
            critical_in_review = int(c_col_id) == int(col_review["id"])
            critical_in_docs = bool(col_docs is not None and int(c_col_id) == int(col_docs["id"]))

            def pause_noncritical_wip() -> None:
                nonlocal budget
                budget = max(budget, ACTION_BUDGET_CRITICAL)
                current_wip = sorted(tasks_for_column(int(col_wip['id'])), key=sort_key)
                paused_state = state.get('pausedByCritical') or {}
                wip_by_id = {int(t.get('id')): (t, sl_id) for t, sl_id in current_wip}
                pause_ids = plan_pause_wip(
                    [int(t.get('id')) for t, _ in current_wip],
                    critical_task_ids,
                    paused_state,
                )

                for wid in pause_ids:
                    if budget <= 0:
                        break
                    wt, wsl_id = wip_by_id[wid]
                    wtitle = task_title(wt)
                    if dry_run:
                        actions.append(f'Would tag non-critical WIP #{wid} ({wtitle}) as paused:critical (for critical #{cid})')
                        budget -= 1
                        continue

                    # Tag-based pause: do not move columns; keep position intact.
                    try:
                        existing_tags = get_task_tags(wid)
                    except Exception:
                        existing_tags = []
                    lower = {t.lower() for t in existing_tags}
                    added_paused = TAG_PAUSED not in lower
                    add_tags(wid, [TAG_PAUSED, TAG_PAUSED_CRITICAL])
                    record_action(wid)
                    paused_state[str(wid)] = {
                        'criticalTaskId': cid,
                        'pausedAtMs': now_ms(),
                        'swimlaneId': int(wsl_id),
                        'addedPaused': bool(added_paused),
                    }
                    actions.append(f'Tagged WIP #{wid} ({wtitle}) as paused:critical (for critical #{cid})')
                    budget -= 1

                state['pausedByCritical'] = paused_state

            # While a critical is active (and freeze is enabled), keep other WIP cards paused.
            if critical_exclusive:
                pause_noncritical_wip()

            if critical_in_wip:
                # Only a critical actively in WIP is exclusive.
                critical_exclusive = True
                budget = max(budget, ACTION_BUDGET_CRITICAL)
                entry = worker_entry_for(cid, workers_by_task)
                if not worker_handle(entry):
                    try:
                        ctags = get_task_tags(cid)
                        cfull = get_task(cid)
                        cdesc = (cfull.get("description") or "")
                    except Exception:
                        ctags = []
                        cdesc = ""
                    repo_ok, repo_key, repo_path, _source = resolve_repo_for_task(
                        cid, ctitle, ctags, cdesc, require_explicit=True
                    )
                    ok, reason = (
                        ensure_worker_handle_for_task(cid, repo_key, repo_path)
                        if repo_path is not None
                        else (False, None)
                    )
                    if repo_path is not None and ok:
                        if worker_handle(worker_entry_for(cid, workers_by_task)):
                            actions.append(f"Spawned worker for active critical #{cid} ({ctitle})")
                        else:
                            if pause_missing_worker(
                                cid,
                                int(csl_id),
                                ctitle,
                                "worker spawn returned no handle",
                                label="critical",
                            ):
                                budget -= 1
                    else:
                        if isinstance(reason, dict) and reason.get("kind") == "provider-blocked":
                            # Provider outage; do not auto-pause critical further here (we already tagged it).
                            budget -= 1
                            pause_noncritical_wip()
                        else:
                            reason = "missing worker handle"
                            if not repo_ok and not repo_path:
                                reason = "missing worker handle + repo mapping"
                            if not WORKER_SPAWN_CMD:
                                reason = "missing worker handle (no worker spawn command configured)"
                            if pause_missing_worker(cid, int(csl_id), ctitle, reason, label="critical"):
                                budget -= 1
                pause_noncritical_wip()

            elif critical_in_review:
                # Critical in Review is waiting on human attention; keep throughput flowing.
                pass

            elif critical_in_docs:
                # Critical already passed review and is in Documentation.
                # Do NOT bounce it back into WIP; the Documentation flow will handle docs:auto/docs:pending.
                pass

            else:
                full = get_task(cid)
                desc = (full.get("description") or "")
                deps = parse_depends_on(desc)
                unmet = [d for d in deps if not is_done(d)]

                if unmet:
                    reason = "Depends on " + ", ".join("#" + str(x) for x in unmet)
                    errors.append(f"critical #{cid} ({ctitle}) cannot start: {reason}")
                else:
                    ctags = get_task_tags(cid)
                    repo_ok, repo_key, repo_path, _source = resolve_repo_for_task(
                        cid, ctitle, ctags, desc, require_explicit=True
                    )
                    if not repo_ok:
                        if dry_run:
                            actions.append(
                                f"Would keep critical #{cid} ({ctitle}) in Backlog; tag {TAG_BLOCKED_REPO}: No repo mapping"
                            )
                        else:
                            tag_blocked_and_keep_in_backlog(
                                cid,
                                int(csl_id),
                                ctitle,
                                "No repo mapping",
                                TAG_BLOCKED_REPO,
                                from_label="critical",
                                auto_blocked=True,
                            )
                        budget -= 1
                    else:
                        critical_wip_exclusive_keys: set[str] = set()
                        for wt, _wsl in wip_tasks:
                            wid = int(wt.get("id"))
                            if wid == cid or wid not in critical_task_ids:
                                continue
                            wtags = get_task_tags(wid)
                            wdesc = (get_task(wid).get("description") or "")
                            for k in parse_exclusive_keys(wtags, wdesc):
                                critical_wip_exclusive_keys.add(k)

                        ex_keys = parse_exclusive_keys(ctags, desc)
                        ex_conflicts = [k for k in ex_keys if k in critical_wip_exclusive_keys]

                        if ex_conflicts:
                            errors.append(
                                "critical #"
                                + str(cid)
                                + " ("
                                + ctitle
                                + ") cannot start: Exclusive conflict: "
                                + ", ".join("exclusive:" + k for k in ex_conflicts)
                            )
                        elif budget > 0:
                            if dry_run:
                                if not WORKER_SPAWN_CMD:
                                    actions.append(
                                        f"Would NOT start critical #{cid} ({ctitle}) -> WIP (no worker spawn command configured)"
                                    )
                                    actions.append(
                                        f"Would tag critical #{cid} ({ctitle}) as paused:missing-worker (cannot start worker)"
                                    )
                                else:
                                    actions.append(f"Would spawn worker for critical #{cid} ({ctitle})")
                                    actions.append(
                                        f"Would tag non-critical WIP as paused:critical (for critical #{cid})"
                                    )
                                    actions.append(f"Would start critical #{cid} ({ctitle}) -> WIP")
                            else:
                                ok, reason = ensure_worker_handle_for_task(cid, repo_key, repo_path)
                                if ok:
                                    entry = worker_entry_for(cid, workers_by_task)
                                    if worker_handle(entry):
                                        pause_noncritical_wip()
                                        # Clear transient auto-block fences before starting.
                                        remove_tags(
                                            cid,
                                            [
                                                TAG_AUTO_BLOCKED,
                                                TAG_BLOCKED_DEPS,
                                                TAG_BLOCKED_EXCLUSIVE,
                                                TAG_BLOCKED_REPO,
                                                TAG_HOLD_DEPS,
                                                TAG_HOLD_NEEDS_REPO,
                                            ],
                                        )
                                        move_task(pid, cid, int(col_wip["id"]), 1, int(csl_id))
                                        record_action(cid)
                                        moved_to_wip.append(cid)
                                        actions.append(f"Started critical #{cid} ({ctitle}) -> WIP")
                                        critical_exclusive = True
                                    else:
                                        pause_missing_worker(
                                            cid,
                                            int(csl_id),
                                            ctitle,
                                            "worker spawn returned no handle",
                                            force=True,
                                            label="critical",
                                        )
                                else:
                                    if isinstance(reason, dict) and reason.get("kind") == "provider-blocked":
                                        # Provider outage; do not pause/move this critical.
                                        pass
                                    else:
                                        pause_missing_worker(
                                            cid,
                                            int(csl_id),
                                            ctitle,
                                            "cannot start worker",
                                            force=True,
                                            label="critical",
                                        )
                            budget -= 1

            # While a critical is actively in WIP, freeze normal pulling.
            if critical_exclusive:
                state["lastActionsByTaskId"] = last_actions
                state["repoByTaskId"] = repo_by_task
                state["workersByTaskId"] = workers_by_task
                state["autoBlockedByOrchestrator"] = auto_blocked
                state["reviewersByTaskId"] = reviewers_by_task
                state["reviewResultsByTaskId"] = review_results_by_task
                state["reviewReworkHistoryByTaskId"] = review_rework_history_by_task
                state["reviewerSpawnFailuresByTaskId"] = reviewer_spawn_failures_by_task
                state["docsWorkersByTaskId"] = docs_workers_by_task
                state["docsSpawnFailuresByTaskId"] = docs_spawn_failures_by_task
                save_state(state)

                emit_json(
                    mode=mode,
                    actions=actions,
                    promoted_to_ready=promoted_to_ready,
                    moved_to_wip=moved_to_wip,
                    created_tasks=created_tasks,
                    errors=errors,
                )
                return 0

        # ---------------------------------------------------------------------
        # NORMAL MODE
        # ---------------------------------------------------------------------

        # If active WIP > limit, don't pull new work (paused/held WIP does not consume capacity).
        active_wip = wip_active_count()
        if active_wip > WIP_LIMIT:
            actions.append(f"WIP active is {active_wip} (> {WIP_LIMIT}); not pulling new work")
            state["lastActionsByTaskId"] = last_actions
            state["repoByTaskId"] = repo_by_task
            state["workersByTaskId"] = workers_by_task
            state["autoBlockedByOrchestrator"] = auto_blocked
            state["reviewersByTaskId"] = reviewers_by_task
            state["reviewResultsByTaskId"] = review_results_by_task
            state["reviewReworkHistoryByTaskId"] = review_rework_history_by_task
            state["reviewerSpawnFailuresByTaskId"] = reviewer_spawn_failures_by_task
            state["docsWorkersByTaskId"] = docs_workers_by_task
            state["docsSpawnFailuresByTaskId"] = docs_spawn_failures_by_task
            state["docsTimeoutRestartsByTaskId"] = docs_timeout_restarts_by_task
            save_state(state)
            emit_json(
                mode=mode,
                actions=actions,
                promoted_to_ready=promoted_to_ready,
                moved_to_wip=moved_to_wip,
                created_tasks=created_tasks,
                errors=errors,
            )
            return 0

        # Helper: pick top ready/backlog
        ready_tasks_sorted = sorted(ready_tasks, key=sort_key)
        backlog_sorted = sorted(backlog_tasks, key=sort_key)

        # Selection: treat epic containers as non-actionable; skip them and pull the next real task.
        # Also enforce:
        # - Depends on: #<id>
        # - exclusive:<key>
        def pick_next_backlog_action() -> Tuple[
            Optional[Tuple[Dict[str, Any], int]],
            Optional[Dict[str, Any]],
            Optional[Tuple[Dict[str, Any], int, str]],
        ]:
            """Returns (picked_task, epic_container_or_none, blocked_candidate_or_none).

            picked_task is the first non-held, non-epic task that is not blocked by deps/exclusives.
            epic_container is the first epic container encountered (for breakdown) if no picked task exists.
            blocked_candidate is the first non-held, non-epic task that is blocked by deps/exclusives, with reason.
            """
            epic: Optional[Dict[str, Any]] = None
            blocked: Optional[Tuple[Dict[str, Any], int, str]] = None

            # precompute exclusive keys currently in WIP
            wip_exclusive_keys: set[str] = set()
            for wt, _wsl in wip_tasks:
                wid = int(wt.get('id'))
                wtags = get_task_tags(wid)
                if is_held(wtags):
                    continue
                wdesc = (get_task(wid).get('description') or '')
                for k in parse_exclusive_keys(wtags, wdesc):
                    wip_exclusive_keys.add(k)

            for t, sl_id in backlog_sorted:
                tid = int(t.get("id"))
                tags = get_task_tags(tid)
                title = task_title(t)

                if is_held(tags):
                    continue

                if is_epic(tags) or title.lower().startswith("epic:"):
                    if epic is None:
                        epic = t
                    continue

                # Cooldown: don't keep re-moving the same backlog item across runs.
                if not cooled(tid):
                    continue

                full = get_task(tid)
                desc = (full.get('description') or '')

                # deps
                deps = parse_depends_on(desc)
                unmet = [d for d in deps if not is_done(d)]
                if unmet:
                    if blocked is None:
                        blocked = (t, sl_id, f"Depends on {', '.join('#'+str(x) for x in unmet)}")
                    continue

                # exclusive
                ex_keys = parse_exclusive_keys(tags, desc)
                if any(k in wip_exclusive_keys for k in ex_keys):
                    if blocked is None:
                        blocked = (t, sl_id, f"Exclusive conflict: {', '.join('exclusive:'+k for k in ex_keys if k in wip_exclusive_keys)}")
                    continue

                # repo mapping (required for auto-start)
                if not has_repo_mapping(tid, title, tags, desc):
                    if blocked is None:
                        blocked = (t, sl_id, "No repo mapping (add 'Repo:' or tag repo:<key> or tag no-repo)")
                    continue

                return (t, sl_id), epic, blocked

            return None, epic, blocked

        # Make multiple moves per run (bounded by ACTION_BUDGET).
        # Desired behavior:
        # - Keep Ready filled when possible (even if WIP is already full).
        # - Start work immediately when WIP has capacity.
        # Precompute exclusive keys currently in WIP (real board state)
        wip_exclusive_keys: set[str] = set()
        for wt, _wsl in wip_tasks:
            wid = int(wt.get('id'))
            wtags = get_task_tags(wid)
            wdesc = (get_task(wid).get('description') or '')
            for k in parse_exclusive_keys(wtags, wdesc):
                wip_exclusive_keys.add(k)

        while budget > 0:
            did_something = False

            # 0) Auto-heal Backlog tasks that were auto-blocked and are now clear.
            # (e.g. deps resolved, exclusives released, repo mapping added.)
            # Only do this when Ready is empty to avoid thrash.
            if budget > 0 and not ready_tasks_sorted and backlog_sorted:
                for bt, bsl_id in backlog_sorted:
                    bid = int(bt.get("id"))
                    btitle = task_title(bt)
                    try:
                        btags = get_task_tags(bid)
                    except Exception:
                        btags = []
                    lower = {t.lower() for t in btags}
                    if TAG_AUTO_BLOCKED not in lower:
                        continue
                    # Only auto-heal for the transient blocked reasons.
                    if not (TAG_BLOCKED_DEPS in lower or TAG_BLOCKED_EXCLUSIVE in lower or TAG_BLOCKED_REPO in lower):
                        continue
                    if not cooled(bid):
                        continue

                    try:
                        full = get_task(bid)
                        desc = (full.get("description") or "")
                    except Exception:
                        desc = ""

                    # deps
                    deps = parse_depends_on(desc)
                    unmet = [d for d in deps if not is_done(d)]
                    if unmet:
                        continue

                    # exclusive
                    ex_keys = parse_exclusive_keys(btags, desc)
                    if any(k in wip_exclusive_keys for k in ex_keys):
                        continue

                    # repo mapping
                    if not has_repo_mapping(bid, btitle, btags, desc):
                        continue

                    if dry_run:
                        actions.append(f"Would auto-heal Backlog #{bid} ({btitle}) -> Ready")
                    else:
                        move_task(pid, bid, int(col_ready["id"]), 1, bsl_id)
                        record_action(bid)
                        promoted_to_ready.append(bid)
                        remove_tags(
                            bid,
                            [
                                TAG_AUTO_BLOCKED,
                                TAG_BLOCKED_DEPS,
                                TAG_BLOCKED_EXCLUSIVE,
                                TAG_BLOCKED_REPO,
                                TAG_HOLD_DEPS,
                                TAG_HOLD_NEEDS_REPO,
                            ],
                        )
                        auto_blocked.pop(str(bid), None)
                        actions.append(f"Auto-healed Backlog #{bid} ({btitle}) -> Ready")
                    budget -= 1
                    did_something = True
                    ready_tasks_sorted = sorted(tasks_for_column(int(col_ready["id"])), key=sort_key)
                    backlog_sorted = sorted(tasks_for_column(int(col_backlog["id"])), key=sort_key)
                    break

            # 0) Auto-heal Blocked tasks that were auto-blocked and are now clear.
            # Only do this when Ready is empty to avoid thrash.
            if budget > 0 and not ready_tasks_sorted and blocked_tasks:
                blocked_sorted = sorted(tasks_for_column(int(col_blocked["id"])), key=sort_key)
                for bt, bsl_id in blocked_sorted:
                    bid = int(bt.get("id"))
                    btitle = task_title(bt)
                    try:
                        btags = get_task_tags(bid)
                    except Exception:
                        btags = []
                    if is_held(btags):
                        continue
                    if not has_tag(btags, TAG_AUTO_BLOCKED):
                        continue
                    if not cooled(bid):
                        continue
                    try:
                        full = get_task(bid)
                        desc = (full.get("description") or "")
                    except Exception:
                        desc = ""

                    deps = parse_depends_on(desc)
                    unmet = [d for d in deps if not is_done(d)]
                    if unmet:
                        continue

                    ex_keys = parse_exclusive_keys(btags, desc)
                    if any(k in wip_exclusive_keys for k in ex_keys):
                        continue

                    if not has_repo_mapping(bid, btitle, btags, desc):
                        continue

                    if dry_run:
                        actions.append(f"Would auto-heal Blocked #{bid} ({btitle}) -> Ready")
                    else:
                        move_task(pid, bid, int(col_ready["id"]), 1, bsl_id)
                        record_action(bid)
                        promoted_to_ready.append(bid)
                        remove_tags(
                            bid,
                            [
                                TAG_AUTO_BLOCKED,
                                TAG_BLOCKED_DEPS,
                                TAG_BLOCKED_EXCLUSIVE,
                                TAG_BLOCKED_REPO,
                                TAG_HOLD_DEPS,
                                TAG_HOLD_NEEDS_REPO,
                            ],
                        )
                        auto_blocked.pop(str(bid), None)
                        actions.append(f"Auto-healed Blocked #{bid} ({btitle}) -> Ready")
                    budget -= 1
                    did_something = True
                    # refresh lists
                    ready_tasks_sorted = sorted(tasks_for_column(int(col_ready["id"])), key=sort_key)
                    backlog_sorted = sorted(tasks_for_column(int(col_backlog["id"])), key=sort_key)
                    break

            # 1) If Ready is empty and Backlog has work, promote one item to Ready.
            if not ready_tasks_sorted and backlog_sorted:
                picked, epic_container, blocked_candidate = pick_next_backlog_action()

                # If the next candidate is blocked by deps/exclusive/repo, move it to Blocked with a clear reason.
                if picked is None and blocked_candidate is not None:
                    bt, bsl_id, reason = blocked_candidate
                    bid = int(bt.get("id"))
                    btitle = task_title(bt)
                    reason_lower = (reason or "").lower()
                    reason_tag = TAG_BLOCKED_REPO
                    if reason_lower.startswith("depends on"):
                        reason_tag = TAG_BLOCKED_DEPS
                    elif reason_lower.startswith("exclusive conflict"):
                        reason_tag = TAG_BLOCKED_EXCLUSIVE

                    if dry_run:
                        actions.append(
                            f"Would keep Backlog #{bid} ({btitle}) in Backlog; tag {reason_tag}: {reason}"
                        )
                    else:
                        tag_blocked_and_keep_in_backlog(
                            bid,
                            int(bsl_id),
                            btitle,
                            reason,
                            reason_tag,
                            from_label="Backlog",
                            auto_blocked=reason_tag in (TAG_BLOCKED_DEPS, TAG_BLOCKED_EXCLUSIVE, TAG_BLOCKED_REPO),
                        )
                    budget -= 1
                    did_something = True
                    # simulate state / refresh sorted lists next loop
                    backlog_sorted = [(t, sid) for (t, sid) in backlog_sorted if int(t.get("id")) != bid]
                    ready_tasks_sorted = sorted(tasks_for_column(int(col_ready["id"])), key=sort_key)
                    continue

                if picked is not None:
                    candidate, sl_id = picked
                    cid = int(candidate.get("id"))
                    ctitle = task_title(candidate)
                    if dry_run:
                        actions.append(f"Would promote Backlog #{cid} ({ctitle}) -> Ready")
                    else:
                        move_task(pid, cid, int(col_ready["id"]), 1, sl_id)
                        record_action(cid)
                        promoted_to_ready.append(cid)
                        actions.append(f"Promoted Backlog #{cid} ({ctitle}) -> Ready")
                    # simulate state
                    backlog_sorted = [(t, sid) for (t, sid) in backlog_sorted if int(t.get("id")) != cid]
                    ready_tasks_sorted = [(candidate, sl_id)] + ready_tasks_sorted
                    budget -= 1
                    did_something = True

                elif epic_container is not None:
                    # No actionable tasks found; if we're staring at an epic, ensure breakdown exists.
                    eid = int(epic_container.get("id"))
                    etitle = task_title(epic_container)
                    bt = breakdown_title(eid, etitle)

                    # Search for existing breakdown anywhere (including Done) to avoid duplicates
                    all_tasks = (
                        backlog_tasks
                        + ready_tasks
                        + wip_tasks
                        + tasks_for_column(int(col_review["id"]))
                        + tasks_for_column(int(col_done["id"]))
                    )
                    existing = find_existing_breakdown(all_tasks, bt)

                    if existing:
                        if dry_run:
                            actions.append(
                                f"Would create breakdown task for epic #{eid} ({etitle}), but one already exists: #{existing}"
                            )
                    else:
                        if dry_run:
                            actions.append(f"Would create breakdown task for epic #{eid} ({etitle}): '{bt}'")
                        else:
                            new_id = create_task(
                                pid,
                                bt,
                                f"Breakdown for epic #{eid}: {etitle}\n\nEpic: #{eid}",
                                int(col_backlog["id"]),
                            )
                            set_task_tags(pid, new_id, [TAG_STORY, TAG_EPIC_CHILD])
                            created_tasks.append(new_id)
                            actions.append(f"Created breakdown task #{new_id} for epic #{eid} ({etitle})")
                        budget -= 1
                        did_something = True

            # 2) If WIP has capacity and Ready has items, move Ready -> WIP.
            if budget > 0 and wip_active_count() < WIP_LIMIT and ready_tasks_sorted:
                candidate, sl_id = ready_tasks_sorted[0]
                cid = int(candidate.get("id"))
                ctitle = task_title(candidate)
                tags = get_task_tags(cid)

                if is_held(tags):
                    # skip held
                    ready_tasks_sorted = ready_tasks_sorted[1:]
                    continue

                full = get_task(cid)
                desc = (full.get('description') or '')
                deps = parse_depends_on(desc)
                unmet = [d for d in deps if not is_done(d)]
                if unmet:
                    if not cooled(cid):
                        actions.append(
                            f"Skipped Ready #{cid} ({ctitle}) -> Backlog due to cooldown; leaving in Ready"
                        )
                        ready_tasks_sorted = ready_tasks_sorted[1:] + [(candidate, sl_id)]
                        budget -= 1
                        did_something = True
                        continue
                    reason = "Depends on " + ", ".join("#" + str(x) for x in unmet)
                    if dry_run:
                        actions.append(f"Would move Ready #{cid} ({ctitle}) -> Backlog; tag {TAG_BLOCKED_DEPS}: {reason}")
                    else:
                        tag_blocked_and_keep_in_backlog(
                            cid,
                            int(sl_id),
                            ctitle,
                            reason,
                            TAG_BLOCKED_DEPS,
                            from_label="Ready",
                            auto_blocked=True,
                        )
                    budget -= 1
                    did_something = True
                    # simulate / refresh lists
                    ready_tasks_sorted = ready_tasks_sorted[1:]
                    continue

                ex_keys = parse_exclusive_keys(tags, desc)
                if any(k in wip_exclusive_keys for k in ex_keys):
                    # exclusive conflict, keep in Ready but don't start
                    actions.append(
                        f"Skipped Ready #{cid} ({ctitle}) due to exclusive conflict: {', '.join('exclusive:'+k for k in ex_keys if k in wip_exclusive_keys)}"
                    )
                    # move to end of ready queue for now
                    ready_tasks_sorted = ready_tasks_sorted[1:] + [(candidate, sl_id)]
                    budget -= 1
                    did_something = True
                    continue

                repo_ok, repo_key, repo_path, _source = resolve_repo_for_task(
                    cid, ctitle, tags, desc, require_explicit=True
                )
                if not repo_ok:
                    if not cooled(cid):
                        actions.append(
                            f"Skipped Ready #{cid} ({ctitle}) -> Backlog due to cooldown; leaving in Ready"
                        )
                        ready_tasks_sorted = ready_tasks_sorted[1:] + [(candidate, sl_id)]
                        budget -= 1
                        did_something = True
                        continue
                    if dry_run:
                        actions.append(f"Would move Ready #{cid} ({ctitle}) -> Backlog; tag {TAG_BLOCKED_REPO}: No repo mapping")
                    else:
                        tag_blocked_and_keep_in_backlog(
                            cid,
                            int(sl_id),
                            ctitle,
                            "No repo mapping",
                            TAG_BLOCKED_REPO,
                            from_label="Ready",
                            auto_blocked=True,
                        )
                    budget -= 1
                    did_something = True
                    ready_tasks_sorted = ready_tasks_sorted[1:]
                    continue
                # Never create silent WIP.
                # We only move Ready -> WIP when we have (or can spawn) a worker handle immediately.
                started = False
                if dry_run:
                    if not WORKER_SPAWN_CMD:
                        actions.append(
                            f"Would NOT move Ready #{cid} ({ctitle}) -> WIP (no worker spawn command configured)"
                        )
                        actions.append(f"Would tag Ready #{cid} ({ctitle}) as paused:missing-worker")
                    else:
                        actions.append(f"Would spawn worker for #{cid} ({ctitle}) then move Ready -> WIP")
                        started = True
                else:
                    ok, reason = ensure_worker_handle_for_task(cid, repo_key, repo_path)
                    if ok:
                        move_task(pid, cid, int(col_wip["id"]), 1, sl_id)
                        record_action(cid)
                        moved_to_wip.append(cid)
                        actions.append(f"Moved Ready #{cid} ({ctitle}) -> WIP")
                        started = True
                    else:
                        if isinstance(reason, dict) and reason.get("kind") == "provider-blocked":
                            # Provider outage: don't tag paused:missing-worker (not actionable per-card).
                            # Stop pulling additional Ready work this tick to avoid churn.
                            budget = 0
                        else:
                            # Can't start worker (misconfig or spawn failure). Leave the card in Ready,
                            # tag it so it doesn't keep getting retried, and surface the problem.
                            record_action(cid)
                            add_tags(cid, [TAG_PAUSED, TAG_PAUSED_MISSING_WORKER])
                            actions.append(
                                f"Tagged Ready #{cid} ({ctitle}) as paused:missing-worker (cannot start worker)"
                            )

                # simulate state
                ready_tasks_sorted = ready_tasks_sorted[1:]
                if started:
                    wip_tasks.append((candidate, sl_id))
                    wip_count += 1
                    invalidate_wip_active_count()
                    for k in ex_keys:
                        wip_exclusive_keys.add(k)
                budget -= 1
                did_something = True

            if not did_something:
                break

        # Persist state updates
        state["lastActionsByTaskId"] = last_actions
        state["repoByTaskId"] = repo_by_task
        state["workersByTaskId"] = workers_by_task
        state["autoBlockedByOrchestrator"] = auto_blocked
        state["repoHoldCommentedByTaskId"] = repo_hold_commented_by_task_id
        state["reviewersByTaskId"] = reviewers_by_task
        state["reviewResultsByTaskId"] = review_results_by_task
        state["reviewReworkHistoryByTaskId"] = review_rework_history_by_task
        state["reviewerSpawnFailuresByTaskId"] = reviewer_spawn_failures_by_task
        state["docsWorkersByTaskId"] = docs_workers_by_task
        state["docsSpawnFailuresByTaskId"] = docs_spawn_failures_by_task
        if dry_run:
            if dry_runs_remaining > 0:
                state["dryRunRunsRemaining"] = dry_runs_remaining - 1
                if state["dryRunRunsRemaining"] <= 0:
                    state["dryRun"] = False

        # Best-effort human notification (no impact on orchestration decisions).
        maybe_notify(state, actions=actions, errors=errors)
        save_state(state)

        emit_json(
            mode=mode,
            actions=actions,
            promoted_to_ready=promoted_to_ready,
            moved_to_wip=moved_to_wip,
            created_tasks=created_tasks,
            errors=errors,
        )
        return 0

    except Exception as e:
        # Always emit something parseable for cron.
        payload = {
            "mode": "LIVE",
            "actions": [],
            "promotedToReady": [],
            "movedToWip": [],
            "createdTasks": [],
            "errors": [f"RecallDeck board orchestrator error: {e}"],
        }
        print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        return 0

    finally:
        release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
