"""Podcast generation, local TTS stitching/backends, scheduling, and /podcasts API."""
from __future__ import annotations

import datetime as dt
import io
import wave

import httpx

from app import api, config, db, ollama, pipeline, podcast, reports, scoring, tts
from conftest import add_article


def _make_wav(duration_s: float = 0.25, framerate: int = 22050) -> bytes:
    buf = io.BytesIO()
    nframes = int(framerate * duration_s)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        wf.writeframes(b"\x10\x00" * nframes)
    return buf.getvalue()


def test_clean_for_tts_and_split_paragraphs():
    raw = "<think>internal</think>**Breaking**: See [report](https://example.com) at https://foo.bar.\n\n- Bullet one."
    cleaned = podcast.clean_for_tts(raw)
    assert "internal" not in cleaned
    assert "https://" not in cleaned
    assert "Breaking: See report" in cleaned
    assert "Bullet one." in cleaned

    long_para = " ".join(f"Sentence number {i} is here." for i in range(40))
    chunks = podcast.split_paragraphs(long_para, max_chars=180)
    assert len(chunks) > 1
    assert all(len(c) <= 180 for c in chunks)


def test_stitch_wav_segments_adds_pauses_and_chapter_marks(tmp_path):
    w = _make_wav(0.2)
    out = tmp_path / "show.wav"
    dur, marks = tts.stitch_wav_segments(
        [
            {"title": "Intro", "story_ids": [1], "wavs": [w, w]},
            {"title": "Tech", "story_ids": [2, 3], "wavs": [w]},
        ],
        out,
        paragraph_pause_s=0.5,
        chapter_pause_s=1.0,
        chime=True,
    )
    assert out.is_file()
    # Intro: 0.2 + 0.5 + 0.2 = 0.9s; chime+pause ~1.34s; Tech: 0.2s -> ~2.44s total.
    assert dur >= 2.2
    assert len(marks) == 2
    assert marks[0]["title"] == "Intro" and marks[0]["start_sec"] == 0.0
    assert marks[1]["title"] == "Tech" and marks[1]["start_sec"] > marks[0]["end_sec"]
    assert marks[1]["story_ids"] == [2, 3]


def test_tts_magpie_remote_http_and_dialogue(monkeypatch):
    wav = _make_wav(0.1)
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/tts"
        assert req.headers.get("authorization") == "Bearer secret-key"
        seen.append(__import__("json").loads(req.read().decode("utf-8")))
        return httpx.Response(200, content=wav, headers={"content-type": "audio/wav"})

    monkeypatch.setattr(config, "MAGPIE_URL", "http://llm-pc.local:8880")
    monkeypatch.setattr(config, "MAGPIE_API_KEY", "secret-key")
    monkeypatch.setattr(config, "MAGPIE_VOICE_HOST1", "Leo")
    monkeypatch.setattr(config, "MAGPIE_VOICE_HOST2", "Sofia")
    real_client = httpx.Client
    monkeypatch.setattr(
        tts.httpx,
        "Client",
        lambda **kw: real_client(
            base_url=kw.get("base_url", ""),
            headers=kw.get("headers"),
            transport=httpx.MockTransport(handler),
        ),
    )

    st = tts.status()
    assert st["configured"] and st["backend"] == "magpie_remote" and "llm-pc.local:8880" in st["detail"]
    chunks = podcast.dialogue_chunks("HOST1: Hallo!\n\nHOST2: Willkommen!", lang="de")
    out, voice = tts.synthesize_chapters([{"title": "Intro", "story_ids": [1], "chunks": chunks}], lang="de")
    assert voice == "Leo+Sofia" and len(out[0]["wavs"]) == 2
    assert seen == [
        {"text": "Hallo!", "language": "de", "speaker_index": 3},
        {"text": "Willkommen!", "language": "de", "speaker_index": 4},
    ]


