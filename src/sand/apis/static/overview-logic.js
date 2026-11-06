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

// How the lab writes a value. The unit says what a number is ("3 mbar",
// "250 nm"), so the column's measure word goes; the other words stay as
// a short qualifier only where needed: for a time or a temperature
// standing alone ("source 120 °C"), or when the card has the unit twice
// ("rotation 1 min · rest 10 min"). A category column's value speaks for
// itself ("milled", "N2"); a yes/no column is its words, or "no" and them.
// A few columns have the short name the lab uses ("RH").
const MEASURE_WORDS = new Set(["time", "temperature", "duration", "speed", "rate", "distance", "pressure",
  "volume", "thickness", "ratio", "concentration", "area", "power", "voltage", "current", "flow",
  "frequency", "acceleration", "amount", "mass", "weight", "length", "width", "height", "size"]);
const CATEGORY_WORDS = new Set(["state", "type", "mode", "atmosphere", "athmosphere", "method", "gas"]);
const TIME_LIKE = new Set(["time", "temperature", "duration"]);
const SHORT = { relative_humidity: "RH" };
const FLAGS = { yes: true, no: false, true: true, false: false };

// "Source" -> "source", but "UV-Ozone", "PbI2" and "RH" as they are
function lower(word) {
  return /^[A-Z][a-z]+$/.test(word) ? word.toLowerCase() : word;
}

// a column's words without trailing numbers: "Substrate temperature 2"
function wordsOf(param) {
  return String(param.name).trim().split(/\s+/).filter((w) => !/^\d+$/.test(w));
}

function qualifier(param, drop) {
  const short = SHORT[fieldKey(param.name)];
  if (short) return short;
  return wordsOf(param)
    .filter((w) => !drop.has(w.toLowerCase()) && !MEASURE_WORDS.has(w.toLowerCase()) && !CATEGORY_WORDS.has(w.toLowerCase()))
    .map(lower).join(" ");
}

function isTimeLike(param) {
  return wordsOf(param).some((w) => TIME_LIKE.has(w.toLowerCase()));
}

function isCategory(param) {
  return wordsOf(param).some((w) => CATEGORY_WORDS.has(w.toLowerCase()));
}

// a parameter as one value of a line; `drop` are the words the line's
// label already says, `alone` is a value with no label before it
function valueOf(p, changed, { drop = new Set(), alone = false, unitCount = new Map() } = {}) {
  const value = { text: valueCell(p), varies: p.values.length > 1, changed: changeNote(p.column_id, changed) };
  const one = p.values.length === 1 ? String(p.values[0].value ?? "").trim().toLowerCase() : null;
  if (p.type === "text" && one !== null && one in FLAGS) {
    const words = wordsOf(p).map(lower).join(" ");
    value.text = FLAGS[one] ? words : "no " + words;
    return value;
  }
  let needed;
  if (SHORT[fieldKey(p.name)]) needed = true; // "RH 30 %"
  else if (p.type === "text") needed = !isCategory(p) && !isNameColumn(p) && !isNote(p);
  else if (!p.unit) needed = true; // a bare number says nothing by itself
  else needed = (alone && isTimeLike(p)) || (unitCount.get(p.unit) || 0) > 1;
  const words = needed ? qualifier(p, drop) : "";
  if (words) value.text = words + " " + value.text;
  return value;
}

function firstWord(name) {
  return String(name).trim().split(/\s+/)[0];
}

// a numbered family of a name and one bare ratio: "MAI" and 3
function ratioOf(name, rest) {
  return name && rest.length === 1 && !rest[0].unit && rest[0].values.length === 1
    && /ratio/.test(fieldKey(rest[0].name)) ? { name, ratio: rest[0] } : null;
}

