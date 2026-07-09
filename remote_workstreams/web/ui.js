// All DOM rendering and control wiring. app.js owns transport, audio, and
// state; this module owns pixels. init(handlers) binds the controls once.

import { hasWebAuthn } from "./pairing.js";

const $ = (id) => document.getElementById(id);

const els = {
  menuBtn: $("btn-menu"),
  menu: $("menu"),
  stateChip: $("state-chip"),
  stateLabel: $("state-label"),
  lockBtn: $("btn-lock"),
  chat: $("chat"),
  chatEmpty: $("chat-empty"),
  approvals: $("approvals"),
  workstreams: $("workstreams"),
  wsDots: $("ws-dots"),
  planBtn: $("btn-plan"),
  compactBtn: $("btn-compact"),
  clearBtn: $("btn-clear"),
  muteBtn: $("btn-mute"),
  hushBtn: $("btn-hush"),
  keyboardBtn: $("btn-keyboard"),
  composerInput: $("composer-input"),
  composerSend: $("composer-send"),
  toast: $("toast"),
  screenLogin: $("screen-login"),
  screenPairing: $("screen-pairing"),
  loginUnlock: $("login-unlock"),
  loginPair: $("login-pair"),
  pairForm: $("pair-form"),
  pairPin: $("pair-pin"),
  pairSubmit: $("pair-submit"),
  pairBack: $("pair-back"),
  pairError: $("pair-error"),
};

let handlers = {};
let connState = "offline"; // offline | connecting | connected
let pipeState = "listening"; // protocol pipeline state
const pending = { user: null, assistant: null }; // interim chat bubbles
let wsCount = 0; // workstream cards last render; a new one clears planPending
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
  els.loginUnlock.addEventListener("click", () => handlers.onUnlock());
  els.loginPair.addEventListener("click", () => showPairing(hasWebAuthn()));
  els.pairBack.addEventListener("click", showLogin);
  els.pairForm.addEventListener("submit", (e) => {
    e.preventDefault();
    handlers.onPair(els.pairPin.value.trim());
  });
  els.muteBtn.addEventListener("click", () => handlers.onMute());
  // Both mutes must be instant, like Lock — no confirm tap.
  els.hushBtn.addEventListener("click", () => handlers.onHush());
  els.keyboardBtn.addEventListener("click", () => setComposerOpen(true));
  els.composerInput.addEventListener("blur", () => {
    // Dismissing the keyboard with nothing typed folds the input away.
    if (!els.composerInput.value.trim()) setComposerOpen(false);
  });
  els.composerSend.addEventListener("click", sendComposer);
  els.composerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendComposer();
  });
  confirmTap(els.planBtn, () => {
    if (handlers.onNewWorkstream()) planPending(true);
  });
  confirmTap(els.compactBtn, () => {
    if (handlers.onCompact()) compactPending(true); // spins until "compacted" arrives
  }, "Compact?");
  confirmTap(els.clearBtn, () => {
    if (handlers.onClearConvo()) toast("Starting a fresh conversation…");
  });
  els.workstreams.addEventListener("scroll", updateWsDots, { passive: true });
  els.menuBtn.addEventListener("click", () => {
    const open = els.menu.hidden;
    els.menu.hidden = !open;
    els.menuBtn.setAttribute("aria-expanded", String(open));
  });
  // Model buttons arm-then-confirm, and the armed state says what will happen:
  // blue "swap?" is safe; red "clear?" wipes the convo. Mirrors the server rule:
  // only a claude→claude convo pick switches live via /model — an engine switch
  // or codex→codex change clears and starts fresh.
  for (const btn of els.menu.querySelectorAll(".menu-models button")) {
    const target = btn.closest(".menu-models").dataset.target;
    btn.addEventListener("click", (e) => {
      // Already the pick for this row: nothing to swap to — don't even arm.
      if (btn.classList.contains("selected")) e.stopImmediatePropagation();
    });
    confirmTap(btn, () => {
      if (handlers.onSetModel(target, btn.dataset.model)) markModel(target, btn.dataset.model);
    }, () => {
      const sel = els.menu.querySelector(
        '.menu-models[data-target="convo"] button.selected'
      );
      const clears = target === "convo" && sel && btn.dataset.model !== sel.dataset.model
        && (btn.classList.contains("codex") || sel.classList.contains("codex"));
      btn.dataset.arm = clears ? "clear" : "swap";
      return clears ? "clear?" : "swap?";
    });
  }
  document.addEventListener("click", (e) => {
    if (els.menu.hidden || els.menu.contains(e.target) || els.menuBtn.contains(e.target)) return;
    closeMenu();
  });
  // Locking must be instant — no confirm tap.
  els.lockBtn.addEventListener("click", () => handlers.onLock());
}

