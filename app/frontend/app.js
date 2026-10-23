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
const newBtn = document.getElementById("new-btn");

const PAGE = 50;
const STALE_MS = 10 * 60 * 1000; // offer a refresh once the shown content is this old
const POLL_MS = 20 * 1000;
let lastPoll = 0;
// Expanded parts survive live re-renders: details (tags, metrics) and the source list.
const openIds = new Set();
const sourcesOpenIds = new Set();
// Marked read during this page visit: stay visible (faded) with their "New since you read it"
// delta until the next manual refresh or reload pill.
const sessionReadIds = new Set();
const sessionChanges = new Map();
let touchActiveId = null;

function setTouchActive(card) {
  const nextId = card?._data?.id ?? null;
  if (touchActiveId === nextId && (!card || card.classList.contains("touch-active"))) return;
  touchActiveId = nextId;
  document.querySelectorAll("story-card.touch-active").forEach((el) => {
    if (el !== card) el.classList.remove("touch-active");
  });
  if (card) card.classList.add("touch-active");
}
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

function urgencyWord(u) {
  if (u >= 8) return "Critical";
  if (u >= 6) return "High";
  if (u >= 4) return "Medium";
  return "Low";
}

const signed = (w) => `${w > 0 ? "+" : ""}${w}`;
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

// Expanded card: plain-language facts about the story (both views).
function factsHtml(d) {
  const m = d.metrics || {};
  const rel = d.relevance || {};
  const outlets = d.outlet_count ?? d.source_count ?? 0;
  return [
    `<span class="fact" title="Urgency ${m.urgency_level ?? "–"}/10 (AI-rated)">${urgencyWord(m.urgency_level ?? 5)} urgency</span>`,
    m.impact_scale ? `<span class="fact" title="Who is affected">${esc(m.impact_scale)} impact</span>` : "",
    `<span class="fact" title="Trust-weighted median quality of the source articles (1–10)">Quality <b class="${qualityClass(d.quality_score)}">${Number(d.quality_score).toFixed(1)}</b></span>`,
    `<span class="fact" title="Independent outlets reporting it">${plural(outlets, "outlet", "outlets")}</span>`,
    rel.updates_24h ? `<span class="fact" title="Updates with new facts in the last 24 hours">${plural(rel.updates_24h, "update", "updates")} today</span>` : "",
  ].filter(Boolean).join("");
}

// "Why it is in your feed", built only from the score breakdown (no guessing).
function whyHtml(d) {
  const rel = d.relevance || {};
  const m = d.metrics || {};
  const pick = (source, positive) => (rel.matches || [])
    .filter((x) => x.source === source && (x.signal > 0) === positive && Math.abs(x.signal) >= 0.05)
    .map((x) => `<b>#${esc(x.tag)}</b>`).join(", ");
  const lines = [];
  const mine = pick("list", true), avoided = pick("list", false);
  const liked = pick("learned", true), disliked = pick("learned", false);
  if (mine) lines.push(`On your tag list: ${mine}`);
  if (avoided) lines.push(`Turned down in your tag list: ${avoided}`);
  if (liked) lines.push(`Like stories you liked or asked about: ${liked}`);
  if (disliked) lines.push(`Like stories you voted down: ${disliked}`);
  const place = m.location ? ` (${esc(m.location)})` : "";
  const geo = { city: `Near your home${place}`, country: `In your country${place}`, language: "In your language",
    region: `Your regions: ${esc((rel.regions || []).join(", "))}` }[rel.geo_reason];
  if (geo) lines.push(geo);
  if (d.read === "updated") lines.push("New facts since you read it");
  if (!lines.length) return "";
  return `<div class="why"><span class="why-label">Why it is in your feed</span><ul>${lines.map((l) => `<li>${l}</li>`).join("")}</ul></div>`;
}

const PART_LABELS = { quality: "Reporting quality", coverage: "Source coverage", urgency: "Urgency", impact: "Impact",
  velocity: "Development", status: "Status", tags: "Your tags", geo: "Location", news: "Latest new facts" };

