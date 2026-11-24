#!/usr/bin/env python3
"""Standalone NVIDIA Magpie-Multilingual 357M HTTP TTS microservice.

Run this on the PC that hosts llama.cpp (CPU or CUDA GPU), then point the News Aggregator
server at it via ``NEWS_MAGPIE_URL=http://<pc-ip>:8880`` (and optional ``NEWS_MAGPIE_API_KEY``).

Endpoints:
- ``GET /health`` -> model/device/voices info
- ``POST /tts``   -> JSON ``{"text": str, "language": "de", "speaker_index": 3}`` -> 16-bit PCM WAV

Environment variables:
- ``MAGPIE_HOST``    (default ``0.0.0.0``)
- ``MAGPIE_PORT``    (default ``8880``)
- ``MAGPIE_DEVICE``  (default ``cpu``; set ``cuda`` to use NVIDIA GPU)
- ``MAGPIE_THREADS`` (default ``0`` = PyTorch default; set physical core count on CPU)
- ``MAGPIE_MODEL``   (default ``nvidia/magpie_tts_multilingual_357m``)
- ``MAGPIE_API_KEY`` (optional Bearer token required on ``POST /tts``)
"""
from __future__ import annotations

import io
import os
import re
import secrets
import threading
import wave
from contextlib import asynccontextmanager

MODEL_ID = os.environ.get("MAGPIE_MODEL", os.environ.get("NEWS_MAGPIE_MODEL", "nvidia/magpie_tts_multilingual_357m"))
DEVICE = (os.environ.get("MAGPIE_DEVICE", os.environ.get("NEWS_MAGPIE_DEVICE", "cpu")) or "cpu").strip().lower()
THREADS = int(os.environ.get("MAGPIE_THREADS", os.environ.get("NEWS_MAGPIE_THREADS", "0")) or "0")
HOST = os.environ.get("MAGPIE_HOST", "127.0.0.1")
PORT = int(os.environ.get("MAGPIE_PORT", "8880"))
API_KEY = os.environ.get("MAGPIE_API_KEY", "").strip()
ALLOW_NO_AUTH = os.environ.get("MAGPIE_ALLOW_NO_AUTH", "0") == "1"

if HOST not in ("127.0.0.1", "::1", "localhost") and not API_KEY and not ALLOW_NO_AUTH:
    raise SystemExit(
        f"Refusing to bind MagpieTTS to non-loopback address {HOST!r} without MAGPIE_API_KEY. "
        "Set MAGPIE_API_KEY (recommended) or MAGPIE_ALLOW_NO_AUTH=1 to override."
    )

if DEVICE == "cpu":
    os.environ["CUDA_VISIBLE_DEVICES"] = ""

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

SPEAKERS: dict[str, int] = {"Aria": 0, "Jason": 1, "John": 2, "Leo": 3, "Sofia": 4}
_SPEAKERS_LOW = {k.lower(): v for k, v in SPEAKERS.items()}
SUPPORTED_LANGS = frozenset({"ar", "de", "en", "es", "fr", "hi", "it", "ja", "ko", "pt", "vi", "zh"})
MAX_SENTENCE_CHARS = 220
SENTENCE_PAUSE_SEC = 0.25

_model = None
_sample_rate = 22050
_infer_lock = threading.Lock()


def _split_sentences(text: str, max_chars: int = MAX_SENTENCE_CHARS) -> list[str]:
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return []
    raw_sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    out: list[str] = []
    for sent in raw_sents:
        if len(sent) <= max_chars:
            out.append(sent)
            continue
        parts = re.split(r"(?<=[,;:])\s+", sent)
        cur = ""
        for p in parts:
            if cur and len(cur) + 1 + len(p) > max_chars:
                out.append(cur)
                cur = p
            else:
                cur = f"{cur} {p}".strip() if cur else p
        if cur:
            if len(cur) <= max_chars:
                out.append(cur)
            else:
                buf = ""
                for w in cur.split():
                    if buf and len(buf) + 1 + len(w) > max_chars:
                        out.append(buf)
                        buf = w
                    else:
                        buf = f"{buf} {w}".strip() if buf else w
                if buf:
                    out.append(buf)
    return out


def _normalize_lang(lang: str) -> str:
    c = (lang or "de").strip().lower().split("-")[0].split("_")[0]
    return c if c in SUPPORTED_LANGS else "de"


def _resolve_speaker(speaker_index: int | None, voice: str | None) -> int:
    if speaker_index is not None and 0 <= speaker_index <= 4:
        return int(speaker_index)
    if voice:
        v = voice.strip().lower()
        if v.isdigit() and 0 <= int(v) <= 4:
            return int(v)
        if v in _SPEAKERS_LOW:
            return _SPEAKERS_LOW[v]
    return 3  # Leo


def _wav_bytes(samples, sample_rate: int) -> bytes:
    import numpy as np

    arr = np.asarray(samples, dtype=np.float32)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _model, _sample_rate
    import torch
    from nemo.collections.tts.models import MagpieTTSModel  # type: ignore[import-not-found]

    if DEVICE == "cpu" and THREADS > 0:
        torch.set_num_threads(THREADS)
    print(f"Loading {MODEL_ID} on {DEVICE}…", flush=True)
    _model = MagpieTTSModel.from_pretrained(MODEL_ID, map_location=DEVICE).eval()
    _sample_rate = int(getattr(_model, "sample_rate", 22050))
    print(f"MagpieTTS ready on http://{HOST}:{PORT} (sr={_sample_rate}, device={DEVICE})", flush=True)
    yield


app = FastAPI(title="MagpieTTS 357M Service", lifespan=lifespan)


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    language: str = "de"
    speaker_index: int | None = Field(None, ge=0, le=4)
    voice: str | None = None


@app.get("/health")
def health():
    return {
        "ok": _model is not None,
        "model": MODEL_ID,
        "device": DEVICE,
        "sample_rate": _sample_rate,
        "voices": list(SPEAKERS.keys()),
    }


@app.post("/tts")
def synthesize(req: TTSRequest, authorization: str | None = Header(default=None)):
    if API_KEY:
        expected = f"Bearer {API_KEY}"
        if not authorization or not secrets.compare_digest(authorization.strip(), expected):
            raise HTTPException(401, "unauthorized")
    if _model is None:
        raise HTTPException(503, "model not loaded yet")

    import numpy as np
    import torch

    lang = _normalize_lang(req.language)
    sp = _resolve_speaker(req.speaker_index, req.voice)
    pieces = _split_sentences(req.text)
    if not pieces:
        raise HTTPException(422, "empty text")

    gap = np.zeros(int(_sample_rate * SENTENCE_PAUSE_SEC), dtype=np.float32)
    wavs: list[np.ndarray] = []
    with _infer_lock, torch.inference_mode():
        for piece in pieces:
            audio = _model.do_tts(
                piece,
                language=lang,
                speaker_index=sp,
                apply_TN=True,
            )
            if isinstance(audio, tuple):
                audio = audio[0]
            wav = audio.squeeze().detach().cpu().numpy().astype(np.float32)
            if wav.size:
                if wavs:
                    wavs.append(gap)
                wavs.append(wav)

    if not wavs:
        raise HTTPException(500, "model produced no audio")
    return Response(content=_wav_bytes(np.concatenate(wavs), _sample_rate), media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
