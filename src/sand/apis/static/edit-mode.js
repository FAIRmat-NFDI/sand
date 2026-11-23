// The Edits card: what the edit job did, Recompute, Confirm all and
// Reset all, and the final sheet to download. Deciding goes in rounds
// (#208): the ticks of the input tiles (inputs.js, round.js), revised,
// deleted and new updates start a recompute by themselves, about 2 s
// after the last change: it sends the round and reads the updates again,
// once. Confirm all confirms every ticked edit of a computed state and
// writes the sheet.
//
// The job's status file is polled, and selecting an experiment resumes
// an unfinished job's polling (reload-proof).

import { errorDetail, experimentUrl, postJson } from "./api.js";
import {
  changedEntries, checkedKeys, confirmAllLabel, confirmQuestion, decisionCount, describeEditPhase, downloadBlock,
  downloadFilename, editSummary, failedReadKey, inputsToRead, lostUnticks, pendingKeys, readKey, recomputeDue, recomputeLabel,
  reportsByEntry,
  freshReports, roundSnapshot, settledBlock, unchangedAbove, unwrittenKey, withRound, writtenEdits,
} from "./edit-logic.js";
import { requireExperiment, showSheet } from "./experiments.js";
import { setOverviewUpdates, showOverview } from "./overview.js";
import { reloadInputs, setInputsListener, setReports } from "./inputs.js";
import {
  anyRound, clearRound, clearSent, onRoundChange, roundBody, roundEntries, roundFor, roundSize, sentUnticks,
} from "./round.js";
import { clearError, confirmDialog, showError } from "./ui.js";

const readAgainBtn = document.getElementById("read-again-btn");
const recomputeBtn = document.getElementById("recompute-btn");
const recomputeLabelEl = document.getElementById("recompute-label");
const resetBtn = document.getElementById("reset-decisions-btn");
const confirmAllBtn = document.getElementById("confirm-all-btn");
const confirmAllLabelEl = document.getElementById("confirm-all-label");
const applyStatus = document.getElementById("apply-status");
const applyResult = document.getElementById("apply-result");
const applySummary = document.getElementById("apply-summary");
const writtenTable = document.getElementById("written-table");
const writtenBody = writtenTable.querySelector("tbody");
const downloadFinalBtn = document.getElementById("download-final-btn");
const downloadOriginalBtn = document.getElementById("download-original-btn");

const POLL_MS = 3000;
// changes made one after the other (updates said, unticks) go into one job
const SETTLE_MS = 2000;
let pollTimer = null;
let pollGeneration = 0;
let current = null; // the experiment the card shows
let downloading = false;
let editedReady = false; // the shown experiment's last job completed: an edited sheet exists
let confirming = null; // entry id of the experiment whose Confirm all is on its way

// what decides whether a job must start by itself: the inputs list as
// last loaded (any experiment: it can arrive before the card switches),
// the last status, and what the last job was started for (a job that
// failed is not started again for the same reason; Read again does)
let inputsSeen = null; // {entryId, items}
let statusKnown = false; // the shown experiment's status was fetched once
let running = false; // a job of the shown experiment runs, or is being started
let sendingRound = false; // a round is on its way
let lastStatus = null; // the last completed status of the shown experiment
// the latest status, whatever its phase: what Reset all takes back (a
// failed job's status names the decisions too)
let decisionStatus = null;
// the sheet the header was last read for (the view's sha at selection,
// then each completed job's final); null while a failed re-read waits
// for its retry
let headerFor = null;
let reportsShown = {}; // the last status's reports, by entry id
let readFor = null; // readKey of the inputs the last job was started for
let wroteFor = null; // unwrittenKey the last write was started for
// {timer, experiment, what, writes}: the job about to start by itself,
// "write" or "recompute"; `writes`: decisions wait to be written either way
let dueTimer = null;
let roundFailedFor = null; // the round body (JSON) whose send failed
// a job found failed before the inputs list arrived: its readKey is the
// list's, whatever it turns out to be (no restart on a reload)
const FAILED_BEFORE_INPUTS = Symbol("failed");
// what the last Recompute is compared with once read again, and what it
// changed (inputs.js marks): {entryId, before (roundSnapshot), sent}
let roundSent = null;
// the inputs above the first change, when the job started from this page:
// their reports stay shown while it runs (#215)
// `start`: which start made it, so a late answer to an older start (of
// another experiment, shown before) leaves it alone; `job`: the workflow
// id that start answered with
const NO_KEPT = { entryId: null, start: 0, notes: new Set(), job: null };
let kept = NO_KEPT;
let starts = 0;
const NO_MARKS = { changed: new Set(), lost: {} };
let marks = NO_MARKS;

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

