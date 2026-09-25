/** Polling, in one hook, because every page here reads something that moves.
 *
 * The rules it encodes, all of which a hand-rolled `setInterval` in a page gets
 * wrong at least once:
 *
 * * a poll that errors does **not** wipe the data already on screen — a sleeping
 *   host is the normal state of a free tier, and blanking the page every fifteen
 *   seconds because a request timed out is worse than showing a reading that is
 *   one poll old, clearly stamped;
 * * the timer is cleared on unmount and on every dependency change, so a page
 *   left open stops asking when the operator navigates away;
 * * only the *first* load may show a skeleton. Later loads keep the previous
 *   values and animate, which is what makes it feel alive rather than flickery.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type Live<T> = {
  data: T | null;
  error: string | null;
  /** True only before the first successful (or failed) read. */
  loading: boolean;
  /** True while a refresh is in flight and data already exists. */
  refreshing: boolean;
  /** When the last successful read finished, for the "read 4s ago" stamp. */
  readAt: number | null;
  refresh: () => void;
};

export function useLive<T>(
  load: () => Promise<T>,
  intervalMs: number | null,
  deps: unknown[] = [],
): Live<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [readAt, setReadAt] = useState<number | null>(null);

  // The loader is usually an inline arrow, so it is held in a ref: depending on
  // its identity would restart the interval on every render.
  const loadRef = useRef(load);
  loadRef.current = load;

  const alive = useRef(true);
  const [nonce, setNonce] = useState(0);
  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    alive.current = true;
    let timer: number | undefined;

    const tick = async (first: boolean) => {
      if (!first) setRefreshing(true);
      try {
        const next = await loadRef.current();
        if (!alive.current) return;
        setData(next);
        setError(null);
        setReadAt(Date.now());
      } catch (exc) {
        if (!alive.current) return;
        setError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        if (!alive.current) return;
        setLoading(false);
        setRefreshing(false);
      }
    };

    void tick(true);
    if (intervalMs && intervalMs > 0) {
      timer = window.setInterval(() => void tick(false), intervalMs);
    }

    return () => {
      alive.current = false;
      if (timer !== undefined) window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, nonce, ...deps]);

  return { data, error, loading, refreshing, readAt, refresh };
}
