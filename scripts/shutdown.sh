#!/usr/bin/env bash
# Stop the server (every instance started from this checkout): SIGTERM (graceful: workers
# stop, SSE streams close), then SIGKILL if it is still alive after the timeout.
# Usage: scripts/shutdown.sh [timeout_seconds]   (default 20)
set -euo pipefail
cd "$(dirname "$0")/.."

TIMEOUT="${1:-20}"
PORT="${NEWS_PORT:-8787}"
PID_FILE="${NEWS_PID_FILE:-news.pid}"

is_ours() {  # guards against a stale PID file pointing at a reused PID
  [[ -n "$1" ]] && kill -0 "$1" 2>/dev/null \
    && tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null \
    | grep -qE "main\.py|uvicorn.*app\.api"
}

# Every server process of this checkout (the python one, not its `uv run` parent): older
# versions could end up with several, and only one of them is in the PID file / on the port.
candidates() {
  [[ -f "$PID_FILE" ]] && echo "$(cat "$PID_FILE")"  # the file has no trailing newline
  ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 || true
  local p
  for p in $(pgrep -f 'main\.py' || true); do
    [[ "$(readlink "/proc/$p/cwd" 2>/dev/null)" == "$(pwd -P)" ]] || continue
    [[ "$(basename "$(readlink "/proc/$p/exe" 2>/dev/null)")" == python* ]] && echo "$p"
  done
  return 0
}

pids=()
for p in $(candidates | sort -un); do
  is_ours "$p" && pids+=("$p")
done
if ((${#pids[@]} == 0)); then
  echo "not running"
  rm -f "$PID_FILE"
  exit 0
fi

alive() {
  local p
  for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && echo "$p"; done
  return 0
}

echo "stopping pid ${pids[*]} (timeout ${TIMEOUT}s) ..."
kill -TERM "${pids[@]}" 2>/dev/null || true
for ((i = 0; i < TIMEOUT; i++)); do
  if [[ -z "$(alive)" ]]; then
    rm -f "$PID_FILE"
    echo "stopped"
    exit 0
  fi
  sleep 1
done

left="$(alive | tr '\n' ' ')"
echo "still running after ${TIMEOUT}s (pid ${left% }), sending SIGKILL" >&2
kill -KILL $left 2>/dev/null || true
rm -f "$PID_FILE"
echo "killed"