def test_tts_magpie_persistent_worker_and_dialogue(tmp_path, monkeypatch):
    import sys

    seen_jobs = tmp_path / "seen_jobs.jsonl"
    fake_script = tmp_path / "fake_magpie.py"
    fake_script.write_text(
        "import json, os, sys, wave\n"
        "from pathlib import Path\n"
        "assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''\n"
        "print(json.dumps({'event': 'ready', 'sample_rate': 22050, 'device': 'cpu'}), flush=True)\n"
        "for raw in sys.stdin:\n"
        "    line = raw.strip()\n"
        "    if not line: continue\n"
        "    job = json.loads(line)\n"
        "    if job.get('cmd') == 'quit': break\n"
        f"    with open({str(seen_jobs)!r}, 'a', encoding='utf-8') as f: f.write(json.dumps(job) + '\\n')\n"
        "    out = Path(job['out_dir'])\n"
        "    out.mkdir(parents=True, exist_ok=True)\n"
        "    used_h2 = False\n"
        "    for ci, ch in enumerate(job['chapters']):\n"
        "        files = []\n"
        "        for pi, item in enumerate(ch['chunks']):\n"
        "            if isinstance(item, dict) and int(item.get('host') or 1) == 2: used_h2 = True\n"
        "            p = out / f'c{ci}_{pi}.wav'\n"
        "            with wave.open(str(p), 'wb') as wf:\n"
        "                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050)\n"
        "                wf.writeframes(b'\\x01\\x00' * 1100)\n"
        "            files.append(str(p))\n"
        "        used = f\"{job['voice1']}+{job['voice2']}\" if used_h2 else job['voice1']\n"
        "        print(json.dumps({'event': 'chapter', 'job_id': job['job_id'], 'index': ci, 'voice': used, 'files': files}), flush=True)\n"
        "    used = f\"{job['voice1']}+{job['voice2']}\" if used_h2 else job['voice1']\n"
        "    print(json.dumps({'event': 'done', 'job_id': job['job_id'], 'voice': used}), flush=True)\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "MAGPIE_PYTHON", sys.executable)
    monkeypatch.setattr(config, "MAGPIE_DEVICE", "cpu")
    monkeypatch.setattr(config, "MAGPIE_VOICE_HOST1", "Leo")
    monkeypatch.setattr(config, "MAGPIE_VOICE_HOST2", "Sofia")
    monkeypatch.setattr(tts, "_SYNTH_SCRIPT", fake_script)
    tts.shutdown()

    try:
        st = tts.status()
        assert st["configured"] and st["backend"] == "magpie" and "MagpieTTS 357M" in st["detail"]
        assert st["voice"] == "Leo" and "Sofia" in st["voices"]

        progress = []
        out1, voice1 = tts.synthesize_chapters(
            [{"title": "Intro", "story_ids": [1], "chunks": ["Hallo", "Welt"]}],
            lang="de",
            on_progress=lambda i, n, t: progress.append((i, n, t)),
        )
        assert voice1 == "Leo" and len(out1[0]["wavs"]) == 2
        assert progress == [(1, 1, "Intro")]
        pid1 = tts._worker_proc.pid

        # Second call reuses the SAME warm persistent worker process (singleton) and supports 2-host dialogue.
        chunks = podcast.dialogue_chunks("HOST1: Hallo!\n\nHOST2: Willkommen!", lang="de")
        out2, voice2 = tts.synthesize_chapters(
            [{"title": "Dialog", "story_ids": [2], "chunks": chunks}],
            lang="de",
        )
        assert tts._worker_proc.pid == pid1
        assert voice2 == "Leo+Sofia" and len(out2[0]["wavs"]) == 2

        jobs = [__import__("json").loads(ln) for ln in seen_jobs.read_text(encoding="utf-8").splitlines()]
        assert len(jobs) == 2
        assert jobs[1]["speaker1"] == 3 and jobs[1]["speaker2"] == 4
    finally:
        tts.shutdown()


def test_podcast_end_to_end_audio_and_ntfy(client, feed_id, llm, monkeypatch):
    add_article(feed_id, "Solar boom accelerates across Europe")
    add_article(feed_id, "New rail tunnel opens in the Alps")
    pipeline.process_pending()

    wav = _make_wav(0.2)
    monkeypatch.setattr(tts, "status", lambda: {"configured": True, "backend": "kokoro_local",
                                                "voice": "martin", "detail": "Kokoro CPU (de)", "ffmpeg": False})
    monkeypatch.setattr(
        tts,
        "synthesize_chapters",
        lambda chapters, lang="de", on_progress=None, stop_event=None: (
            [{"title": c["title"], "story_ids": c["story_ids"], "stories": c.get("stories", []),
              "wavs": [wav for _ in c["chunks"]]} for c in chapters],
            "martin",
        ),
    )
    monkeypatch.setattr(tts, "encode_audio", lambda p, fmt=None: (p, "audio/wav"))
    monkeypatch.setattr(ollama, "chat", lambda messages, **_kw: "Willkommen zur heutigen Sendung.\n\nHier sind die wichtigsten Nachrichten.")

    pushed = []
    monkeypatch.setattr(config, "HEADS_UP_NTFY_URL", "https://ntfy.example/topic")
    monkeypatch.setattr(reports, "push_ntfy", lambda text, title, click_url=None: (pushed.append((title, text, click_url)), True)[1])

    ep = podcast.start_generation(1, duration_minutes=5, notify=True, background=False)
    assert ep["status"] == "ready"
    assert ep["has_audio"] is True
    assert ep["duration_sec"] > 0
    assert ep["voice"] == "martin"
    assert len(ep["chapters"]) >= 2
    assert len(pushed) == 1 and pushed[0][0] == "Podcast ready"

    # API list, detail, audio stream, and delete.
    listing = client.get("/api/podcasts").json()
    assert len(listing["episodes"]) == 1
    assert listing["episodes"][0]["id"] == ep["id"]

    detail = client.get(f"/api/podcasts/{ep['id']}").json()["episode"]
    assert "Willkommen" in detail["script"]

    audio_resp = client.get(f"/api/podcasts/{ep['id']}/audio")
    assert audio_resp.status_code == 200
    assert audio_resp.headers["content-type"].startswith("audio/wav")
    assert audio_resp.content[:4] == b"RIFF"

    assert client.delete(f"/api/podcasts/{ep['id']}").status_code == 200
    assert client.get(f"/api/podcasts/{ep['id']}").status_code == 404
    assert client.get(f"/api/podcasts/{ep['id']}/audio").status_code == 404


def test_podcast_script_only_without_tts(client, feed_id, llm, monkeypatch, tmp_path):
    add_article(feed_id, "Local council approves new bike lanes")
    pipeline.process_pending()

    monkeypatch.setattr(config, "MAGPIE_PYTHON", str(tmp_path / "missing-python"))
    monkeypatch.setattr(ollama, "chat", lambda messages, **_kw: "Guten Morgen, hier ist Ihr Nachrichten-Update.")

    # Without script_only, unconfigured TTS returns 409.
    r = client.post("/api/podcasts/generate", json={"duration_minutes": 5, "script_only": False})
    assert r.status_code == 409

    # With script_only=True, generation succeeds without TTS.
    ep = podcast.start_generation(1, duration_minutes=5, notify=False, script_only=True, background=False)
    assert ep["status"] == "ready"
    assert ep["has_audio"] is False
    assert "Nachrichten-Update" in ep["script"]


def test_scheduled_daily_podcast_in_reports_tick(monkeypatch):
    started = []
    monkeypatch.setattr(
        podcast,
        "start_generation",
        lambda uid, **kw: (started.append((uid, kw)), {"id": 99, "duration_target": kw.get("duration_minutes", 15)})[1],
    )
    scoring.update_profile({"ntfy_reports": [
        {
            "kind": "podcast", "time": "06:30", "days": [0], "enabled": True,
            "duration_minutes": 30, "view": "default", "max_stories": 12,
            "style": "briefing", "format": "dialogue", "fact_check": False, "script_only": True,
        },
    ]})
    monday = dt.datetime(2026, 10, 12, 6, 31)
    monday -= dt.timedelta(days=monday.weekday())
    reports.tick({}, monday)
    reports.tick({}, monday.replace(minute=32))
    assert len(started) == 1
    assert started[0][0] == 1
    assert started[0][1]["duration_minutes"] == 30
    assert started[0][1]["view"] == "default"
    assert started[0][1]["max_stories"] == 12
    assert started[0][1]["style"] == "briefing"
    assert started[0][1]["fmt"] == "dialogue"
    assert started[0][1]["fact_check"] is False
    assert started[0][1]["script_only"] is True


def test_podcasts_auth_and_public_page(client):
    from fastapi.testclient import TestClient

    assert client.get("/podcasts").status_code == 200
    assert "podcast-form" in client.get("/podcasts").text

    # /podcasts is publicly accessible for signed-out visitors; private /api/podcasts stays 401.
    with TestClient(api.create_app(), follow_redirects=False) as anon:
        assert anon.get("/api/podcasts").status_code == 401
        assert anon.get("/podcasts").status_code == 200
        assert anon.get("/api/podcasts/public").status_code == 200


def test_chat_think_false_merges_structured_extra(monkeypatch):
    sent: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "Hallo"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}

    class _Client:
        def post(self, _path, *, json, **_k):
            sent.update(json)
            return _Resp()

    monkeypatch.setattr(config, "LLM_BASE_URL", "http://llm.local/v1")
    monkeypatch.setattr(config, "LLM_STRUCTURED_EXTRA", {"chat_template_kwargs": {"enable_thinking": False}})
    monkeypatch.setattr(ollama, "_http", lambda: _Client())

    assert ollama.chat([{"role": "user", "content": "hi"}], role="podcast", think=False) == "Hallo"
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}


