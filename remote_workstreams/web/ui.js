// All DOM rendering and control wiring. app.js owns transport, audio, and
// state; this module owns pixels. init(handlers) binds the controls once.

const $ = (id) => document.getElementById(id);

const els = {
  connDot: $("conn-dot"),
  connLabel: $("conn-label"),
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
  els.loginPair.addEventListener("click", () => showPairing(true));
  els.pairBack.addEventListener("click", showLogin);
  els.pairForm.addEventListener("submit", (e) => {
    e.preventDefault();
    handlers.onPair(els.pairPin.value.trim());
  });
  els.muteBtn.addEventListener("click", () => handlers.onMute());
  els.composerSend.addEventListener("click", sendComposer);
  els.composerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendComposer();
  });
  confirmTap(els.planBtn, () => {
    if (handlers.onNewWorkstream()) planPending(true);
  });
  confirmTap(els.compactBtn, () => {
    if (handlers.onCompact()) toast("Compacting the conversation…");
  });
  confirmTap(els.clearBtn, () => {
    if (handlers.onClearConvo()) toast("Starting a fresh conversation…");
  });
  els.workstreams.addEventListener("scroll", updateWsDots, { passive: true });
  // Locking must be instant — no confirm tap.
  els.lockBtn.addEventListener("click", () => handlers.onLock());
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

// Every action-bar button confirms the same way: first tap arms (accented
// label + "?"), a second within 2.6s fires, untouched it disarms itself.
function confirmTap(btn, fire) {
  const label = btn.querySelector(".label");
  const idle = label.textContent;
  let timer = 0;
  const disarm = () => {
    clearTimeout(timer);
    btn.classList.remove("armed");
    label.textContent = idle;
  };
  btn.addEventListener("click", () => {
    if (!btn.classList.contains("armed")) {
      btn.classList.add("armed");
      label.textContent = `${idle}?`;
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

// ---- workstream pager: one card visible, swipe sideways for the others ----

let wsSnapshot = ""; // skip re-rendering unchanged cards: pushes come every 5s

export function renderWorkstreams(workstreams) {
  const added = workstreams.length > wsCount;
  if (added) planPending(false); // the launched workstream is the "done" signal
  wsCount = workstreams.length;

  const snap = JSON.stringify(workstreams.map((ws) => [ws.name, ws.title, ws.status]));
  if (snap === wsSnapshot) return; // rebuilding would kill swipe position + armed ✕
  wsSnapshot = snap;

  const chatPinned = pinned(els.chat); // cards below shrink the chat viewport
  const keepScroll = els.workstreams.scrollLeft;
  els.workstreams.replaceChildren(); // empty list renders nothing at all
  for (const ws of workstreams) {
    // Compact card: status dot + a clear name + the two controls. Nothing else.
    const label = (ws.title || ws.name).slice(0, 40);
    const card = document.createElement("article");
    card.className = `ws ${ws.status}`;

    const head = document.createElement("div");
    head.className = "ws-head";
    const dot = document.createElement("span");
    dot.className = "ws-dot";
    const title = document.createElement("span");
    title.className = "ws-title";
    title.textContent = label;
    const end = document.createElement("button");
    end.className = "ws-end";
    end.textContent = "✕";
    end.setAttribute("aria-label", `End ${label}`);
    end.addEventListener("click", () => {
      // Arm-then-confirm: ending kills a live session.
      if (!end.classList.contains("armed")) {
        end.classList.add("armed");
        end.textContent = "End?";
        setTimeout(() => {
          end.classList.remove("armed");
          end.textContent = "✕";
        }, 3000);
        return;
      }
      if (handlers.onEndWorkstream(ws.name)) toast(`Ended ${label}.`);
    });
    head.append(dot, title, end);
    card.append(head);

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
