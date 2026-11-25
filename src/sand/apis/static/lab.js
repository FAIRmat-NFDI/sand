// The lab view: the page reduced for work at the glove box. One step of
// the sheet with its column names, and the Record card with the notes so
// far and the live transcript in a big font; the rest of the page is
// hidden (styles.css, body.lab-mode). The user picks the step before
// the hands go in.

import { selectedExperiment } from "./experiments.js";
import { addInputsListener } from "./inputs.js";
import { labNotes, labPages, pageIndex } from "./lab-logic.js";
import { clearLiveTranscript } from "./live-transcript.js";

const enterBtn = document.getElementById("lab-btn");
const exitBtn = document.getElementById("lab-exit-btn");
const sheetEl = document.getElementById("lab-sheet");
const titleEl = document.getElementById("lab-step-title");
const subEl = document.getElementById("lab-step-sub");
const rowsEl = document.getElementById("lab-step-rows");
const unfilledEl = document.getElementById("lab-step-unfilled");
const prevBtn = document.getElementById("lab-prev-btn");
const nextBtn = document.getElementById("lab-next-btn");
const placeEl = document.getElementById("lab-step-place");
const notesEl = document.getElementById("lab-notes");
const liveEl = document.getElementById("live-transcript");

let pages = [];
let index = 0;
let shownSheet = null; // entry id of the sheet the pages are of
let transcribed = 0; // notes with their text, at the last load

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setLab(on) {
  // read now: the sheet may have been renamed since its view was shown
  const sheet = selectedExperiment();
  if (on && sheet) sheetEl.textContent = sheet.name || sheet.upload_id;
  document.body.classList.toggle("lab-mode", on);
  if (on) {
    window.scrollTo(0, 0);
    notesEl.scrollTop = notesEl.scrollHeight;
  }
}

function noteTime(datetime) {
  const parsed = new Date(datetime || "");
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// The notes so far in normal font, the newest last, above the live
// transcript. A note that got its text is no longer the big live text:
// it would be shown twice.
function showNotes(experiment, items) {
  const notes = labNotes(items);
  notesEl.replaceChildren(...notes.map((note) => {
    const li = el("li", note.pending ? "lab-note lab-note-pending" : "lab-note");
    li.append(el("span", "lab-note-time", noteTime(note.datetime)), el("span", "lab-note-text", note.text));
    return li;
  }));
  notesEl.scrollTop = notesEl.scrollHeight;
  const now = notes.filter((n) => !n.pending).length;
  if (now > transcribed && document.body.classList.contains("lab-mode")) clearLiveTranscript();
  transcribed = now;
}

function render() {
  const page = pages[index];
  if (!page) return;
  titleEl.textContent = page.title;
  subEl.textContent = page.sub.join(" · ");
  rowsEl.replaceChildren();
  for (const row of page.rows) {
    const item = el("div", "lab-row");
    item.append(el("span", "lab-row-name", row.name), el("span", "lab-row-value", row.value));
    rowsEl.append(item);
  }
  rowsEl.hidden = page.rows.length === 0;
  unfilledEl.replaceChildren();
  unfilledEl.hidden = page.unfilled.length === 0;
  if (page.unfilled.length) {
    unfilledEl.append(el("span", "lab-unfilled-label", "Not filled yet"), page.unfilled.join(" · "));
  }
  prevBtn.disabled = index === 0;
  nextBtn.disabled = index === pages.length - 1;
  placeEl.textContent = "Step " + page.position + " of " + pages[pages.length - 1].position;
}

function go(delta) {
  index = Math.min(Math.max(index + delta, 0), pages.length - 1);
  render();
}

// The shown sheet's steps (the sheet view), or none: without a sheet
// there is no lab view. A refresh of the same sheet keeps the step.
export function showLab(experiment, view) {
  enterBtn.hidden = !view;
  if (!experiment || !view) {
    if (!experiment || view === null) setLab(false); // not while the view is read
    return;
  }
  const position = shownSheet === experiment.entry_id ? pages[index]?.position : 0;
  shownSheet = experiment.entry_id;
  pages = labPages(view);
  index = pageIndex(pages, position);
  render();
}

export function initLab() {
  enterBtn.addEventListener("click", () => setLab(true));
  exitBtn.addEventListener("click", () => setLab(false));
  prevBtn.addEventListener("click", () => go(-1));
  nextBtn.addEventListener("click", () => go(1));
  addInputsListener(showNotes);
  // the live transcript has a few lines in the lab view: the last words stay in view
  new MutationObserver(() => {
    liveEl.scrollTop = liveEl.scrollHeight;
  }).observe(liveEl, { childList: true, characterData: true, subtree: true });
}
