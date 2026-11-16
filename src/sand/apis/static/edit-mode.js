// The Edits card: what the edit job did, Confirm all with its review,
// Undo last and Reset all, and the final sheet to download. The job
// runs by itself: it reads an update as soon as it is saved (or its
// words revised), and writes the confirmed edits a moment after a
// decision. One by one, edits are decided from the input tiles
// (inputs.js) through editActions, in turn; all at once in the review
// (review.js).
//
// The job's status file is polled, and selecting an experiment resumes
// an unfinished job's polling (reload-proof).

import { errorDetail, experimentUrl } from "./api.js";
import {
  checkedKeys, confirmAllLabel, decisionCount, describeEditPhase, downloadBlock, downloadFilename, editSummary,
  inputsToRead, pendingKeys, readKey, reportsByEntry, settledBlock, undoQuestion, unwrittenKey, writtenEdits,
} from "./edit-logic.js";
import { requireExperiment, showSheet } from "./experiments.js";
import { rejectEdit } from "./feedback.js";
import { setOverviewUpdates, showOverview } from "./overview.js";
import { setInputsListener, setReports } from "./inputs.js";
import { openReview, updateReview } from "./review.js";
import { clearError, confirmDialog, showError } from "./ui.js";

const readAgainBtn = document.getElementById("read-again-btn");
const undoLastBtn = document.getElementById("undo-last-btn");
const resetBtn = document.getElementById("reset-decisions-btn");
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

const POLL_MS = 3000;
// confirmations made one after the other go into one write
const WRITE_DELAY_MS = 2000;
let pollTimer = null;
let pollGeneration = 0;
let current = null; // the experiment the card shows
let downloading = false;
let editedReady = false; // the shown experiment's last job completed: an edited sheet exists
let confirming = null; // entry id of the experiment whose review confirmation is on its way

// what decides whether a job must start by itself: the inputs list as
// last loaded (any experiment: it can arrive before the card switches),
// the last status, and what the last job was started for (a job that
// failed is not started again for the same reason; Read again does)
let inputsSeen = null; // {entryId, items}
let statusKnown = false; // the shown experiment's status was fetched once
let running = false; // a job of the shown experiment runs, or is being started
let lastStatus = null; // the last completed status of the shown experiment
// the latest status, whatever its phase: what Undo last and Reset all
// take back (a failed job's status names the decisions too)
let decisionStatus = null;
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
  gateDownload(blockShown);
}

function note(text) {
  applyStatus.textContent = text;
  applyStatus.classList.remove("busy");
}

function busy(text) {
  applyStatus.textContent = text;
  applyStatus.classList.add("busy");
}

// the overview folds once the shown sheet has updates
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

// Confirm all opens the review of `keys`, the undecided edits; `wait`:
// why it cannot open now ("" when it can). `checked`: how many of them
// have a check.
function showPending(keys, checked = 0, wait = "") {
  // this experiment's own confirmation brings the button back itself
  if (current && confirming === current.entry_id) return;
  confirmAllBtn.hidden = keys.length === 0;
  confirmAllBtn.disabled = Boolean(wait);
  confirmAllBtn.title = wait;
  confirmAllLabelEl.textContent = confirmAllLabel(keys.length, checked) + "...";
}

// the shown experiment's inputs list, null before it arrived
function shownItems() {
  return current && inputsSeen && inputsSeen.entryId === current.entry_id ? inputsSeen.items : null;
}

// Confirm all, of a status: open once every update is read and nothing
// was decided since the last run
function showDecidable(status, jobRunning = false) {
  const all = status.inputs || [];
  const keys = pendingKeys(all);
  const marked = new Set(checkedKeys(all));
  const wait = jobRunning || status.phase !== "completed" ? "The updates are being read"
    : settledBlock(status, shownItems()) ? "Wait: the updates are read again" : "";
  showPending(keys, keys.filter((key) => marked.has(key)).length, wait);
}

