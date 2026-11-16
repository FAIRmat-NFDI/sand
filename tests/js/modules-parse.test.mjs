// Every page module must parse: one syntax error stops the whole page
// script, and the buttons silently do nothing. The other tests import only
// the *-logic.js files, which need no DOM.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const STATIC = fileURLToPath(new URL("../../src/sand/apis/static/", import.meta.url));

function modules(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? modules(join(dir, e.name)) : e.name.endsWith(".js") ? [join(dir, e.name)] : [],
  );
}

for (const file of modules(STATIC)) {
  test(`${file.slice(STATIC.length)} parses`, () => {
    const check = spawnSync(process.execPath, ["--input-type=module", "--check"], {
      input: readFileSync(file),
    });
    assert.equal(check.status, 0, check.stderr.toString());
  });
}
