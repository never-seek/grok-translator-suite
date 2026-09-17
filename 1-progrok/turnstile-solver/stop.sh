#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PORT="${TURNSTILE_PORT:-5072}"
if [[ -f logs/turnstile_solver.pid ]]; then
  pid="$(cat logs/turnstile_solver.pid || true)"
  if [[ -n "${pid:-}" ]]; then
    kill "${pid}" 2>/dev/null || true
    sleep 1
    kill -9 "${pid}" 2>/dev/null || true
  fi
  rm -f logs/turnstile_solver.pid
fi
pkill -f "api_solver.py --browser_type .* --port ${PORT}" 2>/dev/null || true
echo "[turnstile-solver] stopped"
