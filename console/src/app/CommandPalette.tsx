/** ⌘K — one place to go anywhere and do the four things worth doing.
 *
 * It exists because the console's own answer to "where is that server?" was a
 * page you had to already be on. The palette reads the *same* lists the pages do
 * (the fleet comes from the API, the destinations from the shell's `pages`), so it
 * cannot offer something that is not there.
 */

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { BootData, ServerRow } from "../types";
import { navigate } from "../lib/router";

export type PaletteAction = {
  id: string;
  label: string;
  hint?: string;
  run: () => void;
};

export function CommandPalette({
  open,
  onClose,
  boot,
  servers,
  actions,
}: {
  open: boolean;
  onClose: () => void;
  boot: BootData;
  servers: ServerRow[];
  actions: PaletteAction[];
}) {
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const input = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  const results = useMemo(() => {
    const destinations: PaletteAction[] = boot.pages.map((page) => ({
      id: `page:${page.id}`,
      label: page.label,
      hint: "page",
      run: () => navigate(page.href.replace(/^\/owner/, "")),
    }));

    const serverEntries: PaletteAction[] = servers.map((server) => ({
      id: `server:${server.id}`,
      label: server.name,
      hint: `${server.on}/${server.total} on · ${server.members} members`,
      run: () => navigate(`/servers/${server.id}`),
    }));

    // Destinations first, then servers, then the two ways out of the app.
    // Order is the thing that decides what Return does before anything has been
    // typed, and "go somewhere in this console" is the reason the palette is
    // opened; the cross-surface links were put first once and Return reloaded
    // the app instead of navigating.
    const all = [...destinations, ...serverEntries, ...actions];
    const needle = query.trim().toLowerCase();
    if (!needle) return all.slice(0, 12);
    return all
      .filter(
        (item) =>
          item.label.toLowerCase().includes(needle) ||
          (item.hint ?? "").toLowerCase().includes(needle),
      )
      .slice(0, 12);
  }, [actions, boot.pages, query, servers]);

  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
      // Focus after paint: focusing while the panel is still `visibility: hidden`
      // is silently ignored by the browser, which is a real bug this project has
      // already shipped once.
      window.setTimeout(() => input.current?.focus(), 60);
    }
  }, [open]);

  useEffect(() => {
    setCursor((current) => Math.min(current, Math.max(0, results.length - 1)));
  }, [results.length]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setCursor((c) => (results.length ? (c + 1) % results.length : 0));
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setCursor((c) => (results.length ? (c - 1 + results.length) % results.length : 0));
      }
      if (event.key === "Enter") {
        const chosen = results[cursor];
        if (chosen) {
          event.preventDefault();
          chosen.run();
          onClose();
        }
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [cursor, onClose, open, results]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="palette"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.14 }}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) onClose();
          }}
        >
          <motion.div
            className="palette__panel"
            role="dialog"
            aria-modal="true"
            aria-label="Command palette"
            initial={{ opacity: 0, y: -16, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -10, scale: 0.99 }}
            transition={{ type: "spring", stiffness: 260, damping: 26 }}
          >
            <input
              ref={input}
              className="palette__input"
              value={query}
              placeholder="Go to a server, a page, or an action…"
              onChange={(event) => setQuery(event.target.value)}
              aria-label="Search"
            />
            <div className="palette__list" ref={listRef}>
              {results.length === 0 && <p className="palette__none">Nothing matches that.</p>}
              {results.map((item, i) => (
                <button
                  type="button"
                  key={item.id}
                  className={`palette__item${i === cursor ? " is-current" : ""}`}
                  onMouseEnter={() => setCursor(i)}
                  onClick={() => {
                    item.run();
                    onClose();
                  }}
                >
                  <span>{item.label}</span>
                  {item.hint && <span className="palette__hint">{item.hint}</span>}
                </button>
              ))}
            </div>
            <footer className="palette__foot">
              <span>↑↓ to move</span>
              <span>↵ to open</span>
              <span>esc to close</span>
            </footer>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
