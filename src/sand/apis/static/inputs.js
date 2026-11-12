// The Inputs card: every recording/note of the experiment, the updates
// of its sheet in the order they were made (the time is fixed). Clicking
// a row turns its text into an editor in place (one row at a time);
// clicking its label renames it (free text, for scanning the list).
// Audio revisions go to corrected_transcript (clearing withdraws them);
// note revisions overwrite the note text. Under each input, once a job
// ran, its report: what it changes, with a confirm button per ready
// edit. The Sheet card hears of every loaded list (setInputsListener)
// and starts the job that reads a new or revised input.

import { errorDetail, experimentUrl } from "./api.js";
import { inputState, previewNotes, rewriteWarning } from "./edit-logic.js";
import { selectedExperiment } from "./experiments.js";
import { editReported, reportEdit } from "./feedback.js";
import { clearError, confirmDialog, showEntryLink, showError } from "./ui.js";

const inputsList = document.getElementById("inputs-list");
const inputsCount = document.getElementById("inputs-count");
const refreshInputsBtn = document.getElementById("refresh-inputs-btn");
const editsOnlyBtn = document.getElementById("edits-only-btn");
const EDITS_ONLY_KEY = "sand.inputs.editsOnly";

// the job's reports by entry id, whether a job runs (no confirming
// meanwhile), and the confirm handler of the Sheet card
let reports = {};
let jobRunning = false;
let onConfirm = null;
let onInputsLoaded = null; // (experiment, items) after every load
// the last rendered list, re-rendered when the reports change; a change
// that arrives while an editor is open renders when the editor closes
let shown = null;
let renderPending = false;

let inputsGeneration = 0;
let inputsRefreshTimer = null;
// {item, experiment, li, textEl, editor, saveBtn} while a row is edited
let revising = null;
// while a label editor is open, list re-renders are held off so the
// input field is not wiped mid-edit (same for a revision editor)
let inlineEditing = false;

function inputUrl(experiment, item, path) {
  return experimentUrl(experiment, "inputs/" + encodeURIComponent(item.entry_id) + "/" + path);
}

function inputTime(item) {
  if (!item.datetime) return "";
  const parsed = new Date(item.datetime);
  if (Number.isNaN(parsed.getTime())) return "";
  const time = parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (parsed.toDateString() === new Date().toDateString()) return time;
  return parsed.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + time;
}

function inputDescription(item) {
  return (item.kind === "audio" ? "recording" : "note")
    + (inputTime(item) ? " from " + inputTime(item) : "");
}

// --- label edit ------------------------------------------------------------

