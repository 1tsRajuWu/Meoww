/** A number the console is allowed to print, and the reason when it is not.
 *
 * Every payload this app reads has some part of it that can legitimately be
 * missing: a cold process knows no channels, the Presence intent is routinely
 * off, a build machine has no tracker tables, the ledger cannot be read on a
 * host whose disk is full. Each of those is a *different sentence*, and the one
 * thing they must never become is a zero — a page that printed `0` for "nobody
 * is in voice" when the truth was "the gateway has not answered" has invented a
 * measurement, which is worse than printing nothing.
 *
 * So a figure is either read or it is not, and the not-case carries its reason:
 *
 *   const figures = readFlags(data.read);
 *   <Figure label="in voice" read={figures.voice} value={compact(totals.in_voice)} />
 *
 * `Figure` (in `ui.tsx`) draws the em dash and keeps the reason as its tooltip,
 * and the page's own gap notice — the list of sentences — is what actually
 * explains it. This module is only the two halves of that decision, in one
 * place, so a page cannot answer it two different ways.
 */

export type Reading<T> = {
  /** Whether the reading was taken. `false` means "nobody asked", not "zero". */
  ok: boolean;
  /** The value, present only when `ok`. */
  value?: T;
  /** Why it is missing, in the page's own words. */
  because: string;
  /** The field this came from, for a key in a list. */
  key?: string;
};

/** The em dash a missing figure is drawn as. One character, one meaning. */
export const NOT_READ = "—";

/** A reading that was taken. */
export function taken<T>(value: T, key?: string): Reading<T> {
  return { ok: true, value, because: "", key };
}

/** A reading that was not, and the sentence for it. */
export function unread<T>(because: string, key?: string): Reading<T> {
  return { ok: false, because, key };
}

/**
 * One boolean flag from a payload's `read` block, as a `Reading<T>`.
 *
 * The server sends `read: { voice: false, tracker: true, ... }` and nothing
 * else — the *sentence* for a flag is a page decision, because the same payload
 * feeds any future surface and only the page knows what it drew. That is what
 * `because` is for, and why this takes it as an argument: a missing reading
 * without a reason is the bug this type exists to prevent.
 */
export function fromFlag<T>(
  flags: Record<string, boolean> | undefined,
  key: string,
  value: T,
  because: string,
): Reading<T> {
  return flags?.[key] ? taken(value, key) : unread(because, key);
}

/**
 * What a `<Figure>` should print: the formatted value, or the em dash.
 *
 * `format` is applied only to a value that exists, so a formatter that cannot
 * cope with `undefined` (most of them) is never handed one.
 */
export function figureText<T>(reading: Reading<T>, format: (value: T) => string): string {
  return reading.ok && reading.value !== undefined ? format(reading.value) : NOT_READ;
}
