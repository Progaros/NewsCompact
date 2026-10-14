/* Story feed — vanilla JS + Web Components. Helpers from common.js. */

const feedEl = document.getElementById("feed");
const statusEl = document.getElementById("status");
const headsEl = document.getElementById("heads-up-text");
const headsStateEl = document.getElementById("heads-up-state");
let headsTimer;
const moreBtn = document.getElementById("load-more");
const searchEl = document.getElementById("search");
const chipsEl = document.getElementById("filter-chips");
const scopeBtns = { active: document.getElementById("scope-active"), archive: document.getElementById("scope-archive") };
const showReadBtn = document.getElementById("show-read");
const viewBtns = { personal: document.getElementById("view-personal"), default: document.getElementById("view-default") };
const refreshBtn = document.getElementById("refresh-btn");

const PAGE = 50;
const STALE_MS = 10 * 60 * 1000; // offer a refresh once the shown content is this old
// Expanded parts survive live re-renders: details (tags, metrics) and the source list.
const openIds = new Set();
const sourcesOpenIds = new Set();
// Marked read during this page visit: stay visible (faded) until the next reload.
const sessionReadIds = new Set();
const state = {
  stories: [],
  shown: 0,
  total: 0,
  hiddenRead: 0,
  pending: null,
  activeDays: null,
  loadedAt: 0,
  loading: false,
  scope: "active",
  query: "",
  tag: "",
  showRead: localStorage.getItem("showRead") === "1",
  view: localStorage.getItem("view") === "default" ? "default" : "personal",
};

const ICON_CHECK = `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9.5"/><path d="M7.5 12.5l3 3 6-6.5"/></svg>`;
const ICON_UPDATED = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v4.5h-4.5"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/></svg>`;

function utcNowDb() {
  return new Date().toISOString().slice(0, 19).replace("T", " ");
}

function qualityClass(q) {
  if (q >= 7) return "q-high";
  if (q >= 4) return "q-mid";
  return "q-low";
}

function urgencyClass(u) {
  if (u >= 7) return "u-high";
  if (u >= 5) return "u-mid";
  return "";
}

// "tagesschau.de - die erste Adresse für …" → "tagesschau.de"
function shortFeed(name) {
  const s = (name || "").split(/\s+[-–|>:]\s+/)[0].trim();
  return s.length > 22 ? `${s.slice(0, 21)}…` : s;
}

