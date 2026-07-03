// All DOM rendering and control wiring. app.js owns transport, audio, and
// state; this module owns pixels. init(handlers) binds the controls once.

const $ = (id) => document.getElementById(id);

const els = {
  connDot: $("conn-dot"),
  connLabel: $("conn-label"),
  sessionBtn: $("btn-session"),
  sessionTitle: $("session-title"),
  orb: $("orb"),
  stage: $("stage"),
  stateLabel: $("state-label"),
  muteChip: $("mute-chip"),
  transcript: $("transcript"),
  transcriptEmpty: $("transcript-empty"),
  composer: $("composer"),
  composerInput: $("composer-input"),
  composerSend: $("composer-send"),
  viewerBtn: $("btn-viewer"),
  viewerBadge: $("viewer-badge"),
  muteBtn: $("btn-mute"),
  keyboardBtn: $("btn-keyboard"),
  backdrop: $("backdrop"),
  sheetViewer: $("sheet-viewer"),
  sheetSessions: $("sheet-sessions"),
  eventFeed: $("event-feed"),
  sessionList: $("session-list"),
  unpairBtn: $("btn-unpair"),
  toast: $("toast"),
  screenStart: $("screen-start"),
  screenPairing: $("screen-pairing"),
  pairForm: $("pair-form"),
  pairToken: $("pair-token"),
  pairPin: $("pair-pin"),
  pairSubmit: $("pair-submit"),
  pairError: $("pair-error"),
};

let handlers = {};
let connState = "offline"; // offline | connecting | connected
let pipeState = "listening"; // protocol pipeline state
const pending = { user: null, assistant: null }; // interim transcript elements
let lastFinal = { role: null, el: null, t: 0 };
let approvalsOpen = 0;
let toastTimer = 0;

export function init(h) {
  handlers = h;
  els.screenStart.addEventListener("click", () => handlers.onStart());
  els.pairForm.addEventListener("submit", (e) => {
    e.preventDefault();
    handlers.onPair(els.pairToken.value.trim(), els.pairPin.value.trim());
  });
  els.muteBtn.addEventListener("click", () => handlers.onMute());
  els.keyboardBtn.addEventListener("click", toggleComposer);
  els.composerSend.addEventListener("click", sendComposer);
  els.composerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendComposer();
  });
  els.viewerBtn.addEventListener("click", () => openSheet(els.sheetViewer));
  els.sessionBtn.addEventListener("click", () => openSheet(els.sheetSessions));
  els.backdrop.addEventListener("click", closeSheets);
  for (const btn of document.querySelectorAll(".sheet-close")) {
    btn.addEventListener("click", closeSheets);
  }
  els.unpairBtn.addEventListener("click", () => {
    if (confirm("Forget this device's pairing? You'll need the token and PIN again.")) {
      handlers.onUnpair();
    }
  });
}

// ---- screens ----

export function showPairing(webauthnAvailable) {
  els.screenStart.hidden = true;
  els.screenPairing.hidden = false;
  if (!webauthnAvailable) {
    els.pairSubmit.disabled = true;
    pairError("This browser has no WebAuthn support. Open the app in Safari on iOS 16 or later.");
  }
}

export function showStart() {
  els.screenPairing.hidden = true;
  els.screenStart.hidden = false;
}

export function hideScreens() {
  els.screenStart.hidden = true;
  els.screenPairing.hidden = true;
}

export function pairBusy(busy) {
  els.pairSubmit.disabled = busy;
  els.pairSubmit.textContent = busy ? "Waiting for Face ID…" : "Continue with Face ID";
}

export function pairError(message) {
  els.pairError.textContent = message || "";
  els.pairError.hidden = !message;
}

// ---- orb + status ----

function applyOrb() {
  const shown = connState === "connected" ? pipeState : connState;
  els.orb.dataset.state = shown;
  els.stage.style.setProperty("--accent", `var(--${accentFor(shown)})`);
  els.stateLabel.textContent = shown;
  if (shown !== "listening" && shown !== "speaking") setLevel(0);
}

