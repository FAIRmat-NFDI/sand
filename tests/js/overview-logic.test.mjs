import assert from "node:assert/strict";
import { test } from "node:test";

import {
  EMPTY, overviewSummary, sampleGroups, stepCard, valueCell,
} from "../../src/sand/apis/static/overview-logic.js";

let nextColumn = 0;

// a sheet-view param: `values` as [value, rows] pairs
function param(name, values, { unit = null, family = null, type = "text" } = {}) {
  nextColumn += 1;
  return {
    column_id: "C" + String(nextColumn).padStart(3, "0"),
    header: unit ? name + " [" + unit + "]" : name,
    name,
    unit,
    type,
    family,
    formula: false,
    values: values.map(([value, rows]) => ({ value, rows, samples: "" })),
  };
}

const ALL = [3, 4, 5, 6, 7, 8];
const FIRST = [3, 4, 5];
const LAST = [6, 7, 8];
const one = (value) => [[value, ALL]];
const num = (name, value, unit, family) => param(name, one(value), { unit, family, type: "int" });

function step(position, stepType, params, { empty = false } = {}) {
  return {
    position,
    step_type: stepType,
    label: position + ": " + stepType,
    material: null,
    layer: null,
    empty,
    varies: params.some((p) => p.values.length > 1),
    params,
  };
}

test("a value cell: one value, a few, a range of many, the unit once", () => {
  assert.equal(valueCell(param("Time", one("900"), { unit: "s" })), "900 s");
  assert.equal(valueCell(param("T", [["120", FIRST], ["115", LAST]], { unit: "°C" })), "120 / 115 °C");
  assert.equal(
    valueCell(param("Time", [["900", [3]], ["1200", [4]], ["1500", [5]], ["1800", [6]]], { unit: "s" })),
    "900–1800 s");
  // texts never make a range
  assert.equal(valueCell(param("V", [["a", [3]], ["b", [4]], ["c", [5]], ["d", [6]]])), "a / b / c / d");
  assert.equal(valueCell(param("Notes", [["x", FIRST], [null, LAST]])), "x · some empty");
});

// a card's lines as "label  v1 · v2" texts
const texts = (card) => card.lines.map((l) =>
  (l.note ? "“" : "") + (l.label ? l.label + "  " : "") + l.values.map((v) => v.text).join(" · ") + (l.note ? "”" : ""));

test("a step card: headline from the material, layer and tool as subtitle, lines like notes", () => {
  const rotation = { group: "rotation_steps", index: 1 };
  const solvent = { group: "solvents", index: 1 };
  const spin = step(2, "Spin Coating", [
    param("Material name", one("Me-4PACz")),
    param("Layer type", one("HTL")),
    param("Tool/GB name", one("box1")),
    param("Solvent 1 name", one("Ethanol"), { family: solvent }),
    num("Solvent 1 volume", "50", "uL", solvent),
    num("Rotation speed", "3000", "rpm", rotation),
    num("Rotation time", "30", "s", rotation),
    num("Annealing time", "10", "min"),
    num("Annealing temperature", "100", "°C"),
    param("Annealing athmosphere", one("N2")),
    param("Notes", one("standard recipe")),
  ]);
  const card = stepCard(spin);
  assert.equal(card.headline, "Spin Coating");
  assert.equal(card.material, "Me-4PACz");
  assert.equal(card.sub, "HTL · box1");
  assert.deepEqual(texts(card), [
    "Ethanol  50 uL",
    "3000 rpm · 30 s",
    "annealing  10 min · 100 °C · N2",
    "“standard recipe”",
  ]);
  assert.equal(card.lines[3].note, true);
  assert.equal(card.varies, false);
  assert.equal(card.empty, false);
});

test("the unit says what a number is; a time or temperature alone keeps its word; loose values run together", () => {
  const rotation = { group: "rotation_steps", index: 1 };
  const card = stepCard(step(2, "Spin Coating", [
    param("Notes", one("x")),
    num("Solution volume", "80", "uL"),
    num("Process pressure", "3", "mbar"),
    num("Rotation speed", "3000", "rpm", rotation),
    num("Rotation time", "30", "s", rotation),
    num("Annealing time", "10", "min"),
  ]));
  assert.deepEqual(texts(card), ["80 uL · 3 mbar", "3000 rpm · 30 s", "annealing 10 min", "“x”"]);
  // a unit the card has twice keeps each value's word
  const twice = stepCard(step(3, "Spin Coating", [
    param("Solvent 1 name", one("Ethanol"), { family: { group: "solvents", index: 1 } }),
    num("Solvent 1 volume", "80", "uL", { group: "solvents", index: 1 }),
    num("Solution volume", "80", "uL"),
  ]));
  assert.deepEqual(texts(twice), ["Ethanol  80 uL", "solution 80 uL"]);
});

