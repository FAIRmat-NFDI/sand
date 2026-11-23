import assert from "node:assert/strict";
import { test } from "node:test";

import { liveTranscriptUrl } from "../../src/sand/apis/static/live-transcript-logic.js";

const PAGE = "http://localhost:8000/nomad-oasis/dashboards/sand/";

test("the relay sits next to the page, over a socket", () => {
  assert.equal(
    liveTranscriptUrl(PAGE, []),
    "ws://localhost:8000/nomad-oasis/dashboards/sand/api/live-transcript",
  );
  assert.match(liveTranscriptUrl("https://lab.example/sand/", []), /^wss:/);
});

test("each key term is its own parameter, in order", () => {
  const url = new URL(liveTranscriptUrl(PAGE, ["MAI+MABr", "Spin Coating"]));

  assert.deepEqual(url.searchParams.getAll("keyterm"), ["MAI+MABr", "Spin Coating"]);
});
