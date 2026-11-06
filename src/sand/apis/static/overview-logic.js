// The sheet overview's content from the sheet view: one card per step
// with its filled parameters folded into readable rows, the samples
// grouped by what varies, and the summary line. Pure functions: tested
// with node. Nothing is invented: every value is the sheet's, the only
// vocabulary added is the label of a column family without a name
// column ("Spin" for a rotation step).

import { fieldKey } from "./sheet-header-logic.js";

// columns that make a step's headline rather than a row
const HEADLINE = { material_name: "material", layer_type: "layer", tool_gb_name: "tool" };
// a family's label when no text column of it names the thing
const FAMILY_LABELS = { rotation_steps: "Spin", cleaning_steps: "Cleaning", solvents: "Solvent", solutes: "Solute" };
const RANGE_FROM = 4; // this many distinct numbers read better as "900–2700"
export const EMPTY = "(empty)";

function isNumber(text) {
  return /^-?\d+(\.\d+)?$/.test(text);
}

function withUnit(text, unit) {
  return unit && text ? text + " " + unit : text;
}

// a parameter's value cell: one value, "120 / 115", "900–2700" for many
// numbers, with the unit once; "· some empty" when cells are empty
export function valueCell(param) {
  const texts = param.values.filter((v) => v.value !== null).map((v) => v.value);
  const empties = param.values.some((v) => v.value === null);
  let text = "";
  if (texts.length === 1) {
    text = texts[0];
  } else if (texts.length >= RANGE_FROM && texts.every(isNumber)) {
    const numbers = texts.map(Number);
    text = Math.min(...numbers) + "–" + Math.max(...numbers);
  } else if (texts.length) {
    text = texts.join(" / ");
  }
  text = withUnit(text, param.unit);
  if (empties && texts.length) text += " · some empty";
  return text;
}

// what the confirmed edits did to a row's columns, for the hover: "was
// 3000" for one column from one old value, else "edited on N samples"
function changeNote(columnIds, changed) {
  const cells = changed.filter((c) => columnIds.has(c.column_id));
  if (!cells.length) return "";
  const columns = new Set(cells.map((c) => c.column_id));
  const olds = new Set(cells.map((c) => c.old));
  if (columns.size === 1 && olds.size === 1) return "was " + [...olds][0];
  const rows = new Set(cells.map((c) => c.row)).size;
  return "edited on " + rows + (rows === 1 ? " sample" : " samples");
}

function familyLabel(family, families) {
  const base = FAMILY_LABELS[family.group];
  const siblings = [...families.keys()].filter((k) => k.startsWith(family.group + ":")).length;
  return siblings > 1 ? base + " " + family.index : base;
}

// the column that names a family's thing: "Solvent 1 name", "Material
// name 2", "Source material name 1", or a cleaning step's bare "Solvent 1"
function isNameColumn(param) {
  const key = fieldKey(param.name);
  return param.type === "text" && (/_name(_\d+)?$/.test(key) || /^solvent_\d+$/.test(key));
}

function columnRow(p, changed) {
  return {
    varies: p.values.length > 1,
    changed: changeNote(new Set([p.column_id]), changed),
    label: p.name,
    value: valueCell(p),
  };
}

// One step card: the headline (material, or the step type), its
// subtitle (layer · tool), and rows. The measures of one family fold
// into one row behind the family's name column ("Ethanol  80 uL",
// "PbI2  250 nm · 1 angstrom/s") or behind the family's label when the
// name is missing ("Spin  3000 rpm · 30 s"); every other column (a
// type, a unitless number, a varying name) is a labelled row of its own.
export function stepCard(step, changed = []) {
  const head = {};
  const rows = [];
  const families = new Map(); // "group:index" or column id -> params, in sheet order
  for (const p of step.params) {
    const role = HEADLINE[fieldKey(p.name)];
    if (role && p.values.length === 1 && !head[role]) {
      head[role] = p.values[0].value;
      continue;
    }
    const key = p.family ? (p.family.group || "") + ":" + p.family.index : p.column_id;
    if (!families.has(key)) families.set(key, []);
    families.get(key).push(p);
  }
  for (const params of families.values()) {
    const family = params[0].family;
    const label = family && FAMILY_LABELS[family.group];
    const names = params.filter((p) => isNameColumn(p) && p.values.length === 1);
    // numbered columns without a group share a key by number alone, so
    // two names there ("Material name 1", "Source material name 1") are
    // two things: no merging
    const name = names.length === 1 || (names.length && label) ? names[0] : null;
    const measures = params.filter((p) => p !== name && p.unit);
    const merge = family && ((label && (name || measures.length > 1)) || (name && measures.length));
    if (!merge) {
      for (const p of params) rows.push(columnRow(p, changed));
      continue;
    }
    const merged = name ? [name, ...measures] : measures;
    const value = measures.map(valueCell).filter(Boolean).join(" · ");
    rows.push({
      varies: merged.some((p) => p.values.length > 1),
      changed: changeNote(new Set(merged.map((p) => p.column_id)), changed),
      // only the name is filled: "Solvent  Ethanol"
      label: value ? (name ? name.values[0].value : familyLabel(family, families)) : familyLabel(family, families),
      value: value || valueCell(name),
    });
    for (const p of params) if (!merged.includes(p)) rows.push(columnRow(p, changed));
  }
  return {
    position: step.position,
    type: step.step_type,
    headline: head.material || step.step_type,
    kicker: head.material ? step.step_type : "",
    sub: [head.layer, head.tool].filter(Boolean).join(" · "),
    empty: step.empty,
    varies: step.varies,
    rows,
  };
}

