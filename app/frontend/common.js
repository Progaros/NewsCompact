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

// Copies an absolute link for `path`; the clipboard API needs HTTPS, so fall back to a prompt.
async function copyLink(path) {
  const url = new URL(path, location.origin).href;
  try {
    await navigator.clipboard.writeText(url);
    if (typeof Auth !== "undefined") Auth.toast("Link copied.");
  } catch {
    prompt("Copy this link:", url);
  }
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
