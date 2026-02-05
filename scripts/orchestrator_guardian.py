#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.orchestrator_guardian_lib import is_heartbeat_stale, parse_heartbeat_text, heartbeat_age_s


def now_s() -> int:
    return int(time.time())


def load_json(path: str) -> Dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def save_json(path: str, payload: Dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    Path(tmp).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def sha256(s: str) -> str:
    try:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def notify(message: str, *, state: Dict[str, Any], key: str, cooldown_s: int = 3600) -> None:
    cmd = (os.environ.get("BOARD_ORCHESTRATOR_NOTIFY_CMD") or "").strip()
    if not cmd:
        return

    n = state.get("notify")
    if not isinstance(n, dict):
        n = {}
        state["notify"] = n
    cur = n.get(key)
    if not isinstance(cur, dict):
        cur = {}

    digest = sha256(message)
    last_at = int(cur.get("lastAtS") or 0)
    last_digest = str(cur.get("lastDigest") or "")
    if digest and digest == last_digest and last_at and (now_s() - last_at) < max(1, cooldown_s):
        return

    env = dict(os.environ)
    env["BOARD_ORCHESTRATOR_NOTIFY_MESSAGE"] = message
    try:
        subprocess.run(
            cmd,
            shell=True,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=10,
        )
    except Exception:
        pass

    n[key] = {"lastAtS": now_s(), "lastDigest": digest}


def tmux_bin() -> str:
    return shutil.which("tmux") or ""


def run_tmux(args: List[str], *, timeout: int = 2) -> Tuple[int, str]:
    tb = tmux_bin()
    if not tb:
        return 127, ""
    try:
        p = subprocess.run([tb] + args, check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        return int(p.returncode or 0), (p.stdout or b"").decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, ""
    except Exception:
        return 1, ""


def has_session(session: str) -> bool:
    code, _ = run_tmux(["has-session", "-t", session])
    return code == 0


def window_id_by_name(session: str, window_name: str) -> Optional[str]:
    code, out = run_tmux(["list-windows", "-t", session, "-F", "#{window_id}:#{window_name}"])
    if code != 0:
        return None
    for line in out.splitlines():
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        wid, name = parts[0].strip(), parts[1].strip()
        if name == window_name and wid:
            return wid
    return None


def first_pane_id(window_id: str) -> Optional[str]:
    code, out = run_tmux(["list-panes", "-t", window_id, "-F", "#{pane_id}"])
    if code != 0:
        return None
    for line in out.splitlines():
        pane = line.strip()
        if pane:
            return pane
    return None


def respawn_pane(pane_id: str, cmd: str) -> bool:
    # tmux expects a single string for the command; run via bash -lc for consistent PATH/env.
    tmux_cmd = f"bash -lc {shlex.quote(cmd)}"
    code, _ = run_tmux(["respawn-pane", "-k", "-t", pane_id, tmux_cmd], timeout=5)
    return code == 0


def read_heartbeat(path: str) -> Optional[Dict[str, Any]]:
    try:
        txt = Path(path).read_text(encoding="utf-8")
    except Exception:
        return None
    return parse_heartbeat_text(txt)


def restart_block_active(state: Dict[str, Any], *, now_epoch_s: int) -> bool:
    try:
        blocked_until = int(state.get("blockedUntilS") or 0)
    except Exception:
        blocked_until = 0
    return bool(blocked_until and now_epoch_s < blocked_until)


def restart_limiter_allows(state: Dict[str, Any], *, now_epoch_s: int, max_restarts: int, window_s: int) -> bool:
    history = state.get("restartHistoryS")
    if not isinstance(history, list):
        history = []
    history2: List[int] = []
    for x in history:
        try:
            xi = int(x)
        except Exception:
            continue
        if now_epoch_s - xi <= window_s:
            history2.append(xi)
    state["restartHistoryS"] = history2

    if len(history2) >= max(1, max_restarts):
        return False
    return True


def record_restart(state: Dict[str, Any], *, now_epoch_s: int, window_s: int) -> None:
    history = state.get("restartHistoryS")
    if not isinstance(history, list):
        history = []
    try:
        history.append(int(now_epoch_s))
    except Exception:
        pass
    pruned: List[int] = []
    for x in history:
        try:
            xi = int(x)
        except Exception:
            continue
        if now_epoch_s - xi <= window_s:
            pruned.append(xi)
    state["restartHistoryS"] = pruned


def main() -> int:
    session = (os.environ.get("CLAWD_TMUX_SESSION") or "clawd").strip() or "clawd"
    window_name = (os.environ.get("CLAWD_ORCHESTRATOR_WINDOW_NAME") or "orchestrator").strip() or "orchestrator"
    heartbeat_path = (
        os.environ.get("CLAWD_ORCHESTRATOR_HEARTBEAT_PATH") or "/Users/joshwegener/clawd/memory/orchestrator-heartbeat.json"
    )
    tick_seconds = int(os.environ.get("CLAWD_TICK_SECONDS") or "20")
    stale_factor = int(os.environ.get("CLAWD_ORCHESTRATOR_HEARTBEAT_STALE_FACTOR") or "3")

    max_restarts = int(os.environ.get("CLAWD_ORCHESTRATOR_GUARD_MAX_RESTARTS") or "5")
    restart_window_min = int(os.environ.get("CLAWD_ORCHESTRATOR_GUARD_RESTART_WINDOW_MIN") or "10")
    restart_window_s = max(60, restart_window_min * 60)
    block_min = int(os.environ.get("CLAWD_ORCHESTRATOR_GUARD_BLOCK_MIN") or "30")
    block_s = max(60, block_min * 60)

    state_path = (
        os.environ.get("CLAWD_ORCHESTRATOR_GUARD_STATE")
        or "/Users/joshwegener/clawd/memory/orchestrator-guardian-state.json"
    )
    state = load_json(state_path)
    state.setdefault("schemaVersion", 1)

    if not tmux_bin():
        notify("RecallDeck orchestrator guardian: tmux not found; cannot self-heal.", state=state, key="tmux-missing")
        save_json(state_path, state)
        return 0

    # If Kanboard env is missing, do not thrash restarts. Notify once (best-effort).
    env_ok = bool((os.environ.get("KANBOARD_USER") or "").strip() and (os.environ.get("KANBOARD_TOKEN") or "").strip())
    if not env_ok:
        state["missingEnv"] = True
        notify(
            "RecallDeck orchestrator guardian: missing Kanboard env (KANBOARD_USER/KANBOARD_TOKEN). Not restarting until fixed.",
            state=state,
            key="missing-env",
            cooldown_s=24 * 3600,
        )
        save_json(state_path, state)
        return 0
    if state.get("missingEnv"):
        state["missingEnv"] = False

    now_epoch_s = now_s()
    if not restart_block_active(state, now_epoch_s=now_epoch_s):
        # Clear expired blocks (do not keep stale timestamps around).
        try:
            if int(state.get("blockedUntilS") or 0) and now_epoch_s >= int(state.get("blockedUntilS") or 0):
                state["blockedUntilS"] = 0
        except Exception:
            state["blockedUntilS"] = 0

    clawd_home = os.environ.get("CLAWD_HOME") or "/Users/joshwegener/clawd"
    tmux_up = os.path.join(clawd_home, "scripts", "tmux_up.sh")
    window_cmd = os.environ.get("CLAWD_ORCHESTRATOR_WINDOW_CMD") or os.path.join(clawd_home, "scripts", "run_orchestrator_loop.sh")

    pane_id: Optional[str] = None
    hb: Optional[Dict[str, Any]] = None
    stale = True
    needs_repair = False
    repair_reason = ""
    did_repair = False

    if not has_session(session):
        needs_repair = True
        repair_reason = "session-missing"
    else:
        wid = window_id_by_name(session, window_name)
        if not wid:
            needs_repair = True
            repair_reason = "window-missing"
        else:
            pane_id = first_pane_id(wid)
            if not pane_id:
                needs_repair = True
                repair_reason = "pane-missing"
            else:
                hb = read_heartbeat(heartbeat_path)
                stale = is_heartbeat_stale(hb, now_s=now_epoch_s, tick_seconds=tick_seconds, factor=stale_factor)
                if stale:
                    needs_repair = True
                    repair_reason = "heartbeat-stale"

    if needs_repair:
        # If we are currently blocked due to restart thrash, do not attempt repair.
        if restart_block_active(state, now_epoch_s=now_epoch_s):
            state["lastCheckAtS"] = now_epoch_s
            state["lastRepairReason"] = repair_reason
            save_json(state_path, state)
            return 0

        if not restart_limiter_allows(state, now_epoch_s=now_epoch_s, max_restarts=max_restarts, window_s=restart_window_s):
            state["blockedUntilS"] = now_epoch_s + block_s
            state["lastCheckAtS"] = now_epoch_s
            state["lastRepairReason"] = repair_reason
            notify(
                f"RecallDeck orchestrator guardian: restart loop detected (>= {max_restarts} attempts in {restart_window_min}m). Pausing auto-repair for {block_min}m.",
                state=state,
                key="restart-loop",
                cooldown_s=block_s,
            )
            save_json(state_path, state)
            return 0

        if repair_reason in ("session-missing", "window-missing", "pane-missing"):
            try:
                subprocess.run([tmux_up], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            except Exception:
                pass
            record_restart(state, now_epoch_s=now_epoch_s, window_s=restart_window_s)
            state["lastRepairAtS"] = now_epoch_s
            state["lastRepairReason"] = repair_reason
            did_repair = True

        if repair_reason == "heartbeat-stale":
            ok = False
            if pane_id:
                ok = respawn_pane(pane_id, window_cmd)
            if not ok:
                try:
                    subprocess.run([tmux_up], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                except Exception:
                    pass
            record_restart(state, now_epoch_s=now_epoch_s, window_s=restart_window_s)
            state["lastRepairAtS"] = now_epoch_s
            state["lastRepairReason"] = repair_reason
            did_repair = True

    # If heartbeat is healthy (or no repair needed), clear any prior block.
    if not needs_repair and not did_repair:
        state["blockedUntilS"] = 0

    # Store a tiny status snapshot (useful for ops_status without parsing tmux).
    try:
        age = heartbeat_age_s(hb or {}, now_s=now_epoch_s)
    except Exception:
        age = None
    state["lastCheckAtS"] = now_epoch_s
    state["lastHeartbeatAgeS"] = age
    state["lastHeartbeatPath"] = heartbeat_path

    save_json(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
