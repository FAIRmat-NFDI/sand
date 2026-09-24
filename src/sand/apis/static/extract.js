// The Extraction card: asynchronous extraction (issue #19) and the
// sheet download/upload.
//
// Extract starts a workflow and returns a job id; progress lives in a
// status file in the upload, polled every few seconds. Reload-proof:
// selecting an experiment checks for an unfinished job and resumes the
// polling - no job id needs to survive in the browser.

import { authFetch, errorDetail, experimentUrl } from "./api.js";
import { requireExperiment } from "./experiments.js";
import { clearError, showError } from "./ui.js";

const extractBtn = document.getElementById("extract-btn");
const extractStatus = document.getElementById("extract-status");
const extractResult = document.getElementById("extract-result");
const extractSummary = document.getElementById("extract-summary");
const derivedEntryEl = document.getElementById("derived-entry");
const sheetIssuesEl = document.getElementById("sheet-issues");
const downloadSheetBtn = document.getElementById("download-sheet-btn");
const uploadSheetBtn = document.getElementById("upload-sheet-btn");
const uploadSheetInput = document.getElementById("upload-sheet-input");

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
    ? "Steps: " + status.step_types.join(" → ")
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
  sheetIssuesEl.textContent = notes.join(" — ");
  sheetIssuesEl.style.display = notes.length ? "block" : "none";
  extractResult.hidden = false;
}

async function pollExtraction(experiment, generation) {
  if (generation !== extractPollGeneration) return; // superseded
  extractBtn.disabled = true;
  let res;
  try {
    res = await authFetch(experimentUrl(experiment, "extract-status"));
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

// A newly selected experiment (or none): clear the old result and
// resume the polling of an unfinished job.
export function showExtraction(experiment) {
  stopExtractPolling();
  extractBtn.disabled = false;
  extractStatus.textContent = "";
  extractResult.hidden = true;
  if (experiment) startExtractPolling(experiment);
}

async function startExtraction() {
  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  extractBtn.disabled = true;
  extractResult.hidden = true;
  extractStatus.textContent = "Starting extraction...";
  try {
    const res = await authFetch(experimentUrl(experiment, "extract-async"), { method: "POST" });
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
}

async function downloadSheet() {
  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  downloadSheetBtn.disabled = true;
  try {
    const res = await authFetch(experimentUrl(experiment, "sheet"));
    if (!res.ok) {
      showError("Download failed: " + await errorDetail(res));
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
}

async function uploadSheet() {
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
    const res = await authFetch(experimentUrl(experiment, "sheet"), { method: "PUT", body: form });
    if (!res.ok) {
      showError("Sheet upload failed: " + await errorDetail(res));
      return;
    }
    const body = await res.json();
    extractStatus.textContent = body.changed
      ? "Sheet replaced and reparsed."
      : "Sheet unchanged (same content).";
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    uploadSheetBtn.disabled = false;
    if (!extractStatus.textContent.startsWith("Sheet")) extractStatus.textContent = "";
  }
}

export function initExtract() {
  extractBtn.addEventListener("click", startExtraction);
  downloadSheetBtn.addEventListener("click", downloadSheet);
  uploadSheetBtn.addEventListener("click", () => uploadSheetInput.click());
  uploadSheetInput.addEventListener("change", uploadSheet);
}
