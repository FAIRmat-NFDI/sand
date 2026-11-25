// The lab view: the page reduced for work at the glove box. On the left
// one step of the sheet with every column of it in view, on the right
// the Record card: whether it records, the live transcript in a big
// font, what was said before. The rest of the page is hidden
// (styles.css, body.lab-mode). The user picks the step before the
// hands go in.

import { selectedExperiment } from "./experiments.js";
import { addInputsListener } from "./inputs.js";
import { labNotes, labPages, pageIndex, rowLayout } from "./lab-logic.js";
import { clearLiveTranscript } from "./live-transcript.js";

const enterBtn = document.getElementById("lab-btn");
const exitBtn = document.getElementById("lab-exit-btn");
const sheetEl = document.getElementById("lab-sheet");
const titleEl = document.getElementById("lab-step-title");
const subEl = document.getElementById("lab-step-sub");
const rowsEl = document.getElementById("lab-step-rows");
const unfilledEl = document.getElementById("lab-step-unfilled");
const chipsEl = document.getElementById("lab-step-chips");
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
    fit(); // the panel had no size while hidden
  }
}

const MANY_UNFILLED = 8; // more empty columns than this are named smaller
const SHRINK = 0.85; // a row too long for its width: this much smaller at most, then its value wraps

// The rows' font from the room a row gets, so that every column of the
// step is in view without scrolling; then a little smaller until no
// name or value is cut, and a value still too long (a note) wraps.
function fit() {
  const style = rowsEl.style;
  const per = Number(style.getPropertyValue("--per")) || 1;
  const cols = Number(style.getPropertyValue("--cols")) || 1;
  const most = (cols === 1 ? 0.023 : 0.0165) * window.innerWidth;
  const roomy = Math.max(11, Math.min(most, (rowsEl.clientHeight / per) * 0.56));
  let size = roomy;
  rowsEl.classList.remove("lab-rows-wrap");
  style.setProperty("--row-font", size + "px");
  // measured as text: half a pixel too much already shows as "…"
  const range = document.createRange();
  const cut = () => [...rowsEl.querySelectorAll(".lab-row-name, .lab-row-value")].some((part) => {
    range.selectNodeContents(part);
    return range.getBoundingClientRect().width > part.getBoundingClientRect().width;
  });
  while (size - 1 >= roomy * SHRINK && cut()) {
    size -= 1;
    style.setProperty("--row-font", size + "px");
  }
  if (cut()) rowsEl.classList.add("lab-rows-wrap");
}

function noteTime(datetime) {
  const parsed = new Date(datetime || "");
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// What was said before in normal font, the newest first, under the live
// transcript. A note that got its text is no longer the big live text:
// it would be shown twice.
function showNotes(experiment, items) {
  const notes = labNotes(items);
  notesEl.replaceChildren(...notes.map((note) => {
    const li = el("li", note.pending ? "lab-note lab-note-pending" : "lab-note");
    li.append(el("span", "lab-note-time", noteTime(note.datetime)), el("span", "lab-note-text", note.text));
    return li;
  }));
  const now = notes.filter((n) => !n.pending).length;
  if (now > transcribed && document.body.classList.contains("lab-mode")) clearLiveTranscript();
  transcribed = now;
}

function render() {
  const page = pages[index];
  if (!page) return;
  titleEl.textContent = page.title;
  subEl.textContent = page.sub.join(" · ");
  const layout = rowLayout(page.rows.length);
  rowsEl.style.setProperty("--cols", layout.cols);
  rowsEl.style.setProperty("--per", layout.per);
  rowsEl.replaceChildren(...page.rows.map((row) => {
    const item = el("div", "lab-row");
    item.append(el("span", "lab-row-name", row.name), el("span", "lab-row-value", row.value));
    return item;
  }));
  rowsEl.hidden = page.rows.length === 0;
  unfilledEl.hidden = page.unfilled.length === 0;
  unfilledEl.style.setProperty("--chip-font", (page.unfilled.length > MANY_UNFILLED ? 0.95 : 1.2) + "vw");
  chipsEl.replaceChildren(...page.unfilled.map((name) => el("span", "lab-chip", name)));
  prevBtn.disabled = index === 0;
  nextBtn.disabled = index === pages.length - 1;
  placeEl.textContent = "Step " + page.position + " of " + pages[pages.length - 1].position;
  fit();
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
  window.addEventListener("resize", fit);
  document.fonts?.ready.then(fit); // the page's font is wider than the fallback
  // the live transcript has a few lines in the lab view: the last words stay in view
  new MutationObserver(() => {
    liveEl.scrollTop = liveEl.scrollHeight;
  }).observe(liveEl, { childList: true, characterData: true, subtree: true });
}
