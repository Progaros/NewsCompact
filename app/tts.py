"""NVIDIA Magpie-Multilingual 357M TTS + WAV stitching for podcasts.

Uses ``nvidia/magpie_tts_multilingual_357m`` (NeMo TTS) running in the isolated
``.venv-magpie`` (Python 3.11) via a persistent singleton worker process
(``scripts/magpie_synth.py``) so the model and NeMo text-normalization grammars
load only once and stay warm in memory across episodes.

Built-in multilingual speakers (all speak German, English, and 10 other languages):
  0 = Aria (female), 1 = Jason (male), 2 = John (male), 3 = Leo (male), 4 = Sofia (female).
"""
from __future__ import annotations

import atexit
import io
import json
import logging
import math
import os
import secrets
import shutil
import struct
import subprocess
import tempfile
import threading
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from . import config

log = logging.getLogger("news.tts")

_SYNTH_SCRIPT = config.BASE_DIR / "scripts" / "magpie_synth.py"

# Official speaker_map from nvidia/magpie_tts_multilingual_357m (v2607).
SPEAKERS: dict[str, int] = {
    "Aria": 0,
    "Jason": 1,
    "John": 2,
    "Leo": 3,
    "Sofia": 4,
}
_SPEAKER_BY_IDX: dict[int, str] = {idx: name for name, idx in SPEAKERS.items()}
_SPEAKER_BY_LOW: dict[str, tuple[str, int]] = {name.lower(): (name, idx) for name, idx in SPEAKERS.items()}


def resolve_speaker(spec: str | int | None, default_name: str = "Leo") -> tuple[str, int]:
    """Resolve a speaker name or index (0..4) to ``(canonical_name, speaker_index)``."""
    if isinstance(spec, int) and spec in _SPEAKER_BY_IDX:
        return _SPEAKER_BY_IDX[spec], spec
    raw = str(spec or "").strip()
    if raw.isdigit():
        idx = int(raw)
        if idx in _SPEAKER_BY_IDX:
            return _SPEAKER_BY_IDX[idx], idx
    if raw.lower() in _SPEAKER_BY_LOW:
        return _SPEAKER_BY_LOW[raw.lower()]
    return default_name, SPEAKERS.get(default_name, 3)


def _magpie_python() -> Path | None:
    if config.MAGPIE_PYTHON:
        p = Path(config.MAGPIE_PYTHON).expanduser()
        return p if p.is_file() else None
    for rel in (".venv-magpie/bin/python", ".venv-magpie/Scripts/python.exe"):
        p = config.BASE_DIR / rel
        if p.is_file():
            return p
    return None


def host1_speaker() -> tuple[str, int]:
    return resolve_speaker(config.MAGPIE_VOICE_HOST1, "Leo")


def host2_speaker() -> tuple[str, int]:
    return resolve_speaker(config.MAGPIE_VOICE_HOST2, "Sofia")


def resolve_backend(lang: str = "") -> tuple[str | None, str, str]:
    """Return ``(backend, voice_label, detail)`` for MagpieTTS 357M (remote HTTP or local worker)."""
    v1, _ = host1_speaker()
    v2, _ = host2_speaker()
    base = (config.MAGPIE_URL or "").strip().rstrip("/")
    if base:
        return "magpie_remote", v1, f"MagpieTTS 357M ({base}, {v1}+{v2})"
    py = _magpie_python()
    if not py:
        return None, "", "Set NEWS_MAGPIE_URL (remote MagpieTTS server) or run scripts/setup_magpie.sh locally"
    dev = (config.MAGPIE_DEVICE or "cpu").upper()
    return "magpie", v1, f"MagpieTTS 357M ({dev}, {v1}+{v2})"


def status() -> dict[str, Any]:
    backend, voice, detail = resolve_backend()
    return {
        "configured": backend is not None,
        "backend": backend,
        "model": config.MAGPIE_MODEL,
        "voice": voice,
        "voices": list(SPEAKERS.keys()),
        "device": config.MAGPIE_DEVICE,
        "detail": detail,
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }


# --- Persistent singleton MagpieTTS worker -----------------------------------
_worker_lock = threading.Lock()
_worker_proc: subprocess.Popen[str] | None = None
_worker_key: tuple[str, str, str, str] | None = None


def _kill_worker_unlocked() -> None:
    global _worker_proc, _worker_key
    proc = _worker_proc
    _worker_proc = None
    _worker_key = None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                    proc.stdin.flush()
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
    except OSError:
        pass


