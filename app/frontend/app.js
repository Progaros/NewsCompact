/* Story feed — vanilla JS + Web Components. Helpers from common.js. */

const feedEl = document.getElementById("feed");
const statusEl = document.getElementById("status");
const headsEl = document.getElementById("heads-up-text");
const moreBtn = document.getElementById("load-more");
const searchEl = document.getElementById("search");
const chipsEl = document.getElementById("filter-chips");
const scopeBtns = { active: document.getElementById("scope-active"), archive: document.getElementById("scope-archive") };

const PAGE = 50;
const openIds = new Set(); // expanded cards survive live re-renders
const state = {
  stories: [],
  shown: 0,
  total: 0,
  pending: null,
  lastArticle: null,
  activeDays: null,
  live: false,
  loading: false,
  scope: "active",
  query: "",
  tag: "",
};

function qualityClass(q) {
  if (q >= 7) return "q-high";
  if (q >= 4) return "q-mid";
  return "q-low";
}

// --- <story-card> ---------------------------------------------------------
class StoryCard extends HTMLElement {
  set story(data) {
    this._data = data;
    this.render();
  }

  _feedback(kind) {
    fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ story_id: this._data.id, kind }),
    }).then((res) => {
      if (Auth.unauth(res) || !res.ok) return;
      this.querySelectorAll(".fb[data-kind]").forEach((b) => b.classList.toggle("active", b.dataset.kind === kind));
      Auth.toast(kind === "up" ? "Thanks — more like this." : "Got it — less like this.");
    });
  }

  render() {
    const d = this._data;
    const m = d.metrics || {};
    const breaking = m.status === "Breaking" && m.urgency_level >= 8;
    const articles = d.articles || [];
    const srcCount = d.source_count ?? articles.length;
    const rel = d.relevance || {};
    const location = m.location && !/^global\b/i.test(m.location)
      ? `<span class="pill metric" title="Story location (distance boost)">📍 ${esc(m.location)}</span>` : "";
    const tags = (d.tags || []).map((t) => `<button type="button" class="pill tag" data-tag="${esc(t)}" title="Filter by tag">#${esc(t)}</button>`).join("");
    const articleRows = articles.map((a) => {
      const src = [a.feed || hostname(a.url), a.author].filter(Boolean).join(" · ");
      const when = a.published_date ? ` · ${esc(timeAgo(a.published_date))}` : "";
      return `<div class="article">
          <a href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>
          <div class="src">${esc(src)}${when}</div>
        </div>`;
    }).join("");

    this.classList.toggle("breaking", breaking);
    this.classList.toggle("open", openIds.has(d.id));
    this.innerHTML = `
      <div class="card-head" role="button" tabindex="0" aria-expanded="${this.classList.contains("open")}">
        <div class="card-title-row">
          <h2 class="headline">${esc(d.headline)}</h2>
          <span class="quality-badge ${qualityClass(d.quality_score)}" title="Story quality (median of article quality)">Q ${Number(d.quality_score).toFixed(1)}</span>
        </div>
        <div class="byline">
          ${breaking ? `<span class="pill breaking">BREAKING</span>` : ""}
          <span class="pill sources" title="distinct sources · articles">${srcCount} ${srcCount === 1 ? "source" : "sources"} · ${articles.length} ${articles.length === 1 ? "article" : "articles"}</span>
          <span class="muted" title="${esc(fmtUtc(d.updated_at))}">updated ${esc(timeAgo(d.updated_at))}</span>
        </div>
        <p class="summary">${esc(d.summary)}</p>
        <div class="meta-row">
          <span class="pill metric" title="urgency 1-10">urgency ${m.urgency_level ?? "–"}</span>
          ${m.impact_scale ? `<span class="pill metric">${esc(m.impact_scale)}</span>` : ""}
          ${m.status ? `<span class="pill metric">${esc(m.status)}</span>` : ""}
          ${location}${tags}
        </div>
      </div>
      <div class="accordion">
        <div class="score-row" title="relevance = quality·0.40 + credibility·0.35 + impact·0.25 (0–10); display = relevance + urgency + interest + distance − time decay">
          <span class="rel-val">relevance ${rel.base ?? "–"}</span>
          <span class="rel-break">q ${rel.quality ?? "–"} · cred ${rel.credibility ?? "–"} · impact ${rel.user_impact ?? "–"} · interest ${rel.user_interest_points ?? "–"} · dist ${rel.distance_points ?? "–"}</span>
          <span class="score">display ${d.display_score}</span>
        </div>
        ${articleRows}
      </div>
      <div class="interaction">
        <button type="button" class="fb" data-kind="up" title="More like this">👍</button>
        <button type="button" class="fb" data-kind="down" title="Less like this">👎</button>
        <button type="button" class="fb chat-btn" title="Ask about this story">💬 Ask</button>
      </div>`;

    const head = this.querySelector(".card-head");
    const toggle = () => {
      const open = this.classList.toggle("open");
      if (open) openIds.add(d.id); else openIds.delete(d.id);
      head.setAttribute("aria-expanded", open);
    };
    head.addEventListener("click", (e) => { if (!e.target.closest(".tag")) toggle(); });
    head.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    this.querySelectorAll(".tag").forEach((b) => b.addEventListener("click", () => setTag(b.dataset.tag)));
    this.querySelectorAll(".fb[data-kind]").forEach((b) => b.addEventListener("click", () => this._feedback(b.dataset.kind)));
    this.querySelector(".chat-btn").addEventListener("click", () => Chat.open(d.id));
  }
}
customElements.define("story-card", StoryCard);

