#!/usr/bin/env bash
set -euo pipefail

TMPDIR="$(mktemp -d)"
cleanup() {
  tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
  rm -rf "$TMPDIR" 2>/dev/null || true
}
trap cleanup EXIT

TMUX_SESSION="clawd-smoke-$$"
HB_PATH="${TMPDIR}/orchestrator-heartbeat.json"
LOOP="${TMPDIR}/orch_loop.sh"

cat >"$LOOP" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

HEARTBEAT_PATH="${CLAWD_ORCHESTRATOR_HEARTBEAT_PATH:?}"
TICK_SECONDS="${CLAWD_TICK_SECONDS:-1}"
VERSION="${CLAWD_VERSION:-smoke}"

write_hb() {
  local ts_iso ts_epoch tmp
  ts_iso="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  ts_epoch="$(date -u '+%s')"
  tmp="$(mktemp "${HEARTBEAT_PATH}.tmp.XXXXXX")"
  echo "{\"ts\":\"$ts_iso\",\"tsEpochS\":$ts_epoch,\"pid\":$$,\"version\":\"$VERSION\",\"tickSeconds\":$TICK_SECONDS}" >"$tmp"
  mv -f "$tmp" "$HEARTBEAT_PATH"
}

while true; do
  write_hb || true
  sleep "$TICK_SECONDS"
done
EOF
chmod +x "$LOOP"

export CLAWD_TMUX_SESSION="$TMUX_SESSION"
export CLAWD_ORCHESTRATOR_WINDOW_CMD="$LOOP"
export CLAWD_ORCHESTRATOR_HEARTBEAT_PATH="$HB_PATH"
export CLAWD_TICK_SECONDS="1"
export CLAWD_VERSION="smoke"
export KANBOARD_USER="smoke"
export KANBOARD_TOKEN="smoke"

echo "Bringing up smoke tmux session: $TMUX_SESSION"
/Users/joshwegener/clawd/scripts/tmux_up.sh >/dev/null

echo "Waiting for heartbeat..."
for i in {1..20}; do
  [[ -f "$HB_PATH" ]] && break
  sleep 0.2
done
if [[ ! -f "$HB_PATH" ]]; then
  echo "FAIL: heartbeat never appeared at $HB_PATH" >&2
  exit 1
fi

HB_BEFORE="$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["tsEpochS"]))' "$HB_PATH" 2>/dev/null || echo 0)"
echo "Heartbeat before: $HB_BEFORE"

echo "Killing orchestrator pane/window..."
tmux kill-window -t "${TMUX_SESSION}:orchestrator" 2>/dev/null || true

echo "Running guardian..."
python3 /Users/joshwegener/clawd/scripts/orchestrator_guardian.py >/dev/null 2>&1 || true

echo "Waiting for orchestrator to be restored..."
for i in {1..20}; do
  tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | rg -q '^orchestrator$' && break
  sleep 0.2
done
if ! tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | rg -q '^orchestrator$'; then
  echo "FAIL: guardian did not restore orchestrator window" >&2
  exit 1
fi

echo "Waiting for heartbeat to advance..."
for i in {1..20}; do
  HB_AFTER="$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["tsEpochS"]))' "$HB_PATH" 2>/dev/null || echo 0)"
  if [[ "$HB_AFTER" -gt "$HB_BEFORE" ]]; then
    echo "OK: heartbeat advanced ($HB_BEFORE -> $HB_AFTER)"
    exit 0
  fi
  sleep 0.2
done

echo "FAIL: heartbeat did not advance after guardian repair" >&2
exit 1
