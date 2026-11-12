// The "Something's wrong" button and its dialog. A report is stored in
// the open sheet's upload, so the button shows only while a sheet is open.

import { errorDetail, experimentUrl } from "./api.js";
import { recentErrors, watchPage } from "./recent-errors.js";

const button = document.getElementById("feedback-btn");
const dialog = document.getElementById("feedback-dialog");
const form = document.getElementById("feedback-form");
const wrong = document.getElementById("feedback-wrong");
const expected = document.getElementById("feedback-expected");
const sendBtn = document.getElementById("feedback-send");
const statusEl = document.getElementById("feedback-status");

let experiment = null;

export function showFeedback(selected) {
  experiment = selected;
  button.hidden = !selected;
}

async function send(event) {
  event.preventDefault();
  if (!experiment || !wrong.value.trim()) return;
  sendBtn.disabled = true;
  statusEl.textContent = "Sending…";
  try {
    const res = await fetch(experimentUrl(experiment, "feedback"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        what_wrong: wrong.value.trim(),
        expected: expected.value.trim(),
        page: "sheet",
        user_agent: navigator.userAgent,
        recent_errors: recentErrors(),
      }),
    });
    if (!res.ok) throw new Error(await errorDetail(res));
    form.reset();
    statusEl.textContent = "Thanks, your report is saved.";
    setTimeout(() => dialog.close(), 1500);
  } catch (err) {
    // the dialog stays open with the words: nothing typed is lost
    statusEl.textContent = "Could not send: " + err.message;
  } finally {
    sendBtn.disabled = false;
  }
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
  form.addEventListener("submit", send);
}