// Collapsed by default: the exact points behind the priority for the curious.
function inspectorHtml(d) {
  const rel = d.relevance || {};
  if (!rel.parts) return "";
  const detail = { quality: `${rel.quality}/10`, coverage: plural(d.outlet_count ?? 0, "outlet", "outlets"),
    urgency: `${rel.urgency}/10`, impact: rel.impact || "–", velocity: `${rel.updates_24h || 0} updates / 24 h`,
    status: rel.status || "–", tags: rel.affinity ? `affinity ${signed(rel.affinity)}` : "", geo: rel.geo_reason || "", news: "" };
  const rows = Object.entries(rel.parts).map(([k, v]) => `<tr><td>${esc(PART_LABELS[k] || k)}</td>
    <td class="muted">${esc(detail[k] ?? "")}</td><td class="num">${esc(signed(v.toFixed(1)))}</td></tr>`).join("");
  return `<details class="score-inspector"><summary>Priority <b>${Math.round(d.display_score)}</b>/100 · how it is computed</summary>
    <table>${rows}
      <tr class="sum"><td>Value</td><td class="muted">sum, bent towards 100 above 80</td><td class="num">${rel.value}</td></tr>
      <tr><td>Freshness</td><td class="muted">${rel.age_h} h since news, half-life ${rel.half_life_h} h</td><td class="num">×${rel.freshness}</td></tr>
      <tr class="sum"><td>Priority</td><td></td><td class="num">${d.display_score}</td></tr>
    </table></details>`;
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

// Collapsed cards show at most this much summary; the full text appears
// with the details when the card is expanded.
const SUMMARY_MAX = 400;
function clampSummary(text) {
  if (!text || text.length <= SUMMARY_MAX) return text;
  const cut = text.slice(0, SUMMARY_MAX);
  const at = cut.lastIndexOf(" ");
  return (at > SUMMARY_MAX * 0.8 ? cut.slice(0, at) : cut) + "\u2026";
}

// --- <story-card> ---------------------------------------------------------
class StoryCard extends HTMLElement {
  set story(data) {
    this._data = data;
    this.render();
  }

  _feedback(kind) {
    const next = this._data.vote === kind ? null : kind;
    fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ story_id: this._data.id, kind: next }),
    }).then(async (res) => {
      if (Auth.unauth(res) || !res.ok) return;
      const body = await res.json().catch(() => null);
      if (body?.profile?.tag_weights) Auth.setTagWeights(body.profile.tag_weights);
      this._data.vote = next;
      this.querySelectorAll(".fb[data-kind]").forEach((b) => {
        const on = b.dataset.kind === next;
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", on);
      });
      Auth.toast(next === "up" ? "Thanks — more like this."
        : next === "down" ? "Got it — less like this." : "Vote removed.");
    });
  }

  _rememberChanges() {
    const d = this._data;
    if (d.changes?.length && !sessionChanges.has(d.id)) {
      sessionChanges.set(d.id, { changes: d.changes, read_at: d.prev_read_at || d.read_at });
    }
    if (d.read_at && !d.prev_read_at) d.prev_read_at = d.read_at;
  }

  _syncReadDom() {
    const d = this._data;
    const isRead = d.read === "read";
    const updated = d.read === "updated";
    const showChanges = (updated || (isRead && sessionReadIds.has(d.id))) && d.changes?.length;
    if ((showChanges && !this.querySelector(".changes")) || (updated && !this.querySelector(".badge-updated"))) {
      this.render();
      return;
    }
    this.classList.toggle("is-read", isRead);
    this.classList.toggle("is-updated", updated);
    const readTitle = { read: "Read — click to mark as unread", updated: "Updated since you read it — click to mark as read again" }[d.read]
      || "Mark as read";
    const readBtn = this.querySelector(".read-btn");
    if (readBtn) {
      readBtn.setAttribute("aria-pressed", isRead);
      readBtn.title = readTitle;
      readBtn.setAttribute("aria-label", readTitle);
      readBtn.innerHTML = updated ? ICON_UPDATED : ICON_CHECK;
    }
    if (!updated) this.querySelector(".badge-updated")?.remove();
    if (!showChanges) {
      this.querySelector(".changes")?.remove();
      this.querySelectorAll(".new-badge").forEach((el) => el.remove());
    }
  }

  async _toggleRead() {
    const d = this._data;
    const prevRead = d.read, prevReadAt = d.read_at, prevPrevReadAt = d.prev_read_at;
    const hadSessionRead = sessionReadIds.has(d.id);
    const markRead = d.read !== "read";
    if (markRead) {
      // Keep d.changes for the rest of the page visit: the "New since you
      // read it" block stays under the freshly-marked story until a refresh.
      this._rememberChanges();
      Object.assign(d, { read: "read", read_at: utcNowDb() });
      sessionReadIds.add(d.id);
    } else {
      d.read = null;
      sessionReadIds.delete(d.id);
    }
    this._syncReadDom();
    const res = await fetch(`/api/stories/${d.id}/read`, { method: markRead ? "POST" : "DELETE" });
    if (Auth.unauth(res) || !res.ok) {
      Object.assign(d, { read: prevRead, read_at: prevReadAt, prev_read_at: prevPrevReadAt });
      if (hadSessionRead) sessionReadIds.add(d.id); else sessionReadIds.delete(d.id);
      this._syncReadDom();
    }
  }

  render() {
    const d = this._data;
    const m = d.metrics || {};
    const breaking = m.status === "Breaking" && m.urgency_level >= 8;
    const articles = d.articles || [];
    const srcCount = d.outlet_count ?? d.source_count ?? articles.length;
    const feedCount = d.source_count ?? articles.length;
    const feeds = [...new Set(articles.map((a) => shortFeed(a.feed || hostname(a.url))).filter(Boolean))];
    const feedPreview = feeds.slice(0, 3).map(esc).join(" · ") + (feeds.length > 3 ? ` +${feeds.length - 3}` : "");
    const location = m.location && !/^global\b/i.test(m.location) ? `<span>📍 ${esc(m.location)}</span>` : "";
    const tags = (d.tags || []).map((t) => tagButton(t)).join("");
    const updated = d.read === "updated";
    const showChanges = (updated || (d.read === "read" && sessionReadIds.has(d.id))) && d.changes?.length;
    const sinceAt = d.prev_read_at || d.read_at;
    const articleRows = articles.map((a) => {
      const src = [a.feed || hostname(a.url), a.author].filter(Boolean).join(" · ");
      const when = a.published_date ? ` · ${esc(timeAgo(a.published_date))}` : "";
      const isNew = (updated || showChanges) && sinceAt && (a.created_at || "") > sinceAt;
      return `<li class="article">
          ${isNew ? `<span class="new-badge">new</span>` : ""}<a href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>
          <div class="src">${esc(src)}${when}</div>
        </li>`;
    }).join("");
    const newCount = updated ? articles.filter((a) => (a.created_at || "") > (d.read_at || "")).length : 0;
    // Freshly-marked (this session) stories keep showing the delta until a
    // page reload / refresh replaces this state.
    const changes = showChanges
      ? `<div class="changes"><span class="changes-label">New since you read it</span><ul>${d.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>` : "";
    const related = (d.related || []).map((r) =>
      `<li><a href="/s/${Number(r.id)}" target="_blank" rel="noopener">${esc(r.headline)}</a></li>`).join("");
    const readTitle = { read: "Read — click to mark as unread", updated: "Updated since you read it — click to mark as read again" }[d.read]
      || "Mark as read";
    const open = openIds.has(d.id);
    const srcOpen = sourcesOpenIds.has(d.id);
    const fullSummary = d.summary || "";
    const summary = clampSummary(fullSummary);

    this.className = [urgencyClass(m.urgency_level), breaking ? "breaking" : "", open ? "open" : "", srcOpen ? "src-open" : "",
      d.read === "read" ? "is-read" : "", updated ? "is-updated" : "", touchActiveId === d.id ? "touch-active" : ""].filter(Boolean).join(" ");
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
        <h2 class="headline"><a class="headline-link" href="/s/${Number(d.id)}">${esc(d.headline)}</a></h2>
        <p class="summary">${esc(summary)}</p>
        ${changes}
      </div>
      <div class="collapse details"><div>
        <div class="tags">${tags || `<span class="muted">no tags</span>`}</div>
        <div class="facts">${factsHtml(d)}</div>
        ${state.view === "personal" && Auth.state.authenticated ? whyHtml(d) : ""}
        ${inspectorHtml(d)}
        ${related ? `<div class="related"><span class="muted">Related stories</span><ul>${related}</ul></div>` : ""}
      </div></div>
      <div class="card-foot">
        <button type="button" class="src-toggle" aria-expanded="${srcOpen}" title="${srcCount} independent ${srcCount === 1 ? "outlet" : "outlets"} from ${feedCount} ${feedCount === 1 ? "feed" : "feeds"} — show all source articles">
          <span class="src-count">${srcCount} ${srcCount === 1 ? "source" : "sources"}</span>
          <span class="src-names">${feedPreview}</span>
          <span class="chev" aria-hidden="true"></span>
        </button>
        <button type="button" class="icon-btn share-btn" title="Copy a public link to this story" aria-label="Share">🔗</button>
        <div class="interaction">
          <button type="button" class="icon-btn fb${d.vote === "up" ? " active" : ""}" data-kind="up" aria-pressed="${d.vote === "up"}" title="More like this" aria-label="More like this">👍</button>
          <button type="button" class="icon-btn fb${d.vote === "down" ? " active" : ""}" data-kind="down" aria-pressed="${d.vote === "down"}" title="Less like this" aria-label="Less like this">👎</button>
          <button type="button" class="icon-btn chat-btn" title="Ask about this story">💬 Ask</button>
        </div>
      </div>
      <div class="collapse sources"><div>
        <ol class="article-list">${articleRows}</ol>
      </div></div>`;

    const body = this.querySelector(".card-body");
    const summaryEl = this.querySelector(".summary");
    // Same element serves both states — the text swaps instead of duplicating:
    // collapsed shows ~400 chars, expanded shows the full summary.
    const setSummary = (openNow) => {
      summaryEl.textContent = openNow ? fullSummary : clampSummary(fullSummary);
    };
    setSummary(open);
    const toggleDetails = () => {
      const openNow = toggleIn(openIds, d.id, this, "open");
      body.setAttribute("aria-expanded", openNow);
      setSummary(openNow);
      return openNow;
    };
    // Selecting summary text must not toggle the card. Opening an unread or
    // updated story also marks it read (signed in only). The headline
    // navigates to the public link instead, so the card-body click handler
    // is scoped to its summary/kicker area only.
    const onClick = () => {
      if (String(window.getSelection())) return;
      const openNow = toggleDetails();
      if (openNow && Auth.state.authenticated && this._data.read !== "read") this._toggleRead();
    };
    summaryEl.addEventListener("click", onClick);
    this.querySelector(".headline-link").addEventListener("click", (e) => {
      e.stopPropagation(); // don't toggle the card or clear the text selection
      onClick(); // headline taps/keys count as touching the story
    });
    body.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(); } });
    const srcBtn = this.querySelector(".src-toggle");
    srcBtn.addEventListener("click", () => srcBtn.setAttribute("aria-expanded", toggleIn(sourcesOpenIds, d.id, this, "src-open")));
    this.querySelectorAll(".fb[data-kind]").forEach((b) => b.addEventListener("click", () => this._feedback(b.dataset.kind)));
    this.querySelector(".chat-btn").addEventListener("click", () => Chat.open(d.id));
    this.querySelector(".share-btn").addEventListener("click", () => copyLink(`/s/${d.id}`));
    this.querySelector(".read-btn").addEventListener("click", () => this._toggleRead());
  }
}
customElements.define("story-card", StoryCard);

// --- tag popup menu: hover/click a tag → "add to my tag list" / filter ---
function tagClass(t) {
  if (!Auth.state.authenticated || !Auth.hasTagWeight(t)) return "tag";
  const w = Auth.tagWeight(t);
  return w <= -10 ? "tag tag-muted" : w < 0 ? "tag tag-neg" : "tag tag-weighted";
}

function tagLabel(t) {
  const has = Auth.state.authenticated && Auth.hasTagWeight(t);
  return `#${esc(t)}${has ? ` <span class="tag-w" title="Your weight">${esc(signed(Auth.tagWeight(t)))}</span>` : ""}`;
}

