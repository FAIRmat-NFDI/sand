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

test("a step card: headline from the material, layer and tool as subtitle, families as rows", () => {
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
    num("Annealing temperature", "100", "°C"),
  ]);
  const card = stepCard(spin);
  assert.equal(card.headline, "Me-4PACz");
  assert.equal(card.kicker, "Spin Coating");
  assert.equal(card.sub, "HTL · box1");
  assert.deepEqual(card.rows.map((r) => [r.label, r.value]), [
    ["Ethanol", "50 uL"],
    ["Spin", "3000 rpm · 30 s"],
    ["Annealing temperature", "100 °C"],
  ]);
  assert.equal(card.varies, false);
  assert.equal(card.empty, false);
});

test("without a material the step type is the headline; a lone name makes a labelled row", () => {
  const cleaning = { group: "cleaning_steps", index: 1 };
  const card = stepCard(step(1, "Cleaning UV-Ozone", [
    param("Solvent 1", one("Acetone"), { family: cleaning }),
    num("Time 1", "600", "s", cleaning),
    param("Solvent 2", one("IPA"), { family: { group: "cleaning_steps", index: 2 } }),
    param("Solvent 1 name", one("Ethanol"), { family: { group: "solvents", index: 1 } }),
  ]));
  assert.equal(card.headline, "Cleaning UV-Ozone");
  assert.equal(card.kicker, "");
  assert.deepEqual(card.rows.map((r) => [r.label, r.value]), [
    ["Acetone", "600 s"],
    ["Cleaning 2", "IPA"],
    ["Solvent", "Ethanol"],
  ]);
});

test("numbered columns merge behind their name only when the rest carry units", () => {
  const two = { group: null, index: 2 };
  const one1 = { group: null, index: 1 };
  const card = stepCard(step(4, "Seq-Evaporation", [
    param("Material name 2", one("PbI2"), { family: two }),
    num("Thickness 2", "200", "nm", two),
    num("Rate 2", "1", "angstrom/s", two),
    param("Source material name 1", one("MAI"), { family: one1 }),
    num("Source material ratio 1", "3", null, one1),
  ]));
  assert.deepEqual(card.rows.map((r) => [r.label, r.value]), [
    ["PbI2", "200 nm · 1 angstrom/s"],
    ["Source material name 1", "MAI"],
    ["Source material ratio 1", "3"],
  ]);
});

test("a varying row is marked, a changed one carries its old value", () => {
  const temperature = num("Substrate temperature", "120", "°C");
  temperature.values = [{ value: "120", rows: FIRST }, { value: "115", rows: LAST }];
  const time = num("Deposition Time", "900", "s");
  const changed = [
    ...ALL.map((row) => ({ column_id: time.column_id, row, old: "600", new: "900", by_formula: false })),
    { column_id: temperature.column_id, row: 3, old: "100", new: "120", by_formula: false },
    { column_id: temperature.column_id, row: 4, old: "110", new: "120", by_formula: false },
  ];
  const card = stepCard(step(5, "Close Space Sublimation", [temperature, time]), changed);
  assert.deepEqual(card.rows, [
    { label: "Substrate temperature", value: "120 / 115 °C", varies: true, changed: "edited on 2 samples" },
    { label: "Deposition Time", value: "900 s", varies: false, changed: "was 600" },
  ]);
  assert.equal(card.varies, true);
});

test("an empty step", () => {
  const card = stepCard(step(7, "Spin Coating", [], { empty: true }));
  assert.equal(card.empty, true);
  assert.equal(card.headline, "Spin Coating");
  assert.deepEqual(card.rows, []);
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
});
