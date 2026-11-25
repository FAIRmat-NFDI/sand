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
    title: INFO,
    sub: [samples + (samples === 1 ? " sample" : " samples")],
    rows: inSheetOrder([...fields, ...varying]).map(row),
    unfilled: (info.unfilled || []).map((c) => c.header),
  };
}

function stepPage(step) {
  return {
    position: step.position,
    title: step.label,
    sub: [step.material, step.layer].filter(Boolean),
    rows: step.params.map(row),
    unfilled: (step.unfilled || []).map((c) => c.header),
  };
}

export function labPages(view) {
  return [infoPage(view), ...(view.steps || []).map(stepPage)];
}

// the page to show after a refresh of the sheet: the same step, or the
// first when the sheet no longer has it
export function pageIndex(pages, position) {
  const index = pages.findIndex((p) => p.position === position);
  return index < 0 ? 0 : index;
}