function tagButton(t) {
  const title = Auth.state.authenticated ? "Weight this tag · filter by tag" : "Filter by tag";
  return `<button type="button" class="${tagClass(t)}" data-tag="${esc(t)}" title="${title}">${tagLabel(t)}</button>`;
}

const tagMenuEl = document.createElement("div");
tagMenuEl.className = "tag-menu";
tagMenuEl.hidden = true;
tagMenuEl.setAttribute("role", "menu");
document.body.appendChild(tagMenuEl);
let tagMenuTag = null, tagMenuHoverTimer = 0, tagMenuCloseTimer = 0, tagMenuHoverOpenedAt = 0;

// One click sets the weight: −10 mutes the tag in "For you", 10 = must see.
const TAG_PRESETS = [[-10, "Mute", "Hide stories with this tag from “For you”"], [-3, "−3", "Less of this"],
  [1, "+1", "A bit more"], [3, "+3", "More of this"], [10, "+10", "Must see"]];

function setTagWeight(tag, weight) {
  const had = Auth.hasTagWeight(tag);
  const prev = Auth.tagWeight(tag);
  return Auth.addTagWeight(tag, weight).then((ok) => {
    if (tagMenuTag === tag) tagMenuRender();
    refreshTagButtons();
    if (!ok) return Auth.toast("Saving the tag failed.");
    const what = weight === null ? `#${tag} removed from your tag list`
      : weight <= -10 ? `#${tag} muted in “For you”` : `#${tag} set to ${signed(weight)}`;
    Auth.toast(`${what} — the feed re-ranks on the next refresh.`, {
      label: "Undo",
      onClick: () => Auth.addTagWeight(tag, had ? prev : null).then(() => {
        if (tagMenuTag === tag) tagMenuRender();
        refreshTagButtons();
      }),
    });
  });
}

