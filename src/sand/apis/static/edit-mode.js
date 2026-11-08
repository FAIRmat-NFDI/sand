// The Sheet card: what the edit job did, the confirm-all button, and
// the final sheet to download. The job runs by itself: it reads an
// update as soon as it is saved (or its words revised), and writes the
// confirmed edits a moment after a confirmation. Confirmations come
// from the input tiles (inputs.js) through confirmEdit, or all at once
// from here.
//
// The job's status file is polled, and selecting an experiment resumes
// an unfinished job's polling (reload-proof).

import { errorDetail, experimentUrl } from "./api.js";
import {
  confirmAllLabel, describeEditPhase, downloadFilename, editSummary, inputsToRead, pendingKeys,
  readKey, reportsByEntry, unwrittenCount, unwrittenKey, writtenEdits,
} from "./edit-logic.js";
import { requireExperiment, showSheet } from "./experiments.js";
import { setOverviewUpdates, showOverview } from "./overview.js";
import { setInputsListener, setReports } from "./inputs.js";
import { clearError, showError } from "./ui.js";

const readAgainBtn = document.getElementById("read-again-btn");
const confirmAllBtn = document.getElementById("confirm-all-btn");
const confirmAllLabelEl = document.getElementById("confirm-all-label");
const applyStatus = document.getElementById("apply-status");
const applyResult = document.getElementById("apply-result");
const applySummary = document.getElementById("apply-summary");
const changelogEl = document.getElementById("changelog-text");
const changelogFold = document.querySelector(".changelog-details");
const writtenTable = document.getElementById("written-table");
const writtenBody = writtenTable.querySelector("tbody");
const downloadFinalBtn = document.getElementById("download-final-btn");
const downloadOriginalBtn = document.getElementById("download-original-btn");
const sheetSummaryEl = document.getElementById("sheet-summary");

const POLL_MS = 3000;
// confirmations made one after the other go into one write
const WRITE_DELAY_MS = 2000;
let pollTimer = null;
let pollGeneration = 0;
let current = null; // the experiment the card shows
let downloading = false;
let editedReady = false; // the shown experiment's last job completed: an edited sheet exists
let pending = []; // keys of the last completed job's unconfirmed edits
let confirming = null; // entry id of the experiment whose confirm-all is on its way

// what decides whether a job must start by itself: the inputs list as
// last loaded (any experiment: it can arrive before the card switches),
// the last status, and what the last job was started for (a job that
// failed is not started again for the same reason; Read again does)
let inputsSeen = null; // {entryId, items}
let statusKnown = false; // the shown experiment's status was fetched once
let running = false; // a job of the shown experiment runs, or is being started
let lastStatus = null; // the last completed status of the shown experiment
// the sheet the header was last read for (the view's sha at selection,
// then each completed job's final); null while a failed re-read waits
// for its retry
let headerFor = null;
let reportsShown = {}; // the last status's reports, by entry id
let readFor = null; // readKey of the inputs the last job was started for
let wroteFor = null; // unwrittenKey the last write was started for
let writeTimer = null; // {timer, experiment}: the write about to start
// a job found failed before the inputs list arrived: its readKey is the
// list's, whatever it turns out to be (no restart on a reload)
const FAILED_BEFORE_INPUTS = Symbol("failed");

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

// the write waits for no one: leaving the experiment starts it now
function flushWrite() {
  if (!writeTimer) return;
  clearTimeout(writeTimer.timer);
  const { experiment } = writeTimer;
  writeTimer = null;
  startJob(experiment);
}

function idle() {
  running = false;
  applyStatus.textContent = "";
  applyStatus.classList.remove("busy");
  gateDownload(unwrittenShown);
}

function note(text) {
  applyStatus.textContent = text;
  applyStatus.classList.remove("busy");
}

function busy(text) {
  applyStatus.textContent = text;
  applyStatus.classList.add("busy");
}

// the header's line: the updates of the shown sheet and what became of them
function updateProgress() {
  if (!current) {
    setOverviewUpdates(null);
    return;
  }
  const updates = inputsSeen && inputsSeen.entryId === current.entry_id
    ? inputsSeen.items.length
    : null;
  setOverviewUpdates(updates);
}

