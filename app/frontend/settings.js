/* Unified Settings page (/settings):
 * User tabs: Profile, Tags, Sources, Account
 * Admin tabs: Schedules, Users, Tag Synonyms (with bulk review) */

const statusEl = document.getElementById("settings-status");
const tabsEl = document.getElementById("settings-tabs");
const panes = [...document.querySelectorAll(".page-pane")];
const form = document.getElementById("prefs-form");
const saveBar = document.getElementById("save-bar");
const profilePane = document.querySelector('[data-pane="profile"]');
const tagsCountEl = document.getElementById("tags-count");
const synCountEl = document.getElementById("syn-count");

const PROFILE_TABS = new Set(["profile", "tags", "sources", "schedules"]);
const USER_TABS = new Set(["profile", "tags", "sources", "account"]);
const ADMIN_TABS = new Set(["schedules", "users", "synonyms"]);

const DAY_NAMES = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
const REPORT_LABELS = { brief: "News brief", pipeline: "Pipeline report", podcast: "Daily podcast" };
const PODCAST_LANGS = [["auto", "Auto"], ["de", "Deutsch"], ["en", "English"]];
const PODCAST_STYLES = [["informative", "Informative"], ["entertaining", "Entertaining"], ["analytical", "Analytical"], ["briefing", "Briefing"]];
const PODCAST_FORMATS = [["solo", "Solo host"], ["dialogue", "2 hosts"]];
const FEED_VIEWS = [["personal", "For you"], ["default", "For everyone"]];

let activeTab = "profile";
let weights = {};
let reports = [];
let catOverrides = {};
let feedOverrides = {};
let excluded = new Set();
let ntfy = null;
let loadedUsers = false;
let loadedSynonyms = false;
let pendingAliases = [];

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (Auth.unauth(res)) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    Auth.toast(typeof d === "string" ? d : `Failed (${res.status})`);
    return null;
  }
  return data;
}

function isAllowedTab(id) {
  if (USER_TABS.has(id)) return true;
  return ADMIN_TABS.has(id) && !!Auth.state.user?.is_admin;
}

function switchTab(id, { pushHash = true } = {}) {
  if (!isAllowedTab(id)) id = "profile";
  activeTab = id;
  tabsEl.querySelectorAll('[role="tab"]').forEach((t) => {
    const on = t.dataset.tab === id;
    t.setAttribute("aria-selected", on);
    t.tabIndex = on ? 0 : -1;
  });
  panes.forEach((p) => { p.hidden = p.dataset.pane !== id; });
  saveBar.hidden = !PROFILE_TABS.has(id);
  if (pushHash && location.hash !== `#${id}`) {
    history.replaceState(null, "", `${location.pathname}#${id}`);
  }
  if (id === "users" && !loadedUsers) loadUsers();
  if (id === "synonyms" && !loadedSynonyms) loadAliases();
}

tabsEl.querySelectorAll('[role="tab"]').forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  btn.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const visible = [...tabsEl.querySelectorAll('[role="tab"]')].filter((t) => !t.hidden);
    const next = visible[(visible.indexOf(btn) + (e.key === "ArrowRight" ? 1 : -1) + visible.length) % visible.length];
    switchTab(next.dataset.tab);
    next.focus();
  });
});

