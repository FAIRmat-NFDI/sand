// Voice mode's sounds: beeps made in code, spoken clips from files. All
// loaded when voice mode turns on, so they play at once and without
// network later.
//
// The clips are made with Piper, voice en_US-ljspeech-medium (trained on
// the public domain LJ Speech data): echo "Saved." | piper -m <voice> -f saved.wav

const CLIPS = [
  "saved", "upload_failed", "cannot_record", "no_experiment",
  "no_connection", "still_saving", "listening", "voice_off", "voice_stopped",
];

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
let clips = {};
let quietAt = 0;

// Call it first thing in a click handler: browsers start audio only on a
// user gesture.
export async function loadFeedback() {
  ctx = new AudioContext();
  const loading = ctx;
  await loading.resume();
  const loaded = await Promise.all(CLIPS.map(async (name) => {
    const res = await fetch("static/voice/clips/" + name + ".wav");
    if (!res.ok) throw new Error("Could not load the sound \"" + name + "\".");
    return [name, await loading.decodeAudioData(await res.arrayBuffer())];
  }));
  if (ctx === loading) clips = Object.fromEntries(loaded);
}

export function closeFeedback() {
  if (ctx) ctx.close();
  ctx = null;
  clips = {};
}

// true while a sound plays: what is recognized then is SAND's own voice
export function speaking() {
  return performance.now() < quietAt;
}

function ended(seconds) {
  const ms = seconds * 1000;
  quietAt = Math.max(quietAt, performance.now() + ms + ECHO_MS);
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

// Both resolve when the sound has ended, at once when voice mode is off.
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
  return ended(tones.length * TONE_S);
}

export function say(name) {
  if (!ctx || !clips[name]) return Promise.resolve();
  const source = ctx.createBufferSource();
  source.buffer = clips[name];
  source.connect(ctx.destination);
  source.start();
  return ended(source.buffer.duration);
}
