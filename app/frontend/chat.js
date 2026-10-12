/* Per-story chat — a page-level popup, per-user (signed-in only), backed by
 * the local CHAT_MODEL. The story's context is injected server-side into the
 * SYSTEM prompt; before answering the agent may run 0..N searchngx web-search
 * turns to gather fresh context. History persists per-story in the DB.
 *
 * Backend: app/chat.py, app/searchngx.py. One popup instance, bound to a
 * single story at a time; reopening for the same story focuses it, a different
 * story switches it. Survives story-card re-renders (it lives on <body>). */

const Chat = (() => {
  let currentStory = null; // story_id the popup is bound to
  let busy = false;        // a reply is in flight (guards double-send)

  // --- markdown (compact, HTML-safe) ---------------------------------------
  function inline(s) {
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return s;
  }

  // The text is already escaped before line handling, so the tags we emit here
  // are the only HTML in the output (no injection).
  function mdToHtml(text) {
    const lines = esc(text).split(/\r?\n/);
    let html = "";
    let inList = false;
    let inCode = false;
    let codeBuf = [];
    const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };
    for (const raw of lines) {
      if (/^```/.test(raw)) {
        if (inCode) { html += `<pre><code>${codeBuf.join("\n")}</code></pre>`; codeBuf = []; inCode = false; }
        else { closeList(); inCode = true; }
        continue;
      }
      if (inCode) { codeBuf.push(raw); continue; }
      if (/^[-*]\s+/.test(raw)) {
        if (!inList) html += "<ul>"; inList = true;
        html += `<li>${inline(raw.replace(/^[-*]\s+/, ""))}</li>`;
      } else if (/^\s*\d+[.)]\s+/.test(raw)) {
        if (!inList) html += "<ul>"; inList = true;
        html += `<li>${inline(raw.replace(/^\s*\d+[.)]\s+/, ""))}</li>`;
      } else if (/^#{1,3}\s+/.test(raw)) {
        closeList();
        html += `<p class="chat-h">${inline(raw.replace(/^#{1,3}\s+/, ""))}</p>`;
      } else if (raw.trim() === "") {
        closeList();
      } else {
        closeList();
        html += `<p>${inline(raw)}</p>`;
      }
    }
    if (inCode) html += `<pre><code>${codeBuf.join("\n")}</code></pre>`;
    closeList();
    return html;
  }

  function msgEl(role, text, thinking = false) {
    const div = document.createElement("div");
    div.className = `chat-msg ${role}`;
    const who = role === "user" ? "You" : "AI";
    if (thinking) {
      div.innerHTML = `<span class="chat-who">${who}</span><span class="chat-thinking">Thinking\u2026</span>`;
      return div;
    }
    const body = role === "assistant" ? mdToHtml(text) : esc(text);
    div.innerHTML = `<span class="chat-who">${who}</span><span class="chat-body">${body}</span>`;
    return div;
  }

  function isOpen() { return !!document.getElementById("chat-popup"); }

  function open(storyId) {
    if (!Auth.state.authenticated) {
      Auth.toast("Sign in to use story chat.", {
        label: "Sign in", onClick: Auth.showLoginModal,
      });
      return;
    }
    if (isOpen() && currentStory === storyId) {
      document.getElementById("chat-input")?.focus();
      return;
    }
    render(storyId);
  }

  function close() {
    document.getElementById("chat-popup")?.remove();
    currentStory = null;
  }

  function render(storyId) {
    close();
    busy = false;
    currentStory = storyId;
    const overlay = document.createElement("div");
    overlay.id = "chat-popup";
    overlay.innerHTML = `
      <div class="chat-panel">
        <div class="chat-head">
          <span class="chat-title">\u{1F4AC} Story chat</span>
          <button id="chat-close" class="chat-x" title="Close">×</button>
        </div>
        <div class="chat-messages" id="chat-messages"></div>
        <div class="chat-input-row">
          <select id="chat-turns" title="Web-search turns to run before answering"></select>
          <textarea id="chat-input" rows="2" placeholder="Ask about this story\u2026"></textarea>
          <button id="chat-send" class="btn">Send</button>
        </div>
        <div class="chat-foot">Local model · Ctrl/⌘+Enter to send · per-story history</div>
      </div>`;
    document.body.appendChild(overlay);

    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    overlay.querySelector("#chat-close").onclick = close;

    const sel = overlay.querySelector("#chat-turns");
    const input = overlay.querySelector("#chat-input");
    const sendBtn = overlay.querySelector("#chat-send");
    sendBtn.onclick = () => send();
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send();
    });
    loadHistory(storyId, sel, input);
  }

  function loadHistory(storyId, sel, input) {
    fetch(`/api/stories/${storyId}/chat`)
      .then((res) => {
        if (Auth.unauth(res)) { close(); return null; }
        return res.json();
      })
      .then((d) => {
        const box = document.getElementById("chat-messages");
        if (!box) return; // popup closed meanwhile
        sel.innerHTML = (d.search_turns_options || [0])
          .map((t) => `<option value="${t}">${t === 0 ? "No web search" : "Web search ×" + t}</option>`)
          .join("");
        box.innerHTML = "";
        if (!d.messages.length) {
          box.innerHTML = `<div class="chat-empty">Ask a question about this story.</div>`;
        } else {
          for (const m of d.messages) box.appendChild(msgEl(m.role, m.content));
        }
        box.scrollTop = box.scrollHeight;
        input.focus();
      })
      .catch(() => { /* history is non-critical; the input still works */ });
  }

  async function send() {
    if (busy) return;
    const storyId = currentStory;
    const input = document.getElementById("chat-input");
    const sel = document.getElementById("chat-turns");
    const box = document.getElementById("chat-messages");
    const text = (input.value || "").trim();
    if (!text || !storyId) return;
    const turns = parseInt(sel.value, 10) || 0;
    box.querySelector(".chat-empty")?.remove();
    box.appendChild(msgEl("user", text));
    input.value = "";
    const reply = msgEl("assistant", "", true);
    box.appendChild(reply);
    box.scrollTop = box.scrollHeight;
    busy = true;

    let answer = "";
    let rendered = 0;
    const setStatus = (s) => { reply.querySelector(".chat-thinking").textContent = s; };
    const render = () => {
      reply.innerHTML = `<span class="chat-who">AI</span><span class="chat-body">${mdToHtml(answer)}</span>`;
      rendered = answer.length;
      const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
      if (nearBottom) box.scrollTop = box.scrollHeight;
    };
    try {
      const res = await fetch(`/api/stories/${storyId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, search_turns: turns }),
      });
      if (Auth.unauth(res)) { reply.remove(); return; }
      if (!res.ok) throw new Error("HTTP " + res.status);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          const ev = JSON.parse(line);
          if (ev.type === "status" && !answer) setStatus(ev.text);
          else if (ev.type === "delta") answer += ev.text;
          else if (ev.type === "error") throw new Error(ev.text);
        }
        if (answer.length > rendered) render();
      }
      if (!answer) throw new Error("empty reply");
      render();
    } catch (err) {
      answer += `${answer ? "\n\n" : ""}(error: ${err.message || err})`;
      render();
    } finally {
      busy = false;
    }
  }

  return { open, close };
})();
