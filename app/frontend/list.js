/* Article list (/list): every ingested article with its pipeline timings. */

const listEl = document.getElementById("article-list");
const statusEl = document.getElementById("list-status");
const moreBtn = document.getElementById("load-more");
const sortBtn = document.getElementById("sort-toggle");
const searchEl = document.getElementById("search");
const statusFilterEl = document.getElementById("status-filter");

const PAGE_SIZE = 100; // rows per "Load more"; the API takes a cumulative limit

let order = "newest";
let shown = 0;
let totalCount = 0;
let articles = [];
let workerStatus = {};
let loadingMore = false;

const COLUMNS = [
  ["wait", "Wait", "Queue wait: first seen → pipeline start (ingest of other feeds + articles ahead)"],
  ["embed", "Embed", "Embedding call"],
  ["cluster", "Cluster", "Vector search + LLM event verification"],
  ["harness", "Harness", "Quality/breaking LLM call"],
  ["story", "Story", "Prompt 2: story headline/summary update"],
  ["wall", "Work", "Pipeline work for this article (sum of the stages)"],
  ["latency", "Latency", "First seen → in a story (wait + work)"],
  ["merge", "Merge", "Recall merge that moved this article into another story (verifier + story rewrite)"],
];

function median(values) {
  const v = values.filter((x) => x != null).sort((a, b) => a - b);
  if (!v.length) return null;
  const mid = Math.floor(v.length / 2);
  return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
}

function sourceText(a) {
  const parts = [a.feed, a.author, a.published_date ? fmtShort(a.published_date) : null].filter(Boolean);
  return parts.length ? parts.join(" · ") : "–";
}

function statusText() {
  let text = `showing ${Math.min(shown, articles.length)}/${totalCount} articles`;
  const work = median(articles.map((a) => a.timings.wall));
  const latency = median(articles.map((a) => a.timings.latency));
  if (latency != null) text += ` · median work ${fmtDuration(work)}, latency ${fmtDuration(latency)}`;
  const w = workerStatus;
  if (w.ingest_running) text += " · ingest running";
  else if (w.ingest_last_started) {
    text += ` · last ingest ${timeAgo(w.ingest_last_started)} (${fmtDuration(w.ingest_last_seconds)}, ${w.ingest_last_new} new)`;
  }
  text += w.pipeline_running ? " · pipeline running" : " · pipeline idle";
  if (w.merge_last_finished) {
    text += ` · last merge pass ${timeAgo(w.merge_last_finished)} (${w.merge_last_checked} checked, ${w.merge_last_merged} merged)`;
  }
  if (w.retention_last_run) {
    text += ` · cleanup ${timeAgo(w.retention_last_run)} (${w.retention_last_deleted_articles} articles removed)`;
  }
  return text;
}

function render() {
  const table = document.createElement("table");
  table.className = "feeds";
  table.innerHTML = `<thead><tr>
      <th>First seen</th><th>Title</th><th title="Feed · author · published">Source</th>
      <th title="Body classifier from ingest: ok / thin / broken / empty">Class</th>
      <th title="LLM-assessed article quality, 1-10">Quality</th>
      <th title="assigned = in a story · pending = queued · duplicate = copy from another feed, joins the original's story without AI calls · skipped = broken body or disabled feed · dead = failed too often">Status</th>
      ${COLUMNS.map(([, label, title]) => `<th class="num" title="${esc(title)}">${label}</th>`).join("")}
    </tr></thead>`;
  const body = document.createElement("tbody");
  for (const a of articles) {
    const t = a.timings || {};
    const tr = document.createElement("tr");
    const statusCls = { dead: "dead", pending: "muted", skipped: "muted", duplicate: "muted" }[a.status] || "";
    tr.innerHTML = `
      <td class="muted nowrap" title="first seen ${esc(fmtUtc(a.created_at))} · processed ${esc(fmtUtc(a.processed_at))}">${esc(fmtShort(a.created_at))}</td>
      <td><a href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a></td>
      <td class="muted">${esc(sourceText(a))}</td>
      <td>${esc(a.class || "–")}</td>
      <td>${a.quality ?? "–"}</td>
      <td class="${statusCls}">${esc(a.status)}</td>
      ${COLUMNS.map(([key]) => `<td class="num${key === "latency" ? "" : " muted"}">${fmtDuration(t[key])}</td>`).join("")}`;
    body.appendChild(tr);
  }
  table.appendChild(body);
  listEl.innerHTML = "";
  listEl.appendChild(table);
  statusEl.textContent = statusText();
  statusEl.title = statusEl.textContent;
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
    const params = new URLSearchParams({ limit: String(shown + PAGE_SIZE), order });
    if (searchEl.value.trim()) params.set("q", searchEl.value.trim());
    if (statusFilterEl.value) params.set("status", statusFilterEl.value);
    const res = await fetch(`/api/articles?${params}`);
    if (Auth.unauth(res) || !res.ok) return;
    const data = await res.json();
    articles = data.articles;
    totalCount = data.count;
    workerStatus = data.worker || {};
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
statusFilterEl.addEventListener("change", load);
let searchTimer;
searchEl.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(load, 300);
});

(async function boot() {
  await Auth.init();
  load();
})();
