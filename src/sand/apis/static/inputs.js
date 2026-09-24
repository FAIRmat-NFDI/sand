// The Inputs card: every recording/note of the experiment, in extraction
// order. Clicking a row turns its text into an editor in place (one row
// at a time); clicking its time moves it on the timeline. Audio
// revisions go to corrected_transcript (clearing withdraws them); note
// revisions overwrite the note text.

import { authFetch, errorDetail, experimentUrl } from "./api.js";
import { selectedExperiment } from "./experiments.js";
import { clearError, showEntryLink, showError } from "./ui.js";

const inputsList = document.getElementById("inputs-list");
const inputsCount = document.getElementById("inputs-count");
const refreshInputsBtn = document.getElementById("refresh-inputs-btn");

let inputsGeneration = 0;
let inputsRefreshTimer = null;
// {item, experiment, li, textEl, editor, saveBtn} while a row is edited
let revising = null;
// while a time editor is open, list re-renders are held off so the
// input field is not wiped mid-edit (same for a revision editor)
let timeEditing = false;

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

// --- time edit -------------------------------------------------------------

function toLocalInputValue(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
    + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
}

function beginTimeEdit(whenSpan, item, experiment) {
  if (timeEditing) return;
  timeEditing = true;
  const editor = document.createElement("input");
  editor.type = "datetime-local";
  editor.className = "input-time-edit";
  editor.setAttribute("aria-label", "Time of this input (reorders the inputs)");
  editor.value = toLocalInputValue(item.datetime);
  editor.addEventListener("click", (e) => e.stopPropagation());
  let done = false;
  const finish = (refresh) => {
    if (done) return;
    done = true;
    timeEditing = false;
    editor.replaceWith(whenSpan);
    // the server returns once NOMAD reprocessed the entry
    if (refresh) startInputsRefresh(experiment);
  };
  let saving = false;
  const commit = async () => {
    // disabling the focused editor below fires blur -> commit again
    if (done || saving) return;
    if (!editor.value || toLocalInputValue(item.datetime) === editor.value) {
      finish(false);
      return;
    }
    const iso = new Date(editor.value).toISOString();
    clearError();
    saving = true;
    editor.disabled = true;
    try {
      const res = await authFetch(inputUrl(experiment, item, "datetime"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ datetime: iso }),
      });
      if (!res.ok) {
        showError("Could not change the time: " + await errorDetail(res));
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
  whenSpan.replaceWith(editor);
  editor.focus();
}

// --- revision in place -----------------------------------------------------

function beginRevision(item, experiment, li, textEl) {
  if (revising && revising.item.entry_id === item.entry_id) return;
  // a revision already being saved is not lost by switching: no prompt
  if (revising && !revising.saveBtn.disabled
      && revising.editor.value.trim() !== (revising.item.text || "").trim()
      && !window.confirm("Discard the unsaved revision and open this input?")) {
    return;
  }
  endRevision();

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
  clearError();
  saveBtn.disabled = true;
  try {
    const res = await authFetch(inputUrl(experiment, item, "text"), {
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

// --- list ------------------------------------------------------------------

function showInputsPlaceholder(message) {
  const li = document.createElement("li");
  li.className = "inputs-empty";
  li.textContent = message;
  inputsList.replaceChildren(li);
  inputsCount.textContent = "";
}

function renderInputs(experiment, items) {
  if (!items.length) {
    showInputsPlaceholder("No inputs yet - record or write a note.");
    return;
  }
  // a re-render replaces the tiles: keep keyboard focus on the same one
  const focusedId = inputsList.contains(document.activeElement)
    ? document.activeElement.closest(".input-tile")?.dataset.entryId
    : null;
  inputsList.replaceChildren();
  inputsCount.textContent = "(" + items.length + ")";
  for (const item of items) {
    const li = document.createElement("li");
    li.className = "input-tile";
    li.dataset.entryId = item.entry_id;

    const meta = document.createElement("div");
    meta.className = "input-meta";
    const icon = document.createElement("span");
    icon.className = "material-icons";
    icon.textContent = item.kind === "audio" ? "mic" : "edit_note";
    const kind = document.createElement("span");
    kind.className = "input-kind";
    kind.textContent = item.kind === "audio" ? "Recording" : "Note";
    meta.append(icon, kind);
    const when = document.createElement("button");
    when.type = "button";
    when.className = "input-when";
    const time = inputTime(item);
    when.textContent = "· " + (time || "set time");
    when.title = "Click to change the time (reorders the inputs)";
    when.addEventListener("click", (e) => {
      e.stopPropagation();
      beginTimeEdit(when, item, experiment);
    });
    meta.append(when);
    if (item.corrected) {
      const badge = document.createElement("span");
      badge.className = "input-badge";
      badge.textContent = "· ✎ corrected";
      meta.append(badge);
    }
    const nomadLink = document.createElement("a");
    nomadLink.href = item.entry_url;
    nomadLink.target = "_blank";
    nomadLink.rel = "noopener noreferrer";
    nomadLink.className = "material-icons input-nomad-link";
    nomadLink.textContent = "open_in_new";
    nomadLink.title = "View on NOMAD";
    nomadLink.addEventListener("click", (e) => e.stopPropagation());
    meta.append(nomadLink);

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
    li.tabIndex = 0;
    li.setAttribute("aria-label", "Revise the " + inputDescription(item));
    li.addEventListener("click", () => beginRevision(item, experiment, li, text));
    li.addEventListener("keydown", (e) => {
      // only the tile itself: Enter/Space on its time button, link or
      // editor must keep their own action
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
    res = await authFetch(experimentUrl(experiment, "inputs"));
  } catch (err) {
    return; // next manual refresh or upload will retry
  }
  if (!inputsStillWanted(experiment, generation)) return;
  if (!res.ok) return;
  const data = await res.json().catch(() => null);
  if (!inputsStillWanted(experiment, generation) || !data) return;
  if (timeEditing || revising) {
    // don't wipe an open editor; try again shortly
    inputsRefreshTimer = setTimeout(() => loadInputs(experiment, generation), 3000);
    return;
  }
  renderInputs(experiment, data.inputs);
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
  showInputsPlaceholder(experiment ? "Loading..." : "Select an experiment to see its inputs.");
  if (experiment) startInputsRefresh(experiment);
  else stopInputsRefresh();
}

// For the Record and Note cards: report the new entry's link in `el`
// and show the input in the list (audio rows start as "transcribing..."
// and the list keeps refreshing until text arrives).
export async function reportNewInput(el, fetchPromise, failPrefix, message, linkText) {
  const res = await fetchPromise;
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

export function initInputs() {
  refreshInputsBtn.addEventListener("click", () => {
    const experiment = selectedExperiment();
    if (experiment) startInputsRefresh(experiment);
  });
}