test("without a material the step type is the headline; a lone name is just the name", () => {
  const cleaning = { group: "cleaning_steps", index: 1 };
  const card = stepCard(step(1, "Cleaning UV-Ozone", [
    param("Solvent 1", one("Acetone"), { family: cleaning }),
    num("Time 1", "600", "s", cleaning),
    param("Solvent 2", one("IPA"), { family: { group: "cleaning_steps", index: 2 } }),
    param("Solvent 1 name", one("Ethanol"), { family: { group: "solvents", index: 1 } }),
    num("UV-Ozone Time", "1200", "s"),
  ]));
  assert.equal(card.headline, "Cleaning UV-Ozone");
  assert.equal(card.material, "");
  assert.deepEqual(texts(card), ["Acetone  600 s", "IPA · Ethanol · UV-Ozone 1200 s"]);
});

test("the step's own word and the material's own line need no label; the lab's short words", () => {
  const two = { group: null, index: 2 };
  const anneal = stepCard(step(6, "Annealing", [
    num("Annealing time", "10", "min"),
    num("Annealing temperature", "130", "°C"),
    num("Relative humidity", "30", "%"),
  ]));
  assert.deepEqual(texts(anneal), ["10 min · 130 °C", "RH 30 %"]);
  const evaporation = stepCard(step(4, "Seq-Evaporation", [
    param("Material name", one("PbI2")),
    param("Material name 2", one("PbI2"), { family: two }),
    num("Thickness 2", "250", "nm", two),
    num("Rate 2", "1", "angstrom/s", two),
  ]));
  assert.equal(evaporation.headline, "Seq-Evaporation");
  assert.equal(evaporation.material, "PbI2");
  assert.deepEqual(texts(evaporation), ["250 nm · 1 angstrom/s"]);
  // a yes/no column is its words; a category's value speaks for itself
  const css = stepCard(step(5, "Close Space Sublimation", [
    param("Organic", one("yes")),
    param("Material state", one("milled")),
    num("Source temperature", "120", "°C"),
    num("Substrate temperature", "115", "°C"),
    num("Substrate source distance", "3", "mm"),
    param("Carrier gas", one("no")),
    num("Milling rotation speed", "450", "rpm"),
    num("Milling rotation time", "1", "min"),
    num("Milling rest time", "10", "min"),
  ]));
  assert.deepEqual(texts(css), [
    "organic · milled · source 120 °C",
    "substrate  115 °C · 3 mm",
    "no carrier gas",
    "milling  450 rpm · rotation 1 min · rest 10 min",
  ]);
});

test("a type column is not the family's name; a varying name stays a chip in its line", () => {
  const solute = { group: "solutes", index: 1 };
  const solvent = { group: "solvents", index: 1 };
  const withName = stepCard(step(2, "Spin Coating", [
    param("Solute 1 name", one("PbI2"), { family: solute }),
    num("Solute 1 Concentration", "1.5", "mM", solute),
    param("Solute 1 type", one("SAM"), { family: solute }),
    num("Solvent 1 volume", "50", "uL", solvent),
  ]));
  assert.deepEqual(texts(withName), ["PbI2  1.5 mM · SAM", "50 uL"]);
  const typeOnly = stepCard(step(2, "Spin Coating", [param("Solute 1 type", one("SAM"), { family: solute })]));
  assert.deepEqual(texts(typeOnly), ["SAM"]);
  const rotation = { group: "rotation_steps", index: 1 };
  const varying = stepCard(step(2, "Spin Coating", [
    param("Solvent 1 name", [["Ethanol", FIRST], ["IPA", LAST]], { family: solvent }),
    num("Solvent 1 volume", "50", "uL", solvent),
    num("Rotation speed", "3000", "rpm", rotation),
    num("Rotation time", "30", "s", rotation),
    param("Material name", [["A", FIRST], ["B", LAST]]),
  ]));
  assert.equal(varying.material, "");
  assert.deepEqual(texts(varying), ["Ethanol / IPA · 50 uL", "3000 rpm · 30 s", "A / B"]);
  assert.deepEqual(varying.lines.map((l) => l.values.map((v) => v.varies)), [[true, false], [false, false], [true]]);
});

