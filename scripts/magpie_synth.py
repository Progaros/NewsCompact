#!/usr/bin/env python3
"""Persistent NVIDIA Magpie-Multilingual 357M TTS worker invoked by app/tts.py.

Runs inside the isolated ``.venv-magpie`` (Python 3.11 + ``nemo_toolkit[tts]``) so the
main app can stay on Python 3.14 and the model + NeMo FST text-normalization grammars
stay loaded in memory as a singleton across episodes.

Protocol (JSON lines, stdin -> stdout):
- On startup (once model is loaded):
  {"event": "ready", "sample_rate": 22050, "device": "cpu"}
- Request line on stdin:
  {
    "job_id": "abc123",
    "lang": "de",
    "speaker1": 3,
    "speaker2": 4,
    "voice1": "Leo",
    "voice2": "Sofia",
    "out_dir": "/abs/tmp/dir",
    "chapters": [{"title": "...", "chunks": ["para 1", {"text": "...", "host": 2}]}]
  }
- Response lines on stdout:
  {"event": "chapter", "job_id": "abc123", "index": 0, "voice": "Leo", "files": [".../ch0_0.wav"]}
  {"event": "done", "job_id": "abc123", "voice": "Leo"}
  (or {"event": "error", "job_id": "abc123", "error": "..."})
"""
from __future__ import annotations

import json
import os
import re
import sys
import wave
from pathlib import Path

MODEL_ID = os.environ.get("NEWS_MAGPIE_MODEL", "nvidia/magpie_tts_multilingual_357m")
DEVICE = (os.environ.get("NEWS_MAGPIE_DEVICE", "cpu") or "cpu").strip().lower()
THREADS = int(os.environ.get("NEWS_MAGPIE_THREADS", "0") or "0")

# Force CPU before importing torch so VRAM stays free for local LLMs unless CUDA is requested.
if DEVICE == "cpu":
    os.environ["CUDA_VISIBLE_DEVICES"] = ""

# Supported language codes for MagpieTTS Multilingual v2607.
SUPPORTED_LANGS = frozenset({"ar", "de", "en", "es", "fr", "hi", "it", "ja", "ko", "pt", "vi", "zh"})
# Keep single do_tts() calls well below the ~20s autoregressive standard-mode limit.
MAX_SENTENCE_CHARS = 220
SENTENCE_PAUSE_SEC = 0.25


def _split_sentences(text: str, max_chars: int = MAX_SENTENCE_CHARS) -> list[str]:
    """Split a paragraph into sentence-sized pieces suitable for autoregressive MagpieTTS."""
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return []
    raw_sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    out: list[str] = []
    for sent in raw_sents:
        if len(sent) <= max_chars:
            out.append(sent)
            continue
        # Sub-split very long sentences on clauses/commas, then words.
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
                words = cur.split()
                buf = ""
                for w in words:
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


def _write_wav(path: Path, samples, sample_rate: int) -> None:
    import numpy as np

    arr = np.asarray(samples, dtype=np.float32)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    import numpy as np
    import torch
    from nemo.collections.tts.models import MagpieTTSModel  # type: ignore[import-not-found]

    if DEVICE == "cpu" and THREADS > 0:
        torch.set_num_threads(THREADS)

    model = MagpieTTSModel.from_pretrained(MODEL_ID, map_location=DEVICE).eval()
    sample_rate = int(getattr(model, "sample_rate", 22050))
    gap = np.zeros(int(sample_rate * SENTENCE_PAUSE_SEC), dtype=np.float32)

    _emit({"event": "ready", "sample_rate": sample_rate, "device": DEVICE, "model": MODEL_ID})

    def _synth_text(text: str, lang: str, speaker_idx: int) -> np.ndarray:
        pieces = _split_sentences(text)
        wavs: list[np.ndarray] = []
        with torch.inference_mode():
            for piece in pieces:
                audio = model.do_tts(
                    piece,
                    language=lang,
                    speaker_index=speaker_idx,
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
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(wavs)

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        job = json.loads(line)
        if job.get("cmd") == "quit":
            break
        job_id = str(job.get("job_id") or "")
        try:
            lang = _normalize_lang(str(job.get("lang") or "de"))
            sp1 = int(job.get("speaker1", 3))
            sp2 = int(job.get("speaker2", 4))
            voice1 = str(job.get("voice1") or f"speaker_{sp1}")
            voice2 = str(job.get("voice2") or f"speaker_{sp2}")
            out_dir = Path(job["out_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            used_host2 = False

            for ci, ch in enumerate(job.get("chapters") or []):
                files: list[str] = []
                for pi, item in enumerate(ch.get("chunks") or []):
                    if isinstance(item, dict):
                        text = str(item.get("text") or "").strip()
                        host = int(item.get("host") or 1)
                        sp = int(item["speaker_index"]) if item.get("speaker_index") is not None else (sp2 if host == 2 else sp1)
                        if host == 2:
                            used_host2 = True
                    else:
                        text = str(item or "").strip()
                        sp = sp1
                    if not text:
                        continue
                    wav = _synth_text(text, lang, sp)
                    if not wav.size:
                        continue
                    out_wav = out_dir / f"ch{ci}_{pi}.wav"
                    _write_wav(out_wav, wav, sample_rate)
                    files.append(str(out_wav))
                used = f"{voice1}+{voice2}" if used_host2 else voice1
                _emit({"event": "chapter", "job_id": job_id, "index": ci, "voice": used, "files": files})

            used = f"{voice1}+{voice2}" if used_host2 else voice1
            _emit({"event": "done", "job_id": job_id, "voice": used})
        except Exception as e:  # noqa: BLE001
            _emit({"event": "error", "job_id": job_id, "error": str(e)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
