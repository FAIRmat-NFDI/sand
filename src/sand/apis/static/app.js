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

// The experiment chosen when recording started: the upload must go
// there even if the dropdown changes while recording.
let recordingExperiment = null;

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
  mediaRecorder = new MediaRecorder(stream);

  mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) chunks.push(e.data);
  };

  mediaRecorder.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    experimentSelect.disabled = false;
    const experiment = recordingExperiment;
    recordingExperiment = null;
    const blob = new Blob(chunks, { type: mediaRecorder.mimeType });
    if (blob.size === 0) {
      showError("No audio recorded.");
      statusEl.textContent = "";
      uploadBtn.disabled = false;
      return;
    }
    await uploadAudio(blob, experiment);
  };

  mediaRecorder.start();
  recordBtn.innerHTML = '<span class="material-icons">stop</span> Stop';
  recordBtn.classList.remove("btn-primary");
  recordBtn.classList.add("btn-recording");
  uploadBtn.disabled = true;
  startTimer();
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
  }
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
  return true;
}

async function uploadAudio(blobOrFile, experiment) {
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
const extractJson = document.getElementById("extract-json");
const derivedEntryEl = document.getElementById("derived-entry");
const sheetIssuesEl = document.getElementById("sheet-issues");

extractBtn.addEventListener("click", async () => {
  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  extractBtn.disabled = true;
  extractResult.hidden = true;
  extractStatus.textContent = "Extracting... this can take a few minutes.";
  try {
    const extractUrl = "api/input-collections/" + experiment.upload_id
      + "/extract?collection_entry_id=" + encodeURIComponent(experiment.entry_id);
    const res = await authFetch(extractUrl, { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      const detail = body && body.detail ? body.detail : res.statusText;
      showError("Extract failed: " + detail);
      return;
    }
    const data = await res.json();
    extractSummary.textContent =
      data.archive.samples.length + " sample(s), "
      + data.archive.steps.length + " step(s): "
      + data.step_types.join(" → ");
    extractJson.textContent = JSON.stringify(data.archive, null, 2);
    derivedEntryEl.replaceChildren();
    derivedEntryEl.style.display = "none";
    if (data.derived_entry) {
      const link = document.createElement("a");
      link.href = data.derived_entry.entry_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "View derived experiment on NOMAD";
      derivedEntryEl.replaceChildren(link);
      derivedEntryEl.style.display = "block";
    }
    const issues = data.sheet_issues || [];
    sheetIssuesEl.textContent = issues.length
      ? "Sheet could not hold everything: " + issues.join("; ")
      : "";
    sheetIssuesEl.style.display = issues.length ? "block" : "none";
    extractResult.hidden = false;
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    extractBtn.disabled = false;
    extractStatus.textContent = "";
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
