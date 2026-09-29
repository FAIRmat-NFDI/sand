// The Record card: recording (with live transcript), discard, uploading
// an audio file, and keeping a recording whose upload failed.

import { experimentUrl, sessionState } from "./api.js";
import { lockExperimentSelect, requireExperiment } from "./experiments.js";
import { reportNewInput } from "./inputs.js";
import {
  clearLivePanel,
  sendLiveChunk,
  startLiveTranscript,
  stopLiveTranscript,
  storeLiveChosen,
} from "./live-transcript.js";
import { clearEntryLink, clearError, confirmDialog, showError } from "./ui.js";
import { withoutStop } from "./voice/commands.js";

const recordBtn = document.getElementById("record-btn");
const discardBtn = document.getElementById("discard-btn");
const uploadBtn = document.getElementById("upload-btn");
const uploadInput = document.getElementById("upload-input");
const statusEl = document.getElementById("status");
const labelInput = document.getElementById("record-label");
const audioEntryEl = document.getElementById("audio-entry");
const unsentEl = document.getElementById("unsent-recordings");

// Keep in sync with MAX_UPLOAD_BYTES in apis/routers/input_collections.py.
const MAX_UPLOAD_SIZE = 25 * 1024 * 1024;

let mediaRecorder = null;
let chunks = [];
let timerInterval = null;
let startTime = 0;
// The experiment chosen when recording started: the upload must go
// there even if the dropdown changes while recording.
let recordingExperiment = null;
// Recordings whose upload failed. They exist only in this tab's memory:
// kept until uploaded or discarded, lost when the tab closes.
let unsent = [];
const UNSENT_CHECK_MS = 5000;
// Uploads run one after another: each owns the buttons and the status line.
let uploadQueue = Promise.resolve();
let pendingUploads = 0;
// Stopped recordings that still wait for their transcript before uploading.
let finalizing = 0;

function formatTime(ms) {
  const seconds = Math.floor(ms / 1000);
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m + ":" + String(s).padStart(2, "0");
}

function startTimer() {
  startTime = Date.now();
  timerInterval = setInterval(() => {
    statusEl.textContent = "Recording " + formatTime(Date.now() - startTime);
  }, 200);
}

function stopTimer() {
  clearInterval(timerInterval);
  timerInterval = null;
}

// Resolves to "started", or to why not: "busy", "no-experiment", "login"
// or "mic". Shows the error itself, except for "busy".
export async function startRecording() {
  // the record button is disabled then; a voice command is not
  if (isRecording() || pendingUploads > 0) return "busy";
  clearError();
  const experiment = requireExperiment();
  if (!experiment) return "no-experiment";
  // a recording lives only in memory until uploaded: do not start one
  // that cannot be saved
  if (sessionState() !== "ok") {
    showError("Log in to NOMAD again before you record: the recording could not be saved.");
    return "login";
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    showError("Microphone access denied. Check your browser permissions.");
    return "mic";
  }
  // another start may have passed the check above while this one waited
  // for the microphone
  if (isRecording() || pendingUploads > 0) {
    stream.getTracks().forEach((t) => t.stop());
    return "busy";
  }

  recordingExperiment = experiment;
  lockExperimentSelect(true);
  chunks = [];
  const recorder = new MediaRecorder(stream);
  mediaRecorder = recorder;
  // what became of this recording, known only after the upload
  recorder.outcome = new Promise((resolve) => {
    recorder.resolveOutcome = resolve;
  });

  recorder.ondataavailable = (e) => {
    if (e.data.size > 0) {
      chunks.push(e.data);
      sendLiveChunk(e.data);
    }
  };

  recorder.onstop = async () => {
    if (recorder.discardRequested) {
      // close this recording's relay; detach so its late drain finals
      // cannot repaint the cleared panel
      stopLiveTranscript(myConn, true);
      stream.getTracks().forEach((t) => t.stop());
      if (mediaRecorder === recorder) {
        // only reset shared state if no newer recording took over
        lockExperimentSelect(false);
        recordingExperiment = null;
        chunks = [];
        clearLivePanel();
        statusEl.textContent = "Recording discarded.";
        uploadBtn.disabled = false;
      }
      recorder.resolveOutcome("discarded");
      return;
    }
    // snapshot this recording's state BEFORE any await: the record
    // button is live again during the relay wait, and a new recording
    // rebinds the globals (chunks, mediaRecorder, recordingExperiment)
    const experiment = recordingExperiment;
    recordingExperiment = null;
    const recorded = chunks;
    const mimeType = recorder.mimeType;
    stream.getTracks().forEach((t) => t.stop());
    lockExperimentSelect(false);
    // fires after the final ondataavailable, so the last chunk has been
    // streamed before we tell the relay to flush; the promise resolves
    // with the full final transcript once the relay closed
    // the toggle state at stop time decides whether the live text is
    // stored; either way the panel keeps showing it
    const storeLive = storeLiveChosen();
    const label = labelInput.value.trim();
    statusEl.textContent = "Finishing transcript...";
    finalizing += 1;
    renderUnsent();
    let liveTranscript;
    try {
      liveTranscript = await stopLiveTranscript(myConn);
    } finally {
      finalizing -= 1;
      renderUnsent();
    }
    const blob = new Blob(recorded, { type: mimeType });
    if (blob.size === 0) {
      showError("No audio recorded.");
      statusEl.textContent = "";
      uploadBtn.disabled = false;
      recorder.resolveOutcome("empty");
      return;
    }
    const item = {
      blob,
      experiment,
      // "hey sand stop" is a command, not a part of the note
      transcript: storeLive ? withoutStop(liveTranscript) : "",
      label,
      time: new Date(),
    };
    let outcome = "unsent";
    try {
      outcome = await uploadRecording(item);
    } catch (err) {
      // not expected: the recording is kept, and whoever waits for the
      // outcome gets one
      console.error(err);
      if (!unsent.includes(item)) unsent.push(item);
      item.problem = "The upload failed.";
      renderUnsent();
    } finally {
      recorder.resolveOutcome(outcome);
    }
  };

  const myConn = startLiveTranscript();
  // timeslice: periodic chunks feed the live stream; the local blob is
  // assembled from the same chunks, so the stored audio is unchanged
  recorder.start(250);
  discardBtn.hidden = false;
  recordBtn.innerHTML = '<span class="material-icons">stop</span> Stop';
  recordBtn.classList.remove("btn-primary");
  recordBtn.classList.add("btn-recording");
  uploadBtn.disabled = true;
  startTimer();
  return "started";
}

