// User reports, stored in the open sheet's upload: the "Something's
// wrong" button (shown only while a sheet is open) and the "Wrong" flag
// on a proposed edit (inputs.js). A report changes nothing in the sheet.

import { errorDetail, experimentUrl } from "./api.js";
import { editReportBody, pageReportBody, WRONG_REASONS } from "./feedback-logic.js";
import { recentErrors, watchPage } from "./recent-errors.js";

const button = document.getElementById("feedback-btn");
const dialog = document.getElementById("feedback-dialog");
const form = document.getElementById("feedback-form");
const wrong = document.getElementById("feedback-wrong");
const expected = document.getElementById("feedback-expected");
const sendBtn = document.getElementById("feedback-send");
const statusEl = document.getElementById("feedback-status");

const editDialog = document.getElementById("edit-wrong-dialog");
const editForm = document.getElementById("edit-wrong-form");
const editLine = document.getElementById("edit-wrong-line");
const editReasons = document.getElementById("edit-wrong-reasons");
const editMeant = document.getElementById("edit-wrong-meant");
const editSendBtn = document.getElementById("edit-wrong-send");
const editStatus = document.getElementById("edit-wrong-status");

let experiment = null;
// keys reported wrong in this page session: their flag stays filled
// across re-renders (not stored: step 1 of #172 changes no edit state)
const reportedKeys = new Set();
let editKey = null;
let editAnswer = () => {};

export function showFeedback(selected) {
  experiment = selected;
  button.hidden = !selected;
}

export function editReported(key) {
  return reportedKeys.has(key);
}

async function postReport(body) {
  const res = await fetch(experimentUrl(experiment, "feedback"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await errorDetail(res));
}

// The dialogs stay open with the words on a failed send: nothing typed
// is lost.
async function sendPage(event) {
  event.preventDefault();
  if (!experiment || !wrong.value.trim()) return;
  sendBtn.disabled = true;
  statusEl.textContent = "Sending…";
  try {
    await postReport(pageReportBody({
      wrong: wrong.value,
      expected: expected.value,
      userAgent: navigator.userAgent,
      errors: recentErrors(),
    }));
    form.reset();
    statusEl.textContent = "Thanks, your report is saved.";
    setTimeout(() => dialog.close(), 1500);
  } catch (err) {
    statusEl.textContent = "Could not send: " + err.message;
  } finally {
    sendBtn.disabled = false;
  }
}

// Resolves true once the report is saved, false when cancelled.
export function reportEdit(edit) {
  editAnswer(false);
  editKey = edit.key;
  editForm.reset();
  editStatus.textContent = "";
  editLine.textContent = edit.line;
  editDialog.showModal();
  return new Promise((resolve) => {
    editAnswer = (sent) => {
      editAnswer = () => {};
      resolve(sent);
    };
  });
}

async function sendEdit(event) {
  event.preventDefault();
  const reason = editForm.elements.reason.value;
  if (!experiment || !editKey || !reason) return;
  editSendBtn.disabled = true;
  editStatus.textContent = "Sending…";
  try {
    await postReport(editReportBody({
      key: editKey,
      reason,
      meant: editMeant.value,
      userAgent: navigator.userAgent,
      errors: recentErrors(),
    }));
    reportedKeys.add(editKey);
    editAnswer(true);
    editStatus.textContent = "Thanks, your report is saved.";
    setTimeout(() => editDialog.close(), 1000);
  } catch (err) {
    editStatus.textContent = "Could not send: " + err.message;
  } finally {
    editSendBtn.disabled = false;
  }
}

function renderReasons() {
  editReasons.replaceChildren(...WRONG_REASONS.map(([value, text], i) => {
    const label = document.createElement("label");
    label.className = "edit-wrong-reason";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "reason";
    radio.value = value;
    radio.required = i === 0; // one required radio makes the group required
    label.append(radio, " " + text);
    return label;
  }));
}

// First of all inits: watchPage must see every request.
export function initFeedback() {
  watchPage();
  fetch("ui-config")
    .then((res) => res.json())
    .then((config) => {
      document.getElementById("feedback-version").textContent = "sand " + config.sand_version;
    })
    .catch(() => {});
  button.addEventListener("click", () => {
    statusEl.textContent = "";
    dialog.showModal();
    wrong.focus();
  });
  document.getElementById("feedback-cancel").addEventListener("click", () => dialog.close());
  form.addEventListener("submit", sendPage);

  renderReasons();
  document.getElementById("edit-wrong-cancel").addEventListener("click", () => {
    editAnswer(false);
    editDialog.close();
  });
  // Escape
  editDialog.addEventListener("close", () => editAnswer(false));
  editForm.addEventListener("submit", sendEdit);
}