function tagMenuRender() {
  const tag = tagMenuTag;
  const inList = Auth.state.authenticated && Auth.hasTagWeight(tag);
  const w = Auth.tagWeight(tag);
  tagMenuEl.innerHTML = `
    <div class="tag-menu-head">#${esc(tag)} <span class="muted">${inList ? `· your weight ${esc(signed(w))}` : "· not on your tag list"}</span></div>
    <div class="tag-presets" role="group" aria-label="Weight for #${esc(tag)}">${TAG_PRESETS.map(([v, label, title]) =>
      `<button type="button" class="tag-preset${v < 0 ? " neg" : ""}${inList && w === v ? " active" : ""}" data-w="${v}" title="${esc(title)}" aria-pressed="${inList && w === v}">${label}</button>`).join("")}</div>
    <div class="tag-menu-row">
      <label>Exact <input type="number" id="tag-menu-weight" class="tag-weight-input" min="-10" max="10" step="0.5" value="${inList ? esc(w) : ""}" placeholder="0" aria-label="Exact weight" /></label>
      ${inList ? `<button type="button" class="link" id="tag-menu-remove">Remove</button>` : ""}
    </div>
    <button type="button" class="tag-menu-opt" id="tag-menu-filter">Filter feed by this tag</button>`;
  tagMenuEl.querySelectorAll(".tag-preset").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    const v = Number(b.dataset.w);
    setTagWeight(tag, inList && w === v ? null : v); // clicking the active preset removes the tag
  }));
  const weight = tagMenuEl.querySelector("#tag-menu-weight");
  let pending = false;
  const commit = () => {
    const v = parseFloat(weight.value);
    if (pending || Number.isNaN(v)) return;
    const clamped = Math.max(-10, Math.min(10, v));
    if (inList && clamped === w) return;
    pending = true;
    setTagWeight(tag, clamped).finally(() => { pending = false; });
  };
  weight.addEventListener("click", (e) => e.stopPropagation());
  weight.addEventListener("change", commit);
  weight.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
  });
  tagMenuEl.querySelector("#tag-menu-remove")?.addEventListener("click", (e) => { e.stopPropagation(); setTagWeight(tag, null); });
  tagMenuEl.querySelector("#tag-menu-filter").addEventListener("click", (e) => { e.stopPropagation(); closeTagMenu(); setTag(tag); });
}