// Returns a promise of what became of the recording: "saved", "unsent"
// (kept for a retry), "discarded" or "empty". null when none was running.
export function stopRecording() {
  let outcome = null;
  if (mediaRecorder && mediaRecorder.state === "recording") {
    outcome = mediaRecorder.outcome;
    mediaRecorder.stop();
  } else {
    stopLiveTranscript();
  }
  discardBtn.hidden = true;
  stopTimer();
  recordBtn.innerHTML = '<span class="material-icons">mic</span> Record';
  recordBtn.classList.remove("btn-recording");
  recordBtn.classList.add("btn-primary");
  return outcome;
}

function audioExtension(blob) {
  const mimeSubtype = blob.type ? blob.type.split(";")[0].split("/")[1] : null;
  return mimeSubtype || "wav";
}

// Resolves to "saved", "login" (NOMAD did not accept the login) or
// "failed". Shows the error itself, except for "login".
async function uploadAudio(blobOrFile, experiment, transcript, label) {
  if (!experiment) return "failed";
  pendingUploads += 1;
  renderUnsent();
  const previous = uploadQueue;
  let finished;
  uploadQueue = new Promise((resolve) => {
    finished = resolve;
  });
  await previous;
  try {
    return await sendAudio(blobOrFile, experiment, transcript, label);
  } finally {
    pendingUploads -= 1;
    if (pendingUploads === 0) {
      recordBtn.disabled = false;
      uploadBtn.disabled = false;
      statusEl.textContent = "";
    }
    renderUnsent();
    finished();
  }
}

async function sendAudio(blobOrFile, experiment, transcript, label) {
  recordBtn.disabled = true;
  uploadBtn.disabled = true;
  statusEl.textContent = "Uploading audio to NOMAD...";
  clearError();
  clearEntryLink(audioEntryEl);

  const form = new FormData();
  if (blobOrFile instanceof File) {
    form.append("file", blobOrFile);
  } else {
    form.append("file", blobOrFile, "recording." + audioExtension(blobOrFile));
  }
  // the live transcription result: the entry is created pre-transcribed
  // and the automatic whisper run is skipped
  if (transcript) form.append("transcript", transcript);
  if (label) form.append("label", label);

  try {
    const res = await fetch(experimentUrl(experiment, "audio"), { method: "POST", body: form });
    if (res.status === 401) return "login";
    const saved = await reportNewInput(
      audioEntryEl,
      res,
      "Audio upload failed",
      "Audio added to " + experiment.name + ".",
      "View audio entry on NOMAD"
    );
    // a label typed meanwhile for the next recording is kept
    if (saved && labelInput.value.trim() === label) labelInput.value = "";
    return saved ? "saved" : "failed";
  } catch (err) {
    showError("Network error: " + err.message);
    return "failed";
  }
}

// --- recordings whose upload failed ------------------------------------

