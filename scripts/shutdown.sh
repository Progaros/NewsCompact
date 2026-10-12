#!/usr/bin/env bash
# Stop the server: SIGTERM (graceful: workers stop, SSE streams close), then
# SIGKILL if it is still alive after the timeout.
# Usage: scripts/shutdown.sh [timeout_seconds]   (default 20)
set -euo pipefail
cd "$(dirname "$0")/.."

TIMEOUT="${1:-20}"
PORT="${NEWS_PORT:-8787}"
PID_FILE="${NEWS_PID_FILE:-news.pid}"

is_ours() {  # guards against a stale PID file pointing at a reused PID
  [[ -n "$1" ]] && kill -0 "$1" 2>/dev/null \
    && tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | grep -q "main.py"
}

pid=""
[[ -f "$PID_FILE" ]] && pid="$(cat "$PID_FILE")"
if ! is_ours "$pid"; then
  pid="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -n1 || true)"
fi
if ! is_ours "$pid"; then
  echo "not running"
  rm -f "$PID_FILE"
  exit 0
fi

echo "stopping pid $pid (timeout ${TIMEOUT}s) ..."
kill -TERM "$pid"
for ((i = 0; i < TIMEOUT; i++)); do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "stopped"
    exit 0
  fi
  sleep 1
done

echo "still running after ${TIMEOUT}s, sending SIGKILL" >&2
kill -KILL "$pid" 2>/dev/null || true
rm -f "$PID_FILE"
echo "killed"
