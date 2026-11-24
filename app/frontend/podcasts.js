/* Podcasts page (/podcasts):
 * - Signed-in users: generate episodes, manage personal & public episodes, private + public RSS.
 * - Signed-out visitors: public podcast page showing ONLY ready "For everyone" episodes + public RSS.
 * - Custom interactive podcast player with chapter timeline markers, skip -15/+30s, speed cycle, and
 *   structured dialogue transcript viewer. */

const statusEl = document.getElementById("podcast-status");
const publicBanner = document.getElementById("public-banner");
const publicRssCopyTop = document.getElementById("public-rss-copy");
const builderEl = document.getElementById("podcast-builder");
const ttsStatusEl = document.getElementById("tts-status");
const ttsHintEl = document.getElementById("tts-hint");
const formEl = document.getElementById("podcast-form");
const durGroup = document.getElementById("dur-presets");
const minutesEl = document.getElementById("pod-minutes");
const langEl = document.getElementById("pod-lang");
const styleEl = document.getElementById("pod-style");
const formatEl = document.getElementById("pod-format");
const viewEl = document.getElementById("pod-view");
const storiesEl = document.getElementById("pod-stories");
const notifyWrap = document.getElementById("notify-wrap");
const notifyEl = document.getElementById("pod-notify");
const factCheckEl = document.getElementById("pod-fact-check");
const scriptOnlyEl = document.getElementById("pod-script-only");
const genBtn = document.getElementById("pod-generate");
const rssBar = document.getElementById("rss-bar");
const rssCopyBtn = document.getElementById("rss-copy");
const rssRotateBtn = document.getElementById("rss-rotate");
const rssPublicCopyBtn = document.getElementById("rss-public-copy");
const listEl = document.getElementById("podcast-list");

const ACTIVE = new Set(["queued", "scripting", "synthesizing"]);
const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2];
const SPEED_KEY = "news_pod_speed";

const ICON_PLAY = `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M8 5.14v13.72a1 1 0 0 0 1.53.85l11.02-6.86a1 1 0 0 0 0-1.7L9.53 4.29A1 1 0 0 0 8 5.14z"/></svg>`;
const ICON_PAUSE = `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="4.5" width="4.5" height="15" rx="1.2" fill="currentColor"/><rect x="13.5" y="4.5" width="4.5" height="15" rx="1.2" fill="currentColor"/></svg>`;
const ICON_VOL = `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M11 5L6 9H3v6h3l5 4V5zM15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></svg>`;
const ICON_MUTE = `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M11 5L6 9H3v6h3l5 4V5zM22 9l-6 6M16 9l6 6"/></svg>`;

let isPublicMode = false;
let selectedDuration = 15;
let storiesTouched = false;
let ttsConfigured = false;
let feedPath = "";
let publicFeedPath = "/api/podcasts/public/feed.xml";
let pollTimer = null;
const scriptCache = new Map();

function savedSpeed() {
  const v = Number(localStorage.getItem(SPEED_KEY));
  return SPEEDS.includes(v) ? v : 1;
}

function posKey(id) {
  return `news_pod_pos_${id}`;
}

function recommendedStories(min) {
  if (min <= 3) return 4;
  if (min <= 8) return 8;
  if (min <= 20) return 16;
  if (min <= 40) return 28;
  if (min <= 75) return 40;
  return 50;
}

function setDuration(min, { syncInput = true } = {}) {
  selectedDuration = Math.max(1, Math.min(180, Math.round(Number(min) || 15)));
  if (syncInput) minutesEl.value = selectedDuration;
  durGroup.querySelectorAll("button[data-min]").forEach((b) => {
    b.classList.toggle("active", Number(b.dataset.min) === selectedDuration);
  });
  if (!storiesTouched) storiesEl.value = recommendedStories(selectedDuration);
}

function fmtClock(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rem = s % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(rem).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function fmtBytes(n) {
  if (!n) return "";
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!isPublicMode && Auth.unauth(res)) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    Auth.toast(typeof d === "string" ? d : `Failed (${res.status})`);
    return null;
  }
  return data;
}

