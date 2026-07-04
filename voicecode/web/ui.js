// All DOM rendering and control wiring. app.js owns transport, audio, and
// state; this module owns pixels. init(handlers) binds the controls once.

const $ = (id) => document.getElementById(id);

const els = {
  connDot: $("conn-dot"),
  connLabel: $("conn-label"),
  stateChip: $("state-chip"),
  stateLabel: $("state-label"),
  unpairBtn: $("btn-unpair"),
  chat: $("chat"),
  chatEmpty: $("chat-empty"),
  approvals: $("approvals"),
  workstreams: $("workstreams"),
  planBtn: $("btn-plan"),
  compactBtn: $("btn-compact"),
  muteBtn: $("btn-mute"),
  composerInput: $("composer-input"),
  composerSend: $("composer-send"),
  backdrop: $("backdrop"),
  sheetPlan: $("sheet-plan"),
  planBody: $("plan-body"),
  planTitle: $("plan-title"),
  planText: $("plan-text"),
  planLaunch: $("btn-plan-launch"),
  planDismiss: $("btn-plan-dismiss"),
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
const pending = { user: null, assistant: null }; // interim chat bubbles
let planId = null; // plan_id of the stint plan currently in the sheet
let compactTimer = 0;
let toastTimer = 0;

// Chip copy per pipeline state (quoted so the protocol drift test sees them).
const STATE_COPY = {
  "listening": "listening",
  "thinking": "thinking",
  "speaking": "speaking",
  "interrupted": "interrupted",
};

export function init(h) {
  handlers = h;
  els.screenStart.addEventListener("click", () => handlers.onStart());
  els.pairForm.addEventListener("submit", (e) => {
    e.preventDefault();
    handlers.onPair(els.pairToken.value.trim(), els.pairPin.value.trim());
  });
  els.muteBtn.addEventListener("click", () => handlers.onMute());
  els.composerSend.addEventListener("click", sendComposer);
  els.composerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendComposer();
  });
  els.planBtn.addEventListener("click", () => {
    if (handlers.onPlanStint()) planPending(true);
  });
  els.compactBtn.addEventListener("click", compactTap);
  els.planLaunch.addEventListener("click", () => {
    if (planId !== null && handlers.onLaunch(planId)) hideStintPlan();
  });
  els.planDismiss.addEventListener("click", hideStintPlan);
  els.backdrop.addEventListener("click", hideStintPlan);
  for (const btn of document.querySelectorAll(".sheet-close")) {
    btn.addEventListener("click", hideStintPlan);
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

// ---- connection + state chip ----

function applyChip() {
  const shown = connState === "connected" ? pipeState : connState;
  els.stateChip.dataset.state = shown;
  els.stateLabel.textContent = STATE_COPY[shown] || shown;
  if (shown !== "listening" && shown !== "speaking") setLevel(0);
}

export function setConnection(state) {
  connState = state;
  els.connDot.className = state;
  els.connLabel.textContent = state === "connecting" ? "connecting…" : state;
  applyChip();
}

export function setState(state) {
  pipeState = state;
  applyChip();
}

export function currentState() {
  return connState === "connected" ? pipeState : connState;
}

export function setLevel(v) {
  els.stateChip.style.setProperty("--level", Math.min(1, Math.max(0, v)).toFixed(3));
}

export function setMuted(muted) {
  els.muteBtn.setAttribute("aria-pressed", String(muted));
}

// ---- chat ----

function pinned(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 60;
}

function keepPinned(el, wasPinned) {
  if (wasPinned) el.scrollTop = el.scrollHeight;
}

export function addChat(role, text, final) {
  els.chatEmpty.hidden = true;
  const wasPinned = pinned(els.chat);
  if (role === "activity") {
    const el = document.createElement("p");
    el.className = "turn activity";
    el.textContent = text;
    els.chat.append(el);
    keepPinned(els.chat, wasPinned);
    return;
  }
  let el = pending[role];
  if (el) {
    // Interims replace wholesale (each carries the utterance so far); the
    // final replaces the interim's text and clears the slot.
    el.textContent = text;
    if (final) {
      el.classList.remove("interim");
      pending[role] = null;
    }
  } else {
    el = document.createElement("p");
    el.className = `turn ${role}${final ? "" : " interim"}`;
    el.textContent = text;
    els.chat.append(el);
    if (!final) pending[role] = el;
  }
  keepPinned(els.chat, wasPinned);
}

export function clearChat() {
  els.chat.replaceChildren(els.chatEmpty);
  els.chatEmpty.hidden = false;
  pending.user = pending.assistant = null;
}

// ---- composer ----

function sendComposer() {
  const text = els.composerInput.value.trim();
  if (!text) return;
  if (handlers.onText(text)) els.composerInput.value = "";
}

// ---- action bar ----

export function planPending(on) {
  els.planBtn.classList.toggle("pending", on);
  els.planBtn.disabled = on;
}

// Compact needs a confirm tap: first tap arms, second within 2.6s fires.
function compactTap() {
  if (!els.compactBtn.classList.contains("armed")) {
    els.compactBtn.classList.add("armed");
    els.compactBtn.textContent = "Compact?";
    compactTimer = setTimeout(disarmCompact, 2600);
    return;
  }
  disarmCompact();
  if (handlers.onCompact()) toast("Compacting the conversation…");
}

function disarmCompact() {
  clearTimeout(compactTimer);
  els.compactBtn.classList.remove("armed");
  els.compactBtn.textContent = "Compact";
}

// ---- stint plan sheet ----

export function showStintPlan(plan) {
  planId = plan.plan_id;
  els.planTitle.textContent = plan.title || "Stint plan";
  els.planText.textContent = plan.text || "";
  els.planBody.scrollTop = 0;
  els.sheetPlan.classList.add("open");
  els.backdrop.classList.add("open");
}

export function hideStintPlan() {
  els.sheetPlan.classList.remove("open");
  els.backdrop.classList.remove("open");
}

// ---- workstream cards ----

function relTime(iso) {
  const t = Date.parse(iso); // protocol timestamps are ISO strings
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "now";
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function renderWorkstreams(workstreams) {
  const chatPinned = pinned(els.chat); // cards below shrink the chat viewport
  els.workstreams.replaceChildren(); // empty list renders nothing at all
  for (const ws of workstreams) {
    const label = ws.title || ws.name;
    const card = document.createElement("article");
    card.className = `ws ${ws.status}`;

    const head = document.createElement("div");
    head.className = "ws-head";
    const dot = document.createElement("span");
    dot.className = "ws-dot";
    const title = document.createElement("span");
    title.className = "ws-title";
    title.textContent = label;
    const when = document.createElement("span");
    when.className = "ws-when";
    when.textContent = ws.last_activity ? relTime(ws.last_activity) : "";
    head.append(dot, title, when);

    const name = document.createElement("p");
    name.className = "ws-name";
    name.textContent = `${ws.name} · ${ws.status}`;
    card.append(head, name);

    if (ws.tail?.length) {
      const tail = document.createElement("div");
      tail.className = "ws-tail";
      tail.textContent = ws.tail.join("\n");
      card.append(tail);
    }

    const actions = document.createElement("div");
    actions.className = "ws-actions";
    const sendBtn = document.createElement("button");
    sendBtn.textContent = "Send latest";
    sendBtn.addEventListener("click", () => {
      if (handlers.onSendToWorkstream(ws.name)) toast(`Routing the latest to ${label}…`);
    });
    const checkBtn = document.createElement("button");
    checkBtn.textContent = "Check in";
    checkBtn.addEventListener("click", () => {
      if (handlers.onCheckIn(ws.name)) toast(`Checking in on ${label}…`);
    });
    actions.append(sendBtn, checkBtn);
    card.append(actions);

    els.workstreams.append(card);
  }
  keepPinned(els.chat, chatPinned);
}

// ---- approval cards ----

export function addApproval(req) {
  const chatPinned = pinned(els.chat); // the card below shrinks the chat viewport
  const id = String(req.approval_id);
  // A reconnect can replay a still-open gate; replace, don't duplicate.
  els.approvals.querySelector(`[data-approval-id="${CSS.escape(id)}"]`)?.remove();

  const card = document.createElement("div");
  card.className = "approval";
  card.dataset.approvalId = id;

  const head = document.createElement("div");
  head.className = "approval-head";
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = "approval required";
  const tool = document.createElement("span");
  tool.className = "tool";
  tool.textContent = req.tool;
  head.append(tag, tool);

  const summary = document.createElement("p");
  summary.className = "approval-summary";
  summary.textContent = req.summary;

  const session = document.createElement("p");
  session.className = "approval-session";
  session.textContent = req.session;
  card.append(head, summary, session);

  // The server times the gate out around 60s; fade the card out to match.
  const expire = setTimeout(() => {
    card.classList.add("expired");
    setTimeout(() => card.remove(), 450);
  }, 60000);

  const actions = document.createElement("div");
  actions.className = "approval-actions";
  const deny = document.createElement("button");
  deny.className = "btn-deny";
  deny.textContent = "Deny";
  const approve = document.createElement("button");
  approve.className = "btn-approve";
  approve.textContent = "Approve";
  const decide = (approved) => {
    // Only dismiss the card if the verdict actually reached the server.
    if (!handlers.onApproval(req.approval_id, approved)) return;
    clearTimeout(expire);
    card.remove();
  };
  deny.addEventListener("click", () => decide(false));
  approve.addEventListener("click", () => decide(true));
  actions.append(deny, approve);
  card.append(actions);

  els.approvals.append(card);
  keepPinned(els.chat, chatPinned);
}

export function clearApprovals() {
  els.approvals.replaceChildren();
}

// ---- toast ----

export function toast(message, isError = false) {
  clearTimeout(toastTimer);
  els.toast.textContent = message;
  els.toast.className = isError ? "error" : "";
  els.toast.hidden = false;
  toastTimer = setTimeout(() => {
    els.toast.hidden = true;
  }, isError ? 6000 : 3500);
}
