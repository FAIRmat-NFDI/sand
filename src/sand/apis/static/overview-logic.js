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

// what the confirmed edits did to a column, for the hover: "was 3000"
// from one old value, else "edited on N samples"
function changeNote(columnId, changed) {
  const cells = changed.filter((c) => c.column_id === columnId);
  if (!cells.length) return "";
  const olds = new Set(cells.map((c) => c.old));
  if (olds.size === 1) return "was " + [...olds][0];
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

function isNote(param) {
  return /(^|_)(notes?|comments?|remarks?)($|_)/.test(fieldKey(param.name));
}

// a parameter as one value of a line
function valueOf(p, changed) {
  return { text: valueCell(p), varies: p.values.length > 1, changed: changeNote(p.column_id, changed) };
}

function firstWord(name) {
  return String(name).trim().split(/\s+/)[0];
}

// One step card: the headline (material, or the step type), its
// subtitle (layer · tool), and lines written the way the lab notes a
// step: a label and the values after it. The measures of one column
// family make one line behind the family's name ("Ethanol  80 uL",
// "PbI2  25 °C · 250 nm · 1 angstrom/s"), or behind the family's label
// without one ("Spin  3000 rpm · 30 s"); columns that share their first
// word make one line ("Annealing  10 min · 100 °C · N2"); a note stands
// alone at the end. Each value keeps its column: a varying one is a
// chip, a changed one carries its old value.
export function stepCard(step, changed = []) {
  const head = {};
  const families = new Map(); // "group:index" or column id -> params, in sheet order
  const order = new Map(step.params.map((p, i) => [p, i]));
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
  const lines = []; // with `at`, the first column's place, to keep the sheet's order
  const singles = [];
  for (const params of families.values()) {
    const family = params[0].family;
    const label = family && FAMILY_LABELS[family.group];
    const names = params.filter((p) => isNameColumn(p) && p.values.length === 1);
    // numbered columns without a group share a key by number alone, so
    // two names there ("Material name 1", "Source material name 1") are
    // two things: nothing merges
    const name = names.length === 1 || (names.length && label) ? names[0] : null;
    const rest = params.filter((p) => p !== name);
    if (!family || !(name || (label && rest.length > 1))) {
      singles.push(...params);
      continue;
    }
    const values = rest.map((p) => valueOf(p, changed)).filter((v) => v.text);
    const at = order.get(params[0]);
    if (name && !values.length) {
      // only the name is filled: "Solvent  Ethanol"
      lines.push({ at, label: label ? familyLabel(family, families) : name.name, values: [valueOf(name, changed)] });
    } else {
      // the material's own line needs no label under its headline
      const text = name ? name.values[0].value : familyLabel(family, families);
      lines.push({ at, label: text === head.material ? "" : text, values });
    }
  }
  // columns that share their first word: one line, the word as label,
  // or none when it is the step's own word ("Annealing" in an annealing)
  const groups = new Map();
  for (const p of singles) {
    if (isNote(p) && p.values.length === 1) continue;
    const word = firstWord(p.name);
    const key = word.toLowerCase();
    if (!groups.has(key)) groups.set(key, { word, params: [] });
    groups.get(key).params.push(p);
  }
  const stepWord = firstWord(step.step_type).toLowerCase();
  for (const [key, group] of groups) {
    const at = order.get(group.params[0]);
    if (group.params.length === 1) {
      const p = group.params[0];
      lines.push({ at, label: p.name, values: [valueOf(p, changed)] });
    } else {
      lines.push({ at, label: key === stepWord ? "" : group.word, values: group.params.map((p) => valueOf(p, changed)) });
    }
  }
  lines.sort((x, y) => x.at - y.at);
  for (const p of singles) {
    if (isNote(p) && p.values.length === 1) lines.push({ label: "", note: true, values: [valueOf(p, changed)] });
  }
  return {
    position: step.position,
    type: step.step_type,
    headline: head.material || step.step_type,
    kicker: head.material ? step.step_type : "",
    sub: [head.layer, head.tool].filter(Boolean).join(" · "),
    empty: step.empty,
    varies: step.varies,
    lines: lines.map(({ at, ...line }) => line),
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