def shutdown() -> None:
    """Terminate the persistent MagpieTTS worker if running."""
    with _worker_lock:
        _kill_worker_unlocked()


atexit.register(shutdown)


def _ensure_worker_unlocked(py: Path) -> subprocess.Popen[str]:
    """Start (or reuse) the persistent MagpieTTS worker process."""
    global _worker_proc, _worker_key
    key = (str(py), str(_SYNTH_SCRIPT), config.MAGPIE_MODEL, config.MAGPIE_DEVICE)
    if _worker_proc is not None and _worker_proc.poll() is None and _worker_key == key:
        return _worker_proc

    _kill_worker_unlocked()
    env = os.environ.copy()
    env["NEWS_MAGPIE_MODEL"] = config.MAGPIE_MODEL
    env["NEWS_MAGPIE_DEVICE"] = config.MAGPIE_DEVICE
    if config.MAGPIE_THREADS > 0:
        env["NEWS_MAGPIE_THREADS"] = str(config.MAGPIE_THREADS)
    if config.MAGPIE_DEVICE == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = ""

    log.info("starting persistent MagpieTTS worker (%s, device=%s)", config.MAGPIE_MODEL, config.MAGPIE_DEVICE)
    proc = subprocess.Popen(
        [str(py), str(_SYNTH_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=env,
    )
    assert proc.stdout is not None and proc.stderr is not None
    # Wait for the worker's {"event": "ready"} handshake (model + FST grammar load).
    ready_line = proc.stdout.readline()
    if not ready_line:
        err = proc.stderr.read().strip()
        proc.wait(timeout=5)
        raise RuntimeError(f"MagpieTTS worker failed to start: {err[:400]}")
    try:
        ev = json.loads(ready_line)
    except json.JSONDecodeError as e:
        err = proc.stderr.read().strip()
        proc.terminate()
        raise RuntimeError(f"MagpieTTS worker bad handshake ({ready_line[:120]!r}): {err[:300]}") from e
    if ev.get("event") != "ready":
        proc.terminate()
        raise RuntimeError(f"MagpieTTS worker unexpected handshake: {ev}")

    _worker_proc = proc
    _worker_key = key
    return proc


def synthesize_wav(text: str, *, lang: str = "de") -> tuple[bytes, str]:
    """Synthesize a single text chunk to 16-bit PCM WAV bytes."""
    chapters, voice = synthesize_chapters(
        [{"title": "chunk", "story_ids": [], "chunks": [text]}],
        lang=lang,
    )
    wavs = chapters[0]["wavs"] if chapters else []
    if not wavs:
        raise RuntimeError("TTS produced no audio")
    return wavs[0], voice


def _synth_remote(
    chapters: list[dict[str, Any]],
    *,
    lang: str,
    on_progress: Callable[[int, int, str], None] | None,
    stop_event: threading.Event | None,
) -> tuple[list[dict[str, Any]], str]:
    base = config.MAGPIE_URL.rstrip("/")
    headers = {"Authorization": f"Bearer {config.MAGPIE_API_KEY}"} if config.MAGPIE_API_KEY else {}
    v1_name, sp1 = host1_speaker()
    v2_name, sp2 = host2_speaker()
    total = len(chapters)
    used_host2 = False
    out: list[dict[str, Any]] = []

    with httpx.Client(base_url=base, headers=headers, timeout=config.TTS_TIMEOUT, trust_env=False) as client:
        for ci, ch in enumerate(chapters):
            if stop_event and stop_event.is_set():
                raise InterruptedError("cancelled")
            wavs: list[bytes] = []
            for item in ch.get("chunks") or []:
                if stop_event and stop_event.is_set():
                    raise InterruptedError("cancelled")
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
                r = client.post("/tts", json={"text": text, "language": lang, "speaker_index": sp})
                r.raise_for_status()
                wavs.append(r.content)
            out.append({
                "title": ch["title"],
                "story_ids": list(ch.get("story_ids") or []),
                "stories": list(ch.get("stories") or []),
                "wavs": wavs,
            })
            if on_progress:
                on_progress(ci + 1, total, ch["title"])

    voice_used = f"{v1_name}+{v2_name}" if used_host2 else v1_name
    return out, voice_used


def synthesize_chapters(
    chapters: list[dict[str, Any]],
    *,
    lang: str = "de",
    on_progress: Callable[[int, int, str], None] | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Synthesize all chapter chunks via remote MagpieTTS HTTP server or local persistent worker.

    Each input chapter has ``{"title": str, "story_ids": list[int], "chunks": list[str | dict]}``.
    Returns ``(chapters_with_wavs, voice_label)``.
    """
    backend, _voice, detail = resolve_backend(lang)
    if backend is None:
        raise RuntimeError(f"TTS is not configured: {detail}")
    if backend == "magpie_remote":
        return _synth_remote(chapters, lang=lang, on_progress=on_progress, stop_event=stop_event)
    py = _magpie_python()
    assert py is not None

    total = len(chapters)
    v1_name, sp1 = host1_speaker()
    v2_name, sp2 = host2_speaker()
    job_id = secrets.token_hex(6)

    with _worker_lock:
        if stop_event and stop_event.is_set():
            raise InterruptedError("cancelled")
        proc = _ensure_worker_unlocked(py)
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        with tempfile.TemporaryDirectory(prefix="magpie-") as tmp_dir:
            job: dict[str, Any] = {
                "job_id": job_id,
                "lang": lang,
                "speaker1": sp1,
                "speaker2": sp2,
                "voice1": v1_name,
                "voice2": v2_name,
                "out_dir": tmp_dir,
                "chapters": [{"title": c["title"], "chunks": list(c.get("chunks") or [])} for c in chapters],
            }

            done_watch = threading.Event()
            if stop_event is not None:
                def _watch() -> None:
                    while not done_watch.is_set():
                        if stop_event.is_set():
                            try:
                                proc.terminate()
                            except OSError:
                                pass
                            return
                        done_watch.wait(0.25)
                threading.Thread(target=_watch, name="magpie-cancel", daemon=True).start()

            try:
                proc.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
                proc.stdin.flush()

                voice_used = v1_name
                results: dict[int, list[bytes]] = {}
                while True:
                    if stop_event and stop_event.is_set():
                        _kill_worker_unlocked()
                        raise InterruptedError("cancelled")
                    line = proc.stdout.readline()
                    if not line:
                        if stop_event and stop_event.is_set():
                            _kill_worker_unlocked()
                            raise InterruptedError("cancelled")
                        err = proc.stderr.read().strip()
                        _kill_worker_unlocked()
                        raise RuntimeError(f"MagpieTTS worker exited unexpectedly: {err[:400]}")
                    line = line.strip()
                    if not line:
                        continue
                    ev = json.loads(line)
                    if ev.get("job_id") and ev["job_id"] != job_id:
                        continue
                    kind = ev.get("event")
                    if kind == "chapter":
                        ci = int(ev["index"])
                        voice_used = str(ev.get("voice") or voice_used)
                        results[ci] = [Path(f).read_bytes() for f in ev.get("files") or []]
                        if on_progress and 0 <= ci < total:
                            on_progress(ci + 1, total, chapters[ci]["title"])
                    elif kind == "done":
                        voice_used = str(ev.get("voice") or voice_used)
                        break
                    elif kind == "error":
                        raise RuntimeError(f"MagpieTTS synthesis error: {ev.get('error')}")
            except InterruptedError:
                _kill_worker_unlocked()
                raise
            except Exception:
                if proc.poll() is not None:
                    _kill_worker_unlocked()
                raise
            finally:
                done_watch.set()

            out = [
                {
                    "title": ch["title"],
                    "story_ids": list(ch.get("story_ids") or []),
                    "stories": list(ch.get("stories") or []),
                    "wavs": results.get(ci, []),
                }
                for ci, ch in enumerate(chapters)
            ]
            return out, voice_used


# --- WAV assembly & optional transcoding ------------------------------------
def _read_wav(data: bytes) -> tuple[tuple[int, int, int], bytes]:
    with wave.open(io.BytesIO(data), "rb") as wf:
        params = (wf.getnchannels(), wf.getsampwidth(), wf.getframerate())
        frames = wf.readframes(wf.getnframes())
    return params, frames


def _silence_frames(seconds: float, nchannels: int, sampwidth: int, framerate: int) -> bytes:
    count = max(0, int(round(seconds * framerate)))
    return b"\x00" * (count * nchannels * sampwidth)


def _chime_frames(nchannels: int, sampwidth: int, framerate: int) -> bytes:
    """Gentle two-note transition chime (G4 -> C5, 0.32s) for 16-bit PCM."""
    if sampwidth != 2:
        return b""
    out = bytearray()
    for freq, dur in ((392.0, 0.14), (523.25, 0.20)):
        n = int(framerate * dur)
        amp = 2600  # subtle (~8% of full scale)
        for i in range(n):
            t = i / framerate
            # Smooth attack/decay envelope to avoid clicks.
            env = math.sin(math.pi * i / max(1, n - 1)) ** 1.4
            sample = int(amp * env * math.sin(2.0 * math.pi * freq * t))
            packed = struct.pack("<h", max(-32767, min(32767, sample)))
            out.extend(packed * nchannels)
    return bytes(out)


def stitch_wav_segments(
    chapters: list[dict[str, Any]],
    out_wav: Path,
    *,
    paragraph_pause_s: float = 0.45,
    chapter_pause_s: float = 1.0,
    chime: bool = True,
) -> tuple[float, list[dict[str, Any]]]:
    """Concatenate synthesized chapter WAV chunks with pauses and return ``(duration_sec, chapter_marks)``.

    Each item in ``chapters`` has ``{"title": str, "story_ids": list[int], "wavs": list[bytes]}``.
    """
    params: tuple[int, int, int] | None = None
    decoded: list[tuple[dict[str, Any], list[bytes]]] = []
    for ch in chapters:
        chunk_frames: list[bytes] = []
        for raw in ch.get("wavs") or []:
            if not raw:
                continue
            p, frames = _read_wav(raw)
            if params is None:
                params = p
            elif p != params:
                raise RuntimeError(f"inconsistent TTS WAV format: {p} != {params}")
            chunk_frames.append(frames)
        if chunk_frames:
            decoded.append((ch, chunk_frames))

    if params is None:
        raise RuntimeError("TTS produced no audio frames")

    nchannels, sampwidth, framerate = params
    frame_bytes = nchannels * sampwidth
    para_gap = _silence_frames(paragraph_pause_s, nchannels, sampwidth, framerate)
    pre_chime = _silence_frames(chapter_pause_s * 0.45, nchannels, sampwidth, framerate)
    post_chime = _silence_frames(chapter_pause_s * 0.55, nchannels, sampwidth, framerate)
    chime_pcm = _chime_frames(nchannels, sampwidth, framerate) if chime else b""

    marks: list[dict[str, Any]] = []
    cursor_frames = 0
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(nchannels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)

        for idx, (ch, chunk_frames) in enumerate(decoded):
            if idx > 0:
                sep = pre_chime + chime_pcm + post_chime
                wf.writeframes(sep)
                cursor_frames += len(sep) // frame_bytes

            start_sec = round(cursor_frames / framerate, 1)
            for ci, frames in enumerate(chunk_frames):
                if ci > 0 and para_gap:
                    wf.writeframes(para_gap)
                    cursor_frames += len(para_gap) // frame_bytes
                wf.writeframes(frames)
                cursor_frames += len(frames) // frame_bytes
            end_sec = round(cursor_frames / framerate, 1)
            marks.append({
                "title": ch["title"],
                "start_sec": start_sec,
                "end_sec": end_sec,
                "story_ids": list(ch.get("story_ids") or []),
                "stories": list(ch.get("stories") or []),
            })

    return round(cursor_frames / framerate, 1), marks


def encode_audio(wav_path: Path, fmt: str | None = None) -> tuple[Path, str]:
    """Optionally transcode ``wav_path`` to mp3/opus via ffmpeg; falls back to wav."""
    target = (fmt or config.PODCAST_FORMAT or "auto").lower()
    ffmpeg = shutil.which("ffmpeg")
    if target == "wav" or not ffmpeg:
        return wav_path, "audio/wav"

    if target == "opus":
        out = wav_path.with_suffix(".ogg")
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_path), "-c:a", "libopus", "-b:a", "64k", str(out)]
        mime = "audio/ogg"
    else:
        out = wav_path.with_suffix(".mp3")
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_path), "-c:a", "libmp3lame", "-q:a", "4", str(out)]
        mime = "audio/mpeg"

    proc = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if proc.returncode != 0 or not out.is_file():
        log.warning("ffmpeg transcode failed (%s), keeping WAV: %s",
                    proc.returncode, proc.stderr.decode("utf-8", errors="replace").strip()[:200])
        out.unlink(missing_ok=True)
        return wav_path, "audio/wav"

    wav_path.unlink(missing_ok=True)
    return out, mime
