/* Shared auth UI: session state, header sign-in/out, login modal, toasts.
 * Loaded on both pages before app.js / feeds.js. Backend: app/auth.py. */

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

  function esc(s) {
    return (s ?? "").toString().replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
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
    const btn = document.getElementById("auth-btn");
    if (!controls) return;
    if (!state.authEnabled) {
      if (btn) btn.remove();
      return;
    }
    if (!btn) {
      const b = document.createElement("button");
      b.id = "auth-btn";
      b.className = "btn";
      controls.appendChild(b);
    }
    const el = document.getElementById("auth-btn");
    if (state.authenticated) {
      el.textContent = "\u{1F464} User";
      el.title = "Preferences & sign out";
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

  /* Read-only view of the user's profile (preferences + tag weights) with a
   * sign-out action. Shown via the header User button when signed in. */
  function showUserModal() {
    if (document.getElementById("user-modal")) return;
    const overlay = document.createElement("div");
    overlay.id = "user-modal";
    overlay.innerHTML = `
      <div class="login-panel user-panel">
        <h2>Preferences</h2>
        <p class="hint">Your current profile (read-only).</p>
        <div id="user-profile" class="user-profile">Loading\u2026</div>
        <div class="login-row">
          <button type="button" class="btn" id="user-close">Close</button>
          <button type="button" class="btn" id="user-signout">Sign out</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const close = () => overlay.remove();
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
    overlay.querySelector("#user-close").onclick = close;
    overlay.querySelector("#user-signout").onclick = () => {
      close();
      signOut();
    };
    const box = overlay.querySelector("#user-profile");
    fetch("/api/profile")
      .then((res) => {
        if (Auth.unauth(res)) { close(); return null; }
        return res.json();
      })
      .then((d) => {
        if (d && box) box.innerHTML = renderProfile(d);
      })
      .catch(() => { if (box) box.textContent = "(profile unavailable)"; });
  }

  function renderProfile(p) {
    const prefs = p.preferences || {};
    const prefRows = Object.entries(prefs)
      .map(([k, v]) => `<div class="pref-row"><span>${esc(k)}</span><span>${esc(v)}</span></div>`)
      .join("");
    const tags = Object.entries(p.tag_weights || {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, 30)
      .map(([t, w]) => `<div class="pref-row"><span>${esc(t)}</span><span>${Number(w).toFixed(2)}</span></div>`)
      .join("");
    const tagBlock = tags || `<div class="muted">No tag weights yet.</div>`;
    return `
      <div class="user-section">
        <div class="user-section-label">Preferences</div>${prefRows}
      </div>
      <div class="user-section">
        <div class="user-section-label">Tag weights (${Object.keys(p.tag_weights || {}).length})</div>${tagBlock}
      </div>`;
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
