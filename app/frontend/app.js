/* AI News Aggregator — vanilla JS + Web Components. No frameworks. */

const feedEl = document.getElementById("feed");
const statusEl = document.getElementById("status");
const headsEl = document.getElementById("heads-up-text");
const refreshBtn = document.getElementById("refresh");
const moreBtn = document.getElementById("load-more");

function esc(s) {
  return (s || "").toString().replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function qualityClass(q) {
  if (q >= 7) return "q-high";
  if (q >= 4) return "q-mid";
  return "q-low";
}

function fmtDate(s) {
  if (!s) return "";
  const d = new Date(s.replace(" ", "T"));
  return isNaN(d) ? s : d.toLocaleString();
}

// --- <story-card> ---------------------------------------------------------
class StoryCard extends HTMLElement {
  connectedCallback() {
    this.render();
    const head = this.querySelector(".card-head");
    head.addEventListener("click", () => this.classList.toggle("open"));
  }
  set story(data) {
    this._data = data;
    this.render();
  }
  _fb(kind) {
    const d = this._data;
    fetch(`/api/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ story_id: d.id, kind }),
    }).then((res) => {
      if (Auth.unauth(res)) return; // not signed in → toast with Sign in
      this.querySelector(`.fb[data-kind="${kind}"]`)?.classList.add("active");
    });
  }
  render() {
    const d = this._data;
    const m = d.metrics || {};
    const breaking = m.status === "Breaking" && m.urgency_level >= 8;
    const pills = d.tags.map((t) => `<span class="pill">${esc(t)}</span>`).join("");
    const metricsPill =
      `<span class="pill metric">urgency ${m.urgency_level ?? "–"}</span>` +
      `<span class="pill metric">${esc(m.impact_scale ?? "")}</span>` +
      `<span class="pill metric">${esc(m.status ?? "")}</span>`;
    const breakingPill = breaking ? `<span class="pill breaking">BREAKING</span>` : "";
    // Badge shows BOTH the distinct-source count (news outlets) and the
    // attached-article count, so the number above matches the list below
    // (one outlet can contribute several articles -> the two differ).
    const srcCount = d.source_count ?? (d.articles || []).length;
    const artCount = (d.articles || []).length;
    const sourcesPill =
      `<span class="pill sources" title="distinct sources · articles attached">`
      + `${srcCount} ${srcCount === 1 ? "source" : "sources"} · `
      + `${artCount} ${artCount === 1 ? "article" : "articles"}</span>`;
    const rel = d.relevance || {};
    const scoreRow =
      `<span class="rel-val">relevance ${rel.base ?? "–"}</span>` +
      `<span class="rel-break" title="relevance base = quality·0.40 + credibility·0.35 + impact·0.25 (0–10); ` +
      `display = base + urgency + interest − time decay">` +
      `q ${rel.quality ?? "–"} · cred ${rel.credibility ?? "–"} · impact ${rel.user_impact ?? "–"} · int ${rel.user_interest ?? "–"}` +
      `</span>` +
      `<span class="score">display ${d.display_score}</span>`;
    const articles = (d.articles || []).map((a) => {
      const src = a.author || (a.url ? new URL(a.url).hostname : "");
      return `<div class="article">
          <a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a>
          <div class="src">${esc(src)}${a.published_date ? " · " + esc(a.published_date) : ""}</div>
        </div>`;
    }).join("");
    this.innerHTML = `
      <div class="card-head">
        <div class="card-title-row">
          <span class="chev">▶</span>
          <h2 class="headline">${esc(d.headline)}</h2>
          <span class="quality-badge ${qualityClass(d.quality_score)}">quality ${d.quality_score}</span>
        </div>
        <p class="summary">${esc(d.summary)}</p>
        <div class="meta-row">
          ${breakingPill}${sourcesPill}${metricsPill}${pills}
        </div>
        <div class="score-row">${scoreRow}</div>
        <div class="interaction">
          <button class="fb" data-kind="up">👍 helpful</button>
          <button class="fb" data-kind="down">👎 not for me</button>
        </div>
      </div>
      <div class="accordion">${articles}</div>`;

    this.querySelectorAll(".fb").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        this._fb(b.dataset.kind);
      });
    });
  }
}
customElements.define("story-card", StoryCard);

// --- feed rendering (paginated load-more) ----------------------------------
const PAGE = 50;
let allStories = [];
let shown = 0;
let totalCount = 0; // canonical server-side total (data.count), independent of fetch window
let loadingMore = false;

function renderFeed() {
  feedEl.innerHTML = "";
  const slice = allStories.slice(0, shown);
  if (!slice.length) {
    feedEl.innerHTML = `<div class="empty">No stories yet — the worker is still ingesting.</div>`;
  }
  for (const s of slice) {
    const el = document.createElement("story-card");
    el.story = s;
    feedEl.appendChild(el);
  }
  moreBtn.hidden = shown >= totalCount;
  statusEl.textContent = `showing ${Math.min(shown, allStories.length)}/${totalCount} stories`;
}

async function loadStories() {
  shown = 0;
  allStories = [];
  totalCount = 0;
  await loadMore();
}

// Re-fetch the currently visible window without resetting `shown` — used by
// SSE so live updates don't drag the user back to page 1.
async function refreshFeed() {
  const res = await fetch(`/api/stories?limit=${shown}`);
  const data = await res.json();
  allStories = data.stories;
  totalCount = data.count; // always keep the server total as source of truth
  if (shown > data.count) shown = data.count;
  renderFeed();
}

async function loadMore() {
  if (loadingMore) return;
  loadingMore = true;
  moreBtn.disabled = true;
  try {
    const res = await fetch(`/api/stories?limit=${shown + PAGE}`);
    const data = await res.json();
    allStories = data.stories;
    totalCount = data.count;
    shown = Math.min(shown + PAGE, data.count);
    renderFeed();
  } finally {
    loadingMore = false;
    moreBtn.disabled = false;
  }
}

async function loadHeadsUp() {
  try {
    const res = await fetch("/api/heads-up");
    const data = await res.json();
    headsEl.textContent = data.text;
  } catch {
    headsEl.textContent = "(heads-up unavailable)";
  }
}

async function refresh() {
  refreshBtn.disabled = true;
  statusEl.textContent = "refreshing…";
  try {
    const res = await fetch("/api/refresh", { method: "POST" });
    if (Auth.unauth(res)) {
      statusEl.textContent = "sign-in needed";
      return;
    }
    await Promise.all([loadStories(), loadHeadsUp()]);
  } finally {
    refreshBtn.disabled = false;
  }
}
refreshBtn.addEventListener("click", refresh);
moreBtn.addEventListener("click", () => loadMore());

// --- live updates via SSE --------------------------------------------------
let sseRetry = 0;
function connectSSE() {
  const es = new EventSource("/api/events");
  es.onopen = () => { statusEl.textContent = statusEl.textContent + " · live"; };
  es.onerror = () => {
    es.close();
    sseRetry += 1;
    setTimeout(connectSSE, Math.min(30000, 1000 * sseRetry));
  };
  let debounce;
  es.onmessage = (ev) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => refreshFeed(), 500);
  };
}

// --- boot ------------------------------------------------------------------
(async function boot() {
  await Auth.init(); // session state + header sign-in/out before first render
  await loadStories();
  loadHeadsUp();
  connectSSE();
})();
