/* Feed management page — plain fetch + DOM. Helpers from common.js. */

const listEl = document.getElementById("feed-list");
const statusEl = document.getElementById("feed-status");
const addForm = document.getElementById("add-feed");
const searchEl = document.getElementById("feed-search");
const onlyErrorsEl = document.getElementById("only-errors");
let lastData = null;

function avgPerDay(n7) {
  return (n7 / 7).toFixed(1) + "/d";
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
      <th>Enabled</th><th>Feed</th><th>Category</th>
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
    const head = document.createElement("tr");
    head.className = "cat-row";
    head.innerHTML = `<td colspan="3">${esc(cat)}
        <button class="btn cat-toggle" title="Toggle AI ingestion for the whole category">
          ${allOn ? "turn all off" : "turn all on"}
        </button></td>
        <td>${g.last_24h}</td><td>${g.last_7d}</td>
        <td>${avgPerDay(g.last_7d)}</td>
        <td colspan="2">${g.enabled}/${g.feeds} enabled</td>`;
    head.querySelector(".cat-toggle").addEventListener("click", () =>
      setCategory(cat, allOn ? 0 : 1),
    );
    body.appendChild(head);

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
        <td>${f.last_24h}</td>
        <td>${f.last_7d}</td>
        <td>${avgPerDay(f.last_7d)}</td>
        <td>${f.article_total}</td>
        <td class="muted nowrap" title="${esc(fmtUtc(f.last_fetched_at))}">${esc(timeAgo(f.last_fetched_at))}</td>
        <td><button class="btn del" title="Delete feed + its articles">✕</button></td>`;
      tr.querySelector(".toggle").addEventListener("click", () => {
        patch(f.id, { enabled: f.enabled ? 0 : 1 });
      });
      tr.querySelector(".del").addEventListener("click", () => {
        if (confirm(`Delete feed "${f.title}" and its ${f.article_total} articles?`)) {
          del(f.id);
        }
      });
      tr.querySelector(".cat").addEventListener("change", (e) => {
        patch(f.id, { category: e.target.value });
      });
      body.appendChild(tr);
    }
  }
  table.appendChild(body);
  listEl.innerHTML = "";
  listEl.appendChild(table);
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
    Auth.toast(`update failed: ${res.status}`);
  }
}

async function setCategory(cat, enabled) {
  const res = await fetch("/api/feeds/category", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ category: cat === "(uncategorized)" ? null : cat, enabled }),
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

(async function boot() {
  await Auth.init();
  loadFeeds();
})();
