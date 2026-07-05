// voice-code phone client. Transport, audio, and app state live here; pixels
// live in ui.js. Speaks voicecode/protocol.py over one WebSocket: text frames
// are JSON messages, binary up is mic PCM, binary down is TTS PCM.
//
// Auth: the session token lives ONLY in app.session (a variable) — never in
// localStorage. Every fresh page load starts at the login screen; Lock wipes
// the token instantly.

import { MicCapture, Playback } from "./audio.js";
import { loginDevice, PairError, pairDevice } from "./pairing.js";
import * as ui from "./ui.js";

// The web can't distinguish phone-lock from an app switch; time hidden is the proxy.
const AUTO_LOCK_HIDDEN_S = 120;

const app = {
  ws: null,
  ctx: null,
  mic: null,
  playback: null,
  unlockPromise: null,
  session: null, // in-memory session token; null = locked
  muted: false,
  ready: false,
  started: false, // a logged-in session is live
  attempts: 0,
  reconnectTimer: 0,
  wakeLock: null,
  hiddenAt: 0,
};

// ---- boot ----

ui.init({
  onUnlock: unlock,
  onPair: pair,
  onLock: lock,
  onMute: toggleMute,
  onText: (text) => send({ type: "text_input", text }),
  onNewWorkstream: () => send({ type: "new_workstream" }),
  onCompact: () => send({ type: "compact" }),
  onClearConvo: () => send({ type: "clear_convo" }),
  onSendToWorkstream: (name) => send({ type: "send_to_workstream", workstream: name }),
  onCheckIn: (name) => send({ type: "check_in", workstream: name }),
  onEndWorkstream: (name) => send({ type: "end_workstream", workstream: name }),
  onApproval: (approvalId, approved) => send({ type: "approval", approval_id: approvalId, approved }),
});

ui.showLogin();
requestAnimationFrame(levelLoop);

// ---- login / pairing (both run inside a tap: iOS gates audio on a gesture) ----

// Must run synchronously in the tap, before the Face ID await eats the gesture.
function ensureAudio() {
  if (app.ctx) return;
  app.ctx = new (window.AudioContext || window.webkitAudioContext)();
  app.playback = new Playback(app.ctx);
  app.unlockPromise = app.playback.unlock();
}

async function unlock() {
  ensureAudio();
  ui.loginBusy(true);
  try {
    app.session = await loginDevice();
    await beginSession();
  } catch (err) {
    ui.toast(err instanceof PairError ? err.message : `Login failed: ${err.message}`, true);
  } finally {
    ui.loginBusy(false);
  }
}

async function pair(pin) {
  ensureAudio();
  ui.pairError("");
  ui.pairBusy(true);
  try {
    app.session = await pairDevice(pin);
    await beginSession();
  } catch (err) {
    ui.pairError(err instanceof PairError ? err.message : `Pairing failed: ${err.message}`);
  } finally {
    ui.pairBusy(false);
  }
}

// ---- lock: instant — wipe the token, drop everything, back to login ----

function lock() {
  app.session = null;
  app.started = false;
  app.ready = false;
  clearTimeout(app.reconnectTimer);
  const ws = app.ws;
  app.ws = null;
  if (ws) ws.close();
  app.mic?.stop();
  app.mic = null;
  app.playback?.flush();
  ui.setConnection("offline");
  ui.showLogin();
}

// ---- session start ----

async function beginSession() {
  if (app.started) return;
  app.started = true;
  ui.hideScreens();

  // Network first: audio unlock must never delay (or wedge) the connection.
  app.attempts = 0;
  connect();
  requestWakeLock();

  await app.ctx.resume().catch(() => {});
  if (!(await app.unlockPromise)) ui.toast("Audio output blocked — reload and retry", true);

  try {
    app.mic = new MicCapture(app.ctx);
    await app.mic.start();
    app.mic.onchunk = (pcm, level) => {
      if (!app.muted && app.ready && app.ws?.readyState === WebSocket.OPEN) app.ws.send(pcm);
      if (ui.currentState() === "listening" && !app.muted) ui.setLevel(level * 4);
    };
  } catch (err) {
    app.mic = null;
    ui.toast(`Microphone unavailable (${err.name}). Text input still works.`, true);
  }
}

function levelLoop() {
  if (app.playback && ui.currentState() === "speaking") ui.setLevel(app.playback.level());
  requestAnimationFrame(levelLoop);
}

// ---- transport ----

