import assert from "node:assert/strict";
import { test } from "node:test";

import {
  fieldKey, formatDay, isSheetFile, metaLines, nameFromFile, renameChoice, uploadedText,
  withRenamed,
} from "../../src/sand/apis/static/sheet-header-logic.js";

function field(name, value, unit = null) {
  return { column_id: "C000", header: unit ? name + " [" + unit + "]" : name, name, unit, type: "text", value };
}

// the real sheet's Experiment Info, as the sheet view lists it
const INFO = {
  samples: 42,
  subbatches: ["0"],
  fields: [
    field("Date", "20260914"),
    field("Project_Name", "JoDa"),
    field("Batch", "275nmPbI2"),
    field("Sample dimension", "16x16"),
    field("Sample area", "0.105", "cm^2"),
    field("Number of pixels", "4"),
    field("Pixel area", "0.105"),
    field("Number of junctions", "1"),
    field("Substrate material", "Glass"),
    field("Substrate conductive layer", "ITO"),
  ],
};

test("the header's line tells the batch from the others; the substrate is the overview's line", () => {
  assert.deepEqual(metaLines(INFO), {
    main: ["JoDa", "14 Sep 2026", "42 samples"],
    substrate: ["Glass / ITO", "16x16", "4 pixels", "batch 275nmPbI2", "sample area 0.105 cm^2", "pixel area 0.105", "number of junctions 1"],
  });
});

test("a sheet with fewer fields gets a shorter line, nothing invented", () => {
  assert.deepEqual(metaLines({ samples: 1, subbatches: [], fields: [field("Batch", "B1")] }), {
    main: ["1 sample"],
    substrate: ["batch B1"],
  });
  assert.deepEqual(metaLines({ samples: 12, subbatches: ["0", "1"], fields: [] }).main, [
    "12 samples in 2 subbatches",
  ]);
  // only one of the substrate pair
  assert.deepEqual(metaLines({ samples: 3, fields: [field("Substrate material", "Glass")] }).substrate, ["Glass"]);
  assert.deepEqual(metaLines({}), { main: [], substrate: [] });
});

test("a blank cell is no value, and two columns with the same words both show", () => {
  const info = {
    samples: 2,
    fields: [
      field("Substrate material", ""),
      field("Substrate conductive layer", "ITO"),
      field("Batch", "B1"),
      field("batch", "B2"),
    ],
  };
  assert.deepEqual(metaLines(info), { main: ["2 samples"], substrate: ["ITO", "batch B1", "batch B2"] });
});

test("a field's key is its words", () => {
  assert.equal(fieldKey("Substrate conductive layer"), "substrate_conductive_layer");
  assert.equal(fieldKey("Project_Name"), "project_name");
  assert.equal(fieldKey(" Sample area "), "sample_area");
});

test("days as the sheet writes them, or ISO, read as days", () => {
  assert.equal(formatDay("20260914"), "14 Sep 2026");
  assert.equal(formatDay("2025-01-01"), "1 Jan 2025");
  assert.equal(formatDay("2025-01-01T10:00:00"), "1 Jan 2025");
  // not a day: left as it is
  assert.equal(formatDay("20261350"), "20261350");
  assert.equal(formatDay("yesterday"), "yesterday");
  assert.equal(formatDay(null), "");
});

test("when a sheet was uploaded, briefly", () => {
  // midday instants: the same day in every zone the tests run in
  const now = new Date(2026, 10, 6, 12);
  assert.equal(uploadedText("2026-11-02T12:00:00+00:00", now), "uploaded 2 Nov");
  assert.equal(uploadedText("2025-12-24T12:00:00+00:00", now), "uploaded 24 Dec 2025");
  assert.equal(uploadedText(null, now), "");
  assert.equal(uploadedText("not a time", now), "");
});

test("the name proposed from the file", () => {
  assert.equal(nameFromFile("Batch 7.xlsx"), "Batch 7");
  assert.equal(nameFromFile("C:\\sheets\\275nmPbI2.XLSX"), "275nmPbI2");
  assert.equal(nameFromFile("folder/no extension"), "no extension");
  assert.equal(nameFromFile(""), "");
  assert.equal(isSheetFile("a.xlsx"), true);
  assert.equal(isSheetFile("a.XLSX"), true);
  assert.equal(isSheetFile("a.xls"), false);
  assert.equal(isSheetFile("a.csv"), false);
});

test("a rename sends the name single-spaced, keeps an unchanged one, refuses a blank", () => {
  assert.deepEqual(renameChoice("batch 7", "  batch   8 "), { action: "rename", name: "batch 8" });
  assert.deepEqual(renameChoice("batch 7", " batch 7\n"), { action: "keep", name: "batch 7" });
  assert.deepEqual(renameChoice("batch 7", "   "), { action: "refuse", name: "" });
  assert.deepEqual(renameChoice(undefined, ""), { action: "refuse", name: "" });
  assert.deepEqual(renameChoice(undefined, "batch 7"), { action: "rename", name: "batch 7" });
});

test("the renamed sheet keeps its place in the list and what the response left out", () => {
  const list = [
    { entry_id: "e2", upload_id: "u2", name: "new", created: "2026-11-02T10:00:00Z" },
    { entry_id: "e1", upload_id: "u1", name: "old", created: "2026-11-01T10:00:00Z" },
  ];
  const renamed = { entry_id: "e1", upload_id: "u1", name: "older", created: null, entry_url: "http://n/u1/e1" };
  assert.deepEqual(withRenamed(list, renamed), [
    list[0],
    { entry_id: "e1", upload_id: "u1", name: "older", created: "2026-11-01T10:00:00Z", entry_url: "http://n/u1/e1" },
  ]);
  assert.notEqual(withRenamed(list, renamed)[1], list[1]); // a new object, the list is not mutated
  assert.equal(list[1].name, "old");
});
