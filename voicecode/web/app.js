// voice-code phone client. Transport, audio, and app state live here; pixels
// live in ui.js. Speaks voicecode/protocol.py over one WebSocket: text frames
// are JSON messages, binary up is mic PCM, binary down is TTS PCM.

import { MicCapture, Playback } from "./audio.js";
import { hasWebAuthn, PairError, pairDevice } from "./pairing.js";
import * as ui from "./ui.js";

const CRED_KEY = "voicecode.credential";

const app = {
  ws: null,
  ctx: null,
  mic: null,
  playback: null,
  muted: false,
  ready: false,
  started: false, // the one-time user gesture happened
  attempts: 0,
  reconnectTimer: 0,
  wakeLock: null,
};

const credential = () => localStorage.getItem(CRED_KEY);

// ---- boot ----

ui.init({
  onStart: beginSession,
  onPair: pair,
  onMute: toggleMute,
  onText: (text) => send({ type: "text_input", text }),
  onPlanStint: () => send({ type: "plan_stint" }),
  onCompact: () => send({ type: "compact" }),
  onLaunch: (planId) => send({ type: "launch_workstream", plan_id: planId }),
  onSendToWorkstream: (name) => send({ type: "send_to_workstream", workstream: name }),
  onCheckIn: (name) => send({ type: "check_in", workstream: name }),
  onApproval: (approvalId, approved) => send({ type: "approval", approval_id: approvalId, approved }),
  onUnpair: unpair,
});

if (credential()) ui.showStart();
else ui.showPairing(hasWebAuthn());

// ---- pairing ----

async function pair(token, pin) {
  ui.pairError("");
  ui.pairBusy(true);
  try {
    const cred = await pairDevice(token, pin);
    localStorage.setItem(CRED_KEY, cred);
    ui.showStart();
  } catch (err) {
    ui.pairError(err instanceof PairError ? err.message : `Pairing failed: ${err.message}`);
  } finally {
    ui.pairBusy(false);
  }
}

function unpair() {
  localStorage.removeItem(CRED_KEY);
  if (app.ws) app.ws.close();
  location.reload();
}

// ---- session start (must run inside a tap: iOS gates AudioContext on a gesture) ----

async function beginSession() {
  if (app.started) return;
  app.started = true;
  ui.hideScreens();

  app.ctx = new (window.AudioContext || window.webkitAudioContext)();
  await app.ctx.resume().catch(() => {});
  app.playback = new Playback(app.ctx);
  if (!(await app.playback.unlock())) ui.toast("Audio output blocked — reload and retry", true);

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

  connect();
  requestWakeLock();
  requestAnimationFrame(levelLoop);
}

function levelLoop() {
  if (ui.currentState() === "speaking") ui.setLevel(app.playback.level());
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
    // State is server-side: re-present the credential, no re-auth.
    ws.send(JSON.stringify({ type: "hello", credential: credential() }));
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
    ui.planPending(false); // a stint_plan can't arrive on a dead socket
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
      // Barge-in kill: anything queued is stale the instant the server leaves "speaking".
      if (msg.state === "interrupted" || msg.state === "listening") app.playback.flush();
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
    case "stint_plan":
      ui.planPending(false);
      ui.showStintPlan(msg);
      break;
    case "approval_request":
      ui.addApproval(msg);
      break;
    case "error":
      ui.planPending(false); // the pending plan_stint may be what errored
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
  if (!app.started || document.visibilityState !== "visible") return;
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

document.addEventListener("visibilitychange", resumeFromSuspend);
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