function renderTts(tts, ntfyEnabled) {
  ttsConfigured = !!tts?.configured;
  notifyWrap.hidden = !ntfyEnabled;
  if (ttsConfigured) {
    const enc = tts.ffmpeg ? "MP3" : "WAV";
    ttsStatusEl.textContent = `${tts.detail} · ${enc}`;
    ttsStatusEl.className = "podcast-tts-ok";
    ttsHintEl.hidden = true;
  } else {
    ttsStatusEl.textContent = "TTS not configured";
    ttsStatusEl.className = "podcast-tts-warn";
    ttsHintEl.textContent =
      "Audio synthesis needs NVIDIA MagpieTTS 357M (set NEWS_MAGPIE_URL to your Magpie server, or run scripts/setup_magpie.sh locally). "
      + "You can still generate scripts now with 'Script only'.";
    ttsHintEl.hidden = false;
    scriptOnlyEl.checked = true;
  }
}

function renderTranscriptHtml(rawText) {
  const text = (rawText || "").trim();
  if (!text) return `<div class="muted">(no transcript)</div>`;
  const blocks = text.split(/\n\s*\n/);
  return blocks.map((blk) => {
    const line = blk.trim();
    const hdr = line.match(/^===\s*(.+?)\s*===$/);
    if (hdr) return `<h4 class="pod-tr-ch">${esc(hdr[1])}</h4>`;
    const turn = line.match(/^(Moderation|Co-Moderation|Host\s*1|Host\s*2)\s*:\s*([\s\S]+)$/i);
    if (turn) {
      const who = turn[1];
      const isHost2 = /co-moderation|2$/i.test(who);
      return `<p class="pod-tr-turn ${isHost2 ? "h2" : "h1"}"><span class="pod-tr-who">${esc(who)}</span> ${esc(turn[2].trim())}</p>`;
    }
    return `<p class="pod-tr-p">${esc(line)}</p>`;
  }).join("");
}

function audioUrlFor(id) {
  return isPublicMode ? `/api/podcasts/public/${Number(id)}/audio` : `/api/podcasts/${Number(id)}/audio`;
}

function episodeDetailUrl(id) {
  return isPublicMode ? `/api/podcasts/public/${Number(id)}` : `/api/podcasts/${Number(id)}`;
}

