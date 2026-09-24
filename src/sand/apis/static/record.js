// The Record card: recording (with live transcript), discard, and
// uploading an audio file.

import { authFetch, experimentUrl } from "./api.js";
import { lockExperimentSelect, requireExperiment } from "./experiments.js";
import { reportNewInput } from "./inputs.js";
import {
  clearLivePanel,
  sendLiveChunk,
  startLiveTranscript,
  stopLiveTranscript,
  storeLiveChosen,
} from "./live-transcript.js";
import { clearEntryLink, clearError, showError } from "./ui.js";

const recordBtn = document.getElementById("record-btn");
const discardBtn = document.getElementById("discard-btn");
const uploadBtn = document.getElementById("upload-btn");
const uploadInput = document.getElementById("upload-input");
const statusEl = document.getElementById("status");
const labelInput = document.getElementById("record-label");
const audioEntryEl = document.getElementById("audio-entry");

// Keep in sync with MAX_UPLOAD_BYTES in apis/routers/input_collections.py.
const MAX_UPLOAD_SIZE = 25 * 1024 * 1024;

let mediaRecorder = null;
let chunks = [];
let timerInterval = null;
let startTime = 0;
// The experiment chosen when recording started: the upload must go
// there even if the dropdown changes while recording.
let recordingExperiment = null;

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

async function startRecording() {
  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    showError("Microphone access denied. Check your browser permissions.");
    return;
  }

  recordingExperiment = experiment;
  lockExperimentSelect(true);
  chunks = [];
  const recorder = new MediaRecorder(stream);
  mediaRecorder = recorder;

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
    const liveTranscript = await stopLiveTranscript(myConn);
    const blob = new Blob(recorded, { type: mimeType });
    if (blob.size === 0) {
      showError("No audio recorded.");
      statusEl.textContent = "";
      uploadBtn.disabled = false;
      return;
    }
    await uploadAudio(blob, experiment, storeLive ? liveTranscript : "", label);
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
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
  } else {
    stopLiveTranscript();
  }
  discardBtn.hidden = true;
  stopTimer();
  recordBtn.innerHTML = '<span class="material-icons">mic</span> Record';
  recordBtn.classList.remove("btn-recording");
  recordBtn.classList.add("btn-primary");
}

async function uploadAudio(blobOrFile, experiment, transcript, label) {
  if (!experiment) return;
  recordBtn.disabled = true;
  uploadBtn.disabled = true;
  statusEl.textContent = "Uploading audio to NOMAD...";
  clearError();
  clearEntryLink(audioEntryEl);

  const form = new FormData();
  if (blobOrFile instanceof File) {
    form.append("file", blobOrFile);
  } else {
    const mimeSubtype = blobOrFile.type ? blobOrFile.type.split(";")[0].split("/")[1] : null;
    const ext = mimeSubtype || "wav";
    form.append("file", blobOrFile, "recording." + ext);
  }
  // the live transcription result: the entry is created pre-transcribed
  // and the automatic whisper run is skipped
  if (transcript) form.append("transcript", transcript);
  if (label) form.append("label", label);

  try {
    const saved = await reportNewInput(
      audioEntryEl,
      authFetch(experimentUrl(experiment, "audio"), { method: "POST", body: form }),
      "Audio upload failed",
      "Audio added to " + experiment.name + ".",
      "View audio entry on NOMAD"
    );
    // a label typed meanwhile for the next recording is kept
    if (saved && labelInput.value.trim() === label) labelInput.value = "";
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    recordBtn.disabled = false;
    uploadBtn.disabled = false;
    statusEl.textContent = "";
  }
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
  await uploadAudio(file, experiment, "", labelInput.value.trim());
}

export function initRecord() {
  recordBtn.addEventListener("click", () => {
    if (mediaRecorder && mediaRecorder.state === "recording") {
      stopRecording();
    } else {
      startRecording();
    }
  });

  discardBtn.addEventListener("click", () => {
    if (!mediaRecorder || mediaRecorder.state !== "recording") return;
    if (!window.confirm("Discard this recording? Nothing will be saved.")) return;
    // intent rides on THIS recorder object: a quick discard-then-redo
    // creates a new recorder and cannot re-route or reset it
    mediaRecorder.discardRequested = true;
    stopRecording();
  });

  uploadBtn.addEventListener("click", () => uploadInput.click());
  uploadInput.addEventListener("change", uploadAudioFile);

  window.addEventListener("beforeunload", () => {
    if (mediaRecorder && mediaRecorder.state === "recording") {
      mediaRecorder.stream.getTracks().forEach((t) => t.stop());
    }
  });
}
