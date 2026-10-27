/* Per-story chat — a page-level popup, per-user (signed-in only), backed by
 * the local CHAT_MODEL. The story's context is injected server-side into the
 * SYSTEM prompt; when web search is enabled (default), the agent decides by
 * itself whether and how deep to search via searchngx before answering.
 * History persists per-story in the DB.
 *
 * Backend: app/chat.py, app/chat_history.py, app/searchngx.py. One popup
 * instance, bound to a single story at a time; reopening for the same story
 * focuses it, a different story switches it. Survives story-card re-renders
 * (it lives on <body>).
 *
 * Answers run as server-side jobs (app/chat_jobs.py): closing the popup,
 * switching stories or reloading the page does NOT stop them — only the Stop
 * button does. This tab follows each running answer (its own sends, and any
 * running one it finds via /api/chats or when a chat is opened). After every
 * stream end the history is re-fetched from the server, so the view settles
 * into a consistent state (✎ / version arrows are always present).
 *
 * Clicking beside the popup while an answer is in progress turns it into a
 * bubble (bottom right) showing the progress, then "answer ready"; it stays
 * until the chat is opened or the answer read elsewhere. A chat opened while
 * its answer is pending doesn't bubble again on the next click beside it (until
 * the next question). Unread answers are counted on the "Chats" tab (/chats). */

