import assert from "node:assert/strict";
import { test } from "node:test";

import { joinFinal } from "../../src/sand/apis/static/transcript-text.js";

test("words are joined with a space", () => {
  assert.equal(joinFinal("And so", "my fellow Americans"), "And so my fellow Americans");
});

test("punctuation attaches to the text before it", () => {
  let text = "";
  for (const final of ["And so, my fellow Americans", ",", "ask", "not", ".", "Ask"]) {
    text = joinFinal(text, final);
  }
  assert.equal(text, "And so, my fellow Americans, ask not. Ask");
});
