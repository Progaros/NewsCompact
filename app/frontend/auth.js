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
      el.className = "nav-link";
      controls.appendChild(el);
    }
    if (state.authenticated) {
      const name = state.user?.name || "Settings";
      el.innerHTML = `<span class="btn-label user-name">\u2699 ${esc(name.charAt(0).toUpperCase() + name.slice(1))}</span>`;
      el.title = "Settings";
      el.setAttribute("aria-label", "Settings");
      el.classList.toggle("active", location.pathname === "/settings");
      el.onclick = () => { location.href = "/settings"; };
    } else {
      el.textContent = "Sign in";
      el.title = "";
      el.removeAttribute("aria-label");
      el.classList.remove("active");
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
    signOut,
    showLoginModal,
    get state() {
      return state;
    },
  };
})();