// One step card: the step type as headline, the material with its
// layer and tool under it, and lines written the way the lab notes a
// step, in the sheet's order. A column family is one line behind its
// name ("Ethanol  80 uL", "PbI2  250 nm · 1 angstrom/s"); columns that
// share their first word are one line behind it ("annealing  10 min ·
// 100 °C · N2"); ratios read "MAI:MABr 3:1"; what is left runs together
// ("3 mbar · source 120 °C · milled"); a note stands alone at the end.
// Each value keeps its column: a varying one is a chip, a changed one
// carries its old value.
export function stepCard(step, changed = []) {
  const head = {};
  const families = new Map(); // "group:index" or column id -> params, in sheet order
  const order = new Map(step.params.map((p, i) => [p, i]));
  const unitCount = new Map();
  for (const p of step.params) {
    const role = HEADLINE[fieldKey(p.name)];
    if (role && p.values.length === 1 && !head[role]) {
      head[role] = p.values[0].value;
      continue;
    }
    if (p.unit) unitCount.set(p.unit, (unitCount.get(p.unit) || 0) + 1);
    const key = p.family ? (p.family.group || "") + ":" + p.family.index : p.column_id;
    if (!families.has(key)) families.set(key, []);
    families.get(key).push(p);
  }
  const stepWord = firstWord(step.step_type).toLowerCase();
  const items = []; // {at, line} or {at, single: param}; singles run together
  const ratios = [];
  const text = (p) => String(p.values[0].value);
  for (const params of families.values()) {
    const family = params[0].family;
    const label = family && FAMILY_LABELS[family.group];
    const names = params.filter((p) => isNameColumn(p) && p.values.length === 1);
    // numbered columns without a group share a key by number alone, so
    // two names there ("Material name 1", "Source material name 1") are
    // two things: nothing merges
    const name = names.length === 1 || (names.length && label) ? names[0] : null;
    const rest = params.filter((p) => p !== name);
    const at = order.get(params[0]);
    const ratio = !label && ratioOf(name, rest);
    if (ratio) {
      ratios.push({ at, ...ratio });
    } else if (name && rest.length) {
      const drop = new Set(wordsOf(name).map((w) => w.toLowerCase()));
      const values = rest.map((p) => valueOf(p, changed, { drop, unitCount })).filter((v) => v.text);
      // the material's own line needs no label: the card names it already
      items.push({ at, line: { label: text(name) === head.material ? "" : text(name), values } });
    } else if (name) {
      items.push({ at, single: name, value: valueOf(name, changed) }); // "Ethanol"
    } else if (label && rest.length > 1) {
      const siblings = [...families.keys()].filter((k) => k.startsWith(family.group + ":")).length;
      const values = rest.map((p) => valueOf(p, changed, { unitCount }));
      items.push({ at, line: { label: siblings > 1 ? familyLabel(family, families) : "", values } });
    } else {
      for (const p of params) items.push({ at: order.get(p), single: p });
    }
  }
  if (ratios.length) {
    ratios.sort((x, y) => x.at - y.at);
    items.push({
      at: ratios[0].at,
      line: {
        label: ratios.map((r) => text(r.name)).join(":"),
        values: [{
          text: ratios.map((r) => text(r.ratio)).join(":"),
          varies: false,
          changed: ratios.map((r) => changeNote(r.ratio.column_id, changed)).filter(Boolean).join("; "),
        }],
      },
    });
  }
  // singles that share their first word: one line behind the word, or
  // behind nothing when it is the step's own ("Annealing" in an annealing)
  const groups = new Map();
  const singles = items.filter((i) => i.single && !i.value);
  for (const item of singles) {
    if (isNote(item.single) && item.single.values.length === 1) continue;
    const word = firstWord(item.single.name);
    const key = word.toLowerCase();
    if (!groups.has(key)) groups.set(key, { word, items: [] });
    groups.get(key).items.push(item);
  }
  const notes = [];
  for (const item of singles) {
    const p = item.single;
    if (isNote(p) && p.values.length === 1) {
      notes.push({ label: "", note: true, values: [valueOf(p, changed)] });
      items.splice(items.indexOf(item), 1);
      continue;
    }
    const group = groups.get(firstWord(p.name).toLowerCase());
    if (group.items.length === 1) {
      item.value = valueOf(p, changed, { alone: true, unitCount });
      continue;
    }
    if (group.items[0] !== item) {
      items.splice(items.indexOf(item), 1);
      continue;
    }
    const drop = new Set([group.word.toLowerCase()]);
    const line = {
      label: group.word.toLowerCase() === stepWord ? "" : lower(group.word),
      values: group.items.map((g) => valueOf(g.single, changed, { drop, unitCount })),
    };
    delete item.single;
    item.line = line;
  }
  items.sort((x, y) => x.at - y.at);
  // what is left runs together, in the sheet's order
  const lines = [];
  let run = null;
  for (const item of items) {
    if (item.line) {
      lines.push(item.line);
      run = null;
    } else if (run) {
      run.values.push(item.value);
    } else {
      run = { label: "", values: [item.value] };
      lines.push(run);
    }
  }
  lines.push(...notes);
  return {
    position: step.position,
    headline: step.step_type,
    material: head.material || "",
    sub: [head.layer, head.tool].filter(Boolean).join(" · "),
    empty: step.empty,
    varies: step.varies,
    lines,
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
