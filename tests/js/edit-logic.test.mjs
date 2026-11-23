import assert from "node:assert/strict";
import { test } from "node:test";

import {
  confirmAllLabel, describeEditPhase, downloadBlock, downloadFilename, editSummary, inputApplied, inputState,
  changedEntries, checkedKeys, confirmQuestion, decisionCount, lostUnticks, recomputeLabel, inputsToRead, outdatedEntries, pendingKeys, previewNotes, problemLines, problemShort, readKey,
  failedReadKey, recomputeDue, reportsByEntry, resetNote, roundSnapshot, rewriteWarning, settledBlock, withRound,
  freshReports, unchangedAbove, unwrittenKey, writtenCount, writtenEdits,
} from "../../src/sand/apis/static/edit-logic.js";

const REPORT = {
  input: 1,
  entry_id: "n1",
  text: "speed 4000 for samples 1 to 3",
  preview: "2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, samples 1–3\nnot found: \"and dry\"",
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
  assert.equal(
    describeEditPhase({ phase: "interpreting", inputs_done: 0, inputs_total: 3 }),
    "Reading update 1 of 3...");
  assert.equal(
    describeEditPhase({ phase: "interpreting", inputs_done: 3, inputs_total: 3 }),
    "Reading update 3 of 3...");
  assert.equal(describeEditPhase({ phase: "interpreting" }), "Reading update 1 of ?...");
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

test("pending keys are the ready edits nobody decided", () => {
  assert.deepEqual(pendingKeys([REPORT]), ["k1"]);
  assert.deepEqual(pendingKeys([{ ...REPORT, confirmed: ["k1"] }]), []);
  assert.deepEqual(pendingKeys([{ ...REPORT, rejected: ["k1"] }]), []);
  assert.deepEqual(pendingKeys([{ ...REPORT, ready: [] }]), []);
});

test("a round marks the updates it changed", () => {
  const two = (line, fields = {}) => ({ ...REPORT, input: 2, entry_id: "n2", ready: [{ key: line, line }], ...fields });
  const before = roundSnapshot({ inputs: [REPORT, two("10 s → 20 s")] });
  // "from" changed, then the anchor gone (unclear)
  for (const after of [two("0 s → 20 s"), two("x", { ready: [], unresolved: 1 })]) {
    assert.deepEqual([...changedEntries(before, roundSnapshot({ inputs: [REPORT, after] }))], ["n2"]);
  }
  assert.deepEqual([...changedEntries(before, before)], []);
  assert.deepEqual([...changedEntries(null, before)], []);
});

test("the user's own untick or restore is no change of the round", () => {
  const status = { inputs: [REPORT] };
  const untick = { reject: [{ key: "k1", reason: "wrong" }], restore: [] };
  const unticked = { inputs: [{ ...REPORT, rejected: ["k1"] }] };
  // without the round: the untick itself looks like a change
  assert.deepEqual([...changedEntries(roundSnapshot(status), roundSnapshot(unticked))], ["n1"]);
  const before = roundSnapshot(withRound(status, untick));
  assert.deepEqual([...changedEntries(before, roundSnapshot(unticked))], []);
  // ticked back: open again before the reread
  const restore = { reject: [], restore: ["k1"] };
  assert.deepEqual(
    [...changedEntries(roundSnapshot(withRound(unticked, restore)), roundSnapshot(status))], []);
  // what sand changed is still marked
  const other = { inputs: [{ ...REPORT, rejected: ["k1"], ready: [...REPORT.ready, { key: "k2", line: "x" }] }] };
  assert.deepEqual([...changedEntries(before, roundSnapshot(other))], ["n1"]);
  assert.equal(withRound(status, null), status);
});

test("Confirm all waits for every update read and nothing decided since", () => {
  const items = [{ entry_id: "n1", text: REPORT.text }];
  assert.equal(settledBlock({ inputs: [REPORT] }, items), "");
  assert.notEqual(settledBlock({ inputs: [{ ...REPORT, rejected_since: ["k1"] }] }, items), "");
  assert.equal(settledBlock({ inputs: [REPORT] }, [...items, { entry_id: "n2", text: "new" }]),
    "An update is not read yet");
});

test("Reset all counts the decisions it takes back", () => {
  assert.equal(decisionCount({ inputs: [{ ...REPORT, confirmed: ["k1"], rejected: ["k2"] }] }), 2);
});

test("the updates read again first: below a change the last run did not know", () => {
  const items = ["n1", "n2", "n3"].map((id) => ({ entry_id: id, text: REPORT.text }));
  const report = (n, fields = {}) => ({ ...REPORT, input: n, entry_id: "n" + n, ...fields });
  const reports = (...list) => reportsByEntry({ inputs: list });
  assert.deepEqual([...outdatedEntries(items, reports(report(1), report(2), report(3)))], []);
  // a confirmation changes nothing below: they were read with the edit
  const confirming = report(1, { confirmed: ["k1"], confirmed_since: ["k1"] });
  assert.deepEqual([...outdatedEntries(items, reports(confirming, report(2), report(3)))], []);
  for (const field of ["rejected_since", "unrejected_since", "unconfirmed_since"]) {
    const changed = report(2, { [field]: ["k1"] });
    assert.deepEqual([...outdatedEntries(items, reports(report(1), changed, report(3)))], ["n3"], field);
  }
  // revised: itself and the ones after it
  const revised = [items[0], { ...items[1], text: "speed 4500" }, items[2]];
  assert.deepEqual([...outdatedEntries(revised, reports(report(1), report(2), report(3)))], ["n2", "n3"]);
  // deleted: the ones after it
  assert.deepEqual([...outdatedEntries([items[0], items[2]], reports(report(1), report(2), report(3)))],
    ["n2", "n3"]);
  // without the inputs list: only the decisions count
  assert.deepEqual([...outdatedEntries(null, reports(report(1), report(2)))], []);
});

test("confirmed edits the last run did not write", () => {
  assert.equal(unwrittenKey({ inputs: [REPORT] }), "");
  const since = { inputs: [{ ...REPORT, confirmed: ["k1"], confirmed_since: ["k1"] }] };
  assert.equal(unwrittenKey(since), "confirmed k1");
  assert.equal(
    unwrittenKey({ inputs: [{ ...REPORT, confirmed_since: ["k2"] }, { ...REPORT, confirmed_since: ["k1"] }] }),
    "confirmed k1\nconfirmed k2");
  // withdrawn: its update was revised or deleted, the sheet still has it
  const withdrawn = { inputs: [{ ...REPORT, ready: [], unconfirmed_since: ["k0"] }] };
  assert.equal(unwrittenKey(withdrawn), "withdrawn k0");
});

test("the edited sheet downloads once every edit is decided and written", () => {
  const items = [{ entry_id: "n1", text: REPORT.text }];
  const decided = { ...REPORT, confirmed: ["k1"] };
  assert.equal(downloadBlock({ inputs: [decided] }, items), "");
  assert.equal(downloadBlock({ inputs: [{ ...REPORT, rejected: ["k1"] }] }, items), "");
  // nothing to decide in an unclear update: like a rejection
  assert.equal(downloadBlock({ inputs: [{ ...REPORT, ready: [], unresolved: 1 }] }, items), "");
  assert.equal(downloadBlock({ inputs: [REPORT] }, items),
    "Decide every edit first: 1 edit waits for a decision");
  for (const field of ["confirmed_since", "rejected_since", "unrejected_since", "unconfirmed_since"]) {
    assert.match(downloadBlock({ inputs: [{ ...decided, [field]: ["k1"] }] }, items),
      /being written again/, field);
  }
  const added = [...items, { entry_id: "n2", text: "speed 5000" }];
  assert.equal(downloadBlock({ inputs: [decided] }, added), "An update is not read yet");
  const transcribing = [...items, { entry_id: "n2", text: null }];
  assert.equal(downloadBlock({ inputs: [decided] }, transcribing), "An update is not read yet");
  assert.equal(downloadBlock({ inputs: [decided] }, null), "");
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
  // the last input deleted: the job writes the original back
  assert.deepEqual(inputsToRead([], reports), ["n1"]);
  assert.deepEqual(inputsToRead([], {}), []);
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

test("an input is applied once one of its edits is confirmed", () => {
  const note = { entry_id: "n1", text: REPORT.text };
  assert.equal(inputApplied(note, undefined), false);
  assert.equal(inputApplied(note, REPORT), false);
  assert.equal(inputApplied(note, { ...REPORT, confirmed: ["k1"] }), true);
  assert.equal(inputApplied({ ...note, applied: true }, undefined), true);
  // the report of its words knows better: its confirmation matched nothing
  assert.equal(inputApplied({ ...note, applied: true }, REPORT), false);
  // a report of other words does not
  assert.equal(inputApplied({ ...note, applied: true, text: "speed 4500" }, REPORT), true);
});

test("revising or deleting a written update warns, naming the written ones after it", () => {
  const items = ["n1", "n2", "n3", "n4"].map((id) => ({ entry_id: id, text: REPORT.text }));
  const written = { ...REPORT, confirmed: ["k1"] };
  const reports = { n2: written, n3: REPORT, n4: written };
  assert.equal(rewriteWarning("delete", items, reports, "n1"), null);
  assert.equal(rewriteWarning("delete", items, reports, "n2"),
    "This update was written into the sheet. Deleting it takes its edits out of the sheet."
    + " The 1 written update after it is read again: where what it shows changes, it needs your decision again.");
  assert.equal(rewriteWarning("revise", items, reports, "n4"),
    "This update was written into the sheet. Changing its words reads it again;"
    + " its edits need your confirmation again.");
  const all = { ...reports, n3: written };
  assert.match(rewriteWarning("revise", items, all, "n2"), / The 2 written updates after it are read again/);
  assert.equal(rewriteWarning("delete", items, reports, "gone"), null);
});

test("the state chip of an input", () => {
  const note = { entry_id: "n1", text: REPORT.text };
  const label = (item, report, running = false, canConfirm = true, asked = running) =>
    inputState(item, report, running, canConfirm, false, asked)?.label ?? null;
  assert.equal(label({ ...note, text: null }, undefined), null);
  assert.equal(label(note, undefined, false, false), null);
  assert.equal(label(note, undefined), "Waiting to be read");
  assert.equal(label(note, undefined, true), "Reading...");
  // the job reads another one, or reuses this one's answer: no spinner
  assert.equal(label(note, undefined, true, true, false), "Waiting to be read");
  // a report of other words is not this update's
  assert.equal(label({ ...note, text: "speed 4500" }, REPORT, true), "Reading...");
  assert.equal(label({ ...note, applied: true }, undefined), "Applied");
  // revised after it was applied, and now asked: read again
  assert.equal(label({ ...note, applied: true }, undefined, true), "Reading...");
  assert.equal(label({ ...note, applied: true }, undefined, true, true, false), "Applied");
  assert.equal(label(note, REPORT), "1 edit to confirm");
  assert.equal(label(note, { ...REPORT, ready: [...REPORT.ready, { ...REPORT.ready[0], key: "k2" }] }),
    "2 edits to confirm");
  assert.equal(label(note, { ...REPORT, confirmed: ["k1"], confirmed_since: ["k1"] }), "Confirmed, writing...");
  assert.equal(label(note, { ...REPORT, confirmed: ["k1"] }), "Applied");
  // partly applied: what is left to rephrase
  assert.equal(label(note, { ...REPORT, confirmed: ["k1"], unresolved: 1 }), "Applied · 1 unclear");
  assert.equal(label(note, { ...REPORT, confirmed: ["k1"], unresolved: 2, not_applied: 1 }),
    "Applied · 2 unclear · 1 not applied");
  // an earlier update changed: its decision matched nothing
  assert.equal(label(note, { ...REPORT, decide_again: 1 }), "1 edit to confirm again");
  const two = { ...REPORT, ready: [...REPORT.ready, { ...REPORT.ready[0], key: "k2" }] };
  assert.equal(label(note, { ...two, decide_again: 1 }), "2 edits to confirm · 1 decided before");
  assert.equal(label(note, { ...two, decide_again: 3 }), "2 edits to confirm again");
  assert.equal(label(note, { ...REPORT, confirmed: ["k1"], refused: "a formula error" }), "Not written");
  // rejected: decided, nothing written
  assert.equal(label(note, { ...REPORT, rejected: ["k1"] }), "Rejected");
  assert.equal(inputState(note, { ...REPORT, rejected: ["k1"] }, false, true).state, "rejected");
  assert.equal(label(note, { ...REPORT, rejected: ["k1"], unresolved: 1 }), "Rejected · 1 unclear");
  assert.equal(label(note, { ...two, confirmed: ["k1"], rejected: ["k2"] }), "Applied · 1 rejected");
  assert.equal(label(note, { ...two, rejected: ["k2"] }), "1 edit to confirm");
  assert.equal(label(note, { ...REPORT, ready: [] }), "No edit found");
  // two readings sand could not decide between: not "nothing found"
  assert.equal(label(note, { ...REPORT, ready: [], unresolved: 1 }), "Unclear");
  assert.equal(label(note, { ...REPORT, unresolved: 1 }), "1 edit to confirm");
  assert.equal(label(note, { ...REPORT, ready: [], not_applied: 1 }), "Not applied");
  assert.equal(label(note, { ...REPORT, ready: [], unchanged: 2 }), "Nothing to change");
  assert.equal(label(note, { ...REPORT, ready: [], unresolved: 1, not_applied: 1 }), "Unclear");
  assert.equal(inputState(note, REPORT, false, true).state, "ready");
  // read again first: below a change above
  const outdated = (report, running = false, asked = running) =>
    inputState(note, report, running, true, true, asked).label;
  assert.equal(outdated(REPORT), "Waiting to be read");
  assert.equal(outdated({ ...REPORT, confirmed: ["k1"] }), "Waiting to be read");
  assert.equal(outdated(REPORT, true), "Reading...");
  // the job has not asked the model about it (yet): no spinner
  assert.equal(outdated(REPORT, true, false), "Waiting to be read");
  // asked: read again, whatever it was
  assert.equal(label(note, { ...REPORT, refused: "a formula error" }, true), "Reading...");
  // the writer refused a confirmed edit: said first, whatever is undecided
  assert.equal(label(note, { ...REPORT, refused: "a formula error" }, false, true), "Not written");
});

test("the confirm-all button names the pending edits", () => {
  assert.equal(confirmAllLabel(1), "Confirm the edit");
  assert.equal(confirmAllLabel(3), "Confirm all 3 edits");
  assert.equal(confirmAllLabel(3, 1), "Confirm all 3 edits, 1 with a check");
});

test("the preview notes are what the ready edits do not show", () => {
  assert.deepEqual(previewNotes(REPORT), ["not found: \"and dry\""]);
  // the coverage warning is hidden (it mostly names the step)
  assert.deepEqual(previewNotes({ ...REPORT, preview: REPORT.preview + "\n⚠ no update in: \"step 1 cleaning\"" }),
    ["not found: \"and dry\""]);
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

test("the lines of an unclear part or a refused edit are problems, a ready edit's doubt is not", () => {
  const preview = [
    REPORT.ready[0].line,
    "   ? read from 'four thousand'",
    "ambiguous: \"the speed\"",
    "   - 2: Spin Coating · Rotation speed [rpm]",
    "   - 4: Spin Coating · Rotation speed [rpm]",
    "not found: \"and dry\"",
    "✗ not applied: \"time 9999\"",
    "   too long",
    "? sand could not propose an edit for this note. Try again, or write the note more clear.",
    "disputed: \"speed up\"",
    "   - Reading 1: 2: Spin Coating · Rotation speed [rpm] → 3000",
    "   - Reading 2: no edit",
  ].join("\n");
  assert.deepEqual([...problemLines({ preview })], preview.split("\n").slice(2));
  assert.equal(problemLines({}).size, 0);
});

test("edits only shows a problem as one short line", () => {
  assert.equal(problemShort("ambiguous: \"the speed\""), "Ambiguous: \"the speed\"");
  // read before the ? went
  assert.equal(problemShort("? ambiguous: \"the speed\""), "Ambiguous: \"the speed\"");
  assert.equal(problemShort("disputed: \"speed up\""), "Disputed: \"speed up\"");
  assert.equal(problemShort("? disputed: \"speed up\""), "Disputed: \"speed up\"");
  assert.equal(problemShort("not found: \"and dry\""), "Not found: \"and dry\"");
  assert.equal(problemShort("? not found: \"and dry\""), "Not found: \"and dry\"");
  assert.equal(problemShort("✗ not applied: \"time 9999\""), "Not applied: \"time 9999\"");
  assert.equal(problemShort("? sand could not propose an edit for this note. Try again, or write the note more clear."),
    "No edit proposed");
  assert.equal(problemShort("   or 4: Spin Coating · Rotation speed [rpm]?"), null);
});

test("the summary line of a completed job", () => {
  assert.equal(editSummary({ changes: 0, inputs: [] }), "Nothing written yet.");
  assert.equal(editSummary({ changes: 1, inputs: [REPORT] }), "1 edit waits for your confirmation");
  assert.equal(editSummary({ changes: 1, inputs: [{ ...REPORT, ready: [], unconfirmed_since: ["k0"] }] }),
    "1 edit of a changed update, being taken out");
  assert.equal(editSummary({ changes: 1, inputs: [{ ...REPORT, decide_again: 1 }] }),
    "1 edit waits for your confirmation · 1 update was read again after an earlier update changed");
  assert.equal(
    editSummary({ changes: 0, inputs: [{ ...REPORT, confirmed: ["k1"], confirmed_since: ["k1"] }] }),
    "1 edit confirmed, being written");
  assert.equal(editSummary({ changes: 0, inputs: [{ ...REPORT, rejected: ["k1"] }] }), "1 edit rejected");
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

test("the download name comes from the server when it says one", () => {
  assert.equal(
    downloadFilename("attachment; filename=\"Batch 7 _.xlsx\"; filename*=UTF-8''Batch%207%20%CE%B1.xlsx", "x.xlsx"),
    "Batch 7 α.xlsx");
  assert.equal(downloadFilename("attachment; filename=\"batch.xlsx\"", "x.xlsx"), "batch.xlsx");
  assert.equal(downloadFilename(null, "x.xlsx"), "x.xlsx");
  assert.equal(downloadFilename("attachment; filename*=UTF-8''%E0%A4%A", "x.xlsx"), "x.xlsx");
});


test("confirm all takes the edits a word check marked, and counts them", () => {
  const report = {
    ready: [{ key: "k1" }, { key: "k2", checks: ["'4400' is not in your words"] }, { key: "k3", checks: [] }],
    confirmed: [],
  };
  assert.deepEqual(pendingKeys([report]), ["k1", "k2", "k3"]);
  assert.deepEqual(checkedKeys([report]), ["k2"]);
  assert.deepEqual(checkedKeys([{ ...report, confirmed: ["k2"] }]), []);
});

test("confirm all says how many of its edits have a check", () => {
  assert.equal(confirmAllLabel(3, 1), "Confirm all 3 edits, 1 with a check");
  assert.equal(confirmAllLabel(1, 1), "Confirm the edit, with a check");
  assert.equal(confirmAllLabel(3, 3), "Confirm all 3 edits, all with a check");
  assert.equal(confirmAllLabel(2), "Confirm all 2 edits");
});

test("an update a decision above changed says its decisions are asked again", () => {
  assert.equal(resetNote(REPORT), null);
  assert.equal(resetNote({ ...REPORT, decide_again: 1 }),
    "An update above changed what this one shows: your decision here is asked again.");
  assert.match(resetNote({ ...REPORT, decide_again: 2 }), /your 2 decisions here are asked again/);
});

test("a held edit is confirmed, not written, and waits for no write", () => {
  const held = { ...REPORT, confirmed: ["k1"], held: ["k1"] };
  assert.deepEqual(inputState({ text: REPORT.text }, held, false, true),
    { state: "held", label: "Confirmed, waiting for the updates above" });
  assert.equal(inputApplied({ text: REPORT.text }, held), false);
  assert.equal(writtenCount({ inputs: [held] }), 0);
  assert.equal(unwrittenKey({ inputs: [held] }), "");
  assert.deepEqual(pendingKeys([held]), []);
  assert.match(editSummary({ inputs: [held] }), /1 edit confirmed waits for the updates above/);
});

test("a rejection made or taken back since the last run is written by the next", () => {
  const two = { ...REPORT, input: 2, entry_id: "n2", ready: [{ key: "k2", line: "..." }] };
  const rejected = { ...REPORT, rejected: ["k1"], rejected_since: ["k1"] };
  assert.deepEqual([...outdatedEntries(null, reportsByEntry({ inputs: [rejected, two] }))], ["n2"]);
  assert.equal(unwrittenKey({ inputs: [rejected] }), "rejected k1");
  // rejected again after the rejection was taken back: another key
  assert.notEqual(unwrittenKey({ inputs: [rejected] }), unwrittenKey({ inputs: [{ ...REPORT, unrejected_since: ["k1"] }] }));
  const undone = { ...REPORT, unrejected_since: ["k1"] };
  assert.deepEqual([...outdatedEntries(null, reportsByEntry({ inputs: [undone, two] }))], ["n2"]);
  assert.match(editSummary({ inputs: [undone] }), /1 rejection taken back/);
});


test("Confirm all asks what it confirms, and says skipped updates are locked too", () => {
  const status = { inputs: [
    { entry_id: "n1", ready: [{ key: "a" }, { key: "b" }] },
    { entry_id: "n2", ready: [{ key: "c" }], rejected: ["c"] },
    { entry_id: "n3", ready: [], unresolved: 1 },
  ] };
  assert.equal(confirmQuestion(status),
    "Confirm 2 edits in 1 update? Then all updates are locked. 1 update has an unclear part, not applied.");
  assert.ok(!confirmQuestion({ inputs: status.inputs.slice(0, 1) }).includes("unclear"));
});

test("an untick whose edit is gone after the reread no longer applies", () => {
  const sent = [
    { key: "a", entry: "n1", line: "speed 4000" },
    { key: "b", entry: "n2", line: "time 10 s" },
    { key: "c", entry: "n3", line: "anneal" },
  ];
  const status = { inputs: [
    { entry_id: "n1", ready: [{ key: "a" }] },
    { entry_id: "n2", ready: [{ key: "b2" }] },
  ] };
  assert.deepEqual(lostUnticks(sent, status), { n2: ["time 10 s"], n3: ["anneal"] });
});

test("Recompute counts the round's changes", () => {
  assert.equal(recomputeLabel(1), "Recompute (1 change)");
  assert.equal(recomputeLabel(3), "Recompute (3 changes)");
});

test("changes recompute by themselves, not twice for the same reason", () => {
  const last = { read: "inputs A", round: "" };
  assert.equal(recomputeDue(0, "inputs B", "", last), false);
  // an update added, revised or deleted
  assert.equal(recomputeDue(1, "inputs B", "", last), true);
  // the job for these inputs failed
  assert.equal(recomputeDue(1, "inputs A", "", last), false);
  // an untick; its send failed, then another untick
  assert.equal(recomputeDue(1, "inputs A", "round 1", last), true);
  assert.equal(recomputeDue(1, "inputs A", "round 1", { ...last, round: "round 1" }), false);
  assert.equal(recomputeDue(2, "inputs A", "round 2", { ...last, round: "round 1" }), true);
});

test("a failed job holds back the inputs it was started for, not one added since", () => {
  const one = [{ entry_id: "n1", text: "speed 4000" }];
  const two = [...one, { entry_id: "n2", text: "anneal 120" }];
  const failed = failedReadKey(readKey(one), two);
  assert.equal(recomputeDue(1, readKey(two), "", { read: failed, round: null }), true);
  assert.equal(recomputeDue(1, readKey(one), "", { read: failed, round: null }), false);
  // a reload onto the failed status: the inputs shown
  assert.equal(failedReadKey(null, two), readKey(two));
  assert.equal(failedReadKey(null, null), null);
});

test("a job keeps the reports of the notes above the first change (#215)", () => {
  const items = [
    { entry_id: "n1", text: "a" }, { entry_id: "n2", text: "b" },
    { entry_id: "n3", text: "c" }, { entry_id: "n4", text: "d" },
  ];
  const status = {
    inputs: [
      { input: 2, entry_id: "n2", text: "b" }, { input: 1, entry_id: "n1", text: "a" },
      { input: 3, entry_id: "n3", text: "c" }, { input: 4, entry_id: "n4", text: "d" },
    ],
  };
  const kept = (its, decided) => [...unchangedAbove(its, status, decided)];
  assert.deepEqual(kept(items), ["n1", "n2", "n3", "n4"]);
  // note 3 revised, deleted, or decided in a round
  assert.deepEqual(kept(items.map((i) => (i.entry_id === "n3" ? { ...i, text: "c2" } : i))), ["n1", "n2"]);
  assert.deepEqual(kept(items.filter((i) => i.entry_id !== "n3")), ["n1", "n2"]);
  // a rejection of note 3's edit: note 3 is not read again, note 4 is
  assert.deepEqual(kept(items, new Set(["n3"])), ["n1", "n2", "n3"]);
  // a note added at the end leaves all others
  assert.deepEqual(kept([...items, { entry_id: "n5", text: "e" }]), ["n1", "n2", "n3", "n4"]);
  // a decision the last run did not know changes what comes after it
  const since = { inputs: status.inputs.map((r) => (r.entry_id === "n2" ? { ...r, rejected_since: ["k"] } : r)) };
  assert.deepEqual([...unchangedAbove(items, since)], ["n1", "n2"]);
  assert.deepEqual([...unchangedAbove(items, null)], []);
});

test("the kept notes hold for the started workflow only; the reservation reads none", () => {
  const kept = new Set(["n1"]);
  const last = [{ entry_id: "n1" }, { entry_id: "n2" }];
  // the reservation carries the last run's reports: note 2 is not read yet
  const reserved = freshReports({ job_id: "starting", inputs: last }, true, { kept, job: "w1" });
  assert.deepEqual([...reserved.fresh], ["n1"]);
  const ours = freshReports({ job_id: "w1", inputs: [{ entry_id: "n2" }] }, true, { kept, job: "w1" });
  assert.deepEqual([...ours.fresh].sort(), ["n1", "n2"]);
  // another tab's workflow: nothing kept
  const other = freshReports({ job_id: "w2", inputs: [] }, true, { kept, job: "w1" });
  assert.deepEqual([...other.fresh], []);
  assert.equal(other.kept.size, 0);
  assert.deepEqual(freshReports({ job_id: "w1" }, false, { kept, job: "w1" }), { fresh: null, kept: new Set() });
});

test("a ready edit's checks are not shown twice, its new term not at all", () => {
  const line = REPORT.ready[0].line;
  const report = {
    ...REPORT,
    preview: [line, "   · new term: NiO2", "   ⚠ check: you did not say 'NiOx'",
      "= 2: Spin Coating · Rotation time [s]", "   ⚠ check: of a no-op"].join("\n"),
  };
  assert.deepEqual(previewNotes(report), [
    "= 2: Spin Coating · Rotation time [s]", "   ⚠ check: of a no-op",
  ]);
});
