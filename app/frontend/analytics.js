/* Analytics (/analytics): tabbed view combining
 * 1) Outlets & Bias (/api/outlets)
 * 2) AI Usage (/api/stats)
 * 3) Pipeline & Articles (/api/articles) */

const statusEl = document.getElementById("analytics-status");
const tabsEl = document.getElementById("analytics-tabs");
const panes = [...document.querySelectorAll(".page-pane")];

const VALID_TABS = new Set(["outlets", "usage", "articles"]);
const loaded = { outlets: false, usage: false, articles: false };
let activeTab = "outlets";

async function api(method, url) {
  const res = await fetch(url, { method });
  if (Auth.unauth(res)) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    Auth.toast(typeof d === "string" ? d : `Failed (${res.status})`);
    return null;
  }
  return data;
}

function initialTab() {
  const params = new URLSearchParams(location.search);
  const hash = location.hash.replace(/^#/, "");
  if (VALID_TABS.has(hash)) return hash;
  const qtab = params.get("tab");
  if (VALID_TABS.has(qtab)) return qtab;
  if (params.get("outlet")) return "outlets";
  return "outlets";
}

function switchTab(id, { pushHash = true } = {}) {
  if (!VALID_TABS.has(id)) id = "outlets";
  activeTab = id;
  tabsEl.querySelectorAll('[role="tab"]').forEach((t) => {
    const on = t.dataset.tab === id;
    t.setAttribute("aria-selected", on);
    t.tabIndex = on ? 0 : -1;
  });
  panes.forEach((p) => { p.hidden = p.dataset.pane !== id; });
  if (pushHash && location.hash !== `#${id}`) {
    history.replaceState(null, "", `${location.pathname}${location.search}#${id}`);
  }
  if (id === "outlets" && !loaded.outlets) loadOutlets();
  if (id === "usage" && !loaded.usage) loadUsage();
  if (id === "articles" && !loaded.articles) loadArticles();
}

tabsEl.querySelectorAll('[role="tab"]').forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  btn.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const all = [...tabsEl.querySelectorAll('[role="tab"]')];
    const next = all[(all.indexOf(btn) + (e.key === "ArrowRight" ? 1 : -1) + all.length) % all.length];
    switchTab(next.dataset.tab);
    next.focus();
  });
});

window.addEventListener("hashchange", () => {
  const h = location.hash.replace(/^#/, "");
  if (VALID_TABS.has(h) && h !== activeTab) switchTab(h, { pushHash: false });
});

// ==================== 1. Outlets & Bias ====================
const budgetEl = document.getElementById("budget-pill");
const outletDaysEl = document.getElementById("outlet-days");
const sampleBtn = document.getElementById("sample-btn");
const compassBox = document.getElementById("compass-box");
const detailEl = document.getElementById("outlet-detail");
const outletSearchEl = document.getElementById("outlet-search");
const outletSortEl = document.getElementById("outlet-sort");
const outletsBodyEl = document.getElementById("outlets-body");

let allOutlets = [];
let selectedOutlet = new URLSearchParams(location.search).get("outlet") || "";

function fmtAxis(v) {
  if (v == null) return "–";
  return `${v > 0 ? "+" : ""}${Number(v).toFixed(2)}`;
}

function renderCompass(outlets) {
  const pts = outlets.filter((o) => o.economic_axis != null && o.social_axis != null);
  const NS = "http://www.w3.org/2000/svg";
  const W = 380, H = 320, PAD = 38;
  const cx = W / 2, cy = H / 2;
  const rx = (W - PAD * 2) / 2, ry = (H - PAD * 2) / 2;

  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "compass-svg");

  const el = (tag, attrs, text) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    if (text != null) n.textContent = text;
    return n;
  };

  svg.appendChild(el("rect", { x: PAD, y: PAD, width: rx * 2, height: ry * 2, class: "compass-bg", rx: 8 }));
  svg.appendChild(el("line", { x1: PAD, y1: cy, x2: W - PAD, y2: cy, class: "compass-axis" }));
  svg.appendChild(el("line", { x1: cx, y1: PAD, x2: cx, y2: H - PAD, class: "compass-axis" }));

  svg.appendChild(el("text", { x: cx, y: 20, "text-anchor": "middle", class: "compass-lbl" }, "Conservative / Authoritarian (+1)"));
  svg.appendChild(el("text", { x: cx, y: H - 10, "text-anchor": "middle", class: "compass-lbl" }, "Progressive / Libertarian (-1)"));
  svg.appendChild(el("text", { x: PAD, y: cy - 6, "text-anchor": "start", class: "compass-lbl" }, "Left (-1)"));
  svg.appendChild(el("text", { x: W - PAD, y: cy - 6, "text-anchor": "end", class: "compass-lbl" }, "Market (+1)"));

  if (!pts.length) {
    svg.appendChild(el("text", { x: cx, y: cy + 4, "text-anchor": "middle", class: "compass-empty" },
      "No political samples yet — wait for background sampling or click Sample."));
  } else {
    for (const o of pts) {
      const x = cx + o.economic_axis * rx;
      const y = cy - o.social_axis * ry;
      const r = Math.min(10, 4.5 + Math.sqrt(o.political_n || 1));
      const lowN = (o.political_n || 0) < 3;
      const g = el("g", { class: `compass-pt${o.outlet === selectedOutlet ? " active" : ""}${lowN ? " low-n" : ""}` });
      g.appendChild(el("title", {}, `${o.outlet} (n=${o.political_n} political${lowN ? ", low sample" : ""}): econ ${fmtAxis(o.economic_axis)}, social ${fmtAxis(o.social_axis)}`));
      g.appendChild(el("circle", { cx: x.toFixed(1), cy: y.toFixed(1), r: r.toFixed(1) }));
      const short = o.outlet.replace(/\.(de|com|org|net|info|ch|at|uk)$/i, "");
      const flipLeft = x > W - PAD - 68;
      g.appendChild(el("text", {
        x: (flipLeft ? x - r - 3 : x + r + 3).toFixed(1),
        y: (y + 3).toFixed(1),
        "text-anchor": flipLeft ? "end" : "start",
        class: "compass-pt-lbl",
      }, short.slice(0, 14)));
      g.addEventListener("click", () => selectOutlet(o.outlet));
      svg.appendChild(g);
    }
  }
  compassBox.replaceChildren(svg);
}

