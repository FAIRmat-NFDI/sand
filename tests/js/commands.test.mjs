import assert from "node:assert/strict";
import { test } from "node:test";

import { COMMANDS, GRAMMAR, commandIn } from "../../src/sand/apis/static/voice/commands.js";

test("every phrase gives its command", () => {
  for (const [command, phrases] of Object.entries(COMMANDS)) {
    for (const phrase of phrases) assert.equal(commandIn(phrase), command, phrase);
  }
});

test("a command counts at the end of what was said", () => {
  assert.equal(commandIn("[unk] [unk] [unk] hey sand stop"), "stop");
  assert.equal(commandIn("[unk] hey sand start record"), "start");
  assert.equal(commandIn("  hey sand   stop  "), "stop");
});

test("a command in the middle does nothing", () => {
  assert.equal(commandIn("hey sand stop [unk] [unk]"), null);
  assert.equal(commandIn("hey sand start record [unk]"), null);
});

test("the longest phrase wins", () => {
  assert.equal(commandIn("hey sand stop recording"), "stop");
  assert.equal(commandIn("hey sand start recording"), "start");
  assert.equal(commandIn("hey sand"), "test");
});

test("other talk is no command", () => {
  for (const text of ["", "[unk]", "[unk] [unk]", "stop", "sand stop", "start record",
    "hey sam stop", "they sand stop", "hey sandstop"]) {
    assert.equal(commandIn(text), null, text);
  }
});

test("the words of a command in another order, or alone, are no command", () => {
  // as the recognizer reported them for ordinary talk
  for (const text of ["stop sand hey", "[unk] stop [unk]", "start [unk] record",
    "sand sand record", "hey [unk] sand stop", "hey sand [unk] stop"]) {
    assert.equal(commandIn(text), null, text);
  }
});

test("the grammar holds every phrase and the unknown word", () => {
  assert.ok(GRAMMAR.includes("[unk]"));
  for (const phrase of Object.values(COMMANDS).flat()) assert.ok(GRAMMAR.includes(phrase));
});