function cancelDue(experiment = null) {
  if (!dueTimer || (experiment && dueTimer.experiment.entry_id !== experiment.entry_id)) return;
  clearTimeout(dueTimer.timer);
  dueTimer = null;
}

// The job a change starts by itself: a recompute for a round or updates
// no job read (it writes the decisions too), else a write of decisions
// made and not written (a Confirm all whose job did not start: a
// reload). Null for nothing new: a failed job or round is not started
// again for the same reason (Read again does).
function dueJob(experiment) {
  if (!stillShown(experiment) || running || sendingRound || confirming === experiment.entry_id) return null;
  const unwritten = lastStatus ? unwrittenKey(lastStatus) : "";
  // a run that left nothing unwritten: the same decision made again later
  // is a new one
  if (!unwritten) wroteFor = null;
  const writes = Boolean(unwritten) && unwritten !== wroteFor;
  const items = itemsOf(experiment);
  const round = roundSize() ? JSON.stringify(roundBody()) : "";
  if (items && recomputeDue(changesToApply(), readKey(items), round, { read: readFor, round: roundFailedFor })) {
    return { what: "recompute", writes };
  }
  return writes ? { what: "write", writes } : null;
}

// each change waits SETTLE_MS again, so a proposal shows while one is
// still speaking; a change during a job waits for it: its end settles again
function startSoon(experiment) {
  cancelDue();
  const due = dueJob(experiment);
  if (!due) return;
  const timer = setTimeout(() => {
    dueTimer = null;
    if (!stillShown(experiment) || running || confirming === experiment.entry_id) return;
    if (due.what === "write") startJob(experiment);
    else recompute(experiment);
  }, SETTLE_MS);
  dueTimer = { timer, experiment, ...due };
}

// leaving the experiment: decisions are written now, a recompute waits
// until it is shown again (its status settles then)
function leaveDue() {
  if (!dueTimer) return;
  const { experiment, writes } = dueTimer;
  cancelDue();
  if (writes) startJob(experiment);
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
  const items = itemsOf(current);
  setOverviewUpdates(items ? items.length : null);
}

// the round's changes and the updates no job read in these words: what
// Recompute sends and reads
function changesToApply() {
  const items = shownItems();
  return roundSize() + (items ? inputsToRead(items, reportsShown).length : 0);
}

// Recompute while there are changes, else Confirm all of `status`'s
// ticked edits once every update is read; `jobRunning`: neither now
function showActions(status, jobRunning = false) {
  // this experiment's own confirmation brings the buttons back itself
  if (current && confirming === current.entry_id) return;
  const changes = changesToApply();
  const items = shownItems();
  const transcribing = items && items.some((item) => !item.text);
  recomputeBtn.hidden = changes === 0 && !(transcribing && roundSize());
  recomputeBtn.disabled = jobRunning || Boolean(transcribing);
  recomputeBtn.title = jobRunning ? "The updates are being read"
    : transcribing ? "Wait: a recording is being transcribed" : "";
  recomputeLabelEl.textContent = recomputeLabel(Math.max(changes, roundSize()));
  const all = status ? status.inputs || [] : [];
  const keys = changes ? [] : pendingKeys(all);
  const marked = new Set(checkedKeys(all));
  confirmAllBtn.hidden = keys.length === 0;
  const wait = jobRunning || !status || status.phase !== "completed" ? "The updates are being read"
    : settledBlock(status, items) ? "Wait: the updates are read again" : "";
  confirmAllBtn.disabled = Boolean(wait);
  confirmAllBtn.title = wait;
  confirmAllLabelEl.textContent = confirmAllLabel(keys.length, keys.filter((key) => marked.has(key)).length) + "...";
}

// the experiment's inputs list, null before it arrived
function itemsOf(experiment) {
  return experiment && inputsSeen && inputsSeen.entryId === experiment.entry_id ? inputsSeen.items : null;
}

function shownItems() {
  return itemsOf(current);
}

// Reset all: shown with a decision to take back, used between runs (the
// server refuses while a job reads them)
function showTakeBack(status, jobRunning) {
  resetBtn.hidden = !status || !decisionCount(status);
  resetBtn.disabled = jobRunning;
  resetBtn.title = jobRunning ? "The updates are being read" : "";
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
    for (const [field, text] of [
      ["step", row.first ? row.step : ""], ["column", row.column], ["old", row.old ?? ""],
      ["value", row.value], ["samples", row.samples],
    ]) {
      const td = document.createElement("td");
      td.className = "written-" + field;
      td.textContent = text;
      tr.append(td);
    }
    writtenBody.append(tr);
  }
}

