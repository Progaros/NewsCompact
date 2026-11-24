/* Public share page: /s/<story id> (a story, default view) or /p/<token> (a brief or chat answer snapshot). */

const root = document.getElementById("shared");

function storyHtml(d) {
  const sources = (d.articles || []).map((a) => `<li class="article">
      <a href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>
      <div class="src">${esc([a.feed || hostname(a.url), a.author].filter(Boolean).join(" · "))}${a.published_date ? ` · ${esc(timeAgo(a.published_date))}` : ""}</div>
    </li>`).join("");
  const related = (d.related || []).map((r) =>
    `<li><a href="/s/${Number(r.id)}">${esc(r.headline)}</a></li>`).join("");
  return `<div class="shared-kicker">Story · ${esc(timeAgo(d.news_at || d.updated_at))}</div>
    <h2 class="headline">${esc(d.headline)}</h2>
    <p class="summary">${esc(d.summary)}</p>
    <div class="tags">${(d.tags || []).map((t) => `<span class="tag">#${esc(t)}</span>`).join("")}</div>
    ${related ? `<div class="related"><span class="muted">Related stories</span><ul>${related}</ul></div>` : ""}
    <div class="shared-label">${d.outlet_count ?? d.source_count} ${(d.outlet_count ?? d.source_count) === 1 ? "source" : "sources"}</div>
    <ol class="article-list">${sources}</ol>`;
}

function snapshotHtml(d) {
  const by = `Shared · ${esc(fmtUtc(d.created_at))}`;
  if (d.kind === "brief") {
    return `<div class="shared-kicker">Quick Heads Up${d.view === "personal" ? " (personal)" : ""}</div>
      <p class="summary shared-brief">${esc(d.text)}</p>
      <div class="shared-by">${by}</div>`;
  }
  return `<div class="shared-kicker">Story chat · <a href="/s/${Number(d.story_id)}">${esc(d.headline)}</a></div>
    ${d.question ? `<div class="chat-msg user shared-q"><span class="chat-body">${esc(d.question)}</span></div>` : ""}
    <div class="chat-body shared-a">${Chat.mdToHtml(d.answer)}</div>
    <div class="shared-by">${by}</div>`;
}

(async function boot() {
  const [, kind, key] = location.pathname.split("/");
  const url = kind === "s" ? `/api/stories/${encodeURIComponent(key)}?view=default` : `/api/share/${encodeURIComponent(key)}`;
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(res.status === 404 ? "This link doesn't exist (anymore)." : `Error ${res.status}`);
    const d = await res.json();
    root.innerHTML = kind === "s" ? storyHtml(d) : snapshotHtml(d);
    document.title = `${kind === "s" ? d.headline : d.kind === "brief" ? "Heads Up" : d.headline} — News`;
  } catch (err) {
    root.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
})();
