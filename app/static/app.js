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
    err.textContent = r && r.status === 429
      ? "Too many attempts — locked out, wait a bit."
      : "Incorrect PIN or token.";
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

  const preview = el(
    "div", "preview",
    (c.last_sender === "agent" ? "" : "You: ") + (c.last_body || "—"),
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
  const bubble = el("div", "bubble " + (m.sender === "agent" ? "agent" : "user"), m.body);
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