def test_podcast_uses_thinking_with_fallback_and_fact_check(client, feed_id, llm, monkeypatch):
    add_article(feed_id, "European grid hits 60 percent renewable share")
    pipeline.process_pending()

    calls: list[tuple[bool, int, str]] = []

    def fake_chat(messages, **kw):
        think = kw.get("think", True)
        max_tokens = kw.get("max_tokens", 0)
        sys_msg = messages[0]["content"]
        is_fc = ("fact-checking" in sys_msg) or ("prüfst ein DEUTSCHES Podcast-Skript" in sys_msg)
        calls.append((think, max_tokens, "fact-checking" if is_fc else "draft"))
        # Simulate first draft call exhausting thinking tokens -> empty content -> retry think=False.
        if len(calls) == 1:
            return ""
        if is_fc:
            return "Faktengepruefter Text ueber das europaeische Stromnetz."
        return "Entwurfstext ueber das europaeische Stromnetz."

    monkeypatch.setattr(ollama, "chat", fake_chat)
    ep = podcast.start_generation(1, duration_minutes=3, notify=False, script_only=True, fact_check=True, background=False)
    assert ep["status"] == "ready"
    assert "Faktengepruefter Text" in ep["script"]
    # First call used think=True with >=6144 tokens, fell back to think=False, and fact-check ran.
    assert calls[0][0] is True and calls[0][1] >= 6144
    assert calls[1][0] is False
    assert any(kind == "fact-checking" and think is True for think, _mt, kind in calls)