function renderOutletsTable() {
  const q = outletSearchEl.value.trim().toLowerCase();
  const sort = outletSortEl.value;
  const rows = allOutlets.filter((o) =>
    !q || o.outlet.toLowerCase().includes(q)
      || (o.feeds || []).some((f) => f.toLowerCase().includes(q))
      || (o.top_tags || []).some((t) => t.toLowerCase().includes(q))
  );
  rows.sort((a, b) => {
    if (sort === "quality") return (b.avg_quality ?? -1) - (a.avg_quality ?? -1);
    if (sort === "first") return (b.first_pct ?? -1) - (a.first_pct ?? -1);
    if (sort === "analyzed") return (b.analyzed || 0) - (a.analyzed || 0);
    if (sort === "clickbait") return (b.avg_clickbait ?? -1) - (a.avg_clickbait ?? -1);
    return (b.articles || 0) - (a.articles || 0);
  });
  if (!rows.length) {
    outletsBodyEl.innerHTML = '<tr><td colspan="9" class="muted">No outlets match.</td></tr>';
    return;
  }
  outletsBodyEl.innerHTML = rows.map((o) => {
    const fav = (o.favored || []).slice(0, 2).map((e) => `<span class="ent-pos">+${esc(e.name)}</span>`).join(" ");
    const crit = (o.criticized || []).slice(0, 2).map((e) => `<span class="ent-neg">-${esc(e.name)}</span>`).join(" ");
    const compass = o.economic_axis != null
      ? `${fmtAxis(o.economic_axis)} / ${fmtAxis(o.social_axis)} <span class="muted">(n=${o.political_n})</span>`
      : '<span class="muted">–</span>';
    return `<tr class="outlet-row${o.outlet === selectedOutlet ? " selected" : ""}" data-outlet="${esc(o.outlet)}">
      <td><b>${esc(o.outlet)}</b>${o.feeds.length > 1 ? ` <span class="muted">(${o.feeds.length} feeds)</span>` : ""}</td>
      <td class="num">${o.articles}</td>
      <td class="num">${o.fulltext_pct}%</td>
      <td class="num">${o.avg_quality != null ? o.avg_quality.toFixed(1) : "–"}</td>
      <td class="num">${o.first_pct != null ? `${o.first_pct}%` : "–"}</td>
      <td class="num">${o.analyzed}</td>
      <td class="num">${o.avg_clickbait != null ? `${o.avg_clickbait}/10` : "–"}</td>
      <td class="nowrap">${compass}</td>
      <td>${fav} ${crit}</td>
    </tr>`;
  }).join("");
  outletsBodyEl.querySelectorAll("tr[data-outlet]").forEach((tr) => {
    tr.addEventListener("click", () => selectOutlet(tr.dataset.outlet));
  });
}