function renderResult(status) {
  gateDownload(downloadBlock(status, shownItems()));
  applySummary.textContent = editSummary(status);
  renderWritten(writtenEdits(status));
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
    showTakeBack(null, false);
    updateProgress();
    setReports({}, false, true, null, marks);
    showActions(null);
    settle(experiment);
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
  const own = kept.entryId === experiment.entry_id ? kept : NO_KEPT;
  const job = freshReports(status, running, { kept: own.notes, job: own.job });
  // a late poll of another experiment leaves the shown one's alone
  if (own === kept && !job.kept.size) kept = NO_KEPT;
  const fresh = job.fresh;
  if (status.phase === "completed" && roundSent && roundSent.entryId === experiment.entry_id) {
    // read again after a Recompute: what the round changed
    marks = {
      changed: changedEntries(roundSent.before, roundSnapshot(status)),
      lost: lostUnticks(roundSent.sent, status),
    };
    roundSent = null;
  }
  setReports(reportsShown, running, true, fresh, marks, status.asking ?? null);
  decisionStatus = status;
  // while a job runs, the buttons stay, closed, for the last run's edits
  showActions(running ? { ...status, inputs: Object.values(reportsShown) } : status, running);
  showTakeBack(status, running);
  if (status.phase === "completed") {
    editedReady = true;
    idle();
    lastStatus = status;
    renderResult(status);
    updateProgress();
    // the job wrote a sheet the header has not been read for yet
    if (status.final && status.final !== headerFor) refreshSheet(experiment, status.final);
    settle(experiment);
    return;
  }
  if (status.phase === "failed") {
    // the download refuses after a failed job, whatever an earlier one wrote
    editedReady = false;
    idle();
    // not started again for these inputs by itself (Read again does)
    readFor = failedReadKey(readFor, itemsOf(experiment)) || FAILED_BEFORE_INPUTS;
    showError("Reading the updates failed: " + (status.error || "unknown error"));
    readAgainBtn.hidden = false;
    // an update added while it ran, already listed: recomputed all the same
    startSoon(experiment);
    return;
  }
  readAgainBtn.hidden = true;
  busy(describeEditPhase(status));
  gateDownload(blockShown);
  later(experiment, generation);
}

// after a status settled, or the inputs arrived: what waits, and the
// job that is due
function settle(experiment) {
  if (!stillShown(experiment) || running || confirming === experiment.entry_id) return;
  const items = itemsOf(experiment);
  if (items) waitingNote(items);
  startSoon(experiment);
}

