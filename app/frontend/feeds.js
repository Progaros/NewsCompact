/* Feed management page — plain fetch + DOM. Helpers from common.js. */

const listEl = document.getElementById("feed-list");
const statusEl = document.getElementById("feed-status");
const addForm = document.getElementById("add-feed");
const searchEl = document.getElementById("feed-search");
const onlyErrorsEl = document.getElementById("only-errors");
const COLLAPSED_KEY = "feeds.collapsed";
let lastData = null;
let collapsed = new Set((() => {
  try { return JSON.parse(localStorage.getItem(COLLAPSED_KEY)) || []; } catch { return []; }
})());

function saveCollapsed() {
  localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...collapsed]));
}

function avgPerDay(n7) {
  return (n7 / 7).toFixed(1) + "/d";
}

const fmtTrust = (v) => Number(v).toFixed(2);

// null = empty field (use the category's / default), undefined = invalid.
function parseTrust(input) {
  const raw = input.value.trim();
  if (raw === "") return input.validity.badInput ? undefined : null;
  const v = Number(raw);
  return Number.isFinite(v) && v >= 0 && v <= 1 ? v : undefined;
}

function filtering() {
  return searchEl.value.trim() !== "" || onlyErrorsEl.checked;
}

function visible(f) {
  const q = searchEl.value.trim().toLowerCase();
  if (onlyErrorsEl.checked && !f.last_error) return false;
  return !q || `${f.title} ${f.xml_url} ${f.category || ""}`.toLowerCase().includes(q);
}

async function loadFeeds() {
  const res = await fetch("/api/feeds");
  if (Auth.unauth(res) || !res.ok) return;
  lastData = await res.json();
  renderFeeds(lastData);
}