def test_strip_thinking_handles_unclosed_tag_and_heads_up(monkeypatch):
    assert ollama.strip_thinking("<think>secret</think>Answer.") == "Answer."
    assert ollama.strip_thinking("Lead.<think>truncated reasoning") == "Lead."
    assert podcast.clean_for_tts("Spoken.<think>never finished") == "Spoken."


def test_dialogue_mode_parses_hosts_and_rss_feed_and_stop(client, feed_id, llm, monkeypatch):
    from fastapi.testclient import TestClient

    add_article(feed_id, "Quantum network links two German cities")
    pipeline.process_pending()

    # Dialogue chunking splits HOST1 / HOST2 turns cleanly by host index.
    chunks_en = podcast.dialogue_chunks("HOST1: Welcome back.\n\nHOST2: Great to be here!", lang="en")
    assert len(chunks_en) == 2
    assert chunks_en[0] == {"text": "Welcome back.", "host": 1}
    assert chunks_en[1] == {"text": "Great to be here!", "host": 2}

    wav = _make_wav(0.2)
    monkeypatch.setattr(tts, "status", lambda: {"configured": True, "backend": "magpie",
                                                "voice": "Leo", "detail": "MagpieTTS 357M (CPU, Leo+Sofia)", "ffmpeg": False})
    monkeypatch.setattr(
        tts,
        "synthesize_chapters",
        lambda chapters, lang="en", on_progress=None, stop_event=None: (
            [{"title": c["title"], "story_ids": c["story_ids"], "stories": c.get("stories", []),
              "wavs": [wav for _ in c["chunks"]]} for c in chapters],
            "Leo+Sofia",
        ),
    )
    monkeypatch.setattr(tts, "encode_audio", lambda p, fmt=None: (p, "audio/wav"))
    monkeypatch.setattr(ollama, "chat", lambda messages, **_kw: "HOST1: Hello!\n\nHOST2: Big news today.")

    ep = podcast.start_generation(
        1, duration_minutes=3, language="en", style="entertaining", fmt="dialogue",
        notify=False, fact_check=False, background=False,
    )
    assert ep["status"] == "ready"
    assert ep["language"] == "en" and ep["style"] == "entertaining" and ep["format"] == "dialogue"
    assert ep["chapters"][0]["stories"]

    # Private RSS feed works without cookies using the token, and rotates cleanly.
    listing = client.get("/api/podcasts").json()
    feed_path = listing["feed_path"]
    assert feed_path.startswith("/api/podcasts/feed/") and feed_path.endswith(".xml")

    with TestClient(api.create_app()) as anon:
        rss_resp = anon.get(feed_path)
        assert rss_resp.status_code == 200
        assert "<rss version=\"2.0\"" in rss_resp.text
        assert ep["title"] in rss_resp.text
        token = feed_path.rsplit("/", 1)[-1].removesuffix(".xml")
        aud = anon.get(f"/api/podcasts/feed/{token}/audio/{ep['id']}")
        assert aud.status_code == 200 and aud.content[:4] == b"RIFF"

    rotated = client.post("/api/podcasts/feed/rotate").json()["feed_path"]
    assert rotated != feed_path
    with TestClient(api.create_app()) as anon:
        assert anon.get(feed_path).status_code == 404
        assert anon.get(rotated).status_code == 200

    # Stopping an active queued episode marks it cancelled.
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO PodcastEpisode (user_id, title, status) VALUES (1, 'In progress', 'scripting')"
        )
        active_id = int(cur.lastrowid)
    # Starting a second episode while one is active is rejected with 409 (prevents double-submit).
    dup = client.post("/api/podcasts/generate", json={"duration_minutes": 3, "script_only": True})
    assert dup.status_code == 409
    assert client.post(f"/api/podcasts/{active_id}/stop").status_code == 200
    assert podcast.get_episode(active_id, 1)["status"] == "cancelled"

    # Deleting a queued episode cancels its stop event so its thread cannot run as a zombie.
    import threading
    with db.get_conn() as conn:
        qid = int(conn.execute(
            "INSERT INTO PodcastEpisode (user_id, title, status) VALUES (1, 'Queued', 'queued')"
        ).lastrowid)
    ev = threading.Event()
    podcast._stops[qid] = ev
    assert client.delete(f"/api/podcasts/{qid}").status_code == 200
    assert ev.is_set()
    podcast._stops.pop(qid, None)


