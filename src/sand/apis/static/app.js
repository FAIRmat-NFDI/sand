let keycloak = null;

async function initAuth() {
  const res = await fetch("auth/config");
  const cfg = await res.json();

  keycloak = new Keycloak({
    url: cfg.keycloak_url,
    realm: cfg.keycloak_realm,
    clientId: cfg.keycloak_client_id,
  });

  const authenticated = await keycloak.init({
    onLoad: "check-sso",
    checkLoginIframe: false,
  });

  if (authenticated) {
    showApp();
  } else {
    showLoginPrompt();
  }

  setInterval(() => {
    if (keycloak.authenticated) {
      keycloak.updateToken(30).catch(() => {
        showLoginPrompt();
      });
    }
  }, 10000);
}

function showLoginPrompt() {
  document.getElementById("login-prompt").style.display = "block";
  document.getElementById("app-content").style.display = "none";
  document.getElementById("auth-area").innerHTML = "";
}

function showApp() {
  document.getElementById("login-prompt").style.display = "none";
  document.getElementById("app-content").style.display = "block";

  const name = keycloak.tokenParsed.preferred_username || keycloak.tokenParsed.name || "";
  const nameEl = document.createElement("span");
  nameEl.textContent = name;

  const icon = document.createElement("span");
  icon.className = "material-icons";
  icon.textContent = "logout";

  const logoutBtn = document.createElement("button");
  logoutBtn.className = "btn btn-text";
  logoutBtn.id = "logout-btn";
  logoutBtn.appendChild(icon);
  logoutBtn.addEventListener("click", () => {
    keycloak.logout({ redirectUri: window.location.href });
  });

  document.getElementById("auth-area").replaceChildren(nameEl, logoutBtn);

  loadExperiments().catch((err) => {
    showError("Could not load experiments: " + err.message);
  });
}

document.getElementById("login-btn").addEventListener("click", () => {
  keycloak.login({ redirectUri: window.location.href });
});

async function authFetch(url, options = {}) {
  if (keycloak.authenticated) {
    try { await keycloak.updateToken(5); } catch { /* ignore */ }
    options.headers = options.headers || {};
    options.headers["Authorization"] = "Bearer " + keycloak.token;
  }
  return fetch(url, options);
}

async function errorDetail(res) {
  const body = await res.json().catch(() => null);
  if (!body || body.detail == null) return res.statusText;
  return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
}

// --- App logic ---

const textArea = document.getElementById("text");
const recordBtn = document.getElementById("record-btn");
const statusEl = document.getElementById("status");
const error = document.getElementById("error");
const uploadBtn = document.getElementById("upload-btn");
const saveNoteBtn = document.getElementById("save-note-btn");

// --- Experiments ---

const experimentSelect = document.getElementById("experiment-select");
const experimentLink = document.getElementById("experiment-link");
const newExperimentBtn = document.getElementById("new-experiment-btn");
const newExperimentForm = document.getElementById("new-experiment-form");
const SELECTED_EXPERIMENT_KEY = "sand.selectedExperiment";

let experimentsById = {};

function selectedExperiment() {
  return experimentsById[experimentSelect.value] || null;
}

function requireExperiment() {
  const experiment = selectedExperiment();
  if (!experiment) {
    showError("Select an experiment first (or create a new one).");
    return null;
  }
  return experiment;
}

function updateExperimentLink() {
  const experiment = selectedExperiment();
  if (experiment) {
    experimentLink.href = experiment.entry_url;
    experimentLink.hidden = false;
  } else {
    experimentLink.hidden = true;
  }
}

// Options are keyed by entry_id: an upload can hold more than one
// InputCollection entry, so upload_id alone would collide.
function addExperimentOption(experiment) {
  experimentsById[experiment.entry_id] = experiment;
  const option = document.createElement("option");
  option.value = experiment.entry_id;
  option.textContent = experiment.name || experiment.upload_id;
  experimentSelect.appendChild(option);
}

async function loadExperiments(selectEntryId) {
  const res = await authFetch("api/input-collections");
  if (!res.ok) {
    showError("Could not load experiments: " + await errorDetail(res));
    return;
  }
  const data = await res.json();
  experimentsById = {};
  experimentSelect.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "— select an experiment —";
  experimentSelect.appendChild(placeholder);
  for (const experiment of data.input_collections) {
    addExperimentOption(experiment);
  }
  let remembered = null;
  try { remembered = localStorage.getItem(SELECTED_EXPERIMENT_KEY); } catch { /* ignore */ }
  const wanted = selectEntryId || remembered;
  if (wanted && experimentsById[wanted]) {
    experimentSelect.value = wanted;
  }
  updateExperimentLink();
  // restoring the selection fires no 'change' event: resume the polling
  // of an unfinished extraction explicitly (reload-proof progress)
  const restored = selectedExperiment();
  if (restored) {
    startExtractPolling(restored);
    startInputsRefresh(restored);
  }
}

