/* Compact read-only article list (/list) — plain fetch + DOM. No frameworks. */

const listEl = document.getElementById("article-list");
const statusEl = document.getElementById("list-status");
const moreBtn = document.getElementById("load-more");
const sortBtn = document.getElementById("sort-toggle");

const PAGE_SIZE = 100; // rows per "Load more" click; API takes cumulative limit

function esc(s) {
  return (s || "").toString().replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// created_at is naive-UTC ("YYYY-MM-DD HH:MM:SS") → parse as UTC for display.
function fmtDate(s) {
  if (!s) return "–";
  const d = new Date(s.replace(" ", "T") + "Z");
  return isNaN(d) ? s : d.toLocaleString();
}

// Pipeline stage wall time (seconds) → "1.2s"; null (never run) → "–".
function fmtSec(v) {
  if (v == null) return "–";
  const n = Number(v);
  return isNaN(n) ? "–" : n.toFixed(1) + "s";
}

// Source cell: "site · author · published date" — skips empty parts.
function fmtSource(a) {
  const parts = [a.feed, a.author, a.published_date].filter(Boolean);
  return parts.length ? parts.join(" · ") : "–";
}

let order = "newest";
let shown = 0;
let totalCount = 0; // server total (data.count), source of truth
let articles = [];
let loadingMore = false;

function statusText() {
  return `showing ${Math.min(shown, articles.length)}/${totalCount} articles`;
}

function render() {
  const table = document.createElement("table");
  table.className = "feeds";
  table.innerHTML = `<thead><tr>
      <th>First seen</th><th>Title</th><th title="Article feed · author · published date">Source</th>
      <th title="Deterministic body classifier from ingest: ok / thin / broken / empty">Class</th>
      <th title="LLM-assessed article quality, 1-10">Quality</th>
      <th title="assigned = in a story · pending = queued · dead = failed N times">Status</th>
      <th class="num" title="Embedding (CPU vector, ~1s)">Embed</th>
      <th class="num" title="Clustering: vec0 search + LLM event verification">Cluster</th>
      <th class="num" title="Harness: quality/breaking LLM call">Harness</th>
      <th class="num" title="Prompt2: story headline/summary update">Story</th>
      <th class="num" title="End-to-end pipeline wall time">Total</th>
    </tr></thead>`;
  const body = document.createElement("tbody");
  for (const a of articles) {
    const t = a.timings || {};
    const tr = document.createElement("tr");
    const q = a.quality == null ? "–" : a.quality;
    tr.innerHTML = `
      <td class="muted">${fmtDate(a.created_at)}</td>
      <td><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a></td>
      <td class="muted">${esc(fmtSource(a))}</td>
      <td>${esc(a.class || "–")}</td>
      <td>${q}</td>
      <td class="${a.status === "dead" ? "dead" : a.status === "pending" ? "muted" : ""}">${a.status}</td>
      <td class="num muted">${fmtSec(t.embed)}</td>
      <td class="num muted">${fmtSec(t.cluster)}</td>
      <td class="num muted">${fmtSec(t.harness)}</td>
      <td class="num muted">${fmtSec(t.story)}</td>
      <td class="num muted">${fmtSec(t.wall)}</td>`;
    body.appendChild(tr);
  }
  table.appendChild(body);
  listEl.innerHTML = "";
  listEl.appendChild(table);
  statusEl.textContent = statusText();
  moreBtn.hidden = shown >= totalCount;
}

async function load() {
  shown = 0;
  await loadMore();
}

async function loadMore() {
  if (loadingMore) return;
  loadingMore = true;
  moreBtn.disabled = true;
  try {
    const res = await fetch(`/api/articles?limit=${shown + PAGE_SIZE}&order=${order}`);
    if (Auth.unauth(res)) return;
    const data = await res.json();
    articles = data.articles;
    totalCount = data.count;
    shown = Math.min(shown + PAGE_SIZE, data.count);
    render();
  } finally {
    loadingMore = false;
    moreBtn.disabled = false;
  }
}

sortBtn.addEventListener("click", () => {
  order = order === "newest" ? "oldest" : "newest";
  sortBtn.textContent = `${order} first`;
  load();
});
moreBtn.addEventListener("click", () => loadMore());

(async function boot() {
  await Auth.init();
  load();
})();