const Chat = (() => {
  let currentStory = null; // story_id the popup is bound to
  const streams = new Map(); // story_id -> state of the followed answer job; outlives the box
  const queues = new Map();  // story_id -> FIFO queue of sends typed while its stream is running
  const chats = new Map();   // story_id -> /api/chats row (headline, unread, running, status)
  const headlines = new Map(); // story_id -> headline (known before the first sync)
  const noBubble = new Set();  // opened while pending: clicking beside it doesn't bubble it again
  const listeners = [];
  function busyFor(storyId) { return streams.has(storyId); }
  function serverBusy(storyId) { return !!chats.get(storyId)?.running; }
  let webSearch = localStorage.getItem("chatWebSearch") !== "0"; // on by default
  let signedIn = false;

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

  const STEP_ICONS = { thinking: "\u{1F4AD}", search: "\u{1F50D}", read: "\u{1F4C4}", error: "\u26A0", stopped: "\u23F9" };

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

  function msgEl(role, text, steps = [], working = false) {
    const div = document.createElement("div");
    div.className = `chat-msg ${role}`;
    const who = role === "user" ? "You" : "AI";
    if (role === "user") {
      div.innerHTML = `<span class="chat-who">${who}</span><span class="chat-body">${esc(text)}</span>`;
      return div;
    }
    div.innerHTML = `<span class="chat-who">${who}</span><div class="chat-steps"></div><span class="chat-body"></span>`;
    const box = div.querySelector(".chat-steps");
    const stoppedStep = (steps || []).find((s) => s.kind === "stopped");
    const errorStep = !text && !stoppedStep ? (steps || []).find((s) => s.kind === "error") : null;
    for (const s of steps || []) if (s.kind !== "stopped") upsertStep(box, s);
    div.querySelector(".chat-body").innerHTML = working
      ? `<span class="chat-thinking">Working\u2026</span>`
      : mdToHtml(text)
        + (stoppedStep ? `<p class="chat-cancelled">${esc(stoppedStep.title)}</p>` : "")
        + (errorStep ? `<p class="chat-error">${esc(errorStep.detail || errorStep.title)}</p>` : "");
    return div;
  }

  function isOpen() { return !!document.getElementById("chat-popup"); }

  function open(storyId, headline) {
    if (!Auth.state.authenticated) {
      Auth.toast("Sign in to use story chat.", {
        label: "Sign in", onClick: Auth.showLoginModal,
      });
      return;
    }
    if (headline) headlines.set(storyId, headline);
    // Looked at while pending: a click beside it later must not bubble it again.
    if (busyFor(storyId) || serverBusy(storyId) || chats.get(storyId)?.unread) noBubble.add(storyId);
    removeBubble(storyId);
    if (isOpen() && currentStory === storyId) {
      document.getElementById("chat-input")?.focus();
      return;
    }
    render(storyId);
  }

  function close() {
    // Closing does NOT stop the answer: it runs on the server (whatever is
    // generated is persisted). Only stopSend() (the Stop button) stops it.
    // render()/open() re-fetches the fresh state next time.
    document.getElementById("chat-popup")?.remove();
    document.body.classList.remove("chat-open");
    currentStory = null;
    renderBubbles();
  }

  // Click beside the popup: an answer in progress continues as a bubble.
  function minimize() {
    const storyId = currentStory;
    close();
    if (storyId != null && !noBubble.has(storyId)
        && (busyFor(storyId) || serverBusy(storyId) || (queues.get(storyId) || []).length)) {
      addBubble(storyId);
    }
  }

  function titleOf(storyId) {
    return headlines.get(storyId) || chats.get(storyId)?.headline || "Story chat";
  }

  function render(storyId) {
    close();
    currentStory = storyId;
    const overlay = document.createElement("div");
    overlay.id = "chat-popup";
    overlay.innerHTML = `
      <div class="chat-panel">
        <div class="chat-head">
          <span class="chat-title"></span>
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
        <div class="chat-foot">Local model · Enter to send · Shift+Enter for newline · click beside the chat to keep a working answer as a bubble · messages queue while working</div>
      </div>`;
    overlay.querySelector(".chat-title").textContent = `\u{1F4AC} ${titleOf(storyId)}`;
    document.body.appendChild(overlay);
    document.body.classList.add("chat-open");
    renderBubbles();

    overlay.addEventListener("click", (e) => { if (e.target === overlay) minimize(); });
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    overlay.querySelector("#chat-close").onclick = close;

    const searchBtn = overlay.querySelector("#chat-search");
    searchBtn.onclick = () => {
      webSearch = !webSearch;
      localStorage.setItem("chatWebSearch", webSearch ? "1" : "0");
      searchBtn.setAttribute("aria-pressed", webSearch);
    };
    const input = overlay.querySelector("#chat-input");
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    });
    input.addEventListener("input", syncSendBtn);
    syncSendBtn();
    loadHistory(storyId, input);
    if (!busyFor(storyId)) attach(storyId); // an answer may be running (other tab, before a reload)
    drain(storyId); // anything queued before the close now starts
  }

  // "Send" turns into "Stop" once the agent is working and there is nothing
  // to send: stopping makes the server end its model / web-search calls
  // (whatever was generated is kept and saved).
  function syncSendBtn() {
    const btn = document.getElementById("chat-send");
    const input = document.getElementById("chat-input");
    if (!btn) return;
    const st = streams.get(currentStory);
    const stopMode = !!st && input && !input.value.trim();
    btn.textContent = stopMode ? (st.stopping ? "Stopping\u2026" : "Stop") : "Send";
    btn.disabled = !!(stopMode && st.stopping);
    btn.classList.toggle("stop", stopMode);
    btn.title = stopMode ? "Stop the answer (ends further model calls)" : "";
    btn.onclick = stopMode ? stopSend : () => send(); // must not pass the click event as `edited`
  }

  async function stopSend() {
    const storyId = currentStory;
    const st = streams.get(storyId);
    if (!st) return;
    st.stopping = true;
    syncSendBtn();
    try {
      const res = await fetch(`/api/stories/${storyId}/chat/stop`, { method: "POST" });
      if (Auth.unauth(res)) return;
    } catch { /* the stream end (or the next sync) settles the state */ }
  }

  function loadHistory(storyId, input) {
    fetch(`/api/stories/${storyId}/chat`)
      .then((res) => {
        if (Auth.unauth(res)) { close(); return null; }
        return res.json();
      })
      .then((d) => {
        const box = document.getElementById("chat-messages");
        if (!d || !box || currentStory !== storyId) return; // popup closed or switched meanwhile
        renderMessages(d.messages);
        bindLive(storyId, d.messages);
        syncSendBtn();
        if (!busyFor(storyId) && !serverBusy(storyId)) markRead(storyId);
        input.focus();
      })
      .catch(() => { /* history is non-critical; the input still works */ });
  }

  // The answer in progress gets a live reply element with all steps/text gathered
  // so far, so streaming continues in the reopened popup.
  function bindLive(storyId, messages) {
    const st = streams.get(storyId);
    const box = document.getElementById("chat-messages");
    if (!st || !box || currentStory !== storyId) return;
    if (st.reply && box.contains(st.reply)) return;
    box.querySelector(".chat-empty")?.remove();
    box.querySelector(".chat-retry")?.remove();
    const lastEl = [...box.querySelectorAll(".chat-msg")].pop();
    const endsWithQuestion = messages ? messages[messages.length - 1]?.role === "user" : !!lastEl?.classList.contains("user");
    if (st.question && !endsWithQuestion) box.appendChild(msgEl("user", st.question));
    const reply = msgEl("assistant", st.answer, st.steps, !st.answer);
    if (!st.answer) {
      const t = reply.querySelector(".chat-thinking");
      if (t) t.textContent = st.status;
    }
    box.appendChild(reply);
    st.reply = reply;
    renderQueue();
    box.scrollTop = box.scrollHeight;
  }

  // Shows one branch of the conversation; edited user messages get ‹ n/m › version arrows.
  function renderMessages(messages) {
    const box = document.getElementById("chat-messages");
    if (!box) return;
    box.innerHTML = "";
    if (!messages.length) {
      box.innerHTML = `<div class="chat-empty">Ask a question about this story.</div>`;
      renderQueue();
      return;
    }
    for (const m of messages) {
      const el = msgEl(m.role, m.content, m.steps);
      if (m.id != null) el.dataset.id = m.id;
      if (m.role === "user") addMsgTools(el, m);
      else if (m.content) addAnswerTools(el, m);
      box.appendChild(el);
    }
    if (!busyFor(currentStory) && !serverBusy(currentStory) && messages.length) {
      const last = messages[messages.length - 1];
      const retryable = last.role === "user"
        || (last.role === "assistant" && (!last.content || (last.steps || []).some((s) => s.kind === "stopped" || s.kind === "error")));
      if (retryable) attachRetry(currentStory, messages);
    }
    renderQueue();
    box.scrollTop = box.scrollHeight;
  }

  // "N queued" indicator with per-item × (removal); rebuilt after every
  // full re-render of the box.
  function renderQueue() {
    const box = document.getElementById("chat-messages");
    box?.querySelector(".chat-queue")?.remove();
    if (!box || !currentStory) return;
    const q = queues.get(currentStory) || [];
    if (!q.length) return;
    const div = document.createElement("div");
    div.className = "chat-queue";
    div.appendChild(document.createTextNode(`⏳ ${q.length} queued · `));
    for (let i = 0; i < q.length; i++) {
      if (i > 0) div.appendChild(document.createTextNode(" · "));
      const p = q[i];
      const t = p.text || "";
      const item = document.createElement("span");
      item.className = "chat-queue-item";
      item.appendChild(document.createTextNode(t.slice(0, 50) + (t.length > 50 ? "\u2026" : "")));
      const x = document.createElement("button");
      x.type = "button";
      x.className = "chat-queue-x";
      x.title = "Remove queued message";
      x.textContent = "\u00D7";
      x.onclick = () => { q.splice(i, 1); renderQueue(); syncSendBtn(); };
      item.appendChild(x);
      div.appendChild(item);
    }
    box.appendChild(div);
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
    if (busyFor(currentStory) || messageId == null) return; // nav while streaming is a race
    const res = await fetch(`/api/stories/${currentStory}/chat/switch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_id: messageId }),
    });
    if (Auth.unauth(res) || !res.ok) return;
    renderMessages((await res.json()).messages);
  }

  function startEdit(el, m) {
    // No busy guard: editing is always possible — if a stream is active the
    // resulting send is simply queued (see send()/drain()).
    if (el.querySelector(".chat-edit-box")) return;
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
      if (text === m.content.trim()) return cancel();
      if (!text) return; // empty edit: keep the ✎ open so it can't be sent
      cancel(); // close the edit box now; the send may just be queued
      send(text, m.id, el);
    };
    form.querySelector('[data-a="cancel"]').onclick = cancel;
    form.querySelector('[data-a="send"]').onclick = submit;
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); cancel(); }
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
    });
  }

  // Re-fetch the story's history (server-side source of truth) and
  // re-render it. Runs after every stream end — completed, stopped, or
  // error — because the question and whatever was generated are already
  // persisted; we get real message ids + versions, so ✎ and version arrows
  // are always present, whatever state the stream ended in.
  async function refresh(storyId) {
    if (currentStory !== storyId) return null; // popup bound elsewhere: next open() re-fetches
    try {
      const res = await fetch(`/api/stories/${storyId}/chat`);
      if (!res.ok) return null;
      const messages = (await res.json()).messages;
      if (currentStory === storyId) { renderMessages(messages); syncSendBtn(); }
      return messages;
    } catch { return null; }
  }

  // "Retry" pill under a stopped or failed answer: resends the question as
  // a new version (edit_of) rather than a fresh duplicate.
  function attachRetry(storyId, messages) {
    const box = document.getElementById("chat-messages");
    if (currentStory !== storyId || !box || box.querySelector(".chat-retry")) return;
    let qi = -1;
    for (let i = messages.length - 1; i >= 0; i--) if (messages[i].role === "user") { qi = i; break; }
    const els = box.querySelectorAll(".chat-msg");
    if (qi < 0 || els.length !== messages.length) return;
    const question = messages[qi];
    const qEl = els[qi];
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "chat-retry";
    pill.title = "Send this message again";
    pill.textContent = "↻ Retry";
    pill.onclick = () => { pill.remove(); send(question.content, question.id, qEl); };
    els[els.length - 1].appendChild(pill);
  }

  // editOf/editEl: resend an edited user message as a new version of it.
  // While a story's answer is in progress, further sends are queued per-story
  // and pumped FIFO by drain(storyId) once that stream ends.
  function send(edited = null, editOf = null, editEl = null) {
    const storyId = currentStory;
    const input = document.getElementById("chat-input");
    const text = (edited ?? input?.value ?? "").trim();
    if (!text || !storyId) return;
    if (busyFor(storyId) || serverBusy(storyId)) {
      enqueue(storyId, { text, editOf, editEl, isNew: edited == null });
      if (edited == null && input) input.value = "";
      renderQueue();
      syncSendBtn();
      if (!busyFor(storyId)) attach(storyId);
      return;
    }
    doSend(storyId, text, editOf, editEl, edited == null);
  }

  function enqueue(storyId, item, front = false) {
    let q = queues.get(storyId);
    if (!q) { q = []; queues.set(storyId, q); }
    if (front) q.unshift(item); else q.push(item);
  }

  function newStream(storyId, question, reply) {
    const st = {
      controller: new AbortController(), // detaches this tab only; the server job runs on
      question,
      steps: [],
      answer: "",
      status: "Working\u2026",
      reply,
      started: Date.now(),
      stopping: false,
      failed: "",
      lost: false,
    };
    streams.set(storyId, st); // before any awaits: Stop button effective immediately
    const c = chats.get(storyId);
    if (c) {
      c.running = true;
      if (question) Object.assign(c, { question: question.slice(0, 240), answer: "", updated_at: new Date().toISOString().slice(0, 19).replace("T", " ") });
      else if (c.started) st.started = c.started * 1000; // reattached: count from the server's start
    }
    changed();
    return st;
  }

  // A stream belongs to a story, not to the popup: it's registered in
  // `streams` synchronously (before the first await) and may outlive this
  // box — closing or switching stories detaches the DOM, from then on
  // writes are no-ops (until reopened), while the fetch keeps going. On every
  // stream end, history is re-fetched so ✎ and version arrows always exist.
  async function doSend(storyId, text, editOf = null, editEl = null, isNew = true) {
    const visible = () => currentStory === storyId;
    const input = visible() ? document.getElementById("chat-input") : null;
    const box = visible() ? document.getElementById("chat-messages") : null;
    let reply = null;
    if (box) {
      box.querySelector(".chat-empty")?.remove();
      box.querySelector(".chat-retry")?.remove();
      const target = (editOf != null ? box.querySelector(`.chat-msg[data-id="${Number(editOf)}"]`) : null) || editEl;
      if (target && box.contains(target)) {
        while (target.nextSibling) target.nextSibling.remove();
        target.remove();
      }
      reply = msgEl("assistant", "", [], true);
      box.appendChild(msgEl("user", text));
      box.appendChild(reply);
      renderQueue();
      if (isNew && input) input.value = "";
      box.scrollTop = box.scrollHeight;
    }
    noBubble.delete(storyId); // a new question may bubble again
    const st = newStream(storyId, text, reply);
    if (visible()) syncSendBtn();
    let res;
    try {
      res = await fetch(`/api/stories/${storyId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, web_search: webSearch, edit_of: editOf }),
        signal: st.controller.signal,
      });
    } catch (err) {
      res = null;
      st.failed = `Error: ${err.message || err}`;
    }
    if (res && Auth.unauth(res)) {
      st.reply?.remove();
      streams.delete(storyId);
      changed();
      return;
    }
    if (res && (res.status === 409 || res.status === 429)) {
      // 409: this story is still answering (started elsewhere) — queue and follow it.
      st.reply?.remove();
      streams.delete(storyId);
      if (res.status === 409) {
        enqueue(storyId, { text, editOf, editEl: null, isNew: false }, true);
        attach(storyId);
      } else {
        Auth.toast("Too many answers in progress \u2014 try again when one is done.");
        if (visible() && isNew && input && !input.value) input.value = text;
      }
      await refresh(storyId);
      changed();
      return;
    }
    if (res && !res.ok) st.failed = `Error: HTTP ${res.status}`;
    if (res?.ok) await consume(storyId, st, res);
    await finish(storyId, st);
  }

  // Follows an answer that is already running on the server (started in another
  // tab, before a reload, or found by sync()). Registers only when one runs.
  async function attach(storyId) {
    if (busyFor(storyId) || !signedIn) return;
    const pending = new AbortController();
    let res;
    try {
      res = await fetch(`/api/stories/${storyId}/chat/stream`, { signal: pending.signal });
    } catch { return; }
    if (res.status !== 200 || busyFor(storyId)) {
      try { await res.body?.cancel(); } catch { /* ignore */ }
      if (res.status === 204) {
        const c = chats.get(storyId);
        if (c?.running) { c.running = false; changed(); drain(storyId); }
      }
      return;
    }
    const st = newStream(storyId, "", null);
    st.controller = pending;
    bindLive(storyId);
    if (currentStory === storyId) syncSendBtn();
    await consume(storyId, st, res);
    await finish(storyId, st);
  }

  // Reads the NDJSON events (steps, thinking, deltas, done/error) into `st` and its reply element.
  async function consume(storyId, st, res) {
    const visible = () => currentStory === storyId;
    let rendered = 0;
    const setStatus = (s) => {
      st.status = s;
      const t = st.reply?.querySelector(".chat-thinking");
      if (t) t.textContent = s;
    };
    const render = () => {
      rendered = st.answer.length;
      const r = st.reply;
      const b = visible() ? document.getElementById("chat-messages") : null;
      if (!r || !r.isConnected || !b) return;
      r.querySelector(".chat-body").innerHTML = mdToHtml(st.answer)
        + (st.failed ? `<p class="chat-error">${esc(st.failed)}</p>` : "");
      const nearBottom = b.scrollHeight - b.scrollTop - b.clientHeight < 80;
      if (nearBottom) b.scrollTop = b.scrollHeight;
    };
    try {
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let finished = false;
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
            const idx = st.steps.findIndex((s) => s.id === ev.step.id);
            if (idx >= 0) st.steps[idx] = { ...st.steps[idx], ...ev.step };
            else st.steps.push({ ...ev.step });
            const sb = st.reply?.querySelector(".chat-steps");
            if (sb && ev.step.kind !== "stopped") upsertStep(sb, ev.step);
            if (ev.step.kind === "stopped") st.stopped = true;
            if (!st.answer) setStatus(ev.step.status === "running" ? ev.step.title : "Working\u2026");
            changed();
          } else if (ev.type === "thinking") {
            const run = st.steps.find((s) => s.kind === "thinking" && s.status === "running");
            if (run) run.detail = (run.detail || "") + ev.text;
            const sb = st.reply?.querySelector(".chat-steps");
            if (sb) appendThinking(sb, ev.text);
          } else if (ev.type === "delta") {
            st.answer += ev.text;
            changed();
          } else if (ev.type === "error") st.failed = ev.text;
          else if (ev.type === "done") finished = true;
        }
        if (st.answer.length > rendered) render();
      }
      if (!finished && !st.failed) st.lost = true; // connection dropped; the job may still run
      if (!st.answer && !st.failed && !st.lost && !st.stopped) st.failed = "No answer.";
      render();
    } catch (err) {
      if (err?.name === "AbortError") return;
      st.lost = true;
      render();
    }
  }

  // After a stream ended: settle the view from the server, mark read or announce it, start the next queued send.
  async function finish(storyId, st) {
    if (streams.get(storyId) === st) streams.delete(storyId);
    if (st.controller.signal.aborted) { changed(); return; } // signed out: nothing to settle
    const visible = () => currentStory === storyId;
    const c = chats.get(storyId) || { story_id: storyId, headline: titleOf(storyId), questions: 1 };
    chats.set(storyId, c);
    if (st.lost) {
      setTimeout(sync, 1500); // reattach if the server is still working on it
    } else {
      c.running = false;
      c.unread = true;
      c.status = st.failed && !st.answer ? "error" : st.stopped ? "stopped" : "ok";
      c.answer = st.answer.slice(0, 240);
      if (st.question) c.question = st.question.slice(0, 240);
      c.updated_at = new Date().toISOString().slice(0, 19).replace("T", " ");
    }
    await refresh(storyId);
    if (!st.lost) {
      if (visible() && document.visibilityState === "visible") markRead(storyId);
      else if (!bubbleIds().includes(storyId) && document.visibilityState === "visible" && !queuedFor(storyId)) {
        Auth.toast(`Answer ready: ${titleOf(storyId).slice(0, 60)}`, { label: "Open", onClick: () => open(storyId) });
      }
    }
    if (visible()) syncSendBtn();
    changed();
    drain(storyId);
    scheduleSync(800);
  }

  function queuedFor(storyId) { return (queues.get(storyId) || []).length; }

  function drain(storyId) {
    const q = queues.get(storyId);
    if (busyFor(storyId) || serverBusy(storyId) || !q || !q.length) return;
    const next = q.shift();
    if (storyId === currentStory) renderQueue();
    doSend(storyId, next.text, next.editOf, next.editEl, next.isNew);
  }

  async function markRead(storyId) {
    const c = chats.get(storyId);
    if (c) c.unread = false;
    removeBubble(storyId);
    changed();
    try {
      const res = await fetch(`/api/stories/${storyId}/chat/read`, { method: "POST" });
      if (res.ok) scheduleSync(300);
    } catch { /* offline: the answer stays unread on the server */ }
  }

  // --- progress of an answer (bubbles, Chats tab) ---------------------------
  const STAGES = ["Research", "Thinking", "Writing"];

  // { state: running|queued|ready|error|stopped|idle, stage: 0..3 (0 = unknown), text, secs }
  function progressOf(storyId) {
    const st = streams.get(storyId);
    const c = chats.get(storyId);
    if (st) {
      const secs = Math.round((Date.now() - st.started) / 1000);
      const reads = st.steps.filter((s) => s.kind === "read" && s.status !== "running").length;
      const searches = st.steps.filter((s) => s.kind === "search" && /^Web search: /.test(s.title)).length;
      const run = [...st.steps].reverse().find((s) => s.status === "running");
      let stage = 1;
      let text = "Starting\u2026";
      if (st.stopping) text = "Stopping\u2026";
      else if (st.answer) {
        stage = 3;
        text = "Writing the answer\u2026";
      } else if (run?.kind === "thinking") {
        stage = 2;
        text = "Thinking\u2026";
      } else if (run && (run.kind === "search" || run.kind === "read")) {
        text = run.title;
      } else if (st.steps.length) {
        stage = 2;
        text = "Preparing the answer\u2026";
      }
      const research = stage === 1 && (searches || reads)
        ? `${searches} search${searches === 1 ? "" : "es"} \u00b7 ${reads} page${reads === 1 ? "" : "s"} read` : "";
      return { state: "running", stage, text, research, secs };
    }
    if (c?.running) {
      return { state: "running", stage: 0, text: "Working\u2026", research: "",
        secs: c.started ? Math.round(Date.now() / 1000 - c.started) : null };
    }
    if (queuedFor(storyId)) return { state: "queued", stage: 0, text: "Queued", research: "", secs: null };
    if (!c) return { state: "idle", stage: 0, text: "", research: "", secs: null };
    if (c.unread) {
      if (c.status === "error") return { state: "error", stage: 0, text: "Answer failed", research: "", secs: null };
      if (c.status === "stopped") return { state: "stopped", stage: 0, text: "Stopped (partial answer)", research: "", secs: null };
      return { state: "ready", stage: 3, text: "Answer ready \u2014 click to read", research: "", secs: null };
    }
    return { state: "idle", stage: 0, text: "", research: "", secs: null };
  }

  // --- bubbles (bottom right, stacking upwards) -------------------------------
  const BUBBLES_KEY = "chatBubbles";
  const MAX_BUBBLES = 6;
  const bubbleEls = new Map();

  function bubbleIds() {
    try {
      const ids = JSON.parse(localStorage.getItem(BUBBLES_KEY) || "[]");
      return Array.isArray(ids) ? ids.map(Number).filter(Number.isFinite) : [];
    } catch { return []; }
  }

  function saveBubbles(ids) { localStorage.setItem(BUBBLES_KEY, JSON.stringify(ids)); }

  function addBubble(storyId) {
    const ids = bubbleIds().filter((id) => id !== storyId);
    ids.push(storyId);
    saveBubbles(ids);
    renderBubbles();
  }

  function removeBubble(storyId) {
    const ids = bubbleIds();
    if (!ids.includes(storyId)) return;
    saveBubbles(ids.filter((id) => id !== storyId));
    renderBubbles();
  }

  const ICONS = { ready: "\u2713", error: "\u26A0", stopped: "\u23F9", queued: "\u23F3", idle: "\u{1F4AC}" };

  function renderBubbles() {
    let wrap = document.getElementById("chat-bubbles");
    const ids = signedIn ? bubbleIds() : [];
    if (!ids.length) {
      wrap?.remove();
      bubbleEls.clear();
      return;
    }
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.id = "chat-bubbles";
      wrap.setAttribute("aria-label", "Chat answers");
      document.body.appendChild(wrap);
    }
    const shown = ids.slice(-MAX_BUBBLES); // oldest at the bottom (column-reverse), newer ones stack up
    for (const [id, el] of bubbleEls) if (!shown.includes(id)) { el.remove(); bubbleEls.delete(id); }
    shown.forEach((id, i) => {
      let el = bubbleEls.get(id);
      if (!el) {
        el = document.createElement("div");
        el.className = "chat-bubble";
        el.innerHTML = `
          <button type="button" class="cb-main">
            <span class="cb-icon" aria-hidden="true"></span>
            <span class="cb-text"><span class="cb-title"></span><span class="cb-sub"></span>
              <span class="cb-stages" aria-hidden="true">${STAGES.map((s) => `<i title="${s}"></i>`).join("")}</span></span>
          </button>
          <button type="button" class="cb-x" title="Dismiss (the answer stays in Chats)" aria-label="Dismiss">\u00D7</button>`;
        el.querySelector(".cb-main").onclick = () => open(id);
        el.querySelector(".cb-x").onclick = () => removeBubble(id);
        bubbleEls.set(id, el);
      }
      if (wrap.children[i] !== el) wrap.insertBefore(el, wrap.children[i] || null);
      const p = progressOf(id);
      const state = p.state;
      el.className = `chat-bubble cb-${state}`;
      el.querySelector(".cb-icon").textContent = ICONS[state] || "";
      el.querySelector(".cb-title").textContent = titleOf(id);
      const sub = [p.text, p.research, p.secs != null ? fmtSecs(p.secs) : ""].filter(Boolean).join(" \u00b7 ");
      el.querySelector(".cb-sub").textContent = sub;
      el.querySelector(".cb-main").title = `${titleOf(id)}\n${sub}`;
      el.querySelectorAll(".cb-stages i").forEach((dot, k) => {
        dot.className = k + 1 < p.stage ? "done" : k + 1 === p.stage ? (state === "running" ? "now" : "done") : "";
      });
    });
    const more = ids.length - shown.length;
    let moreEl = wrap.querySelector(".cb-more");
    if (more > 0) {
      if (!moreEl) {
        moreEl = document.createElement("a");
        moreEl.className = "cb-more";
        moreEl.href = "/chats";
      }
      moreEl.textContent = `+${more} more`;
      wrap.appendChild(moreEl);
    } else moreEl?.remove();
  }

  function fmtSecs(s) {
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  }

  // --- unread badge on the Chats tab + page title ----------------------------
  function renderBadge() {
    const unread = signedIn ? [...chats.values()].filter((c) => c.unread).length : 0;
    const running = signedIn ? [...chats.values()].filter((c) => c.running).length + [...streams.keys()].filter((id) => !chats.get(id)).length : 0;
    document.querySelectorAll("#chats-link").forEach((a) => {
      let b = a.querySelector(".nav-badge");
      if (!b) {
        b = document.createElement("span");
        b.className = "nav-badge";
        a.appendChild(b);
      }
      b.textContent = unread ? String(unread) : "";
      b.classList.toggle("busy", running > 0);
      b.hidden = !unread && !running;
      a.title = [unread ? `${unread} unread answer${unread === 1 ? "" : "s"}` : "", running ? `${running} in progress` : ""]
        .filter(Boolean).join(" \u00b7 ");
    });
    const base = document.title.replace(/^\(\d+\) /, "");
    document.title = unread ? `(${unread}) ${base}` : base;
  }

  // Coalesces state changes into one bubble/badge/listener update (a timer, not an
  // animation frame: background tabs must still update the title).
  let pendingUpdate = false;
  function changed() {
    if (pendingUpdate) return;
    pendingUpdate = true;
    setTimeout(() => {
      pendingUpdate = false;
      renderBubbles();
      renderBadge();
      for (const fn of listeners) {
        try { fn(); } catch { /* a listener bug never breaks the chat */ }
      }
    }, 60);
  }

  function onUpdate(fn) { listeners.push(fn); }

  // --- server sync: unread state, answers running in other tabs --------------
  const SYNC_MS = 20000;
  let syncTimer = 0;
  let syncing = false;

  function scheduleSync(ms = SYNC_MS) {
    clearTimeout(syncTimer);
    if (signedIn) syncTimer = setTimeout(sync, ms);
  }

  async function sync() {
    if (!signedIn || syncing) return;
    syncing = true;
    try {
      const res = await fetch("/api/chats");
      if (!res.ok) return;
      const d = await res.json();
      const seen = new Set();
      for (const row of d.chats) {
        seen.add(row.story_id);
        const local = streams.get(row.story_id);
        chats.set(row.story_id, local ? { ...row, running: true } : row);
        if (row.running && !local) attach(row.story_id);
      }
      for (const id of [...chats.keys()]) if (!seen.has(id) && !busyFor(id)) chats.delete(id);
      // Bubbles of answers read elsewhere (or gone) disappear.
      const keep = bubbleIds().filter((id) => {
        const c = chats.get(id);
        return busyFor(id) || queuedFor(id) || (c && (c.running || c.unread));
      });
      if (keep.length !== bubbleIds().length) saveBubbles(keep);
      for (const id of queues.keys()) drain(id);
      changed();
    } catch { /* offline: try again later */ } finally {
      syncing = false;
      scheduleSync(streams.size ? 5000 : SYNC_MS);
    }
  }

  function chatList() { return [...chats.values()]; }

  let ticker = 0;
  function start() {
    if (signedIn) return;
    signedIn = true;
    renderBubbles();
    sync();
    ticker = setInterval(() => { if (streams.size || [...chats.values()].some((c) => c.running)) changed(); }, 1000);
  }

  function stopAll() {
    signedIn = false;
    clearTimeout(syncTimer);
    clearInterval(ticker);
    for (const st of streams.values()) st.controller.abort();
    streams.clear();
    queues.clear();
    chats.clear();
    close();
    renderBubbles();
    renderBadge();
  }

  if (typeof Auth !== "undefined") {
    Auth.onChange((s) => (s.authenticated ? start() : stopAll()));
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible" || !signedIn) return;
      sync();
      if (currentStory != null && isOpen() && !busyFor(currentStory) && chats.get(currentStory)?.unread) markRead(currentStory);
    });
  }

  return { open, close, mdToHtml, onUpdate, progressOf, chatList, markRead, sync, titleOf };
})();