window.addEventListener("hashchange", () => {
  const h = location.hash.replace(/^#/, "");
  if (isAllowedTab(h) && h !== activeTab) switchTab(h, { pushHash: false });
});

form.addEventListener("invalid", (e) => {
  const pane = e.target.closest?.(".page-pane");
  if (pane && pane.hidden) switchTab(pane.dataset.pane);
}, true);

// ==================== Profile / Tags / Sources / Schedules ====================
function renderProfileFields(prefs, home = {}) {
  const join = (v) => (Array.isArray(v) ? v.join(", ") : "");
  profilePane.innerHTML = `
    <label class="field">About you <span class="hint">a few words about yourself (job, interests, where you live) — used to tailor your heads-up, chat, podcasts, and semantic matching; never shared</span>
      <textarea name="about" rows="3" maxlength="1000" placeholder="e.g. Teacher in Hamburg, follows climate, education and local politics">${esc(prefs.about || "")}</textarea></label>
    <div class="field-row">
      <label class="field">Home city <span class="hint">stories there rank higher</span>
        <input name="home_city" maxlength="80" value="${esc(prefs.home_city || "")}" placeholder="${esc(home.home_city || "")}" /></label>
      <label class="field">Country
        <input name="home_country" maxlength="80" value="${esc(prefs.home_country || "")}" placeholder="${esc(home.home_country || "")}" /></label>
      <label class="field">Language <span class="hint">code</span>
        <input name="language" maxlength="3" pattern="[A-Za-z]{2,3}" value="${esc(prefs.language || "")}" placeholder="${esc(home.language || "")}" /></label>
    </div>
    <div class="field-row field-row-2">
      <label class="field">Interest regions <span class="hint">comma-separated; boosts matching stories</span>
        <input name="interest_regions" value="${esc(join(prefs.interest_regions))}" placeholder="europe, scandinavia" /></label>
      <label class="field">Locations <span class="hint">comma-separated, same effect</span>
        <input name="locations" value="${esc(join(prefs.locations))}" placeholder="hamburg" /></label>
    </div>
    <div class="user-section">
      <div class="user-section-label">Feed display</div>
      <label class="field">Minimum story quality <span class="hint">0 = show everything</span>
        <input name="min_quality" type="number" min="0" max="10" step="0.5" value="${esc(prefs.min_quality ?? 0)}" /></label>
      <label class="check pref-check"><input type="checkbox" name="show_votes" ${prefs.show_votes === false ? "" : "checked"} />
        Show \u{1F44D}/\u{1F44E} on stories <span class="hint">votes teach "For you" a little; your tag list is never changed by them</span></label>
      <label class="check pref-check"><input type="checkbox" name="semantic_ranking" ${prefs.semantic_ranking === false ? "" : "checked"} />
        Semantic topic matching <span class="hint">boosts stories close in meaning to your tags, \u{1F44D}/\u{1F44E} votes, and "About you" — even without an exact tag match</span></label>
    </div>`;
}

const DIACRITICS = { "ä": "a", "ö": "o", "ü": "u", "ß": "ss", "é": "e", "è": "e", "ê": "e", "ë": "e",
  "à": "a", "ù": "u", "ç": "c", "ñ": "n", "å": "a", "ø": "o", "æ": "ae" };
function normalizeTag(tag) {
  return String(tag).trim().toLowerCase().replace(/[äöüßéèêëàùçñåøæ]/g, (c) => DIACRITICS[c])
    .replace(/[^a-z0-9]+/g, " ").trim();
}

function syncTagCount() {
  const n = Object.keys(weights).length;
  tagsCountEl.textContent = n ? String(n) : "";
}

function renderWeights() {
  const el = document.getElementById("weights");
  const entries = Object.entries(weights).sort((a, b) => b[1] - a[1]);
  el.innerHTML = `<div class="pref-row add-weight">
        <input type="text" class="tag-input" placeholder="add tag, e.g. climate" maxlength="80" aria-label="New tag" />
        <span><input type="number" class="weight-input" min="-10" max="10" step="1" form="" value="2" aria-label="New tag weight" />
        <button type="button" class="link" data-add>add</button></span></div>`
    + (entries.length
      ? `<div class="weight-chips">` + entries.map(([t, w]) => `<div class="weight-chip" title="weight ${w >= 0 ? "+" : ""}${w} for #${esc(t)}">
        <span class="w-tag">#${esc(t)}</span>
        <button type="button" class="w-val ${w >= 0 ? "pos" : "neg"}" data-tag="${esc(t)}" title="Adjust weight" aria-label="Weight of #${esc(t)}: ${w}">${w}</button>
        <button type="button" class="w-x" data-del="${esc(t)}" title="Forget this tag" aria-label="Remove #${esc(t)}">\u00d7</button></div>`).join("") + `</div>`
      : `<div class="muted">No tags yet \u2014 click a tag on a story or add one above.</div>`);
  el.querySelectorAll(".w-val").forEach((b) => b.addEventListener("click", () => {
    const tag = b.dataset.tag;
    const input = document.createElement("input");
    input.type = "number"; input.min = "-10"; input.max = "10"; input.step = "1"; input.setAttribute("form", "");
    input.value = weights[tag];
    b.replaceWith(input);
    input.focus(); input.select();
    let done = false;
    const commit = () => {
      if (done) return; done = true;
      const v = Math.max(-10, Math.min(10, parseFloat(input.value)));
      if (!isNaN(v)) weights[tag] = v;
      renderWeights();
    };
    input.addEventListener("blur", commit);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      else if (e.key === "Escape") { e.stopPropagation(); done = true; renderWeights(); }
    });
  }));
  el.querySelectorAll("button[data-del]").forEach((b) => b.addEventListener("click", () => {
    delete weights[b.dataset.del];
    renderWeights();
  }));
  const add = el.querySelector(".add-weight");
  const addTag = () => {
    const tag = normalizeTag(add.querySelector(".tag-input").value);
    const v = Math.max(-10, Math.min(10, parseFloat(add.querySelector(".weight-input").value)));
    if (!tag || isNaN(v)) return;
    weights[tag] = v;
    renderWeights();
    document.querySelector(".add-weight .tag-input")?.focus();
  };
  add.querySelector("[data-add]").onclick = addTag;
  add.querySelectorAll("input").forEach((i) => i.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addTag(); }
  }));
  document.getElementById("reset-weights").onclick = () => {
    for (const k of Object.keys(weights)) delete weights[k];
    renderWeights();
  };
  syncTagCount();
}

