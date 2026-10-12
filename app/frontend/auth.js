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
      el.textContent = "\u2699 Preferences";
      el.title = state.authEnabled ? "Preferences & sign out" : "Preferences";
      el.onclick = () => showUserModal();
    } else {
      el.textContent = "Sign in";
      el.title = "";
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
    fetch("/api/profile")
      .then((res) => (unauth(res) ? null : res.json()))
      .then((p) => {
        if (!p) return close();
        weights = { ...(p.tag_weights || {}) };
        box.innerHTML = renderProfileForm(p.preferences || {});
        renderWeights(box, weights);
      })
      .catch(() => { box.textContent = "(profile unavailable)"; });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const list = (name) => form.elements[name].value.split(",").map((s) => s.trim()).filter(Boolean);
      const minQ = parseFloat(form.elements.min_quality.value);
      const res = await fetch("/api/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          preferences: {
            interest_regions: list("interest_regions"),
            locations: list("locations"),
            min_quality: isNaN(minQ) ? 0 : Math.min(10, Math.max(0, minQ)),
          },
          tag_weights: weights,
        }),
      });
      if (unauth(res)) return;
      if (!res.ok) return toast(`saving failed: ${res.status}`);
      close();
      toast("Preferences saved.");
      _notify();
    });
  }

  function renderProfileForm(prefs) {
    const join = (v) => (Array.isArray(v) ? v.join(", ") : "");
    return `
      <label class="field">Interest regions <span class="hint">comma-separated; matching story tags raise "impact"</span>
        <input name="interest_regions" value="${esc(join(prefs.interest_regions))}" placeholder="europe, bavaria" /></label>
      <label class="field">Locations <span class="hint">comma-separated, same effect</span>
        <input name="locations" value="${esc(join(prefs.locations))}" placeholder="munich" /></label>
      <label class="field">Minimum story quality <span class="hint">0 = show everything</span>
        <input name="min_quality" type="number" min="0" max="10" step="0.5" value="${esc(prefs.min_quality ?? 0)}" /></label>
      <div class="user-section">
        <div class="user-section-label">Learned tag weights <button type="button" class="link" id="reset-weights">reset all</button></div>
        <div id="weights"></div>
      </div>`;
  }

  function renderWeights(box, weights) {
    const el = box.querySelector("#weights");
    const entries = Object.entries(weights).sort((a, b) => b[1] - a[1]);
    el.innerHTML = entries.length
      ? entries.map(([t, w]) => `<div class="pref-row"><span>#${esc(t)}</span>
          <span class="${w >= 0 ? "pos" : "neg"}">${Number(w).toFixed(1)}
          <button type="button" class="link" data-tag="${esc(t)}" title="Forget this tag">\u00d7</button></span></div>`).join("")
      : `<div class="muted">No tag weights yet \u2014 use \u{1F44D}/\u{1F44E} on stories.</div>`;
    el.querySelectorAll("button[data-tag]").forEach((b) => b.addEventListener("click", () => {
      delete weights[b.dataset.tag];
      renderWeights(box, weights);
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
