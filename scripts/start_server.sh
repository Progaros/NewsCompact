#!/usr/bin/env sh
# Detached server launch: uvicorn (main.py) -> server.log. Idempotent-ish;
# run after killing any existing listener on the port.
cd "$(dirname "$0")/.."
setsid uv run python main.py > server.log 2>&1 < /dev/null &
echo "started server (pid $!)"