function showPending(keys) {
  pending = keys;
  // this experiment's own confirm-all brings the button back itself
  if (current && confirming === current.entry_id) return;
  confirmAllBtn.hidden = keys.length === 0;
  confirmAllBtn.disabled = false;
  confirmAllLabelEl.textContent = confirmAllLabel(keys.length);
}

// the sheet to download is the last run's: not while a job runs, not
// before confirmed edits are written into it
let unwrittenShown = 0; // the shown experiment's gate, kept across a download

function gateDownload(unwritten) {
  unwrittenShown = unwritten;
  downloadFinalBtn.disabled = !editedReady || unwritten > 0 || downloading || running;
  downloadFinalBtn.title = unwritten > 0
    ? unwritten + " confirmed edit" + (unwritten === 1 ? " is" : "s are") + " being written into the sheet"
    : running ? "The updates are being read"
      : editedReady ? "" : "No edited sheet yet: the updates have not been read";
  // the original is there whatever the job does
  downloadOriginalBtn.disabled = downloading;
}

// what is in the sheet now, a table to glance at: the step once per
// group, then column, value and samples per edit
function renderWritten(rows) {
  writtenBody.replaceChildren();
  writtenTable.hidden = rows.length === 0;
  for (const row of rows) {
    const tr = document.createElement("tr");
    if (row.first) tr.className = "written-first";
    if (row.line) {
      const td = document.createElement("td");
      td.colSpan = 4;
      td.textContent = row.line;
      tr.append(td);
    } else {
      for (const [field, text] of [
        ["step", row.first ? row.step : ""], ["column", row.column], ["value", row.value], ["samples", row.samples],
      ]) {
        const td = document.createElement("td");
        td.className = "written-" + field;
        td.textContent = text;
        tr.append(td);
      }
    }
    writtenBody.append(tr);
  }
}

function renderResult(status) {
  gateDownload(unwrittenCount(status));
  applySummary.textContent = editSummary(status);
  renderWritten(writtenEdits(status));
  // the full log is for the record, behind a fold
  changelogEl.textContent = status.changelog || "";
  applyResult.hidden = false;
}

async function poll(experiment, generation) {
  if (generation !== pollGeneration) return;
  let res;
  try {
    res = await fetch(experimentUrl(experiment, "edit-status"));
  } catch {
    if (generation === pollGeneration) later(experiment, generation);
    return;
  }
  if (generation !== pollGeneration) return;
  if (!res.ok && res.status !== 404) {
    // a server hiccup: keep asking; anything else (the login ended) is
    // told and polling stops
    if (res.status >= 500) {
      later(experiment, generation);
      return;
    }
    showError("Could not read the job's status: " + await errorDetail(res));
    if (generation === pollGeneration) idle();
    return;
  }
  statusKnown = true;
  if (res.status === 404) {
    editedReady = false;
    idle();
    lastStatus = null;
    reportsShown = {};
    showPending([]);
    updateProgress();
    setReports({}, false, confirmHandler(experiment));
    settle(experiment, null);
    return;
  }
  const status = await res.json().catch(() => null);
  if (generation !== pollGeneration) return;
  if (!status) {
    later(experiment, generation);
    return;
  }
  running = status.phase !== "completed" && status.phase !== "failed";
  // while the job runs, the last run's reports stay under the inputs it
  // has not reached yet (its own replace them one by one)
  reportsShown = running
    ? { ...reportsShown, ...reportsByEntry(status) }
    : reportsByEntry(status);
  setReports(reportsShown, running, confirmHandler(experiment));
  showPending(status.phase === "completed" ? pendingKeys(status.inputs || []) : []);
  if (status.phase === "completed") {
    editedReady = true;
    idle();
    lastStatus = status;
    renderResult(status);
    updateProgress();
    // the job wrote a sheet the header has not been read for yet
    if (status.final && status.final !== headerFor) refreshSheet(experiment, status.final);
    settle(experiment, status);
    return;
  }
  if (status.phase === "failed") {
    // the download refuses after a failed job, whatever an earlier one wrote
    editedReady = false;
    idle();
    // not started again for these inputs by itself (Read again does)
    readFor = inputsSeen && inputsSeen.entryId === experiment.entry_id
      ? readKey(inputsSeen.items)
      : FAILED_BEFORE_INPUTS;
    showError("Reading the updates failed: " + (status.error || "unknown error"));
    readAgainBtn.hidden = false;
    return;
  }
  readAgainBtn.hidden = true;
  busy(describeEditPhase(status));
  gateDownload(unwrittenShown);
  later(experiment, generation);
}

