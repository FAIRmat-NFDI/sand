// Runs on the browser's audio thread: collects the microphone's samples
// (128 at a time) and posts them in blocks the recognizer can take.

const BLOCK = 4096;

class Tap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = new Float32Array(BLOCK);
    this.filled = 0;
  }

  process(inputs) {
    const samples = inputs[0][0];
    // no audio yet
    if (!samples) return true;
    for (const sample of samples) {
      this.block[this.filled] = sample;
      this.filled += 1;
      if (this.filled === BLOCK) {
        this.port.postMessage(this.block);
        this.block = new Float32Array(BLOCK);
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor("tap", Tap);