test("numbered columns merge behind their name; ratios read MAI:MABr 3:1", () => {
  const two = { group: null, index: 2 };
  const one1 = { group: null, index: 1 };
  const card = stepCard(step(5, "Close Space Sublimation", [
    param("Material name 2", one("PbI2"), { family: two }),
    num("Thickness 2", "200", "nm", two),
    num("Rate 2", "1", "angstrom/s", two),
    param("Source material name 1", one("MAI"), { family: one1 }),
    num("Source material ratio 1", "3", null, one1),
  ]));
  assert.deepEqual(texts(card), ["PbI2  200 nm · 1 angstrom/s", "MAI  3"]);
  const ratios = stepCard(step(5, "Close Space Sublimation", [
    num("Process pressure", "3", "mbar"),
    param("Source material name 1", one("MAI"), { family: one1 }),
    param("Source material name 2", one("MABr"), { family: two }),
    num("Source material ratio 1", "3", null, one1),
    num("Source material ratio 2", "1", null, two),
    num("Source temperature", "120", "°C"),
  ]));
  assert.deepEqual(texts(ratios), ["3 mbar", "MAI:MABr  3:1", "source 120 °C"]);
  // two names behind one number without a group are two things: nothing merges
  const twoNames = stepCard(step(5, "Close Space Sublimation", [
    param("Material name 1", one("C60"), { family: one1 }),
    num("Thickness 1", "20", "nm", one1),
    param("Source material name 1", one("MAI"), { family: one1 }),
  ]));
  assert.deepEqual(texts(twoNames), ["C60 · 20 nm · MAI"]);
});

test("a varying value is marked, a changed one carries its old value", () => {
  const temperature = num("Substrate temperature", "120", "°C");
  temperature.values = [{ value: "120", rows: FIRST }, { value: "115", rows: LAST }];
  const time = num("Deposition Time", "900", "s");
  const changed = [
    ...ALL.map((row) => ({ column_id: time.column_id, row, old: "600", new: "900", by_formula: false })),
    { column_id: temperature.column_id, row: 3, old: "100", new: "120", by_formula: false },
    { column_id: temperature.column_id, row: 4, old: "110", new: "120", by_formula: false },
  ];
  const card = stepCard(step(5, "Close Space Sublimation", [temperature, time]), changed);
  assert.deepEqual(card.lines, [{
    label: "",
    values: [
      { text: "substrate 120 / 115 °C", varies: true, changed: "edited on 2 samples" },
      { text: "deposition 900 s", varies: false, changed: "was 600" },
    ],
  }]);
  assert.equal(card.varies, true);
  // in a merged line each value keeps its own mark
  const rotation = { group: "rotation_steps", index: 1 };
  const speed = num("Rotation speed", "3000", "rpm", rotation);
  const spinTime = num("Rotation time", "30", "s", rotation);
  const spin = stepCard(step(2, "Spin Coating", [speed, spinTime]), [
    { column_id: speed.column_id, row: 3, old: "2000", new: "3000", by_formula: false },
  ]);
  assert.deepEqual(spin.lines[0].values.map((v) => v.changed), ["was 2000", ""]);
});

test("an empty step", () => {
  const card = stepCard(step(7, "Spin Coating", [], { empty: true }));
  assert.equal(card.empty, true);
  assert.equal(card.headline, "Spin Coating");
  assert.deepEqual(card.lines, []);
});

const ROWS = ALL.map((row, i) => ({ row, label: String(i + 1), subbatch: "0" }));

function variation(name, values, { unit = null, step = "5: CSS" } = {}) {
  return {
    column_id: "V" + name,
    step,
    header: name,
    name,
    unit,
    type: "text",
    values: values.map(([value, rows]) => ({ value, rows, samples: "" })),
  };
}

test("one varying column: its values with their samples", () => {
  const view = { info: { rows: ROWS }, variations: [variation("Variation", [["10min", FIRST], ["20min", LAST]])] };
  assert.deepEqual(sampleGroups(view), {
    kind: "flat",
    also: [],
    blocks: [{
      name: "Variation", unit: null, step: "5: CSS",
      groups: [{ value: "10min", labels: ["1", "2", "3"] }, { value: "20min", labels: ["4", "5", "6"] }],
    }],
  });
});