// ---- settings menu ----

function closeMenu() {
  els.menu.hidden = true;
  els.menuBtn.setAttribute("aria-expanded", "false");
}

function markModel(target, model) {
  const seg = els.menu.querySelector(`.menu-models[data-target="${target}"]`);
  for (const btn of seg.children) btn.classList.toggle("selected", btn.dataset.model === model);
  applyModelVisibility();
}

export function setModels(convo, workstream) {
  markModel("convo", convo);
  markModel("workstream", workstream);
}

// Only models whose engine is wired on the Mac get buttons (the server sends
// the list). A selected model always stays visible, even misconfigured.
let enabledModels = null; // null = everything, until the first push arrives

export function setEnabledModels(models) {
  enabledModels = models;
  applyModelVisibility();
}

function applyModelVisibility() {
  if (!enabledModels) return;
  for (const btn of els.menu.querySelectorAll(".menu-models button")) {
    btn.hidden = !enabledModels.includes(btn.dataset.model)
      && !btn.classList.contains("selected");
  }
}

// ---- screens ----

export function showPairing(webauthnAvailable) {
  els.screenLogin.hidden = true;
  els.screenPairing.hidden = false;
  if (!webauthnAvailable) {
    els.pairSubmit.disabled = true;
    pairError("This browser has no WebAuthn support. Open the app in Safari on iOS 16 or later.");
  }
}

export function showLogin() {
  els.screenPairing.hidden = true;
  els.screenLogin.hidden = false;
  closeMenu();
}

export function hideScreens() {
  els.screenLogin.hidden = true;
  els.screenPairing.hidden = true;
}

export function pairBusy(busy) {
  els.pairSubmit.disabled = busy;
  els.pairSubmit.textContent = busy ? "Waiting for Face ID…" : "Continue with Face ID";
}