experimentSelect.addEventListener("change", () => {
  try {
    localStorage.setItem(SELECTED_EXPERIMENT_KEY, experimentSelect.value);
  } catch { /* ignore */ }
  updateExperimentLink();
});

newExperimentBtn.addEventListener("click", () => {
  newExperimentForm.hidden = !newExperimentForm.hidden;
});

document.getElementById("cancel-experiment-btn").addEventListener("click", () => {
  newExperimentForm.hidden = true;
});

document.getElementById("create-experiment-btn").addEventListener("click", async () => {
  clearError();
  const fields = {
    project_name: document.getElementById("exp-project").value.trim(),
    batch: document.getElementById("exp-batch").value.trim(),
    subbatch: document.getElementById("exp-subbatch").value.trim(),
    first_sample: document.getElementById("exp-first-sample").value.trim(),
    n_samples: document.getElementById("exp-n-samples").value.trim(),
  };
  if (Object.values(fields).some((value) => !value)) {
    showError("Fill in all experiment info fields.");
    return;
  }
  const nSamples = Number(fields.n_samples);
  if (!Number.isInteger(nSamples) || nSamples < 1) {
    showError("Number of samples must be a whole number of at least 1.");
    return;
  }
  const info = { ...fields, n_samples: nSamples };
  // Sample/substrate info - the same for every sample of the experiment.
  // Blank fields are omitted; the server applies its defaults.
  const stringField = (id, key) => {
    const value = document.getElementById(id).value.trim();
    if (value) info[key] = value;
  };
  const numberField = (id, key) => {
    const value = document.getElementById(id).value.trim();
    if (!value) return true;
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) {
      showError("Invalid number in " + key.replaceAll("_", " ") + ".");
      return false;
    }
    info[key] = parsed;
    return true;
  };
  stringField("exp-substrate-material", "substrate_material");
  stringField("exp-substrate-conductive-layer", "substrate_conductive_layer");
  stringField("exp-sample-dimension", "sample_dimension");
  const numbersOk = [
    numberField("exp-number-of-pixels", "number_of_pixels"),
    numberField("exp-sample-area", "sample_area"),
    numberField("exp-pixel-area", "pixel_area"),
    numberField("exp-sheet-resistance", "sheet_resistance"),
    numberField("exp-transmission", "transmission"),
    numberField("exp-number-of-junctions", "number_of_junctions"),
  ].every(Boolean);
  if (!numbersOk) return;
  const body = { info };
  try {
    const res = await authFetch("api/input-collections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showError("Could not create experiment: " + await errorDetail(res));
      return;
    }
    const created = await res.json();
    newExperimentForm.hidden = true;
    for (const input of newExperimentForm.querySelectorAll("input")) {
      input.value = input.defaultValue; // keeps the Glass/ITO/6 prefills
    }
    // Insert the new experiment locally: NOMAD indexes the entry
    // asynchronously, so an immediate list refetch would not have it.
    addExperimentOption(created);
    experimentSelect.value = created.entry_id;
    try {
      localStorage.setItem(SELECTED_EXPERIMENT_KEY, created.entry_id);
    } catch { /* ignore */ }
    updateExperimentLink();
  } catch (err) {
    showError("Network error: " + err.message);
  }
});

let mediaRecorder = null;
let chunks = [];
let timerInterval = null;
let startTime = 0;

function showError(msg) {
  error.textContent = msg;
  error.style.display = "block";
}

function clearError() {
  error.textContent = "";
  error.style.display = "none";
}

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

// --- live transcription while recording (best-effort) ------------------
// Chunks stream to sand's Deepgram relay in parallel with the local
// accumulation; if the relay is off or fails, recording works unchanged.

// One object per relay connection: handlers close over it, so a socket
// that outlives its recording (draining finals) or a stale callback from
// a quickly-restarted recording can never touch the next recording's
// state. `liveConn` always points at the connection of the CURRENT
// recording; only that one may write to the panel or receive chunks.
let liveConn = null;

const liveTranscriptEl = document.getElementById("live-transcript");
const liveFinalEl = document.getElementById("live-final");
const liveInterimEl = document.getElementById("live-interim");

