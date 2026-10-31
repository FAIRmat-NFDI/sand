// The Apply card of edit mode: run the edit job on the experiment's
// updates, show its progress, the changelog, and the final sheet to
// download. Confirmations are sent from the input tiles (inputs.js)
// through confirmEdit; applying again writes them into the sheet.
//
// Like the extraction: the job's status file is polled, and selecting
// an experiment resumes an unfinished job's polling (reload-proof).

import { errorDetail, experimentUrl } from "./api.js";
import { describeEditPhase, downloadFilename, editSummary, reportsByEntry } from "./edit-logic.js";
import { requireExperiment } from "./experiments.js";
import { setReports } from "./inputs.js";
import { clearError, showError } from "./ui.js";

const applyBtn = document.getElementById("apply-btn");
const applyStatus = document.getElementById("apply-status");
const applyResult = document.getElementById("apply-result");
const applySummary = document.getElementById("apply-summary");
const changelogEl = document.getElementById("changelog-text");
const downloadFinalBtn = document.getElementById("download-final-btn");
const sheetSummaryEl = document.getElementById("sheet-summary");

const POLL_MS = 3000;
let pollTimer = null;
let pollGeneration = 0;
let current = null; // the experiment the card shows

function stopPolling() {
  pollGeneration += 1;
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
}

function startPolling(experiment, delayMs) {
  stopPolling();
  const generation = pollGeneration;
  pollTimer = setTimeout(() => poll(experiment, generation), delayMs || 0);
}

function later(experiment, generation) {
  pollTimer = setTimeout(() => poll(experiment, generation), POLL_MS);
}

function idle() {
  applyBtn.disabled = false;
  applyStatus.textContent = "";
}

function renderResult(status) {
  applySummary.textContent = editSummary(status);
  changelogEl.textContent = status.changelog || "";
  applyResult.hidden = false;
}

async function poll(experiment, generation) {
  if (generation !== pollGeneration) return;
  applyBtn.disabled = true;
  let res;
  try {
    res = await fetch(experimentUrl(experiment, "edit-status"));
  } catch {
    if (generation === pollGeneration) later(experiment, generation);
    return;
  }
  if (generation !== pollGeneration) return;
  if (res.status === 404) {
    idle();
    setReports({}, false, confirmHandler(experiment));
    return;
  }
  const status = await res.json().catch(() => null);
  if (generation !== pollGeneration) return;
  if (!status) {
    later(experiment, generation);
    return;
  }
  const running = status.phase !== "completed" && status.phase !== "failed";
  setReports(reportsByEntry(status), running, confirmHandler(experiment));
  if (status.phase === "completed") {
    idle();
    renderResult(status);
    return;
  }
  if (status.phase === "failed") {
    idle();
    showError("Applying the updates failed: " + (status.error || "unknown error"));
    return;
  }
  applyStatus.textContent = describeEditPhase(status);
  later(experiment, generation);
}

function confirmHandler(experiment) {
  return (key) => confirmEdit(experiment, key);
}

async function confirmEdit(experiment, key) {
  clearError();
  try {
    const res = await fetch(experimentUrl(experiment, "edits/confirm"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys: [key] }),
    });
    if (!res.ok) {
      showError("Could not confirm: " + await errorDetail(res));
      return;
    }
    // the status shows the confirmation (as not yet applied) at once
    startPolling(experiment);
  } catch (err) {
    showError("Network error: " + err.message);
  }
}

// The card for a newly selected experiment with an uploaded sheet
// (summary from GET .../sheet-summary), or none.
export function showEdit(experiment, summary) {
  stopPolling();
  current = experiment;
  idle();
  applyResult.hidden = true;
  changelogEl.textContent = "";
  if (!experiment) {
    sheetSummaryEl.textContent = "";
    return;
  }
  sheetSummaryEl.textContent = "Sheet “" + summary.name + "”: "
    + summary.samples.length + " samples, " + summary.columns.length + " columns.";
  startPolling(experiment);
}

async function applyEdits() {
  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  applyBtn.disabled = true;
  applyResult.hidden = true;
  applyStatus.textContent = "Starting...";
  try {
    const res = await fetch(experimentUrl(experiment, "edit-async"), { method: "POST" });
    if (!res.ok) {
      showError("Could not apply the updates: " + await errorDetail(res));
      idle();
      return;
    }
    startPolling(experiment, 2000);
  } catch (err) {
    showError("Network error: " + err.message);
    idle();
  }
}

async function downloadFinal() {
  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  downloadFinalBtn.disabled = true;
  try {
    const res = await fetch(experimentUrl(experiment, "final-sheet"));
    if (!res.ok) {
      showError("Download failed: " + await errorDetail(res));
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = downloadFilename(
      res.headers.get("Content-Disposition"), experiment.name + ".xlsx");
    link.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    downloadFinalBtn.disabled = false;
  }
}

// null when the experiment did not start from a sheet; throws when
// SAND or NOMAD failed to answer
export async function fetchSheetSummary(experiment) {
  const res = await fetch(experimentUrl(experiment, "sheet-summary"));
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(await errorDetail(res));
  return res.json();
}

export function initEdit() {
  applyBtn.addEventListener("click", applyEdits);
  downloadFinalBtn.addEventListener("click", downloadFinal);
}