function episodeHtml(ep) {
  const active = ACTIVE.has(ep.status);
  const dur = ep.duration_sec ? fmtClock(ep.duration_sec) : `~${ep.duration_target}m`;
  const totalSec = Number(ep.duration_sec) || 0;
  const chList = Array.isArray(ep.chapters) ? ep.chapters : [];

  const pills = [
    ep.view === "default"
      ? `<span class="pod-pill pub" title="Visible on the public podcast page">Public</span>`
      : `<span class="pod-pill">For you</span>`,
    ep.language ? `<span class="pod-pill">${esc(ep.language.toUpperCase())}</span>` : "",
    ep.style ? `<span class="pod-pill">${esc(ep.style)}</span>` : "",
    ep.format === "dialogue" ? `<span class="pod-pill">2 hosts</span>` : "",
    ep.voice ? `<span class="pod-pill muted">${esc(ep.voice)}</span>` : "",
  ].filter(Boolean).join("");

  const meta = [
    fmtUtc(ep.created_at),
    dur,
    ep.story_count ? `${ep.story_count} stories` : "",
    ep.status === "ready" && !ep.has_audio ? "script only" : "",
    fmtBytes(ep.file_size),
  ].filter(Boolean).join(" · ");

  // Chapter tick markers along the player timeline (positioned via CSSOM in wireEpisodeCard for CSP).
  const ticks = totalSec > 0 && chList.length > 1
    ? chList.slice(1).map((c) => {
        const pct = Math.max(0, Math.min(100, (Number(c.start_sec) / totalSec) * 100));
        return `<span class="pod-tick" data-pct="${pct.toFixed(2)}" title="${esc(c.title)} (${fmtClock(c.start_sec)})"></span>`;
      }).join("")
    : "";

  const chapters = chList.length
    ? `<div class="pod-chapters">${chList.map((c, idx) => `
        <button type="button" class="pod-chapter" data-ch-idx="${idx}" data-seek="${Number(c.start_sec) || 0}" ${ep.has_audio ? "" : "disabled"}>
          <span class="pod-ch-time">${fmtClock(c.start_sec)}</span>
          <span class="pod-ch-title">${esc(c.title)}</span>
        </button>`).join("")}</div>`
    : "";

  const seenStories = new Map();
  for (const c of chList) {
    if (Array.isArray(c.stories) && c.stories.length) {
      for (const s of c.stories) {
        if (s && s.id && !seenStories.has(s.id)) seenStories.set(s.id, s.headline || `Story #${s.id}`);
      }
    } else if (Array.isArray(c.story_ids)) {
      for (const sid of c.story_ids) {
        if (sid && !seenStories.has(sid)) seenStories.set(sid, `Story #${sid}`);
      }
    }
  }
  const storiesBlock = seenStories.size
    ? `<details class="pod-transcript">
        <summary>Source stories (${seenStories.size})</summary>
        <ul class="pod-stories">${[...seenStories.entries()].map(([sid, title]) =>
          `<li><a href="/s/${Number(sid)}" target="_blank" rel="noopener">${esc(title)}</a></li>`).join("")}</ul>
      </details>`
    : "";

  const spd = savedSpeed();
  const aUrl = audioUrlFor(ep.id);
  const playerHtml = ep.has_audio
    ? `<div class="pod-player" data-ep="${Number(ep.id)}">
        <audio class="pod-audio-el" preload="metadata" data-ep="${Number(ep.id)}" src="${esc(aUrl)}"></audio>
        <div class="pod-player-top">
          <button type="button" class="pod-play-btn" aria-label="Play" title="Play / Pause">${ICON_PLAY}</button>
          <div class="pod-player-center">
            <div class="pod-now-row">
              <span class="pod-now-ch">${esc(chList[0]?.title || ep.title)}</span>
              <span class="pod-clock"><span class="pod-cur">00:00</span> / <span class="pod-dur">${fmtClock(totalSec)}</span></span>
            </div>
            <div class="pod-timeline" role="slider" tabindex="0" aria-label="Seek audio" aria-valuemin="0" aria-valuemax="${Math.round(totalSec)}" aria-valuenow="0">
              <div class="pod-track">
                <div class="pod-fill"></div>
                ${ticks}
                <div class="pod-thumb"></div>
              </div>
            </div>
          </div>
        </div>
        <div class="pod-player-controls">
          <button type="button" class="pod-ctl" data-skip="-15" title="Back 15 seconds" aria-label="Back 15 seconds">-15s</button>
          <button type="button" class="pod-ctl" data-skip="30" title="Forward 30 seconds" aria-label="Forward 30 seconds">+30s</button>
          <button type="button" class="pod-ctl pod-speed-btn" title="Playback speed" aria-label="Playback speed">${spd}\u00d7</button>
          <button type="button" class="pod-ctl pod-mute-btn" title="Mute / Unmute" aria-label="Mute">${ICON_VOL}</button>
          <span class="spacer"></span>
          <a class="pod-ctl pod-dl" href="${esc(aUrl)}" download title="Download audio">Download</a>
        </div>
      </div>`
    : "";

  return `<article class="stat-block pod-card st-${esc(ep.status)}" data-id="${Number(ep.id)}">
    <div class="pod-head">
      <div class="pod-head-info">
        <div class="pod-pills">${pills}</div>
        <h3 class="pod-title">${esc(ep.title)}</h3>
        <div class="pod-meta muted">${esc(meta)}</div>
      </div>
      <div class="pod-head-actions">
        ${ep.status !== "ready" ? `<span class="pod-badge st-${esc(ep.status)}">${esc(ep.status)}</span>` : ""}
        ${!isPublicMode && active ? `<button type="button" class="btn" data-stop="${Number(ep.id)}">Stop</button>` : ""}
        ${!isPublicMode ? `<button type="button" class="btn pod-del" data-del="${Number(ep.id)}">Delete</button>` : ""}
      </div>
    </div>
    ${active ? `<div class="pod-progress"><span class="pod-spinner" aria-hidden="true"></span><span>${esc(ep.progress || "Working…")}</span></div>` : ""}
    ${ep.status === "failed" && ep.error ? `<div class="pod-error">${esc(ep.error)}</div>` : ""}
    ${playerHtml}
    ${chapters}
    ${storiesBlock}
    ${ep.status === "ready" ? `
      <details class="pod-transcript" data-ep="${Number(ep.id)}">
        <summary>Transcript</summary>
        <div class="pod-script-rich">Loading…</div>
      </details>` : ""}
  </article>`;
}

function episodeSig(ep) {
  return [
    ep.status, ep.title, ep.has_audio ? 1 : 0, ep.duration_sec || 0,
    ep.story_count || 0, (ep.chapters || []).length, ep.error || "", ep.voice || "",
    isPublicMode ? "pub" : "priv",
  ].join("|");
}