// Undo last and Reset all: shown with a decision to take back, used
// between runs (the server refuses while a job reads them)
function showTakeBack(status, jobRunning) {
  const last = status ? status.last_decision || [] : [];
  undoLastBtn.hidden = last.length === 0;
  resetBtn.hidden = !status || (!decisionCount(status) && !last.length);
  undoLastBtn.disabled = resetBtn.disabled = jobRunning;
  undoLastBtn.title = resetBtn.title = jobRunning ? "The updates are being read" : "";
}

// the sheet to download is the last run's, once every edit is decided
// and written into it: not while a job runs
let blockShown = ""; // the shown experiment's gate, kept across a download

function gateDownload(block) {
  blockShown = block;
  downloadFinalBtn.disabled = !editedReady || Boolean(block) || downloading || running;
  downloadFinalBtn.title = block
    || (running ? "The updates are being read"
      : editedReady ? "" : "No edited sheet yet: the updates have not been read");
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
  gateDownload(downloadBlock(status, shownItems()));
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
    decisionStatus = null;
    reportsShown = {};
    showPending([]);
    showTakeBack(null, false);
    updateProgress();
    setReports({}, false, editActions(experiment));
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
  // while a job runs, the updates it has reported are decided already;
  // the others show the last run's reports
  const fresh = running ? new Set((status.inputs || []).map((report) => report.entry_id)) : null;
  setReports(reportsShown, running, editActions(experiment), fresh);
  // while a job runs, the button stays, closed, for the last run's edits
  showDecidable(running ? { ...status, inputs: Object.values(reportsShown) } : status, running);
  decisionStatus = status;
  showTakeBack(status, running);
  updateReview(experiment, status, shownItems(), running);
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
  gateDownload(blockShown);
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
  // a run that left nothing unwritten: the same decision made again later
  // is a new one
  if (!unwritten) wroteFor = null;
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
  // a revised or deleted update holds back the ones after it
  if (lastStatus && !running) {
    showDecidable(lastStatus);
    gateDownload(downloadBlock(lastStatus, items));
    updateReview(experiment, lastStatus, items, false);
  }
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

function editActions(experiment) {
  return {
    confirm: (key) => confirmEdit(experiment, key),
    reject: async (edit) => {
      const done = await rejectEdit(edit, (why) => postReject(experiment, edit.key, why));
      if (done && stillShown(experiment)) {
        // the updates below are read again: no review until then
        confirmAllBtn.disabled = true;
        startPolling(experiment);
      }
      return done;
    },
  };
}

// throws, for the dialog to show why
async function postReject(experiment, key, { reason, meant }) {
  const res = await fetch(experimentUrl(experiment, "edits/reject"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keys: [key], reason, meant }),
  });
  if (!res.ok) throw new Error(await errorDetail(res));
}

