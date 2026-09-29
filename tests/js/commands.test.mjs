import assert from "node:assert/strict";
import { test } from "node:test";

import { COMMANDS, GRAMMAR, commandIn, stopSaidIn, withoutStop } from "../../src/sand/apis/static/voice/commands.js";

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

test("the stop command at the end of a transcript", () => {
  for (const text of ["Hey, Sand. Stop.", "and then we wait. Hey Sand, stop!", "hey sam stop",
    "Hi Sand, stop recording.", "Hey, send stop", "...five millilitres hey sand stop the recording"]) {
    assert.ok(stopSaidIn(text), text);
  }
});

test("a transcript that does not end with the stop command", () => {
  for (const text of ["", "stop", "then we stop", "sand stop", "Hey Sand", "Hey Sand, stop the pump",
    "Hey Sand, stop. And then", "they sand and stop", "hey, stop"]) {
    assert.equal(stopSaidIn(text), false, text);
    assert.equal(withoutStop(text), text);
  }
});

test("the stop command is taken off the transcript", () => {
  assert.equal(withoutStop("Add five millilitres. Hey, Sand. Stop."), "Add five millilitres.");
  assert.equal(withoutStop("the film is dry, hey Sam stop recording"), "the film is dry,");
  assert.equal(withoutStop("it is 80 degrees - hey sand - stop."), "it is 80 degrees");
  assert.equal(withoutStop("Hey Sand, stop."), "");
});