test("two crossing columns: the one with fewer values as rows, the other across", () => {
  const view = {
    info: { rows: ROWS },
    variations: [
      variation("Variation", [["a", [3]], ["b", [4]], ["c", [5]], ["d", [6]], ["e", [7]], ["f", [8]]]),
      variation("Deposition Time", [["900", [3, 6]], ["1200", [4, 7]], ["1500", [5, 8]]], { unit: "s" }),
      variation("Substrate temperature", [["120", FIRST], ["115", LAST]], { unit: "°C" }),
    ],
  };
  const groups = sampleGroups(view);
  assert.equal(groups.kind, "cross");
  assert.equal(groups.a.name, "Substrate temperature");
  assert.equal(groups.b.name, "Deposition Time");
  assert.deepEqual(groups.columns, ["900 s", "1200 s", "1500 s"]);
  assert.deepEqual(groups.rows.map((r) => [r.value, r.cells.map((c) => c.labels.join(","))]), [
    ["120 °C", ["1", "2", "3"]],
    ["115 °C", ["4", "5", "6"]],
  ]);
  // the per-sample Variation column is only named: it does not cross the rows
  assert.deepEqual(groups.also, ["Variation"]);
});

test("the cross is found behind a stray column with fewer values", () => {
  const temperature = variation("Substrate temperature", [["120", FIRST], ["115", LAST]], { unit: "°C" });
  const time = variation("Deposition Time", [["900", [3, 6]], ["1200", [4, 7]], ["1500", [5, 8]]], { unit: "s" });
  // a note on one sample crosses nothing: the next candidate makes the rows
  const stray = variation("Notes", [["x", [3]], [null, [4, 5, 6, 7, 8]]], { step: "Experiment Info" });
  let groups = sampleGroups({ info: { rows: ROWS }, variations: [stray, temperature, time] });
  assert.equal(groups.kind, "cross");
  assert.deepEqual([groups.a.name, groups.b.name, groups.also], ["Substrate temperature", "Deposition Time", ["Notes"]]);
  // a note split like the temperature: the filled column makes the rows
  const twin = variation("Notes", [["x", FIRST], [null, LAST]], { step: "Experiment Info" });
  groups = sampleGroups({ info: { rows: ROWS }, variations: [twin, temperature, time] });
  assert.deepEqual([groups.a.name, groups.b.name], ["Substrate temperature", "Deposition Time"]);
  // a tie between two filled columns: the sheet's first makes the rows
  const atmosphere = variation("Atmosphere", [["N2", FIRST], ["air", LAST]]);
  groups = sampleGroups({ info: { rows: ROWS }, variations: [atmosphere, temperature, time] });
  assert.deepEqual([groups.a.name, groups.b.name, groups.also], ["Atmosphere", "Deposition Time", ["Substrate temperature"]]);
});

test("varying columns that do not cross are blocks of their own; an empty value is said", () => {
  const view = {
    info: { rows: ROWS },
    variations: [
      variation("Notes", [["x", FIRST], [null, LAST]]),
      variation("Thickness", [["100", [3, 4]], ["120", [5, 6, 7, 8]]], { unit: "nm" }),
    ],
  };
  const groups = sampleGroups(view);
  assert.equal(groups.kind, "flat");
  assert.deepEqual(groups.blocks.map((b) => [b.name, b.groups.map((g) => g.value)]), [
    ["Notes", ["x", EMPTY]],
    ["Thickness", ["100 nm", "120 nm"]],
  ]);
  assert.deepEqual(sampleGroups({ info: { rows: ROWS }, variations: [] }), { kind: "none" });
});

test("a row the sheet lost its label for is named by its row", () => {
  const rows = [{ row: 3, label: null, subbatch: "0" }, { row: 4, label: "2", subbatch: "0" }];
  const view = { info: { rows }, variations: [variation("Notes", [["x", [3]], ["y", [4]]])] };
  assert.deepEqual(sampleGroups(view).blocks[0].groups.map((g) => g.labels), [["row 3"], ["2"]]);
  // a view without rows (an older server): every sample by its row
  const bare = { info: { samples: 2 }, variations: view.variations };
  assert.deepEqual(sampleGroups(bare).blocks[0].groups.map((g) => g.labels), [["row 3"], ["row 4"]]);
});

test("the summary line", () => {
  const view = {
    steps: [{ position: 1, varies: false }, { position: 5, varies: true }, { position: 7, varies: false }],
    warnings: ["Sample 22 is missing", "Step 7 (Spin Coating) is empty"],
    changed: [{ column_id: "C1", row: 3 }, { column_id: "C1", row: 4 }],
  };
  assert.equal(overviewSummary(view), "3 steps · step 5 varies · 2 to check · 2 cells edited");
  assert.equal(overviewSummary({ steps: [{ position: 1, varies: true }, { position: 2, varies: true }] }),
    "2 steps · steps 1, 2 vary");
  assert.equal(overviewSummary({ steps: [] }), "0 steps");
  assert.equal(overviewSummary({ steps: [{ position: 1, varies: false }], changed: [{ column_id: "C1", row: 3 }] }),
    "1 step · 1 cell edited");
});