function liveTranscriptUrl() {
  const url = new URL("api/live-transcript", window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

function startLiveTranscript() {
  liveFinalEl.textContent = "";
  liveInterimEl.textContent = "";
  liveTranscriptEl.hidden = true;
  let ws;
  try {
    ws = new WebSocket(liveTranscriptUrl());
  } catch (err) {
    return null;
  }
  const conn = {
    ws,
    ready: false,
    stopped: false,
    // MediaRecorder's FIRST chunk carries the WebM container header, so
    // chunks produced before the relay is ready are queued, not dropped.
    queue: [],
    // finals accumulate here (not read back from the DOM), so the value
    // resolved on close is this recording's text even if another
    // recording has taken over the panel meanwhile
    finals: "",
    finish: null,
    done: null,
  };
  conn.done = new Promise((resolve) => {
    conn.finish = resolve;
  });
  liveConn = conn;

  ws.onopen = () => ws.send(JSON.stringify({ token: keycloak.token }));
  ws.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    if (msg.type === "relay-ready") {
      if (liveConn !== conn) {
        // superseded by a newer recording: never touch its state
        ws.close();
        return;
      }
      conn.ready = true;
      liveTranscriptEl.hidden = false;
      for (const chunk of conn.queue) ws.send(chunk);
      conn.queue = [];
      // recording already stopped while we were connecting: the queued
      // chunks (header included) are sent above, now ask for the flush
      if (conn.stopped) ws.send(JSON.stringify({ type: "relay-stop" }));
      return;
    }
    const alt = msg.channel && msg.channel.alternatives && msg.channel.alternatives[0];
    if (!alt) return;
    if (msg.is_final) {
      if (alt.transcript) {
        conn.finals += (conn.finals ? " " : "") + alt.transcript;
      }
    }
    if (liveConn === conn) {
      // drain finals of a stopped recording still render, but a newer
      // recording owns the panel
      if (msg.is_final) {
        liveFinalEl.textContent = conn.finals;
        liveInterimEl.textContent = "";
      } else {
        liveInterimEl.textContent = alt.transcript || "";
      }
    }
  };
  // clean completion only: a close after a requested stop carries the
  // finals; an unexpected close resolves empty, so a half-dead stream
  // can never be saved as a complete transcript (whisper covers it)
  ws.onclose = () => conn.finish(conn.stopped ? conn.finals.trim() : "");
  return conn;
}

function sendLiveChunk(chunk) {
  const conn = liveConn;
  if (!conn || conn.stopped) return;
  if (conn.ready && conn.ws.readyState === WebSocket.OPEN) {
    conn.ws.send(chunk);
  } else if (conn.ws.readyState !== WebSocket.CLOSED) {
    conn.queue.push(chunk);
  }
}

// Resolves with this recording's final transcript once the relay socket
// has closed - Deepgram's LAST finals arrive after the stop message, so
// reading any earlier would truncate the text.
function stopLiveTranscript(conn, detach = false) {
  if (!conn) return Promise.resolve("");
  if (detach && liveConn === conn) liveConn = null;
  if (conn.stopped) return conn.done;
  conn.stopped = true;
  const ws = conn.ws;
  if (ws.readyState === WebSocket.OPEN && conn.ready) {
    // ask sand to flush Deepgram; the remaining finals arrive before close
    ws.send(JSON.stringify({ type: "relay-stop" }));
  }
  // not ready yet: keep the socket - the relay-ready handler flushes the
  // queued chunks and sends relay-stop itself. Either way, give up after
  // a deadline so the upload can never hang on a wedged socket.
  setTimeout(() => {
    if (ws.readyState !== WebSocket.CLOSED) ws.close();
    conn.finish(""); // a wedged socket is not a clean completion
  }, 12000);
  if (ws.readyState === WebSocket.CLOSED) conn.finish("");
  return conn.done;
}

// --- save-live-transcript toggle -----------------------------------
// The user decides per recording whether the live text is stored
// (whisper skipped) or display-only (whisper transcribes). The server
// config only sets the default; the last choice is remembered locally.

const storeLiveLabel = document.getElementById("store-live-label");
const storeLiveToggle = document.getElementById("store-live-toggle");
let storeLiveConfigured = false;
const STORE_LIVE_KEY = "sand.storeLiveTranscript";

async function initUiConfig() {
  try {
    const res = await fetch("ui-config");
    const cfg = await res.json();
    if (!cfg.live_transcript_available) return; // no relay: keep hidden
    let remembered = null;
    try { remembered = localStorage.getItem(STORE_LIVE_KEY); } catch { /* ignore */ }
    storeLiveToggle.checked =
      remembered === null ? Boolean(cfg.store_live_transcript) : remembered === "true";
    storeLiveConfigured = true;
    storeLiveLabel.hidden = false;
  } catch { /* toggle stays hidden; recording works without it */ }
}
initUiConfig();

storeLiveToggle.addEventListener("change", () => {
  try { localStorage.setItem(STORE_LIVE_KEY, String(storeLiveToggle.checked)); } catch { /* ignore */ }
});

// The experiment chosen when recording started: the upload must go
// there even if the dropdown changes while recording.
let recordingExperiment = null;

const discardBtn = document.getElementById("discard-btn");

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
  experimentSelect.disabled = true;
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
        experimentSelect.disabled = false;
        recordingExperiment = null;
        chunks = [];
        liveFinalEl.textContent = "";
        liveInterimEl.textContent = "";
        liveTranscriptEl.hidden = true;
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
    experimentSelect.disabled = false;
    // fires after the final ondataavailable, so the last chunk has been
    // streamed before we tell the relay to flush; the promise resolves
    // with the full final transcript once the relay closed
    // the toggle state at stop time decides whether the live text is
    // stored; either way the panel keeps showing it
    const storeLive = !storeLiveLabel.hidden && storeLiveToggle.checked;
    statusEl.textContent = "Finishing transcript...";
    const liveTranscript = await stopLiveTranscript(myConn);
    const blob = new Blob(recorded, { type: mimeType });
    if (blob.size === 0) {
      showError("No audio recorded.");
      statusEl.textContent = "";
      uploadBtn.disabled = false;
      return;
    }
    await uploadAudio(blob, experiment, storeLive ? liveTranscript : "");
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
    stopLiveTranscript(liveConn);
  }
  discardBtn.hidden = true;
  stopTimer();
  recordBtn.innerHTML = '<span class="material-icons">mic</span> Record';
  recordBtn.classList.remove("btn-recording");
  recordBtn.classList.add("btn-primary");
}