function toggleIn(set, id, el, cls) {
  const open = el.classList.toggle(cls);
  if (open) set.add(id); else set.delete(id);
  return open;
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

  async _toggleRead() {
    const d = this._data;
    const markRead = d.read !== "read";
    const res = await fetch(`/api/stories/${d.id}/read`, { method: markRead ? "POST" : "DELETE" });
    if (Auth.unauth(res) || !res.ok) return;
    if (markRead) {
      Object.assign(d, { read: "read", read_at: utcNowDb(), read_summary: d.summary });
      sessionReadIds.add(d.id);
    } else {
      d.read = null;
    }
    this.render();
  }

  render() {
    const d = this._data;
    const m = d.metrics || {};
    const breaking = m.status === "Breaking" && m.urgency_level >= 8;
    const articles = d.articles || [];
    const srcCount = d.source_count ?? articles.length;
    const rel = d.relevance || {};
    const feeds = [...new Set(articles.map((a) => shortFeed(a.feed || hostname(a.url))).filter(Boolean))];
    const feedPreview = feeds.slice(0, 3).map(esc).join(" · ") + (feeds.length > 3 ? ` +${feeds.length - 3}` : "");
    const location = m.location && !/^global\b/i.test(m.location) ? `<span>📍 ${esc(m.location)}</span>` : "";
    const tags = (d.tags || []).map((t) => `<button type="button" class="tag" data-tag="${esc(t)}" title="Filter by tag">#${esc(t)}</button>`).join("");
    const updated = d.read === "updated";
    const articleRows = articles.map((a) => {
      const src = [a.feed || hostname(a.url), a.author].filter(Boolean).join(" · ");
      const when = a.published_date ? ` · ${esc(timeAgo(a.published_date))}` : "";
      const isNew = updated && d.read_at && (a.created_at || "") > d.read_at;
      return `<li class="article">
          ${isNew ? `<span class="new-badge">new</span>` : ""}<a href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>
          <div class="src">${esc(src)}${when}</div>
        </li>`;
    }).join("");
    const newCount = updated ? articles.filter((a) => (a.created_at || "") > (d.read_at || "")).length : 0;
    const readSummary = updated && d.read_summary && d.read_summary !== d.summary
      ? `<details class="read-summary"><summary>Summary when you read it</summary><p>${esc(d.read_summary)}</p></details>` : "";
    const related = (d.related || []).map((r) =>
      `<li><a href="/s/${Number(r.id)}" target="_blank" rel="noopener">${esc(r.headline)}</a></li>`).join("");
    const readTitle = { read: "Read — click to mark as unread", updated: "Updated since you read it — click to mark as read again" }[d.read]
      || "Mark as read";
    const open = openIds.has(d.id);
    const srcOpen = sourcesOpenIds.has(d.id);

    this.className = [urgencyClass(m.urgency_level), breaking ? "breaking" : "", open ? "open" : "", srcOpen ? "src-open" : "",
      d.read === "read" ? "is-read" : "", updated ? "is-updated" : ""].filter(Boolean).join(" ");
    this.innerHTML = `
      <button type="button" class="read-btn" aria-pressed="${d.read === "read"}" title="${readTitle}" aria-label="${readTitle}">
        ${updated ? ICON_UPDATED : ICON_CHECK}
      </button>
      <div class="card-body" role="button" tabindex="0" aria-expanded="${open}" title="Show tags and details">
        <div class="kicker">
          ${updated ? `<span class="badge-updated" title="Significant update since you marked it read">Updated since you read${newCount ? ` · ${newCount} new` : ""}</span>` : ""}
          ${breaking ? `<span class="badge-breaking">Breaking</span>` : ""}
          ${m.status && !breaking ? `<span>${esc(m.status)}</span>` : ""}
          ${location}
          <span title="Last news ${esc(fmtUtc(d.news_at))}">${esc(timeAgo(d.news_at))}</span>
        </div>
        <h2 class="headline">${esc(d.headline)}</h2>
        <p class="summary">${esc(d.summary)}</p>
      </div>
      <div class="collapse details"><div>
        ${readSummary}
        <div class="tags">${tags || `<span class="muted">no tags</span>`}</div>
        <div class="metrics">
          <span class="metric" title="urgency 1-10">Urgency <b>${m.urgency_level ?? "–"}</b></span>
          ${m.impact_scale ? `<span class="metric">Impact <b>${esc(m.impact_scale)}</b></span>` : ""}
          <span class="metric" title="Story quality (trust-weighted median of article quality)">Quality <b class="${qualityClass(d.quality_score)}">${Number(d.quality_score).toFixed(1)}</b></span>
          <span class="metric" title="relevance = quality·0.40 + credibility·0.35 + impact·0.25 (0–10); display = relevance + urgency + interest + distance − time decay">
            Score <b>${d.display_score}</b>
            <span class="rel-break">rel ${rel.base ?? "–"} · q ${rel.quality ?? "–"} · cred ${rel.credibility ?? "–"} · impact ${rel.user_impact ?? "–"} · interest ${rel.user_interest_points ?? "–"} · dist ${rel.distance_points ?? "–"}</span>
          </span>
        </div>
        ${related ? `<div class="related"><span class="muted">Related stories</span><ul>${related}</ul></div>` : ""}
      </div></div>
      <div class="card-foot">
        <button type="button" class="src-toggle" aria-expanded="${srcOpen}" title="Show all source articles">
          <span class="src-count">${srcCount} ${srcCount === 1 ? "source" : "sources"}</span>
          <span class="src-names">${feedPreview}</span>
          <span class="chev" aria-hidden="true"></span>
        </button>
        <button type="button" class="icon-btn share-btn" title="Copy a public link to this story" aria-label="Share">🔗</button>
        <div class="interaction">
          <button type="button" class="icon-btn fb" data-kind="up" title="More like this" aria-label="More like this">👍</button>
          <button type="button" class="icon-btn fb" data-kind="down" title="Less like this" aria-label="Less like this">👎</button>
          <button type="button" class="icon-btn chat-btn" title="Ask about this story">💬 Ask</button>
        </div>
      </div>
      <div class="collapse sources"><div>
        <ol class="article-list">${articleRows}</ol>
      </div></div>`;

    const body = this.querySelector(".card-body");
    const toggleDetails = () => body.setAttribute("aria-expanded", toggleIn(openIds, d.id, this, "open"));
    // Selecting summary text must not toggle the card.
    body.addEventListener("click", () => { if (!String(window.getSelection())) toggleDetails(); });
    body.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleDetails(); } });
    const srcBtn = this.querySelector(".src-toggle");
    srcBtn.addEventListener("click", () => srcBtn.setAttribute("aria-expanded", toggleIn(sourcesOpenIds, d.id, this, "src-open")));
    this.querySelectorAll(".tag").forEach((b) => b.addEventListener("click", () => setTag(b.dataset.tag)));
    this.querySelectorAll(".fb[data-kind]").forEach((b) => b.addEventListener("click", () => this._feedback(b.dataset.kind)));
    this.querySelector(".chat-btn").addEventListener("click", () => Chat.open(d.id));
    this.querySelector(".share-btn").addEventListener("click", () => copyLink(`/s/${d.id}`));
    this.querySelector(".read-btn").addEventListener("click", () => this._toggleRead());
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
  if (state.hiddenRead) parts.push(`${state.hiddenRead} read hidden`);
  if (state.pending) parts.push(`${state.pending} queued`);
  statusEl.textContent = parts.join(" · ");
  statusEl.title = statusEl.textContent;
}