def test_user_deletion_removes_podcast_audio_files(client, feed_id, llm, monkeypatch):
    from app import users

    users.create("alice", "longpassword123", False)
    with db.get_conn() as conn:
        alice_id = conn.execute("SELECT id FROM AppUser WHERE name = 'alice'").fetchone()[0]
    config.PODCAST_DIR.mkdir(parents=True, exist_ok=True)
    audio = config.PODCAST_DIR / "alice-ep.wav"
    audio.write_bytes(_make_wav(0.1))
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO PodcastEpisode (user_id, title, status, audio_path) VALUES (?, 'Ep', 'ready', ?)",
            (alice_id, audio.name),
        )
    assert audio.is_file()
    users.delete(alice_id, actor_id=1)
    assert not audio.is_file()


def test_ranking_cache_hits_and_invalidates_on_read(client, feed_id, llm, monkeypatch):
    add_article(feed_id, "Cache test story headline")
    pipeline.process_pending()

    calls = 0
    real_ranked = scoring.ranked_stories

    def counted(*a, **kw):
        nonlocal calls
        calls += 1
        return real_ranked(*a, **kw)

    monkeypatch.setattr(scoring, "ranked_stories", counted)
    r1 = client.get("/api/stories?limit=50").json()
    r2 = client.get("/api/stories?limit=100").json()
    assert calls == 1  # second call served from ranking cache
    sid = r1["stories"][0]["id"]
    assert client.post(f"/api/stories/{sid}/read").status_code == 200
    client.get("/api/stories?limit=50")
    assert calls == 2  # invalidated by mark_read


