/* "Chats" tab (/chats): the signed-in user's story chats — answers in progress and
 * unread ones first, then the rest. State comes from Chat (chat.js: /api/chats sync
 * plus the live answer streams); a row opens the chat popup. */

const listEl = document.getElementById("chat-list");
const statusEl = document.getElementById("chats-status");
const searchEl = document.getElementById("search");
const markAllBtn = document.getElementById("mark-all");
let loaded = false;
let lastRender = 0;
let renderTimer = 0;

// Answer previews are markdown; the list shows them as one plain line.
function plain(md) {
  return (md || "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/[`*#>_]+/g, "").replace(/\s+/g, " ").trim();
}

function secs(s) {
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

const STATE_LABEL = { running: "working", queued: "queued", ready: "new", error: "failed", stopped: "stopped" };

function rowHtml(c) {
  const p = Chat.progressOf(c.story_id);
  const busy = p.state === "running" || p.state === "queued";
  const progress = busy ? [p.text, p.research, p.secs != null ? secs(p.secs) : ""].filter(Boolean).join(" \u00b7 ") : "";
  const label = STATE_LABEL[p.state];
  const n = Number(c.questions) || 0;
  return `<article class="chat-row st-${esc(p.state)}" data-id="${Number(c.story_id)}" tabindex="0">
    <span class="cr-state" aria-hidden="true"></span>
    <div class="cr-main">
      <div class="cr-head">
        <span class="cr-title">${esc(c.headline || Chat.titleOf(c.story_id))}</span>
        ${label ? `<span class="cr-flag">${label}</span>` : ""}
        <a class="cr-story" href="/s/${Number(c.story_id)}" target="_blank" rel="noopener" title="Open the story">\u2197</a>
      </div>
      ${c.question ? `<div class="cr-line"><span class="cr-who">You</span>${esc(c.question)}</div>` : ""}
      ${busy
        ? `<div class="cr-line cr-progress"><span class="cr-who">AI</span>${esc(progress)}</div>`
        : c.answer ? `<div class="cr-line"><span class="cr-who">AI</span>${esc(plain(c.answer))}</div>` : ""}
      <div class="cr-meta">${n} question${n === 1 ? "" : "s"} \u00b7 ${esc(timeAgo(c.updated_at))}</div>
    </div>
  </article>`;
}

function render() {
  lastRender = Date.now();
  const q = searchEl.value.trim().toLowerCase();
  const all = Chat.chatList()
    .filter((c) => !q || [c.headline, c.question, c.answer].some((t) => (t || "").toLowerCase().includes(q)))
    .sort((a, b) => (parseUtc(b.updated_at) || 0) - (parseUtc(a.updated_at) || 0));
  const pending = all.filter((c) => ["running", "queued", "ready", "error", "stopped"].includes(Chat.progressOf(c.story_id).state));
  const rest = all.filter((c) => !pending.includes(c));
  const unread = Chat.chatList().filter((c) => c.unread).length;
  const running = Chat.chatList().filter((c) => Chat.progressOf(c.story_id).state === "running").length;
  markAllBtn.disabled = !unread;
  statusEl.textContent = [`${Chat.chatList().length} chats`, unread ? `${unread} unread` : "", running ? `${running} working` : ""]
    .filter(Boolean).join(" \u00b7 ");
  if (!loaded) return;
  if (!all.length) {
    listEl.innerHTML = `<div class="empty">${q ? "No chat matches." : "No chats yet \u2014 ask about a story with \u{1F4AC} Ask."}</div>`;
    return;
  }
  listEl.innerHTML = (pending.length ? `<h2 class="chats-h">Unread &amp; in progress</h2>${pending.map(rowHtml).join("")}` : "")
    + (rest.length ? `<h2 class="chats-h">${pending.length ? "Earlier" : "All chats"}</h2>${rest.map(rowHtml).join("")}` : "");
}

// Live updates arrive many times a second while an answer streams; the list redraws at most once a second
// (redrawing under the pointer could swallow a click).
function scheduleRender() {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(render, Math.max(0, 1000 - (Date.now() - lastRender)));
}

function openRow(e) {
  if (e.target.closest("a")) return;
  const row = e.target.closest(".chat-row");
  if (row) Chat.open(Number(row.dataset.id), row.querySelector(".cr-title")?.textContent);
}

listEl.addEventListener("click", openRow);
listEl.addEventListener("keydown", (e) => { if (e.key === "Enter") openRow(e); });
searchEl.addEventListener("input", render);
markAllBtn.addEventListener("click", () => {
  for (const c of Chat.chatList()) if (c.unread) Chat.markRead(c.story_id);
});

Chat.onUpdate(() => {
  if (!loaded) { loaded = true; render(); return; }
  scheduleRender();
});

(async function boot() {
  await Auth.init();
  if (!Auth.state.authenticated) location.href = "/";
})();
