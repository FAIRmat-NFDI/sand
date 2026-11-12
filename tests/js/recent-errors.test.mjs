import assert from "node:assert/strict";
import { test } from "node:test";

import {
  recentErrors, rememberError, requestLine, watchPage,
} from "../../src/sand/apis/static/recent-errors.js";

const AT = new Date("2026-11-12T10:15:02Z");

test("a request line drops the host and the query", () => {
  assert.equal(
    requestLine("post", "http://n/dash/api/input-collections/u1/edit-async?collection_entry_id=c1", 502),
    "POST /dash/api/input-collections/u1/edit-async 502",
  );
  assert.equal(requestLine(undefined, "api/me", 401), "GET api/me 401");
});

test("only the newest errors are kept, each cut to length", () => {
  for (let i = 0; i < 25; i++) rememberError("error " + i, AT);
  rememberError("x".repeat(2000), AT);
  const errors = recentErrors();
  assert.equal(errors.length, 20);
  assert.equal(errors[0], "10:15:02 error 6");
  assert.equal(errors.at(-1).length, 1000);
});

test("watched fetches remember failures, not successes", async () => {
  const listeners = {};
  const win = {
    fetch: async (url) => {
      if (url === "down") throw new Error("network");
      return { ok: url === "fine", status: url === "fine" ? 200 : 404 };
    },
    addEventListener: (name, fn) => { listeners[name] = fn; },
  };
  watchPage(win);

  await win.fetch("fine");
  await win.fetch("missing", { method: "DELETE" });
  await assert.rejects(win.fetch("down"));
  listeners.error({ message: "x is undefined" });

  // the time prefix ("10:15:02 ") cut off
  const tail = recentErrors().slice(-3).map((line) => line.slice(9));
  assert.deepEqual(tail, ["DELETE missing 404", "GET down failed: network", "JS error: x is undefined"]);
});
