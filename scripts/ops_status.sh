#!/usr/bin/env bash
set -euo pipefail

echo "=== clawd ==="
HB_PATH="/Users/joshwegener/clawd/memory/orchestrator-heartbeat.json"
GUARD_STATE="/Users/joshwegener/clawd/memory/orchestrator-guardian-state.json"

echo "heartbeat: $HB_PATH"
python3 - <<'PY' || true
import json, time
from pathlib import Path

path = Path("/Users/joshwegener/clawd/memory/orchestrator-heartbeat.json")
if not path.is_file():
    print("  missing")
    raise SystemExit(0)

try:
    hb = json.loads(path.read_text(encoding="utf-8"))
except Exception as e:
    print(f"  unreadable: {e}")
    raise SystemExit(0)

now = int(time.time())
ts = None
try:
    ts = int(hb.get("tsEpochS") or 0) or None
except Exception:
    ts = None
age = (now - ts) if ts else None

ts_iso = hb.get("ts") or ""
pid = hb.get("pid")
ver = hb.get("version") or ""
phase = hb.get("phase") or ""
tick = hb.get("tickSeconds")

age_s = f"{age}s" if isinstance(age, int) else "?"
print(f"  age={age_s} ts={ts_iso} pid={pid} version={ver} phase={phase} tickSeconds={tick}")
PY

if [[ -f "$GUARD_STATE" ]]; then
  echo "guardian state: $GUARD_STATE"
  python3 - <<'PY' || true
import json
from pathlib import Path

path = Path("/Users/joshwegener/clawd/memory/orchestrator-guardian-state.json")
try:
    st = json.loads(path.read_text(encoding="utf-8"))
except Exception as e:
    print(f"  unreadable: {e}")
    raise SystemExit(0)

print(f"  lastCheckAtS={st.get('lastCheckAtS')} lastHeartbeatAgeS={st.get('lastHeartbeatAgeS')} blockedUntilS={st.get('blockedUntilS')}")
hist = st.get("restartHistoryS")
if isinstance(hist, list):
    print(f"  restartHistoryS(last {len(hist)}): {hist[-5:]}")
PY
fi

if tmux ls 2>/dev/null | rg -q '^clawd:'; then
  tmux list-windows -t clawd -F '#{window_index}:#{window_name} panes=#{window_panes} active=#{window_active}' || true
else
  echo "tmux: no clawd session"
fi
pgrep -fl 'run_orchestrator_loop.sh|board_orchestrator.py' || echo "process: none"

echo
echo "=== openclaw ==="
if tmux ls 2>/dev/null | rg -q '^openclaw:'; then
  tmux list-windows -t openclaw -F '#{window_index}:#{window_name} panes=#{window_panes} active=#{window_active}' || true
else
  echo "tmux: no openclaw session"
fi

lsof -nP -iTCP:18789 -sTCP:LISTEN >/dev/null 2>&1 && echo "gateway: listening on 18789" || echo "gateway: not listening"
openclaw status | sed -n '1,70p' || true
