import assert from "node:assert/strict";
import { test } from "node:test";

import {
  describeEditPhase, downloadFilename, editSummary, pendingKeys, previewNotes, reportsByEntry,
} from "../../src/sand/apis/static/edit-logic.js";

const REPORT = {
  input: 1,
  entry_id: "n1",
  text: "speed 4000 for samples 1 to 3",
  preview: "2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, samples 1–3\n⚠ not applied: \"and dry\"",
  ready: [{ key: "k1", line: "2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, samples 1–3" }],
  confirmed: [],
  cells_written: 0,
  refused: null,
};

test("the phase as the user reads it", () => {
  assert.equal(describeEditPhase({ phase: "starting" }), "Starting...");
  assert.equal(describeEditPhase({ phase: "collecting" }), "Collecting the updates...");
  assert.equal(
    describeEditPhase({ phase: "interpreting", inputs_done: 0, inputs_total: 3 }),
    "Interpreting update 1 of 3...");
  assert.equal(
    describeEditPhase({ phase: "interpreting", inputs_done: 3, inputs_total: 3 }),
    "Interpreting update 3 of 3...");
  assert.equal(describeEditPhase({ phase: "interpreting" }), "Interpreting update 1 of ?...");
  assert.equal(describeEditPhase({ phase: "writing-sheet" }), "Writing the sheet...");
});

test("reports are found by the input's entry id", () => {
  const byEntry = reportsByEntry({ inputs: [REPORT, { ...REPORT, entry_id: "n2" }] });
  assert.deepEqual(Object.keys(byEntry), ["n1", "n2"]);
  assert.equal(byEntry.n1, REPORT);
  assert.deepEqual(reportsByEntry({}), {});
});

test("pending keys are the ready edits nobody confirmed", () => {
  assert.deepEqual(pendingKeys([REPORT]), ["k1"]);
  assert.deepEqual(pendingKeys([{ ...REPORT, confirmed: ["k1"] }]), []);
  assert.deepEqual(pendingKeys([{ ...REPORT, ready: [] }]), []);
});

test("the preview notes are what the ready edits do not show", () => {
  assert.deepEqual(previewNotes(REPORT), ["⚠ not applied: \"and dry\""]);
  assert.deepEqual(previewNotes({ ...REPORT, preview: "" }), []);
});

test("the summary line of a completed job", () => {
  assert.equal(editSummary({ changes: 0, inputs: [] }), "0 cells changed");
  assert.equal(
    editSummary({ changes: 1, inputs: [REPORT] }),
    "1 cell changed · 1 edit awaits confirmation");
  assert.equal(
    editSummary({
      changes: 4,
      stale_confirmations: 1,
      inputs: [REPORT, { ...REPORT, entry_id: "n2", ready: [], refused: "a formula error" }],
    }),
    "4 cells changed · 1 edit awaits confirmation · 1 update refused"
      + " · 1 confirmation is stale (the input changed since): look again");
});

test("the download name comes from the server when it says one", () => {
  assert.equal(
    downloadFilename("attachment; filename=\"Batch 7 _.xlsx\"; filename*=UTF-8''Batch%207%20%CE%B1.xlsx", "x.xlsx"),
    "Batch 7 α.xlsx");
  assert.equal(downloadFilename("attachment; filename=\"batch.xlsx\"", "x.xlsx"), "batch.xlsx");
  assert.equal(downloadFilename(null, "x.xlsx"), "x.xlsx");
  assert.equal(downloadFilename("attachment; filename*=UTF-8''%E0%A4%A", "x.xlsx"), "x.xlsx");
});
