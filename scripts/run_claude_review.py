#!/usr/bin/env python3
"""Run Claude review prompt and write a parseable review_result line.

Purpose: keep the reviewer pipeline robust (timeouts, non-JSON output) and
avoid brittle shell quoting.

Writes to LOG_PATH:
- ### REVIEW START ...
- raw model output (best-effort)
- review_result: {"score":...,"verdict":...,"critical_items":[...],"notes":"..."}

Exit code is not important (used from nohup).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from typing import Any, Dict, Optional


def utc_now() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def append_line(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


def compact_json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def parse_review_json(s: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(s)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if "score" not in obj or "verdict" not in obj:
        return None
    return obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-path", required=True)
    ap.add_argument("--log-path", required=True)
    ap.add_argument("--model", default=os.environ.get("CLAUDE_MODEL", "opus"))
    ap.add_argument("--timeout-sec", type=int, default=int(os.environ.get("CLAUDE_REVIEW_TIMEOUT_SEC", "600")))
    ap.add_argument("--prompt", required=True)
    args = ap.parse_args()

    log_path = args.log_path
    append_line(log_path, f"### REVIEW START {utc_now()}")

    cmd = [
        "claude",
        "-p",
        "--model",
        args.model,
        "--dangerously-skip-permissions",
        "--output-format",
        "text",
        args.prompt,
    ]

    try:
        p = subprocess.run(
            cmd,
            cwd=args.repo_path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result = {
            "score": 1,
            "verdict": "BLOCKER",
            "critical_items": [f"Reviewer timed out after {args.timeout_sec}s"],
            "notes": "Claude review command did not return in time; investigate claude CLI/auth/quota.",
        }
        append_line(log_path, "review_result: " + compact_json(result))
        return 0
    except Exception as e:
        result = {
            "score": 1,
            "verdict": "BLOCKER",
            "critical_items": [f"Reviewer execution error: {type(e).__name__}: {e}"],
            "notes": "Claude review command failed to execute.",
        }
        append_line(log_path, "review_result: " + compact_json(result))
        return 0

    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()

    # Save raw output for debugging, but keep it bounded.
    if out:
        append_line(log_path, out[:20000])
    if err:
        append_line(log_path, "[stderr] " + err[:20000])

    parsed = parse_review_json(out)
    if not parsed:
        # Non-JSON output: treat as blocker.
        result = {
            "score": 1,
            "verdict": "BLOCKER",
            "critical_items": [
                "Reviewer did not output valid JSON",
                f"exit_code={p.returncode}",
            ],
            "notes": (out[:400] if out else (err[:400] if err else "no output")),
        }
        append_line(log_path, "review_result: " + compact_json(result))
        return 0

    # Normalize fields + ensure marker line.
    try:
        score = int(parsed.get("score"))
    except Exception:
        score = 1
    verdict = str(parsed.get("verdict") or "").strip().upper()
    if verdict not in ("PASS", "REWORK", "BLOCKER"):
        verdict = "BLOCKER"

    critical_items = parsed.get("critical_items")
    if not isinstance(critical_items, list):
        critical_items = []

    result = {
        "score": max(1, min(100, score)),
        "verdict": verdict,
        "critical_items": [str(x) for x in critical_items if str(x).strip()][:20],
        "notes": str(parsed.get("notes") or "")[:1000],
    }

    append_line(log_path, "review_result: " + compact_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