// --- feed ------------------------------------------------------------------
function renderFeed() {
  feedEl.replaceChildren();
  const slice = state.stories.slice(0, state.shown);
  if (!slice.length) {
    const msg = state.query || state.tag ? "No stories match the filter."
      : state.scope === "archive" ? "The archive is empty." : "No stories yet — the worker is still ingesting.";
    feedEl.innerHTML = `<div class="empty">${esc(msg)}</div>`;
  }
  for (const s of slice) {
    const el = document.createElement("story-card");
    el.story = s;
    feedEl.appendChild(el);
  }
  moreBtn.hidden = state.shown >= state.total;
  renderChips();
  updateStatus();
}

function renderChips() {
  chipsEl.hidden = !state.tag;
  chipsEl.innerHTML = state.tag
    ? `<span class="pill chip">#${esc(state.tag)} <button type="button" aria-label="Clear tag filter">×</button></span>` : "";
  chipsEl.querySelector("button")?.addEventListener("click", () => setTag(""));
}

function updateStatus() {
  const parts = [`${Math.min(state.shown, state.stories.length)}/${state.total} ${state.scope === "archive" ? "archived" : "stories"}`];
  if (state.pending) parts.push(`${state.pending} queued`);
  if (state.lastArticle) parts.push(`last article ${timeAgo(state.lastArticle)}`);
  if (state.live) parts.push("live");
  statusEl.textContent = parts.join(" · ");
  statusEl.classList.toggle("live", state.live);
}

function storiesUrl(limit) {
  const p = new URLSearchParams({ limit: String(limit) });
  if (state.scope === "archive") p.set("archived", "true");
  if (state.query) p.set("q", state.query);
  if (state.tag) p.set("tag", state.tag);
  return `/api/stories?${p}`;
}

async function fetchStories(limit) {
  const res = await fetch(storiesUrl(limit));
  if (!res.ok) return false;
  const data = await res.json();
  Object.assign(state, {
    stories: data.stories, total: data.count, pending: data.pending,
    lastArticle: data.last_article, activeDays: data.active_days,
  });
  return true;
}

async function loadMore() {
  if (state.loading) return;
  state.loading = true;
  moreBtn.disabled = true;
  try {
    if (await fetchStories(state.shown + PAGE)) {
      state.shown = Math.min(state.shown + PAGE, state.total);
      renderFeed();
    }
  } finally {
    state.loading = false;
    moreBtn.disabled = false;
  }
}

function reload() {
  state.shown = 0;
  return loadMore();
}

// SSE refresh keeps the visible window instead of jumping back to page 1.
async function refreshFeed() {
  const keep = Math.max(state.shown, PAGE);
  if (await fetchStories(keep)) {
    state.shown = Math.min(keep, state.total);
    renderFeed();
  }
}

function setTag(tag) {
  state.tag = tag;
  reload();
}

function setScope(scope) {
  state.scope = scope;
  for (const [k, b] of Object.entries(scopeBtns)) b.classList.toggle("active", k === scope);
  scopeBtns.archive.title = state.activeDays ? `Stories without updates for ${state.activeDays}+ days` : "";
  reload();
}

let searchTimer;
searchEl.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = searchEl.value.trim();
    reload();
  }, 300);
});
scopeBtns.active.addEventListener("click", () => setScope("active"));
scopeBtns.archive.addEventListener("click", () => setScope("archive"));
moreBtn.addEventListener("click", () => loadMore());

async function loadHeadsUp() {
  try {
    const res = await fetch("/api/heads-up");
    headsEl.textContent = (await res.json()).text;
  } catch {
    headsEl.textContent = "(heads-up unavailable)";
  }
}

// --- live updates via SSE --------------------------------------------------
let sseRetry = 0;
function connectSSE() {
  const es = new EventSource("/api/events");
  es.onopen = () => {
    sseRetry = 0;
    state.live = true;
    updateStatus();
  };
  es.onerror = () => {
    state.live = false;
    es.close();
    sseRetry += 1;
    updateStatus();
    setTimeout(connectSSE, Math.min(30000, 1000 * sseRetry));
  };
  let debounce;
  es.onmessage = () => {
    clearTimeout(debounce);
    debounce = setTimeout(refreshFeed, 500);
  };
}

(async function boot() {
  await Auth.init();
  await reload();
  Auth.onChange(() => reload()); // sign-in/out or saved preferences
  loadHeadsUp();
  connectSSE();
})();
