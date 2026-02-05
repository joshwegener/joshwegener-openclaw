from __future__ import annotations

import json
import time
import calendar
from typing import Any, Dict, Optional


def parse_heartbeat_text(text: str) -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(text)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def heartbeat_ts_epoch_s(hb: Dict[str, Any]) -> Optional[int]:
    try:
        v = int(hb.get("tsEpochS") or 0)
        if v > 0:
            return v
    except Exception:
        pass

    ts = hb.get("ts")
    if not isinstance(ts, str) or not ts:
        return None
    # Expected: 2026-02-05T03:26:30Z
    try:
        t = time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        return int(calendar.timegm(t))
    except Exception:
        return None


def heartbeat_age_s(hb: Dict[str, Any], *, now_s: Optional[int] = None) -> Optional[int]:
    if now_s is None:
        now_s = int(time.time())
    ts = heartbeat_ts_epoch_s(hb)
    if ts is None:
        return None
    try:
        return max(0, int(now_s) - int(ts))
    except Exception:
        return None


def is_heartbeat_stale(
    hb: Optional[Dict[str, Any]],
    *,
    now_s: Optional[int] = None,
    tick_seconds: int = 20,
    factor: int = 3,
) -> bool:
    if now_s is None:
        now_s = int(time.time())
    try:
        tick_seconds = int(tick_seconds)
    except Exception:
        tick_seconds = 20
    try:
        factor = int(factor)
    except Exception:
        factor = 3
    threshold = max(1, tick_seconds * max(1, factor))

    if not hb:
        return True
    age = heartbeat_age_s(hb, now_s=now_s)
    if age is None:
        return True
    return age >= threshold