// After a status settled: write what was confirmed since the last run
// (a moment later, so a row of confirmations makes one write), else
// read what no job has read in these words. Nothing twice for the same
// reason: a job that failed stays failed until Read again.
function settle(experiment, status) {
  if (!stillShown(experiment) || running || confirming === experiment.entry_id) return;
  if (inputsSeen && inputsSeen.entryId === experiment.entry_id) waitingNote(inputsSeen.items);
  const unwritten = status ? unwrittenKey(status) : "";
  if (unwritten) {
    if (unwritten === wroteFor || writeTimer) return;
    const timer = setTimeout(() => {
      writeTimer = null;
      if (stillShown(experiment) && !running) startJob(experiment);
    }, WRITE_DELAY_MS);
    writeTimer = { timer, experiment };
    return;
  }
  if (!inputsSeen || inputsSeen.entryId !== experiment.entry_id) return;
  const toRead = inputsToRead(inputsSeen.items, reportsShown);
  if (!toRead.length || readKey(inputsSeen.items) === readFor) return;
  startJob(experiment);
}

// the inputs list as just loaded (inputs.js): a new or revised update
// is read without a click
function inputsLoaded(experiment, items) {
  inputsSeen = { entryId: experiment.entry_id, items };
  if (!stillShown(experiment)) return;
  updateProgress();
  if (readFor === FAILED_BEFORE_INPUTS) readFor = readKey(items);
  if (statusKnown) settle(experiment, lastStatus);
}

