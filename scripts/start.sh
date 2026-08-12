#!/usr/bin/env bash
# Start Switch-codex (uvicorn) in tmux. Does not manage cloudflared.
# Env:
#   SR_FORCE=1   always restart even if health is already ok
#   SW_PORT      listen port (default 4100; SR_PORT also accepted for compatibility)
#   SW_HOST      listen host (default 127.0.0.1)
#   SR_TMUX_SESSION  tmux session name (default switchyard)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SESSION="${SR_TMUX_SESSION:-switchyard}"
HOST="${SW_HOST:-127.0.0.1}"
PORT="${SW_PORT:-${SR_PORT:-4100}}"
VENV="$ROOT/.venv"
LOG="$ROOT/logs/switchyard.tmux.log"
FORCE="${SR_FORCE:-0}"
mkdir -p "$ROOT/logs"

if [[ ! -x "$VENV/bin/uvicorn" ]]; then
  echo "installing deps..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r "$ROOT/requirements.txt"
fi

if [[ "$FORCE" != "1" ]] && curl -fsS --max-time 2 "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
  echo "already healthy on :$PORT (set SR_FORCE=1 to restart and reload code)"
  curl -sS "http://${HOST}:${PORT}/health"; echo
  exit 0
fi

# Ensure a clean start when forcing / when not healthy.
"$ROOT/scripts/stop.sh" >/dev/null 2>&1 || true

tmux new-session -d -s "$SESSION" \
  "bash -lc 'cd \"$ROOT\" && exec \"$VENV/bin/uvicorn\" app:app --host \"$HOST\" --port \"$PORT\" 2>&1 | tee -a \"$LOG\"'"

for i in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
    echo "ready  http://${HOST}:${PORT}/  (tmux: $SESSION)"
    curl -sS "http://${HOST}:${PORT}/health"; echo
    exit 0
  fi
  sleep 0.3
done

echo "failed to start"; tail -n 40 "$LOG" || true; exit 1
