/* Shared helpers for all pages (loaded first). */

function esc(s) {
  return (s ?? "").toString().replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Only http(s) links are rendered as hrefs (feed/LLM content could carry javascript: URLs).
function safeUrl(url) {
  return /^https?:\/\//i.test(url || "") ? url : "#";
}

function hostname(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

// Copies an absolute link for `path`; the clipboard API needs HTTPS, so fall back to a copyable input.
async function copyLink(path) {
  const url = new URL(path, location.origin).href;
  try {
    await navigator.clipboard.writeText(url);
    if (typeof Auth !== "undefined") Auth.toast("Link copied.");
  } catch {
    const ta = document.createElement("textarea");
    ta.value = url;
    ta.style.cssText = "position:fixed;opacity:0;pointer-events:none";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand?.("copy");
    ta.remove();
    if (ok) {
      if (typeof Auth !== "undefined") Auth.toast("Link copied.");
      return;
    }
    showCopyDialog(url);
  }
}

function showCopyDialog(url) {
  document.getElementById("copy-modal")?.remove();
  const overlay = document.createElement("div");
  overlay.id = "copy-modal";
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="login-panel">
      <h2>Copy link</h2>
      <div class="pw-row" style="margin-top:12px">
        <input type="text" readonly value="${esc(url)}" aria-label="Link URL" />
        <button type="button" class="btn" id="copy-close">Close</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const input = overlay.querySelector("input");
  input.focus();
  input.select();
  const close = () => overlay.remove();
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  overlay.querySelector("#copy-close").onclick = close;
}

// Server timestamps are naive UTC "YYYY-MM-DD HH:MM:SS".
function parseUtc(s) {
  if (!s) return NaN;
  // Older rows kept the feed's ISO offset ("…T06:15:00+02:00").
  if (/[zZ]|[+-]\d\d:?\d\d$/.test(s.slice(10))) return Date.parse(s);
  return Date.parse(s.replace(" ", "T") + "Z");
}

function fmtUtc(s) {
  const t = parseUtc(s);
  return isNaN(t) ? (s || "–") : new Date(t).toLocaleString();
}

// Compact "12.10. 10:59"-style timestamp for dense tables.
function fmtShort(s) {
  const t = parseUtc(s);
  if (isNaN(t)) return s || "–";
  return new Date(t).toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function timeAgo(s) {
  const t = parseUtc(s);
  if (isNaN(t)) return s || "–";
  const m = Math.floor(Math.max(0, (Date.now() - t) / 60000));
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

// Seconds → "8.2s" / "3m 05s" / "2h 10m"; null → "–".
function fmtDuration(v) {
  if (v == null || isNaN(Number(v))) return "–";
  const s = Number(v);
  if (s < 60) return `${s.toFixed(1)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")}m`;
}

// Reusable top navigation bar (<news-topbar active="/stats" status-id="stats-status">).
// Replaces itself with <header class="topbar"> synchronously when common.js loads so
// sticky positioning, auth.js (#auth-btn), chat.js (#chats-link) and page scripts all
// see the standard header structure without duplicating markup across HTML pages.
const _TOPBAR_NAV = [
  { href: "/", label: "Stories" },
  { href: "/chats", label: "Chats", id: "chats-link", cls: "signed-only" },
  { href: "/podcasts", label: "Podcasts" },
  { href: "/analytics", label: "Analytics", cls: "signed-only" },
  { href: "/feeds", label: "Feeds", id: "feeds-link", cls: "admin-link" },
];

class NewsTopbar extends HTMLElement {
  connectedCallback() {
    const active = this.getAttribute("active") || "";
    const statusId = this.getAttribute("status-id") || "";
    const minimal = this.hasAttribute("minimal");
    const extra = [...this.childNodes];

    const header = document.createElement("header");
    header.className = "topbar";
    const items = minimal ? _TOPBAR_NAV.slice(0, 1) : _TOPBAR_NAV;
    const links = items.map((item) => {
      const classes = [item.cls, item.href === active ? "active" : ""].filter(Boolean).join(" ");
      const idAttr = item.id ? ` id="${item.id}"` : "";
      const clsAttr = classes ? ` class="${classes}"` : "";
      return `<a href="${item.href}"${idAttr}${clsAttr}>${item.label}</a>`;
    }).join("");
    header.innerHTML =
      `<a href="/" class="brand"><img class="logo" src="/static/logo.svg" alt="" width="28" height="28" /><h1>News</h1></a>`
      + `<nav class="nav">${links}</nav>`;

    if (!minimal) {
      const controls = document.createElement("div");
      controls.className = "controls";
      if (statusId) {
        const span = document.createElement("span");
        span.id = statusId;
        span.className = "status";
        span.textContent = "loading…";
        controls.appendChild(span);
      }
      for (const node of extra) controls.appendChild(node);
      header.appendChild(controls);
    }
    this.replaceWith(header);
  }
}
customElements.define("news-topbar", NewsTopbar);