async function selectOutlet(outlet) {
  selectedOutlet = outlet;
  const u = new URL(location.href);
  u.searchParams.set("outlet", outlet);
  u.hash = "#outlets";
  history.replaceState(null, "", u);
  renderCompass(allOutlets);
  renderOutletsTable();

  detailEl.innerHTML = '<div class="muted">Loading outlet details…</div>';
  const data = await api("GET", `/api/outlets/${encodeURIComponent(outlet)}?days=${outletDaysEl.value}`);
  if (!data) {
    detailEl.innerHTML = '<div class="muted">Outlet details unavailable.</div>';
    return;
  }
  const s = data.summary;
  const tones = Object.entries(s.tones || {}).sort((a, b) => b[1] - a[1])
    .map(([t, n]) => `${t} (${n})`).join(" · ") || "no samples yet";
  const favHtml = (s.favored || []).length
    ? s.favored.map((e) => `<span class="weight-chip"><span class="w-tag ent-pos">${esc(e.name)}</span><span class="muted">×${e.count}</span></span>`).join("")
    : '<span class="muted">None yet</span>';
  const critHtml = (s.criticized || []).length
    ? s.criticized.map((e) => `<span class="weight-chip"><span class="w-tag ent-neg">${esc(e.name)}</span><span class="muted">×${e.count}</span></span>`).join("")
    : '<span class="muted">None yet</span>';
  const tagsHtml = (s.top_tags || []).length
    ? s.top_tags.map((t) => `<a class="tag" href="/?tag=${encodeURIComponent(t)}">#${esc(t)}</a>`).join(" ")
    : '<span class="muted">–</span>';
  const blindHtml = (data.blind_spots || []).length
    ? `<ul class="pod-stories">${data.blind_spots.map((b) =>
        `<li><a href="/s/${Number(b.id)}" target="_blank" rel="noopener">${esc(b.headline)}</a> <span class="muted">(${b.outlets} other outlets)</span></li>`
      ).join("")}</ul>`
    : '<div class="muted">No major blind spots in this window.</div>';
  const samplesHtml = (data.samples || []).length
    ? `<details class="pod-transcript"><summary>Analyzed articles (${data.samples.length})</summary>
        <div class="outlet-samples">${data.samples.map((a) => `
          <div class="outlet-sample">
            <div><a href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener">${esc(a.title)}</a></div>
            <div class="muted">
              ${esc(a.tone)} · clickbait ${a.clickbait}/10
              ${a.political ? `· econ ${fmtAxis(a.economic_axis)} · soc ${fmtAxis(a.social_axis)}` : "· non-political"}
              ${a.favored.length ? `· <span class="ent-pos">+${esc(a.favored.join(", "))}</span>` : ""}
              ${a.criticized.length ? `· <span class="ent-neg">-${esc(a.criticized.join(", "))}</span>` : ""}
            </div>
          </div>`).join("")}</div></details>`
    : "";

  detailEl.innerHTML = `
    <div class="outlet-detail-head">
      <h3>${esc(s.outlet)}</h3>
      <span class="muted">${s.articles} articles · ${s.stories} stories · ${s.analyzed} LLM samples</span>
    </div>
    <div class="outlet-kpis">
      <div><span class="muted">Quality</span><b>${s.avg_quality != null ? `${s.avg_quality.toFixed(1)}/10` : "–"}</b></div>
      <div><span class="muted">Full-text</span><b>${s.fulltext_pct}%</b></div>
      <div><span class="muted">First-to-report</span><b>${s.first_pct != null ? `${s.first_pct}%` : "–"}</b></div>
      <div><span class="muted">Exclusive</span><b>${s.exclusive_pct != null ? `${s.exclusive_pct}%` : "–"}</b></div>
      <div><span class="muted">Duplicates</span><b>${s.duplicate_pct}%</b></div>
      <div><span class="muted">Clickbait</span><b>${s.avg_clickbait != null ? `${s.avg_clickbait}/10` : "–"}</b></div>
    </div>
    <div class="outlet-sub"><b>Tone:</b> <span class="muted">${esc(tones)}</span></div>
    <div class="outlet-sub"><b>Top topics:</b> ${tagsHtml}</div>
    <div class="entity-grid">
      <div><div class="podcast-label">Favored / strengthened</div><div class="weight-chips">${favHtml}</div></div>
      <div><div class="podcast-label">Criticized</div><div class="weight-chips">${critHtml}</div></div>
    </div>
    <details class="pod-transcript">
      <summary>Blind spots — major stories not covered (${(data.blind_spots || []).length})</summary>
      ${blindHtml}
    </details>
    ${samplesHtml}`;
}