function renderLearned(learned) {
  const el = document.getElementById("learned");
  const entries = Object.entries(learned).filter(([t]) => !(t in weights))
    .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 24);
  el.innerHTML = entries.length
    ? `<div class="weight-chips">` + entries.map(([t, w]) => `<button type="button" class="weight-chip learned" data-tag="${esc(t)}" title="Add #${esc(t)} to your tag list">
        <span class="w-tag">#${esc(t)}</span><span class="w-val ${w >= 0 ? "pos" : "neg"}">${w >= 0 ? "+" : ""}${w.toFixed(1)}</span></button>`).join("") + `</div>`
    : `<div class="muted">Nothing learned yet.</div>`;
  el.querySelectorAll("button[data-tag]").forEach((b) => b.addEventListener("click", () => {
    const w = learned[b.dataset.tag];
    weights[b.dataset.tag] = Math.max(-10, Math.min(10, Math.round(w) || Math.sign(w)));
    renderWeights();
    renderLearned(learned);
  }));
}

function renderTrust(defaults, feeds) {
  const el = document.getElementById("trust");
  const fmt = (v) => Number(v).toFixed(2);
  const byCat = {};
  for (const f of feeds) (byCat[f.category] ||= []).push(f);
  const catValue = (cat) => catOverrides[cat] ?? defaults[cat] ?? 1;
  const feedValue = (f) => feedOverrides[f.id] ?? (f.category in catOverrides ? catOverrides[f.category] : f.default);
  const slider = (attrs, v) => `<input type="range" min="0" max="1" step="0.05" value="${esc(v)}" ${attrs} /><output>${fmt(v)}</output>`;
  el.innerHTML = Object.keys(defaults).sort((a, b) => a.localeCompare(b)).map((cat) => `
    <div class="trust-group" data-cat="${esc(cat)}">
      <div class="trust-row cat">
        <button type="button" class="trust-toggle" aria-expanded="false" title="Show single sources">
          <span class="chev" aria-hidden="true"></span>${esc(cat || "(uncategorized)")} <span class="muted">${(byCat[cat] || []).length}</span> <span class="hidden-count"></span></button>
        ${slider(`data-trust="${esc(cat)}" aria-label="Trust of ${esc(cat || "uncategorized")}"`, catValue(cat))}
        <button type="button" class="link" data-cat-reset title="Back to the general value (${fmt(defaults[cat] ?? 1)})">\u21ba</button>
      </div>
      <div class="trust-feeds" hidden>${(byCat[cat] || []).map((f) => `
        <div class="trust-row feed" data-feed="${Number(f.id)}">
          <span class="trust-name" title="${esc(f.title)}"><button type="button" class="link exclude-btn" data-exclude title="Hide this source from your feed">\u2298</button>${esc(f.title)}</span>
          ${slider(`data-feed-id="${Number(f.id)}" aria-label="Trust of ${esc(f.title)}"`, feedValue(f))}
          <button type="button" class="link" data-reset title="Back to the category value">\u21ba</button>
        </div>`).join("")}</div>
    </div>`).join("") || `<div class="muted">No feeds yet.</div>`;

  document.getElementById("reset-trust").onclick = () => {
    for (const k of Object.keys(catOverrides)) delete catOverrides[k];
    for (const k of Object.keys(feedOverrides)) delete feedOverrides[k];
    renderTrust(defaults, feeds);
  };

  el.querySelectorAll(".trust-group").forEach((g) => {
    const cat = g.dataset.cat;
    const catRow = g.querySelector(".trust-row.cat");
    const catInput = catRow.querySelector("input[data-trust]");
    const feedRows = [...g.querySelectorAll(".trust-row.feed")];
    const feedOf = Object.fromEntries((byCat[cat] || []).map((f) => [String(f.id), f]));
    let prev = catInput.value;
    const set = (input, v) => { input.value = v; input.nextElementSibling.textContent = fmt(v); };
    const refresh = () => {
      let custom = 0, hidden = 0;
      for (const row of feedRows) {
        const on = row.dataset.feed in feedOverrides;
        const off = excluded.has(row.dataset.feed);
        row.classList.toggle("custom", on);
        row.classList.toggle("excluded", off);
        row.querySelector("input").disabled = off;
        const btn = row.querySelector("[data-exclude]");
        btn.title = off ? "Hidden from your feed — click to show it again" : "Hide this source from your feed";
        btn.setAttribute("aria-pressed", off);
        custom += on;
        hidden += off;
      }
      catRow.classList.toggle("custom", cat in catOverrides);
      g.classList.toggle("faded", custom > 0);
      g.querySelector(".hidden-count").textContent = hidden ? `${hidden} hidden` : "";
      catInput.title = custom ? `${custom} source(s) set individually — moving this resets them` : "";
    };
    const follow = () => feedRows.forEach((row) => {
      if (!(row.dataset.feed in feedOverrides)) set(row.querySelector("input"), feedValue(feedOf[row.dataset.feed]));
    });
    g.querySelector(".trust-toggle").onclick = (e) => {
      const list = g.querySelector(".trust-feeds");
      list.hidden = !list.hidden;
      e.currentTarget.setAttribute("aria-expanded", !list.hidden);
    };
    catInput.addEventListener("input", () => {
      catInput.nextElementSibling.textContent = fmt(catInput.value);
      if (g.classList.contains("faded")) return;
      catOverrides[cat] = parseFloat(catInput.value);
      prev = catInput.value;
      follow();
      refresh();
    });
    catInput.addEventListener("change", () => {
      if (!g.classList.contains("faded")) return;
      if (!confirm(`Set all ${feedRows.length} sources in “${cat || "(uncategorized)"}” to ${fmt(catInput.value)}? This replaces their individual values.`)) {
        set(catInput, prev);
        return;
      }
      for (const row of feedRows) delete feedOverrides[row.dataset.feed];
      catOverrides[cat] = parseFloat(catInput.value);
      prev = catInput.value;
      follow();
      refresh();
    });
    catRow.querySelector("[data-cat-reset]").onclick = () => {
      delete catOverrides[cat];
      set(catInput, catValue(cat));
      prev = catInput.value;
      follow();
      refresh();
    };
    for (const row of feedRows) {
      const input = row.querySelector("input");
      input.addEventListener("input", () => {
        input.nextElementSibling.textContent = fmt(input.value);
        feedOverrides[row.dataset.feed] = parseFloat(input.value);
        refresh();
      });
      row.querySelector("[data-reset]").onclick = () => {
        delete feedOverrides[row.dataset.feed];
        set(input, feedValue(feedOf[row.dataset.feed]));
        refresh();
      };
      row.querySelector("[data-exclude]").onclick = () => {
        if (!excluded.delete(row.dataset.feed)) excluded.add(row.dataset.feed);
        refresh();
      };
    }
    refresh();
  });
}