def test_german_language_drift_and_mid_show_greeting_stripping(monkeypatch):
    en_slip = (
        "In Washington, Democrats have secured a historic win, taking control of both the House "
        "and the Senate, which will limit the legislative agenda."
    )
    assert podcast._looks_english(en_slip) is True
    assert podcast._looks_english("In Washington haben die Demokraten die Mehrheit im Senat gewonnen.") is False

    # Mixed German + English paragraph is caught and only the English paragraph is translated.
    mixed = (
        "Die Europäische Zentralbank hält die Leitzinsen stabil.\n\n"
        + en_slip
    )
    monkeypatch.setattr(ollama, "chat", lambda *_a, **_k: "In Washington haben die Demokraten die Mehrheit gewonnen.")
    fixed = podcast._ensure_german(mixed, fmt="solo", label="block")
    assert "Europäische Zentralbank" in fixed
    assert "Demokraten die Mehrheit gewonnen" in fixed
    assert "historic win" not in fixed

    # Mid-episode greetings are stripped from both solo and dialogue segments.
    solo_greet = "Hallo zusammen! Die Energiepreise sind im November erneut gesunken."
    assert podcast._strip_mid_show_greetings(solo_greet, lang="de", fmt="solo") == "Die Energiepreise sind im November erneut gesunken."
    dlg_greet = "HOST1: Herzlich willkommen zurück! Heute schauen wir auf die Bahn.\n\nHOST2: Genau."
    stripped_dlg = podcast._strip_mid_show_greetings(dlg_greet, lang="de", fmt="dialogue")
    assert stripped_dlg.startswith("HOST1: Heute schauen wir auf die Bahn.")


def test_satire_detection_omits_articles_from_stories_podcasts_and_briefs(client, feed_id, llm, monkeypatch):
    from app import content_quality, harness

    assert content_quality.is_satire(url="https://www.der-postillon.com/2026/10/verbrenner.html") is True
    assert content_quality.is_satire(title="Union plant Gesetz (der-postillon.com)") is True
    assert content_quality.is_satire(content="Berlin (dpo) - Die Bundesregierung hat beschlossen...") is True
    assert content_quality.is_satire(url="https://www.tagesschau.de/inland/test.html", title="Real news", content="Normal text") is False

    # 1) Real story + 2) Postillon satire (deterministic) + 3) Unknown-domain satire (LLM-flagged).
    real_id = add_article(feed_id, "Bundestag verabschiedet neues Klimapaket",
                          url="https://www.tagesschau.de/inland/klima.html")
    postillon_id = add_article(feed_id, "Union will Verbrenner-Aus stoppen (der-postillon.com)",
                               body="Berlin (dpo) - Autofahrer sollen hohe Spritpreise weiter genießen können. " + "wort " * 80,
                               url="https://www.der-postillon.com/2026/10/verbrenner-aus.html")
    llm_satire_id = add_article(feed_id, "Absurde Parodie auf unbekannter Website",
                                body="Eine völlig erfundene satirische Meldung ohne bekanntes Satire-Portal. " + "wort " * 80,
                                url="https://example.org/parodie-artikel")

    real_assess = harness.assess_article
    monkeypatch.setattr(
        harness,
        "assess_article",
        lambda title, content: (
            {"quality": 1, "breaking": False, "satire": True}
            if "Parodie" in title else real_assess(title, content)
        ),
    )
    pipeline.process_pending()

    with db.get_conn() as conn:
        stories = conn.execute("SELECT id, headline FROM Story").fetchall()
        art_map = {
            r["id"]: (r["story_id"], r["content_class"])
            for r in conn.execute("SELECT id, story_id, content_class FROM Article").fetchall()
        }
    # ONLY the real article produced a Story; both satire articles were omitted completely.
    assert len(stories) == 1
    assert art_map[real_id][0] == stories[0]["id"]
    assert art_map[postillon_id] == (None, "satire")
    assert art_map[llm_satire_id] == (None, "satire")


