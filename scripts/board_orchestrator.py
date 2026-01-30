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
import json
import os
import re
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

STATE_PATH = os.environ.get(
    "BOARD_ORCHESTRATOR_STATE",
    "/Users/joshwegener/clawd/memory/board-orchestrator-state.json",
)
LOCK_PATH = os.environ.get("BOARD_ORCHESTRATOR_LOCK", "/tmp/board-orchestrator.lock")

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
TAG_NOAUTO = "no-auto"
TAG_STORY = "story"
TAG_EPIC_CHILD = "epic-child"
TAG_DOCS_REQUIRED = "docs-required"
TAG_CRITICAL = "critical"
TAG_PAUSED = "paused"

DEPENDS_RE = re.compile(r"^depends on\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
EXCLUSIVE_RE = re.compile(r"^exclusive\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

COL_BACKLOG = "Backlog"
COL_READY = "Ready"
COL_WIP = "Work in progress"
COL_REVIEW = "Review"
COL_PAUSED = "Paused"
COL_BLOCKED = "Blocked"
COL_DONE = "Done"


def now_ms() -> int:
    return int(time.time() * 1000)


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
    }


def save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def acquire_lock() -> bool:
    # stale after 10 minutes
    stale_ms = 10 * 60 * 1000
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r") as f:
                lock = json.load(f)
            if now_ms() - int(lock.get("createdAtMs", 0)) < stale_ms:
                return False
        except Exception:
            # if unreadable, treat as stale
            pass
    with open(LOCK_PATH, "w") as f:
        json.dump({"pid": os.getpid(), "createdAtMs": now_ms()}, f)
    return True


def release_lock() -> None:
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        pass


def rpc(method: str, params: Any = None) -> Any:
    if not KANBOARD_USER or not KANBOARD_TOKEN:
        raise RuntimeError("KANBOARD_USER/KANBOARD_TOKEN not set")

    payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params is not None:
        payload["params"] = params

    auth = base64.b64encode(f"{KANBOARD_USER}:{KANBOARD_TOKEN}".encode()).decode()
    req = urllib.request.Request(
        KANBOARD_BASE,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()

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
    ids = []
    for part in raw.split(','):
        part = part.strip()
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
    return TAG_HOLD in lower or TAG_NOAUTO in lower


def is_epic(tags: List[str]) -> bool:
    return TAG_EPIC in {x.lower() for x in tags}


def is_critical(tags: List[str]) -> bool:
    return TAG_CRITICAL in {x.lower() for x in tags}


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
    if not acquire_lock():
        print("NO_REPLY")
        return 0

    try:
        state = load_state()
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
        col_paused = find_column(columns, COL_PAUSED)
        col_blocked = find_column(columns, COL_BLOCKED)
        col_done = find_column(columns, COL_DONE)

        missing = [
            name
            for name, col in [
                (COL_BACKLOG, col_backlog),
                (COL_READY, col_ready),
                (COL_WIP, col_wip),
                (COL_REVIEW, col_review),
                (COL_PAUSED, col_paused),
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
        paused_tasks = tasks_for_column(int(col_paused["id"]))
        blocked_tasks = tasks_for_column(int(col_blocked["id"]))

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

        wip_count = len(wip_tasks)
        actions: List[str] = []
        promoted_to_ready: List[int] = []
        moved_to_wip: List[int] = []
        created_tasks: List[int] = []
        errors: List[str] = []

        # Drift: WIP tasks missing worker handle
        for t, _sl_id in wip_tasks:
            tid = int(t.get("id"))
            title = task_title(t)
            if str(tid) not in workers_by_task:
                errors.append(f"drift: WIP #{tid} ({title}) has no worker handle recorded")

        # Docs drift is handled after dry-run mode is computed.

        last_actions = state.get("lastActionsByTaskId") or {}
        cooldown_ms = TASK_COOLDOWN_MIN * 60 * 1000

        def cooled(task_id: int) -> bool:
            last = int(last_actions.get(str(task_id), 0) or 0)
            return (now_ms() - last) >= cooldown_ms

        def record_action(task_id: int) -> None:
            last_actions[str(task_id)] = now_ms()

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

        # Determine dry-run
        dry_runs_remaining = int(state.get("dryRunRunsRemaining") or 0)
        dry_run = bool(state.get("dryRun", True))
        auto_arm = False
        if dry_run and dry_runs_remaining <= 0:
            dry_run = False
        if dry_run and dry_runs_remaining == 1:
            auto_arm = True

        mode = "DRY_RUN" if dry_run else "LIVE"

        # Docs drift: if a review task looks like an API change, ensure a companion Docs task exists.
        # (MVP heuristic)
        def needs_docs(task_id: int, title: str) -> bool:
            try:
                tags = {x.lower() for x in get_task_tags(task_id)}
                if TAG_DOCS_REQUIRED in tags:
                    return True
                t = get_task(task_id)
                desc = (t.get('description') or '').lower()
                if title.lower().startswith('server:') and ('/v1/' in title.lower() or '/v1/' in desc):
                    return True
            except Exception:
                return False
            return False

        all_board_tasks: List[Tuple[int, str]] = []
        for sl in swimlanes:
            for c in (sl.get('columns') or []):
                for t in (c.get('tasks') or []):
                    all_board_tasks.append((int(t.get('id')), task_title(t)))

        for rt, rsl_id in review_tasks:
            rid = int(rt.get('id'))
            rtitle = task_title(rt)
            if not needs_docs(rid, rtitle):
                continue
            docs_title = f"Docs: (from #{rid}) {rtitle}"
            if any(title == docs_title for _id, title in all_board_tasks):
                continue
            if dry_run:
                actions.append(f"Would create docs task for review #{rid} ({rtitle})")
            else:
                new_id = create_task(
                    pid,
                    docs_title,
                    f"Docs companion task auto-created for review card #{rid}.\n\nSource: #{rid} {rtitle}",
                    int(col_backlog['id']),
                    swimlane_id=int(rsl_id),
                )
                try:
                    set_task_tags(pid, new_id, ['docs', TAG_DOCS_REQUIRED])
                except Exception:
                    pass
                created_tasks.append(new_id)
                actions.append(f"Created docs task #{new_id} for review #{rid} ({rtitle})")

        budget = ACTION_BUDGET

        # ---------------------------------------------------------------------
        # CRITICAL MODE (preemptive)
        # ---------------------------------------------------------------------
        # If any non-Done task is tagged `critical`, it takes absolute priority.
        # While critical is active (WIP or Review or anywhere not Done), pause all
        # non-critical WIP tasks into `Paused` and do not pull/start any non-critical
        # work. Critical is not "done" until it reaches Done.
        #
        # Note: We still respect dependencies/exclusive constraints; if the critical
        # task cannot start, we do NOT pause everything (avoids deadlock) and we
        # emit an error instead.

        # Collect all non-Done tasks so we can find critical candidates anywhere.
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
            if is_critical(tags) and not is_held(tags):
                critical_candidates.append((t, sl_id, col_id))
                critical_task_ids.add(tid)

        # Prefer critical already in WIP/Review, then Ready, then other columns.
        active_critical: Optional[Tuple[Dict[str, Any], int, int]] = None
        if critical_candidates:
            active_critical = sorted(
                critical_candidates,
                key=lambda item: critical_sort_key(
                    item[2],
                    int(col_wip["id"]),
                    int(col_review["id"]),
                    int(col_ready["id"]),
                    sort_key(item),
                ),
            )[0]

        # Resume paused tasks only when NO critical tasks remain.
        paused_by_critical: Dict[str, Any] = state.get("pausedByCritical") or {}

        if active_critical is None and paused_by_critical:
            budget = max(budget, ACTION_BUDGET_CRITICAL)
            paused_tasks = tasks_for_column(int(col_paused["id"]))
            paused_task_ids = {int(t.get("id")) for t, _ in paused_tasks}
            paused_by_id = {int(t.get("id")): (t, sl_id) for t, sl_id in paused_tasks}
            wip_now = len(tasks_for_column(int(col_wip["id"])))

            resume_to_wip, resume_to_ready, drop_ids = plan_resume_from_state(
                paused_by_critical, paused_task_ids, wip_now, WIP_LIMIT
            )

            def resume_task(tid: int, dest_col: int) -> None:
                nonlocal budget
                if budget <= 0:
                    return
                info = paused_by_critical.get(str(tid), {})
                sl_id = int(info.get("swimlaneId") or 0)
                if not sl_id:
                    sl_id = int(paused_by_id.get(tid, ({}, 0))[1])
                if dry_run:
                    actions.append(
                        f"Would resume paused #{tid} -> {'WIP' if dest_col==int(col_wip['id']) else 'Ready'}"
                    )
                else:
                    move_task(pid, tid, dest_col, 1, sl_id)
                    record_action(tid)
                    remove_tag(tid, TAG_PAUSED)
                    actions.append(
                        f"Resumed paused #{tid} -> {'WIP' if dest_col==int(col_wip['id']) else 'Ready'}"
                    )
                    paused_by_critical.pop(str(tid), None)
                budget -= 1

            for tid in resume_to_wip:
                resume_task(tid, int(col_wip["id"]))

            for tid in resume_to_ready:
                resume_task(tid, int(col_ready["id"]))

            for tid in drop_ids:
                if dry_run:
                    actions.append(f"Would clear paused state for #{tid} (no longer in Paused)")
                else:
                    paused_by_critical.pop(str(tid), None)
                    actions.append(f"Cleared paused state for #{tid} (no longer in Paused)")

            if not dry_run:
                state["pausedByCritical"] = paused_by_critical
                state["lastActionsByTaskId"] = last_actions
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

        if active_critical is not None:
            ct, csl_id, c_col_id = active_critical
            cid = int(ct.get("id"))
            ctitle = task_title(ct)

            # Critical stays active through Review; nothing else should run until it reaches Done.
            critical_in_progress = c_col_id in (int(col_wip["id"]), int(col_review["id"]))

            def pause_noncritical_wip() -> None:
                nonlocal budget
                budget = max(budget, ACTION_BUDGET_CRITICAL)
                current_wip = sorted(tasks_for_column(int(col_wip["id"])), key=sort_key)
                paused_state = state.get("pausedByCritical") or {}
                wip_by_id = {int(t.get("id")): (t, sl_id) for t, sl_id in current_wip}
                pause_ids = plan_pause_wip(
                    [int(t.get("id")) for t, _ in current_wip],
                    critical_task_ids,
                    paused_state,
                )

                for wid in pause_ids:
                    if budget <= 0:
                        break
                    wt, wsl_id = wip_by_id[wid]

                    if dry_run:
                        actions.append(f"Would pause non-critical WIP #{wid} -> Paused (for critical #{cid})")
                        budget -= 1
                        continue

                    move_task(pid, wid, int(col_paused["id"]), 1, int(wsl_id))
                    record_action(wid)
                    add_tag(wid, TAG_PAUSED)
                    paused_state[str(wid)] = {
                        "criticalTaskId": cid,
                        "pausedAtMs": now_ms(),
                        "swimlaneId": int(wsl_id),
                    }
                    actions.append(f"Paused WIP #{wid} -> Paused (for critical #{cid})")
                    budget -= 1

                state["pausedByCritical"] = paused_state

            if critical_in_progress:
                pause_noncritical_wip()

            else:
                full = get_task(cid)
                desc = (full.get("description") or "")
                deps = parse_depends_on(desc)
                unmet = [d for d in deps if not is_done(d)]

                if unmet:
                    reason = "Depends on " + ", ".join("#" + str(x) for x in unmet)
                    errors.append(f"critical #{cid} ({ctitle}) cannot start: {reason}")
                else:
                    pause_noncritical_wip()

                    critical_wip_exclusive_keys: set[str] = set()
                    for wt, _wsl in wip_tasks:
                        wid = int(wt.get("id"))
                        if wid == cid or wid not in critical_task_ids:
                            continue
                        wtags = get_task_tags(wid)
                        wdesc = (get_task(wid).get("description") or "")
                        for k in parse_exclusive_keys(wtags, wdesc):
                            critical_wip_exclusive_keys.add(k)

                    ctags = get_task_tags(cid)
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
                            actions.append(f"Would start critical #{cid} ({ctitle}) -> WIP")
                        else:
                            move_task(pid, cid, int(col_wip["id"]), 1, int(csl_id))
                            record_action(cid)
                            moved_to_wip.append(cid)
                            actions.append(f"Started critical #{cid} ({ctitle}) -> WIP")
                        budget -= 1

            # While critical exists anywhere not Done, freeze normal pulling.
            state["lastActionsByTaskId"] = last_actions
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

        # If WIP > limit, don't pull new work (MVP)
        if wip_count > WIP_LIMIT:
            actions.append(f"WIP is {wip_count} (> {WIP_LIMIT}); not pulling new work")
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

            # 1) If Ready is empty and Backlog has work, promote one item to Ready.
            if not ready_tasks_sorted and backlog_sorted:
                picked, epic_container, blocked_candidate = pick_next_backlog_action()

                # If the next candidate is blocked by deps/exclusive, move it to Blocked with a clear reason.
                if picked is None and blocked_candidate is not None and not dry_run:
                    bt, bsl_id, reason = blocked_candidate
                    bid = int(bt.get('id'))
                    btitle = task_title(bt)
                    move_task(pid, bid, int(col_blocked["id"]), 1, bsl_id)
                    record_action(bid)
                    actions.append(f"Moved Backlog #{bid} ({btitle}) -> Blocked (auto): {reason}")
                    budget -= 1
                    did_something = True
                    # refresh sorted lists next loop
                    ready_tasks_sorted = sorted(tasks_for_column(int(col_ready["id"])), key=sort_key)
                    backlog_sorted = sorted(tasks_for_column(int(col_backlog["id"])), key=sort_key)
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
            if budget > 0 and wip_count < WIP_LIMIT and ready_tasks_sorted:
                candidate, sl_id = ready_tasks_sorted[0]
                cid = int(candidate.get("id"))
                tags = get_task_tags(cid)

                if is_held(tags):
                    # skip held
                    ready_tasks_sorted = ready_tasks_sorted[1:]
                    continue

                full = get_task(cid)
                desc = (full.get('description') or '')
                deps = parse_depends_on(desc)
                unmet = [d for d in deps if not is_done(d)]
                if unmet and not dry_run:
                    ctitle = task_title(candidate)
                    move_task(pid, cid, int(col_blocked["id"]), 1, sl_id)
                    record_action(cid)
                    actions.append(
                        f"Moved Ready #{cid} ({ctitle}) -> Blocked (auto): Depends on {', '.join('#'+str(x) for x in unmet)}"
                    )
                    budget -= 1
                    did_something = True
                    # refresh lists
                    ready_tasks_sorted = sorted(tasks_for_column(int(col_ready["id"])), key=sort_key)
                    continue

                ex_keys = parse_exclusive_keys(tags, desc)
                if any(k in wip_exclusive_keys for k in ex_keys):
                    # exclusive conflict, keep in Ready but don't start
                    ctitle = task_title(candidate)
                    actions.append(
                        f"Skipped Ready #{cid} ({ctitle}) due to exclusive conflict: {', '.join('exclusive:'+k for k in ex_keys if k in wip_exclusive_keys)}"
                    )
                    # move to end of ready queue for now
                    ready_tasks_sorted = ready_tasks_sorted[1:] + [(candidate, sl_id)]
                    budget -= 1
                    did_something = True
                    continue

                ctitle = task_title(candidate)
                if dry_run:
                    actions.append(f"Would move Ready #{cid} ({ctitle}) -> WIP")
                else:
                    move_task(pid, cid, int(col_wip["id"]), 1, sl_id)
                    record_action(cid)
                    moved_to_wip.append(cid)
                    actions.append(f"Moved Ready #{cid} ({ctitle}) -> WIP")
                # simulate state
                ready_tasks_sorted = ready_tasks_sorted[1:]
                wip_count += 1
                for k in ex_keys:
                    wip_exclusive_keys.add(k)
                budget -= 1
                did_something = True

            if not did_something:
                break

        # Persist state updates
        state["lastActionsByTaskId"] = last_actions
        if dry_run:
            if dry_runs_remaining > 0:
                state["dryRunRunsRemaining"] = dry_runs_remaining - 1
                if state["dryRunRunsRemaining"] <= 0:
                    state["dryRun"] = False
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
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
