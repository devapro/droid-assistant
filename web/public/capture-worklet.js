/**
 * Capture AudioWorklet (FR-CAP-3, FR-CAP-4).
 *
 * Runs on the audio thread, so React rendering a two-thousand-line transcript
 * cannot glitch the recording. `ScriptProcessorNode` would run this on the main
 * thread, which is exactly why it is deprecated and why the SRS forbids it.
 *
 * Responsibilities, in order:
 *   1. downmix to mono
 *   2. resample to 16 kHz
 *   3. accumulate into fixed-size chunks
 *   4. post each chunk to the main thread with a peak level for the meter
 *
 * Served as a static file rather than bundled: `addModule` needs a real URL,
 * and a hashed chunk name would be one more thing to resolve at runtime.
 */

const TARGET_RATE = 16000

class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const opts = options.processorOptions || {}
    this.chunkSamples = Math.round((TARGET_RATE * (opts.chunkMs || 200)) / 1000)
    this.ratio = sampleRate / TARGET_RATE
    this.buffer = new Float32Array(this.chunkSamples)
    this.filled = 0
    // Fractional read position into the incoming block, carried across blocks so
    // resampling does not drift by up to a sample every 128 frames.
    this.position = 0
    this.tail = new Float32Array(0)
    this.peak = 0
    this.running = true

    this.port.onmessage = (event) => {
      if (event.data?.type === 'stop') this.running = false
    }
  }

  /** Linear interpolation. Adequate at 48k→16k, and it costs almost nothing. */
  resample(input) {
    const source = this.tail.length ? concat(this.tail, input) : input
    const out = []
    let pos = this.position
    while (pos < source.length - 1) {
      const i = Math.floor(pos)
      const frac = pos - i
      out.push(source[i] * (1 - frac) + source[i + 1] * frac)
      pos += this.ratio
    }
    // Keep the samples the next block will interpolate against.
    const consumed = Math.floor(pos)
    this.tail = source.slice(Math.max(0, consumed))
    this.position = pos - consumed
    return out
  }

  process(inputs) {
    if (!this.running) return false
    const input = inputs[0]
    if (!input || input.length === 0) return true

    // Downmix: averaging channels is right for a stereo mic capturing one room.
    let mono
    if (input.length === 1) {
      mono = input[0]
    } else {
      mono = new Float32Array(input[0].length)
      for (let c = 0; c < input.length; c++) {
        const channel = input[c]
        for (let i = 0; i < mono.length; i++) mono[i] += channel[i] / input.length
      }
    }

    for (let i = 0; i < mono.length; i++) {
      const magnitude = Math.abs(mono[i])
      if (magnitude > this.peak) this.peak = magnitude
    }

    for (const sample of this.resample(mono)) {
      this.buffer[this.filled++] = sample
      if (this.filled === this.chunkSamples) {
        this.emit()
      }
    }
    return true
  }

  emit() {
    // int16 on the audio thread: half the bytes to transfer, and the wire format
    // the server expects anyway.
    const pcm = new Int16Array(this.chunkSamples)
    for (let i = 0; i < this.chunkSamples; i++) {
      const clamped = Math.max(-1, Math.min(1, this.buffer[i]))
      pcm[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff
    }
    this.port.postMessage({ type: 'chunk', pcm: pcm.buffer, peak: this.peak }, [pcm.buffer])
    this.filled = 0
    this.peak = 0
  }
}

function concat(a, b) {
  const out = new Float32Array(a.length + b.length)
  out.set(a, 0)
  out.set(b, a.length)
  return out
}

registerProcessor('capture-processor', CaptureProcessor)