function beginLabelEdit(labelBtn, item, experiment) {
  if (inlineEditing) return;
  inlineEditing = true;
  const editor = document.createElement("input");
  editor.type = "text";
  editor.className = "input-label-edit";
  editor.placeholder = "Label";
  editor.setAttribute("aria-label", "Label of the " + inputDescription(item));
  editor.value = item.label || "";
  editor.addEventListener("click", (e) => e.stopPropagation());
  let done = false;
  const finish = (refresh) => {
    if (done) return;
    done = true;
    inlineEditing = false;
    editor.replaceWith(labelBtn);
    if (renderPending) renderIfIdle();
    // the server returns once NOMAD reprocessed the entry
    if (refresh) startInputsRefresh(experiment);
  };
  let saving = false;
  const commit = async () => {
    // disabling the focused editor below fires blur -> commit again
    if (done || saving) return;
    const label = editor.value.trim();
    if (label === (item.label || "")) {
      finish(false);
      return;
    }
    clearError();
    saving = true;
    editor.disabled = true;
    try {
      const res = await fetch(inputUrl(experiment, item, "label"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label }),
      });
      if (!res.ok) {
        showError("Could not change the label: " + await errorDetail(res));
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
  labelBtn.replaceWith(editor);
  editor.focus();
}

// --- revision in place -----------------------------------------------------

async function beginRevision(item, experiment, tile, textNode) {
  if (revising && revising.item.entry_id === item.entry_id) return;
  // a revision already being saved is not lost by switching: no prompt
  if (revising && !revising.saveBtn.disabled
      && revising.editor.value.trim() !== (revising.item.text || "").trim()
      && !await confirmDialog("Discard the unsaved revision and open this input?", "Discard")) {
    return;
  }
  endRevision();
  // closing the other editor may have rendered the list anew: the
  // tile clicked is then replaced (or gone meanwhile)
  let li = tile;
  let textEl = textNode;
  if (!li.isConnected) {
    li = inputsList.querySelector(`[data-entry-id="${CSS.escape(item.entry_id)}"]`);
    textEl = li?.querySelector(".input-text");
    if (!li || !textEl) return;
  }

  const box = document.createElement("div");
  box.className = "input-revise";
  // clicks inside must not re-trigger the tile's own click
  box.addEventListener("click", (e) => e.stopPropagation());

  const editor = document.createElement("textarea");
  editor.className = "input-revise-text";
  editor.value = item.text || "";
  editor.setAttribute("aria-label", "Revise the " + inputDescription(item));

  const actions = document.createElement("div");
  actions.className = "input-revise-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn btn-primary btn-small";
  saveBtn.textContent = "Save";
  saveBtn.addEventListener("click", saveRevision);
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "btn btn-outlined btn-small";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", () => endRevision(true));
  const hint = document.createElement("span");
  hint.className = "input-revise-hint";
  hint.textContent = item.kind === "audio"
    ? "Saved as corrected transcript; empty withdraws the correction."
    : "";
  actions.append(saveBtn, cancelBtn, hint);

  editor.addEventListener("keydown", (e) => {
    if (e.key === "Escape") endRevision(true);
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) saveRevision();
  });

  box.append(editor, actions);
  textEl.replaceWith(box);
  li.classList.add("selected");
  // no confirming words that are about to change
  for (const btn of li.querySelectorAll(".input-report button")) btn.disabled = true;
  revising = { item, experiment, li, textEl, editor, saveBtn };
  // fit the whole text, capped by the CSS max-height
  editor.style.height = editor.scrollHeight + 2 + "px";
  editor.focus();
}

// refocus: the editor held focus, so keyboard users would otherwise
// fall back to the page start (not wanted when switching tiles)
function endRevision(refocus = false) {
  if (!revising) return;
  const { li, textEl, editor } = revising;
  revising = null;
  editor.parentElement.replaceWith(textEl);
  li.classList.remove("selected");
  // back to the state the buttons had: none while a job runs, and a
  // confirmation in flight stays disabled
  for (const btn of li.querySelectorAll(".input-report button")) {
    btn.disabled = jobRunning || !onConfirm || btn.dataset.busy === "1";
  }
  if (renderPending) {
    renderIfIdle(); // replaces the tiles
    if (refocus) inputsList.querySelector(`[data-entry-id="${CSS.escape(li.dataset.entryId)}"]`)?.focus();
    return;
  }
  if (refocus && li.isConnected) li.focus();
}

