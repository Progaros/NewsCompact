/* Shared auth UI: session state, header sign-in/out, login modal, toasts.
 * Loaded on every page after common.js. Backend: app/auth.py. */

const Auth = (() => {
  const TOAST_MS = 8000; // toasts auto-dismiss after this (spec: 5–10 s)

  const state = { authenticated: false, user: null };
  const listeners = [];
  let toastTimer = null;
  const tagWeights = {}; // tag -> weight, cached so article tag buttons can react (see addTagWeight)
  const tagListeners = [];

  async function init() {
    try {
      const res = await fetch("/api/auth/session");
      if (res.ok) {
        const d = await res.json();
        state.authenticated = !!d.authenticated;
        state.user = d.user || null;
      }
    } catch {
      // network hiccup at boot — stay unsigned in, no UI harm
    }
    applyDom();
    _notify();
    if (state.authenticated) await loadTagWeights();
  }

  /* Tag weights outside the prefs modal: cached on boot + after every change so the
   * article tag buttons can show "already in my list" and offer "add to my list". */
  function setTagWeights(weights) {
    for (const k of Object.keys(tagWeights)) delete tagWeights[k];
    Object.assign(tagWeights, weights || {});
    _tagsNotify();
  }

  function loadTagWeights() {
    return fetch("/api/profile")
      .then((res) => (res.ok ? res.json() : null))
      .then((p) => {
        if (!p) return;
        setTagWeights(p.tag_weights);
        applyPrefs(p.preferences);
      })
      .catch(() => {});
  }

  /* Preferences that change the page itself: 👍/👎 can be hidden (show_votes). */
  function applyPrefs(prefs) {
    document.body.classList.toggle("hide-votes", (prefs || {}).show_votes === false);
  }

  function _tagsNotify() {
    for (const fn of tagListeners) {
      try { fn(tagWeights); } catch { /* listener bugs never break the feed */ } }
  }

  function onTagWeights(fn) { tagListeners.push(fn); }

  function tagWeight(tag) { return tagWeights[tag]; }

  function hasTagWeight(tag) { return Object.prototype.hasOwnProperty.call(tagWeights, tag); }

  /* PUT replaces the whole tag_weights map — read-back keeps the cache canonical.
   * weight=null deletes. */
  async function addTagWeight(tag, weight) {
    const next = { ...tagWeights };
    if (weight === null || weight === undefined) delete next[tag];
    else next[tag] = Math.max(-10, Math.min(10, Number(weight) || 0));
    const res = await fetch("/api/profile", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tag_weights: next }) });
    if (res.ok) {
      const p = await res.json();
      setTagWeights(p.tag_weights);
    }
    return res.ok;
  }

  function onChange(fn) {
    listeners.push(fn);
  }

  function _notify() {
    for (const fn of listeners) {
      try { fn(state); } catch { /* listener bugs never break the toast flow */ }
    }
  }

  /* body class makes the page read-only for a not-signed-in viewer:
   * hides 👍/👎 (`.interaction`); `.is-admin` shows the management links (Feeds, Users). */
  function applyDom() {
    document.body.classList.toggle("unsigned-in", !state.authenticated);
    document.body.classList.toggle("is-admin", !!(state.authenticated && state.user?.is_admin));
    renderHeader();
  }

  function renderHeader() {
    const controls = document.querySelector(".topbar .controls");
    if (!controls) return;
    let el = document.getElementById("auth-btn");
    if (!el) {
      el = document.createElement("button");
      el.id = "auth-btn";
      el.type = "button";
      el.className = "btn";
      controls.appendChild(el);
    }
    if (state.authenticated) {
      const name = state.user?.name || "Preferences";
      el.innerHTML = `<span aria-hidden="true">\u2699</span> <span class="btn-label">${esc(name)}</span>`;
      el.title = "Preferences & sign out";
      el.setAttribute("aria-label", "Preferences");
      el.onclick = () => showUserModal();
    } else {
      el.textContent = "Sign in";
      el.title = "";
      el.removeAttribute("aria-label");
      el.onclick = () => showLoginModal();
    }
  }

  async function signOut() {
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } catch { /* ignore */ }
    state.authenticated = false;
    state.user = null;
    for (const k of Object.keys(tagWeights)) delete tagWeights[k];
    _tagsNotify();
    applyPrefs({});
    applyDom();
    _notify();
  }

  /* Preferences editor: ranking inputs (regions/locations feed user_impact,
   * min quality hides weak stories) and the learned tag weights. */
  function showUserModal() {
    if (document.getElementById("user-modal")) return;
    const overlay = document.createElement("div");
    overlay.id = "user-modal";
    overlay.innerHTML = `
      <form class="login-panel user-panel" id="prefs-form">
        <h2>Preferences${state.user ? ` <span class="muted">· ${esc(state.user.name)}</span>` : ""}</h2>
        <div id="user-profile" class="user-profile">Loading\u2026</div>
        <div class="login-row">
          <button type="button" class="btn" id="user-signout">Sign out</button>
          <span class="spacer"></span>
          <button type="button" class="btn" id="user-close">Cancel</button>
          <button type="submit" class="btn primary">Save</button>
        </div>
      </form>`;
    document.body.appendChild(overlay);
    const form = overlay.querySelector("form");
    const box = overlay.querySelector("#user-profile");
    const close = () => overlay.remove();
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    overlay.querySelector("#user-close").onclick = close;
    overlay.querySelector("#user-signout")?.addEventListener("click", () => { close(); signOut(); });

    let weights = {};
    let reports = [];
    let feedOverrides = {};
    let excluded = new Set();
    let ntfy = null;
    fetch("/api/profile")
      .then((res) => (unauth(res) ? null : res.json()))
      .then((p) => {
        if (!p) return close();
        weights = { ...(p.tag_weights || {}) };
        reports = (p.ntfy_reports || []).map((r) => ({ ...r, days: [...(r.days || [])] }));
        feedOverrides = { ...(p.feed_weights || {}) };
        excluded = new Set(((p.preferences || {}).excluded_feeds || []).map(String));
        ntfy = p.ntfy || null; // admins only
        box.innerHTML = renderProfileForm(p.preferences || {}, ntfy, p.home_defaults || {});
        renderTrust(box, p.source_trust || {}, p.feed_trust || [], feedOverrides, excluded);
        if (ntfy) renderReports(box, reports, ntfy);
        renderWeights(box, weights);
        renderLearned(box, p.inferred_weights || {}, weights);
        renderPassword(box);
        renderShares(box);
      })
      .catch(() => { box.textContent = "(profile unavailable)"; });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const list = (name) => form.elements[name].value.split(",").map((s) => s.trim()).filter(Boolean);
      const minQ = parseFloat(form.elements.min_quality.value);
      const sourceWeights = {};
      form.querySelectorAll("input[data-trust]").forEach((i) => { sourceWeights[i.dataset.trust] = parseFloat(i.value); });
      const res = await fetch("/api/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
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
          },
          tag_weights: weights,
          source_weights: sourceWeights,
          feed_weights: feedOverrides,
        }),
      });
      if (unauth(res)) return;
      if (!res.ok) return toast(`saving failed: ${res.status}`);
      const saved = await res.json().catch(() => null);
      if (saved) {
        setTagWeights(saved.tag_weights);
        applyPrefs(saved.preferences);
      }
      close();
      toast("Preferences saved.");
      _notify();
    });
  }

  function renderProfileForm(prefs, ntfy, home = {}) {
    const join = (v) => (Array.isArray(v) ? v.join(", ") : "");
    return `
      <label class="field">About you <span class="hint">a few words about yourself (job, interests, where you live) — used to make your heads-up and story chat more personal; never shared</span>
        <textarea name="about" rows="3" maxlength="1000" placeholder="e.g. Software developer in Munich, follows AI, security and local politics">${esc(prefs.about || "")}</textarea></label>
      <div class="field-row">
        <label class="field">Home city <span class="hint">stories there rank higher</span>
          <input name="home_city" maxlength="80" value="${esc(prefs.home_city || "")}" placeholder="${esc(home.home_city || "")}" /></label>
        <label class="field">Country
          <input name="home_country" maxlength="80" value="${esc(prefs.home_country || "")}" placeholder="${esc(home.home_country || "")}" /></label>
        <label class="field">Language <span class="hint">code</span>
          <input name="language" maxlength="3" pattern="[A-Za-z]{2,3}" value="${esc(prefs.language || "")}" placeholder="${esc(home.language || "")}" /></label>
      </div>
      <label class="field">Interest regions <span class="hint">comma-separated; stories whose tags name one rank higher in “For you” (location score)</span>
        <input name="interest_regions" value="${esc(join(prefs.interest_regions))}" placeholder="europe, bavaria" /></label>
      <label class="field">Locations <span class="hint">comma-separated, same effect</span>
        <input name="locations" value="${esc(join(prefs.locations))}" placeholder="munich" /></label>
      <label class="field">Minimum story quality <span class="hint">0 = show everything</span>
        <input name="min_quality" type="number" min="0" max="10" step="0.5" value="${esc(prefs.min_quality ?? 0)}" /></label>
      <label class="check pref-check"><input type="checkbox" name="show_votes" ${prefs.show_votes === false ? "" : "checked"} />
        Show \u{1F44D}/\u{1F44E} on stories <span class="hint">votes teach "For you" a little; your tag list below is never changed by them</span></label>
      <div class="user-section">
        <div class="user-section-label">Source trust</div>
        <p class="hint">How much a source counts as independent confirmation (credibility) and how much its articles weigh in story quality. 0 = ignore, 1 = full. Expand a category to set single sources; ⊘ hides a source from your “For you” feed.</p>
        <div id="trust"></div>
      </div>${ntfy ? `
      <div class="user-section">
        <div class="user-section-label">Scheduled ntfy reports <button type="button" class="link" id="add-report">+ add</button></div>
        <p class="hint">${ntfy.enabled ? `Server time now ${esc(ntfy.server_time)}.` : "ntfy is not configured on the server (NEWS_HEADS_UP_NTFY_URL)."}</p>
        <div id="reports"></div>
      </div>` : ""}
      <div class="user-section">
        <div class="user-section-label">Tag list <button type="button" class="link" id="reset-weights">reset all</button></div>
        <p class="hint">Your own weights (\u221210 \u2026 10): 1 = a bit, 3 = clearly, 10 = must see; \u221210 mutes a tag in \u201cFor you\u201d. Set them here or by clicking a tag on a story.</p>
        <div id="weights"></div>
      </div>
      <div class="user-section">
        <div class="user-section-label">Learned interests</div>
        <p class="hint">From your \u{1F44D}/\u{1F44E} and story chats; they fade over weeks and count less for common tags. Click one to put it on your tag list.</p>
        <div id="learned"></div>
      </div>`;
  }

  const DAY_NAMES = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

  // Own password (not part of the Save button: it takes effect at once).
  function renderPassword(box) {
    const sec = document.createElement("div");
    sec.className = "user-section";
    sec.innerHTML = `<div class="user-section-label">Password</div>
      <div class="pw-row">
        <input type="password" class="pw-current" placeholder="Current password" autocomplete="current-password" />
        <input type="password" class="pw-new" placeholder="New password (min. 8)" minlength="8" autocomplete="new-password" />
        <button type="button" class="btn">Change</button>
      </div>`;
    box.appendChild(sec);
    sec.querySelector("button").onclick = async () => {
      const cur = sec.querySelector(".pw-current");
      const nw = sec.querySelector(".pw-new");
      if (nw.value.length < 8) return toast("New password too short (min. 8).");
      const res = await fetch("/api/auth/password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current: cur.value, new: nw.value }),
      });
      if (unauth(res)) return;
      toast(res.ok ? "Password changed." : res.status === 429 ? "Too many attempts — wait a minute." : "Current password is wrong.");
      if (res.ok) { cur.value = ""; nw.value = ""; }
    };
  }
  // Own share links: anyone with a link can read the snapshot until it is revoked here.
  async function renderShares(box) {
    const sec = document.createElement("div");
    sec.className = "user-section";
    sec.innerHTML = `<div class="user-section-label">Shared links</div><div class="share-list muted">Loading…</div>`;
    box.appendChild(sec);
    const list = sec.querySelector(".share-list");
    const res = await fetch("/api/share");
    if (unauth(res) || !res.ok) { list.textContent = "(unavailable)"; return; }
    const { links } = await res.json();
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
        const r = await fetch(`/api/share/${encodeURIComponent(b.dataset.token)}`, { method: "DELETE" });
        if (unauth(r)) return;
        if (!r.ok) return toast(`revoking failed: ${r.status}`);
        b.closest(".share-row").remove();
        if (!list.children.length) list.textContent = "You haven't shared anything yet.";
        toast("Link revoked.");
      };
    });
  }

  const REPORT_LABELS = { brief: "News brief", pipeline: "Pipeline report" };

  /* Category sliders with expandable per-source sliders. `overrides` ({feed id: trust})
   * is edited in place; a category with individually set sources is faded and moving
   * its slider asks before overwriting them. `excluded` (feed ids) hides sources from "For you". */
  function renderTrust(box, cats, feeds, overrides, excluded) {
    const el = box.querySelector("#trust");
    const fmt = (v) => Number(v).toFixed(1);
    const byCat = {};
    for (const f of feeds) (byCat[f.category] ||= []).push(f);
    const slider = (attrs, v) => `<input type="range" min="0" max="1" step="0.1" value="${esc(v)}" ${attrs} /><output>${fmt(v)}</output>`;
    el.innerHTML = Object.keys(cats).sort((a, b) => a.localeCompare(b)).map((cat) => `
      <div class="trust-group" data-cat="${esc(cat)}">
        <div class="trust-row">
          <button type="button" class="trust-toggle" aria-expanded="false" title="Show single sources">
            <span class="chev" aria-hidden="true"></span>${esc(cat || "(uncategorized)")} <span class="muted">${(byCat[cat] || []).length}</span> <span class="hidden-count"></span></button>
          ${slider(`data-trust="${esc(cat)}" aria-label="Trust of ${esc(cat || "uncategorized")}"`, cats[cat])}
        </div>
        <div class="trust-feeds" hidden>${(byCat[cat] || []).map((f) => `
          <div class="trust-row feed" data-feed="${Number(f.id)}">
            <span class="trust-name" title="${esc(f.title)}"><button type="button" class="link exclude-btn" data-exclude title="Hide this source from your feed">\u2298</button>${esc(f.title)}</span>
            ${slider(`data-feed-id="${Number(f.id)}" aria-label="Trust of ${esc(f.title)}"`, f.trust)}
            <button type="button" class="link" data-reset title="Follow the category again">\u21ba</button>
          </div>`).join("")}</div>
      </div>`).join("") || `<div class="muted">No feeds yet.</div>`;

    el.querySelectorAll(".trust-group").forEach((g) => {
      const cat = g.dataset.cat;
      const catInput = g.querySelector("input[data-trust]");
      const feedRows = [...g.querySelectorAll(".trust-row.feed")];
      let prev = catInput.value;
      const set = (input, v) => { input.value = v; input.nextElementSibling.textContent = fmt(v); };
      const refresh = () => {
        let custom = 0;
        let hidden = 0;
        for (const row of feedRows) {
          const on = row.dataset.feed in overrides;
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
        g.classList.toggle("faded", custom > 0);
        g.querySelector(".hidden-count").textContent = hidden ? `${hidden} hidden` : "";
        catInput.title = custom ? `${custom} source(s) set individually — moving this resets them` : "";
      };
      const follow = () => feedRows.forEach((row) => {
        if (!(row.dataset.feed in overrides)) set(row.querySelector("input"), catInput.value);
      });
      g.querySelector(".trust-toggle").onclick = (e) => {
        const list = g.querySelector(".trust-feeds");
        list.hidden = !list.hidden;
        e.currentTarget.setAttribute("aria-expanded", !list.hidden);
      };
      catInput.addEventListener("input", () => {
        catInput.nextElementSibling.textContent = fmt(catInput.value);
        if (!g.classList.contains("faded")) { follow(); prev = catInput.value; }
      });
      catInput.addEventListener("change", () => {
        if (!g.classList.contains("faded")) return;
        const n = feedRows.length;
        if (!confirm(`Set all ${n} sources in “${cat || "(uncategorized)"}” to ${fmt(catInput.value)}? This replaces their individual values.`)) {
          set(catInput, prev);
          return;
        }
        for (const row of feedRows) delete overrides[row.dataset.feed];
        prev = catInput.value;
        follow();
        refresh();
      });
      for (const row of feedRows) {
        const input = row.querySelector("input");
        input.addEventListener("input", () => {
          input.nextElementSibling.textContent = fmt(input.value);
          overrides[row.dataset.feed] = parseFloat(input.value);
          refresh();
        });
        row.querySelector("[data-reset]").onclick = () => {
          delete overrides[row.dataset.feed];
          set(input, catInput.value);
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

  function renderReports(box, reports, ntfy) {
    const el = box.querySelector("#reports");
    const kinds = Object.keys(ntfy.kinds || {}).map((k) => [k, REPORT_LABELS[k] || ntfy.kinds[k]]);
    el.innerHTML = reports.length ? reports.map((r, i) => `
      <div class="report-row${r.enabled === false ? " off" : ""}" data-i="${i}">
        <input type="checkbox" data-f="enabled" title="Enabled" ${r.enabled === false ? "" : "checked"} />
        <select data-f="kind">${kinds.map(([k, label]) => `<option value="${esc(k)}" ${k === r.kind ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>
        <input type="time" data-f="time" value="${esc(r.time)}" required />
        <span class="days">${DAY_NAMES.map((d, di) => `<button type="button" class="day${r.days.includes(di) ? " on" : ""}" data-day="${di}" aria-pressed="${r.days.includes(di)}">${d}</button>`).join("")}</span>
        <button type="button" class="link" data-act="send" title="Send this report now" ${ntfy.enabled ? "" : "disabled"}>send now</button>
        <button type="button" class="link" data-act="del" title="Remove">\u00d7</button>
      </div>`).join("")
      : `<div class="muted">No scheduled reports.</div>`;
    el.querySelectorAll(".report-row").forEach((row) => {
      const r = reports[Number(row.dataset.i)];
      row.querySelector('[data-f="enabled"]').onchange = (e) => { r.enabled = e.target.checked; row.classList.toggle("off", !r.enabled); };
      row.querySelector('[data-f="kind"]').onchange = (e) => { r.kind = e.target.value; };
      row.querySelector('[data-f="time"]').onchange = (e) => { r.time = e.target.value; };
      row.querySelectorAll(".day").forEach((b) => b.addEventListener("click", () => {
        const d = Number(b.dataset.day);
        r.days = r.days.includes(d) ? r.days.filter((x) => x !== d) : [...r.days, d].sort();
        b.classList.toggle("on", r.days.includes(d));
        b.setAttribute("aria-pressed", r.days.includes(d));
      }));
      row.querySelector('[data-act="del"]').onclick = () => { reports.splice(Number(row.dataset.i), 1); renderReports(box, reports, ntfy); };
      row.querySelector('[data-act="send"]').onclick = async (e) => {
        e.target.disabled = true;
        e.target.textContent = "sending\u2026";
        const res = await fetch(`/api/reports/${encodeURIComponent(r.kind)}/send`, { method: "POST" });
        e.target.disabled = false;
        e.target.textContent = "send now";
        if (unauth(res)) return;
        toast(res.ok ? "Report sent." : `Sending failed: ${(await res.json().catch(() => ({}))).detail || res.status}`);
      };
    });
    box.querySelector("#add-report").onclick = () => {
      reports.push({ kind: "pipeline", time: "07:00", days: [0, 1, 2, 3, 4, 5, 6], enabled: true });
      renderReports(box, reports, ntfy);
    };
  }

  // Same spelling rules as app/tags.py (_clean); the server adds its aliases on save.
  const DIACRITICS = { "ä": "a", "ö": "o", "ü": "u", "ß": "ss", "é": "e", "è": "e", "ê": "e", "ë": "e",
    "à": "a", "ù": "u", "ç": "c", "ñ": "n", "å": "a", "ø": "o", "æ": "ae" };
  function normalizeTag(tag) {
    return String(tag).trim().toLowerCase().replace(/[äöüßéèêëàùçñåøæ]/g, (c) => DIACRITICS[c])
      .replace(/[^a-z0-9]+/g, " ").trim();
  }

  function renderWeights(box, weights) {
    const el = box.querySelector("#weights");
    const entries = Object.entries(weights).sort((a, b) => b[1] - a[1]);
    el.innerHTML = `<div class="pref-row add-weight">
          <input type="text" class="tag-input" placeholder="add tag, e.g. climate" maxlength="80" aria-label="New tag" />
          <span><input type="number" class="weight-input" min="-10" max="10" step="0.5" value="2" aria-label="New tag weight" />
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
      input.type = "number"; input.min = "-10"; input.max = "10"; input.step = "0.5";
      input.value = weights[tag];
      b.replaceWith(input);
      input.focus(); input.select();
      let done = false;
      const commit = () => {
        if (done) return; done = true;
        const v = Math.max(-10, Math.min(10, parseFloat(input.value)));
        if (!isNaN(v)) weights[tag] = v;
        renderWeights(box, weights);
      };
      input.addEventListener("blur", commit);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); commit(); }
        else if (e.key === "Escape") { e.stopPropagation(); done = true; renderWeights(box, weights); }
      });
    }));
    el.querySelectorAll("button[data-del]").forEach((b) => b.addEventListener("click", () => {
      delete weights[b.dataset.del];
      renderWeights(box, weights);
    }));
    const add = el.querySelector(".add-weight");
    const addTag = () => {
      const tag = normalizeTag(add.querySelector(".tag-input").value);
      const v = Math.max(-10, Math.min(10, parseFloat(add.querySelector(".weight-input").value)));
      if (!tag || isNaN(v)) return;
      weights[tag] = v;
      renderWeights(box, weights);
      box.querySelector(".add-weight .tag-input").focus();
    };
    add.querySelector("[data-add]").onclick = addTag;
    add.querySelectorAll("input").forEach((i) => i.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); addTag(); }
    }));
    box.querySelector("#reset-weights").onclick = () => {
      for (const k of Object.keys(weights)) delete weights[k];
      renderWeights(box, weights);
    };
  }

  /* Read-only learned weights; a click copies one (rounded) into the editable tag list. */
  function renderLearned(box, learned, weights) {
    const el = box.querySelector("#learned");
    const entries = Object.entries(learned).filter(([t]) => !(t in weights))
      .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 24);
    el.innerHTML = entries.length
      ? `<div class="weight-chips">` + entries.map(([t, w]) => `<button type="button" class="weight-chip learned" data-tag="${esc(t)}" title="Add #${esc(t)} to your tag list">
          <span class="w-tag">#${esc(t)}</span><span class="w-val ${w >= 0 ? "pos" : "neg"}">${w >= 0 ? "+" : ""}${w.toFixed(1)}</span></button>`).join("") + `</div>`
      : `<div class="muted">Nothing learned yet.</div>`;
    el.querySelectorAll("button[data-tag]").forEach((b) => b.addEventListener("click", () => {
      const w = learned[b.dataset.tag];
      weights[b.dataset.tag] = Math.max(-10, Math.min(10, Math.round(w) || Math.sign(w)));
      renderWeights(box, weights);
      renderLearned(box, learned, weights);
    }));
  }

  function showLoginModal() {
    if (document.getElementById("login-modal")) return;
    const overlay = document.createElement("div");
    overlay.id = "login-modal";
    overlay.innerHTML = `
      <div class="login-panel">
        <h2>Sign in</h2>
        <p class="hint">Sign in for your personal feed, chat and preferences.</p>
        <form id="login-form">
          <input type="text" name="username" placeholder="User name" maxlength="80"
                 autocomplete="username" autocapitalize="none" spellcheck="false" required />
          <input type="password" name="password" placeholder="Password"
                 autocomplete="current-password" required />
          <div class="login-row">
            <button type="button" class="btn" id="login-cancel">Cancel</button>
            <button type="submit" class="btn">Sign in</button>
          </div>
          <p class="login-error" hidden></p>
        </form>
      </div>`;
    document.body.appendChild(overlay);
    const form = overlay.querySelector("#login-form");
    const err = overlay.querySelector(".login-error");
    const nameInput = form.elements.username;
    const pwInput = form.elements.password;
    nameInput.focus();
    const close = () => overlay.remove();
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
    overlay.querySelector("#login-cancel").onclick = close;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: nameInput.value.trim(), password: pwInput.value }),
      });
      if (res.ok) {
        state.authenticated = true;
        state.user = (await res.json()).user || state.user;
        close();
        applyDom();
        _notify();
        loadTagWeights();
      } else {
        err.hidden = false;
        err.textContent = res.status === 429
          ? "Too many attempts — wait a minute."
          : "Wrong user name or password.";
        pwInput.select();
      }
    });
  }

  /* Call after each API call: on 401 show the login toast once and return
   * true so the caller can bail. Other responses pass through and return false. */
  function unauth(res) {
    if (res && res.status === 401) {
      toast("Not signed in — full access requires login.", {
        label: "Sign in",
        onClick: showLoginModal,
      });
      return true;
    }
    return false;
  }

  function toast(msg, opts = {}) {
    const existing = document.getElementById("app-toast");
    if (existing) existing.remove();
    if (toastTimer) {
      clearTimeout(toastTimer);
      toastTimer = null;
    }
    const el = document.createElement("div");
    el.id = "app-toast";
    el.className = "toast";
    el.textContent = msg;
    if (opts.label) {
      const b = document.createElement("button");
      b.className = "btn";
      b.textContent = opts.label;
      b.onclick = () => {
        el.remove();
        if (opts.onClick) opts.onClick();
      };
      el.appendChild(b);
    }
    document.body.appendChild(el);
    toastTimer = setTimeout(() => el.remove(), TOAST_MS);
  }

  return {
    init,
    onChange,
    onTagWeights,
    hasTagWeight,
    tagWeight,
    addTagWeight,
    setTagWeights,
    unauth,
    toast,
    applyPrefs,
    showLoginModal,
    get state() {
      return state;
    },
  };
})();