def test_public_podcast_privacy_and_endpoints(client, feed_id, llm, monkeypatch):
    from fastapi.testclient import TestClient

    add_article(feed_id, "European rail network expands night trains")
    pipeline.process_pending()

    # Give user 1 sensitive personal info in their profile.
    scoring.update_profile({"about": "SECRET_PRIVATE_BIO_XYZ", "language": "en"}, user_id=1)

    prompts_seen: list[str] = []
    monkeypatch.setattr(ollama, "chat", lambda msgs, **_k: (prompts_seen.append(msgs[0]["content"]), "Öffentliche Nachrichtensendung.")[1])
    wav = _make_wav(0.2)
    monkeypatch.setattr(tts, "status", lambda: {"configured": True, "backend": "magpie",
                                                "voice": "Leo", "detail": "MagpieTTS 357M (CPU, Leo+Sofia)", "ffmpeg": False})
    monkeypatch.setattr(
        tts,
        "synthesize_chapters",
        lambda chapters, lang="de", on_progress=None, stop_event=None: (
            [{"title": c["title"], "story_ids": c["story_ids"], "stories": c.get("stories", []),
              "wavs": [wav for _ in c["chunks"]]} for c in chapters],
            "Leo",
        ),
    )
    monkeypatch.setattr(tts, "encode_audio", lambda p, fmt=None: (p, "audio/wav"))

    pub_ep = podcast.start_generation(1, duration_minutes=3, view="default", style="briefing",
                                      notify=False, fact_check=False, background=False)
    priv_ep = podcast.start_generation(1, duration_minutes=3, view="personal",
                                       notify=False, fact_check=False, background=False)
    # Public episode must NEVER see user's private bio or profile language override ('en').
    assert pub_ep["language"] == "de"
    assert not any("SECRET_PRIVATE_BIO_XYZ" in p for p in prompts_seen[:len(pub_ep["chapters"])])

    with TestClient(api.create_app()) as anon:
        pub_list = anon.get("/api/podcasts/public").json()
        ids = [e["id"] for e in pub_list["episodes"]]
        assert pub_ep["id"] in ids
        assert priv_ep["id"] not in ids
        assert "user_id" not in pub_list["episodes"][0]

        assert anon.get(f"/api/podcasts/public/{pub_ep['id']}").status_code == 200
        assert anon.get(f"/api/podcasts/public/{priv_ep['id']}").status_code == 404
        assert anon.get(f"/api/podcasts/public/{pub_ep['id']}/audio").status_code == 200
        assert anon.get(f"/api/podcasts/public/{priv_ep['id']}/audio").status_code == 404

        rss = anon.get("/api/podcasts/public/feed.xml")
        assert rss.status_code == 200
        assert pub_ep["title"] in rss.text
        assert "admin" not in rss.text.lower()


