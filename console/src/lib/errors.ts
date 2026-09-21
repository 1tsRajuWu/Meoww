/** The one error type this app throws, in its own module for a dull reason.
 *
 * It lived in `api.ts`, and `lib/payload.ts` — the boundary that checks every
 * payload before a page sees it — extends it. `api.ts` imports the check, the
 * check imports the class, and a class that is not defined yet when the other
 * module evaluates is not a cycle JavaScript forgives: `extends ApiError` would
 * run against `undefined` at import time. So the class lives here, alone, and
 * `api.ts` re-exports it so the three pages that catch it keep compiling.
 */

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