async function loadOutlets() {
  const data = await api("GET", `/api/outlets?days=${outletDaysEl.value}`);
  if (!data) return;
  loaded.outlets = true;
  allOutlets = data.outlets || [];
  const b = data.budget || {};
  const pct = ((b.share || 0) * 100).toFixed(1);
  const capPart = b.max_share > 0 ? ` / ${(b.max_share * 100).toFixed(0)}% cap` : "";
  budgetEl.textContent = `LLM share (7d): ${pct}%${capPart} · ${b.total_analyzed || 0} samples`;
  if (activeTab === "outlets") statusEl.textContent = `${allOutlets.length} outlets · ${data.days}d`;
  renderCompass(allOutlets);
  renderOutletsTable();
  if (selectedOutlet && allOutlets.some((o) => o.outlet === selectedOutlet)) {
    selectOutlet(selectedOutlet);
  }
}

outletDaysEl.addEventListener("change", loadOutlets);
outletSearchEl.addEventListener("input", renderOutletsTable);
outletSortEl.addEventListener("change", renderOutletsTable);
sampleBtn.addEventListener("click", async () => {
  sampleBtn.disabled = true;
  sampleBtn.textContent = "Sampling…";
  const res = await api("POST", "/api/outlets/sample?count=5");
  sampleBtn.disabled = false;
  sampleBtn.textContent = "Sample 5 articles";
  if (res) {
    Auth.toast(`Analyzed ${res.analyzed} article(s).`);
    loadOutlets();
  }
});

// ==================== 2. AI Usage ====================
const usageDaysEl = document.getElementById("usage-days");
const ROLE_LABEL = {
  harness: "Article quality",
  verify: "Event verification",
  story: "Story generation",
  chat: "Chat answers",
  brief: "News briefs",
  podcast: "Podcasts",
  analysis: "Media analysis",
  embedding: "Embeddings",
};

function fmtTok(n) {
  n = Number(n) || 0;
  if (n < 1e3) return String(n);
  if (n < 1e6) return (n / 1e3).toFixed(n < 1e4 ? 1 : 0) + "k";
  if (n < 1e9) return (n / 1e6).toFixed(1) + "M";
  return (n / 1e9).toFixed(1) + "B";
}

