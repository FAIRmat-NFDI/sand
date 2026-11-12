import assert from "node:assert/strict";
import { test } from "node:test";

import {
  editReportBody, pageReportBody, WRONG_REASONS,
} from "../../src/sand/apis/static/feedback-logic.js";

test("a page report carries the words, trimmed", () => {
  assert.deepEqual(
    pageReportBody({ wrong: " it hangs ", expected: "", userAgent: "ua", errors: ["e"] }),
    { what_wrong: "it hangs", expected: "", page: "sheet", user_agent: "ua", recent_errors: ["e"] },
  );
});

test("an edit report names the edit by its key and needs no words", () => {
  assert.deepEqual(
    editReportBody({ key: "k1", reason: "samples", meant: " 1 to 3 ", userAgent: "ua", errors: [] }),
    {
      what_wrong: "",
      expected: "1 to 3",
      page: "sheet/edit",
      user_agent: "ua",
      recent_errors: [],
      edit: { key: "k1", reason: "samples" },
    },
  );
});

test("the reasons are the ones the API accepts", () => {
  assert.deepEqual(WRONG_REASONS.map(([value]) => value), ["column", "samples", "value", "other"]);
});