const audioEntryEl = document.getElementById("audio-entry");

function clearAudioEntryLink() {
  audioEntryEl.replaceChildren();
  audioEntryEl.style.display = "none";
}

function showEntryLink(message, entryUrl, linkText) {
  const link = document.createElement("a");
  link.href = entryUrl;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = linkText;
  audioEntryEl.replaceChildren(document.createTextNode(message + " "), link);
  audioEntryEl.style.display = "block";
}

async function handleEntryResponse(fetchPromise, failPrefix, message, linkText) {
  const res = await fetchPromise;
  if (!res.ok) {
    showError(failPrefix + ": " + await errorDetail(res));
    return false;
  }
  const data = await res.json();
  showEntryLink(message, data.entry_url, linkText);
  // a new input exists: show it in the list (audio rows start as
  // "transcribing..." and the list keeps refreshing until text arrives)
  const experiment = selectedExperiment();
  if (experiment) startInputsRefresh(experiment, 1000);
  return true;
}

async function uploadAudio(blobOrFile, experiment, transcript) {
  if (!experiment) return;
  recordBtn.disabled = true;
  uploadBtn.disabled = true;
  statusEl.textContent = "Uploading audio to NOMAD...";
  clearError();
  clearAudioEntryLink();

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

  const audioUrl = "api/input-collections/" + experiment.upload_id
    + "/audio?collection_entry_id=" + encodeURIComponent(experiment.entry_id);
  try {
    await handleEntryResponse(
      authFetch(audioUrl, { method: "POST", body: form }),
      "Audio upload failed",
      "Audio added to " + experiment.name + ".",
      "View audio entry on NOMAD"
    );
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    recordBtn.disabled = false;
    uploadBtn.disabled = false;
    statusEl.textContent = "";
  }
}

saveNoteBtn.addEventListener("click", async () => {
  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  const text = textArea.value.trim();
  if (!text) {
    showError("Nothing to save. Type a step note first.");
    return;
  }
  saveNoteBtn.disabled = true;
  clearAudioEntryLink();
  try {
    const notesUrl = "api/input-collections/" + experiment.upload_id
      + "/notes?collection_entry_id=" + encodeURIComponent(experiment.entry_id);
    const saved = await handleEntryResponse(
      authFetch(notesUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      }),
      "Saving the note failed",
      "Note added to " + experiment.name + ".",
      "View note on NOMAD"
    );
    if (saved) textArea.value = "";
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    saveNoteBtn.disabled = false;
  }
});

const extractBtn = document.getElementById("extract-btn");
const extractStatus = document.getElementById("extract-status");
const extractResult = document.getElementById("extract-result");
const extractSummary = document.getElementById("extract-summary");
const derivedEntryEl = document.getElementById("derived-entry");
const sheetIssuesEl = document.getElementById("sheet-issues");