// Resolves to "saved" or "unsent".
async function uploadRecording(item) {
  const result = await uploadAudio(item.blob, item.experiment, item.transcript, item.label);
  if (result === "saved") {
    unsent = unsent.filter((other) => other !== item);
  } else {
    if (!unsent.includes(item)) {
      unsent.push(item);
      // the label went with this recording, not with the next one
      if (labelInput.value.trim() === item.label) labelInput.value = "";
    }
    // Retried by itself once the login is back. Not when the token looks
    // valid and NOMAD still refuses it: that would retry without end.
    item.waitsForLogin = result === "login" && sessionState() !== "ok";
    if (item.waitsForLogin) {
      item.problem = "Waiting for your NOMAD login: it uploads by itself once you are logged in.";
    } else if (result === "login") {
      item.problem = "NOMAD did not accept your login. Log in to NOMAD again, then retry.";
    } else {
      item.problem = "The upload failed.";
    }
  }
  renderUnsent();
  return result === "saved" ? "saved" : "unsent";
}

export function isRecording() {
  return Boolean(mediaRecorder) && mediaRecorder.state === "recording";
}

// A retry waits for a recording to finish: an upload takes over the record
// button and the status line.
function busy() {
  return isRecording() || finalizing > 0 || pendingUploads > 0;
}

function retryUnsent(item) {
  clearError();
  if (isRecording()) {
    showError("Stop the recording before you retry.");
    return;
  }
  if (busy()) return;
  uploadRecording(item);
}

function saveUnsentAsFile(item) {
  const stamp = item.time.toISOString().slice(0, 16).replace(/[T:]/g, "-");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(item.blob);
  link.download = "sand_recording_" + stamp + "." + audioExtension(item.blob);
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

async function discardUnsent(item) {
  // no automatic retry behind the open dialog: it would upload a
  // recording the user is about to discard
  const waited = item.waitsForLogin;
  item.waitsForLogin = false;
  if (!await confirmDialog("Discard this recording? It was not uploaded.", "Discard")) {
    item.waitsForLogin = waited;
    return;
  }
  unsent = unsent.filter((other) => other !== item);
  renderUnsent();
}

function unsentButton(text, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-small " + className;
  button.textContent = text;
  button.disabled = finalizing > 0 || pendingUploads > 0;
  button.addEventListener("click", onClick);
  return button;
}

function renderUnsent() {
  unsentEl.replaceChildren(...unsent.map((item) => {
    const time = item.time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const text = document.createElement("p");
    text.textContent = "Not uploaded: recording of " + time
      + (item.label ? ' ("' + item.label + '")' : "")
      + " for " + item.experiment.name + ". " + item.problem;

    const actions = document.createElement("div");
    actions.className = "controls";
    actions.append(
      unsentButton("Retry", "btn-primary", () => retryUnsent(item)),
      unsentButton("Save as file", "btn-outlined", () => saveUnsentAsFile(item)),
      unsentButton("Discard", "btn-outlined", () => discardUnsent(item)),
    );

    const row = document.createElement("div");
    row.className = "unsent-recording";
    row.append(text, actions);
    return row;
  }));
}

async function uploadAudioFile() {
  const file = uploadInput.files[0];
  uploadInput.value = "";
  if (!file) return;

  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  if (file.size > MAX_UPLOAD_SIZE) {
    showError("File too large (max 25 MB).");
    return;
  }
  // not kept for a retry: the file is still on the user's disk
  const result = await uploadAudio(file, experiment, "", labelInput.value.trim());
  if (result === "login") showError("Log in to NOMAD again, then upload the file again.");
}

export function initRecord() {
  recordBtn.addEventListener("click", () => {
    if (mediaRecorder && mediaRecorder.state === "recording") {
      stopRecording();
    } else {
      startRecording();
    }
  });

  discardBtn.addEventListener("click", async () => {
    const recorder = mediaRecorder;
    if (!recorder || recorder.state !== "recording") return;
    // the recording keeps running while the dialog is open
    if (!await confirmDialog("Discard this recording? Nothing will be saved.", "Discard")) return;
    if (mediaRecorder !== recorder || recorder.state !== "recording") return;
    // intent rides on THIS recorder object: a quick discard-then-redo
    // creates a new recorder and cannot re-route or reset it
    recorder.discardRequested = true;
    stopRecording();
  });

  uploadBtn.addEventListener("click", () => uploadInput.click());
  uploadInput.addEventListener("change", uploadAudioFile);

  setInterval(() => {
    if (busy() || sessionState() !== "ok") return;
    const item = unsent.find((other) => other.waitsForLogin);
    if (item) uploadRecording(item);
  }, UNSENT_CHECK_MS);

  window.addEventListener("beforeunload", (event) => {
    // the browser asks before leaving: the recordings would be lost
    if (unsent.length > 0) event.preventDefault();
  });

  // not in beforeunload: the user may choose to stay, and the recording
  // has to go on then
  window.addEventListener("pagehide", () => {
    if (isRecording()) mediaRecorder.stream.getTracks().forEach((t) => t.stop());
  });
}