function renderUsage(data) {
  const llm = data.llm || {};
  const emb = data.embedding || {};
  const days = data.days || 30;
  const llmIn = llm.in_tokens || 0;
  const llmOut = llm.out_tokens || 0;
  const embTok = (emb.in_tokens || 0) + (emb.out_tokens || 0);

  document.getElementById("llm-label").textContent = `LLM tokens (${days}d)`;
  document.getElementById("embed-label").textContent = `Embedding tokens (${days}d)`;
  document.getElementById("llm-total").textContent = fmtTok(llmIn + llmOut);
  document.getElementById("llm-sub").textContent =
    `${fmtTok(llmIn)} in · ${fmtTok(llmOut)} out · ${Number(llm.calls || 0).toLocaleString()} calls`;
  document.getElementById("embed-total").textContent = fmtTok(embTok);
  document.getElementById("embed-sub").textContent = `${Number(emb.calls || 0).toLocaleString()} embed calls`;
  if (activeTab === "usage") statusEl.textContent = `${days}-day window · ${new Date().toISOString().slice(0, 10)}`;

  // Chart
  const chartEl = document.getElementById("stat-chart");
  const series = data.series || [];
  if (!series.length || series.every((d) => !d.tokens)) {
    chartEl.innerHTML = '<div class="empty">No usage recorded yet.</div>';
  } else {
    const max = Math.max(...series.map((d) => d.tokens), 1);
    const step = series.length <= 10 ? 1 : series.length <= 35 ? 5 : 14;
    const bars = document.createElement("div");
    bars.className = "bars";
    series.forEach((d, i) => {
      const col = document.createElement("div");
      col.className = "bar-col";
      col.title = `${d.day} · ${fmtTok(d.tokens)} tokens (${fmtTok(d.in_tokens)} in / ${fmtTok(d.out_tokens)} out)`;
      const bar = document.createElement("div");
      bar.className = "bar";
      if (d.tokens > 0) {
        bar.style.height = `${Math.max(2, (d.tokens / max) * 100)}%`;
        if (d.embed_tokens > 0) {
          const embSpan = document.createElement("span");
          embSpan.className = "bar-embed";
          embSpan.style.height = `${(d.embed_tokens / d.tokens) * 100}%`;
          bar.appendChild(embSpan);
        }
      } else {
        bar.classList.add("bar-zero");
      }
      const label = document.createElement("div");
      label.className = "bar-label";
      const showLabel = i === 0 || i === series.length - 1 || (i % step === 0 && series.length - 1 - i >= Math.ceil(step / 2));
      label.textContent = showLabel ? (d.day ? d.day.slice(5) : "") : "\u00a0";
      col.append(bar, label);
      bars.appendChild(col);
    });
    chartEl.replaceChildren(bars);
  }

  // Roles table
  const roleBody = document.getElementById("role-body");
  const roleRows = Object.entries(data.roles || {})
    .sort((a, b) => (b[1].in_tokens + b[1].out_tokens) - (a[1].in_tokens + a[1].out_tokens));
  if (!roleRows.length) {
    roleBody.innerHTML = '<tr><td colspan="6" class="muted">No usage recorded yet.</td></tr>';
  } else {
    const llmGrand = roleRows.filter(([r]) => r !== "embedding")
      .reduce((acc, [, v]) => acc + v.in_tokens + v.out_tokens, 0);
    roleBody.innerHTML = roleRows.map(([role, v]) => {
      const tot = v.in_tokens + v.out_tokens;
      const share = role === "embedding" ? "–" : (llmGrand > 0 ? `${((tot / llmGrand) * 100).toFixed(1)}%` : "0%");
      return `<tr>
        <td>${esc(ROLE_LABEL[role] || role)}</td>
        <td class="num">${Number(v.calls).toLocaleString()}</td>
        <td class="num">${fmtTok(v.in_tokens)}</td>
        <td class="num">${fmtTok(v.out_tokens)}</td>
        <td class="num"><b>${fmtTok(tot)}</b></td>
        <td class="num muted">${share}</td>
      </tr>`;
    }).join("");
  }

  // Models table
  const modelBody = document.getElementById("model-body");
  const models = data.models || [];
  if (!models.length) {
    modelBody.innerHTML = '<tr><td colspan="4" class="muted">No usage recorded yet.</td></tr>';
  } else {
    modelBody.innerHTML = models.map((m) => {
      const rNames = (m.roles || []).map((r) => ROLE_LABEL[r] || r).join(", ");
      const tot = (m.in_tokens || 0) + (m.out_tokens || 0);
      return `<tr>
        <td><b>${esc(m.model)}</b></td>
        <td class="muted">${esc(rNames)}</td>
        <td class="num">${Number(m.calls || 0).toLocaleString()}</td>
        <td class="num"><b>${fmtTok(tot)}</b></td>
      </tr>`;
    }).join("");
  }
}

async function loadUsage() {
  const data = await api("GET", `/api/stats?days=${usageDaysEl.value}`);
  if (!data) return;
  loaded.usage = true;
  renderUsage(data);
}

usageDaysEl.addEventListener("change", loadUsage);

// ==================== 3. Pipeline & Articles ====================
const artListEl = document.getElementById("article-list");
const artMoreBtn = document.getElementById("art-load-more");
const artSortBtn = document.getElementById("art-sort-toggle");
const artSearchEl = document.getElementById("art-search");
const artStatusEl = document.getElementById("art-status-filter");

