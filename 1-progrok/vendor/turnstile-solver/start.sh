#!/usr/bin/env bash
# Start local Turnstile Solver (host process; reachable by grokcli-2api container via docker bridge gateway).
set -euo pipefail
cd "$(dirname "$0")"

HOST="${TURNSTILE_HOST:-0.0.0.0}"
PORT="${TURNSTILE_PORT:-5072}"
THREAD="${TURNSTILE_THREAD:-2}"
BROWSER_TYPE="${TURNSTILE_BROWSER_TYPE:-camoufox}"
LOG_FILE="${TURNSTILE_LOG:-logs/turnstile_solver.log}"

mkdir -p logs keys

if [[ ! -x .venv/bin/python ]]; then
  echo "[turnstile-solver] creating venv..."
  python3 -m venv .venv
  .venv/bin/pip install -U pip setuptools wheel
  .venv/bin/pip install -r requirements.txt
  # browser binaries (first run)
  .venv/bin/python -m camoufox fetch || true
  .venv/bin/python -m patchright install chromium || true
fi

# stop previous instance on same port
if command -v ss >/dev/null 2>&1; then
  old_pid="$(ss -lntp 2>/dev/null | awk -v p=":${PORT}" '$4 ~ p {print}' | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1 || true)"
  if [[ -n "${old_pid:-}" ]]; then
    echo "[turnstile-solver] stopping old pid ${old_pid} on :${PORT}"
    kill "${old_pid}" 2>/dev/null || true
    sleep 1
    kill -9 "${old_pid}" 2>/dev/null || true
  fi
fi
pkill -f "api_solver.py --browser_type .* --port ${PORT}" 2>/dev/null || true
sleep 1

echo "[turnstile-solver] starting ${BROWSER_TYPE} thread=${THREAD} ${HOST}:${PORT}"
nohup .venv/bin/python api_solver.py \
  --browser_type "${BROWSER_TYPE}" \
  --thread "${THREAD}" \
  --debug \
  --host "${HOST}" \
  --port "${PORT}" \
  >"${LOG_FILE}" 2>&1 &
echo $! > logs/turnstile_solver.pid
echo "[turnstile-solver] pid=$(cat logs/turnstile_solver.pid) log=${LOG_FILE}"

# wait ready
for i in $(seq 1 40); do
  if curl -fsS -m 1 "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
    echo "[turnstile-solver] ready http://127.0.0.1:${PORT}"
    exit 0
  fi
  sleep 1
done
echo "[turnstile-solver] WARN: not ready yet; check ${LOG_FILE}" >&2
exit 1