function storiesUrl(limit) {
  const p = new URLSearchParams({ limit: String(limit) });
  p.set("view", Auth.state.authenticated ? state.view : "default");
  if (state.scope === "archive") p.set("archived", "true");
  if (state.query) p.set("q", state.query);
  if (state.tag) p.set("tag", state.tag);
  if (state.scope === "active" && !state.showRead) {
    p.set("hide_read", "true");
    if (sessionReadIds.size) p.set("keep", [...sessionReadIds].join(","));
  }
  return `/api/stories?${p}`;
}

async function fetchStories(limit) {
  const res = await fetch(storiesUrl(limit));
  if (!res.ok) return false;
  const data = await res.json();
  Object.assign(state, {
    stories: data.stories, total: data.count, pending: data.pending, hiddenRead: data.hidden_read || 0,
    activeDays: data.active_days,
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
  state.loadedAt = Date.now();
  checkStale();
  return loadMore();
}

// Keeps the visible window instead of jumping back to page 1.
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
  showReadBtn.disabled = scope === "archive";
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
function setView(view) {
  state.view = view;
  localStorage.setItem("view", view);
  for (const [k, b] of Object.entries(viewBtns)) {
    b.classList.toggle("active", k === view);
    b.setAttribute("aria-pressed", k === view);
  }
}
for (const [k, b] of Object.entries(viewBtns)) b.addEventListener("click", () => { setView(k); reload(); loadHeadsUp(); });
setView(state.view);
showReadBtn.setAttribute("aria-pressed", state.showRead);
showReadBtn.addEventListener("click", () => {
  state.showRead = !state.showRead;
  localStorage.setItem("showRead", state.showRead ? "1" : "0");
  showReadBtn.setAttribute("aria-pressed", state.showRead);
  reload();
});
moreBtn.addEventListener("click", () => loadMore());

async function loadHeadsUp() {
  clearTimeout(headsTimer);
  const view = Auth.state.authenticated ? state.view : "default";
  try {
    const d = await (await fetch(`/api/heads-up?view=${view}`)).json();
    if ((Auth.state.authenticated ? state.view : "default") !== view) return; // toggled meanwhile
    setHeads(d.text);
    headsStateEl.textContent = d.generating ? "(updating…)" : "";
    headsEl.closest(".heads-up").classList.toggle("updating", d.generating);
    if (d.generating) headsTimer = setTimeout(loadHeadsUp, 3000);
  } catch {
    headsEl.textContent = "(heads-up unavailable)";
    headsStateEl.textContent = "";
  }
}

// A new brief fades in instead of jumping.
function setHeads(text) {
  if (headsEl.textContent === text) return;
  if (!headsEl.dataset.loaded) {
    headsEl.dataset.loaded = "1";
    headsEl.textContent = text;
    return;
  }
  headsEl.classList.add("swap");
  setTimeout(() => {
    headsEl.textContent = text;
    headsEl.classList.remove("swap");
  }, 250);
}

// --- manual refresh once the content is stale ------------------------------
function checkStale() {
  const mins = Math.floor((Date.now() - state.loadedAt) / 60000);
  const stale = state.loadedAt && Date.now() - state.loadedAt >= STALE_MS;
  refreshBtn.hidden = !stale && !refreshBtn.disabled;
  if (stale) refreshBtn.title = `Stories loaded ${mins} min ago — reload stories and the brief`;
}

async function refreshAll() {
  refreshBtn.disabled = true;
  refreshBtn.classList.add("busy");
  try {
    await Promise.all([refreshFeed(), loadHeadsUp()]);
    state.loadedAt = Date.now();
  } finally {
    refreshBtn.disabled = false;
    refreshBtn.classList.remove("busy");
    checkStale();
  }
}

refreshBtn.addEventListener("click", refreshAll);
document.getElementById("share-brief").addEventListener("click", async () => {
  const view = Auth.state.authenticated ? state.view : "default";
  const res = await fetch("/api/share", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: "brief", view }),
  });
  if (Auth.unauth(res)) return;
  if (!res.ok) return Auth.toast("No brief to share yet.");
  copyLink((await res.json()).url);
});
setInterval(checkStale, 30000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) checkStale(); });

(async function boot() {
  await Auth.init();
  await reload();
  Auth.onChange(() => { reload(); loadHeadsUp(); }); // sign-in/out or saved preferences
  loadHeadsUp();
})();