function syncAllSpeeds(rate) {
  localStorage.setItem(SPEED_KEY, String(rate));
  listEl.querySelectorAll("audio.pod-audio-el").forEach((a) => { a.playbackRate = rate; });
  listEl.querySelectorAll(".pod-speed-btn").forEach((b) => { b.textContent = `${rate}\u00d7`; });
}

function wireEpisodeCard(card, ep) {
  const player = card.querySelector(".pod-player");
  const audio = card.querySelector("audio.pod-audio-el");
  const chapters = Array.isArray(ep.chapters) ? ep.chapters : [];

  if (player && audio) {
    const id = Number(audio.dataset.ep);
    const playBtn = player.querySelector(".pod-play-btn");
    const curEl = player.querySelector(".pod-cur");
    const durEl = player.querySelector(".pod-dur");
    const nowChEl = player.querySelector(".pod-now-ch");
    const timeline = player.querySelector(".pod-timeline");
    const fillEl = player.querySelector(".pod-fill");
    const thumbEl = player.querySelector(".pod-thumb");
    const speedBtn = player.querySelector(".pod-speed-btn");
    const muteBtn = player.querySelector(".pod-mute-btn");
    const chBtns = [...card.querySelectorAll(".pod-chapter[data-ch-idx]")];

    audio.playbackRate = savedSpeed();
    player.querySelectorAll(".pod-tick[data-pct]").forEach((t) => {
      t.style.left = `${t.dataset.pct}%`;
    });

    const updateUI = () => {
      const dur = audio.duration || Number(ep.duration_sec) || 0;
      const cur = audio.currentTime || 0;
      const pct = dur > 0 ? Math.max(0, Math.min(100, (cur / dur) * 100)) : 0;
      curEl.textContent = fmtClock(cur);
      if (dur > 0) durEl.textContent = fmtClock(dur);
      fillEl.style.width = `${pct}%`;
      thumbEl.style.left = `${pct}%`;
      timeline.setAttribute("aria-valuemax", String(Math.round(dur)));
      timeline.setAttribute("aria-valuenow", String(Math.round(cur)));

      // Highlight active chapter.
      if (chapters.length) {
        let activeIdx = 0;
        for (let i = 0; i < chapters.length; i++) {
          if (cur >= (Number(chapters[i].start_sec) || 0) - 0.3) activeIdx = i;
        }
        nowChEl.textContent = chapters[activeIdx]?.title || ep.title;
        chBtns.forEach((b, i) => b.classList.toggle("active", i === activeIdx));
      }
    };

    const setPlayIcon = () => {
      const playing = !audio.paused && !audio.ended;
      playBtn.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
      playBtn.setAttribute("aria-label", playing ? "Pause" : "Play");
      player.classList.toggle("playing", playing);
    };

    playBtn.addEventListener("click", () => {
      if (audio.paused) {
        // Pause any other playing episode on the page.
        listEl.querySelectorAll("audio.pod-audio-el").forEach((other) => {
          if (other !== audio && !other.paused) other.pause();
        });
        audio.play().catch(() => {});
      } else {
        audio.pause();
      }
    });

    audio.addEventListener("play", setPlayIcon);
    audio.addEventListener("pause", setPlayIcon);
    audio.addEventListener("ended", () => {
      setPlayIcon();
      localStorage.removeItem(posKey(id));
    });

    audio.addEventListener("loadedmetadata", () => {
      const saved = Number(localStorage.getItem(posKey(id)) || 0);
      if (saved > 2 && (!audio.duration || saved < audio.duration - 5)) {
        audio.currentTime = saved;
      }
      audio.playbackRate = savedSpeed();
      updateUI();
    });

    let lastSave = 0;
    audio.addEventListener("timeupdate", () => {
      updateUI();
      const now = Date.now();
      if (now - lastSave < 2000) return;
      lastSave = now;
      if (audio.currentTime > 2) localStorage.setItem(posKey(id), String(Math.floor(audio.currentTime)));
    });

    const seekByClientX = (clientX) => {
      const rect = timeline.getBoundingClientRect();
      const dur = audio.duration || Number(ep.duration_sec) || 0;
      if (!dur || rect.width <= 0) return;
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      audio.currentTime = ratio * dur;
      updateUI();
    };

    let dragging = false;
    timeline.addEventListener("pointerdown", (e) => {
      dragging = true;
      timeline.setPointerCapture(e.pointerId);
      seekByClientX(e.clientX);
    });
    timeline.addEventListener("pointermove", (e) => {
      if (dragging) seekByClientX(e.clientX);
    });
    timeline.addEventListener("pointerup", () => { dragging = false; });
    timeline.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight") { e.preventDefault(); audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 10); updateUI(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); audio.currentTime = Math.max(0, audio.currentTime - 10); updateUI(); }
    });

    player.querySelectorAll("[data-skip]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const delta = Number(btn.dataset.skip) || 0;
        const dur = audio.duration || Number(ep.duration_sec) || 0;
        audio.currentTime = Math.max(0, Math.min(dur || Infinity, audio.currentTime + delta));
        updateUI();
      });
    });

    speedBtn.addEventListener("click", () => {
      const cur = savedSpeed();
      const next = SPEEDS[(SPEEDS.indexOf(cur) + 1) % SPEEDS.length];
      syncAllSpeeds(next);
    });

    muteBtn.addEventListener("click", () => {
      audio.muted = !audio.muted;
      muteBtn.innerHTML = audio.muted ? ICON_MUTE : ICON_VOL;
      muteBtn.setAttribute("aria-label", audio.muted ? "Unmute" : "Mute");
    });

    chBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        audio.currentTime = Number(btn.dataset.seek) || 0;
        listEl.querySelectorAll("audio.pod-audio-el").forEach((other) => {
          if (other !== audio && !other.paused) other.pause();
        });
        audio.play().catch(() => {});
        updateUI();
      });
    });
  }

  const stopBtn = card.querySelector("button[data-stop]");
  if (stopBtn) {
    stopBtn.addEventListener("click", async () => {
      const id = Number(stopBtn.dataset.stop);
      stopBtn.disabled = true;
      stopBtn.textContent = "Stopping…";
      const res = await api("POST", `/api/podcasts/${id}/stop`);
      if (res) {
        Auth.toast("Stopping podcast generation…");
        load();
      } else {
        stopBtn.disabled = false;
        stopBtn.textContent = "Stop";
      }
    });
  }

  const det = card.querySelector("details.pod-transcript[data-ep]");
  if (det) {
    det.addEventListener("toggle", async () => {
      if (!det.open) return;
      const id = Number(det.dataset.ep);
      const box = det.querySelector(".pod-script-rich");
      if (scriptCache.has(id)) {
        box.innerHTML = renderTranscriptHtml(scriptCache.get(id));
        return;
      }
      const data = await api("GET", episodeDetailUrl(id));
      const txt = data?.episode?.script || "";
      scriptCache.set(id, txt);
      box.innerHTML = renderTranscriptHtml(txt);
    });
  }

  const delBtn = card.querySelector("button[data-del]");
  if (delBtn) {
    delBtn.addEventListener("click", async () => {
      const id = Number(delBtn.dataset.del);
      if (!confirm("Delete this podcast episode?")) return;
      const res = await api("DELETE", `/api/podcasts/${id}`);
      if (res) {
        scriptCache.delete(id);
        localStorage.removeItem(posKey(id));
        Auth.toast("Episode deleted.");
        load();
      }
    });
  }
}

