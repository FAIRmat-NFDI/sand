// Voice mode: start and stop a recording without hands, and hear what
// happened. Turning it on checks everything first, while the user's
// hands are still free.
//
// Whether a recording runs is never kept here: record.js knows, so a
// click on the Record button and a command can not disagree.

import { sessionState } from "../api.js";
import { selectedExperiment } from "../experiments.js";
import { isRecording, startRecording, stopRecording } from "../record.js";
import { onLiveText } from "../live-transcript.js";
import { commandIn, stopSaidIn } from "./commands.js";
import { beep, closeFeedback, openFeedback, speaking } from "./feedback.js";
import { loadRecognizer, recognizerLoaded, startListening, stopListening } from "./recognizer.js";

const voiceBtn = document.getElementById("voice-btn");
const statusEl = document.getElementById("voice-status");
const checksEl = document.getElementById("voice-checks");
const heardEl = document.getElementById("voice-heard");

// one command, not two, when it is heard twice
const COOLDOWN_MS = 1500;
const VOICE_TEST_MS = 10000;
const RENDER_MS = 500;

let state = "off"; // "off", "checking" or "on"
let ignoreUntil = 0;
let wakeLock = null;
let renderInterval = null;
// counts the times voice mode was turned on or off: checks that are
// still running then belong to an old turn
let turn = 0;
// get what the recognizer heard; they change with what voice mode is doing
let heard = () => {};
let hearing = () => {};

function render() {
  voiceBtn.disabled = state === "checking";
  voiceBtn.lastChild.textContent = state === "on" ? " Voice mode off" : " Voice mode on";
  let text = "";
  let look = "voice-status";
  if (state === "checking") {
    text = "Checking...";
  } else if (state === "on" && isRecording()) {
    text = "Recording";
    look += " voice-recording";
  } else if (state === "on") {
    text = "Listening";
    look += " voice-listening";
  }
  // only on a change: a screen reader reads a status out on every write
  if (statusEl.textContent !== text) statusEl.textContent = text;
  if (statusEl.className !== look) statusEl.className = look;
}

// What the recognizer makes of the voice, so the user sees why a command
// was not taken. It knows the words of the commands only: the rest is "...".
function showHeard(words) {
  heardEl.textContent = "Heard: " + words.replaceAll("[unk]", "...");
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

async function checkRecognizer() {
  try {
    await loadRecognizer();
    return "";
  } catch (err) {
    return err.message;
  }
}

// The user says "hey sand": it shows that the microphone works and that
// the recognizer understands this voice in this room.
async function checkVoice(hint) {
  if (!recognizerLoaded()) return "Needs the speech recognizer.";
  try {
    await startListening((text) => heard(text), (words) => hearing(words));
  } catch (err) {
    stopListening();
    if (err.name === "NotAllowedError") return "Allow the microphone for this page.";
    return "The microphone could not be opened.";
  }
  const understood = await new Promise((resolve) => {
    const timeout = setTimeout(() => resolve(false), VOICE_TEST_MS);
    hearing = (words) => hint(words ? 'hearing "' + words + '"' : "");
    heard = (text) => {
      if (commandIn(text) === null) return;
      clearTimeout(timeout);
      resolve(true);
    };
  });
  heard = () => {};
  hearing = () => {};
  return understood ? "" : "SAND did not understand you. Move closer to the microphone and try again.";
}

async function keepScreenOn() {
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    return "";
  } catch {
    return "The screen may turn off during the experiment: this browser can not keep it on.";
  }
}

function letScreenTurnOff() {
  if (wakeLock) wakeLock.release().catch(() => {});
  wakeLock = null;
}

// Returns hint(text), to show something while the check runs, and
// done(problem).
function showCheck(label) {
  const item = document.createElement("li");
  item.textContent = label;
  checksEl.append(item);
  const hint = (text) => {
    item.textContent = text ? label + ": " + text : label;
  };
  const done = (problem, warning = false) => {
    item.className = !problem ? "voice-check-ok" : warning ? "voice-check-warning" : "voice-check-failed";
    hint(problem);
  };
  return { hint, done };
}

// All checks run, so the user sees every problem at once.
async function runChecks() {
  checksEl.replaceChildren();
  checksEl.hidden = false;
  let failed = false;
  const checks = [
    ["Experiment", checkExperiment],
    ["NOMAD login", checkLogin],
    ["Speech recognizer", checkRecognizer],
    ['Microphone and voice (say "hey sand")', checkVoice],
  ];
  for (const [label, check] of checks) {
    const { hint, done } = showCheck(label);
    const problem = await check(hint);
    done(problem);
    if (problem) failed = true;
  }
  showCheck("Screen stays on").done(await keepScreenOn(), true);
  return !failed;
}

// --- on and off ----------------------------------------------------------

async function turnOn() {
  // before any await: audio needs the click
  openFeedback();
  state = "checking";
  turn += 1;
  const mine = turn;
  render();
  const passed = await runChecks();
  if (mine !== turn) {
    // turned off meanwhile: close what the checks opened after that
    if (state === "off") {
      stopListening();
      letScreenTurnOff();
    }
    return;
  }
  if (!passed) {
    stopListening();
    closeFeedback();
    letScreenTurnOff();
    state = "off";
    render();
    return;
  }
  state = "on";
  heard = (text) => {
    // SAND's own beep is no command
    if (speaking()) return;
    showHeard(text);
    const command = commandIn(text);
    if (command === "start" || command === "stop") voiceCommand(command);
  };
  hearing = (words) => {
    if (words && !speaking()) showHeard(words);
  };
  heardEl.textContent = "";
  heardEl.hidden = false;
  renderInterval = setInterval(render, RENDER_MS);
  render();
}

// Turning on and off is shown, not spoken: the user is at the screen to
// click. Also during the checks, when the page is left.
function turnOff() {
  if (state === "off") return;
  state = "off";
  turn += 1;
  // stopped and saved like by the Stop button
  if (isRecording()) stopRecording();
  clearInterval(renderInterval);
  heard = () => {};
  hearing = () => {};
  heardEl.hidden = true;
  stopListening();
  letScreenTurnOff();
  checksEl.hidden = true;
  closeFeedback();
  render();
}

// --- commands ------------------------------------------------------------
// The beeps: rising = recording runs, falling = stopped, one high = saved,
// two low = it did not work. The screen shows why.

async function start() {
  const outcome = await startRecording();
  render();
  await beep(outcome === "started" ? "start" : "error");
}

async function stop() {
  const saving = stopRecording();
  render();
  await beep("stop");
  const outcome = await saving;
  if (outcome !== "discarded") await beep(outcome === "saved" ? "ok" : "error");
}

// intent: "start" or "stop"
async function voiceCommand(intent) {
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

  // the browser drops the wake lock when the tab is hidden
  document.addEventListener("visibilitychange", () => {
    if (state === "on" && document.visibilityState === "visible") keepScreenOn();
  });

  // Voice mode never survives leaving the page. Turned off fully: the
  // browser may bring the page back as it was, not reloaded.
  // During a recording the live transcript hears the stop command too,
  // and better than the small recognizer in the browser: after a long
  // dictation or with noise that one often misses it.
  onLiveText((text) => {
    if (stopSaidIn(text)) voiceCommand("stop");
  });

  window.addEventListener("pagehide", turnOff);
  render();
}
