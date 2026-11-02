// The Apply card of edit mode: run the edit job on the experiment's
// updates, show its progress, the changelog, and the final sheet to
// download. Confirmations are sent from the input tiles (inputs.js)
// through confirmEdit; applying again writes them into the sheet.
//
// Like the extraction: the job's status file is polled, and selecting
// an experiment resumes an unfinished job's polling (reload-proof).

import { errorDetail, experimentUrl } from "./api.js";
import {
  confirmAllLabel, describeEditPhase, downloadFilename, editSummary, pendingKeys, reportsByEntry,
} from "./edit-logic.js";
import { requireExperiment } from "./experiments.js";
import { setReports } from "./inputs.js";
import { clearError, showError } from "./ui.js";

const applyBtn = document.getElementById("apply-btn");
const confirmAllBtn = document.getElementById("confirm-all-btn");
const confirmAllLabelEl = document.getElementById("confirm-all-label");
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
let pending = []; // keys of the last completed job's unconfirmed edits
let confirming = null; // entry id of the experiment whose confirm-all is on its way

// a response for an experiment no longer shown must not touch the card
function stillShown(experiment) {
  return current !== null && current.entry_id === experiment.entry_id;
}

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

function showPending(keys) {
  pending = keys;
  // this experiment's own confirm-all brings the button back itself
  if (current && confirming === current.entry_id) return;
  confirmAllBtn.hidden = keys.length === 0;
  confirmAllBtn.disabled = false;
  confirmAllLabelEl.textContent = confirmAllLabel(keys.length);
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
    showPending([]);
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
  showPending(status.phase === "completed" ? pendingKeys(status.inputs || []) : []);
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

// true when confirmed
async function postConfirm(experiment, keys) {
  try {
    const res = await fetch(experimentUrl(experiment, "edits/confirm"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys }),
    });
    if (res.ok) return true;
    showError("Could not confirm: " + await errorDetail(res));
  } catch (err) {
    showError("Network error: " + err.message);
  }
  return false;
}

// true when confirmed; false lets the tile offer the button again
async function confirmEdit(experiment, key) {
  clearError();
  const ok = await postConfirm(experiment, [key]);
  // the status shows the confirmation (as not written yet) at once
  if (ok && stillShown(experiment)) startPolling(experiment);
  return ok;
}

// every pending edit of the last job, then the job that writes them
async function confirmAll(experiment) {
  clearError();
  // no job may start on a snapshot without these confirmations: Apply
  // waits too (startJob or the poll gives it back)
  confirmAllBtn.disabled = true;
  applyBtn.disabled = true;
  confirming = experiment.entry_id;
  let ok;
  try {
    ok = await postConfirm(experiment, pending);
  } finally {
    confirming = null;
  }
  if (!ok) {
    // the server's view decides what is still pending (another tab may
    // have run a job)
    if (stillShown(experiment)) startPolling(experiment);
    return;
  }
  // the write was asked for: it starts even if the user moved on
  if (stillShown(experiment)) confirmAllBtn.hidden = true;
  await startJob(experiment);
}

// The card for a newly selected experiment with an uploaded sheet
// (summary from GET .../sheet-summary), or none.
export function showEdit(experiment, summary) {
  stopPolling();
  current = experiment;
  idle();
  showPending([]);
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
  if (experiment) await startJob(experiment);
}

// posts the start whatever is shown; the card is touched only while
// it still shows this experiment
async function startJob(experiment) {
  if (stillShown(experiment)) {
    applyBtn.disabled = true;
    confirmAllBtn.hidden = true;
    applyResult.hidden = true;
    applyStatus.textContent = "Starting...";
  }
  try {
    const res = await fetch(experimentUrl(experiment, "edit-async"), { method: "POST" });
    if (!res.ok) {
      showError("Could not apply the updates: " + await errorDetail(res));
      // a poll puts the card back as the server sees it (pending edits,
      // confirmations already made)
      if (stillShown(experiment)) startPolling(experiment);
      return;
    }
    if (stillShown(experiment)) startPolling(experiment, 2000);
  } catch (err) {
    showError("Network error: " + err.message);
    if (stillShown(experiment)) startPolling(experiment);
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
  // the card's experiment, not the selection: they differ while a newly
  // selected experiment's summary is loading
  confirmAllBtn.addEventListener("click", () => {
    if (current) confirmAll(current);
  });
  downloadFinalBtn.addEventListener("click", downloadFinal);
}