async function saveRevision() {
  if (!revising || revising.saveBtn.disabled) return;
  const { item, experiment, editor, saveBtn } = revising;
  const text = editor.value.trim();
  if (text === (item.text || "").trim()) {
    // unchanged: save nothing - a stored revision must mean a human
    // actually changed something
    endRevision(true);
    return;
  }
  // disabled while the warning is open too: no second save
  saveBtn.disabled = true;
  const warning = rewriteWarning("revise", shown?.items || [], reports, item.entry_id);
  if (warning && !await confirmDialog(warning, "Save")) {
    saveBtn.disabled = false;
    return;
  }
  clearError();
  try {
    const res = await fetch(inputUrl(experiment, item, "text"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      showError("Could not save the revision: " + await errorDetail(res));
      return;
    }
    // the save can outlive its editor (cancelled or another tile opened
    // meanwhile): only close the editor it came from
    if (revising?.editor === editor) endRevision(true);
    // the server returns once NOMAD reprocessed the entry
    startInputsRefresh(experiment);
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    saveBtn.disabled = false;
  }
}

// --- delete ----------------------------------------------------------------

async function deleteInput(item, experiment, button) {
  const what = item.kind === "audio"
    ? "Delete this " + inputDescription(item) + "? Its audio and transcript are deleted from NOMAD."
    : "Delete this " + inputDescription(item) + "? It is deleted from NOMAD.";
  const warning = rewriteWarning("delete", shown?.items || [], reports, item.entry_id);
  if (!await confirmDialog(warning ? what + " " + warning : what, "Delete")) return;
  // its open editor would hold off the list refresh and could still save
  if (revising?.item.entry_id === item.entry_id) endRevision();
  clearError();
  button.disabled = true;
  try {
    const res = await fetch(
      experimentUrl(experiment, "inputs/" + encodeURIComponent(item.entry_id)),
      { method: "DELETE" },
    );
    if (!res.ok) showError("Delete failed: " + await errorDetail(res));
  } catch (err) {
    showError("Network error: " + err.message);
  }
  // also after a failure: the list shows what is left
  startInputsRefresh(experiment);
}

// --- edit mode: the report under an input --------------------------------------

// the cells of an edit row: column, old → new, samples. A report of an
// older job has only the line: one cell across the three.
function renderEditCells(edit) {
  if (edit.column === undefined) {
    const line = document.createElement("span");
    line.className = "report-line";
    line.textContent = edit.line;
    return [line];
  }
  const column = document.createElement("span");
  column.className = "report-column";
  column.textContent = edit.column;
  const change = document.createElement("span");
  change.className = "report-change";
  const oldValue = document.createElement("span");
  oldValue.className = "report-old";
  oldValue.textContent = edit.old;
  const newValue = document.createElement("span");
  newValue.className = "report-new";
  newValue.textContent = edit.new;
  change.append(oldValue, " → ", newValue);
  const samples = document.createElement("span");
  samples.className = "report-samples";
  samples.textContent = edit.samples;
  if (edit.unchanged) {
    const kept = document.createElement("span");
    kept.className = "report-unchanged";
    kept.textContent = edit.unchanged;
    samples.append(kept);
  }
  return [column, change, samples];
}

// "This edit is wrong": a report for #172, the edit itself unchanged
function wrongFlag(edit) {
  const flag = document.createElement("button");
  flag.type = "button";
  flag.className = "btn btn-icon report-wrong";
  const icon = document.createElement("span");
  icon.className = "material-icons";
  flag.append(icon);
  const show = (reported) => {
    icon.textContent = reported ? "flag" : "outlined_flag";
    flag.classList.toggle("reported", reported);
    flag.disabled = reported;
    flag.title = reported ? "Reported as wrong" : "This edit is wrong";
    flag.setAttribute("aria-label", flag.title + ": " + edit.line);
  };
  show(editReported(edit.key));
  flag.addEventListener("click", async () => {
    if (await reportEdit(edit)) show(true);
  });
  return flag;
}

// The edits of a report, grouped under their step as the Sheet table
// groups them: a step heading, then one aligned row per edit with its
// state (written, or the button that confirms it) at the right.
function renderReport(report) {
  const box = document.createElement("div");
  box.className = "input-report";
  box.addEventListener("click", (e) => e.stopPropagation());
  const confirmed = new Set(report.confirmed || []);
  const since = new Set(report.confirmed_since || []);
  let step;
  for (const edit of report.ready || []) {
    if (edit.column !== undefined && edit.step !== step) {
      step = edit.step;
      const heading = document.createElement("div");
      heading.className = "report-step";
      heading.textContent = step;
      box.append(heading);
    }
    const row = document.createElement("div");
    row.className = "report-edit";
    row.append(...renderEditCells(edit));
    // one cell: the row's grid places its last child
    const state = document.createElement("span");
    state.className = "report-state";
    row.append(state);
    if (confirmed.has(edit.key)) {
      const mark = document.createElement("span");
      mark.className = "report-confirmed" + (since.has(edit.key) ? " report-writing" : "");
      mark.textContent = since.has(edit.key) ? "writing..." : "written";
      state.append(mark);
    } else {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-outlined btn-small report-confirm";
      btn.textContent = "Confirm";
      btn.disabled = jobRunning || !onConfirm;
      btn.title = jobRunning ? "Wait: the updates are being read" : "Confirm this edit";
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.dataset.busy = "1";
        const done = await onConfirm(edit.key);
        delete btn.dataset.busy;
        if (!done) btn.disabled = jobRunning || !onConfirm;
      });
      state.append(btn);
    }
    state.append(wrongFlag(edit));
    box.append(row);
    for (const check of edit.checks || []) {
      const p = document.createElement("p");
      p.className = "report-check";
      p.textContent = "Check: " + check;
      box.append(p);
    }
  }
  for (const note of previewNotes(report)) {
    const p = document.createElement("p");
    p.className = "report-note";
    p.textContent = note;
    box.append(p);
  }
  if (report.refused) {
    const p = document.createElement("p");
    p.className = "report-note problem-text";
    p.textContent = "Not written: " + report.refused;
    box.append(p);
  }
  return box;
}

