#!/usr/bin/env bash
# Install NVIDIA Magpie-Multilingual 357M (NeMo TTS) into an isolated Python 3.11 venv
# (.venv-magpie) so the main app stays on Python 3.14.
#
# Idempotent: safe to run on every start via scripts/start.sh (exits immediately once installed).
# Requires g++ / gcc-c++ on Linux for building pyopenjtalk.
# Set NEWS_MAGPIE_DEVICE=cuda before running to install CUDA PyTorch instead of CPU-only.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

VENV=".venv-magpie"
PY="$VENV/bin/python"
DEVICE="${NEWS_MAGPIE_DEVICE:-cpu}"
MODEL_ID="${NEWS_MAGPIE_MODEL:-nvidia/magpie_tts_multilingual_357m}"

if [[ -x "$PY" ]] && "$PY" -c "import nemo.collections.tts.models, soundfile" 2>/dev/null; then
  exit 0
fi

if ! command -v g++ >/dev/null 2>&1; then
  echo "error: g++ is required to build NeMo TTS dependencies (install gcc-c++ or g++)" >&2
  exit 1
fi

echo "magpie setup: creating Python 3.11 venv ($VENV, device=$DEVICE)…"
uv venv --python 3.11 "$VENV"
uv pip install --python "$PY" pip setuptools wheel cython

if [[ "$DEVICE" == "cuda" ]]; then
  uv pip install --python "$PY" torch torchaudio
else
  uv pip install --python "$PY" torch torchaudio --index-url https://download.pytorch.org/whl/cpu
fi

echo "magpie setup: installing nemo_toolkit[tts], soundfile, fastapi, uvicorn…"
CXXFLAGS="-include cstdint -fpermissive" CMAKE_POLICY_VERSION_MINIMUM=3.5 \
  uv pip install --python "$PY" "nemo_toolkit[tts]" soundfile fastapi uvicorn

echo "magpie setup: pre-downloading $MODEL_ID checkpoint…"
CUDA_VISIBLE_DEVICES="" "$PY" -c "
from nemo.collections.tts.models import MagpieTTSModel
MagpieTTSModel.from_pretrained('$MODEL_ID', map_location='cpu')
print('magpie setup: checkpoint cached.')
"