function makeEpisodeCard(ep) {
  const wrap = document.createElement("div");
  wrap.innerHTML = episodeHtml(ep);
  const card = wrap.firstElementChild;
  card.dataset.sig = episodeSig(ep);
  wireEpisodeCard(card, ep);
  return card;
}

function renderEpisodes(episodes) {
  listEl.removeAttribute("aria-busy");
  const ready = episodes.filter((e) => e.status === "ready").length;
  const working = episodes.filter((e) => ACTIVE.has(e.status)).length;
  if (!isPublicMode) {
    genBtn.disabled = working > 0;
    genBtn.textContent = working > 0 ? "Generating…" : "Generate podcast";
    statusEl.textContent = working
      ? `${working} generating · ${ready} ready`
      : `${episodes.length} episode${episodes.length === 1 ? "" : "s"}`;
  } else {
    statusEl.textContent = `${episodes.length} public episode${episodes.length === 1 ? "" : "s"}`;
  }

  if (!episodes.length) {
    listEl.innerHTML = isPublicMode
      ? '<div class="empty">No public podcast episodes published yet.</div>'
      : '<div class="empty">No podcast episodes yet — pick a length above and click Generate.</div>';
    return;
  }

  listEl.querySelectorAll(".empty, .skel-card").forEach((n) => n.remove());
  const existing = new Map([...listEl.querySelectorAll(".pod-card[data-id]")].map((el) => [Number(el.dataset.id), el]));
  const wantedIds = new Set(episodes.map((e) => Number(e.id)));
  for (const [id, el] of existing) {
    if (!wantedIds.has(id)) el.remove();
  }

  episodes.forEach((ep, idx) => {
    const id = Number(ep.id);
    const sig = episodeSig(ep);
    let card = existing.get(id);
    if (!card) {
      card = makeEpisodeCard(ep);
    } else if (card.dataset.sig !== sig) {
      const fresh = makeEpisodeCard(ep);
      card.replaceWith(fresh);
      card = fresh;
    } else {
      const prog = card.querySelector(".pod-progress span:last-child");
      if (prog && ACTIVE.has(ep.status)) prog.textContent = ep.progress || "Working…";
    }
    const atIdx = listEl.children[idx];
    if (atIdx !== card) listEl.insertBefore(card, atIdx || null);
  });
}