function renderReports() {
  if (!ntfy) return;
  document.getElementById("ntfy-hint").textContent = ntfy.enabled
    ? `Server time now ${ntfy.server_time}.`
    : "ntfy is not configured on the server (NEWS_HEADS_UP_NTFY_URL).";
  const el = document.getElementById("reports");
  const kinds = Object.keys(ntfy.kinds || {}).map((k) => [k, REPORT_LABELS[k] || ntfy.kinds[k]]);
  el.innerHTML = reports.length ? reports.map((r, i) => `
    <div class="report-card${r.enabled === false ? " off" : ""}" data-i="${i}">
      <div class="report-row">
        <input type="checkbox" data-f="enabled" title="Enabled" ${r.enabled === false ? "" : "checked"} />
        <select data-f="kind">${kinds.map(([k, label]) => `<option value="${esc(k)}" ${k === r.kind ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>
        <input type="time" data-f="time" value="${esc(r.time)}" required />
        <span class="days">${DAY_NAMES.map((d, di) => `<button type="button" class="day${r.days.includes(di) ? " on" : ""}" data-day="${di}" aria-pressed="${r.days.includes(di)}">${d}</button>`).join("")}</span>
        <span class="spacer"></span>
        <button type="button" class="link" data-act="send" title="${r.kind === "podcast" ? "Generate podcast now with these settings" : "Send this report now"}" ${r.kind === "podcast" || ntfy.enabled ? "" : "disabled"}>${r.kind === "podcast" ? "run now" : "send now"}</button>
        <button type="button" class="link" data-act="del" title="Remove">\u00d7</button>
      </div>
      ${r.kind === "podcast" ? `
        <div class="report-opts">
          <label class="report-opt"><span>Length</span><input type="number" data-f="duration" min="1" max="180" step="1" value="${Number(r.duration_minutes) || 15}" aria-label="Podcast duration in minutes" /> <span class="muted">min</span></label>
          <label class="report-opt"><span>Language</span><select data-f="language" aria-label="Podcast language">${PODCAST_LANGS.map(([v, lbl]) => `<option value="${v}" ${(r.language || "auto") === v ? "selected" : ""}>${lbl}</option>`).join("")}</select></label>
          <label class="report-opt"><span>Style</span><select data-f="style" aria-label="Podcast style">${PODCAST_STYLES.map(([v, lbl]) => `<option value="${v}" ${(r.style || "informative") === v ? "selected" : ""}>${lbl}</option>`).join("")}</select></label>
          <label class="report-opt"><span>Format</span><select data-f="format" aria-label="Podcast format">${PODCAST_FORMATS.map(([v, lbl]) => `<option value="${v}" ${(r.format || "solo") === v ? "selected" : ""}>${lbl}</option>`).join("")}</select></label>
          <label class="report-opt"><span>Feed</span><select data-f="view" aria-label="Podcast feed">${FEED_VIEWS.map(([v, lbl]) => `<option value="${v}" ${(r.view || "personal") === v ? "selected" : ""}>${lbl}</option>`).join("")}</select></label>
          <label class="report-opt"><span>Stories</span><input type="number" data-f="max_stories" min="3" max="50" step="1" value="${Number(r.max_stories) || ""}" placeholder="auto" aria-label="Top stories count" /></label>
          <label class="check report-check"><input type="checkbox" data-f="fact_check" ${r.fact_check === false ? "" : "checked"} /> Fact-check</label>
          <label class="check report-check"><input type="checkbox" data-f="notify" ${r.notify === false ? "" : "checked"} /> ntfy</label>
          <label class="check report-check"><input type="checkbox" data-f="script_only" ${r.script_only ? "checked" : ""} /> Script only</label>
        </div>` : r.kind === "brief" ? `
        <div class="report-opts">
          <label class="report-opt"><span>Feed</span><select data-f="view" aria-label="Brief feed">${FEED_VIEWS.map(([v, lbl]) => `<option value="${v}" ${(r.view || "personal") === v ? "selected" : ""}>${lbl}</option>`).join("")}</select></label>
          <label class="report-opt"><span>Top stories</span><input type="number" data-f="max_stories" min="3" max="30" step="1" value="${Number(r.max_stories) || 5}" aria-label="Top stories count" /></label>
        </div>` : ""}
    </div>`).join("")
    : `<div class="muted">No scheduled reports.</div>`;
  el.querySelectorAll(".report-card").forEach((card) => {
    const r = reports[Number(card.dataset.i)];
    card.querySelector('[data-f="enabled"]').onchange = (e) => { r.enabled = e.target.checked; card.classList.toggle("off", !r.enabled); };
    card.querySelector('[data-f="kind"]').onchange = (e) => {
      r.kind = e.target.value;
      if (r.kind === "podcast") {
        r.duration_minutes = r.duration_minutes || 15;
        r.language = r.language || "auto";
        r.style = r.style || "informative";
        r.format = r.format || "solo";
        r.view = r.view || "personal";
        r.fact_check = r.fact_check !== false;
        r.notify = r.notify !== false;
        r.script_only = !!r.script_only;
      } else if (r.kind === "brief") {
        delete r.duration_minutes; delete r.language; delete r.style; delete r.format;
        delete r.fact_check; delete r.notify; delete r.script_only;
        r.view = r.view || "personal";
        r.max_stories = r.max_stories || 5;
      } else {
        delete r.duration_minutes; delete r.language; delete r.style; delete r.format;
        delete r.view; delete r.max_stories; delete r.fact_check; delete r.notify; delete r.script_only;
      }
      renderReports();
    };
    const bind = (sel, fn) => { const n = card.querySelector(sel); if (n) n.onchange = fn; };
    bind('[data-f="duration"]', (e) => { r.duration_minutes = Math.max(1, Math.min(180, Number(e.target.value) || 15)); e.target.value = r.duration_minutes; });
    bind('[data-f="language"]', (e) => { r.language = e.target.value; });
    bind('[data-f="style"]', (e) => { r.style = e.target.value; });
    bind('[data-f="format"]', (e) => { r.format = e.target.value; });
    bind('[data-f="view"]', (e) => { r.view = e.target.value; });
    bind('[data-f="max_stories"]', (e) => {
      const v = Number(e.target.value);
      if (v >= 3 && v <= 50) r.max_stories = Math.round(v);
      else delete r.max_stories;
    });
    bind('[data-f="fact_check"]', (e) => { r.fact_check = e.target.checked; });
    bind('[data-f="notify"]', (e) => { r.notify = e.target.checked; });
    bind('[data-f="script_only"]', (e) => { r.script_only = e.target.checked; });
    card.querySelector('[data-f="time"]').onchange = (e) => { r.time = e.target.value; };
    card.querySelectorAll(".day").forEach((b) => b.addEventListener("click", () => {
      const d = Number(b.dataset.day);
      r.days = r.days.includes(d) ? r.days.filter((x) => x !== d) : [...r.days, d].sort();
      b.classList.toggle("on", r.days.includes(d));
      b.setAttribute("aria-pressed", r.days.includes(d));
    }));
    card.querySelector('[data-act="del"]').onclick = () => { reports.splice(Number(card.dataset.i), 1); renderReports(); };
    card.querySelector('[data-act="send"]').onclick = async (e) => {
      const idleLabel = r.kind === "podcast" ? "run now" : "send now";
      e.target.disabled = true;
      e.target.textContent = r.kind === "podcast" ? "starting\u2026" : "sending\u2026";
      const payload = r.kind === "podcast"
        ? {
            duration_minutes: r.duration_minutes || 15,
            language: r.language || "auto",
            style: r.style || "informative",
            format: r.format || "solo",
            view: r.view || "personal",
            max_stories: r.max_stories || null,
            fact_check: r.fact_check !== false,
            notify: r.notify !== false,
            script_only: !!r.script_only,
          }
        : r.kind === "brief"
          ? { view: r.view || "personal", max_stories: r.max_stories || 5 }
          : {};
      const res = await fetch(`/api/reports/${encodeURIComponent(r.kind)}/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      e.target.disabled = false;
      e.target.textContent = idleLabel;
      if (Auth.unauth(res)) return;
      Auth.toast(res.ok ? (r.kind === "podcast" ? "Podcast generation started." : "Report sent.") : `Failed: ${(await res.json().catch(() => ({}))).detail || res.status}`);
    };
  });
  document.getElementById("add-report").onclick = () => {
    reports.push({ kind: "pipeline", time: "07:00", days: [0, 1, 2, 3, 4, 5, 6], enabled: true });
    renderReports();
  };
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const list = (name) => form.elements[name].value.split(",").map((s) => s.trim()).filter(Boolean);
  const minQ = parseFloat(form.elements.min_quality.value);
  const saved = await api("PUT", "/api/profile", {
    preferences: {
      interest_regions: list("interest_regions"),
      locations: list("locations"),
      min_quality: isNaN(minQ) ? 0 : Math.min(10, Math.max(0, minQ)),
      ntfy_reports: ntfy ? reports.filter((r) => r.time) : undefined,
      excluded_feeds: [...excluded].map(Number),
      about: form.elements.about.value.trim(),
      home_city: form.elements.home_city.value.trim(),
      home_country: form.elements.home_country.value.trim(),
      language: form.elements.language.value.trim().toLowerCase(),
      show_votes: form.elements.show_votes.checked,
      semantic_ranking: form.elements.semantic_ranking.checked,
    },
    tag_weights: weights,
    source_weights: catOverrides,
    feed_weights: feedOverrides,
  });
  if (!saved) return;
  Auth.setTagWeights(saved.tag_weights);
  Auth.applyPrefs(saved.preferences);
  Auth.toast("Settings saved.");
});

