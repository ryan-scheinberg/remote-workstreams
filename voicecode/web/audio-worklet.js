// Mic capture processor: float32 at the context rate in, 16 kHz signed 16-bit
// little-endian PCM out, posted to the main thread in ~40 ms chunks together
// with an RMS level for the UI. Standard worklet syntax only (passes `node --check`).

const CHUNK_SAMPLES = 640; // 40 ms at 16 kHz

class MicCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || 16000;
    this.carry = new Float32Array(0); // unconsumed tail of the previous block
    this.pos = 0; // fractional read position into carry+input
    this.out = new Int16Array(CHUNK_SAMPLES);
    this.outLen = 0;
    this.sumSquares = 0;
    this.port.onmessage = (e) => {
      if (e.data && e.data.targetRate) this.targetRate = e.data.targetRate;
    };
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input || input.length === 0) return true;

    const src = new Float32Array(this.carry.length + input.length);
    src.set(this.carry, 0);
    src.set(input, this.carry.length);

    // Linear-interpolation resample; works for any context rate (48k, 44.1k, ...).
    const step = sampleRate / this.targetRate;
    let pos = this.pos;
    while (pos + 1 < src.length) {
      const i = Math.floor(pos);
      const sample = src[i] + (src[i + 1] - src[i]) * (pos - i);
      const clamped = Math.max(-1, Math.min(1, sample));
      this.out[this.outLen++] = clamped * 0x7fff;
      this.sumSquares += clamped * clamped;
      if (this.outLen === CHUNK_SAMPLES) this.emit();
      pos += step;
    }

    const consumed = Math.min(Math.floor(pos), src.length);
    this.carry = src.slice(consumed);
    this.pos = pos - consumed;
    return true;
  }

  emit() {
    const level = Math.sqrt(this.sumSquares / CHUNK_SAMPLES);
    this.port.postMessage({ pcm: this.out.buffer, level }, [this.out.buffer]);
    this.out = new Int16Array(CHUNK_SAMPLES);
    this.outLen = 0;
    this.sumSquares = 0;
  }
}

registerProcessor("mic-capture", MicCaptureProcessor);