// the inputs list as just loaded (inputs.js): a new or revised update
// is one more change for Recompute
function inputsLoaded(experiment, items) {
  inputsSeen = { entryId: experiment.entry_id, items };
  if (!stillShown(experiment)) return;
  updateProgress();
  if (readFor === FAILED_BEFORE_INPUTS) readFor = readKey(items);
  // a revised or deleted update holds back the ones after it
  if (!running) showActions(lastStatus);
  if (lastStatus && !running) gateDownload(downloadBlock(lastStatus, items));
  if (statusKnown) settle(experiment);
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

function post(experiment, path, body) {
  return postJson(experimentUrl(experiment, path), body);
}

// Confirm all's confirmation of every ticked edit, then the job that
// writes them; "" once confirmed, else why not
async function confirmEdits(experiment, keys) {
  // no job may start on a snapshot without these confirmations: settle
  // waits too (startJob or the poll gives the button back), and a write
  // already due goes with this one
  confirmAllBtn.disabled = true;
  confirming = experiment.entry_id;
  cancelDue(experiment);
  let why;
  try {
    why = await post(experiment, "edits/confirm", { keys });
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
  // every report changes, and the last status predates the confirmations
  startJob(experiment, null);
  return "";
}

// the round, then the job that reads the updates again: once
async function recompute(experiment) {
  if (running || sendingRound || recomputeBtn.disabled) return;
  clearError();
  const round = roundSize() ? roundBody() : null;
  const before = lastStatus ? roundSnapshot(withRound(lastStatus, round)) : null;
  const sent = sentUnticks();
  const decided = roundEntries();
  if (round) {
    // ticks made while it is on its way stay for the next round; the
    // button stays off until then
    sendingRound = true;
    recomputeBtn.disabled = true;
    let why;
    try {
      why = await post(experiment, "edits/round", round);
    } finally {
      sendingRound = false;
    }
    if (why) {
      roundFailedFor = JSON.stringify(round);
      showError("Could not apply the round: " + why);
      if (stillShown(experiment)) startPolling(experiment);
      return;
    }
    roundFailedFor = null;
    clearSent(round, experiment.entry_id);
  }
  roundSent = { entryId: experiment.entry_id, before, sent };
  marks = NO_MARKS;
  startJob(experiment, decided);
}

async function confirmAll(experiment) {
  if (!lastStatus || confirmAllBtn.disabled) return;
  const status = lastStatus;
  if (!await confirmDialog(confirmQuestion(status), "Confirm all")) return;
  clearError();
  const why = await confirmEdits(experiment, pendingKeys(status.inputs || []));
  if (why) {
    showError("Could not confirm: " + why);
    return;
  }
  marks = NO_MARKS;
  // the updates it covered are locked now
  reloadInputs(experiment);
}

async function resetDecisions(experiment) {
  const n = decisionStatus ? decisionCount(decisionStatus) : 0;
  const what = n === 1 ? "the 1 decision" : n ? "all " + n + " decisions" : "every decision";
  const question = "Reset " + what + "? The sheet goes back to the original.";
  if (!await confirmDialog(question, "Reset")) return;
  clearError();
  const why = await post(experiment, "edits/reset", {});
  // a refused reset keeps the round not sent yet
  if (why) showError("Could not reset: " + why);
  else clearRound();
  marks = NO_MARKS;
  reloadInputs(experiment);
  if (stillShown(experiment)) startPolling(experiment);
}

// The card for a newly selected experiment with an uploaded sheet
// (`view` from GET .../sheet-view), or none.
export function showEdit(experiment, view) {
  stopPolling();
  leaveDue();
  current = experiment;
  roundFor(experiment ? experiment.entry_id : null);
  roundSent = null;
  kept = NO_KEPT;
  marks = NO_MARKS;
  statusKnown = false;
  lastStatus = null;
  decisionStatus = null;
  headerFor = view ? view.sha256 : null;
  editedReady = false;
  reportsShown = {};
  readFor = null;
  wroteFor = null;
  idle();
  confirmAllBtn.hidden = recomputeBtn.hidden = true;
  showTakeBack(null, false);
  updateProgress();
  gateDownload("");
  readAgainBtn.hidden = true;
  applyResult.hidden = true;
  if (!experiment || !view) {
    // nothing to poll: the tiles forget the last job's reports here
    setReports({}, false, false, null, NO_MARKS);
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
// it still shows this experiment. `decided`: the inputs whose decisions
// changed (null: all of them)
async function startJob(experiment, decided = new Set()) {
  let start = 0; // this start's number, when it shows its experiment
  // any job reads the new updates and writes the decisions too
  cancelDue(experiment);
  const items = itemsOf(experiment);
  if (stillShown(experiment)) {
    running = true;
    if (items) readFor = readKey(items);
    wroteFor = lastStatus ? unwrittenKey(lastStatus) : null;
    confirmAllBtn.hidden = true;
    readAgainBtn.hidden = true;
    applyResult.hidden = true;
    busy("Starting...");
    gateDownload(blockShown);
    confirmAllBtn.hidden = recomputeBtn.hidden = true;
    // the latest status: a failed run's knows only what it reported
    const notes = decided ? unchangedAbove(items, decisionStatus || lastStatus, decided) : new Set();
    start = ++starts;
    kept = { entryId: experiment.entry_id, start, notes, job: null };
    // no ticking on the way out: the job reads the decisions
    setReports(reportsShown, true, true, notes, marks);
  }
  // the kept notes are this start's while no later start replaced them
  const forget = () => {
    if (start && kept.start === start) kept = NO_KEPT;
  };
  try {
    const res = await fetch(experimentUrl(experiment, "edit-async"), { method: "POST" });
    if (!res.ok) {
      // the job running now, if any, is not this page's
      forget();
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
    const id = await res.json().then((job) => job.job_id, () => null);
    if (start && kept.start === start) kept = { ...kept, job: id };
    // the reservation is written when the POST returns: polled at once
    if (stillShown(experiment)) startPolling(experiment);
  } catch (err) {
    forget();
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
    if (current) confirmAll(current);
  });
  recomputeBtn.addEventListener("click", () => {
    if (current) recompute(current);
  });
  onRoundChange(() => {
    if (current && statusKnown && !running && !sendingRound) {
      showActions(lastStatus);
      startSoon(current);
    }
  });
  // a round not sent yet lives in this page only
  window.addEventListener("beforeunload", (event) => {
    if (anyRound()) event.preventDefault();
  });
  resetBtn.addEventListener("click", () => {
    if (current) resetDecisions(current);
  });
  downloadFinalBtn.addEventListener("click", () => downloadSheet("final-sheet", ".xlsx"));
  downloadOriginalBtn.addEventListener("click", () => downloadSheet("original-sheet", " (original).xlsx"));
}