// ==================== Account (Password, Shares, Sign out) ====================
document.getElementById("pw-change").onclick = async () => {
  const cur = document.getElementById("pw-current");
  const nw = document.getElementById("pw-new");
  if (nw.value.length < 8) return Auth.toast("New password too short (min. 8).");
  const res = await fetch("/api/auth/password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current: cur.value, new: nw.value }),
  });
  if (Auth.unauth(res)) return;
  Auth.toast(res.ok ? "Password changed." : res.status === 429 ? "Too many attempts — wait a minute." : "Current password is wrong.");
  if (res.ok) { cur.value = ""; nw.value = ""; }
};

document.getElementById("signout-btn").onclick = async () => {
  await Auth.signOut();
  location.href = "/";
};

async function loadShares() {
  const list = document.getElementById("share-list");
  const data = await api("GET", "/api/share");
  if (!data) { list.textContent = "(unavailable)"; return; }
  const links = data.links || [];
  if (!links.length) { list.textContent = "You haven't shared anything yet."; return; }
  list.classList.remove("muted");
  list.innerHTML = links.map((l) => `
    <div class="share-row">
      <a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.title)}</a>
      <span class="muted" title="${esc(fmtUtc(l.created_at))}">${esc(timeAgo(l.created_at))}</span>
      <button type="button" class="link" data-token="${esc(l.token)}">revoke</button>
    </div>`).join("");
  list.querySelectorAll("button[data-token]").forEach((b) => {
    b.onclick = async () => {
      if (!(await api("DELETE", `/api/share/${encodeURIComponent(b.dataset.token)}`))) return;
      b.closest(".share-row").remove();
      if (!list.children.length) list.textContent = "You haven't shared anything yet.";
      Auth.toast("Link revoked.");
    };
  });
}