function renderFeeds(data) {
  const feeds = data.feeds;
  const shownFeeds = feeds.filter(visible);
  const cats = data.categories || {};
  const errors = feeds.filter((f) => f.last_error).length;
  statusEl.textContent = `${feeds.length} feeds · ${feeds.filter((f) => f.enabled).length} enabled`
    + (errors ? ` · ${errors} failing` : "");
  statusEl.title = statusEl.textContent;

  const table = document.createElement("table");
  table.className = "feeds";
  table.innerHTML = `<thead><tr>
      <th>Enabled</th><th>Feed</th><th>Category</th><th title="Source trust for everyone (0–1)">Trust</th>
      <th>24h (enabled)</th><th>7d (enabled)</th><th>avg/day (7d, enabled)</th><th>total</th><th>last fetched</th><th></th>
    </tr></thead>`;
  const body = document.createElement("tbody");

  // Populate the category datalist in the add-feed form.
  const dl = document.getElementById("catlist");
  const existing = [...new Set(feeds.map((f) => f.category).filter(Boolean))].sort();
  dl.innerHTML = existing.map((c) => `<option value="${esc(c)}">`).join("");

  for (const [cat, g] of Object.entries(cats)) {
    const catFeeds = shownFeeds.filter((f) => (f.category || "(uncategorized)") === cat);
    if (!catFeeds.length) continue;
    const allOn = g.enabled === g.feeds;
    const open = !collapsed.has(cat) || filtering();
    const head = document.createElement("tr");
    head.className = "cat-row";
    head.innerHTML = `<td colspan="3">
        <button type="button" class="cat-fold" aria-expanded="${open}" title="${open ? "Collapse" : "Expand"} category">
          <span class="chev" aria-hidden="true"></span>${esc(cat)}</button>
        <span class="cat-count">${g.feeds} feeds</span>
        <button class="btn cat-toggle" title="Toggle AI ingestion for the whole category">
          ${allOn ? "turn all off" : "turn all on"}
        </button></td>
        <td><input class="trust" type="number" min="0" max="1" step="0.05" value="${esc(fmtTrust(g.trust))}"
          title="Trust of this category's feeds that have no value of their own (empty = 1.00)"
          aria-label="Trust of ${esc(cat)}" /></td>
        <td>${g.last_24h}</td><td>${g.last_7d}</td>
        <td>${avgPerDay(g.last_7d)}</td>
        <td colspan="3">${g.enabled}/${g.feeds} enabled</td>`;
    head.querySelector(".cat-fold").addEventListener("click", () => {
      if (!collapsed.delete(cat)) collapsed.add(cat);
      saveCollapsed();
      renderFeeds(lastData);
    });
    head.querySelector(".cat-toggle").addEventListener("click", () =>
      postCategory(cat, { enabled: allOn ? 0 : 1 }),
    );
    head.querySelector(".trust").addEventListener("change", (e) => {
      const trust = parseTrust(e.target);
      if (trust === undefined) return Auth.toast("Trust must be between 0 and 1.");
      postCategory(cat, { trust });
    });
    body.appendChild(head);
    if (!open) continue;

    for (const f of catFeeds) {
      const tr = document.createElement("tr");
      tr.className = f.enabled ? "feed-on" : "feed-off";
      tr.innerHTML = `
        <td><button class="btn toggle" title="${f.enabled ? "Disable AI ingestion" : "Enable AI ingestion"}">${f.enabled ? "on" : "off"}</button></td>
        <td class="feed-name">
          <div class="name">${esc(f.title)}</div>
          <div class="url">${esc(f.xml_url)}</div>
          ${f.last_error ? `<div class="feed-error" title="${esc(f.last_error)}">⚠ fetch failing: ${esc(f.last_error.slice(0, 80))}</div>` : ""}
        </td>
        <td><input class="cat" value="${esc(f.category || "")}" /></td>
        <td><input class="trust" type="number" min="0" max="1" step="0.05"
          value="${f.trust == null ? "" : esc(fmtTrust(f.trust))}" placeholder="${esc(fmtTrust(g.trust))}"
          title="${f.trust == null ? "Uses the category's trust — type a value to set this feed's own" : "Own trust — clear the field to use the category's"}"
          aria-label="Trust of ${esc(f.title)}" /></td>
        <td>${f.last_24h}</td>
        <td>${f.last_7d}</td>
        <td>${avgPerDay(f.last_7d)}</td>
        <td>${f.article_total}</td>
        <td class="muted nowrap" title="${esc(fmtUtc(f.last_fetched_at))}">${esc(timeAgo(f.last_fetched_at))}</td>
        <td class="nowrap"><button class="btn edit" title="Edit name and URLs">✎</button><button class="btn del" title="Delete feed + its articles">✕</button></td>`;
      tr.querySelector(".toggle").addEventListener("click", () => {
        patch(f.id, { enabled: f.enabled ? 0 : 1 });
      });
      tr.querySelector(".edit").addEventListener("click", () => editFeed(tr, f));
      tr.querySelector(".del").addEventListener("click", () => {
        if (confirm(`Delete feed "${f.title}" and its ${f.article_total} articles?`)) {
          del(f.id);
        }
      });
      tr.querySelector(".cat").addEventListener("change", (e) => {
        patch(f.id, { category: e.target.value });
      });
      tr.querySelector(".trust").addEventListener("change", (e) => {
        const trust = parseTrust(e.target);
        if (trust === undefined) return Auth.toast("Trust must be between 0 and 1.");
        patch(f.id, { trust });
      });
      body.appendChild(tr);
    }
  }
  table.appendChild(body);
  listEl.innerHTML = "";
  listEl.appendChild(table);
}

