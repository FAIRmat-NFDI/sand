// The sheet header: the open sheet's name, what tells it from the other
// sheets and its layer stack (from the sheet view), the "Other sheets"
// list, and the drop zone that uploads a new sheet. Other parts react to the selection through the handler
// set with onExperimentSelected. In code a sheet is still an experiment
// (an InputCollection entry in NOMAD); on the page it is a sheet.

import { errorDetail } from "./api.js";
import {
  isSheetFile, metaLines, nameFromFile, uploadedText,
} from "./sheet-header-logic.js";
import { layerStack } from "./overview-logic.js";
import { clearError, showError } from "./ui.js";

const header = document.getElementById("sheet-header");
const sheetNameEl = document.getElementById("sheet-name");
const experimentLink = document.getElementById("experiment-link");
const sheetMeta = document.getElementById("sheet-meta");
const sheetStack = document.getElementById("sheet-stack");
const sheetsBtn = document.getElementById("sheets-btn");
const sheetsBtnLabel = document.getElementById("sheets-btn-label");
const sheetsMenu = document.getElementById("sheets-menu");
const uploadPanel = document.getElementById("sheet-upload");
const dropZone = document.getElementById("drop-zone");
const dropText = document.getElementById("drop-text");
const sheetFile = document.getElementById("sheet-file");
const sheetNameInput = document.getElementById("sheet-name-input");
const uploadBtn = document.getElementById("upload-sheet-btn");
const cancelUploadBtn = document.getElementById("cancel-upload-btn");
const sheetProblems = document.getElementById("sheet-problems");
const SELECTED_EXPERIMENT_KEY = "sand.selectedExperiment";

let experiments = []; // newest first, as listed
let experimentsById = {};
let selectedId = "";
let selectionHandler = () => {};
let locked = false; // a recording runs: no switching, no new sheet
let uploading = false;
let chosenFile = null; // the file dropped or chosen, not uploaded yet

export function selectedExperiment() {
  return experimentsById[selectedId] || null;
}

export function requireExperiment() {
  const experiment = selectedExperiment();
  if (!experiment) {
    showError("Open a sheet first, or upload one.");
    return null;
  }
  return experiment;
}

// called with the new selection (or null) whenever it changes
export function onExperimentSelected(handler) {
  selectionHandler = handler;
}

// a recording keeps its sheet: no switching until it is stopped
export function lockExperimentSelect(on) {
  locked = on;
  sheetsBtn.disabled = on;
  uploadBtn.disabled = on;
  if (on) closeMenu();
}

// --- the header's texts ------------------------------------------------------

function renderParts(el, parts) {
  el.replaceChildren();
  parts.forEach((part, i) => {
    if (i) el.append(" · ");
    const span = document.createElement("span");
    span.textContent = part;
    el.append(span);
  });
  el.hidden = parts.length === 0;
}

// The header for the selected sheet: `view` is the sheet view (GET
// .../sheet-view), null for an experiment made without a sheet,
// undefined while it is not known (loading, or the read failed): then
// the header claims nothing about the sheet.
export function showSheet(experiment, view) {
  header.classList.toggle("no-sheet", !experiment);
  header.classList.toggle("sheet-missing", view === null);
  if (!experiment) {
    sheetNameEl.textContent = "";
    experimentLink.hidden = true;
    renderParts(sheetMeta, []);
    renderParts(sheetStack, []);
    return;
  }
  sheetNameEl.textContent = experiment.name || experiment.upload_id;
  experimentLink.href = experiment.entry_url;
  experimentLink.hidden = false;
  if (!view) {
    renderParts(sheetMeta, view === null ? ["Made without a sheet: nothing to update here."] : []);
    renderParts(sheetStack, []);
    return;
  }
  renderParts(sheetMeta, metaLines(view.info || {}).main);
  const stack = layerStack(view);
  renderParts(sheetStack, stack ? [stack] : []);
}

// --- selection ---------------------------------------------------------------

function select(entryId) {
  selectedId = experimentsById[entryId] ? entryId : "";
  try {
    localStorage.setItem(SELECTED_EXPERIMENT_KEY, selectedId);
  } catch { /* ignore */ }
  closeMenu();
  showUpload(!selectedId);
  sheetsBtnLabel.textContent = selectedId ? "Other sheets" : "Your sheets";
  sheetsBtn.hidden = experiments.length === 0;
  selectionHandler(selectedExperiment());
}

// Keyed by entry_id: an upload can hold more than one InputCollection
// entry, so upload_id alone would collide.
function setExperiments(list) {
  experiments = list;
  experimentsById = {};
  for (const experiment of list) experimentsById[experiment.entry_id] = experiment;
}

export async function loadExperiments() {
  const res = await fetch("api/input-collections");
  if (!res.ok) {
    showError("Could not load your sheets: " + await errorDetail(res));
    return;
  }
  const data = await res.json();
  setExperiments(data.input_collections);
  let remembered = null;
  try { remembered = localStorage.getItem(SELECTED_EXPERIMENT_KEY); } catch { /* ignore */ }
  // restoring the selection announces it like a click, so an unfinished
  // job resumes its polling (reload-proof progress)
  select(remembered || "");
}

// --- the Other sheets menu ---------------------------------------------------

