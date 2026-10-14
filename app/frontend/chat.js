/* Per-story chat — a page-level popup, per-user (signed-in only), backed by
 * the local CHAT_MODEL. The story's context is injected server-side into the
 * SYSTEM prompt; when web search is enabled (default), the agent decides by
 * itself whether and how deep to search via searchngx before answering.
 * History persists per-story in the DB.
 *
 * Backend: app/chat.py, app/searchngx.py. One popup instance, bound to a
 * single story at a time; reopening for the same story focuses it, a different
 * story switches it. Survives story-card re-renders (it lives on <body>). */

const Chat = (() => {
  let currentStory = null; // story_id the popup is bound to
  let busy = false;        // a reply is in flight (guards double-send)
  let webSearch = localStorage.getItem("chatWebSearch") !== "0"; // on by default

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

  const STEP_ICONS = { thinking: "\u{1F4AD}", search: "\u{1F50D}", read: "\u{1F4C4}", error: "\u26A0" };

  // Agent steps are collapsed one-liners; text goes in via textContent (model/web content).
  function upsertStep(container, step) {
    let el = container.querySelector(`[data-step="${Number(step.id)}"]`);
    if (!el) {
      el = document.createElement("details");
      el.dataset.step = Number(step.id);
      el.innerHTML = `<summary><span class="step-ic"></span><span class="step-title"></span></summary><div class="step-detail"></div>`;
      container.appendChild(el);
    }
    el.className = `chat-step k-${step.kind} s-${step.status}`;
    el.querySelector(".step-ic").textContent = STEP_ICONS[step.kind] || "\u2022";
    el.querySelector(".step-title").textContent = step.title;
    if (step.detail != null) el.querySelector(".step-detail").textContent = step.detail;
    el.classList.toggle("no-detail", !el.querySelector(".step-detail").textContent);
  }

  function appendThinking(container, text) {
    const el = container.querySelector(".chat-step.k-thinking.s-running");
    if (!el) return;
    const detail = el.querySelector(".step-detail");
    detail.append(text);
    el.classList.remove("no-detail");
    if (el.open) detail.scrollTop = detail.scrollHeight;
  }

  function msgEl(role, text, steps = [], pending = false) {
    const div = document.createElement("div");
    div.className = `chat-msg ${role}`;
    const who = role === "user" ? "You" : "AI";
    if (role === "user") {
      div.innerHTML = `<span class="chat-who">${who}</span><span class="chat-body">${esc(text)}</span>`;
      return div;
    }
    div.innerHTML = `<span class="chat-who">${who}</span><div class="chat-steps"></div><span class="chat-body"></span>`;
    const box = div.querySelector(".chat-steps");
    for (const s of steps || []) upsertStep(box, s);
    div.querySelector(".chat-body").innerHTML = pending
      ? `<span class="chat-thinking">Working\u2026</span>` : mdToHtml(text);
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
          <button id="chat-search" class="toggle" type="button" aria-pressed="${webSearch}" title="When enabled, the AI decides by itself whether and how deep to search the web">
            <span class="toggle-track" aria-hidden="true"></span>Web search
          </button>
          <textarea id="chat-input" rows="2" placeholder="Ask about this story\u2026"></textarea>
          <button id="chat-send" class="btn">Send</button>
        </div>
        <div class="chat-foot">Local model · Ctrl/⌘+Enter to send · per-story history</div>
      </div>`;
    document.body.appendChild(overlay);

    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    overlay.querySelector("#chat-close").onclick = close;

    const searchBtn = overlay.querySelector("#chat-search");
    searchBtn.onclick = () => {
      webSearch = !webSearch;
      localStorage.setItem("chatWebSearch", webSearch ? "1" : "0");
      searchBtn.setAttribute("aria-pressed", webSearch);
    };
    const input = overlay.querySelector("#chat-input");
    const sendBtn = overlay.querySelector("#chat-send");
    sendBtn.onclick = () => send();
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send();
    });
    loadHistory(storyId, input);
  }

  function loadHistory(storyId, input) {
    fetch(`/api/stories/${storyId}/chat`)
      .then((res) => {
        if (Auth.unauth(res)) { close(); return null; }
        return res.json();
      })
      .then((d) => {
        if (!d || !document.getElementById("chat-messages")) return; // popup closed meanwhile
        renderMessages(d.messages);
        input.focus();
      })
      .catch(() => { /* history is non-critical; the input still works */ });
  }

  // Shows one branch of the conversation; edited user messages get ‹ n/m › version arrows.
  function renderMessages(messages) {
    const box = document.getElementById("chat-messages");
    if (!box) return;
    box.innerHTML = "";
    if (!messages.length) {
      box.innerHTML = `<div class="chat-empty">Ask a question about this story.</div>`;
      return;
    }
    for (const m of messages) {
      const el = msgEl(m.role, m.content, m.steps);
      if (m.role === "user") addMsgTools(el, m);
      else if (m.content) addAnswerTools(el, m);
      box.appendChild(el);
    }
    box.scrollTop = box.scrollHeight;
  }

  function addAnswerTools(el, m) {
    const tools = document.createElement("div");
    tools.className = "chat-msg-tools";
    tools.innerHTML = `<button type="button" title="Copy a private link to this answer">\u{1F517}</button>`;
    const storyId = currentStory;
    tools.querySelector("button").onclick = async () => {
      const res = await fetch("/api/share", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "chat", story_id: storyId, message_id: m.id }),
      });
      if (Auth.unauth(res)) return;
      if (!res.ok) return Auth.toast("Sharing failed.");
      copyLink((await res.json()).url);
    };
    el.appendChild(tools);
  }

  function addMsgTools(el, m) {
    const n = m.versions.length;
    const tools = document.createElement("div");
    tools.className = "chat-msg-tools";
    tools.innerHTML = (n > 1 ? `
        <button type="button" data-v="-1" title="Previous version" ${m.version <= 1 ? "disabled" : ""}>‹</button>
        <span>${m.version}/${n}</span>
        <button type="button" data-v="1" title="Next version" ${m.version >= n ? "disabled" : ""}>›</button>` : "")
      + `<button type="button" class="chat-edit" title="Edit and resend (keeps the previous version)">✎</button>`;
    tools.querySelectorAll("[data-v]").forEach((b) => b.addEventListener("click", () => {
      switchVersion(m.versions[m.version - 1 + Number(b.dataset.v)]);
    }));
    tools.querySelector(".chat-edit").addEventListener("click", () => startEdit(el, m));
    el.appendChild(tools);
  }

  async function switchVersion(messageId) {
    if (busy || messageId == null) return;
    const res = await fetch(`/api/stories/${currentStory}/chat/switch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_id: messageId }),
    });
    if (Auth.unauth(res) || !res.ok) return;
    renderMessages((await res.json()).messages);
  }

  function startEdit(el, m) {
    if (busy || el.querySelector(".chat-edit-box")) return;
    const body = el.querySelector(".chat-body");
    const tools = el.querySelector(".chat-msg-tools");
    const form = document.createElement("div");
    form.className = "chat-edit-box";
    form.innerHTML = `<textarea rows="3"></textarea>
      <div class="chat-edit-row"><button type="button" class="btn" data-a="cancel">Cancel</button>
      <button type="button" class="btn primary" data-a="send">Send</button></div>`;
    const ta = form.querySelector("textarea");
    ta.value = m.content;
    body.hidden = true;
    tools.hidden = true;
    el.insertBefore(form, tools);
    ta.focus();
    const cancel = () => { form.remove(); body.hidden = false; tools.hidden = false; };
    const submit = () => {
      const text = ta.value.trim();
      if (!text || text === m.content.trim()) return cancel();
      send(text, m.id, el);
    };
    form.querySelector('[data-a="cancel"]').onclick = cancel;
    form.querySelector('[data-a="send"]').onclick = submit;
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); cancel(); }
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit();
    });
  }

  async function refreshMessages(storyId) {
    try {
      const res = await fetch(`/api/stories/${storyId}/chat`);
      if (res.ok && currentStory === storyId) renderMessages((await res.json()).messages);
    } catch { /* keep the streamed view */ }
  }

  // editOf/editEl: resend an edited user message as a new version of it.
  async function send(edited = null, editOf = null, editEl = null) {
    if (busy) return;
    const storyId = currentStory;
    const input = document.getElementById("chat-input");
    const box = document.getElementById("chat-messages");
    const text = (edited ?? input.value ?? "").trim();
    if (!text || !storyId) return;
    box.querySelector(".chat-empty")?.remove();
    if (editEl) {
      while (editEl.nextSibling) editEl.nextSibling.remove();
      editEl.remove();
    }
    box.appendChild(msgEl("user", text));
    if (edited == null) input.value = "";
    const reply = msgEl("assistant", "", [], true);
    box.appendChild(reply);
    box.scrollTop = box.scrollHeight;
    busy = true;

    const stepsBox = reply.querySelector(".chat-steps");
    const bodyEl = reply.querySelector(".chat-body");
    let answer = "";
    let rendered = 0;
    let failed = "";
    let finished = false;
    const setStatus = (s) => { const t = bodyEl.querySelector(".chat-thinking"); if (t) t.textContent = s; };
    const render = () => {
      bodyEl.innerHTML = mdToHtml(answer)
        + (failed ? `<p class="chat-error">${esc(failed)}</p>` : "");
      rendered = answer.length;
      const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
      if (nearBottom) box.scrollTop = box.scrollHeight;
    };
    try {
      const res = await fetch(`/api/stories/${storyId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, web_search: webSearch, edit_of: editOf }),
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
          if (ev.type === "step") {
            upsertStep(stepsBox, ev.step);
            if (!answer) setStatus(ev.step.status === "running" ? ev.step.title : "Working\u2026");
          } else if (ev.type === "thinking") appendThinking(stepsBox, ev.text);
          else if (ev.type === "delta") answer += ev.text;
          else if (ev.type === "error") { failed = ev.text; finished = true; }
          else if (ev.type === "done") finished = true;
        }
        if (answer.length > rendered) render();
      }
      if (!answer && !failed) failed = "No answer.";
      render();
    } catch (err) {
      failed = `Error: ${err.message || err}`;
      render();
    } finally {
      busy = false;
    }
    // Pick up message ids and versions (enables edit / version arrows on the new messages).
    if (finished) refreshMessages(storyId);
  }

  return { open, close, mdToHtml };
})();