// The job's reports (by entry id), from the Sheet card's polling.
// `confirm(key)` resolves false when the confirmation did not go through.
export function setReports(byEntry, running, confirm) {
  reports = byEntry;
  jobRunning = running;
  onConfirm = confirm;
  renderIfIdle();
}

export function setInputsListener(fn) {
  onInputsLoaded = fn;
}

// an open editor is left alone (its field would be wiped); the render
// happens when it closes
function renderIfIdle() {
  if (!shown) return;
  if (inlineEditing || revising) {
    renderPending = true;
    return;
  }
  renderPending = false;
  renderInputs(shown.experiment, shown.items);
}

// --- list ------------------------------------------------------------------

function showInputsPlaceholder(message) {
  const li = document.createElement("li");
  li.className = "inputs-empty";
  li.textContent = message;
  inputsList.replaceChildren(li);
  inputsCount.textContent = "";
  shown = null;
}

function renderInputs(experiment, items) {
  shown = { experiment, items };
  if (!items.length) {
    showInputsPlaceholder("No updates yet. Record one, or write a note.");
    return;
  }
  // a re-render replaces the tiles: keep keyboard focus on the same one
  const focusedId = inputsList.contains(document.activeElement)
    ? document.activeElement.closest(".input-tile")?.dataset.entryId
    : null;
  inputsList.replaceChildren();
  inputsCount.textContent = String(items.length);
  for (const item of items) {
    const li = document.createElement("li");
    li.className = "input-tile";
    li.dataset.entryId = item.entry_id;
    // a report of other words (revised since) is not this update's
    const report = reports[item.entry_id];
    const reported = report && (report.text === undefined || report.text === item.text);

    const meta = document.createElement("div");
    meta.className = "input-meta";
    const icon = document.createElement("span");
    icon.className = "material-icons";
    icon.textContent = item.kind === "audio" ? "mic" : "edit_note";
    const kind = document.createElement("span");
    kind.className = "input-kind";
    kind.textContent = item.kind === "audio" ? "Recording" : "Note";
    meta.append(icon, kind);
    const labelBtn = document.createElement("button");
    labelBtn.type = "button";
    labelBtn.className = "input-label" + (item.label ? "" : " empty");
    labelBtn.textContent = item.label || "+ label";
    labelBtn.title = "Click to change the label";
    labelBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      beginLabelEdit(labelBtn, item, experiment);
    });
    meta.append(labelBtn);
    // the time is when the update was made: not editable
    const when = document.createElement("span");
    when.className = "input-when";
    when.textContent = inputTime(item);
    meta.append(when);
    if (item.corrected) {
      const badge = document.createElement("span");
      badge.className = "input-badge";
      badge.textContent = "corrected";
      meta.append(badge);
    }
    // the state and the hover actions, at the right
    const right = document.createElement("span");
    right.className = "input-right";
    meta.append(right);
    const state = inputState(item, report, jobRunning, Boolean(onConfirm));
    if (state) {
      const chip = document.createElement("span");
      chip.className = "input-state is-" + state.state;
      chip.textContent = state.label;
      right.append(chip);
    }
    const nomadLink = document.createElement("a");
    nomadLink.href = item.entry_url;
    nomadLink.target = "_blank";
    nomadLink.rel = "noopener noreferrer";
    nomadLink.className = "material-icons input-nomad-link";
    nomadLink.textContent = "open_in_new";
    nomadLink.title = "View on NOMAD";
    nomadLink.addEventListener("click", (e) => e.stopPropagation());
    right.append(nomadLink);
    if (item.deletable) {
      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "btn btn-icon input-delete";
      deleteBtn.title = "Delete";
      deleteBtn.setAttribute("aria-label", "Delete the " + inputDescription(item));
      const icon = document.createElement("span");
      icon.className = "material-icons";
      icon.textContent = "close";
      deleteBtn.append(icon);
      deleteBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteInput(item, experiment, deleteBtn);
      });
      right.append(deleteBtn);
    }

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
    if (reported) li.append(renderReport(report));
    li.tabIndex = 0;
    li.setAttribute("aria-label", "Revise the " + inputDescription(item));
    li.addEventListener("click", () => beginRevision(item, experiment, li, text));
    li.addEventListener("keydown", (e) => {
      // only the tile itself: Enter/Space on its link or editor must
      // keep their own action
      if (e.target !== li || (e.key !== "Enter" && e.key !== " ")) return;
      e.preventDefault();
      beginRevision(item, experiment, li, text);
    });
    inputsList.append(li);
    if (item.entry_id === focusedId) li.focus();
  }
}