function menuItem(experiment) {
  const li = document.createElement("li");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "sheet-menu-item";
  if (experiment.entry_id === selectedId) btn.setAttribute("aria-current", "true");
  const name = document.createElement("span");
  name.className = "sheet-menu-name";
  name.textContent = experiment.name || experiment.upload_id;
  const when = document.createElement("span");
  when.className = "sheet-menu-when";
  when.textContent = uploadedText(experiment.created);
  btn.append(name, when);
  btn.addEventListener("click", () => {
    select(experiment.entry_id);
    sheetsBtn.focus();
  });
  li.append(btn);
  return li;
}

function openMenu() {
  sheetsMenu.replaceChildren(...experiments.map(menuItem));
  const li = document.createElement("li");
  li.className = "sheet-menu-new";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "sheet-menu-item";
  const icon = document.createElement("span");
  icon.className = "material-icons";
  icon.textContent = "upload_file";
  btn.append(icon, "Upload a new sheet");
  btn.addEventListener("click", () => {
    closeMenu();
    showUpload(true);
    dropZone.focus();
  });
  li.append(btn);
  sheetsMenu.append(li);
  sheetsMenu.hidden = false;
  sheetsBtn.setAttribute("aria-expanded", "true");
  (sheetsMenu.querySelector("[aria-current]") || sheetsMenu.querySelector("button"))?.focus();
}

function closeMenu() {
  sheetsMenu.hidden = true;
  sheetsBtn.setAttribute("aria-expanded", "false");
}

// --- upload ------------------------------------------------------------------

// The upload panel stands in for the header while no sheet is open; it
// can be dismissed only when there is a sheet to go back to.
function showUpload(on) {
  uploadPanel.hidden = !on;
  cancelUploadBtn.hidden = !selectedId;
  if (!on) resetUpload();
}

function resetUpload() {
  chosenFile = null;
  sheetFile.value = "";
  sheetNameInput.value = "";
  sheetNameInput.placeholder = "Name of the sheet (defaults to the file's)";
  dropText.textContent = "Drop the Excel sheet here, or";
  dropZone.classList.remove("has-file");
  sheetProblems.replaceChildren();
  sheetProblems.hidden = true;
}

function takeFile(file) {
  clearError();
  sheetProblems.replaceChildren();
  sheetProblems.hidden = true;
  if (!file) return;
  if (!isSheetFile(file.name)) {
    showError("Only an Excel sheet (.xlsx) can be updated here.");
    return;
  }
  chosenFile = file;
  dropText.textContent = file.name;
  dropZone.classList.add("has-file");
  sheetNameInput.placeholder = nameFromFile(file.name) || "Name of the sheet";
}

// A sheet becomes an experiment whose updates edit it. A refused sheet
// shows the problems verbatim, under the drop zone.
async function uploadSheet() {
  // Enter in the name field comes here too: the same guards as the button
  if (locked || uploading) return;
  clearError();
  sheetProblems.replaceChildren();
  sheetProblems.hidden = true;
  if (!chosenFile) {
    showError("Choose the Excel sheet to update.");
    return;
  }
  const form = new FormData();
  form.append("file", chosenFile);
  form.append("name", sheetNameInput.value.trim());
  uploading = true;
  uploadBtn.disabled = true;
  try {
    const res = await fetch("api/input-collections/from-sheet", { method: "POST", body: form });
    if (res.status === 422) {
      const body = await res.json().catch(() => null);
      const problems = body?.detail?.problems;
      if (Array.isArray(problems)) {
        for (const problem of problems) {
          const li = document.createElement("li");
          li.textContent = problem;
          sheetProblems.append(li);
        }
        sheetProblems.hidden = false;
        showError("The sheet cannot be updated as it is; see the problems under the drop zone.");
        return;
      }
    }
    if (!res.ok) {
      showError("Could not upload the sheet: " + await errorDetail(res));
      return;
    }
    const created = await res.json();
    setExperiments([created, ...experiments]);
    select(created.entry_id);
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    uploading = false;
    uploadBtn.disabled = locked;
  }
}

export function initExperiments() {
  sheetsBtn.addEventListener("click", () => {
    if (sheetsMenu.hidden) openMenu();
    else closeMenu();
  });
  document.addEventListener("click", (event) => {
    if (!sheetsMenu.hidden && !event.target.closest(".sheet-switch")) closeMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !sheetsMenu.hidden) {
      closeMenu();
      sheetsBtn.focus();
    }
  });
  cancelUploadBtn.addEventListener("click", () => showUpload(false));
  uploadBtn.addEventListener("click", uploadSheet);
  sheetFile.addEventListener("change", () => takeFile(sheetFile.files[0]));
  document.getElementById("choose-sheet-btn").addEventListener("click", () => sheetFile.click());
  // the file button inside the zone opens the picker; the zone itself too
  dropZone.addEventListener("click", (event) => {
    if (event.target.closest("button, input")) return;
    sheetFile.click();
  });
  for (const type of ["dragenter", "dragover"]) {
    dropZone.addEventListener(type, (event) => {
      event.preventDefault();
      dropZone.classList.add("drag-over");
    });
  }
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
  dropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropZone.classList.remove("drag-over");
    takeFile(event.dataTransfer?.files?.[0]);
  });
  sheetNameInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") uploadSheet();
  });
}