function connect() {
  clearTimeout(app.reconnectTimer);
  if (app.ws && app.ws.readyState !== WebSocket.CLOSED) return;
  ui.setConnection("connecting");

  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  app.ws = ws;

  // A socket stuck CONNECTING (unreachable host on flaky LTE) must fail into backoff.
  const connectTimeout = setTimeout(() => {
    if (ws.readyState === WebSocket.CONNECTING) ws.close();
  }, 8000);

  ws.onopen = () => {
    clearTimeout(connectTimeout);
    app.attempts = 0;
    // Reconnects re-present the in-memory session token; no re-auth while it lives.
    ws.send(JSON.stringify({ type: "hello", credential: app.session }));
    if (app.muted) ws.send(JSON.stringify({ type: "mute", muted: true }));
  };

  ws.onmessage = (e) => {
    if (typeof e.data === "string") {
      let msg;
      try {
        msg = JSON.parse(e.data);
      } catch {
        return;
      }
      handleMessage(msg);
    } else {
      app.playback.enqueue(e.data);
    }
  };

  ws.onclose = () => {
    clearTimeout(connectTimeout);
    if (app.ws !== ws) return;
    app.ready = false;
    ui.setConnection("offline");
    ui.planPending(false); // the pending workstream can't report back on a dead socket
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  if (!app.started) return;
  const delay = Math.min(500 * 2 ** app.attempts, 10000) * (0.75 + Math.random() * 0.5);
  app.attempts += 1;
  app.reconnectTimer = setTimeout(connect, delay);
}

function send(msg) {
  if (app.ws?.readyState !== WebSocket.OPEN) {
    ui.toast("Not connected.", true);
    return false;
  }
  app.ws.send(JSON.stringify(msg));
  return true;
}

// ---- protocol (server → client) ----

function handleMessage(msg) {
  switch (msg.type) {
    case "ready": {
      if (msg.mic_format.encoding !== "pcm_s16le" || msg.tts_format.encoding !== "pcm_s16le") {
        ui.toast(`Server wants ${msg.mic_format.encoding}; this client only speaks pcm_s16le.`, true);
        return;
      }
      app.mic?.setTargetRate(msg.mic_format.sample_rate);
      app.playback.setRate(msg.tts_format.sample_rate);
      // The server replays the full chat history after every ready.
      ui.clearChat();
      ui.clearApprovals();
      app.ready = true;
      ui.setConnection("connected");
      ui.setState("listening");
      break;
    }
    case "state":
      // Barge-in kill only. "listening" means the server finished SENDING —
      // the buffer here may still hold seconds of unplayed speech; let it play.
      if (msg.state === "interrupted") app.playback.flush();
      ui.setState(msg.state);
      break;
    case "chat":
      ui.addChat(msg.role, msg.text, msg.final);
      break;
    case "speech_end":
      app.playback.endUtterance();
      break;
    case "workstreams":
      ui.renderWorkstreams(msg.workstreams);
      break;
    case "convo_cleared":
      ui.clearChat();
      ui.toast("Fresh conversation.");
      break;
    case "approval_request":
      ui.addApproval(msg);
      break;
    case "error":
      // The session token died (expiry or server restart): back to the login screen.
      if (msg.message === "invalid credential") {
        lock();
        ui.toast("Session expired — unlock again.", true);
        return;
      }
      ui.planPending(false); // the pending workstream may be what errored
      ui.toast(msg.message, true);
      break;
    default:
      break;
  }
}

// ---- controls ----

function toggleMute() {
  app.muted = !app.muted;
  ui.setMuted(app.muted);
  if (app.muted) ui.setLevel(0);
  send({ type: "mute", muted: app.muted });
}

// ---- iOS lifecycle: Safari suspends the tab; treat return like a dropped phone ----

async function resumeFromSuspend() {
  if (document.visibilityState !== "visible") return;
  // Auto-lock wins over resume after prolonged backgrounding; quick app switches stay seamless.
  const hiddenFor = app.hiddenAt ? Date.now() - app.hiddenAt : 0;
  app.hiddenAt = 0;
  if (app.started && hiddenFor > AUTO_LOCK_HIDDEN_S * 1000) {
    lock();
    return;
  }
  if (!app.started) return;
  await app.ctx.resume().catch(() => {});
  if (app.mic && !app.mic.live()) {
    await app.mic.restart().catch(() => ui.toast("Microphone lost. Reopen the app.", true));
  }
  if (!app.ws || app.ws.readyState === WebSocket.CLOSED || app.ws.readyState === WebSocket.CLOSING) {
    app.attempts = 0;
    connect();
  }
  requestWakeLock();
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") app.hiddenAt = Date.now();
  else resumeFromSuspend();
});
window.addEventListener("pageshow", resumeFromSuspend);
window.addEventListener("online", resumeFromSuspend);

// Keep the screen on while a session is live — a dark phone kills PWA audio.
async function requestWakeLock() {
  try {
    app.wakeLock = await navigator.wakeLock?.request("screen");
  } catch {
    // Denied or unsupported; harmless.
  }
}