// a refresh can outlive its experiment (a save finishing after the user
// switched), so check the selection as well as the generation
function inputsStillWanted(experiment, generation) {
  return generation === inputsGeneration
    && selectedExperiment()?.entry_id === experiment.entry_id;
}

async function loadInputs(experiment, generation) {
  if (!inputsStillWanted(experiment, generation)) return;
  let res;
  try {
    res = await fetch(experimentUrl(experiment, "inputs"));
  } catch (err) {
    return; // next manual refresh or upload will retry
  }
  if (!inputsStillWanted(experiment, generation)) return;
  if (!res.ok) return;
  const data = await res.json().catch(() => null);
  if (!inputsStillWanted(experiment, generation) || !data) return;
  if (inlineEditing || revising) {
    // don't wipe an open editor; try again shortly
    inputsRefreshTimer = setTimeout(() => loadInputs(experiment, generation), 3000);
    return;
  }
  renderInputs(experiment, data.inputs);
  if (onInputsLoaded) onInputsLoaded(experiment, data.inputs);
  // keep refreshing while any audio still has no text and no failure
  if (data.inputs.some((i) => i.kind === "audio" && !i.text && i.status !== "FAILED")) {
    inputsRefreshTimer = setTimeout(() => loadInputs(experiment, generation), 5000);
  }
}

function stopInputsRefresh() {
  inputsGeneration += 1;
  if (inputsRefreshTimer) clearTimeout(inputsRefreshTimer);
  inputsRefreshTimer = null;
}

function startInputsRefresh(experiment, delayMs) {
  stopInputsRefresh();
  const generation = inputsGeneration;
  inputsRefreshTimer = setTimeout(
    () => loadInputs(experiment, generation), delayMs || 0);
}

// The list for a newly selected experiment (or none).
export function showInputs(experiment) {
  endRevision();
  showInputsPlaceholder(experiment ? "Loading..." : "Open a sheet to see its updates.");
  if (experiment) startInputsRefresh(experiment);
  else stopInputsRefresh();
}

// For the Record and Note cards: report the new entry's link in `el`
// and show the input in the list (audio rows start as "transcribing..."
// and the list keeps refreshing until text arrives).
// `response`: the fetch's Response, or the promise of it.
export async function reportNewInput(el, response, failPrefix, message, linkText) {
  const res = await response;
  if (!res.ok) {
    showError(failPrefix + ": " + await errorDetail(res));
    return false;
  }
  const data = await res.json();
  showEntryLink(el, message, data.entry_url, linkText);
  const experiment = selectedExperiment();
  if (experiment) startInputsRefresh(experiment, 1000);
  return true;
}

// "Edits only": the words and the header of every update folded away,
// the cards down to their edits, for a glance at what changes in the
// sheet. Remembered in this browser.
function showEditsOnly(on) {
  inputsList.classList.toggle("edits-only", on);
  // the label stays; the pressed state says the words are folded
  editsOnlyBtn.setAttribute("aria-pressed", String(on));
  editsOnlyBtn.title = on
    ? "Show the updates again, with their words"
    : "See only the edits of the updates";
}

export function initInputs() {
  refreshInputsBtn.addEventListener("click", () => {
    const experiment = selectedExperiment();
    if (experiment) startInputsRefresh(experiment);
  });
  let editsOnly = false;
  try { editsOnly = localStorage.getItem(EDITS_ONLY_KEY) === "true"; } catch { /* ignore */ }
  showEditsOnly(editsOnly);
  editsOnlyBtn.addEventListener("click", () => {
    const on = editsOnlyBtn.getAttribute("aria-pressed") !== "true";
    showEditsOnly(on);
    try { localStorage.setItem(EDITS_ONLY_KEY, String(on)); } catch { /* ignore */ }
  });
}