// no job starts while an input has no text (the server refuses): the
// card says what it waits for, and when waiting is no use
function waitingNote(items) {
  if (items.some((item) => !item.text && item.status === "FAILED")) {
    note("A recording could not be transcribed: delete it, then the updates are read.");
  } else if (items.some((item) => !item.text)) {
    busy("Waiting for the transcript...");
  } else if (!running) {
    note("");
  }
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

// true when confirmed; false lets the tile offer the button again. The
// poll that follows shows the confirmation at once and (settle) starts
// the write a moment later.
async function confirmEdit(experiment, key) {
  clearError();
  const ok = await postConfirm(experiment, [key]);
  if (ok && stillShown(experiment)) startPolling(experiment);
  return ok;
}

// every pending edit of the last job, then the job that writes them
async function confirmAll(experiment) {
  clearError();
  // no job may start on a snapshot without these confirmations: settle
  // waits too (startJob or the poll gives the button back), and a write
  // already due goes with this one
  confirmAllBtn.disabled = true;
  confirming = experiment.entry_id;
  if (writeTimer && writeTimer.experiment.entry_id === experiment.entry_id) {
    clearTimeout(writeTimer.timer);
    writeTimer = null;
  }
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
// (`view` from GET .../sheet-view), or none.
export function showEdit(experiment, view) {
  stopPolling();
  flushWrite();
  current = experiment;
  statusKnown = false;
  lastStatus = null;
  headerFor = view ? view.sha256 : null;
  editedReady = false;
  reportsShown = {};
  readFor = null;
  wroteFor = null;
  idle();
  showPending([]);
  updateProgress();
  gateDownload(0);
  readAgainBtn.hidden = true;
  applyResult.hidden = true;
  changelogEl.textContent = "";
  changelogFold.open = false; // the fold is per result, not per page
  if (!experiment || !view) {
    // nothing to poll: the tiles forget the last job's reports here
    setReports({}, false, null);
  }
  if (!experiment) {
    sheetSummaryEl.textContent = "";
    return;
  }
  if (!view) {
    // made before sheets, or by the other tool of this page's past:
    // the header says so
    sheetSummaryEl.textContent = "";
    return;
  }
  describeSheet(view);
  startPolling(experiment);
}

// the header names the sheet; this line says which version the card is about
function describeSheet(view) {
  sheetSummaryEl.textContent = view.source === "edited"
    ? "The sheet with the confirmed edits."
    : "The sheet as uploaded.";
}

// the header follows the sheet the last job left (`final`: its sha);
// a failed read is tried again a poll's interval later, while this
// experiment is shown and nothing has read the header since
async function refreshSheet(experiment, final) {
  headerFor = final;
  let view;
  try {
    view = await fetchSheetView(experiment);
  } catch {
    if (headerFor !== final) return;
    headerFor = null;
    setTimeout(() => {
      if (stillShown(experiment) && headerFor === null) refreshSheet(experiment, final);
    }, POLL_MS);
    return;
  }
  if (!view || !stillShown(experiment)) return;
  showSheet(experiment, view);
  showOverview(view);
  describeSheet(view);
}

// the fallback after a failed job: the same inputs, read once more
function readAgain() {
  clearError();
  if (!current) return;
  readFor = null;
  wroteFor = null;
  readAgainBtn.hidden = true;
  startJob(current);
}

// posts the start whatever is shown; the card is touched only while
// it still shows this experiment
async function startJob(experiment) {
  if (stillShown(experiment)) {
    running = true;
    if (inputsSeen && inputsSeen.entryId === experiment.entry_id) readFor = readKey(inputsSeen.items);
    wroteFor = lastStatus ? unwrittenKey(lastStatus) : null;
    confirmAllBtn.hidden = true;
    readAgainBtn.hidden = true;
    applyResult.hidden = true;
    changelogFold.open = false;
    busy("Starting...");
    gateDownload(unwrittenShown);
    // no confirming on the way out: the job's reservation refuses it
    setReports(reportsShown, true, confirmHandler(experiment));
  }
  try {
    const res = await fetch(experimentUrl(experiment, "edit-async"), { method: "POST" });
    if (!res.ok) {
      // 409: a job runs already (another tab), its progress is shown;
      // 422: an input is not ready (a recording being transcribed), the
      // next inputs list starts the job
      if (res.status !== 409 && res.status !== 422) {
        const detail = await errorDetail(res);
        if (stillShown(experiment)) {
          showError("Could not read the updates: " + detail);
          readAgainBtn.hidden = false;
        }
      }
      // a poll puts the card back as the server sees it (pending edits,
      // confirmations already made)
      if (stillShown(experiment)) startPolling(experiment);
      return;
    }
    // the reservation is written when the POST returns: polled at once
    if (stillShown(experiment)) startPolling(experiment);
  } catch (err) {
    if (stillShown(experiment)) {
      showError("Network error: " + err.message);
      readAgainBtn.hidden = false;
      startPolling(experiment);
    }
  }
}

// `path` is the sheet's endpoint; the server names the file, `suffix`
// completes the experiment's name when it does not
async function downloadSheet(path, suffix) {
  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  downloading = true;
  gateDownload(unwrittenShown);
  try {
    const res = await fetch(experimentUrl(experiment, path));
    if (!res.ok) {
      showError("Download failed: " + await errorDetail(res));
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = downloadFilename(
      res.headers.get("Content-Disposition"), experiment.name + suffix);
    link.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    // the button follows the gate of whatever the card shows now
    downloading = false;
    gateDownload(unwrittenShown);
  }
}

// null when the experiment did not start from a sheet; throws when
// SAND or NOMAD failed to answer
export async function fetchSheetView(experiment) {
  const res = await fetch(experimentUrl(experiment, "sheet-view"));
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(await errorDetail(res));
  return res.json();
}

export function initEdit() {
  setInputsListener(inputsLoaded);
  readAgainBtn.addEventListener("click", readAgain);
  // the card's experiment, not the selection: they differ while a newly
  // selected experiment's summary is loading
  confirmAllBtn.addEventListener("click", () => {
    if (current) confirmAll(current);
  });
  downloadFinalBtn.addEventListener("click", () => downloadSheet("final-sheet", ".xlsx"));
  downloadOriginalBtn.addEventListener("click", () => downloadSheet("original-sheet", " (original).xlsx"));
}