const ART_PAGE_SIZE = 100;
const ART_COLS = [
  ["wait", "Wait", "Queue wait: first seen → pipeline start"],
  ["embed", "Embed", "Embedding call"],
  ["cluster", "Cluster", "Vector search + LLM event verification"],
  ["harness", "Harness", "Quality/breaking LLM call"],
  ["story", "Story", "Prompt 2: story headline/summary update"],
  ["wall", "Work", "Pipeline work for this article"],
  ["latency", "Latency", "First seen → in a story (wait + work)"],
  ["merge", "Merge", "Recall merge timing"],
];
let artOrder = "newest", artShown = 0, artTotal = 0, artRows = [], artWorker = {}, artLoading = false;

function median(values) {
  const v = values.filter((x) => x != null).sort((a, b) => a - b);
  if (!v.length) return null;
  const mid = Math.floor(v.length / 2);
  return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
}

function renderArticles() {
  const table = document.createElement("table");
  table.className = "feeds";
  table.innerHTML = `<thead><tr>
      <th>First seen</th><th>Title</th><th>Source</th><th>Class</th><th>Quality</th><th>Status</th>
      ${ART_COLS.map(([, label, title]) => `<th class="num" title="${esc(title)}">${label}</th>`).join("")}
    </tr></thead>`;
  const body = document.createElement("tbody");
  for (const a of artRows) {
    const t = a.timings || {};
    const tr = document.createElement("tr");
    const statusCls = { dead: "dead", pending: "muted", skipped: "muted", duplicate: "muted" }[a.status] || "";
    const src = [a.feed, a.author, a.published_date ? fmtShort(a.published_date) : null].filter(Boolean).join(" · ") || "–";
    tr.innerHTML = `
      <td class="muted nowrap" title="first seen ${esc(fmtUtc(a.created_at))} · processed ${esc(fmtUtc(a.processed_at))}">${esc(fmtShort(a.created_at))}</td>
      <td><a href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a></td>
      <td class="muted">${esc(src)}</td>
      <td>${esc(a.class || "–")}</td>
      <td>${a.quality ?? "–"}</td>
      <td class="${statusCls}">${esc(a.status)}</td>
      ${ART_COLS.map(([key]) => `<td class="num${key === "latency" ? "" : " muted"}">${fmtDuration(t[key])}</td>`).join("")}`;
    body.appendChild(tr);
  }
  table.appendChild(body);
  artListEl.replaceChildren(table);
  artMoreBtn.hidden = artShown >= artTotal;

  if (activeTab === "articles") {
    const work = median(artRows.map((a) => a.timings?.wall));
    const lat = median(artRows.map((a) => a.timings?.latency));
    let txt = `showing ${Math.min(artShown, artRows.length)}/${artTotal} articles`;
    if (lat != null) txt += ` · median work ${fmtDuration(work)}, latency ${fmtDuration(lat)}`;
    if (artWorker.pipeline_running) txt += " · pipeline running";
    statusEl.textContent = txt;
  }
}

async function loadArticles() {
  artShown = 0;
  await loadMoreArticles();
}

async function loadMoreArticles() {
  if (artLoading) return;
  artLoading = true;
  artMoreBtn.disabled = true;
  try {
    const params = new URLSearchParams({ limit: String(artShown + ART_PAGE_SIZE), order: artOrder });
    if (artSearchEl.value.trim()) params.set("q", artSearchEl.value.trim());
    if (artStatusEl.value) params.set("status", artStatusEl.value);
    const data = await api("GET", `/api/articles?${params}`);
    if (!data) return;
    loaded.articles = true;
    artRows = data.articles || [];
    artTotal = data.count || 0;
    artWorker = data.worker || {};
    artShown = Math.min(artShown + ART_PAGE_SIZE, artTotal);
    renderArticles();
  } finally {
    artLoading = false;
    artMoreBtn.disabled = false;
  }
}

artSortBtn.addEventListener("click", () => {
  artOrder = artOrder === "newest" ? "oldest" : "newest";
  artSortBtn.textContent = `${artOrder} first`;
  loadArticles();
});
artMoreBtn.addEventListener("click", loadMoreArticles);
artStatusEl.addEventListener("change", loadArticles);
let artSearchTimer;
artSearchEl.addEventListener("input", () => {
  clearTimeout(artSearchTimer);
  artSearchTimer = setTimeout(loadArticles, 300);
});

(async function boot() {
  await Auth.init();
  if (!Auth.state.authenticated) {
    location.href = "/";
    return;
  }
  if (Auth.state.user?.is_admin) sampleBtn.hidden = false;
  switchTab(initialTab(), { pushHash: false });
})();
