/* Shared auth UI: session state, header sign-in/out, login modal, toasts.
 * Loaded on every page after common.js. Backend: app/auth.py. */

const Auth = (() => {
  const TOAST_MS = 8000; // toasts auto-dismiss after this (spec: 5–10 s)

  const state = { authenticated: false, authEnabled: false };
  const listeners = [];
  let toastTimer = null;

  async function init() {
    try {
      const res = await fetch("/api/auth/session");
      if (res.ok) {
        const d = await res.json();
        state.authenticated = !!d.authenticated;
        state.authEnabled = !!d.auth_enabled;
      }
    } catch {
      // network hiccup at boot — stay unsigned in, no UI harm
    }
    applyDom();
    _notify();
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
   * hides 👍/👎 (`.interaction`) and the Feeds subpage link. */
  function applyDom() {
    document.body.classList.toggle("unsigned-in", state.authEnabled && !state.authenticated);
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
    if (!state.authEnabled || state.authenticated) {
      el.innerHTML = `<span aria-hidden="true">\u2699</span><span class="btn-label"> Preferences</span>`;
      el.title = state.authEnabled ? "Preferences & sign out" : "Preferences";
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
        <h2>Preferences</h2>
        <div id="user-profile" class="user-profile">Loading\u2026</div>
        <div class="login-row">
          ${state.authEnabled ? `<button type="button" class="btn" id="user-signout">Sign out</button>` : ""}
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
    let ntfy = { enabled: false, kinds: {} };
    fetch("/api/profile")
      .then((res) => (unauth(res) ? null : res.json()))
      .then((p) => {
        if (!p) return close();
        weights = { ...(p.tag_weights || {}) };
        reports = (p.ntfy_reports || []).map((r) => ({ ...r, days: [...(r.days || [])] }));
        feedOverrides = { ...(p.feed_weights || {}) };
        ntfy = p.ntfy || ntfy;
        box.innerHTML = renderProfileForm(p.preferences || {}, ntfy);
        renderTrust(box, p.source_trust || {}, p.feed_trust || [], feedOverrides);
        renderReports(box, reports, ntfy);
        renderWeights(box, weights);
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
            ntfy_reports: reports.filter((r) => r.time),
          },
          tag_weights: weights,
          source_weights: sourceWeights,
          feed_weights: feedOverrides,
        }),
      });
      if (unauth(res)) return;
      if (!res.ok) return toast(`saving failed: ${res.status}`);
      close();
      toast("Preferences saved.");
      _notify();
    });
  }

  function renderProfileForm(prefs, ntfy) {
    const join = (v) => (Array.isArray(v) ? v.join(", ") : "");
    return `
      <label class="field">Interest regions <span class="hint">comma-separated; matching story tags raise "impact"</span>
        <input name="interest_regions" value="${esc(join(prefs.interest_regions))}" placeholder="europe, bavaria" /></label>
      <label class="field">Locations <span class="hint">comma-separated, same effect</span>
        <input name="locations" value="${esc(join(prefs.locations))}" placeholder="munich" /></label>
      <label class="field">Minimum story quality <span class="hint">0 = show everything</span>
        <input name="min_quality" type="number" min="0" max="10" step="0.5" value="${esc(prefs.min_quality ?? 0)}" /></label>
      <div class="user-section">
        <div class="user-section-label">Source trust</div>
        <p class="hint">How much a source counts as independent confirmation (credibility) and how much its articles weigh in story quality. 0 = ignore, 1 = full. Expand a category to set single sources.</p>
        <div id="trust"></div>
      </div>
      <div class="user-section">
        <div class="user-section-label">Scheduled ntfy reports <button type="button" class="link" id="add-report">+ add</button></div>
        <p class="hint">${ntfy.enabled ? `Server time now ${esc(ntfy.server_time)}.` : "ntfy is not configured on the server (NEWS_HEADS_UP_NTFY_URL)."}</p>
        <div id="reports"></div>
      </div>
      <div class="user-section">
        <div class="user-section-label">Tag weights <button type="button" class="link" id="reset-weights">reset all</button></div>
        <p class="hint">Learned from 👍/👎; edit any value (−10 … 10) or add tags. Positive = more like this.</p>
        <div id="weights"></div>
      </div>`;
  }

  const DAY_NAMES = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
  const REPORT_LABELS = { brief: "News brief", pipeline: "Pipeline report" };

  /* Category sliders with expandable per-source sliders. `overrides` ({feed id: trust})
   * is edited in place; a category with individually set sources is faded and moving
   * its slider asks before overwriting them. */
  function renderTrust(box, cats, feeds, overrides) {
    const el = box.querySelector("#trust");
    const fmt = (v) => Number(v).toFixed(1);
    const byCat = {};
    for (const f of feeds) (byCat[f.category] ||= []).push(f);
    const slider = (attrs, v) => `<input type="range" min="0" max="1" step="0.1" value="${esc(v)}" ${attrs} /><output>${fmt(v)}</output>`;
    el.innerHTML = Object.keys(cats).sort((a, b) => a.localeCompare(b)).map((cat) => `
      <div class="trust-group" data-cat="${esc(cat)}">
        <div class="trust-row">
          <button type="button" class="trust-toggle" aria-expanded="false" title="Show single sources">
            <span class="chev" aria-hidden="true"></span>${esc(cat || "(uncategorized)")} <span class="muted">${(byCat[cat] || []).length}</span></button>
          ${slider(`data-trust="${esc(cat)}" aria-label="Trust of ${esc(cat || "uncategorized")}"`, cats[cat])}
        </div>
        <div class="trust-feeds" hidden>${(byCat[cat] || []).map((f) => `
          <div class="trust-row feed" data-feed="${Number(f.id)}">
            <span class="trust-name" title="${esc(f.title)}">${esc(f.title)}</span>
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
        for (const row of feedRows) {
          const on = row.dataset.feed in overrides;
          row.classList.toggle("custom", on);
          custom += on;
        }
        g.classList.toggle("faded", custom > 0);
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

  function renderWeights(box, weights) {
    const el = box.querySelector("#weights");
    const entries = Object.entries(weights).sort((a, b) => b[1] - a[1]);
    el.innerHTML = `<div class="pref-row add-weight">
          <input type="text" class="tag-input" placeholder="add tag, e.g. climate" maxlength="80" aria-label="New tag" />
          <span><input type="number" class="weight-input" min="-10" max="10" step="0.5" value="2" aria-label="New tag weight" />
          <button type="button" class="link" data-add>add</button></span></div>`
      + (entries.length
        ? entries.map(([t, w]) => `<div class="pref-row"><span>#${esc(t)}</span>
          <span class="${w >= 0 ? "pos" : "neg"}">
          <input type="number" class="weight-input" min="-10" max="10" step="0.5" value="${esc(w)}" data-tag="${esc(t)}" aria-label="Weight of #${esc(t)}" />
          <button type="button" class="link" data-del="${esc(t)}" title="Forget this tag">\u00d7</button></span></div>`).join("")
        : `<div class="muted">No tag weights yet \u2014 use \u{1F44D}/\u{1F44E} on stories or add one above.</div>`);
    el.querySelectorAll("input[data-tag]").forEach((i) => i.addEventListener("change", () => {
      const v = Math.max(-10, Math.min(10, parseFloat(i.value)));
      if (isNaN(v)) { i.value = weights[i.dataset.tag]; return; }
      weights[i.dataset.tag] = v;
      i.value = v;
      i.parentElement.className = v >= 0 ? "pos" : "neg";
    }));
    el.querySelectorAll("button[data-del]").forEach((b) => b.addEventListener("click", () => {
      delete weights[b.dataset.del];
      renderWeights(box, weights);
    }));
    const add = el.querySelector(".add-weight");
    const addTag = () => {
      const tag = add.querySelector(".tag-input").value.trim().replace(/^#/, "").toLowerCase();
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

  function showLoginModal() {
    if (document.getElementById("login-modal")) return;
    const overlay = document.createElement("div");
    overlay.id = "login-modal";
    overlay.innerHTML = `
      <div class="login-panel">
        <h2>Sign in</h2>
        <p class="hint">Enter the app password to get full access.</p>
        <form id="login-form">
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
    const pwInput = form.querySelector("input");
    pwInput.focus();
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
        body: JSON.stringify({ password: pwInput.value }),
      });
      if (res.ok) {
        state.authenticated = true;
        close();
        applyDom();
        _notify();
      } else {
        err.hidden = false;
        err.textContent = res.status === 429
          ? "Too many attempts — wait a minute."
          : "Wrong password.";
        pwInput.select();
      }
    });
  }

  /* Call after each API call: on 401 show the login toast once and return
   * true so the caller can bail. Non-401 / open-mode responses pass through
   * and return false. */
  function unauth(res) {
    if (res && res.status === 401 && state.authEnabled) {
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
    unauth,
    toast,
    showLoginModal,
    get state() {
      return state;
    },
  };
})();