// ==================== Admin: Users ====================
const userListEl = document.getElementById("user-list");
const addUserForm = document.getElementById("add-user");

function renderUsers(users) {
  const me = Auth.state.user?.id;
  userListEl.innerHTML = `<table class="feeds users">
    <thead><tr><th>User</th><th>Role</th><th>Votes</th><th>Read</th><th>Chats</th><th>Created</th><th></th></tr></thead>
    <tbody>${users.map((u) => `
      <tr data-id="${Number(u.id)}">
        <td><b>${esc(u.name)}</b>${u.id === me ? ` <span class="muted">(you)</span>` : ""}${u.id === 1 ? ` <span class="muted">owner</span>` : ""}</td>
        <td><button type="button" class="toggle role" aria-pressed="${!!u.is_admin}"
              ${u.id === 1 || u.id === me ? "disabled" : ""} title="${u.is_admin ? "Demote to user" : "Promote to admin"}">
              <span class="toggle-track" aria-hidden="true"></span>${u.is_admin ? "admin" : "user"}</button></td>
        <td>${u.votes}</td><td>${u.reads}</td><td>${u.chats}</td>
        <td class="muted nowrap">${esc(fmtShort(u.created_at))}</td>
        <td class="nowrap user-actions">
          <form class="reset-form" hidden>
            <input type="password" name="password" placeholder="New password (min. 8)" minlength="8" autocomplete="new-password" aria-label="New password for ${esc(u.name)}" />
            <button type="submit" class="btn">Save</button>
            <button type="button" class="btn reset-cancel">Cancel</button>
          </form>
          <button type="button" class="btn reset">Set password</button>
          <button type="button" class="btn del" ${u.id === 1 || u.id === me ? "disabled" : ""}>Delete</button>
        </td>
      </tr>`).join("")}</tbody></table>`;

  userListEl.querySelectorAll("tr[data-id]").forEach((row) => {
    const id = Number(row.dataset.id);
    const u = users.find((x) => x.id === id);
    row.querySelector(".role").onclick = async () => {
      const d = await api("PATCH", `/api/admin/users/${id}`, { is_admin: !u.is_admin });
      if (d) renderUsers(d.users);
    };
    const resetBtn = row.querySelector(".reset");
    const resetForm = row.querySelector(".reset-form");
    const pwInput = resetForm.elements.password;
    const closeReset = () => { resetForm.hidden = true; resetForm.reset(); resetBtn.hidden = false; };
    resetBtn.onclick = () => { resetBtn.hidden = true; resetForm.hidden = false; pwInput.focus(); };
    row.querySelector(".reset-cancel").onclick = closeReset;
    pwInput.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); closeReset(); resetBtn.focus(); }
    });
    resetForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const pw = pwInput.value;
      if (pw.length < 8) return Auth.toast("Password too short (min. 8).");
      const d = await api("PATCH", `/api/admin/users/${id}`, { password: pw });
      if (d) { renderUsers(d.users); Auth.toast(`Password of ${u.name} changed.`); }
    });
    row.querySelector(".del").onclick = async () => {
      if (!confirm(`Delete ${u.name}? Their preferences, votes, read state, podcasts and chats are removed.`)) return;
      const d = await api("DELETE", `/api/admin/users/${id}`);
      if (d) renderUsers(d.users);
    };
  });
}

