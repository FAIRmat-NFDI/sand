import assert from "node:assert/strict";
import { test } from "node:test";

import { progressLine } from "../../src/sand/apis/static/edit-logic.js";
import {
  fieldKey, formatDay, isSheetFile, metaLines, nameFromFile, uploadedText,
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

test("the meta line names the sheet the way the design does, the rest on a second line", () => {
  assert.deepEqual(metaLines(INFO), {
    main: ["JoDa", "14 Sep 2026", "42 samples", "Glass / ITO", "16x16", "4 pixels"],
    more: ["Batch 275nmPbI2", "Sample area 0.105 cm^2", "Pixel area 0.105", "Number of junctions 1"],
  });
});

test("a sheet with fewer fields gets a shorter line, nothing invented", () => {
  assert.deepEqual(metaLines({ samples: 1, subbatches: [], fields: [field("Batch", "B1")] }), {
    main: ["1 sample"],
    more: ["Batch B1"],
  });
  assert.deepEqual(metaLines({ samples: 12, subbatches: ["0", "1"], fields: [] }).main, [
    "12 samples in 2 subbatches",
  ]);
  // only one of the substrate pair
  assert.deepEqual(metaLines({ samples: 3, fields: [field("Substrate material", "Glass")] }).main, [
    "3 samples", "Glass",
  ]);
  assert.deepEqual(metaLines({}), { main: [], more: [] });
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
  const now = new Date(2026, 10, 6);
  assert.equal(uploadedText("2026-11-02T09:30:00+00:00", now), "uploaded 2 Nov");
  assert.equal(uploadedText("2025-12-24T09:30:00+00:00", now), "uploaded 24 Dec 2025");
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

test("the header's progress line", () => {
  const status = {
    inputs: [
      { ready: [{ key: "k1" }, { key: "k2" }], confirmed: ["k1"], confirmed_since: [] },
      { ready: [{ key: "k3" }], confirmed: ["k3"], confirmed_since: ["k3"] },
    ],
  };
  assert.equal(
    progressLine(2, status),
    "2 updates · 1 edit written · 1 edit waits for your confirmation · 1 edit confirmed, being written");
  assert.equal(progressLine(1, null), "1 update");
  assert.equal(progressLine(0, null), "No updates yet");
  // the list is not loaded yet: only what the job says
  assert.equal(progressLine(null, status),
    "1 edit written · 1 edit waits for your confirmation · 1 edit confirmed, being written");
  assert.equal(progressLine(null, null), "");
});
