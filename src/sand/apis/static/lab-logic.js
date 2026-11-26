// The lab view's content from the sheet view: one page per step, the
// Experiment Info first as step 0. A page names every column of its
// step as the sheet does, the empty ones too: the user says these names.
// Pure functions: tested with node.

import { valueCell } from "./overview-logic.js";

const SOME_EMPTY = " · some empty"; // as valueCell ends a partly filled column

const SHOWN_TEXTS = 3; // more different texts than this are counted, not listed
const INFO = "Experiment Info";

// "120 / 115", "900–2700 rpm" as the overview writes them; many
// different texts ("14 different values") would fill the panel
function valueOf(column) {
  const texts = column.values.filter((v) => v.value !== null);
  const cell = valueCell(column);
  if (texts.length > SHOWN_TEXTS && cell.includes(" / ")) {
    return texts.length + " different values" + (cell.endsWith(SOME_EMPTY) ? SOME_EMPTY : "");
  }
  return cell;
}

function row(column) {
  return { name: column.name, value: valueOf(column) };
}

// the column ids count up in sheet order
function inSheetOrder(columns) {
  return [...columns].sort((a, b) => a.column_id.localeCompare(b.column_id));
}

function infoPage(view) {
  const info = view.info || {};
  const steps = new Set((view.steps || []).map((s) => s.label));
  const fields = (info.fields || []).map((f) => ({ ...f, values: [{ value: f.value }] }));
  // an Experiment Info column that varies is no field
  const varying = (view.variations || []).filter((v) => !steps.has(v.step));
  const samples = info.samples ?? 0;
  return {
    position: 0,
    label: INFO,
    title: INFO,
    sub: [samples + (samples === 1 ? " sample" : " samples")],
    rows: inSheetOrder([...fields, ...varying]).map(row),
    unfilled: (info.unfilled || []).map((c) => c.header),
  };
}

function stepPage(step) {
  return {
    position: step.position,
    label: step.label,
    // "2. Spin Coating": the number says which of the spin coatings
    title: step.step_type ? step.position + ". " + step.step_type : step.label,
    sub: [step.material, step.layer].filter(Boolean),
    rows: step.params.map(row),
    unfilled: (step.unfilled || []).map((c) => c.header),
  };
}

export function labPages(view) {
  return [infoPage(view), ...(view.steps || []).map(stepPage)];
}

const ONE_COLUMN = 9; // more rows than this go in two columns

// How a page's rows are laid out so that all are in view: in sheet
// order down one column, or down two.
export function rowLayout(count) {
  const cols = count > ONE_COLUMN ? 2 : 1;
  return { cols, per: Math.max(1, Math.ceil(count / cols)) };
}

// The notes so far as the lab view lists them, the newest first: what
// was said, or where a recording's transcription stands.
export function labNotes(items) {
  return [...items]
    .sort((a, b) => (Date.parse(b.datetime) || 0) - (Date.parse(a.datetime) || 0))
    .map((item) => ({
      id: item.entry_id,
      datetime: item.datetime || null,
      text: item.text || (item.status === "FAILED" ? "transcription failed" : "transcribing..."),
      pending: !item.text,
    }));
}

// The notes recorded while this step was shown: by their label (see
// labStepLabel), whatever step they speak of. A note made in the full
// view has no step and is in no step's history.
export function notesOfStep(items, label) {
  return items.filter((item) => (item.label || "") === label);
}

// the page to show after a refresh of the sheet: the same step, or the
// first when the sheet no longer has it
export function pageIndex(pages, position) {
  const index = pages.findIndex((p) => p.position === position);
  return index < 0 ? 0 : index;
}