function schedulePoll(episodes) {
  clearTimeout(pollTimer);
  if (!isPublicMode && episodes.some((e) => ACTIVE.has(e.status))) {
    pollTimer = setTimeout(load, 3000);
  }
}

async function load() {
  if (isPublicMode) {
    const data = await api("GET", "/api/podcasts/public");
    if (!data) return;
    if (data.feed_path) publicFeedPath = data.feed_path;
    renderEpisodes(data.episodes || []);
    return;
  }
  const data = await api("GET", "/api/podcasts");
  if (!data) return;
  renderTts(data.tts, data.ntfy_enabled);
  const pubOpt = viewEl.querySelector('option[value="default"]');
  if (pubOpt) {
    pubOpt.disabled = data.can_publish_public === false;
    if (pubOpt.disabled && viewEl.value === "default") viewEl.value = "personal";
  }
  if (data.feed_path) {
    feedPath = data.feed_path;
    rssBar.hidden = false;
  }
  if (data.public_feed_path) publicFeedPath = data.public_feed_path;
  const episodes = data.episodes || [];
  renderEpisodes(episodes);
  schedulePoll(episodes);
}

rssCopyBtn.addEventListener("click", () => { if (feedPath) copyLink(feedPath); });
rssPublicCopyBtn.addEventListener("click", () => { if (publicFeedPath) copyLink(publicFeedPath); });
publicRssCopyTop.addEventListener("click", () => { if (publicFeedPath) copyLink(publicFeedPath); });

rssRotateBtn.addEventListener("click", async () => {
  if (!confirm("Generate a new private RSS link? Existing podcast app subscriptions using the old link will stop updating.")) return;
  const data = await api("POST", "/api/podcasts/feed/rotate");
  if (data?.feed_path) {
    feedPath = data.feed_path;
    copyLink(feedPath);
    Auth.toast("RSS link rotated and copied.");
  }
});

durGroup.querySelectorAll("button[data-min]").forEach((btn) => {
  btn.addEventListener("click", () => setDuration(btn.dataset.min));
});

minutesEl.addEventListener("input", () => {
  const v = Number(minutesEl.value);
  if (v >= 1 && v <= 180) setDuration(v, { syncInput: false });
});

minutesEl.addEventListener("change", () => setDuration(minutesEl.value));
storiesEl.addEventListener("input", () => { storiesTouched = true; });

formEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  setDuration(minutesEl.value);
  genBtn.disabled = true;
  const maxStories = Math.max(3, Math.min(50, Number(storiesEl.value) || recommendedStories(selectedDuration)));
  const data = await api("POST", "/api/podcasts/generate", {
    duration_minutes: selectedDuration,
    view: viewEl.value,
    language: langEl.value,
    style: styleEl.value,
    format: formatEl.value,
    max_stories: maxStories,
    notify: notifyEl.checked,
    script_only: scriptOnlyEl.checked,
    fact_check: factCheckEl.checked,
  });
  if (data?.episode) {
    Auth.toast(`Started generating ${selectedDuration}-min podcast…`);
    load();
  } else {
    genBtn.disabled = false;
  }
});

function syncAuthView() {
  isPublicMode = !Auth.state.authenticated;
  publicBanner.hidden = !isPublicMode;
  builderEl.hidden = isPublicMode;
  if (!isPublicMode) setDuration(selectedDuration);
  scriptCache.clear();
  load();
}

Auth.onChange(syncAuthView);
Auth.init().then(syncAuthView);
