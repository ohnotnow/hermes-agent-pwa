"use strict";

const $ = (s) => document.querySelector(s);
const api = (path, opts = {}) =>
  fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });

const fmtTime = (iso) => { try { return new Date(iso).toLocaleString(); } catch { return ""; } };

// All untrusted text is rendered via textContent / createElement — never
// innerHTML — so content cannot inject markup (PRD §12).
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

// ── markdown (agent messages) ──────────────────────────────────────────────
// Renders a *safe subset* of Markdown by building DOM nodes — never assigning
// innerHTML or an HTML string — so the no-markup-injection guarantee above
// holds even though agent output isn't fully trusted (it can carry web/tool
// text). Covers fenced code blocks, lists, headings, blockquotes, **bold**,
// *italic*, `code` and [links](url). Link hrefs are scheme-checked. This is the
// common ~90%, not full CommonMark; underscore emphasis is intentionally not
// supported so snake_case identifiers aren't mangled.

const INLINE = {
  code: /`([^`]+)`/,
  bold: /\*\*(?=\S)([^*]+?)\*\*/, // (?=\S): "2 * 3 * 4" must not become italic
  italic: /\*(?=\S)([^*]+?)\*/,
  // URL may contain one level of balanced parens, e.g. ...wiki/Foo_(bar)
  link: /\[([^\]]+)\]\(((?:[^()\s]+|\([^()\s]*\))+)\)/,
};

function safeLink(label, url) {
  if (/^(https?:|mailto:)/i.test(url)) {
    const a = el("a", null, label);
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    return a;
  }
  return document.createTextNode(label); // unknown scheme — show the text only
}

function appendInline(parent, text) {
  // Soft line breaks inside a block become <br> (chat-friendly, not strict CommonMark).
  text.split("\n").forEach((part, i) => {
    if (i > 0) parent.appendChild(document.createElement("br"));
    if (part) parent.appendChild(document.createTextNode(part));
  });
}

function renderInline(text, parent) {
  while (text) {
    let hit = null; // earliest match across the inline patterns wins
    for (const [kind, re] of Object.entries(INLINE)) {
      const m = re.exec(text);
      if (m && (!hit || m.index < hit.m.index)) hit = { kind, m };
    }
    if (!hit) { appendInline(parent, text); return; }
    const { kind, m } = hit;
    if (m.index) appendInline(parent, text.slice(0, m.index));
    if (kind === "code") {
      parent.appendChild(el("code", null, m[1]));
    } else if (kind === "link") {
      parent.appendChild(safeLink(m[1], m[2]));
    } else {
      const node = el(kind === "bold" ? "strong" : "em");
      renderInline(m[1], node); // recurse so nested emphasis still renders
      parent.appendChild(node);
    }
    text = text.slice(m.index + m[0].length);
  }
}

const BLANK = (l) => !l.trim();
const FENCE = (l) => /^\s*```/.test(l);
const HEADING = /^\s{0,3}(#{1,6})\s+(.*)$/;
const QUOTE = /^\s*>\s?/;
const UL = /^\s*[-*+]\s+/;
const OL = /^\s*\d+\.\s+/;

function renderMarkdown(text) {
  const frag = document.createDocumentFragment();
  const lines = String(text == null ? "" : text).replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (FENCE(line)) { // ``` fenced code block ```
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++; // consume the closing fence (or fall off the end)
      const pre = el("pre");
      pre.appendChild(el("code", null, buf.join("\n")));
      frag.appendChild(pre);
      continue;
    }

    if (BLANK(line)) { i++; continue; }

    const h = line.match(HEADING);
    if (h) {
      const head = el("h" + h[1].length);
      renderInline(h[2].trim(), head);
      frag.appendChild(head);
      i++;
      continue;
    }

    if (QUOTE.test(line)) {
      const buf = [];
      while (i < lines.length && QUOTE.test(lines[i])) buf.push(lines[i++].replace(QUOTE, ""));
      const bq = el("blockquote");
      renderInline(buf.join("\n"), bq);
      frag.appendChild(bq);
      continue;
    }

    if (UL.test(line) || OL.test(line)) {
      const ordered = OL.test(line) && !UL.test(line);
      const marker = ordered ? OL : UL;
      const list = el(ordered ? "ol" : "ul");
      while (i < lines.length && marker.test(lines[i])) {
        const li = el("li");
        renderInline(lines[i++].replace(marker, ""), li);
        list.appendChild(li);
      }
      frag.appendChild(list);
      continue;
    }

    // Paragraph — gather consecutive "plain" lines until a blank or a block marker.
    const buf = [];
    while (
      i < lines.length && !BLANK(lines[i]) && !FENCE(lines[i]) &&
      !HEADING.test(lines[i]) && !QUOTE.test(lines[i]) &&
      !UL.test(lines[i]) && !OL.test(lines[i])
    ) {
      buf.push(lines[i++]);
    }
    const p = el("p");
    renderInline(buf.join("\n"), p);
    frag.appendChild(p);
  }
  return frag;
}

