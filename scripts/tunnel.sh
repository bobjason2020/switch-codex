#!/usr/bin/env bash
# Manage ONLY the cloudflared tmux session for Switch-codex's tunnel.
# Never touches other tmux sessions or kill-server.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SESSION="${CF_TMUX_SESSION:-cloudflared}"
LOG="$ROOT/logs/cloudflared.tmux.log"
CONFIG="${CF_CONFIG:-$HOME/.cloudflared/config.yml}"
TUNNEL_ID="${CF_TUNNEL_ID:-}"
mkdir -p "$ROOT/logs"

# 从 cloudflared 配置读取 tunnel id（配置在仓库外，避免在仓库中硬编码标识）
if [[ -z "$TUNNEL_ID" && -f "$CONFIG" ]]; then
  TUNNEL_ID="$(sed -n 's/^[[:space:]]*tunnel:[[:space:]]*//p' "$CONFIG" | head -n 1 | tr -d '[:space:]')"
fi

if [[ "$SESSION" == "*" || -z "$SESSION" ]]; then
  echo "refusing empty/wildcard session name" >&2
  exit 1
fi
if [[ -z "$TUNNEL_ID" ]]; then
  echo "cannot determine tunnel id: set CF_TUNNEL_ID or provide config.yml" >&2
  exit 1
fi

# PIDs of cloudflared processes running OUR tunnel (config + tunnel id),
# so other cloudflared instances on this host are never touched.
_our_cloudflared_pids() {
  pgrep -x cloudflared 2>/dev/null | while read -r pid; do
    [[ -n "$pid" && -d "/proc/$pid" ]] || continue
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$cmd" in
      *"$TUNNEL_ID"*|*"$CONFIG"*) echo "$pid" ;;
    esac
  done
}

_cloudflared_running() {
  [[ -n "$(_our_cloudflared_pids)" ]]
}

cmd_status() {
  if _cloudflared_running; then
    echo "cloudflared: running"
    for pid in $(_our_cloudflared_pids); do
      ps -o pid,cmd -p "$pid" 2>/dev/null | tail -n 1 || true
    done
  else
    echo "cloudflared: stopped"
  fi
  if tmux has-session -t "=${SESSION}" 2>/dev/null; then
    echo "tmux session: ${SESSION} (alive)"
  else
    echo "tmux session: ${SESSION} (none)"
  fi
  tail -n 5 "$LOG" 2>/dev/null || true
}

cmd_stop() {
  if tmux has-session -t "=${SESSION}" 2>/dev/null; then
    tmux kill-session -t "=${SESSION}" || true
  fi
  # Kill only cloudflared processes running our tunnel (not every cloudflared).
  _our_cloudflared_pids | xargs -r kill 2>/dev/null || true
  sleep 0.4
  _our_cloudflared_pids | xargs -r kill -9 2>/dev/null || true
  echo "cloudflared stopped (only session=${SESSION})"
}

cmd_start() {
  if _cloudflared_running; then
    echo "cloudflared already running"
    cmd_status
    return 0
  fi
  if [[ ! -f "$CONFIG" ]]; then
    echo "missing config: $CONFIG" >&2
    exit 1
  fi
  if ! command -v cloudflared >/dev/null 2>&1; then
    echo "cloudflared not installed" >&2
    exit 1
  fi
  if tmux has-session -t "=${SESSION}" 2>/dev/null; then
    tmux kill-session -t "=${SESSION}" || true
  fi
  tmux new-session -d -s "$SESSION" \
    "bash -lc 'exec cloudflared tunnel --config \"$CONFIG\" run $TUNNEL_ID 2>&1 | tee -a \"$LOG\"'"

  for i in $(seq 1 30); do
    if _cloudflared_running; then
      if tail -n 40 "$LOG" 2>/dev/null | grep -q 'Registered tunnel connection'; then
        echo "cloudflared ready (tmux: $SESSION)"
        cmd_status
        return 0
      fi
    fi
    sleep 0.4
  done
  echo "cloudflared started but registration not confirmed yet; check $LOG"
  cmd_status
}

cmd_restart() {
  cmd_stop
  sleep 0.5
  cmd_start
}

case "${1:-status}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