// --- inputs card: every recording/note, click to revise ----------------
// Rows come in extraction order. Clicking a row loads its text into the
// big Input text box as an explicit "revising" mode: the label and
// buttons swap, and any unsaved note draft is stashed and restored on
// exit - no data loss, no second cramped editor. Audio revisions go to
// corrected_transcript (clearing withdraws them); note revisions
// overwrite the note text.

const inputsCard = document.getElementById("inputs-card");
const inputsList = document.getElementById("inputs-list");
const inputsCount = document.getElementById("inputs-count");
const refreshInputsBtn = document.getElementById("refresh-inputs-btn");
const textLabel = document.getElementById("text-label");
const saveRevisionBtn = document.getElementById("save-revision-btn");
const cancelRevisionBtn = document.getElementById("cancel-revision-btn");

let inputsGeneration = 0;
let inputsRefreshTimer = null;
// {item, experiment, draftBackup} while the text box is in revising mode
let revising = null;

function stopInputsRefresh() {
  inputsGeneration += 1;
  if (inputsRefreshTimer) clearTimeout(inputsRefreshTimer);
  inputsRefreshTimer = null;
}

function inputTime(item) {
  if (!item.datetime) return "";
  const parsed = new Date(item.datetime);
  if (Number.isNaN(parsed.getTime())) return "";
  const time = parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (parsed.toDateString() === new Date().toDateString()) return time;
  return parsed.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + time;
}

// while a time editor is open, list re-renders are held off so the
// input field is not wiped mid-edit
let timeEditing = false;

function toLocalInputValue(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
    + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
}

function beginTimeEdit(whenSpan, item, experiment) {
  if (timeEditing) return;
  timeEditing = true;
  const editor = document.createElement("input");
  editor.type = "datetime-local";
  editor.className = "input-time-edit";
  editor.value = toLocalInputValue(item.datetime);
  editor.addEventListener("click", (e) => e.stopPropagation());
  let done = false;
  const finish = (refresh) => {
    if (done) return;
    done = true;
    timeEditing = false;
    editor.replaceWith(whenSpan);
    // the server returns once NOMAD reprocessed the entry
    if (refresh) startInputsRefresh(experiment);
  };
  let saving = false;
  const commit = async () => {
    // disabling the focused editor below fires blur -> commit again
    if (done || saving) return;
    if (!editor.value || toLocalInputValue(item.datetime) === editor.value) {
      finish(false);
      return;
    }
    const iso = new Date(editor.value).toISOString();
    clearError();
    saving = true;
    editor.disabled = true;
    try {
      const res = await authFetch(
        "api/input-collections/" + experiment.upload_id
        + "/inputs/" + encodeURIComponent(item.entry_id)
        + "/datetime?collection_entry_id=" + encodeURIComponent(experiment.entry_id),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ datetime: iso }),
        });
      if (!res.ok) {
        showError("Could not change the time: " + await errorDetail(res));
        finish(false);
        return;
      }
      finish(true);
    } catch (err) {
      showError("Network error: " + err.message);
      finish(false);
    }
  };
  editor.addEventListener("keydown", (e) => {
    if (e.key === "Enter") commit();
    if (e.key === "Escape") finish(false);
  });
  editor.addEventListener("blur", commit);
  whenSpan.replaceWith(editor);
  editor.focus();
}

function inputDescription(item) {
  return (item.kind === "audio" ? "recording" : "note")
    + (inputTime(item) ? " from " + inputTime(item) : "");
}

function beginRevision(item, experiment) {
  if (revising && revising.item.entry_id === item.entry_id) return;
  // entering revision mode is non-destructive: a note draft is stashed
  // and restored on exit. Only switching rows with unsaved revision
  // edits would lose something - ask then.
  if (revising && textArea.value.trim() !== (revising.item.text || "").trim()
      && !window.confirm("Discard the unsaved revision and open this input?")) {
    return;
  }
  const draftBackup = revising ? revising.draftBackup : textArea.value;
  revising = { item, experiment, draftBackup };
  textArea.value = item.text || "";
  textArea.classList.add("revising");
  textLabel.textContent = "Revising the " + inputDescription(item)
    + (item.kind === "audio" ? " (saved as corrected transcript)" : "");
  recordBtn.hidden = true;
  uploadBtn.hidden = true;
  saveNoteBtn.hidden = true;
  storeLiveLabel.hidden = true;
  saveRevisionBtn.hidden = false;
  cancelRevisionBtn.hidden = false;
  highlightSelectedRow();
  textArea.focus();
  textArea.scrollIntoView({ behavior: "smooth", block: "center" });
}