async function loadUsers() {
  const d = await api("GET", "/api/admin/users");
  if (!d) return;
  loadedUsers = true;
  renderUsers(d.users);
}

addUserForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = addUserForm.elements;
  const d = await api("POST", "/api/admin/users", {
    name: f.name.value.trim(), password: f.password.value, is_admin: f.is_admin.checked,
  });
  if (!d) return;
  addUserForm.reset();
  renderUsers(d.users);
  Auth.toast("User created.");
});

// ==================== Admin: Tag Synonyms ====================
const aliasCountEl = document.getElementById("tag-alias-count");
const aliasListEl = document.getElementById("tag-alias-list");
const approveHighBtn = document.getElementById("approve-high-btn");
const approveAllBtn = document.getElementById("approve-all-btn");

async function loadAliases() {
  const d = await api("GET", "/api/admin/tag-aliases?status=pending&limit=100");
  if (!d) return;
  loadedSynonyms = true;
  pendingAliases = d.aliases || [];
  const pending = d.counts?.pending || 0;
  synCountEl.textContent = pending ? String(pending) : "";
  aliasCountEl.textContent = `(${pending} pending · ${d.counts?.approved || 0} approved)`;
  const highCount = pendingAliases.filter((a) => (a.similarity || 0) >= 0.88 && (a.confidence || 0) >= 0.90).length;
  approveHighBtn.hidden = highCount === 0;
  approveHighBtn.textContent = `Approve ≥ 0.88 (${highCount})`;
  approveAllBtn.hidden = pendingAliases.length === 0;
  if (!pendingAliases.length) {
    aliasListEl.innerHTML = '<div class="empty">No pending tag synonyms to review.</div>';
    return;
  }
  aliasListEl.innerHTML = `<table class="feeds">
    <thead><tr><th>Alias</th><th>Canonical</th><th>Relation</th><th>Sim / Conf</th><th></th></tr></thead>
    <tbody>${pendingAliases.map((a) => `
      <tr data-alias="${esc(a.alias)}" data-canonical="${esc(a.canonical)}">
        <td>${esc(a.alias)}</td>
        <td><b>${esc(a.canonical)}</b></td>
        <td class="muted">${esc(a.relation || a.method || "")}</td>
        <td class="muted nowrap">${a.similarity != null ? Number(a.similarity).toFixed(2) : "—"} / ${a.confidence != null ? Number(a.confidence).toFixed(2) : "—"}</td>
        <td class="nowrap">
          <button type="button" class="btn approve">Approve</button>
          <button type="button" class="btn reject">Reject</button>
        </td>
      </tr>`).join("")}</tbody></table>`;
  aliasListEl.querySelectorAll("tr[data-alias]").forEach((row) => {
    const payload = (status) => ({ alias: row.dataset.alias, canonical: row.dataset.canonical, status });
    row.querySelector(".approve").onclick = async () => {
      if (await api("POST", "/api/admin/tag-aliases/review", payload("approved"))) loadAliases();
    };
    row.querySelector(".reject").onclick = async () => {
      if (await api("POST", "/api/admin/tag-aliases/review", payload("rejected"))) loadAliases();
    };
  });
}