// "" once posted, else why not
async function post(experiment, path, body) {
  try {
    const res = await fetch(experimentUrl(experiment, path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.ok ? "" : await errorDetail(res);
  } catch (err) {
    return "Network error: " + err.message;
  }
}

// the review's rejections: the poll that follows shows the updates read
// again (settle starts the run)
async function reviewReject(experiment, keys) {
  const why = await post(experiment, "edits/reject", { keys, review: true });
  if (!why && stillShown(experiment)) {
    confirmAllBtn.disabled = true;
    startPolling(experiment);
  }
  return why;
}

// true when confirmed; false lets the tile offer the button again. The
// poll that follows shows the confirmation at once and (settle) starts
// the write a moment later.
async function confirmEdit(experiment, key) {
  clearError();
  const why = await post(experiment, "edits/confirm", { keys: [key] });
  if (why) showError("Could not confirm: " + why);
  else if (stillShown(experiment)) startPolling(experiment);
  return !why;
}

// the review's confirmation of every undecided edit, then the job that
// writes them; "" once confirmed, else why not
async function reviewConfirm(experiment, keys) {
  // no job may start on a snapshot without these confirmations: settle
  // waits too (startJob or the poll gives the button back), and a write
  // already due goes with this one
  confirmAllBtn.disabled = true;
  confirming = experiment.entry_id;
  if (writeTimer && writeTimer.experiment.entry_id === experiment.entry_id) {
    clearTimeout(writeTimer.timer);
    writeTimer = null;
  }
  let why;
  try {
    why = await post(experiment, "edits/confirm", { keys, review: true });
  } finally {
    confirming = null;
  }
  if (why) {
    // the server's view decides what is still pending (another tab may
    // have run a job)
    if (stillShown(experiment)) startPolling(experiment);
    return why;
  }
  // the write was asked for: it starts even if the user moved on
  if (stillShown(experiment)) confirmAllBtn.hidden = true;
  startJob(experiment);
  return "";
}

function openConfirmAll(experiment) {
  if (!lastStatus || confirmAllBtn.disabled) return;
  clearError();
  openReview(experiment, lastStatus, shownItems(), {
    reject: (keys) => reviewReject(experiment, keys),
    confirm: (keys) => reviewConfirm(experiment, keys),
    readAgain: () => {
      if (stillShown(experiment)) readAgain();
    },
  });
}

// the last click's decisions, once the user agreed
async function undoLast(experiment) {
  const status = decisionStatus;
  const question = status && undoQuestion(status);
  if (!question || !await confirmDialog(question, "Undo")) return;
  clearError();
  const keys = status.last_decision.map((decision) => decision.key);
  const why = await post(experiment, "edits/undo-last", { keys });
  if (why) showError("Could not undo: " + why);
  if (stillShown(experiment)) startPolling(experiment);
}

async function resetDecisions(experiment) {
  const n = decisionStatus ? decisionCount(decisionStatus) : 0;
  const what = n === 1 ? "the 1 decision" : n ? "all " + n + " decisions" : "every decision";
  const question = "Reset " + what + "? Every edit waits for your decision again, as if none was made.";
  if (!await confirmDialog(question, "Reset")) return;
  clearError();
  const why = await post(experiment, "edits/reset", {});
  if (why) showError("Could not reset: " + why);
  if (stillShown(experiment)) startPolling(experiment);
}

// The card for a newly selected experiment with an uploaded sheet
// (`view` from GET .../sheet-view), or none.
export function showEdit(experiment, view) {
  stopPolling();
  flushWrite();
  current = experiment;
  statusKnown = false;
  lastStatus = null;
  decisionStatus = null;
  headerFor = view ? view.sha256 : null;
  editedReady = false;
  reportsShown = {};
  readFor = null;
  wroteFor = null;
  idle();
  showPending([]);
  showTakeBack(null, false);
  updateProgress();
  gateDownload("");
  readAgainBtn.hidden = true;
  applyResult.hidden = true;
  changelogEl.textContent = "";
  changelogFold.open = false; // the fold is per result, not per page
  if (!experiment || !view) {
    // nothing to poll: the tiles forget the last job's reports here
    setReports({}, false, null);
  }
  // made before sheets, or by the other tool of this page's past: the
  // header says so, nothing to poll
  if (!experiment || !view) return;
  startPolling(experiment);
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
    gateDownload(blockShown);
    // no confirming on the way out: the job's reservation refuses it
    setReports(reportsShown, true, editActions(experiment));
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
  gateDownload(blockShown);
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
    gateDownload(blockShown);
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
    if (current) openConfirmAll(current);
  });
  undoLastBtn.addEventListener("click", () => {
    if (current) undoLast(current);
  });
  resetBtn.addEventListener("click", () => {
    if (current) resetDecisions(current);
  });
  downloadFinalBtn.addEventListener("click", () => downloadSheet("final-sheet", ".xlsx"));
  downloadOriginalBtn.addEventListener("click", () => downloadSheet("original-sheet", " (original).xlsx"));
}
