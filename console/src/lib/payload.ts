/** The boundary between the server's JSON and this app's types.
 *
 * `types.ts` says what each payload *is*, and the compiler believes it. Nothing
 * checks it. That is fine while both sides ship together, and it is exactly the
 * situation that stops being true: the bundle is committed and served from a
 * CDN, so a browser can be running last week's build against this week's server,
 * and a Render service that half-answered — a 200 whose body is `{error: ...}`
 * from a proxy, a payload with a key the page needs missing — arrives looking
 * like success. The page then fails inside a `.map()`, and the reader is shown a
 * stack trace's worth of nothing instead of a sentence.
 *
 * So every response passes through `expectPayload` on its way out of `api.ts`,
 * and a payload that does not match its schema becomes an ordinary
 * `ApiError` — the same failure card the pages already draw, with the missing
 * key named in it.
 *
 * Three deliberate limits, because a hand-rolled validator that oversells itself
 * is worse than none:
 *
 * 1. **Shallow, and typed.** It checks the *top-level* keys and their kinds —
 *    which is where the crashes were — and never the shape of a nested object.
 *    A check that walked the whole tree would be a second schema to maintain,
 *    and the first thing to go stale would be it.
 * 2. **Required keys only.** Extra keys are ignored, so the server may add a
 *    field without a console release; a *removed* one is what this catches.
 * 3. **Keyed by path.** One schema per endpoint, and the table is the contract:
 *    `tests/test_console.py` reads these paths and fails if one of them is not a
 *    route the app actually serves, so a rename cannot leave a schema behind
 *    pointing at nothing.
 */

import { ApiError } from "./errors";

type Shape = "object" | "array" | "string" | "number" | "boolean";

/** The required keys of each payload this app reads, and what kind each is. */
export const SCHEMAS: Record<string, Record<string, Shape>> = {
  "/api/console/overview": {
    success: "boolean",
    totals: "object",
    gateway: "object",
    bot: "object",
    uptime_seconds: "number",
    figures: "object",
    health: "object",
  },
  "/api/console/fleet": {
    success: "boolean",
    servers: "array",
    totals: "object",
  },
  // A family rather than an endpoint: the path carries a server id.
  "/api/console/server": {
    success: "boolean",
    server: "object",
    systems: "array",
  },
  "/api/console/live": {
    success: "boolean",
    server: "object",
    servers: "array",
    gateway: "object",
    // Not decoration: every figure on the page is drawn from this block, and a
    // payload without it would paint zeros over readings nobody took.
    read: "object",
    channels: "array",
    totals: "object",
    at: "number",
  },
  "/api/console/systems": {
    success: "boolean",
    systems: "array",
    servers: "array",
    matrix: "object",
  },
  "/api/console/commands": {
    success: "boolean",
    commands: "array",
    servers: "array",
    totals: "object",
  },
  "/api/console/economy": {
    success: "boolean",
    server: "object",
    servers: "array",
    totals: "object",
    top: "array",
  },
  // The god tier. The read is shallow-checked like the rest; the three write
  // payloads below it are checked for `success` alone, because what they change
  // is reported in the sentences (`said`, `error`) rather than in structure — and
  // a page that grew a required key here would fail on the *older* server it is
  // meant to keep working against.
  "/api/console/god": {
    success: "boolean",
    locked: "boolean",
    paused: "object",
    actions: "array",
    fleet: "array",
    totals: "object",
    recent: "array",
  },
  "/api/console/god/unlock": { success: "boolean", locked: "boolean" },
  "/api/console/god/lock": { success: "boolean", locked: "boolean" },
  "/api/console/god/act": { success: "boolean" },
  // The fleet announcement: the same shallow check, for the same reason. Its
  // `report` is the dry run the caller reads — and it is the one payload here
  // whose size is a property of the *deployment* (a row per server), so the
  // schema does not try to describe it.
  "/api/console/god/broadcast": { success: "boolean" },
  "/api/console/ops": {
    success: "boolean",
    logs: "array",
    uptime_seconds: "number",
    gateway: "object",
    health: "object",
  },
  "/api/console/changes": {
    success: "boolean",
    entries: "array",
    total: "number",
  },
  "/api/console/access": {
    success: "boolean",
    sessions: "object",
    limits: "object",
    doors: "object",
    process: "object",
    read: "object",
  },
  // The writes. Cheap to describe and worth it for the same reason as the reads:
  // a refresh landing mid-deploy is how a form ends up believing a failed toggle
  // succeeded.
  "/api/console/access/revoke": { success: "boolean" },
  // The operator's controls over a server as a whole. `success` and nothing
  // else is required on purpose: a refusal answers `{success: false, error}` and
  // a success answers `{success: true, said}` — the page reads whichever is
  // there, and pinning `said` would make the *refusals* fail the boundary check
  // before the page could show their sentence.
  "/api/console/server-action": { success: "boolean" },
  "/api/console/economy/adjust": {
    success: "boolean",
    guild_id: "string",
    user_id: "string",
    wallet: "number",
    bank: "number",
  },
  "/api/toggle": { success: "boolean", guild_id: "string" },
  "/api/toggle-all": { success: "boolean", guild_id: "string" },
  // The dashboard's own endpoint, reused rather than reimplemented. `available`
  // is the count of commands Discord actually holds for this server.
  "/api/command-scope": { success: "boolean" },
};