function openTagMenu(tag, anchor, fromHover = false) {
  clearTimeout(tagMenuHoverTimer);
  clearTimeout(tagMenuCloseTimer);
  if (fromHover) tagMenuHoverOpenedAt = Date.now();
  if (tagMenuTag !== tag) {
    tagMenuTag = tag;
    tagMenuRender();
  }
  const r = anchor.getBoundingClientRect();
  tagMenuEl.hidden = false;
  const w = tagMenuEl.offsetWidth, h = tagMenuEl.offsetHeight;
  const left = Math.max(8, Math.min(r.left, innerWidth - w - 8));
  const below = r.top - h - 6 < 4; // no room above → open below
  tagMenuEl.style.left = left + "px";
  tagMenuEl.style.top = (below ? r.bottom + 6 : r.top - h - 6) + "px";
}

function closeTagMenu() {
  tagMenuEl.hidden = true;
  tagMenuTag = null;
  clearTimeout(tagMenuHoverTimer);
  clearTimeout(tagMenuCloseTimer);
}

/* Delegated handlers — story-cards render tags dynamically, so there is no
 * per-button binding. Signed out: click filters directly (no personal list).
 * Signed in: mouse pointer-over opens after 250 ms, closes 400 ms after it
 * leaves the tag/menu; touch or click toggles immediately. */