function endRevision() {
  if (!revising) return;
  textArea.value = revising.draftBackup;
  revising = null;
  textArea.classList.remove("revising");
  textLabel.textContent = "Input text";
  recordBtn.hidden = false;
  uploadBtn.hidden = false;
  saveNoteBtn.hidden = false;
  // the toggle only exists when the relay is configured; initUiConfig
  // decides, so just restore what it decided
  storeLiveLabel.hidden = !storeLiveConfigured;
  saveRevisionBtn.hidden = true;
  cancelRevisionBtn.hidden = true;
  highlightSelectedRow();
}

function highlightSelectedRow() {
  for (const tile of inputsList.querySelectorAll(".input-tile")) {
    tile.classList.toggle(
      "selected",
      Boolean(revising) && tile.dataset.entryId === revising.item.entry_id
    );
  }
}

cancelRevisionBtn.addEventListener("click", endRevision);

saveRevisionBtn.addEventListener("click", async () => {
  if (!revising) return;
  const { item, experiment } = revising;
  const text = textArea.value.trim();
  if (text === (item.text || "").trim()) {
    // unchanged: save nothing - a stored revision must mean a human
    // actually changed something
    endRevision();
    return;
  }
  clearError();
  saveRevisionBtn.disabled = true;
  try {
    const res = await authFetch(
      "api/input-collections/" + experiment.upload_id
      + "/inputs/" + encodeURIComponent(item.entry_id)
      + "/text?collection_entry_id=" + encodeURIComponent(experiment.entry_id),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
    if (!res.ok) {
      showError("Could not save the revision: " + await errorDetail(res));
      return;
    }
    endRevision();
    // the entry reprocesses asynchronously; refresh shortly so the row
    // shows the effective text (withdrawn corrections included)
    startInputsRefresh(experiment, 1500);
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    saveRevisionBtn.disabled = false;
  }
});

function renderInputs(experiment, items) {
  inputsCard.hidden = false;
  inputsCount.textContent = "(" + items.length + ")";
  inputsList.replaceChildren();
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "inputs-empty";
    li.textContent = "No inputs yet - record or type a note above.";
    inputsList.append(li);
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    li.className = "input-tile";
    li.dataset.entryId = item.entry_id;

    const meta = document.createElement("div");
    meta.className = "input-meta";
    const icon = document.createElement("span");
    icon.className = "material-icons";
    icon.textContent = item.kind === "audio" ? "mic" : "edit_note";
    const kind = document.createElement("span");
    kind.className = "input-kind";
    kind.textContent = item.kind === "audio" ? "Recording" : "Note";
    meta.append(icon, kind);
    const when = document.createElement("span");
    when.className = "input-when";
    const time = inputTime(item);
    when.textContent = "\u00b7 " + (time || "set time");
    when.title = "Click to change the time (reorders the inputs)";
    when.addEventListener("click", (e) => {
      e.stopPropagation();
      beginTimeEdit(when, item, experiment);
    });
    meta.append(when);
    if (item.corrected) {
      const badge = document.createElement("span");
      badge.className = "input-badge";
      badge.textContent = "\u00b7 \u270e corrected";
      meta.append(badge);
    }
    const nomadLink = document.createElement("a");
    nomadLink.href = item.entry_url;
    nomadLink.target = "_blank";
    nomadLink.rel = "noopener noreferrer";
    nomadLink.className = "material-icons input-nomad-link";
    nomadLink.textContent = "open_in_new";
    nomadLink.title = "View on NOMAD";
    nomadLink.addEventListener("click", (e) => e.stopPropagation());
    meta.append(nomadLink);

    const text = document.createElement("div");
    text.className = "input-text";
    if (item.text) {
      text.textContent = item.text;
    } else {
      text.textContent =
        item.status === "FAILED" ? "transcription failed" : "transcribing...";
      text.classList.add("input-pending");
    }

    li.append(meta, text);
    li.addEventListener("click", () => beginRevision(item, experiment));
    inputsList.append(li);
  }
  highlightSelectedRow();
}

