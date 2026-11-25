import assert from "node:assert/strict";
import { test } from "node:test";

import { labPages, pageIndex } from "../../src/sand/apis/static/lab-logic.js";

const ALL = [3, 4, 5];

function column(id, name, unit = null) {
  return { column_id: id, header: unit ? name + " [" + unit + "]" : name, name, unit, type: "text" };
}

function param(id, name, values, { unit = null, type = "text" } = {}) {
  return {
    ...column(id, name, unit),
    type,
    family: null,
    formula: false,
    values: values.map(([value, rows]) => ({ value, rows, samples: "" })),
  };
}

const VIEW = {
  info: {
    samples: 3,
    fields: [
      { ...column("C001", "Date"), value: "20260914" },
      { ...column("C009", "Sample area", "cm^2"), type: "float", value: "0.105" },
    ],
    unfilled: [column("C015", "Notes")],
  },
  steps: [
    {
      position: 1,
      label: "1: Spin Coating",
      material: "PbI2",
      layer: "Absorber",
      params: [
        param("C020", "Material name", [["PbI2", ALL]]),
        param("C025", "Rotation speed", [["3000", [3, 4]], ["4000", [5]]], { unit: "rpm", type: "int" }),
      ],
      unfilled: [column("C027", "Spin Delay", "s"), column("C030", "Notes")],
    },
    { position: 2, label: "2: Annealing", material: null, layer: null, params: [], unfilled: [column("C040", "Annealing time", "min")] },
  ],
  variations: [
    { ...param("C004", "Variation", [["a", [3]], ["b", [4]], ["c", [5]], ["d", [6]]]), step: "Experiment Info" },
    { ...param("C025", "Rotation speed", [["3000", [3, 4]], ["4000", [5]]], { unit: "rpm" }), step: "1: Spin Coating" },
  ],
};

test("the Experiment Info is step 0, before the steps", () => {
  const pages = labPages(VIEW);
  assert.deepEqual(pages.map((p) => p.position), [0, 1, 2]);
  assert.deepEqual(pages.map((p) => p.title), ["Experiment Info", "1: Spin Coating", "2: Annealing"]);
});

test("the Experiment Info page: fields and what varies, in sheet order", () => {
  const info = labPages(VIEW)[0];
  assert.deepEqual(info.sub, ["3 samples"]);
  assert.deepEqual(info.rows, [
    { name: "Date", value: "20260914" },
    // many different texts are counted; a step's variation is not here
    { name: "Variation", value: "4 different values" },
    { name: "Sample area", value: "0.105 cm^2" },
  ]);
  assert.deepEqual(info.unfilled, ["Notes"]);
});

test("a step page names its columns as the sheet does", () => {
  const spin = labPages(VIEW)[1];
  assert.deepEqual(spin.sub, ["PbI2", "Absorber"]);
  assert.deepEqual(spin.rows, [
    { name: "Material name", value: "PbI2" },
    { name: "Rotation speed", value: "3000 / 4000 rpm" },
  ]);
  // an empty column with its unit: the user is about to say it
  assert.deepEqual(spin.unfilled, ["Spin Delay [s]", "Notes"]);
});

test("an empty step still names its columns", () => {
  const annealing = labPages(VIEW)[2];
  assert.deepEqual(annealing.sub, []);
  assert.deepEqual(annealing.rows, []);
  assert.deepEqual(annealing.unfilled, ["Annealing time [min]"]);
});

test("one sample is not '1 samples'", () => {
  const view = { ...VIEW, info: { ...VIEW.info, samples: 1 } };
  assert.deepEqual(labPages(view)[0].sub, ["1 sample"]);
});

test("a refresh keeps the step, a sheet without it starts at the first page", () => {
  const pages = labPages(VIEW);
  assert.equal(pageIndex(pages, 2), 2);
  assert.equal(pageIndex(pages, 7), 0);
  assert.equal(pageIndex(pages, undefined), 0);
});

test("a counted column still says that some samples are empty", () => {
  const notes = param("C031", "Notes", [["a", [3]], ["b", [4]], ["c", [5]], ["d", [6]], [null, [7]]]);
  const view = { ...VIEW, steps: [{ ...VIEW.steps[0], params: [notes] }] };
  assert.deepEqual(labPages(view)[1].rows, [{ name: "Notes", value: "4 different values · some empty" }]);
});
