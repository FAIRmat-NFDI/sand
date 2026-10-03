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
// ends the wait for "hey sand" when voice mode is turned off
let endVoiceTest = () => {};
// the checks of the latest turn; there is one recognizer and one
// microphone per page, so a turn's checks start after the old ones ended
let checking = Promise.resolve(false);

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
async function checkVoice(hint, stale) {
  if (!recognizerLoaded()) return "Needs the speech recognizer.";
  try {
    await startListening((text) => heard(text), (words) => hearing(words));
  } catch (err) {
    stopListening();
    if (err.name === "NotAllowedError") return "Allow the microphone for this page.";
    return "The microphone could not be opened.";
  }
  if (stale()) return "";
  const understood = await new Promise((resolve) => {
    const timeout = setTimeout(() => resolve(false), VOICE_TEST_MS);
    endVoiceTest = () => {
      clearTimeout(timeout);
      resolve(false);
    };
    hearing = (words) => hint(words ? 'hearing "' + words + '"' : "");
    heard = (text) => {
      if (commandIn(text) === null) return;
      clearTimeout(timeout);
      resolve(true);
    };
  });
  endVoiceTest = () => {};
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

// The checks stop at the first problem: the later ones would only make
// the user wait (the model loads, the voice test takes 10 s). They also
// stop when stale(): voice mode was turned off meanwhile.
async function runChecks(stale) {
  checksEl.replaceChildren();
  checksEl.hidden = false;
  const checks = [
    ["Experiment", checkExperiment],
    ["NOMAD login", checkLogin],
    ["Speech recognizer", checkRecognizer],
    ['Microphone and voice (say "hey sand")', checkVoice],
  ];
  for (const [label, check] of checks) {
    if (stale()) return false;
    const { hint, done } = showCheck(label);
    const problem = await check(hint, stale);
    done(problem);
    if (problem) return false;
  }
  if (stale()) return false;
  showCheck("Screen stays on").done(await keepScreenOn(), true);
  return true;
}

// false also when the turn is over: then what the checks opened is closed
async function checkTurn(mine) {
  const passed = await runChecks(() => mine !== turn);
  if (mine === turn) return passed;
  stopListening();
  letScreenTurnOff();
  return false;
}

// --- on and off ----------------------------------------------------------

async function turnOn() {
  // before any await: audio needs the click
  openFeedback();
  state = "checking";
  turn += 1;
  const mine = turn;
  render();
  // an old turn's checks may still run: the page was left during them
  // and brought back
  await checking;
  if (mine !== turn) return;
  checking = checkTurn(mine);
  const passed = await checking;
  if (mine !== turn) return;
  if (!passed) {
    stopListening();
    closeFeedback();
    letScreenTurnOff();
    state = "off";
    const result = document.createElement("li");
    result.className = "voice-checks-result";
    result.textContent = "Voice mode is off. Click Voice mode on to try again.";
    checksEl.append(result);
    render();
    return;
  }
  state = "on";
  heard = (text) => {
    // SAND's own beep is no command
    if (speaking()) return;
    const command = commandIn(text);
    if (command === "start" || command === "stop") voiceCommand(command);
  };
  renderInterval = setInterval(render, RENDER_MS);
  render();
}

// Turning on and off is shown, not spoken: the user is at the screen to
// click. Also during the checks, when the page is left.
function turnOff() {
  if (state === "off") return;
  state = "off";
  turn += 1;
  endVoiceTest();
  // stopped and saved like by the Stop button
  if (isRecording()) stopRecording();
  clearInterval(renderInterval);
  heard = () => {};
  hearing = () => {};
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
  // voice mode may be turned off while the microphone opens
  const mine = turn;
  const outcome = await startRecording(() => mine === turn);
  if (outcome === "cancelled") return;
  render();
  await beep(outcome === "started" ? "start" : "error");
}

async function stop() {
  const saving = stopRecording(true);
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