async function loadInputs(experiment, generation) {
  if (generation !== inputsGeneration) return;
  let res;
  try {
    res = await authFetch(
      "api/input-collections/" + experiment.upload_id
      + "/inputs?collection_entry_id=" + encodeURIComponent(experiment.entry_id));
  } catch (err) {
    return; // next manual refresh or upload will retry
  }
  if (generation !== inputsGeneration) return;
  if (!res.ok) return;
  const data = await res.json().catch(() => null);
  if (generation !== inputsGeneration || !data) return;
  if (timeEditing) {
    // don't wipe an open time editor; try again shortly
    inputsRefreshTimer = setTimeout(() => loadInputs(experiment, generation), 3000);
    return;
  }
  renderInputs(experiment, data.inputs);
  // keep refreshing while any audio still has no text and no failure
  if (data.inputs.some((i) => i.kind === "audio" && !i.text && i.status !== "FAILED")) {
    inputsRefreshTimer = setTimeout(() => loadInputs(experiment, generation), 5000);
  }
}

function startInputsRefresh(experiment, delayMs) {
  stopInputsRefresh();
  const generation = inputsGeneration;
  inputsRefreshTimer = setTimeout(
    () => loadInputs(experiment, generation), delayMs || 0);
}

refreshInputsBtn.addEventListener("click", () => {
  const experiment = selectedExperiment();
  if (experiment) startInputsRefresh(experiment);
});

// --- asynchronous extraction (issue #19) --------------------------------
// Extract starts a workflow and returns a job id; progress lives in a
// status file in the upload, polled every few seconds. Reload-proof:
// selecting an experiment checks for an unfinished job and resumes the
// polling - no job id needs to survive in the browser.

let extractPollTimer = null;
// bumped whenever polling (re)starts or stops: in-flight responses from
// a superseded poll are ignored instead of repainting the new selection
let extractPollGeneration = 0;

function entryUrlFor(experiment, entryId) {
  // .../upload/id/<upload>/entry/id/<entry> - swap the entry id
  return experiment.entry_url.replace(/entry\/id\/[^/]+$/, "entry/id/" + entryId);
}

function stopExtractPolling() {
  extractPollGeneration += 1;
  if (extractPollTimer) clearTimeout(extractPollTimer);
  extractPollTimer = null;
}

function startExtractPolling(experiment, delayMs) {
  stopExtractPolling();
  const generation = extractPollGeneration;
  extractPollTimer = setTimeout(
    () => pollExtraction(experiment, generation), delayMs || 0);
}

function describePhase(status) {
  if (status.phase === "extracting") {
    return "Extracting step " + (status.steps_done ?? 0) + "/" + (status.steps_total ?? "?") + "...";
  }
  if (status.phase === "collecting") return "Collecting inputs...";
  if (status.phase === "writing-sheet") return "Writing and parsing the sheet...";
  return status.phase + "...";
}

function renderExtractResult(experiment, status) {
  extractSummary.textContent = (status.step_types || []).length
    ? "Steps: " + status.step_types.join(" \u2192 ")
    : "";
  derivedEntryEl.replaceChildren();
  derivedEntryEl.style.display = "none";
  if (status.derived_entry_id) {
    const link = document.createElement("a");
    link.href = entryUrlFor(experiment, status.derived_entry_id);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "View derived experiment on NOMAD";
    derivedEntryEl.replaceChildren(link);
    derivedEntryEl.style.display = "block";
  }
  const notes = [];
  for (const w of status.warnings || []) notes.push("Warning: " + w);
  const issues = status.sheet_issues || [];
  if (issues.length) notes.push("Sheet could not hold everything: " + issues.join("; "));
  sheetIssuesEl.textContent = notes.join(" \u2014 ");
  sheetIssuesEl.style.display = notes.length ? "block" : "none";
  extractResult.hidden = false;
}

async function pollExtraction(experiment, generation) {
  if (generation !== extractPollGeneration) return; // superseded
  extractBtn.disabled = true;
  let res;
  try {
    res = await authFetch(
      "api/input-collections/" + experiment.upload_id
      + "/extract-status?collection_entry_id=" + encodeURIComponent(experiment.entry_id));
  } catch (err) {
    // transient network problem: keep polling
    if (generation !== extractPollGeneration) return;
    extractPollTimer = setTimeout(() => pollExtraction(experiment, generation), 3000);
    return;
  }
  if (generation !== extractPollGeneration) return; // selection changed mid-flight
  if (res.status === 404) {
    // no extraction for this collection (or none yet)
    extractBtn.disabled = false;
    extractStatus.textContent = "";
    return;
  }
  const status = await res.json().catch(() => null);
  if (generation !== extractPollGeneration) return;
  if (!status) {
    extractPollTimer = setTimeout(() => pollExtraction(experiment, generation), 3000);
    return;
  }
  if (status.phase === "completed") {
    extractBtn.disabled = false;
    extractStatus.textContent = "";
    renderExtractResult(experiment, status);
    return;
  }
  if (status.phase === "failed") {
    extractBtn.disabled = false;
    extractStatus.textContent = "";
    showError("Extract failed: " + (status.error || "unknown error"));
    return;
  }
  extractStatus.textContent = describePhase(status);
  extractPollTimer = setTimeout(() => pollExtraction(experiment, generation), 3000);
}

