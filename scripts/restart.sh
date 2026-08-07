#!/usr/bin/env bash
# Restart Switch-codex only (exact tmux session name).
# Optional: --tunnel also ensures cloudflared session is up (does not kill other sessions).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WITH_TUNNEL=0
for arg in "$@"; do
  case "$arg" in
    --tunnel|-t) WITH_TUNNEL=1 ;;
    -h|--help)
      echo "Usage: $0 [--tunnel]"
      echo "  Restarts only session switchyard (and optionally ensures cloudflared)."
      echo "  Never runs tmux kill-server / never touches other sessions."
      exit 0
      ;;
  esac
done

export SR_FORCE=1
echo "==> restarting switch-codex only (force code reload)"
"$ROOT/scripts/stop.sh"
"$ROOT/scripts/start.sh"

if [[ "$WITH_TUNNEL" == "1" ]]; then
  echo "==> ensuring cloudflared (start if missing; will not kill other tmux)"
  "$ROOT/scripts/tunnel.sh" start
fi

echo "==> local health"
curl -fsS --max-time 3 "http://${SW_HOST:-127.0.0.1}:${SW_PORT:-${SR_PORT:-4100}}/health"; echo
echo "==> remaining tmux sessions:"
tmux ls 2>/dev/null || echo "(none)"
