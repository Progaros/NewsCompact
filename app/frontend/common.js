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
