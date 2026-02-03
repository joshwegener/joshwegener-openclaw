#!/usr/bin/env bash
set -euo pipefail

# Tail the newest N log files under a directory tree.
#
# Usage:
#   tail_latest_logs.sh <root_dir> <basename> [max_files] [tail_lines]
#
# Example:
#   tail_latest_logs.sh /Users/joshwegener/clawd/runs/worker worker.log 20 200
#
# Notes:
# - Uses a one-time "newest N by mtime" selection (good enough for interactive monitoring).
# - Falls back to an interactive shell if no logs exist yet.

ROOT_DIR="${1:?root_dir}"
BASENAME="${2:?basename}"
MAX_FILES="${3:-20}"
TAIL_LINES="${4:-200}"

files0="$(python3 - <<'PY' "$ROOT_DIR" "$BASENAME" "$MAX_FILES"
import os
import sys

root = sys.argv[1]
base = sys.argv[2]
try:
    max_files = int(sys.argv[3])
except Exception:
    max_files = 20

matches = []
for dirpath, _dirnames, filenames in os.walk(root):
    if base not in filenames:
        continue
    path = os.path.join(dirpath, base)
    try:
        st = os.stat(path)
    except OSError:
        continue
    matches.append((st.st_mtime, path))

matches.sort(reverse=True)
for _mt, p in matches[:max_files]:
    sys.stdout.write(p + "\0")
PY
)"

if [[ -z "$files0" ]]; then
  echo "No logs found under $ROOT_DIR ($BASENAME)." >&2
  exec bash
fi

printf '%s' "$files0" | xargs -0 tail -n "$TAIL_LINES" -F

