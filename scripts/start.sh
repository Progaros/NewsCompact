#!/usr/bin/env bash
# Launch the news aggregator server fully detached from the caller's shell.
# Usage: scripts/start.sh   (export NEWS_AUTH_PASSWORD first if you want auth on)
#
# Detaches via `setsid -f` (own session + process group) so the server
# survives the launching shell/terminal exiting. stdio appends to server.log.
set -euo pipefail

cd "$(dirname "$0")/.."
log="server.log"

if ss -ltn 2>/dev/null | grep -q ':8787 '; then
  echo "already listening on :8787" >&2
  exit 1
fi

# `setsid -f` forces a new session even in a non-interactive shell, so the
# server outlives the tool/terminal that launched it.
setsid -f bash -c "exec uv run python main.py >> '$log' 2>&1 < /dev/null" \
  > /dev/null 2>&1 < /dev/null &
echo "started (launch pid $!)"
