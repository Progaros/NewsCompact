/* Feed management page — plain fetch + DOM. No frameworks. */

const listEl = document.getElementById("feed-list");
const statusEl = document.getElementById("feed-status");
const addForm = document.getElementById("add-feed");

function esc(s) {
  return (s || "").toString().replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtDate(s) {
  if (!s) return "–";
  const d = new Date(s.replace(" ", "T"));
  return isNaN(d) ? s : d.toLocaleString();
}

function avgPerDay(n7) {
  return (n7 / 7).toFixed(1) + "/d";
}

async function loadFeeds() {
  const res = await fetch("/api/feeds");
  if (Auth.unauth(res)) return;
  const data = await res.json();
  renderFeeds(data);
}

function renderFeeds(data) {
  const feeds = data.feeds;
  const cats = data.categories || {};
  statusEl.textContent = `${feeds.length} feeds · ${feeds.filter((f) => f.enabled).length} enabled`;

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

    for (const f of feeds) {
      if ((f.category || "(uncategorized)") !== cat) continue;
      const tr = document.createElement("tr");
      tr.className = f.enabled ? "feed-on" : "feed-off";
      tr.innerHTML = `
        <td><button class="btn toggle" title="${f.enabled ? "Disable AI ingestion" : "Enable AI ingestion"}">${f.enabled ? "on" : "off"}</button></td>
        <td class="feed-name">
          <div class="name">${esc(f.title)}</div>
          <div class="url">${esc(f.xml_url)}</div>
        </td>
        <td><input class="cat" value="${esc(f.category || "")}" /></td>
        <td>${f.last_24h}</td>
        <td>${f.last_7d}</td>
        <td>${avgPerDay(f.last_7d)}</td>
        <td>${f.article_total}</td>
        <td class="muted">${fmtDate(f.last_fetched_at)}</td>
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
    alert(`update failed: ${res.status}`);
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
    alert(`category update failed: ${res.status}`);
  }
}

async function del(id) {
  const res = await fetch(`/api/feeds/${id}`, { method: "DELETE" });
  if (Auth.unauth(res)) return;
  if (res.ok) {
    loadFeeds();
  } else {
    alert(`delete failed: ${res.status}`);
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
    alert("Could not fetch a title from the URL — please enter one manually.");
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
    loadFeeds();
  } else {
    alert(`add failed: ${res.status}`);
  }
});

document.getElementById("reload").addEventListener("click", loadFeeds);

(async function boot() {
  await Auth.init();
  loadFeeds();
})();
