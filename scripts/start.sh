#!/usr/bin/env bash
# Start the server detached from the terminal.
# Logs -> server.log (rotated + gzipped by the app), early crash output -> server.out,
# PID -> news.pid. Environment (NEWS_AUTH_PASSWORD, NEWS_PORT, ...) is passed through;
# a .env file next to main.py is sourced first if present.
# Usage: scripts/start.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

PORT="${NEWS_PORT:-8787}"
PID_FILE="${NEWS_PID_FILE:-news.pid}"
export NEWS_LOG_FILE="${NEWS_LOG_FILE:-server.log}"
LOG="$NEWS_LOG_FILE"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "already running (pid $(cat "$PID_FILE"))" >&2
  exit 1
fi
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "port $PORT is already in use" >&2
  exit 1
fi

setsid -f bash -c "exec uv run python main.py > server.out 2>&1 < /dev/null"

for _ in $(seq 1 60); do
  if curl -fsS --noproxy '*' -o /dev/null "http://127.0.0.1:$PORT/api/health" 2>/dev/null; then
    echo "started (pid $(cat "$PID_FILE" 2>/dev/null || echo '?'), port $PORT, log $LOG)"
    exit 0
  fi
  sleep 1
done
echo "server not healthy after 60s; last output:" >&2
tail -n 20 server.out "$LOG" >&2 2>/dev/null || true
exit 1
