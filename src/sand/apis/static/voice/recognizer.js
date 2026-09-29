// The speech recognizer of voice mode: Vosk, running in this browser.
// What it hears does not leave the computer.
//
// Its files (vosk-browser and the model) are not part of sand: they are
// put into static/voice/vosk/ by hand, see the README.

import { GRAMMAR } from "./commands.js";
import { audioContext } from "./feedback.js";

const FILES = "static/voice/vosk/";
const NOT_INSTALLED = "The speech recognizer is not installed on this server.";

let client = null;
let recognizer = null;
let stream = null;
let source = null;
let tap = null;

function fileUrl(name) {
  return new URL(FILES + name, document.baseURI).href;
}

// Rejects with a message for the user.
export async function loadRecognizer() {
  let library;
  try {
    // not a static import: without the files the whole page would not load
    library = await import(fileUrl("vosk.wasm.js"));
    const model = await fetch(fileUrl("model.tar.gz"), { method: "HEAD" });
    if (!model.ok) throw new Error(NOT_INSTALLED);
  } catch {
    throw new Error(NOT_INSTALLED);
  }
  const loading = new library.VoskClient({
    modelUrl: fileUrl("model.tar.gz"),
    workerUrl: fileUrl("vosk.worker.js"),
    wasmUrl: fileUrl("vosk.wasm"),
  });
  // the library's own createVoskClient waits forever when loading fails
  await new Promise((resolve, reject) => {
    const failed = new Error("The voice model could not be loaded.");
    loading.on("load", (message) => (message.result ? resolve() : reject(failed)));
    loading.on("error", () => reject(failed));
  });
  client = loading;
}

// onText gets what was said between two pauses, onHearing the words
// while they are spoken. Rejects when the microphone can not be opened.
export async function startListening(onText, onHearing) {
  const ctx = audioContext();
  stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  await ctx.audioWorklet.addModule("static/voice/tap-worklet.js");
  source = ctx.createMediaStreamSource(stream);
  tap = new AudioWorkletNode(ctx, "tap");
  source.connect(tap);
  // the tap gives no sound, but only a connected node is run
  tap.connect(ctx.destination);

  const listening = new client.KaldiRecognizer(ctx.sampleRate, JSON.stringify(GRAMMAR));
  listening.on("result", (message) => {
    // silence gives an empty text
    if (message.result.text) onText(message.result.text);
  });
  listening.on("partialresult", (message) => onHearing(message.result.partial));
  tap.port.onmessage = (event) => listening.acceptWaveformFloat(event.data, ctx.sampleRate);
  recognizer = listening;
}

// Frees the microphone and the model's memory.
export function stopListening() {
  if (tap) {
    tap.port.onmessage = null;
    tap.disconnect();
  }
  if (source) source.disconnect();
  if (stream) stream.getTracks().forEach((track) => track.stop());
  if (recognizer) recognizer.remove();
  if (client) client.terminate();
  tap = null;
  source = null;
  stream = null;
  recognizer = null;
  client = null;
}

export function recognizerLoaded() {
  return client !== null;
}
