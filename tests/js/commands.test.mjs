import assert from "node:assert/strict";
import { test } from "node:test";

import { COMMANDS, GRAMMAR, commandIn, stopSaidIn, withoutStop } from "../../src/sand/apis/static/voice/commands.js";

test("every phrase gives its command", () => {
  for (const [command, phrases] of Object.entries(COMMANDS)) {
    for (const phrase of phrases) assert.equal(commandIn(phrase), command, phrase);
  }
});

test("a command counts at the end of what was said", () => {
  assert.equal(commandIn("[unk] [unk] [unk] hey sand stop recording"), "stop");
  assert.equal(commandIn("[unk] hey sand start record"), "start");
  assert.equal(commandIn("  hey sand   stop   recording "), "stop");
});

test("hey and hi, with every ending", () => {
  for (const call of ["hey sand", "hi sand"]) {
    for (const ending of ["start record", "start recording", "record"]) {
      assert.equal(commandIn(call + " " + ending), "start", call + " " + ending);
    }
    for (const ending of ["stop recording", "stop the recording", "stop record"]) {
      assert.equal(commandIn(call + " " + ending), "stop", call + " " + ending);
    }
    assert.equal(commandIn(call), "test", call);
  }
});

test("a command in the middle does nothing", () => {
  assert.equal(commandIn("hey sand stop recording [unk] [unk]"), null);
  assert.equal(commandIn("hey sand start record [unk]"), null);
});

test("the longest phrase wins", () => {
  assert.equal(commandIn("hey sand stop the recording"), "stop");
  assert.equal(commandIn("hey sand start recording"), "start");
  assert.equal(commandIn("hey sand"), "test");
});

test("other talk is no command", () => {
  for (const text of ["", "[unk]", "[unk] [unk]", "stop", "sand stop", "start record",
    "hey sam stop recording", "they sand stop recording", "hey sandstop recording",
    // "stop" alone is no command, whatever comes before
    "hey sand stop", "hi sand stop", "[unk] hey sand stop"]) {
    assert.equal(commandIn(text), null, text);
  }
});

test("the words of a command in another order, or alone, are no command", () => {
  // as the recognizer reported them for ordinary talk
  for (const text of ["stop sand hey", "[unk] stop [unk]", "start [unk] record",
    "sand sand record", "hey [unk] sand stop recording", "hey sand [unk] stop recording",
    "hey sand stop [unk]"]) {
    assert.equal(commandIn(text), null, text);
  }
});

test("the grammar holds every phrase and the unknown word", () => {
  assert.ok(GRAMMAR.includes("[unk]"));
  for (const phrase of Object.values(COMMANDS).flat()) assert.ok(GRAMMAR.includes(phrase));
});

test("the stop command at the end of a transcript", () => {
  for (const text of ["Hey, Sand. Stop recording.", "and then we wait. Hey Sand, stop recording!",
    "hey sam stop recording", "Hi Sand, stop the recording.", "Hey, send stop record",
    "Hello Sand, stop recording.", "Hello, Sam. Stop the record.", "...five millilitres hey sand stop the recording"]) {
    assert.ok(stopSaidIn(text), text);
  }
});

test("a transcript that does not end with the stop command", () => {
  for (const text of ["", "stop", "then we stop recording", "sand stop recording", "Hey Sand",
    "Hey Sand, stop the pump", "Hey Sand, stop recording. And then", "they sand and stop recording",
    "hey, stop recording", "stop the recording",
    // "stop" alone is no command: said to a person, or about anything
    "Hey Sand, stop.", "Hey Sam, stop!", "and then we wait. Hey Sand, stop"]) {
    assert.equal(stopSaidIn(text), false, text);
    assert.equal(withoutStop(text), text);
  }
});

test("the stop command is taken off the transcript", () => {
  assert.equal(withoutStop("Add five millilitres. Hey, Sand. Stop recording."), "Add five millilitres.");
  assert.equal(withoutStop("the film is dry, hey Sam stop recording"), "the film is dry,");
  assert.equal(withoutStop("it is 80 degrees - hey sand - stop the recording."), "it is 80 degrees");
  assert.equal(withoutStop("Hey Sand, stop recording."), "");
  assert.equal(withoutStop("The film is dry. Hello Sand, stop the record."), "The film is dry.");
});
