#!/usr/bin/env python3
"""RecallDeck critical monitor.

Outputs:
- NO_REPLY (healthy / no critical tasks)
- One line starting with "ALERT:" when drift/invariant violation is detected.

Uses Kanboard JSON-RPC via stdlib urllib.

Env:
- KANBOARD_BASE (default http://localhost:8401/jsonrpc.php)
- KANBOARD_USER
- KANBOARD_TOKEN
- RECALLDECK_PROJECT (default RecallDeck)
- STATE_PATH (default /Users/joshwegener/clawd/memory/board-orchestrator-state.json)
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

KANBOARD_BASE = os.environ.get("KANBOARD_BASE", "http://localhost:8401/jsonrpc.php")
KANBOARD_USER = os.environ.get("KANBOARD_USER")
KANBOARD_TOKEN = os.environ.get("KANBOARD_TOKEN")
PROJECT = os.environ.get("RECALLDECK_PROJECT", "RecallDeck")
STATE_PATH = os.environ.get("STATE_PATH", "/Users/joshwegener/clawd/memory/board-orchestrator-state.json")


def rpc(method: str, params: Any = None) -> Any:
    if not KANBOARD_USER or not KANBOARD_TOKEN:
        raise RuntimeError("KANBOARD_USER/KANBOARD_TOKEN not set")

    auth = base64.b64encode(f"{KANBOARD_USER}:{KANBOARD_TOKEN}".encode()).decode()
    payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params is not None:
        payload["params"] = params

    req = urllib.request.Request(
        KANBOARD_BASE,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
    )

    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()

    out = json.loads(raw)
    if out.get("error"):
        raise RuntimeError(str(out["error"]))
    return out.get("result")


def main() -> int:
    pid = int(rpc("getProjectByName", {"name": PROJECT})["id"])

    # Map column id -> title
    columns = rpc("getColumns", {"project_id": pid})
    col_by_id = {int(c["id"]): (c.get("title") or "").strip() for c in columns}

    # Swimlane names for stable ordering
    swim = rpc("getActiveSwimlanes", {"project_id": pid})
    sw_by_id = {
        int(s["id"]): {"name": (s.get("name") or "").strip(), "position": int(s.get("position") or 999)}
        for s in swim
    }

    all_tasks = rpc("getAllTasks", {"project_id": pid})

    # Load state (best-effort)
    state: Dict[str, Any] = {}
    try:
        state = json.loads(Path(STATE_PATH).read_text())
    except Exception:
        state = {}

    prio = state.get("swimlanePriority") or []
    prio_index = {name: i for i, name in enumerate(prio)}
    workers = state.get("workersByTaskId") or {}

    def tags_for(tid: int) -> List[str]:
        tagmap = rpc("getTaskTags", {"task_id": tid}) or {}
        if isinstance(tagmap, dict):
            return [str(v) for v in tagmap.values()]
        return [str(x) for x in tagmap]

    def is_held(tags: List[str]) -> bool:
        lower = {x.lower() for x in tags}
        if "hold" in lower or "no-auto" in lower:
            return True
        if any(t.startswith("hold:") for t in lower):
            return True
        # Treat paused tags as manual escape hatch for critical monitoring.
        if "paused" in lower:
            return True
        if any(t.startswith("paused:") for t in lower):
            return True
        return False

    critical: List[Tuple[Dict[str, Any], List[str], str]] = []
    for t in all_tasks:
        tid = int(t["id"])
        col = col_by_id.get(int(t.get("column_id") or 0), "")
        if col == "Done":
            continue
        tags = tags_for(tid)
        # Treat "critical" tasks as active only when not held.
        if "critical" in [x.lower() for x in tags] and not is_held(tags):
            critical.append((t, tags, col))

    if not critical:
        print("NO_REPLY")
        return 0

    def swim_key(task: Dict[str, Any]) -> Tuple[int, int]:
        sid = int(task.get("swimlane_id") or 0)
        s = sw_by_id.get(sid, {"name": "", "position": 999})
        name = s["name"]
        return (prio_index.get(name, 999), s["position"])

    critical_sorted = sorted(
        critical,
        key=lambda item: (swim_key(item[0]), int(item[0].get("position") or 999), int(item[0]["id"])),
    )

    # Multiple critical tasks are allowed; monitor only the top-priority one.
    active_task, _active_tags, active_col = critical_sorted[0]
    active_id = int(active_task["id"])

    # Only enforce the "WIP must be critical-only" invariant when the critical task is actually
    # active (in WIP or Review). If a critical ticket is still sitting in Backlog/Ready/Blocked,
    # the orchestrator hasn't had a chance to preempt yet; alerting here just creates noise.
    if active_col in ("Work in progress", "Review"):
        wip = [t for t in all_tasks if col_by_id.get(int(t.get("column_id") or 0), "") == "Work in progress"]
        noncrit_in_wip: List[Tuple[int, str]] = []
        for t in wip:
            tid = int(t["id"])
            tags = tags_for(tid)
            lower = [x.lower() for x in tags]
            # Allow non-critical tasks to remain in WIP if they are explicitly paused for a critical.
            if "critical" not in lower and "paused:critical" not in lower:
                noncrit_in_wip.append((tid, (t.get("title") or "").strip()))

        if noncrit_in_wip:
            msg = "ALERT: Non-critical tasks still in WIP while critical is active:\n" + "\n".join(
                [f"#{tid} {title}" for tid, title in noncrit_in_wip]
            )
            print(msg)
            return 0

    if active_col == "Work in progress" and (str(active_id) not in workers and active_id not in workers):
        print(f"ALERT: Active critical #{active_id} is in WIP but has no worker handle recorded in state")
        return 0

    print("NO_REPLY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
