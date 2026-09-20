/** Formatting, kept in one place so two pages cannot spell one number two ways. */

/** `90` -> `90`, `12345` -> `12.3k`. The console is read at a glance, not audited. */
export function compact(value: number): string {
  if (!Number.isFinite(value)) return "—";
  if (Math.abs(value) < 1000) return String(value);
  if (Math.abs(value) < 1_000_000) {
    const k = value / 1000;
    return `${k >= 100 ? Math.round(k) : k.toFixed(1).replace(/\.0$/, "")}k`;
  }
  const m = value / 1_000_000;
  return `${m.toFixed(1).replace(/\.0$/, "")}M`;
}

/** A duration as the two largest units that matter: `3d 4h`, `12m 30s`. */
export function duration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.floor(seconds);
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

/** How long ago a timestamp was, in the words a person would use. */
export function ago(timestampSeconds: number | null | undefined): string {
  if (!timestampSeconds) return "never";
  const delta = Date.now() / 1000 - timestampSeconds;
  if (delta < 5) return "just now";
  if (delta < 60) return `${Math.floor(delta)}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

/** The clock time a log line was read at — the operator's own timezone. */
export function clock(date: Date): string {
  return date.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** The uptime row: a count when there is one, the honest state when there is not.
 *
 * `snapshot()` answers three different shapes depending on what it knows —
 * unconfigured, refused, or a real reading — and it puts the answer in `status`
 * either way. The counts only exist in the last case, so this reads them when
 * they are there and falls back to the state rather than printing `0/0 up`,
 * which would say "every monitor is down" about a key nobody has set.
 */
export function uptimeLine(block: Record<string, unknown>): string {
  const total = Number(block.total ?? 0);
  if (Number.isFinite(total) && total > 0) {
    const parts = [`${Number(block.up ?? 0)}/${total} up`];
    const down = Number(block.down ?? 0);
    if (down > 0) parts.push(`${down} down`);
    return parts.join(" · ");
  }
  return scalarish(block.status);
}

/** A setting value that may be a string, number, boolean, null or a list. */
export function scalarish(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") return compact(value);
  if (typeof value === "string") return value.length ? value : "—";
  if (Array.isArray(value)) return value.length ? `${value.length}` : "none";
  if (typeof value === "object") return `${Object.keys(value as object).length}`;
  return String(value);
}
