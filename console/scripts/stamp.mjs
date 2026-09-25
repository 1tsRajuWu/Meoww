/**
 * Stamp the built bundle with a hash of the sources it was built from.
 *
 * `static/console/` **is committed**, which this repository otherwise refuses to
 * do with generated output (`_site/`, `_hub/` are both ignored, on the recorded
 * grounds that "a committed copy is one that goes stale quietly"). The bundle is
 * the exception for one reason: the host that serves the console is a Python
 * runtime on a free tier, and it has no guaranteed Node — a deploy that had to
 * build the app could fail to build it, which is worse than a bundle that is one
 * commit behind.
 *
 * So the rule is kept in substance rather than in letter: the copy is committed,
 * and it **cannot** be stale quietly, because this file writes down exactly which
 * sources produced it and `tests/test_console.py` recomputes that hash. Edit
 * anything under `console/src/` without rebuilding and the suite says so.
 *
 * The hash is taken over sorted `relpath + NUL + bytes` pairs so the Python and
 * JavaScript sides can agree byte for byte without either owning the format.
 */
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const consoleRoot = join(here, "..");
const repoRoot = join(consoleRoot, "..");

/** The inputs whose change means the bundle is out of date. */
const SOURCES = ["src", "package.json", "tsconfig.json", "vite.config.ts", "index.html"];

function collect(entry) {
  const absolute = join(consoleRoot, entry);
  if (statSync(absolute).isFile()) return [absolute];
  const out = [];
  for (const child of readdirSync(absolute)) {
    out.push(...collect(join(entry, child)));
  }
  return out;
}

export function sourceHash() {
  const files = SOURCES.flatMap(collect)
    .map((absolute) => ({ absolute, rel: relative(consoleRoot, absolute).split("\\").join("/") }))
    .sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0));

  const hash = createHash("sha256");
  for (const file of files) {
    hash.update(file.rel, "utf8");
    hash.update(Buffer.from([0]));
    hash.update(readFileSync(file.absolute));
  }
  return { hash: hash.digest("hex"), count: files.length };
}

const { hash, count } = sourceHash();
const out = join(repoRoot, "static", "console", "build.json");
writeFileSync(
  out,
  `${JSON.stringify(
    {
      hash,
      sources: count,
      builtAt: new Date().toISOString(),
      note: "Written by console/scripts/stamp.mjs. tests/test_console.py checks this against console/src.",
    },
    null,
    2,
  )}\n`,
);
console.log(`console: stamped ${count} sources as ${hash.slice(0, 12)}`);
