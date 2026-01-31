#!/usr/bin/env python3
"""Overnight RecallDeck safety check.

Outputs:
- "NO_REPLY" when everything looks healthy.
- "ALERT: ..." when something needs attention.

Designed to be run from a Clawdbot cron isolated session.
Uses ONLY stdlib + Kanboard JSON-RPC.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.request

KANBOARD_BASE = os.environ.get("KANBOARD_BASE", "http://localhost:8401/jsonrpc.php")
KANBOARD_USER = os.environ.get("KANBOARD_USER", "rook")
KANBOARD_TOKEN = os.environ.get("KANBOARD_TOKEN")
PROJECT_NAME = os.environ.get("RECALLDECK_PROJECT", "RecallDeck")
TARGET_SWIMLANE = os.environ.get("RECALLDECK_SWIMLANE", "MVP")
STATE_PATH = os.environ.get(
    "RECALLDECK_STATE_PATH",
    "/Users/joshwegener/clawd/memory/board-orchestrator-state.json",
)

# Spam control: suppress repeating the exact same alert for a short window.
ALERT_STATE_PATH = os.environ.get(
    "RECALLDECK_ALERT_STATE_PATH",
    "/Users/joshwegener/clawd/memory/overnight-safety-state.json",
)
ALERT_DEDUP_SECONDS = int(os.environ.get("RECALLDECK_ALERT_DEDUP_SECONDS", "1800"))

REVIEW_STALE_SECONDS = int(os.environ.get("RECALLDECK_REVIEW_STALE_SECONDS", "7200"))


def load_alert_state() -> dict:
    try:
        with open(ALERT_STATE_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_alert_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(ALERT_STATE_PATH), exist_ok=True)
        with open(ALERT_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except Exception:
        pass


def die_alert(msg: str, code: int = 0) -> None:
    # Dedupe repeated identical alerts to avoid spamming during emergency polling.
    try:
        st = load_alert_state()
        last_msg = str(st.get("lastAlert") or "")
        last_at = int(st.get("lastAlertAtS") or 0)
        now_s = int(time.time())
        if last_msg == msg and last_at and (now_s - last_at) < ALERT_DEDUP_SECONDS:
            print("NO_REPLY")
            raise SystemExit(code)
        st["lastAlert"] = msg
        st["lastAlertAtS"] = now_s
        save_alert_state(st)
    except SystemExit:
        raise
    except Exception:
        pass

    print(f"ALERT: {msg}")
    raise SystemExit(code)


def rpc(method: str, params: dict | None = None):
    if not KANBOARD_TOKEN:
        die_alert("KANBOARD_TOKEN missing")

    payload: dict = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params is not None:
        payload["params"] = params

    auth = base64.b64encode(f"{KANBOARD_USER}:{KANBOARD_TOKEN}".encode()).decode()
    req = urllib.request.Request(
        KANBOARD_BASE,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read().decode())
    except Exception as e:
        die_alert(f"Kanboard RPC failed ({method}): {e}")

    if out.get("error"):
        die_alert(f"Kanboard RPC error ({method}): {out['error']}")
    return out.get("result")


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        die_alert(f"Failed reading state file {STATE_PATH}: {e}")


def main() -> None:
    now_s = int(time.time())

    proj = rpc("getProjectByName", {"name": PROJECT_NAME})
    if not proj or "id" not in proj:
        die_alert(f"Project not found: {PROJECT_NAME!r}")
    project_id = int(proj["id"])

    cols = rpc("getColumns", {"project_id": project_id})
    col_by_id = {int(c["id"]): (c.get("title") or "").strip() for c in cols or []}

    swimlanes = rpc("getActiveSwimlanes", {"project_id": project_id})
    swimlane_id = None
    swimlane_by_id = {int(s["id"]): (s.get("name") or "").strip() for s in swimlanes or []}
    for sid, name in swimlane_by_id.items():
        if name == TARGET_SWIMLANE:
            swimlane_id = sid
            break
    if swimlane_id is None:
        die_alert(f"Swimlane not found: {TARGET_SWIMLANE!r}")

    tasks = rpc("getAllTasks", {"project_id": project_id}) or []

    def in_mvp(t: dict) -> bool:
        try:
            return int(t.get("swimlane_id") or 0) == swimlane_id
        except Exception:
            return False

    mvp_tasks = [t for t in tasks if in_mvp(t)]

    # Counts by column title
    counts: dict[str, int] = {}
    for t in mvp_tasks:
        title = col_by_id.get(int(t.get("column_id") or 0), "?")
        counts[title] = counts.get(title, 0) + 1

    backlog_n = counts.get("Backlog", 0)
    ready_n = counts.get("Ready", 0)
    wip_n = counts.get("Work in progress", 0)
    review_n = counts.get("Review", 0)
    blocked_n = counts.get("Blocked", 0)

    alerts: list[str] = []

    if wip_n == 0 and backlog_n > 0:
        alerts.append("MVP WIP is 0 while Backlog has tasks")

    if ready_n == 0 and wip_n < 2 and backlog_n > 0:
        alerts.append("MVP Ready is 0 while WIP < 2 and Backlog has tasks")

    if blocked_n > 0:
        blocked = [
            (int(t["id"]), (t.get("title") or "").strip())
            for t in mvp_tasks
            if col_by_id.get(int(t.get("column_id") or 0)) == "Blocked"
        ]
        blocked.sort(key=lambda x: x[0])
        blocked_str = ", ".join([f"#{tid} {title}" for tid, title in blocked[:5]])
        alerts.append(f"MVP Blocked tasks: {blocked_n} ({blocked_str})")

    if review_n > 0:
        stale: list[tuple[int, str, int]] = []
        for t in mvp_tasks:
            if col_by_id.get(int(t.get("column_id") or 0)) != "Review":
                continue
            tid = int(t["id"])
            title = (t.get("title") or "").strip()
            # Kanboard returns unix seconds in various fields; date_modification is most useful.
            mod_s = int(t.get("date_modification") or t.get("date_creation") or 0)
            age_s = max(0, now_s - mod_s) if mod_s else 0
            if mod_s and age_s >= REVIEW_STALE_SECONDS:
                stale.append((tid, title, age_s))

        if stale:
            stale.sort(key=lambda x: x[2], reverse=True)
            top = stale[0]
            alerts.append(
                f"MVP Review has stale items (>= {REVIEW_STALE_SECONDS//3600}h); oldest: #{top[0]} {top[1]} (~{top[2]//3600}h)"
            )
        else:
            # Review has items but none are stale; treat as healthy.
            pass

    # Worker mapping cross-check
    state = load_state()
    workers_by_task = (state.get("workersByTaskId") or {})

    wip_tasks = [
        t for t in mvp_tasks if col_by_id.get(int(t.get("column_id") or 0)) == "Work in progress"
    ]

    missing_workers: list[tuple[int, str]] = []
    for t in wip_tasks:
        tid = int(t["id"])
        if str(tid) not in workers_by_task and tid not in workers_by_task:
            missing_workers.append((tid, (t.get("title") or "").strip()))

    if missing_workers:
        missing_workers.sort(key=lambda x: x[0])
        msg = ", ".join([f"#{tid} {title}" for tid, title in missing_workers[:5]])
        alerts.append(f"MVP WIP tasks missing worker entries: {msg}")

    if alerts:
        die_alert("; ".join(alerts))

    print("NO_REPLY")


if __name__ == "__main__":
    main()
