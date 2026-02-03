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

def write_json(path: str, obj: Any) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True, ensure_ascii=False)


def compact_json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def parse_review_json_obj(obj: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    if "score" not in obj or "verdict" not in obj:
        return None
    return obj


def extract_review_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract a {score, verdict, ...} JSON object from model output.

    Claude sometimes prints prose plus a fenced JSON block.
    We try to recover the embedded JSON object.
    """
    if not text:
        return None

    s = text.strip()

    # Fast path: pure JSON.
    if s.startswith("{"):
        try:
            return parse_review_json_obj(json.loads(s))
        except Exception:
            pass

    # Heuristic: find an object starting with {"score" (optionally with whitespace)
    candidates = []
    for needle in ("{\"score\"", "{ \"score\""):
        start = 0
        while True:
            i = s.find(needle, start)
            if i < 0:
                break
            candidates.append(i)
            start = i + 1

    # Also try to locate JSON in fenced code blocks by looking for "score" key.
    if not candidates:
        j = s.find("\"score\"")
        if j >= 0:
            # back up to nearest '{'
            i = s.rfind("{", 0, j)
            if i >= 0:
                candidates.append(i)

    def brace_slice(src: str, start_idx: int) -> Optional[str]:
        depth = 0
        end = None
        for k in range(start_idx, len(src)):
            ch = src[k]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        if end is None:
            return None
        return src[start_idx:end]

    for i in candidates:
        frag = brace_slice(s, i)
        if not frag:
            continue
        try:
            obj = json.loads(frag)
        except Exception:
            continue
        parsed = parse_review_json_obj(obj)
        if parsed:
            return parsed

    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-path", required=True)
    ap.add_argument("--log-path", required=True)
    ap.add_argument("--result-path", default="")
    ap.add_argument("--model", default=os.environ.get("CLAUDE_MODEL", "opus"))
    ap.add_argument("--timeout-sec", type=int, default=int(os.environ.get("CLAUDE_REVIEW_TIMEOUT_SEC", "600")))
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--revision", default="")
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
        if args.revision:
            result["reviewRevision"] = args.revision
        append_line(log_path, "review_result: " + compact_json(result))
        write_json(args.result_path, result)
        return 0
    except Exception as e:
        result = {
            "score": 1,
            "verdict": "BLOCKER",
            "critical_items": [f"Reviewer execution error: {type(e).__name__}: {e}"],
            "notes": "Claude review command failed to execute.",
        }
        if args.revision:
            result["reviewRevision"] = args.revision
        append_line(log_path, "review_result: " + compact_json(result))
        write_json(args.result_path, result)
        return 0

    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()

    # Save raw output for debugging, but keep it bounded.
    if out:
        append_line(log_path, out[:20000])
    if err:
        append_line(log_path, "[stderr] " + err[:20000])

    parsed = extract_review_json_from_text(out)
    if not parsed:
        # Could not recover JSON: treat as blocker.
        result = {
            "score": 1,
            "verdict": "BLOCKER",
            "critical_items": [
                "Reviewer did not output valid JSON (or could not be extracted)",
                f"exit_code={p.returncode}",
            ],
            "notes": (out[:400] if out else (err[:400] if err else "no output")),
        }
        if args.revision:
            result["reviewRevision"] = args.revision
        append_line(log_path, "review_result: " + compact_json(result))
        write_json(args.result_path, result)
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
    if args.revision:
        result["reviewRevision"] = args.revision

    append_line(log_path, "review_result: " + compact_json(result))
    write_json(args.result_path, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