document.addEventListener("click", (e) => {
  const btn = e.target.closest?.(".tag");
  if (btn) {
    if (!Auth.state.authenticated) { setTag(btn.dataset.tag); return; }
    if (tagMenuTag === btn.dataset.tag && Date.now() - tagMenuHoverOpenedAt >= 600) closeTagMenu();
    else openTagMenu(btn.dataset.tag, btn, false);
    return;
  }
  if (!tagMenuEl.hidden && !tagMenuEl.contains(e.target)) closeTagMenu();
});
document.addEventListener("pointerdown", (e) => {
  if (e.pointerType === "mouse") {
    if (touchActiveId !== null) setTouchActive(null);
    return;
  }
  const card = e.target.closest?.("story-card");
  if (card) setTouchActive(card);
  else if (!e.target.closest?.(".tag-menu, #chat-popup")) setTouchActive(null);
});
document.addEventListener("pointerover", (e) => {
  if (e.pointerType !== "mouse") return; // touch = tap-toggle via click only
  if (touchActiveId !== null) setTouchActive(null);
  clearTimeout(tagMenuCloseTimer);
  const btn = e.target.closest?.(".tag");
  if (btn && Auth.state.authenticated) {
    clearTimeout(tagMenuHoverTimer);
    const b = btn;
    tagMenuHoverTimer = setTimeout(() => openTagMenu(b.dataset.tag, b, true), 250);
    return;
  }
  if (tagMenuEl.contains(e.target) || tagMenuEl.contains(document.activeElement) || tagMenuEl.hidden || tagMenuTag === null) return;
  clearTimeout(tagMenuHoverTimer);
  tagMenuCloseTimer = setTimeout(closeTagMenu, 400);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeTagMenu(); });
