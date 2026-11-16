// The Confirm all review: every undecided edit, ticked to confirm.
// Unticked ones are rejected first; the updates below are read again,
// and the review shows them anew, marking what changed, before the rest
// is confirmed at once. Edit mode feeds it every polled status.

import { changedEntries, reviewAction, reviewRows, reviewSnapshot, settledBlock } from "./edit-logic.js";

const dialog = document.getElementById("review-dialog");
const list = document.getElementById("review-list");
const statusEl = document.getElementById("review-status");
const closeBtn = document.getElementById("review-close");
const goBtn = document.getElementById("review-go");

const READING = "Reading the updates again...";

// while open: {experiment, handlers, items, shown (the status shown),
// block (why it may not be decided now), failed (the failed job's
// error, read again from here), unticked (keys), before (the snapshot
// before the last rejection), sending}
let review = null;

// `handlers`: {reject(keys), confirm(keys)}, each resolving to "" once
// done, else why not; readAgain() starts the job again after a failure
export function openReview(experiment, status, items, handlers) {
  review = {
    experiment, handlers, items: items || [], shown: status, block: "", failed: "",
    unticked: new Set(), before: null, sending: false,
  };
  render();
  dialog.showModal();
}

// a polled status of `experiment`; `running`: a job is at work
export function updateReview(experiment, status, items, running) {
  if (!review || review.experiment.entry_id !== experiment.entry_id || review.sending) return;
  if (items) review.items = items;
  review.failed = status.phase === "failed" ? status.error || "unknown error" : "";
  const reading = running || status.phase !== "completed" || settledBlock(status, items);
  review.block = reading ? READING : "";
  if (!review.block) review.shown = status;
  render();
}

function render() {
  const { shown, block } = review;
  const rows = reviewRows(shown, review.items);
  const waiting = Boolean(block) || review.sending;
  // marked once the updates are read again, against what was shown
  const changed = block ? new Set() : changedEntries(review.before, reviewSnapshot(shown));
  const keys = new Set(rows.confirm.map((row) => row.key));
  for (const key of [...review.unticked]) if (!keys.has(key)) review.unticked.delete(key);

  list.replaceChildren();
  section("To confirm", rows.confirm, (row, el) => {
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.checked = !review.unticked.has(row.key);
    tick.disabled = waiting;
    tick.addEventListener("change", () => {
      if (tick.checked) review.unticked.delete(row.key);
      else review.unticked.add(row.key);
      render();
    });
    el.prepend(tick);
  }, changed);
  section("Rejected", rows.rejected, (row, el) => el.prepend(mark("✗")), changed);
  section("Skipped: not an edit to confirm", rows.skipped, (row, el) => el.prepend(mark("–")), changed);
  if (!rows.confirm.length) {
    const p = document.createElement("p");
    p.className = "status-text";
    p.textContent = "Nothing left to decide.";
    list.append(p);
  }

  statusEl.classList.toggle("busy", waiting && !review.failed);
  if (review.failed) {
    // not reading any more: the failure, and the way on
    statusEl.textContent = "Reading the updates failed: " + review.failed;
    goBtn.hidden = false;
    goBtn.disabled = review.sending;
    goBtn.textContent = "Read again";
    return;
  }
  statusEl.textContent = block || (review.sending ? "Sending..." : statusEl.dataset.error || "");
  goBtn.hidden = !rows.confirm.length;
  goBtn.disabled = waiting;
  goBtn.textContent = reviewAction(rows.confirm.length - review.unticked.size, review.unticked.size);
}

function section(title, rows, decorate, changed) {
  if (!rows.length) return;
  const heading = document.createElement("p");
  heading.className = "review-section";
  heading.textContent = title;
  list.append(heading);
  for (const row of rows) {
    const el = document.createElement("label");
    el.className = "review-row";
    const name = document.createElement("span");
    name.className = "review-title";
    name.textContent = row.title;
    const line = document.createElement("span");
    line.textContent = row.line;
    el.append(name, line);
    if (changed.has(row.entry)) {
      const note = document.createElement("span");
      note.className = "review-changed";
      note.textContent = "⟳ changed";
      note.title = "Changed by the rejection above";
      el.append(note);
    }
    decorate(row, el);
    list.append(el);
  }
}

function mark(text) {
  const span = document.createElement("span");
  span.textContent = text;
  return span;
}

async function go() {
  if (review && review.failed && !review.sending) {
    review.failed = "";
    review.block = READING;
    render();
    review.handlers.readAgain();
    return;
  }
  if (!review || review.block || review.sending) return;
  const current = review;
  const rows = reviewRows(current.shown, current.items);
  const rejecting = rows.confirm.filter((row) => current.unticked.has(row.key)).map((row) => row.key);
  current.sending = true;
  delete statusEl.dataset.error;
  render();
  let why;
  if (rejecting.length) {
    current.before = reviewSnapshot(current.shown);
    why = await current.handlers.reject(rejecting);
    // read again before anything is confirmed
    if (!why) current.block = READING;
  } else {
    why = await current.handlers.confirm(rows.confirm.map((row) => row.key));
  }
  current.sending = false;
  if (review !== current) return;
  if (!why && !rejecting.length) {
    close();
    return;
  }
  if (why) statusEl.dataset.error = why;
  render();
}

function close() {
  review = null;
  delete statusEl.dataset.error;
  if (dialog.open) dialog.close();
}

goBtn.addEventListener("click", go);
closeBtn.addEventListener("click", close);
// Escape: the rejections made stay, as with Close
dialog.addEventListener("close", () => {
  review = null;
});