// Flatten Markdown to plain text for the one-line conversation preview.
function stripMarkdown(text) {
  return String(text == null ? "" : text)
    .replace(/```[\s\S]*?```/g, " ")          // fenced code blocks
    .replace(/`([^`]+)`/g, "$1")              // inline code
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")       // headings
    .replace(/^\s*>\s?/gm, "")                // blockquote markers
    .replace(/^\s*(?:[-*+]|\d+\.)\s+/gm, "")  // list markers
    .replace(/\*\*(?=\S)([^*]+?)\*\*/g, "$1") // bold
    .replace(/\*(?=\S)([^*]+?)\*/g, "$1")     // italic
    .replace(/\[([^\]]+)\]\((?:[^()\s]+|\([^()\s]*\))+\)/g, "$1") // links → label
    .replace(/\s+/g, " ")                     // collapse whitespace/newlines
    .trim();
}

const state = {
  view: "login",
  conversationId: null,
  agentLabel: "",
  rendered: new Set(),
  bubbles: new Map(), // message_id -> user bubble element (for delivery ticks)
};
let sse = null;
let agentsOnline = {};

function show(view) {
  state.view = view;
  for (const id of ["login", "list", "convo"]) {
    document.getElementById("view-" + id).hidden = id !== view;
  }
  if (view === "login") setConn(true); // no banner on the login screen
}

function setConn(ok) {
  document.getElementById("conn-banner").hidden = !!ok;
}

async function init() {
  const d = await api("/api/me").then((r) => r.json()).catch(() => ({}));
  if (d.authenticated) { startSSE(); await loadList(); show("list"); }
  else { show("login"); }
}

// ── login ──
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#login-error");
  err.hidden = true;
  const secret = $("#login-secret").value.trim();
  const r = await api("/api/login", { method: "POST", body: JSON.stringify({ secret }) }).catch(() => null);
  if (r && r.ok) {
    $("#login-secret").value = "";
    startSSE();
    await loadList();
    show("list");
  } else {
    if (!r) err.textContent = "Couldn’t reach the gateway — is it running?";
    else if (r.status === 429) err.textContent = "Too many attempts — locked out, wait a bit.";
    else if (r.status === 401) err.textContent = "Incorrect PIN or token.";
    else err.textContent = `Login failed (HTTP ${r.status}).`;
    err.hidden = false;
  }
});

$("#logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  stopSSE();
  show("login");
});

// ── agents / presence ──
function setAgents(agents) {
  agentsOnline = {};
  for (const a of agents) agentsOnline[a.id] = !!a.online;
}

async function refreshAgents() {
  const agents = await api("/api/agents").then((r) => r.json()).then((d) => d.agents).catch(() => null);
  if (!agents) { setConn(false); return; }
  setAgents(agents);
  if (state.view === "convo") updateConvoStatus();
}

function updateConvoStatus() {
  const s = $("#convo-status");
  const online = !!agentsOnline[state.agentLabel];
  s.className = "status" + (online ? " online" : "");
  s.replaceChildren(el("span", "dot"), document.createTextNode(online ? "online" : "offline"));
}

// ── conversation list ──
async function loadList() {
  const [conv, agents] = await Promise.all([
    api("/api/conversations").then((r) => r.json()).then((d) => d.conversations || []).catch(() => []),
    api("/api/agents").then((r) => r.json()).then((d) => d.agents || []).catch(() => []),
  ]);
  setAgents(agents);

  $("#agent-options").replaceChildren(...agents.map((a) => {
    const o = document.createElement("option");
    o.value = a.id;
    return o;
  }));

  const list = $("#conversation-list");
  if (!conv.length) {
    list.replaceChildren(el("div", "empty", "No conversations yet. Start one below."));
    return;
  }
  list.replaceChildren(...conv.map(makeConvRow));
}

function makeConvRow(c) {
  const row = el("div", "conv");
  row.dataset.id = c.id;
  row.dataset.agent = c.agent_id;

  const top = el("div", "row");
  const agent = el("span", "agent", c.agent_id);
  if (agentsOnline[c.agent_id]) agent.prepend(onlineDot());

  const right = el("span", "row-right");
  right.append(el("span", "when", c.last_at ? fmtTime(c.last_at) : ""));
  const del = el("button", "del", "✕");
  del.title = "Delete conversation";
  del.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (confirm("Delete this conversation?")) deleteConversation(c.id, row);
  });
  right.append(del);

  top.append(agent, right);

  const stripped = c.last_body ? stripMarkdown(c.last_body) : "";
  const preview = el(
    "div", "preview",
    (c.last_sender === "agent" ? "" : "You: ") + (stripped || "—"),
  );

  row.append(top, preview);
  row.addEventListener("click", () => openConvo(c.id, c.agent_id));
  return row;
}

function onlineDot() {
  const d = el("span", "dot online-dot");
  d.title = "online";
  return d;
}

async function deleteConversation(id, rowEl) {
  const r = await api("/api/conversations/" + encodeURIComponent(id), { method: "DELETE" }).catch(() => null);
  if (r && r.ok) rowEl.remove();
}

$("#new-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const agent = $("#new-agent").value.trim();
  const body = $("#new-body").value.trim();
  if (!agent || !body) return;
  const r = await api("/api/conversations", { method: "POST", body: JSON.stringify({ agent, body }) }).catch(() => null);
  if (r && r.ok) {
    const d = await r.json();
    $("#new-agent").value = "";
    $("#new-body").value = "";
    openConvo(d.conversation_id, agent);
  }
});

// ── conversation view ──
async function openConvo(id, agentLabel) {
  state.conversationId = id;
  state.agentLabel = agentLabel || "";
  state.rendered = new Set();
  state.bubbles = new Map();
  $("#convo-title").textContent = agentLabel || "";
  updateConvoStatus();
  $("#messages").replaceChildren();
  show("convo");
  const r = await api("/api/conversations/" + encodeURIComponent(id)).catch(() => null);
  if (!r || !r.ok) return;
  const d = await r.json();
  (d.messages || []).forEach(appendMessage);
  refreshAgents(); // freshen the status dot
}

function appendMessage(m) {
  if (m.message_id && state.rendered.has(m.message_id)) return;
  if (m.message_id) state.rendered.add(m.message_id);
  const box = $("#messages");
  const isAgent = m.sender === "agent";
  const bubble = el("div", "bubble " + (isAgent ? "agent" : "user"));
  if (isAgent) {
    const md = el("div", "md");
    md.appendChild(renderMarkdown(m.body));
    bubble.appendChild(md);
  } else {
    bubble.appendChild(document.createTextNode(m.body == null ? "" : String(m.body)));
  }
  if (m.created_at) bubble.appendChild(el("span", "meta", fmtTime(m.created_at)));
  if (m.sender === "user") {
    if (m.message_id) state.bubbles.set(m.message_id, bubble);
    if (m.delivered_to_agent_at) addTick(bubble);
  }
  box.appendChild(bubble);
  box.scrollTop = box.scrollHeight;
}

function addTick(bubble) {
  if (bubble.querySelector(".tick")) return;
  bubble.appendChild(el("span", "tick", "✓ delivered"));
}

function markDelivered(ids) {
  for (const id of ids) {
    const b = state.bubbles.get(id);
    if (b) addTick(b);
  }
}

function showNote(text) {
  const box = $("#messages");
  box.appendChild(el("div", "note", text));
  box.scrollTop = box.scrollHeight;
}

$("#send-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#send-body");
  const body = input.value.trim();
  if (!body || !state.conversationId) return;
  input.value = "";
  try {
    const r = await api("/api/conversations/" + encodeURIComponent(state.conversationId) + "/messages",
      { method: "POST", body: JSON.stringify({ body }) });
    if (!r.ok) throw new Error("send failed");
    if (!agentsOnline[state.agentLabel]) {
      showNote(`${state.agentLabel} hasn’t connected recently — your message is queued and will deliver when it’s back.`);
    }
  } catch {
    setConn(false);
    showNote("Couldn’t send — you appear to be offline. Try again when reconnected.");
  }
});

$("#back-btn").addEventListener("click", async () => {
  state.conversationId = null;
  await loadList();
  show("list");
});

// ── live updates (SSE) ──
function startSSE() {
  if (sse) return;
  sse = new EventSource("/api/events");
  sse.onopen = () => setConn(true);
  sse.onerror = () => setConn(false);
  sse.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "message") {
      if (state.view === "convo" && m.conversation_id === state.conversationId) appendMessage(m);
      else if (state.view === "list") loadList();
    } else if (m.type === "delivered") {
      if (state.view === "convo" && m.conversation_id === state.conversationId) markDelivered(m.message_ids || []);
    } else if (m.type === "deleted") {
      if (state.view === "list") loadList(); // keep other open browsers in sync
    }
  };
}
function stopSSE() { if (sse) { sse.close(); sse = null; } }

// Keep presence fresh (agent online/offline flips as it stops polling).
setInterval(() => { if (state.view !== "login") refreshAgents(); }, 10000);

init();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () =>
    navigator.serviceWorker.register("/sw.js").catch(() => {}));
}
