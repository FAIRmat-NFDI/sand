import assert from "node:assert/strict";
import { test } from "node:test";

import {
  anyRound, clearRound, clearSent, isRestored, isUnticked, keepRejected, onRoundChange, restore, roundBody,
  roundFor, roundSize, sentUnticks, tick, untick,
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

test("each experiment keeps its own round", () => {
  roundFor("c1");
  clearRound();
  untick({ key: "a", line: "x" }, "n1");
  roundFor("c2");
  assert.equal(roundSize(), 0);
  assert.ok(anyRound()); // c1's round is not sent yet
  roundFor("c1");
  assert.ok(isUnticked("a"));
  clearRound();
  roundFor("c2");
  assert.ok(!anyRound());
});

test("a round applied clears what it sent, not what changed meanwhile", () => {
  roundFor("c3");
  untick({ key: "a", line: "x" }, "n1");
  untick({ key: "c", line: "z" }, "n1");
  const body = roundBody();
  untick({ key: "b", line: "y" }, "n2"); // while the request is on its way
  untick({ key: "c", line: "z" }, "n1", { reason: "value", meant: "5" }); // its reason changed
  clearSent(body, "c3");
  assert.ok(!isUnticked("a") && isUnticked("b") && isUnticked("c"));
  const second = roundBody();
  roundFor("c4"); // the page moved on before the answer
  clearSent(second, "c3");
  roundFor("c3");
  assert.equal(roundSize(), 0);
  clearRound();
});
