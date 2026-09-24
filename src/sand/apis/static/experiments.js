// The experiment dropdown and the new-experiment form. Other parts react
// to the selection through the handler set with onExperimentSelected.

import { authFetch, errorDetail } from "./api.js";
import { clearError, showError } from "./ui.js";

const experimentSelect = document.getElementById("experiment-select");
const experimentLink = document.getElementById("experiment-link");
const newExperimentBtn = document.getElementById("new-experiment-btn");
const newExperimentForm = document.getElementById("new-experiment-form");
const SELECTED_EXPERIMENT_KEY = "sand.selectedExperiment";

let experimentsById = {};
let selectionHandler = () => {};

export function selectedExperiment() {
  return experimentsById[experimentSelect.value] || null;
}

export function requireExperiment() {
  const experiment = selectedExperiment();
  if (!experiment) {
    showError("Select an experiment first (or create a new one).");
    return null;
  }
  return experiment;
}

// called with the new selection (or null) whenever it changes
export function onExperimentSelected(handler) {
  selectionHandler = handler;
}

// a recording keeps its experiment: no switching until it is stopped
export function lockExperimentSelect(locked) {
  experimentSelect.disabled = locked;
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

function selectionChanged() {
  try {
    localStorage.setItem(SELECTED_EXPERIMENT_KEY, experimentSelect.value);
  } catch { /* ignore */ }
  updateExperimentLink();
  selectionHandler(selectedExperiment());
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

export async function loadExperiments() {
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
  if (remembered && experimentsById[remembered]) {
    experimentSelect.value = remembered;
  }
  // restoring the selection fires no 'change' event: announce it, so an
  // unfinished extraction resumes its polling (reload-proof progress)
  updateExperimentLink();
  selectionHandler(selectedExperiment());
}

async function createExperiment() {
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
  try {
    const res = await authFetch("api/input-collections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ info }),
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
    selectionChanged();
  } catch (err) {
    showError("Network error: " + err.message);
  }
}

export function initExperiments() {
  experimentSelect.addEventListener("change", selectionChanged);
  newExperimentBtn.addEventListener("click", () => {
    newExperimentForm.hidden = !newExperimentForm.hidden;
  });
  document.getElementById("cancel-experiment-btn").addEventListener("click", () => {
    newExperimentForm.hidden = true;
  });
  document.getElementById("create-experiment-btn").addEventListener("click", createExperiment);
}