export function loginBusy(busy) {
  els.loginUnlock.disabled = busy;
  els.loginUnlock.textContent = busy ? "Waiting for Face ID…" : "Unlock with Face ID";
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
  connState = state; // no dedicated indicator — the state chip shows offline/connecting
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

export function setHushed(hushed) {
  els.hushBtn.setAttribute("aria-pressed", String(hushed));
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

// Typing is the rare path (links, mostly): at rest the keyboard pill stands in
// for the input; tapping it swaps in the real input + send.
function setComposerOpen(open) {
  els.composerInput.hidden = !open;
  els.composerSend.hidden = !open;
  els.keyboardBtn.hidden = open;
  if (open) els.composerInput.focus();
}

function sendComposer() {
  const text = els.composerInput.value.trim();
  if (!text) return;
  if (handlers.onText(text)) els.composerInput.value = "";
}

// ---- action bar ----

// Every button confirms the same way: first tap arms (accented label + "?"),
// a second within 2.6s fires, untouched it disarms itself. Idle text is
// captured at arm time — labels may change while idle (the compact buttons
// show context %); armedText overrides the default `${idle}?` when the idle
// label doesn't name the action. A function armedText is evaluated at arm
// time, for buttons whose consequence depends on current state.
function confirmTap(btn, fire, armedText) {
  const label = btn.querySelector(".label") || btn;
  let idle = null; // non-null while armed
  let timer = 0;
  const disarm = () => {
    clearTimeout(timer);
    btn.classList.remove("armed");
    if (idle !== null) label.textContent = idle;
    idle = null;
  };
  btn.addEventListener("click", () => {
    if (idle === null) {
      idle = label.textContent;
      btn.classList.add("armed");
      const armed = typeof armedText === "function" ? armedText() : armedText;
      label.textContent = armed || `${idle}?`;
      timer = setTimeout(disarm, 2600);
      return;
    }
    disarm();
    fire();
  });
}

export function planPending(on) {
  els.planBtn.classList.toggle("pending", on);
  els.planBtn.disabled = on;
}

export function compactPending(on) {
  els.compactBtn.classList.toggle("pending", on);
  els.compactBtn.disabled = on;
}

// The convo Compact button doubles as its context meter: "39%" idle, the verb
// only appears when armed.
export function setConvoContext(pct) {
  if (els.compactBtn.classList.contains("armed")) return; // next push refreshes it
  els.compactBtn.querySelector(".label").textContent = pct == null ? "Compact" : `${pct}%`;
}

// ---- workstream pager: one card visible, swipe sideways for the others ----

let wsSnapshot = ""; // skip re-rendering unchanged cards: pushes come every 5s

// The name's color carries the session's state: green waiting, blue waiting
// with subagents running, amber mid-turn, red errored or window gone.
function tone(ws) {
  if (ws.status === "gone" || ws.state === "error") return "red";
  if (ws.state === "thinking") return "amber";
  return ws.agents > 0 ? "blue" : "green";
}

export function renderWorkstreams(workstreams) {
  const added = workstreams.length > wsCount;
  if (added) planPending(false); // the launched workstream is the "done" signal
  wsCount = workstreams.length;

  const snap = JSON.stringify(
    workstreams.map((ws) => [
      ws.name, ws.title, ws.status, ws.state, ws.agents, ws.context_pct, ws.model,
    ])
  );
  if (snap === wsSnapshot) return; // rebuilding would kill swipe position + armed buttons
  wsSnapshot = snap;

  const chatPinned = pinned(els.chat); // cards below shrink the chat viewport
  const keepScroll = els.workstreams.scrollLeft;
  els.workstreams.replaceChildren(); // empty list renders nothing at all
  for (const ws of workstreams) {
    // Compact card: status dot + a state-colored name + the controls. Nothing else.
    const label = (ws.title || ws.name).slice(0, 40);
    const card = document.createElement("article");
    card.className = `ws ${ws.status}`;
    card.dataset.tone = tone(ws);

    const head = document.createElement("div");
    head.className = "ws-head";
    const dot = document.createElement("span");
    dot.className = "ws-dot";
    const title = document.createElement("span");
    title.className = "ws-title";
    title.textContent = label;
    head.append(dot, title);
    const model = document.createElement("span");
    model.className = "ws-model";
    model.textContent = ws.model; // the model name alone implies the engine
    head.append(model);
    if (ws.agents > 0) {
      const agents = document.createElement("span");
      agents.className = "ws-agents";
      agents.textContent = ws.agents;
      agents.setAttribute("aria-label", `${ws.agents} subagents running`);
      head.append(agents);
    }
    const end = document.createElement("button");
    end.className = "ws-end";
    end.textContent = "✕";
    end.setAttribute("aria-label", `End ${label}`);
    confirmTap(end, () => {
      if (handlers.onEndWorkstream(ws.name)) toast(`Ended ${label}.`);
    }, "End?");
    head.append(end);
    card.append(head);

    const actions = document.createElement("div");
    actions.className = "ws-actions";
    const sendBtn = document.createElement("button");
    sendBtn.className = "ws-send";
    sendBtn.textContent = "Send latest";
    confirmTap(sendBtn, () => {
      if (handlers.onSendToWorkstream(ws.name)) toast(`Routing the latest to ${label}…`);
    });
    const checkBtn = document.createElement("button");
    checkBtn.className = "ws-check";
    checkBtn.textContent = "Check in";
    confirmTap(checkBtn, () => {
      if (handlers.onCheckIn(ws.name)) toast(`Checking in on ${label}…`);
    });
    // Doubles as the context meter, like the convo Compact button.
    const compactBtn = document.createElement("button");
    compactBtn.className = "ws-compact";
    compactBtn.textContent = ws.context_pct == null ? "Compact" : `${ws.context_pct}%`;
    confirmTap(compactBtn, () => {
      if (handlers.onCompactWorkstream(ws.name)) toast(`Compacting ${label}…`);
    }, "Compact?");
    actions.append(sendBtn, checkBtn, compactBtn);
    card.append(actions);

    els.workstreams.append(card);
  }
  // Dots only when there's something to swipe to.
  els.wsDots.replaceChildren(
    ...(workstreams.length > 1 ? workstreams.map(() => document.createElement("span")) : [])
  );
  if (added) {
    els.workstreams.scrollLeft = els.workstreams.scrollWidth; // show the new card
  } else {
    els.workstreams.scrollLeft = keepScroll;
  }
  updateWsDots();
  keepPinned(els.chat, chatPinned);
}

function updateWsDots() {
  const dots = els.wsDots.children;
  if (!dots.length) return;
  const max = els.workstreams.scrollWidth - els.workstreams.clientWidth;
  const i = max > 0
    ? Math.round((els.workstreams.scrollLeft / max) * (dots.length - 1))
    : 0;
  for (let j = 0; j < dots.length; j++) dots[j].classList.toggle("active", j === i);
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