function labelsOf(rows, byRow) {
  return rows.map((r) => byRow.get(r) ?? "row " + r);
}

function intersect(a, b) {
  const set = new Set(b);
  return a.filter((r) => set.has(r));
}

function axis(variation) {
  return { name: variation.name, unit: variation.unit, step: variation.step };
}

function valueLabel(value, unit) {
  return value === null ? EMPTY : withUnit(value, unit);
}

// The samples grouped by what varies. One varying column: its values,
// each with its samples. Two or more: a cross of the column with the
// fewest values (rows) and the one with the most values that meets
// every row (columns), as the design groups deposition time by
// substrate temperature; the other varying columns are only named.
// Without a crossing pair, each varying column is its own block.
export function sampleGroups(view) {
  const byRow = new Map((view.info?.rows || []).map((r) => [r.row, r.label]));
  const vars = (view.variations || []).filter((v) => v.values.length > 1);
  if (!vars.length) return { kind: "none" };
  const block = (v) => ({
    ...axis(v),
    groups: v.values.map((g) => ({ value: valueLabel(g.value, v.unit), labels: labelsOf(g.rows, byRow) })),
  });
  if (vars.length === 1) return { kind: "flat", blocks: [block(vars[0])], also: [] };
  // fewest values first; among equals, a column with empty cells (a
  // note on some samples) after the fully filled ones
  const hasEmpty = (v) => v.values.some((g) => g.value === null);
  const sorted = [...vars].sort((x, y) =>
    x.values.length - y.values.length || Number(hasEmpty(x)) - Number(hasEmpty(y)));
  const crosses = (a, b) =>
    a.values.every((ag) => b.values.every((bg) => intersect(ag.rows, bg.rows).length > 0));
  for (const a of sorted) {
    const crossing = sorted.filter((b) => b !== a && b.values.length >= a.values.length && crosses(a, b));
    if (!crossing.length) continue;
    const b = crossing[crossing.length - 1]; // the most values among them
    const rows = a.values.map((ag) => ({
      value: valueLabel(ag.value, a.unit),
      cells: b.values.map((bg) => ({
        value: valueLabel(bg.value, b.unit),
        labels: labelsOf(intersect(bg.rows, ag.rows), byRow),
      })),
    }));
    const also = vars.filter((v) => v !== a && v !== b).map((v) => v.name);
    return { kind: "cross", a: axis(a), b: axis(b), columns: b.values.map((g) => valueLabel(g.value, b.unit)), rows, also };
  }
  return { kind: "flat", blocks: vars.map(block), also: [] };
}

// the one line the folded overview keeps: "9 steps · step 5 varies · 2 to check"
export function overviewSummary(view) {
  const steps = view.steps || [];
  const parts = [steps.length + (steps.length === 1 ? " step" : " steps")];
  const varying = steps.filter((s) => s.varies).map((s) => s.position);
  if (varying.length === 1) parts.push("step " + varying[0] + " varies");
  else if (varying.length) parts.push("steps " + varying.join(", ") + " vary");
  const warnings = (view.warnings || []).length;
  if (warnings) parts.push(warnings + " to check");
  const changed = new Set((view.changed || []).map((c) => c.column_id + ":" + c.row)).size;
  if (changed) parts.push(changed + (changed === 1 ? " cell edited" : " cells edited"));
  return parts.join(" · ");
}
