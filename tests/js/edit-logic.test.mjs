import assert from "node:assert/strict";
import { test } from "node:test";

import {
  confirmAllLabel, describeEditPhase, downloadFilename, editSummary, inputLocked, inputState,
  bulkKeys, inputsToRead, pendingKeys, previewNotes, readKey, reportsByEntry, unwrittenCount,
  unwrittenKey, writtenCount, writtenEdits,
} from "../../src/sand/apis/static/edit-logic.js";

const REPORT = {
  input: 1,
  entry_id: "n1",
  text: "speed 4000 for samples 1 to 3",
  preview: "2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, samples 1–3\n? not found: \"and dry\"",
  ready: [{
    key: "k1",
    line: "2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, samples 1–3",
    step: "2: Spin Coating",
    column: "Rotation speed [rpm]",
    old: "3000",
    new: "4000",
    samples: "samples 1–3",
  }],
  confirmed: [],
  cells_written: 0,
  refused: null,
};

test("the phase as the user reads it", () => {
  assert.equal(describeEditPhase({ phase: "starting" }), "Starting...");
  assert.equal(describeEditPhase({ phase: "collecting" }), "Collecting the updates...");
  assert.equal(
    describeEditPhase({ phase: "interpreting", inputs_done: 0, inputs_total: 3 }),
    "Reading update 1 of 3...");
  assert.equal(
    describeEditPhase({ phase: "interpreting", inputs_done: 3, inputs_total: 3 }),
    "Reading update 3 of 3...");
  assert.equal(describeEditPhase({ phase: "interpreting" }), "Reading update 1 of ?...");
  assert.equal(describeEditPhase({ phase: "replaying" }), "Checking the earlier updates...");
  // three replayed, two read: the count is over the two
  assert.equal(
    describeEditPhase({ phase: "interpreting", inputs_done: 3, inputs_total: 5, inputs_replayed: 3 }),
    "Reading update 1 of 2...");
  assert.equal(
    describeEditPhase({ phase: "interpreting", inputs_done: 4, inputs_total: 5, inputs_replayed: 3 }),
    "Reading update 2 of 2...");
  assert.equal(
    describeEditPhase({ phase: "interpreting", inputs_done: 3, inputs_total: 3, inputs_replayed: 3 }),
    "Checking the earlier updates...");
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

test("confirmed edits the last run did not write", () => {
  assert.equal(unwrittenCount({ inputs: [REPORT] }), 0);
  assert.equal(unwrittenKey({ inputs: [REPORT] }), "");
  const since = { inputs: [{ ...REPORT, confirmed: ["k1"], confirmed_since: ["k1"] }] };
  assert.equal(unwrittenCount(since), 1);
  assert.equal(unwrittenKey(since), "k1");
  assert.equal(
    unwrittenKey({ inputs: [{ ...REPORT, confirmed_since: ["k2"] }, { ...REPORT, confirmed_since: ["k1"] }] }),
    "k1\nk2");
});

test("the inputs a job has to read", () => {
  const note = { entry_id: "n1", text: REPORT.text };
  const reports = { n1: REPORT };
  assert.deepEqual(inputsToRead([note], reports), []);
  // no report yet, a report of other words, a report without its input
  assert.deepEqual(inputsToRead([note, { entry_id: "n2", text: "anneal 120" }], reports), ["n2"]);
  assert.deepEqual(inputsToRead([{ ...note, text: "speed 4500" }], reports), ["n1"]);
  assert.deepEqual(inputsToRead([note, { entry_id: "n2", text: "b" }], { ...reports, n3: REPORT }), ["n2", "n3"]);
  assert.deepEqual(inputsToRead([note], {}), ["n1"]);
  // the last input deleted: nothing to read (the job would refuse)
  assert.deepEqual(inputsToRead([], reports), []);
  // nothing while a recording is still transcribed: the job would refuse
  assert.deepEqual(inputsToRead([note, { entry_id: "a1", text: null }], {}), []);
});

test("the read key tells the inputs and their words apart", () => {
  const items = [{ entry_id: "n1", text: "a" }, { entry_id: "n2", text: "b" }];
  assert.equal(readKey(items), readKey(items.map((i) => ({ ...i }))));
  assert.notEqual(readKey(items), readKey([items[0]]));
  assert.notEqual(readKey(items), readKey([items[0], { entry_id: "n2", text: "c" }]));
  assert.notEqual(readKey(items), readKey([{ entry_id: "n1", text: "a\n\nn2\nb" }]));
});

test("an input is locked once one of its edits is confirmed", () => {
  assert.equal(inputLocked(undefined), false);
  assert.equal(inputLocked(REPORT), false);
  assert.equal(inputLocked({ ...REPORT, confirmed: ["k1"] }), true);
});

test("the state chip of an input", () => {
  const note = { entry_id: "n1", text: REPORT.text };
  const label = (item, report, running = false, canConfirm = true) =>
    inputState(item, report, running, canConfirm)?.label ?? null;
  assert.equal(label({ ...note, text: null }, undefined), null);
  assert.equal(label(note, undefined, false, false), null);
  assert.equal(label(note, undefined), "Waiting to be read");
  assert.equal(label(note, undefined, true), "Reading...");
  // a report of other words is not this update's
  assert.equal(label({ ...note, text: "speed 4500" }, REPORT, true), "Reading...");
  assert.equal(label({ ...note, locked: true }, undefined), "Applied");
  assert.equal(label(note, REPORT), "1 edit to confirm");
  assert.equal(label(note, { ...REPORT, ready: [...REPORT.ready, { ...REPORT.ready[0], key: "k2" }] }),
    "2 edits to confirm");
  assert.equal(label(note, { ...REPORT, confirmed: ["k1"], confirmed_since: ["k1"] }), "Confirmed, writing...");
  assert.equal(label(note, { ...REPORT, confirmed: ["k1"] }), "Applied");
  assert.equal(label(note, { ...REPORT, confirmed: ["k1"], refused: "a formula error" }), "Not written");
  assert.equal(label(note, { ...REPORT, ready: [] }), "No edit found");
  // two readings sand could not decide between: not "nothing found"
  assert.equal(label(note, { ...REPORT, ready: [], unresolved: 1 }), "Unclear");
  assert.equal(label(note, { ...REPORT, unresolved: 1 }), "1 edit to confirm");
  assert.equal(inputState(note, REPORT, false, true).state, "ready");
});

test("the confirm-all button names the pending edits", () => {
  assert.equal(confirmAllLabel(1), "Confirm the edit");
  assert.equal(confirmAllLabel(3), "Confirm all 3 edits");
});

test("the preview notes are what the ready edits do not show", () => {
  assert.deepEqual(previewNotes(REPORT), ["? not found: \"and dry\""]);
  // the coverage warning is hidden (it mostly names the step)
  assert.deepEqual(previewNotes({ ...REPORT, preview: REPORT.preview + "\n⚠ no update in: \"step 1 cleaning\"" }),
    ["? not found: \"and dry\""]);
  const withWords = { ...REPORT, preview: REPORT.ready[0].line + "\n   from \"speed 4000\"\n   · read from 'four thousand'" };
  assert.deepEqual(previewNotes(withWords), ["   · read from 'four thousand'"]);
  assert.deepEqual(previewNotes({ ...REPORT, preview: "" }), []);
  // the samples already at the value are in the edit's samples cell, not a note
  const aside = {
    ...REPORT,
    preview: REPORT.ready[0].line + "\n   · samples 8–13 already 4000, nothing to change\n   · read from 'four thousand'",
    ready: [{ ...REPORT.ready[0], unchanged: "samples 8–13 already 4000" }],
  };
  assert.deepEqual(previewNotes(aside), ["   · read from 'four thousand'"]);
});

test("the summary line of a completed job", () => {
  assert.equal(editSummary({ changes: 0, inputs: [] }), "Nothing written yet.");
  assert.equal(editSummary({ changes: 1, inputs: [REPORT] }), "1 edit waits for your confirmation");
  assert.equal(
    editSummary({ changes: 0, inputs: [{ ...REPORT, confirmed: ["k1"], confirmed_since: ["k1"] }] }),
    "1 edit confirmed, being written");
  assert.equal(
    editSummary({
      changes: 4,
      stale_confirmations: 1,
      inputs: [
        { ...REPORT, confirmed: ["k1"] },
        { ...REPORT, entry_id: "n2", ready: [], refused: "a formula error" },
      ],
    }),
    "1 edit written · 1 update could not be written");
});

test("the table is the job's own fold of its changelog when it has one", () => {
  const written = [
    { step: "1: Cleaning", column: "Time 1 [s]", value: "120", samples: "samples 1–3" },
    { step: "1: Cleaning", column: "Time 1 [s]", value: "100", samples: "samples 4–42" },
    { step: "2: Spin Coating", column: "Speed", value: "4000", samples: "all 42 samples" },
  ];
  const status = { written, inputs: [{ ...REPORT, confirmed: ["k1"] }] };
  assert.deepEqual(writtenEdits(status).map((r) => [r.column, r.value, r.first]), [
    ["Time 1 [s]", "120", true], ["Time 1 [s]", "100", false], ["Speed", "4000", true],
  ]);
  assert.equal(written[0].first, undefined); // the status is not changed
  assert.deepEqual(writtenEdits({ written: [], inputs: [{ ...REPORT, confirmed: ["k1"] }] }), []);
  // the summary counts the confirmed edits, not the table's rows
  assert.equal(writtenCount(status), 1);
  assert.equal(writtenCount({ inputs: [{ ...REPORT, confirmed: ["k1"], confirmed_since: ["k1"] }] }), 0);
  assert.equal(writtenCount({ inputs: [{ ...REPORT, confirmed: ["k1"], refused: "a formula error" }] }), 0);
});

test("without the fold, the written edits of the reports grouped by step", () => {
  assert.deepEqual(writtenEdits({ inputs: [REPORT] }), []);
  assert.deepEqual(
    writtenEdits({ inputs: [{ ...REPORT, confirmed: ["k1"] }] }),
    [{ step: "2: Spin Coating", column: "Rotation speed [rpm]", value: "4000", samples: "samples 1–3", first: true }]);
  // confirmed since the run: not written yet; refused: not written
  assert.deepEqual(writtenEdits({ inputs: [{ ...REPORT, confirmed: ["k1"], confirmed_since: ["k1"] }] }), []);
  assert.deepEqual(writtenEdits({ inputs: [{ ...REPORT, confirmed: ["k1"], refused: "a formula error" }] }), []);
  // a report of an older job has only the line
  const old = { ...REPORT, confirmed: ["k1"], ready: [{ key: "k1", line: "the line" }] };
  assert.deepEqual(writtenEdits({ inputs: [old] }), [{ line: "the line", first: true }]);
  // grouped by step in the sheet's order, the written order within a step
  const edit = (key, step, column) => ({ ...REPORT.ready[0], key, step, column });
  const rows = writtenEdits({ inputs: [
    { ...REPORT, confirmed: ["a", "b"], ready: [edit("a", "2: Spin Coating", "Speed"), edit("b", "1: Cleaning", "Time 1")] },
    { ...REPORT, entry_id: "n2", confirmed: ["c"], ready: [edit("c", "2: Spin Coating", "Volume")] },
  ] });
  assert.deepEqual(rows.map((r) => [r.step, r.column, r.first]), [
    ["1: Cleaning", "Time 1", true],
    ["2: Spin Coating", "Speed", true],
    ["2: Spin Coating", "Volume", false],
  ]);
  // "10:" after "2:"; unnumbered steps after the numbered, one group per name
  const mixed = writtenEdits({ inputs: [{ ...REPORT, confirmed: ["a", "b", "c", "d"], ready: [
    edit("a", "Notes", "Remark"), edit("b", "10: Anneal", "Time"), edit("c", "2: Spin", "Speed"), edit("d", "Notes", "Operator"),
  ] }] });
  assert.deepEqual(mixed.map((r) => [r.step, r.column, r.first]), [
    ["2: Spin", "Speed", true],
    ["10: Anneal", "Time", true],
    ["Notes", "Remark", true],
    ["Notes", "Operator", false],
  ]);
});

test("the download name comes from the server when it says one", () => {
  assert.equal(
    downloadFilename("attachment; filename=\"Batch 7 _.xlsx\"; filename*=UTF-8''Batch%207%20%CE%B1.xlsx", "x.xlsx"),
    "Batch 7 α.xlsx");
  assert.equal(downloadFilename("attachment; filename=\"batch.xlsx\"", "x.xlsx"), "batch.xlsx");
  assert.equal(downloadFilename(null, "x.xlsx"), "x.xlsx");
  assert.equal(downloadFilename("attachment; filename*=UTF-8''%E0%A4%A", "x.xlsx"), "x.xlsx");
});


test("confirm all leaves out the edits a word check marked", () => {
  const report = {
    ready: [{ key: "k1" }, { key: "k2", checks: ["'4400' is not in your words"] }, { key: "k3", checks: [] }],
    confirmed: [],
  };
  assert.deepEqual(pendingKeys([report]), ["k1", "k2", "k3"]);
  assert.deepEqual(bulkKeys([report]), ["k1", "k3"]);
  assert.deepEqual(bulkKeys([{ ...report, confirmed: ["k1"] }]), ["k3"]);
});

test("confirm all names the edits it leaves out", () => {
  assert.equal(confirmAllLabel(2, 1), "Confirm 2 edits without a check");
  assert.equal(confirmAllLabel(1, 3), "Confirm 1 edit without a check");
  assert.equal(confirmAllLabel(2), "Confirm all 2 edits");
});
