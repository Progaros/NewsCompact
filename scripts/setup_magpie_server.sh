#!/usr/bin/env bash
# Set up and optionally enable a systemd user service for the standalone MagpieTTS 357M
# HTTP server (scripts/magpie_server.py) on the PC hosting llama.cpp.
#
# Usage (on the llama.cpp PC):
#   MAGPIE_DEVICE=cpu  MAGPIE_PORT=8880 bash scripts/setup_magpie_server.sh
#   MAGPIE_DEVICE=cuda MAGPIE_PORT=8880 bash scripts/setup_magpie_server.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

export NEWS_MAGPIE_DEVICE="${MAGPIE_DEVICE:-cpu}"
PORT="${MAGPIE_PORT:-8880}"
HOST="${MAGPIE_HOST:-0.0.0.0}"
THREADS="${MAGPIE_THREADS:-0}"
API_KEY="${MAGPIE_API_KEY:-}"
REPO_DIR="$(pwd -P)"

bash scripts/setup_magpie.sh

# Auto-generate a Bearer API key when binding to a non-loopback interface so /tts is never exposed unauthenticated.
if [[ "$HOST" != "127.0.0.1" && "$HOST" != "::1" && "$HOST" != "localhost" && -z "$API_KEY" ]]; then
  API_KEY="$("$REPO_DIR/.venv-magpie/bin/python" -c "import secrets; print(secrets.token_urlsafe(32))")"
  echo "Generated MAGPIE_API_KEY for non-loopback binding ($HOST)."
fi

UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/magpie-tts.service"

if command -v systemctl >/dev/null 2>&1; then
  mkdir -p "$UNIT_DIR"
  umask 077
  cat > "$UNIT_FILE" <<EOF
[Unit]
Description=NVIDIA MagpieTTS 357M HTTP Service
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
Environment=MAGPIE_HOST=$HOST
Environment=MAGPIE_PORT=$PORT
Environment=MAGPIE_DEVICE=$NEWS_MAGPIE_DEVICE
Environment=MAGPIE_THREADS=$THREADS
Environment=MAGPIE_API_KEY=$API_KEY
ExecStart=$REPO_DIR/.venv-magpie/bin/python $REPO_DIR/scripts/magpie_server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  chmod 600 "$UNIT_FILE"
  systemctl --user daemon-reload
  systemctl --user enable --now magpie-tts.service
  echo ""
  echo "magpie-tts.service installed and started on http://$HOST:$PORT"
  echo "Check status:  systemctl --user status magpie-tts.service"
  echo "Check health:  curl http://127.0.0.1:$PORT/health"
else
  echo ""
  echo "Start manually with:"
  echo "  MAGPIE_HOST=$HOST MAGPIE_PORT=$PORT MAGPIE_DEVICE=$NEWS_MAGPIE_DEVICE MAGPIE_API_KEY=$API_KEY $REPO_DIR/.venv-magpie/bin/python $REPO_DIR/scripts/magpie_server.py"
fi

echo ""
echo "On the News Aggregator host, add to .env (use WireGuard/Tailscale/TLS over untrusted networks):"
echo "  NEWS_MAGPIE_URL=http://<THIS_PC_IP>:$PORT"
if [[ -n "$API_KEY" ]]; then
  echo "  NEWS_MAGPIE_API_KEY=$API_KEY"
fi
