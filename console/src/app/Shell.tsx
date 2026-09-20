/** The frame every page sits in: a rail, a topbar, and the palette.
 *
 * The navigation is built from `boot.pages` — the server's own `OWNER_PAGES` —
 * rather than from a list typed here. That is the whole reason it is passed
 * through the shell at all: a route added on the server shows up in the rail
 * without a second edit, so the two cannot drift into "linked but not served".
 */

import { motion } from "motion/react";
import { useEffect, useState, type ReactNode } from "react";

import type { BootData, ServerRow } from "../types";
import { Link, usePath } from "../lib/router";
import { THEMES, applyTheme, currentTheme, type ThemeId } from "../lib/theme";
import { Pill } from "../ui";
import { CommandPalette, type PaletteAction } from "./CommandPalette";

export function Shell({
  boot,
  servers,
  actions,
  children,
}: {
  boot: BootData;
  servers: ServerRow[];
  actions: PaletteAction[];
  children: ReactNode;
}) {
  const path = usePath();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [theme, setTheme] = useState<ThemeId>(() => currentTheme());
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    setMenuOpen(false);
  }, [path]);

  const gatewayTone =
    boot.gateway.key === "connected" ? "good" : boot.gateway.key === "reconnecting" ? "warn" : "danger";

  return (
    <div className="shell">
      <aside className={`rail${menuOpen ? " rail--open" : ""}`}>
        <div className="rail__brand">
          <img src="/static/logo.svg" alt="" width={30} height={30} />
          <div>
            <strong>{boot.botName}</strong>
            <span>Console</span>
          </div>
        </div>

        <nav className="rail__nav" aria-label="Console pages">
          {boot.pages.map((page) => {
            const isCurrent =
              page.href === "/owner" ? path === "" : path.startsWith(page.href.replace("/owner", ""));
            return (
              <Link
                key={page.id}
                to={page.href.replace(/^\/owner/, "")}
                className={`rail__link${isCurrent ? " is-current" : ""}`}
                aria-current={isCurrent ? "page" : undefined}
              >
                {isCurrent && (
                  <motion.span
                    className="rail__marker"
                    layoutId="rail-marker"
                    transition={{ type: "spring", stiffness: 380, damping: 30 }}
                  />
                )}
                {page.label}
              </Link>
            );
          })}
        </nav>

        <div className="rail__foot">
          <button
            type="button"
            className="rail__search"
            onClick={() => setPaletteOpen(true)}
          >
            <span>Search everything</span>
            <kbd>⌘K</kbd>
          </button>

          <div className="rail__themes" role="group" aria-label="Theme">
            {THEMES.map((option) => (
              <button
                key={option.id}
                type="button"
                className={`swatch swatch--${option.id}${theme === option.id ? " is-current" : ""}`}
                aria-pressed={theme === option.id}
                title={option.label}
                aria-label={`${option.label} theme`}
                onClick={() => setTheme(option.id)}
              />
            ))}
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <button
            type="button"
            className="topbar__menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
          >
            Menu
          </button>
          <Pill tone={gatewayTone} pulse={boot.gateway.pulse} title="Gateway state">
            {boot.gateway.label}
          </Pill>
          <span className="topbar__spacer" />
          {boot.operator.username && (
            <span className="topbar__who" title="Signed in as">
              {boot.operator.global_name || boot.operator.username}
            </span>
          )}
        </header>

        {children}
      </div>

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        boot={boot}
        servers={servers}
        actions={actions}
      />
    </div>
  );
}
