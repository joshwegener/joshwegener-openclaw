#!/usr/bin/env python3
"""Read JSON from stdin or a file path and emit compact single-line JSON.

Usage:
  python3 compact_json.py /path/to/file.json
  cat file.json | python3 compact_json.py
"""

import json
import sys
from typing import Any


def main() -> int:
    data: Any
    if len(sys.argv) > 1 and sys.argv[1] != "-":
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)

    sys.stdout.write(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
