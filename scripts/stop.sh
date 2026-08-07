#!/usr/bin/env bash
# Stop ONLY the Switch-codex uvicorn process / its dedicated tmux session.
# NEVER runs: tmux kill-server, kill-session without exact name, pkill -f broadly.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Exact session name only — never a pattern, never kill-server.
SESSION="${SR_TMUX_SESSION:-switchyard}"
PORT="${SW_PORT:-${SR_PORT:-4100}}"

if [[ "$SESSION" == "*" || -z "$SESSION" ]]; then
  echo "refusing to operate on empty/wildcard tmux session name" >&2
  exit 1
fi

# Return 0 if the given PID is listening on our TCP port (ss first, lsof fallback).
_pid_on_port() {
  local pid="$1"
  if command -v ss >/dev/null 2>&1 \
     && ss -ltnp 2>/dev/null | grep -q "pid=${pid},.*:${PORT}"; then
    return 0
  fi
  if command -v lsof >/dev/null 2>&1 \
     && lsof -iTCP:"${PORT}" -sTCP:LISTEN -P -n 2>/dev/null \
        | awk -v p="$pid" 'NR>1 && $2==p { found=1 } END { exit found ? 0 : 1 }'; then
    return 0
  fi
  return 1
}

# 1) Kill only the exact named session.
if tmux has-session -t "=${SESSION}" 2>/dev/null; then
  tmux kill-session -t "=${SESSION}" || true
fi

# 2) Kill uvicorn bound to our port whose cwd is this project (PID scan, no pkill -f).
while read -r pid; do
  [[ -z "${pid:-}" || ! -d "/proc/$pid" ]] && continue
  cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  case "$cmd" in
    *uvicorn*) ;;
    *) continue ;;
  esac
  case "$cmd" in
    *app:app*) ;;
    *) continue ;;
  esac
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  [[ "$cwd" == "$ROOT" ]] || continue
  # Must be our port if specified in cmdline, or listening on PORT.
  if [[ "$cmd" == *"--port ${PORT}"* || "$cmd" == *"--port=${PORT}"* ]] \
     || _pid_on_port "$pid"; then
    kill "$pid" 2>/dev/null || true
  fi
done < <(pgrep -x uvicorn 2>/dev/null || true; pgrep -f '[u]vicorn app:app' 2>/dev/null || true)

# 3) Wait for port release.
for _ in $(seq 1 30); do
  if ! ss -ltn 2>/dev/null | grep -qE ":${PORT}\\s"; then
    break
  fi
  sleep 0.1
done

# 4) Free only this TCP port (not other services).
if command -v fuser >/dev/null 2>&1; then
  fuser -k "${PORT}/tcp" 2>/dev/null || true
elif command -v lsof >/dev/null 2>&1; then
  lsof -ti "TCP:${PORT}" -sTCP:LISTEN 2>/dev/null | xargs -r kill 2>/dev/null || true
fi

echo "stopped session=${SESSION} port=${PORT} (no other tmux sessions touched)"
