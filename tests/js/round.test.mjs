import assert from "node:assert/strict";
import { test } from "node:test";

import {
  clearRound, isRestored, isUnticked, keepRejected, onRoundChange, restore, roundBody, roundFor, roundSize,
  sentUnticks, tick, untick,
} from "../../src/sand/apis/static/round.js";

test("a round holds unticks with their reasons and re-ticks, until cleared", () => {
  roundFor("c1");
  let heard = 0;
  onRoundChange(() => { heard += 1; });
  untick({ key: "a", line: "speed 4000" }, "n1", { reason: "value", meant: "3000" });
  untick({ key: "b", line: "time 10 s" }, "n2");
  restore({ key: "r", line: "anneal" }, "n3");
  assert.equal(roundSize(), 3);
  assert.ok(isUnticked("a") && isRestored("r"));
  assert.deepEqual(roundBody(), {
    reject: [{ key: "a", reason: "value", meant: "3000" }, { key: "b", reason: null, meant: "" }],
    restore: ["r"],
  });
  assert.deepEqual(sentUnticks().map((u) => u.entry), ["n1", "n2"]);
  tick("b");
  keepRejected("r");
  assert.equal(roundSize(), 1);
  clearRound();
  assert.equal(roundSize(), 0);
  assert.equal(heard, 6);
});

test("another experiment starts an empty round", () => {
  roundFor("c1");
  untick({ key: "a", line: "x" }, "n1");
  roundFor("c2");
  assert.equal(roundSize(), 0);
  roundFor("c2");
  untick({ key: "a", line: "x" }, "n1");
  roundFor("c2");
  assert.equal(roundSize(), 1);
});
