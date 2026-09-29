// Voice mode: start and stop a recording without hands, and hear what
// happened. Turning it on checks everything first, while the user's
// hands are still free.
//
// Whether a recording runs is never kept here: record.js knows, so a
// click on the Record button and a command can not disagree.

import { sessionState } from "../api.js";
import { selectedExperiment } from "../experiments.js";
import { isRecording, startRecording, stopRecording } from "../record.js";
import { beep, closeFeedback, loadFeedback, say } from "./feedback.js";

const voiceBtn = document.getElementById("voice-btn");
const statusEl = document.getElementById("voice-status");
const checksEl = document.getElementById("voice-checks");

// Stands in for the spoken commands until the recognizer is connected.
const COMMAND_KEY = "F9";
// one command, not two, when it is heard or pressed twice
const COOLDOWN_MS = 1500;
const MIC_LISTEN_MS = 6000;
const MIC_LOUD = 0.05;
const RENDER_MS = 500;

let state = "off"; // "off", "checking" or "on"
let ignoreUntil = 0;
let wakeLock = null;
let renderInterval = null;

function render() {
  voiceBtn.disabled = state === "checking";
  voiceBtn.lastChild.textContent = state === "off" ? " Voice mode on" : " Voice mode off";
  statusEl.className = "voice-status";
  if (state === "off") {
    statusEl.textContent = "";
  } else if (state === "checking") {
    statusEl.textContent = "Checking...";
  } else if (isRecording()) {
    statusEl.textContent = "Recording";
    statusEl.classList.add("voice-recording");
  } else {
    statusEl.textContent = "Listening (" + COMMAND_KEY + " starts and stops)";
    statusEl.classList.add("voice-listening");
  }
}

// --- the checks before voice mode turns on ------------------------------
// Each resolves to "" or to what the user has to do.

async function checkExperiment() {
  return selectedExperiment() ? "" : "Select an experiment.";
}

async function checkLogin() {
  const problem = "Log in to NOMAD, and keep NOMAD open in this browser during the experiment.";
  if (sessionState() !== "ok") return problem;
  try {
    const res = await fetch("api/me");
    if (res.status === 401) return problem;
    return res.ok ? "" : "SAND's server does not answer.";
  } catch {
    return "SAND's server can not be reached.";
  }
}

async function checkMicrophone() {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    return "Allow the microphone for this page.";
  }
  const ctx = new AudioContext();
  const analyser = ctx.createAnalyser();
  ctx.createMediaStreamSource(stream).connect(analyser);
  const samples = new Float32Array(analyser.fftSize);
  const end = performance.now() + MIC_LISTEN_MS;
  let heard = false;
  while (!heard && performance.now() < end) {
    await new Promise((resolve) => {
      setTimeout(resolve, 100);
    });
    analyser.getFloatTimeDomainData(samples);
    heard = samples.some((sample) => Math.abs(sample) > MIC_LOUD);
  }
  stream.getTracks().forEach((track) => track.stop());
  ctx.close();
  return heard ? "" : "The microphone hears nothing. Check that it is the right one and not muted.";
}

async function keepScreenOn() {
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    return "";
  } catch {
    return "The screen may turn off during the experiment: this browser can not keep it on.";
  }
}

function showCheck(label) {
  const item = document.createElement("li");
  item.textContent = label;
  checksEl.append(item);
  return (problem, warning = false) => {
    item.className = !problem ? "voice-check-ok" : warning ? "voice-check-warning" : "voice-check-failed";
    if (problem) item.textContent = label + ": " + problem;
  };
}

// All checks run, so the user sees every problem at once.
async function runChecks(sounds) {
  checksEl.replaceChildren();
  checksEl.hidden = false;
  let failed = false;
  const checks = [
    ["Experiment", checkExperiment],
    ["NOMAD login", checkLogin],
    ["Microphone (say a few words)", checkMicrophone],
    ["Sounds", () => sounds.then(() => "", (err) => err.message)],
  ];
  for (const [label, check] of checks) {
    const done = showCheck(label);
    const problem = await check();
    done(problem);
    if (problem) failed = true;
  }
  if (!failed) showCheck("Screen stays on")(await keepScreenOn(), true);
  return !failed;
}

// --- on and off ----------------------------------------------------------

async function turnOn() {
  // before any await: audio needs the click
  const sounds = loadFeedback();
  state = "checking";
  render();
  if (!await runChecks(sounds)) {
    closeFeedback();
    state = "off";
    render();
    return;
  }
  state = "on";
  renderInterval = setInterval(render, RENDER_MS);
  render();
}

// Turning on and off is shown, not spoken: the user is at the screen to
// click.
function turnOff() {
  if (state !== "on") return;
  state = "off";
  // stopped and saved like by the Stop button
  if (isRecording()) stopRecording();
  clearInterval(renderInterval);
  if (wakeLock) wakeLock.release();
  wakeLock = null;
  checksEl.hidden = true;
  closeFeedback();
  render();
}

// --- commands ------------------------------------------------------------

async function start() {
  const outcome = await startRecording();
  render();
  if (outcome === "started") {
    await beep("start");
    return;
  }
  await beep("error");
  await say(outcome === "busy" ? "still_saving" : "cannot_record");
  if (outcome === "no-experiment") await say("no_experiment");
  if (outcome === "login") await say("no_connection");
}

async function stop() {
  const saving = stopRecording();
  render();
  await beep("stop");
  const outcome = await saving;
  if (outcome === "discarded") return;
  const saved = outcome === "saved";
  // a spoken clip would end up in the next recording
  if (isRecording()) {
    await beep(saved ? "ok" : "error");
    return;
  }
  if (saved) {
    await say("saved");
    return;
  }
  await beep("error");
  if (outcome === "unsent") await say("upload_failed");
}

// intent: "start" or "stop"
export async function voiceCommand(intent) {
  if (state !== "on" || performance.now() < ignoreUntil) return;
  if (intent === "start" && !isRecording()) {
    ignoreUntil = performance.now() + COOLDOWN_MS;
    await start();
  } else if (intent === "stop" && isRecording()) {
    ignoreUntil = performance.now() + COOLDOWN_MS;
    await stop();
  }
}

export function initVoiceMode() {
  voiceBtn.addEventListener("click", () => {
    if (state === "off") turnOn();
    else turnOff();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== COMMAND_KEY || event.repeat || state !== "on") return;
    event.preventDefault();
    voiceCommand(isRecording() ? "stop" : "start");
  });

  // the browser drops the wake lock when the tab is hidden
  document.addEventListener("visibilitychange", () => {
    if (state === "on" && document.visibilityState === "visible") keepScreenOn();
  });

  // voice mode never survives a reload: it starts off
  window.addEventListener("pagehide", () => {
    if (state === "on") closeFeedback();
  });
  render();
}