// Inline form for name, feed URL and website URL; only changed fields are sent.
function editFeed(tr, f) {
  const cell = tr.querySelector(".feed-name");
  cell.innerHTML = `<form class="edit-form">
      <input name="title" value="${esc(f.title)}" placeholder="Name" maxlength="300" required aria-label="Name" />
      <input name="xml_url" type="url" value="${esc(f.xml_url)}" placeholder="Feed URL" maxlength="2000" required aria-label="Feed URL" />
      <input name="site_url" type="url" value="${esc(f.site_url || "")}" placeholder="Website URL (optional)" maxlength="2000" aria-label="Website URL" />
      <div class="row"><button class="btn" type="submit">Save</button><button class="btn cancel" type="button">Cancel</button></div>
    </form>`;
  const form = cell.querySelector("form");
  const cancel = () => renderFeeds(lastData);
  form.elements.title.focus();
  form.querySelector(".cancel").addEventListener("click", cancel);
  form.addEventListener("keydown", (e) => { if (e.key === "Escape") cancel(); });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const changes = {};
    for (const key of ["title", "xml_url", "site_url"]) {
      const v = form.elements[key].value.trim();
      if (v !== (f[key] || "")) changes[key] = v;
    }
    if (!Object.keys(changes).length) return cancel();
    patch(f.id, changes);
  });
}

async function patch(id, body) {
  const res = await fetch(`/api/feeds/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (Auth.unauth(res)) return;
  if (res.ok) {
    loadFeeds();
  } else {
    const detail = (await res.json().catch(() => ({}))).detail;
    Auth.toast(`update failed: ${typeof detail === "string" ? detail : res.status}`);
  }
}

async function postCategory(cat, change) {
  const res = await fetch("/api/feeds/category", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ category: cat === "(uncategorized)" ? null : cat, ...change }),
  });
  if (Auth.unauth(res)) return;
  if (res.ok) {
    loadFeeds();
  } else {
    Auth.toast(`category update failed: ${res.status}`);
  }
}

async function del(id) {
  const res = await fetch(`/api/feeds/${id}`, { method: "DELETE" });
  if (Auth.unauth(res)) return;
  if (res.ok) {
    loadFeeds();
  } else {
    Auth.toast(`delete failed: ${res.status}`);
  }
}

const urlInput = addForm.querySelector('[name="xml_url"]');
const titleInput = addForm.querySelector('[name="title"]');

async function previewTitle(url, titleEl) {
  if (!url || titleEl.value.trim()) return;
  try {
    const res = await fetch(`/api/feeds/preview?url=${encodeURIComponent(url)}`);
    if (Auth.unauth(res)) return;
    if (res.ok) {
      const d = await res.json();
      if (d.title) titleEl.value = d.title;
    }
  } catch (e) {
    // network hiccup — leave title empty, submit will retry
  }
}

urlInput.addEventListener("change", () => previewTitle(urlInput.value.trim(), titleInput));

addForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(addForm);
  const xmlUrl = (fd.get("xml_url") || "").trim();
  let title = titleInput.value.trim();
  if (!title) {
    // User typed a URL but no title — try to fetch the feed's <title> server-side.
    await previewTitle(xmlUrl, titleInput);
    title = titleInput.value.trim();
  }
  if (!title) {
    Auth.toast("Could not fetch a title from the URL — please enter one manually.");
    return;
  }
  const res = await fetch("/api/feeds", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title,
      xml_url: xmlUrl,
      category: (fd.get("category") || "").trim() || null,
    }),
  });
  if (Auth.unauth(res)) return;
  if (res.ok) {
    addForm.reset();
    Auth.toast("Feed added — it is fetched with the next ingest run.");
    loadFeeds();
  } else {
    const detail = (await res.json().catch(() => ({}))).detail;
    Auth.toast(`add failed: ${typeof detail === "string" ? detail : res.status}`);
  }
});

document.getElementById("reload").addEventListener("click", loadFeeds);
searchEl.addEventListener("input", () => lastData && renderFeeds(lastData));
onlyErrorsEl.addEventListener("change", () => lastData && renderFeeds(lastData));
document.getElementById("collapse-all").addEventListener("click", () => {
  if (!lastData) return;
  collapsed = new Set(Object.keys(lastData.categories || {}));
  saveCollapsed();
  renderFeeds(lastData);
});
document.getElementById("expand-all").addEventListener("click", () => {
  collapsed.clear();
  saveCollapsed();
  if (lastData) renderFeeds(lastData);
});

(async function boot() {
  await Auth.init();
  loadFeeds();
})();
