"use strict";

const $ = (s) => document.querySelector(s);
const api = (path, opts = {}) =>
  fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });

const fmtTime = (iso) => {
  try { return new Date(iso).toLocaleString(); } catch { return ""; }
};

// All message/agent text is rendered via textContent / createElement — never
// innerHTML — so untrusted content cannot inject markup (PRD §12).
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

const state = { view: "login", conversationId: null, agentLabel: "", rendered: new Set() };
let sse = null;

function show(view) {
  state.view = view;
  for (const id of ["login", "list", "convo"]) {
    document.getElementById("view-" + id).hidden = id !== view;
  }
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
  const r = await api("/api/login", { method: "POST", body: JSON.stringify({ secret }) });
  if (r.ok) {
    $("#login-secret").value = "";
    startSSE();
    await loadList();
    show("list");
  } else {
    err.textContent = r.status === 429
      ? "Too many attempts — locked out, wait a bit."
      : "Incorrect PIN or token.";
    err.hidden = false;
  }
});

$("#logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  stopSSE();
  show("login");
});

// ── conversation list ──
async function loadList() {
  const [conv, agents] = await Promise.all([
    api("/api/conversations").then((r) => r.json()).then((d) => d.conversations || []).catch(() => []),
    api("/api/agents").then((r) => r.json()).then((d) => d.agents || []).catch(() => []),
  ]);

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
  top.append(
    el("span", "agent", c.agent_id),
    el("span", "when", c.last_at ? fmtTime(c.last_at) : ""),
  );

  const preview = el(
    "div", "preview",
    (c.last_sender === "agent" ? "" : "You: ") + (c.last_body || "—"),
  );

  row.append(top, preview);
  row.addEventListener("click", () => openConvo(c.id, c.agent_id));
  return row;
}

$("#new-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const agent = $("#new-agent").value.trim();
  const body = $("#new-body").value.trim();
  if (!agent || !body) return;
  const r = await api("/api/conversations", { method: "POST", body: JSON.stringify({ agent, body }) });
  if (r.ok) {
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
  $("#convo-title").textContent = agentLabel || "";
  $("#messages").replaceChildren();
  show("convo");
  const r = await api("/api/conversations/" + encodeURIComponent(id));
  if (!r.ok) return;
  const d = await r.json();
  (d.messages || []).forEach(appendMessage);
}

function appendMessage(m) {
  if (m.message_id && state.rendered.has(m.message_id)) return;
  if (m.message_id) state.rendered.add(m.message_id);
  const box = $("#messages");
  const bubble = el("div", "bubble " + (m.sender === "agent" ? "agent" : "user"), m.body);
  if (m.created_at) bubble.appendChild(el("span", "meta", fmtTime(m.created_at)));
  box.appendChild(bubble);
  box.scrollTop = box.scrollHeight;
}

$("#send-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#send-body");
  const body = input.value.trim();
  if (!body || !state.conversationId) return;
  input.value = "";
  await api("/api/conversations/" + encodeURIComponent(state.conversationId) + "/messages",
    { method: "POST", body: JSON.stringify({ body }) });
  // the message echoes back over SSE and is appended there (deduped by id)
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
  sse.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type !== "message") return;
    if (state.view === "convo" && m.conversation_id === state.conversationId) {
      appendMessage(m);
    } else if (state.view === "list") {
      loadList();
    }
  };
  sse.onerror = () => { /* EventSource auto-reconnects */ };
}
function stopSSE() { if (sse) { sse.close(); sse = null; } }

init();