function accentFor(state) {
  switch (state) {
    case "listening":
      return "teal";
    case "thinking":
      return "amber";
    case "speaking":
      return "ember";
    case "interrupted":
      return "text";
    default:
      return "gray";
  }
}

export function setConnection(state) {
  connState = state;
  els.connDot.className = state;
  els.connLabel.textContent = state === "connecting" ? "connecting…" : state;
  applyOrb();
}

export function setState(state) {
  pipeState = state;
  applyOrb();
}

export function currentState() {
  return connState === "connected" ? pipeState : connState;
}

export function setLevel(v) {
  els.orb.style.setProperty("--level", Math.min(1, Math.max(0, v)).toFixed(3));
}

export function setMuted(muted) {
  els.muteBtn.setAttribute("aria-pressed", String(muted));
  els.muteChip.hidden = !muted;
}

// ---- transcript ----

function pinned(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 60;
}

function keepPinned(el, wasPinned) {
  if (wasPinned) el.scrollTop = el.scrollHeight;
}

export function addTranscript(role, text, final) {
  els.transcriptEmpty.hidden = true;
  const wasPinned = pinned(els.transcript);
  let el = pending[role];
  if (el) {
    // Interims replace wholesale (each carries the utterance so far).
    el.textContent = text;
    if (final) {
      el.classList.remove("interim");
      pending[role] = null;
      lastFinal = { role, el, t: Date.now() };
    }
    keepPinned(els.transcript, wasPinned);
    return;
  }
  // Sentence-chunked finals from one utterance merge into one block.
  if (final && lastFinal.role === role && lastFinal.el?.isConnected && Date.now() - lastFinal.t < 10000) {
    lastFinal.el.textContent += " " + text;
    lastFinal.t = Date.now();
    keepPinned(els.transcript, wasPinned);
    return;
  }
  el = document.createElement("p");
  el.className = `turn ${role}${final ? "" : " interim"}`;
  el.textContent = text;
  els.transcript.append(el);
  if (final) lastFinal = { role, el, t: Date.now() };
  else pending[role] = el;
  keepPinned(els.transcript, wasPinned);
}

export function clearTranscript() {
  els.transcript.replaceChildren(els.transcriptEmpty);
  els.transcriptEmpty.hidden = false;
  pending.user = pending.assistant = null;
  lastFinal = { role: null, el: null, t: 0 };
}

// ---- composer ----

function toggleComposer() {
  const show = els.composer.hidden;
  els.composer.hidden = !show;
  els.keyboardBtn.setAttribute("aria-pressed", String(show));
  if (show) els.composerInput.focus();
}

function sendComposer() {
  const text = els.composerInput.value.trim();
  if (!text) return;
  if (handlers.onText(text)) els.composerInput.value = "";
}

// ---- workspace viewer ----

function clockTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// Keys are voicecode/events.py type literals (quoted so the drift test sees them).
const GLYPHS = {
  "task_started": "▸",
  "progress": "·",
  "finding": "◆",
  "completed": "✓",
  "error": "✕",
};

function feedEmpty() {
  const msg = els.eventFeed.querySelector(".sheet-empty");
  if (msg) msg.remove();
}

export function addEvent(evt) {
  feedEmpty();
  const row = document.createElement("div");
  row.className = `evt ${evt.type}`;
  const glyph = document.createElement("span");
  glyph.className = "evt-glyph";
  glyph.textContent = GLYPHS[evt.type] || "·";
  const body = document.createElement("div");
  const summary = document.createElement("p");
  summary.className = "evt-summary";
  summary.textContent = evt.summary;
  body.append(summary);
  if (evt.detail) {
    const detail = document.createElement("p");
    detail.className = "evt-detail";
    detail.textContent = evt.detail;
    body.append(detail);
  }
  const time = document.createElement("time");
  time.textContent = clockTime(evt.ts);
  row.append(glyph, body, time);
  appendToFeed(row);
}

