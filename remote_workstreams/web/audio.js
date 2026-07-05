// Mic capture (worklet-backed) and gapless PCM playback on one AudioContext.
// Formats come from the server's Ready message: mic pcm_s16le 16 kHz mono up,
// TTS pcm_s16le 24 kHz mono down.

export class MicCapture {
  constructor(ctx) {
    this.ctx = ctx;
    this.targetRate = 16000;
    this.onchunk = null; // (arrayBuffer, level) => void
    this.onmute = null; // iOS took the mic: phone lock, Siri, a phone call
    this.onunmute = null;
    this.stream = null;
    this.node = null;
    this.source = null;
    this.sink = null;
  }

  async start() {
    // echoCancellation is load-bearing: barge-in must not trigger on our own TTS.
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
    const track = this.stream.getAudioTracks()[0];
    track.onmute = () => this.onmute?.();
    track.onunmute = () => this.onunmute?.();
    await this.ctx.audioWorklet.addModule("audio-worklet.js");
    this.node = new AudioWorkletNode(this.ctx, "mic-capture", {
      processorOptions: { targetRate: this.targetRate },
    });
    this.node.port.onmessage = (e) => {
      if (this.onchunk) this.onchunk(e.data.pcm, e.data.level);
    };
    this.source = this.ctx.createMediaStreamSource(this.stream);
    // Zero-gain sink: Safari only pulls the worklet if the graph reaches destination.
    this.sink = this.ctx.createGain();
    this.sink.gain.value = 0;
    this.source.connect(this.node);
    this.node.connect(this.sink);
    this.sink.connect(this.ctx.destination);
  }

  setTargetRate(rate) {
    this.targetRate = rate;
    if (this.node) this.node.port.postMessage({ targetRate: rate });
  }

  live() {
    if (!this.stream) return false;
    return this.stream.getAudioTracks().some((t) => t.readyState === "live");
  }

  async restart() {
    this.stop();
    await this.start();
  }

  stop() {
    if (this.stream) for (const t of this.stream.getTracks()) t.stop();
    if (this.source) this.source.disconnect();
    if (this.node) this.node.disconnect();
    if (this.sink) this.sink.disconnect();
    this.stream = this.node = this.source = this.sink = null;
  }
}

export class Playback {
  constructor(ctx) {
    this.ctx = ctx;
    this.rate = 24000;
    this.gain = ctx.createGain();
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 512;
    this.gain.connect(this.analyser);
    // Sink through a media element, not ctx.destination: iOS mutes Web-Audio-only
    // output on the ringer switch; element playback uses the media category and
    // keeps speaking with the phone on silent. Scheduling stays on the ctx clock.
    this.mediaDest = ctx.createMediaStreamDestination();
    this.analyser.connect(this.mediaDest);
    this.el = new Audio();
    this.el.srcObject = this.mediaDest.stream;
    this.el.playsInline = true;
    document.body.appendChild(this.el);
    // iOS pauses live-stream elements on audio-session interruptions (mic
    // category switches, route changes); resume instantly or speech dies mid-word.
    // Only while the context renders, though: a paused context feeds the element
    // no frames and WebKit screeches the last one forever, so when iOS interrupts
    // the context (app switch, phone lock) the element must pause with it.
    this.unlocked = false;
    this.el.addEventListener("pause", () => {
      if (this.unlocked && this.ctx.state === "running") this.el.play().catch(() => {});
    });
    this.ctx.addEventListener("statechange", () => {
      if (this.ctx.state !== "running") this.el.pause();
      else if (this.unlocked && this.el.paused) this.el.play().catch(() => {});
    });
    this.nextTime = 0;
    this.active = new Set();
    this.waveform = new Uint8Array(this.analyser.fftSize);
    this._watchdog = 0;
  }

  // Must be called from a user gesture once; keeps the element playing after.
  async unlock() {
    try {
      await this.el.play();
      this.unlocked = true;
      return true;
    } catch {
      return false;
    }
  }

  // While scheduled audio remains, keep the element playing and the ctx running.
  _watch() {
    if (this._watchdog) return;
    this._watchdog = setInterval(() => {
      if (this.ctx.currentTime >= this.nextTime) {
        clearInterval(this._watchdog);
        this._watchdog = 0;
        return;
      }
      if (this.ctx.state === "suspended") this.ctx.resume().catch(() => {});
      if (this.unlocked && this.el.paused && this.ctx.state === "running") this.el.play().catch(() => {});
    }, 300);
  }

  setRate(rate) {
    this.rate = rate;
  }

  // One binary WebSocket frame of s16le mono PCM; schedule it flush against the
  // previous chunk on the context clock so the stream is gapless.
  enqueue(arrayBuffer) {
    const samples = new Int16Array(arrayBuffer, 0, arrayBuffer.byteLength >> 1);
    if (samples.length === 0) return;
    const buffer = this.ctx.createBuffer(1, samples.length, this.rate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 32768;
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.gain);
    const at = Math.max(this.nextTime, this.ctx.currentTime + 0.06);
    src.start(at);
    this.nextTime = at + buffer.duration;
    this.active.add(src);
    src.onended = () => this.active.delete(src);
    this._watch();
  }

  // Barge-in kill: stop everything scheduled, immediately.
  flush() {
    for (const src of this.active) {
      src.onended = null;
      try {
        src.stop();
      } catch {
        // already ended
      }
      src.disconnect();
    }
    this.active.clear();
    this.nextTime = 0;
  }

  // speech_end: the utterance's binary stream is complete. Keep the schedule
  // cursor — a following utterance must queue after this one's tail, and the
  // Math.max in enqueue() already handles a cursor that fell into the past.
  endUtterance() {}

  // RMS of what is audible right now, 0..1, for the speaking orb.
  level() {
    this.analyser.getByteTimeDomainData(this.waveform);
    let sum = 0;
    for (let i = 0; i < this.waveform.length; i++) {
      const v = (this.waveform[i] - 128) / 128;
      sum += v * v;
    }
    return Math.min(1, Math.sqrt(sum / this.waveform.length) * 3);
  }
}
