// The sheet overview between the header and the workspace: the steps as
// cards, the samples grouped by what varies, the warnings line. Foldable
// to its summary line; the fold is remembered in this browser, and until
// the user folds or unfolds it, the overview is open while the sheet has
// no updates and folded once it has.

import { overviewSummary, sampleGroups, stepCard } from "./overview-logic.js";

const section = document.getElementById("sheet-overview");
const summaryEl = document.getElementById("overview-summary");
const toggleBtn = document.getElementById("overview-toggle");
const toggleIcon = document.getElementById("overview-toggle-icon");
const toggleLabel = document.getElementById("overview-toggle-label");
const body = document.getElementById("overview-body");
const warningsEl = document.getElementById("overview-warnings");
const stepsTitle = document.getElementById("steps-title");
const stepGrid = document.getElementById("step-grid");
const samplesBlock = document.getElementById("samples-block");
const samplesNote = document.getElementById("samples-note");
const samplesBox = document.getElementById("samples-box");
const OPEN_KEY = "sand.overview.open";
const LONG_VALUE = 24; // characters; a longer value may wrap inside

let chosen = null; // true/false once the user folded or unfolded, else null
let updates = null; // the shown sheet's update count, null while not known

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setOpen(open) {
  body.hidden = !open;
  toggleBtn.setAttribute("aria-expanded", String(open));
  toggleIcon.textContent = open ? "expand_less" : "expand_more";
  toggleLabel.textContent = open ? "Hide" : "Show";
}

// the user's choice; else open without updates, folded with some. While
// the count is not known yet (the updates are loading) the fold stays as
// it is, so the overview does not open and then fold on every selection.
function applyFold() {
  if (chosen !== null) setOpen(chosen);
  else if (updates !== null) setOpen(!updates);
}

// the Sheet card tells how many updates the sheet has (null: not known)
export function setOverviewUpdates(count) {
  updates = count;
  if (chosen === null) applyFold();
}

function renderStep(card) {
  const article = el("article", "step-card");
  if (card.varies) article.classList.add("step-varies");
  if (card.empty) article.classList.add("step-empty");
  const top = el("div", "step-top");
  top.append(el("span", "step-number", String(card.position ?? "")));
  article.append(top);
  const title = el("div", "step-title");
  title.append(el("span", "step-headline", card.headline));
  if (card.material || card.sub) {
    const sub = el("span", "step-sub");
    if (card.material) sub.append(el("span", "step-material", card.material));
    if (card.material && card.sub) sub.append(el("span", "line-sep", "·"));
    if (card.sub) sub.append(document.createTextNode(card.sub));
    title.append(sub);
  }
  article.append(title);
  if (card.empty) {
    article.append(el("p", "step-sub", "No data"));
    return article;
  }
  const lines = el("div", "step-lines");
  for (const line of card.lines) {
    const p = el("p", line.note ? "step-line step-note" : "step-line");
    if (line.label) p.append(el("span", "line-label", line.label));
    line.values.forEach((value, i) => {
      if (i) p.append(el("span", "line-sep", "·"));
      if (value.groups) {
        // a varying value: its word, then a chip per value with its samples
        if (value.word) p.append(el("span", "line-word", value.word));
        for (const group of value.groups) {
          const chip = el("span", "line-value value-varies", group.text);
          chip.append(el("span", "value-samples", "(" + group.samples + ")"));
          if (value.changed) {
            chip.classList.add("value-changed");
            chip.title = value.changed;
          }
          p.append(chip);
        }
        return;
      }
      const span = el("span", "line-value", value.text);
      if (value.text.length > LONG_VALUE) span.classList.add("long");
      if (value.changed) {
        span.classList.add("value-changed");
        span.title = value.changed;
      }
      p.append(span);
    });
    lines.append(p);
  }
  article.append(lines);
  return article;
}

function chips(labels, tone) {
  const box = el("div", "sample-chips");
  for (const label of labels) box.append(el("span", "sample-chip tone-" + tone, label));
  return box;
}

function axisText(axis) {
  return axis.name + (axis.unit ? " [" + axis.unit + "]" : "");
}

function renderSamples(groups) {
  samplesBox.replaceChildren();
  samplesBlock.hidden = groups.kind === "none";
  if (groups.kind === "none") return;
  if (groups.kind === "flat") {
    samplesNote.textContent = groups.blocks.map((b) => axisText(b) + " (" + b.step + ")").join(" · ");
    groups.blocks.forEach((block, i) => {
      const row = el("div", "sample-row");
      const head = el("div", "sample-axis");
      head.append(el("span", "sample-axis-value", axisText(block)));
      head.append(el("span", "sample-axis-name", block.step));
      row.append(head);
      const cells = el("div", "sample-cells");
      for (const group of block.groups) {
        const cell = el("div", "sample-cell");
        cell.append(el("span", "sample-cell-value", group.value));
        cell.append(chips(group.labels, i % 4));
        cells.append(cell);
      }
      row.append(cells);
      samplesBox.append(row);
    });
    return;
  }
  samplesNote.textContent = axisText(groups.a) + " × " + axisText(groups.b)
    + (groups.also.length ? " · also varies: " + groups.also.join(", ") : "");
  groups.rows.forEach((row, i) => {
    const line = el("div", "sample-row");
    const head = el("div", "sample-axis");
    head.append(el("span", "sample-axis-value", row.value));
    head.append(el("span", "sample-axis-name", groups.a.name));
    line.append(head);
    const cells = el("div", "sample-cells");
    cells.style.setProperty("--cells", String(row.cells.length));
    for (const cell of row.cells) {
      const box = el("div", "sample-cell");
      box.append(el("span", "sample-cell-value", cell.value));
      box.append(chips(cell.labels, i % 4));
      cells.append(box);
    }
    line.append(cells);
    samplesBox.append(line);
  });
}

// The overview of the shown sheet (the sheet view), or none.
export function showOverview(view) {
  section.hidden = !view;
  if (!view) return;
  summaryEl.textContent = overviewSummary(view);
  const warnings = view.warnings || [];
  warningsEl.replaceChildren();
  warningsEl.hidden = warnings.length === 0;
  if (warnings.length) {
    warningsEl.append(el("span", "material-icons", "error_outline"));
    warningsEl.append(el("span", "", warnings.join(" · ")));
  }
  const steps = view.steps || [];
  stepsTitle.textContent = "Fabrication sequence · " + steps.length + (steps.length === 1 ? " step" : " steps");
  stepGrid.replaceChildren(...steps.map((step) => renderStep(stepCard(step, view.changed || [], view.info?.rows || []))));
  renderSamples(sampleGroups(view));
  applyFold();
}

export function initOverview() {
  try {
    const stored = localStorage.getItem(OPEN_KEY);
    if (stored === "true" || stored === "false") chosen = stored === "true";
  } catch { /* ignore */ }
  applyFold();
  toggleBtn.addEventListener("click", () => {
    chosen = body.hidden;
    setOpen(chosen);
    try { localStorage.setItem(OPEN_KEY, String(chosen)); } catch { /* ignore */ }
  });
}