// selecting an experiment resumes the polling of an unfinished job
experimentSelect.addEventListener("change", () => {
  stopExtractPolling();
  extractBtn.disabled = false;
  extractStatus.textContent = "";
  extractResult.hidden = true;
  const experiment = selectedExperiment();
  endRevision();
  inputsList.replaceChildren();
  inputsCount.textContent = "";
  inputsCard.hidden = true;
  if (experiment) {
    startExtractPolling(experiment);
    startInputsRefresh(experiment);
  }
});

extractBtn.addEventListener("click", async () => {
  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  extractBtn.disabled = true;
  extractResult.hidden = true;
  extractStatus.textContent = "Starting extraction...";
  try {
    const res = await authFetch(
      "api/input-collections/" + experiment.upload_id
      + "/extract-async?collection_entry_id=" + encodeURIComponent(experiment.entry_id),
      { method: "POST" });
    if (!res.ok) {
      showError("Extract failed: " + await errorDetail(res));
      extractBtn.disabled = false;
      extractStatus.textContent = "";
      return;
    }
    // the job id is not kept: the status file is found by experiment
    startExtractPolling(experiment, 2000);
  } catch (err) {
    showError("Network error: " + err.message);
    extractBtn.disabled = false;
    extractStatus.textContent = "";
  }
});

const downloadSheetBtn = document.getElementById("download-sheet-btn");

downloadSheetBtn.addEventListener("click", async () => {
  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  downloadSheetBtn.disabled = true;
  try {
    const res = await authFetch(
      "api/input-collections/" + experiment.upload_id
      + "/sheet?collection_entry_id=" + encodeURIComponent(experiment.entry_id));
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      showError("Download failed: " + (body && body.detail ? body.detail : res.statusText));
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = "hysprint_experiment.xlsx";
    link.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    downloadSheetBtn.disabled = false;
  }
});

const uploadSheetBtn = document.getElementById("upload-sheet-btn");
const uploadSheetInput = document.getElementById("upload-sheet-input");

uploadSheetBtn.addEventListener("click", () => {
  uploadSheetInput.click();
});

uploadSheetInput.addEventListener("change", async () => {
  const file = uploadSheetInput.files[0];
  uploadSheetInput.value = "";
  if (!file) return;

  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  if (!window.confirm("Replace the sheet on NOMAD with this file?")) return;

  uploadSheetBtn.disabled = true;
  extractStatus.textContent = "Uploading sheet...";
  try {
    const form = new FormData();
    form.append("file", file);
    const res = await authFetch(
      "api/input-collections/" + experiment.upload_id
      + "/sheet?collection_entry_id=" + encodeURIComponent(experiment.entry_id),
      { method: "PUT", body: form });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      showError("Sheet upload failed: " + (body && body.detail ? body.detail : res.statusText));
      return;
    }
    extractStatus.textContent = body.changed
      ? "Sheet replaced and reparsed."
      : "Sheet unchanged (same content).";
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    uploadSheetBtn.disabled = false;
    if (!extractStatus.textContent.startsWith("Sheet")) extractStatus.textContent = "";
  }
});

const uploadInput = document.getElementById("upload-input");

uploadBtn.addEventListener("click", () => {
  uploadInput.click();
});

uploadInput.addEventListener("change", async () => {
  const file = uploadInput.files[0];
  uploadInput.value = "";
  if (!file) return;

  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  // Keep in sync with MAX_UPLOAD_BYTES in apis/routers/experiments.py.
  const MAX_SIZE = 25 * 1024 * 1024;
  if (file.size > MAX_SIZE) {
    showError("File too large (max 25 MB).");
    return;
  }
  await uploadAudio(file, experiment);
});

discardBtn.addEventListener("click", () => {
  if (!mediaRecorder || mediaRecorder.state !== "recording") return;
  if (!window.confirm("Discard this recording? Nothing will be saved.")) return;
  // intent rides on THIS recorder object: a quick discard-then-redo
  // creates a new recorder and cannot re-route or reset it
  mediaRecorder.discardRequested = true;
  stopRecording();
});

recordBtn.addEventListener("click", () => {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    stopRecording();
  } else {
    startRecording();
  }
});

window.addEventListener("beforeunload", () => {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stream.getTracks().forEach((t) => t.stop());
  }
});

initAuth().catch((err) => {
  console.error("Auth init failed:", err);
  showLoginPrompt();
});