export function addApproval(evt) {
  feedEmpty();
  const card = document.createElement("div");
  card.className = "approval";

  const head = document.createElement("div");
  head.className = "approval-head";
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = "approval required";
  const tool = document.createElement("span");
  tool.className = "tool";
  tool.textContent = evt.tool_name;
  head.append(tag, tool);

  const summary = document.createElement("p");
  summary.className = "evt-summary";
  summary.textContent = evt.summary;
  card.append(head, summary);

  if (evt.detail) {
    const detail = document.createElement("p");
    detail.className = "evt-detail";
    detail.textContent = evt.detail;
    card.append(detail);
  }

  const actions = document.createElement("div");
  actions.className = "approval-actions";
  const deny = document.createElement("button");
  deny.className = "btn-deny";
  deny.textContent = "Deny";
  const approve = document.createElement("button");
  approve.className = "btn-approve";
  approve.textContent = "Approve";
  const decide = (approved) => {
    // Only resolve the card if the verdict actually reached the server.
    if (!handlers.onApproval(evt.gate_id, approved)) return;
    actions.remove();
    card.classList.add("resolved");
    const verdict = document.createElement("p");
    verdict.className = `approval-verdict ${approved ? "yes" : "no"}`;
    verdict.textContent = approved ? "✓ approved" : "✕ denied";
    card.append(verdict);
    bumpBadge(-1);
  };
  deny.addEventListener("click", () => decide(false));
  approve.addEventListener("click", () => decide(true));
  actions.append(deny, approve);
  card.append(actions);

  appendToFeed(card);
  bumpBadge(1);
}

function appendToFeed(el) {
  const wasPinned = pinned(els.eventFeed);
  els.eventFeed.append(el);
  keepPinned(els.eventFeed, wasPinned);
}

function bumpBadge(delta) {
  approvalsOpen = Math.max(0, approvalsOpen + delta);
  els.viewerBadge.textContent = String(approvalsOpen);
  els.viewerBadge.hidden = approvalsOpen === 0;
}

export function clearEvents() {
  els.eventFeed.replaceChildren();
  const msg = document.createElement("p");
  msg.className = "sheet-empty";
  msg.textContent = "No activity yet. Execution events land here.";
  els.eventFeed.append(msg);
  approvalsOpen = 0;
  bumpBadge(0);
}

// ---- sessions ----

function relTime(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "now";
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function renderSessions(sessions, currentId) {
  els.sessionList.replaceChildren();
  if (sessions.length === 0) {
    const msg = document.createElement("p");
    msg.className = "sheet-empty";
    msg.textContent = "No sessions reported yet.";
    els.sessionList.append(msg);
    return;
  }
  for (const s of sessions) {
    const row = document.createElement("button");
    row.className = `session${s.id === currentId ? " current" : ""}`;
    const dot = document.createElement("span");
    dot.className = "dot";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = s.title || "untitled";
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = relTime(s.last_active);
    row.append(dot, name, when);
    row.addEventListener("click", () => {
      if (s.id !== currentId) handlers.onSwitchSession(s.id);
      closeSheets();
    });
    els.sessionList.append(row);
  }
  const current = sessions.find((s) => s.id === currentId);
  if (current) els.sessionTitle.textContent = current.title || "session";
}

// ---- sheets + toast ----

function openSheet(sheet) {
  closeSheets();
  sheet.classList.add("open");
  els.backdrop.classList.add("open");
}

export function closeSheets() {
  els.sheetViewer.classList.remove("open");
  els.sheetSessions.classList.remove("open");
  els.backdrop.classList.remove("open");
}

export function toast(message, isError = false) {
  clearTimeout(toastTimer);
  els.toast.textContent = message;
  els.toast.className = isError ? "error" : "";
  els.toast.hidden = false;
  toastTimer = setTimeout(() => {
    els.toast.hidden = true;
  }, isError ? 6000 : 3500);
}