async function bulkApprove(items) {
  if (!items.length) return;
  const res = await api("POST", "/api/admin/tag-aliases/review-batch", {
    items: items.map((a) => ({ alias: a.alias, canonical: a.canonical, status: "approved" })),
  });
  if (res) {
    Auth.toast(`Approved ${res.changed} tag synonym(s).`);
    loadAliases();
  }
}

approveHighBtn.onclick = () => bulkApprove(
  pendingAliases.filter((a) => (a.similarity || 0) >= 0.88 && (a.confidence || 0) >= 0.90)
);
approveAllBtn.onclick = () => {
  if (confirm(`Approve all ${pendingAliases.length} shown tag synonyms?`)) bulkApprove(pendingAliases);
};

// ==================== Boot ====================
(async function boot() {
  await Auth.init();
  if (!Auth.state.authenticated) {
    location.href = "/";
    return;
  }
  const user = Auth.state.user;
  statusEl.textContent = user?.is_admin ? `${user.name} · admin` : (user?.name || "");
  if (user?.is_admin) {
    tabsEl.querySelectorAll(".admin-tab").forEach((t) => { t.hidden = false; });
  }

  const p = await api("GET", "/api/profile");
  if (!p) return;
  weights = { ...(p.tag_weights || {}) };
  reports = (p.ntfy_reports || []).map((r) => ({ ...r, days: [...(r.days || [])] }));
  feedOverrides = { ...(p.feed_weights || {}) };
  catOverrides = { ...(p.source_weights || {}) };
  excluded = new Set(((p.preferences || {}).excluded_feeds || []).map(String));
  ntfy = p.ntfy || null;

  renderProfileFields(p.preferences || {}, p.home_defaults || {});
  renderWeights();
  renderLearned(p.inferred_weights || {});
  renderTrust(p.source_trust_default || {}, p.feed_trust || []);
  if (ntfy) renderReports();
  loadShares();

  if (user?.is_admin) {
    // Pre-fetch pending synonym count for the tab badge.
    loadAliases();
  }

  const hash = location.hash.replace(/^#/, "");
  switchTab(isAllowedTab(hash) ? hash : "profile", { pushHash: false });
})();
