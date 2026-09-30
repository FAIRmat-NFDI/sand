// Voice mode's sounds: beeps made in code, so they play at once and
// without network.

const BEEPS = {
  start: [660, 880],
  stop: [880, 660],
  ok: [880],
  error: [220, 220],
};
const TONE_S = 0.12;
// the microphone still hears a sound shortly after it ended
const ECHO_MS = 300;

let ctx = null;
let quietAt = 0;

// Call it first thing in a click handler: browsers start audio only on a
// user gesture.
export function openFeedback() {
  ctx = new AudioContext();
}

// The recognizer listens on the same context.
export function audioContext() {
  return ctx;
}

export function closeFeedback() {
  if (ctx) ctx.close().catch(() => {});
  ctx = null;
}

// true while a sound plays: what is recognized then is SAND's own sound
export function speaking() {
  return performance.now() < quietAt;
}

// Resolves when the sound has ended, at once when voice mode is off.
export function beep(kind) {
  if (!ctx) return Promise.resolve();
  const tones = BEEPS[kind];
  tones.forEach((frequency, i) => {
    const start = ctx.currentTime + i * TONE_S;
    const tone = ctx.createOscillator();
    const volume = ctx.createGain();
    tone.frequency.value = frequency;
    // fade in and out: a hard edge clicks
    volume.gain.setValueAtTime(0, start);
    volume.gain.linearRampToValueAtTime(0.3, start + 0.01);
    volume.gain.linearRampToValueAtTime(0, start + TONE_S);
    tone.connect(volume).connect(ctx.destination);
    tone.start(start);
    tone.stop(start + TONE_S);
  });
  const ms = tones.length * TONE_S * 1000;
  quietAt = Math.max(quietAt, performance.now() + ms + ECHO_MS);
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}