/** The endpoint a request path names, with its query string dropped.
 *
 * `noUncheckedIndexedAccess` is on in this project, so a split's first element is
 * `string | undefined` — and the honest fallback is the whole path, which then
 * finds no schema and reports *that* rather than crashing on the way. */
export function pathKey(path: string): string {
  return path.split("?")[0] ?? path;
}

/**
 * A schema for a server-side route, or `undefined` when this build has none.
 *
 * Exact match first, then the longest prefix that ends at a `/` — so a
 * parameterised route (`/api/console/server/154227…`) is covered by its
 * family's schema (`/api/console/server`) and the table does not have to name an
 * id that can be anything. Longest-first, because `/api/console/economy` and
 * `/api/console/economy/adjust` are two different payloads and the shorter one
 * must not win.
 */
export function schemaFor(path: string): Record<string, Shape> | undefined {
  const key = pathKey(path);
  const exact = SCHEMAS[key];
  if (exact) return exact;
  const families = Object.keys(SCHEMAS)
    .filter((candidate) => key.startsWith(candidate + "/"))
    .sort((a, b) => b.length - a.length);
  for (const family of families) {
    const schema = SCHEMAS[family];
    if (schema) return schema;
  }
  return undefined;
}

function shapeOf(value: unknown): Shape | "null" | "undefined" {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value as Shape | "undefined";
}

/** The path a payload came from, and what was wrong with it. */
export class PayloadError extends ApiError {
  constructor(
    readonly path: string,
    readonly problem: string,
    status: number,
  ) {
    super(
      `The server answered something this build does not understand (${problem}). ` +
        "Reload the page; if it happens again, the console and the bot are out of step.",
      status,
    );
    this.name = "PayloadError";
  }
}

/**
 * Check a payload against its schema and hand it back typed.
 *
 * A missing *schema* is not a payload problem: it means the app fetched a path
 * this build does not know, which is a programming error rather than a server
 * one — so it throws the same class with that spelled out, instead of silently
 * letting the caller pass.
 */
export function expectPayload<T>(path: string, data: unknown, status = 200): T {
  const key = pathKey(path);
  const schema = schemaFor(key);
  if (!schema) {
    throw new PayloadError(key, "this build has no schema for that endpoint", status);
  }
  if (shapeOf(data) !== "object") {
    throw new PayloadError(key, `expected an object, got ${shapeOf(data)}`, status);
  }
  const body = data as Record<string, unknown>;
  for (const field of Object.keys(schema)) {
    const expected = schema[field];
    if (!expected) continue;
    const actual = shapeOf(body[field]);
    if (actual === "undefined") {
      throw new PayloadError(key, `the payload has no \`${field}\``, status);
    }
    if (actual !== expected) {
      throw new PayloadError(key, `\`${field}\` should be ${expected}, is ${actual}`, status);
    }
  }
  return data as T;
}
