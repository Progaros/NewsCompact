#!/usr/bin/env sh
# Launch the server detached, wait for it to come up, then report.
cd "$(dirname "$0")/.."
setsid nohup uv run python main.py > server.log 2>&1 < /dev/null &
sleep 14
echo "=== port ==="
ss -ltn 2>/dev/null | grep 8787 || echo "nothing on 8787"
echo "=== log tail ==="
tail -n 6 server.log