def test_security_fixes_podcast_acl_feed_ssrf_and_proxy_ip(client, monkeypatch):
    from fastapi.testclient import TestClient
    from app import ingest, users, web

    # 1. Non-admin user cannot publish public 'For everyone' podcast or see ntfy enabled.
    users.create("regular", "regular-pass-123", False)
    monkeypatch.setattr(config, "HEADS_UP_NTFY_URL", "https://ntfy.example/topic")
    with TestClient(api.create_app()) as reg:
        assert reg.post("/api/auth/login", json={"username": "regular", "password": "regular-pass-123"}).status_code == 200
        listing = reg.get("/api/podcasts").json()
        assert listing["ntfy_enabled"] is False
        assert listing["can_publish_public"] is False
        r = reg.post("/api/podcasts/generate", json={"duration_minutes": 3, "view": "default", "script_only": True})
        assert r.status_code == 403

    # 2. Public RSS feed redirecting to loopback (127.0.0.1) is blocked.
    def redirect_handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "public.example":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8880/health"})
        return httpx.Response(200, text="should-never-reach")

    real_client = httpx.Client(transport=httpx.MockTransport(redirect_handler), follow_redirects=False)
    monkeypatch.setattr(ingest, "http", lambda: real_client)
    monkeypatch.setattr(ingest, "is_public_url", lambda u: "public.example" in u)
    try:
        try:
            ingest.fetch("https://public.example/rss.xml")
            raise AssertionError("expected redirect to non-public URL to be blocked")
        except ValueError as e:
            assert "non-public" in str(e)
    finally:
        real_client.close()

    # 3. client_ip only trusts peers in FORWARDED_ALLOW_IPS and validates extracted IPs.
    class _Req:
        def __init__(self, host, headers):
            self.client = type("C", (), {"host": host})()
            self.headers = headers

    monkeypatch.setattr(config, "FORWARDED_ALLOW_IPS", "127.0.0.1")
    # Untrusted LAN IP cannot spoof X-Forwarded-For:
    assert web.client_ip(_Req("192.168.1.50", {"x-forwarded-for": "198.51.100.1"})) == "192.168.1.50"
    # Trusted proxy IP extracts valid right-most IP, ignores invalid garbage:
    assert web.client_ip(_Req("127.0.0.1", {"x-forwarded-for": "198.51.100.9"})) == "198.51.100.9"
    assert web.client_ip(_Req("127.0.0.1", {"x-forwarded-for": "not-an-ip"})) == "127.0.0.1"


def test_clip_summary_enforces_max_sentences():
    from app import story

    nine = " ".join(f"Sentence {i} happened." for i in range(1, 10))
    clipped = story.clip_summary(nine)
    assert story._sentence_count(clipped) <= 6
    assert "Sentence 6 happened." in clipped
    assert "Sentence 7" not in clipped
    # Decimal / version numbers must not inflate sentence count.
    assert story._sentence_count("Ling-3.2-air beats Ling-3.0 by 6.7 percent. Users welcome it.") == 2


def test_cleanup_stale_removes_orphan_audio_files(tmp_path):
    config.PODCAST_DIR.mkdir(parents=True, exist_ok=True)
    kept = config.PODCAST_DIR / "kept.mp3"
    orphan = config.PODCAST_DIR / "orphan.mp3"
    kept.write_bytes(b"mp3")
    orphan.write_bytes(b"mp3")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO PodcastEpisode (user_id, title, status, audio_path) VALUES (1, 'Kept', 'ready', 'kept.mp3')"
        )
    podcast.cleanup_stale()
    assert kept.is_file() and not orphan.is_file()


def test_db_backup_creates_snapshot_and_rotates(tmp_path):
    backup_dir = tmp_path / "baks"
    backup_dir.mkdir()
    # Seed 8 old daily backups; keep=3 should keep only the 2 newest + today.
    for d in range(1, 9):
        (backup_dir / f"news-2026-01-0{d}.db").write_bytes(b"old")
    out = db.backup(dest_dir=backup_dir, keep=3)
    assert out is not None and out.is_file() and out.stat().st_size > 0
    remaining = sorted(p.name for p in backup_dir.glob("news-*.db"))
    assert len(remaining) == 3 and out.name in remaining