document.addEventListener("scroll", () => { if (!tagMenuEl.hidden) closeTagMenu(); }, { passive: true });

/* Re-render the weighted state of every open card's tag buttons. */
function refreshTagButtons() {
  document.querySelectorAll("story-card .tag").forEach((b) => {
    const tag = b.dataset.tag;
    b.className = tagClass(tag);
    b.innerHTML = tagLabel(tag);
  });
}
Auth.onTagWeights(refreshTagButtons);

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
  const view = Auth.state.authenticated ? state.view : "default";
  p.set("view", view);
  if (state.scope === "archive") p.set("archived", "true");
  if (state.query) p.set("q", state.query);
  if (state.tag) p.set("tag", state.tag);
  if (state.scope === "active" && view === "personal" && !state.showRead) {
    p.set("hide_read", "true");
    if (sessionReadIds.size) p.set("keep", [...sessionReadIds].join(","));
  }
  return `/api/stories?${p}`;
}

async function fetchStories(limit) {
  const res = await fetch(storiesUrl(limit));
  if (!res.ok) return false;
  const data = await res.json();
  for (const s of data.stories) {
    if (s.changes?.length) {
      sessionChanges.set(s.id, { changes: s.changes, read_at: s.read_at });
    } else if (sessionReadIds.has(s.id) && sessionChanges.has(s.id)) {
      const saved = sessionChanges.get(s.id);
      s.changes = saved.changes;
      s.prev_read_at = saved.read_at;
    }
  }
  Object.assign(state, {
    stories: data.stories, total: data.count, pending: data.pending, hiddenRead: data.hidden_read || 0,
    activeDays: data.active_days,
    newestAt: data.latest || data.stories.reduce((m, s) => (s.news_at > m ? s.news_at : m), ""),
  });
  newBtn.hidden = true;
  return true;
}

// Background check (cheap: no ranking): offer a reload when stories got news since the page loaded.
async function poll() {
  if (document.hidden || state.loading || !state.newestAt || state.scope === "archive") return;
  lastPoll = Date.now();
  const since = state.newestAt;
  let data;
  try {
    const res = await fetch(`/api/stories/pulse?since=${encodeURIComponent(since)}`);
    if (!res.ok) return;
    data = await res.json();
  } catch {
    return;
  }
  if (state.loading || since !== state.newestAt) return; // reloaded meanwhile
  state.pending = data.pending;
  updateStatus();
  if (data.new > 0) {
    newBtn.textContent = `↑ ${data.new} new ${data.new === 1 ? "update" : "updates"}`;
    newBtn.hidden = false;
  } else {
    newBtn.hidden = true;
  }
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

function syncShowReadBtn() {
  const forceShow = state.scope === "archive" || state.view === "default";
  showReadBtn.disabled = forceShow;
  showReadBtn.setAttribute("aria-pressed", forceShow || state.showRead);
}

function setScope(scope) {
  state.scope = scope;
  for (const [k, b] of Object.entries(scopeBtns)) b.classList.toggle("active", k === scope);
  syncShowReadBtn();
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
  syncShowReadBtn();
}
for (const [k, b] of Object.entries(viewBtns)) b.addEventListener("click", () => { setView(k); reload(); loadHeadsUp(); });
setView(state.view);
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
  sessionReadIds.clear();
  sessionChanges.clear();
  setTouchActive(null);
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
newBtn.addEventListener("click", () => {
  newBtn.hidden = true;
  window.scrollTo({ top: 0, behavior: "smooth" });
  refreshAll();
});
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
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  checkStale();
  if (Date.now() - lastPoll >= POLL_MS) poll();
});

(async function boot() {
  await Auth.init();
  await reload();
  Auth.onChange(() => { reload(); loadHeadsUp(); }); // sign-in/out or saved preferences
  loadHeadsUp();
  lastPoll = Date.now();
  setInterval(poll, POLL_MS);
})();
