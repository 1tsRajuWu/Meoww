/** The frame every page sits in: a rail, a topbar, and the palette.
 *
 * The navigation is built from `boot.pages` — the server's own `OWNER_PAGES` —
 * rather than from a list typed here. That is the whole reason it is passed
 * through the shell at all: a route added on the server shows up in the rail
 * without a second edit, so the two cannot drift into "linked but not served".
 */

import { motion } from "motion/react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import type { BootData, ServerRow } from "../types";
import { Link, usePath } from "../lib/router";
import {
  THEMES,
  CURSOR_STYLES,
  CURSOR_COLORS,
  applyTheme,
  currentTheme,
  applyCursorStyle,
  currentCursorStyle,
  applyCursorColor,
  currentCursorColor,
  type ThemeId,
  type CursorStyleId,
  type CursorColorId,
} from "../lib/theme";
import { Pill } from "../ui";
import { CommandPalette, type PaletteAction } from "./CommandPalette";

/**
 * The page list, in the order the server sent it, split into its groups.
 *
 * Order is preserved rather than sorted: the server's tuple is the order
 * somebody chose (the Overview first, then the things you look at, then the
 * things you change), and alphabetising it here would quietly overrule that.
 */
function grouped(pages: BootData["pages"]) {
  const out: { label: string; pages: BootData["pages"] }[] = [];
  for (const page of pages) {
    const label = page.group ?? "";
    const last = out[out.length - 1];
    if (last && last.label === label) last.pages.push(page);
    else out.push({ label, pages: [page] });
  }
  return out;
}

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
  const [cursorStyle, setCursorStyle] = useState<CursorStyleId>(() => currentCursorStyle());
  const [cursorColor, setCursorColor] = useState<CursorColorId>(() => currentCursorColor());
  const [customizerOpen, setCustomizerOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement | null>(null);

  const selectTheme = (next: ThemeId) => {
    setTheme(next);
    applyTheme(next);
  };
  const selectCursorStyle = (next: CursorStyleId) => {
    setCursorStyle(next);
    applyCursorStyle(next);
  };
  const selectCursorColor = (next: CursorColorId) => {
    setCursorColor(next);
    applyCursorColor(next);
  };

  useEffect(() => {
    if (!customizerOpen) return;
    const onClick = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        const btn = document.querySelector(".rail__cursor-btn");
        if (btn && btn.contains(e.target as Node)) return;
        setCustomizerOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setCustomizerOpen(false);
    };
    window.addEventListener("click", onClick);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("click", onClick);
      window.removeEventListener("keydown", onKey);
    };
  }, [customizerOpen]);

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

  // Where the reader is, derived from the *same* list the rail is drawn from —
  // so a page the server adds appears here without a second edit, and the two
  // can never name different places. Jakob's law, applied literally: the
  // location is in the same corner on every screen.
  const current = boot.pages.find((page) =>
    page.href === "/owner"
      ? path === "" || path === "/"
      : path.startsWith(page.href.replace("/owner", "")),
  );

  return (
    <div className="shell">
      <aside className={`rail${menuOpen ? " rail--open" : ""}`}>
        <div className="rail__brand">
          <img
            src={theme === "midnight" ? "/static/logo.svg" : `/static/logo-${theme}.svg`}
            alt=""
            width={30}
            height={30}
          />
          <div>
            <strong>{boot.botName}</strong>
            <span>Console</span>
          </div>
        </div>

        <nav className="rail__nav" aria-label="Console pages">
          {/* Grouped, and the groups come from the server (`OWNER_GROUPS`) for
           * the same reason the pages do: this file cannot be the second place
           * that decides what the navigation contains. A page with no group is
           * drawn in a group of its own with no heading, so the cost of
           * forgetting to place one is a slightly odd-looking line rather than
           * a page nobody can reach. */}
          {grouped(boot.pages).map((group) => (
            <div className="rail__group" key={group.label || "ungrouped"}>
              {group.label && <span className="rail__group-label">{group.label}</span>}
              {group.pages.map((page) => {
                const isCurrent = page.href === "/owner"
                  ? path === "" || path === "/"
                  : path.startsWith(page.href.replace("/owner", ""));
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
            </div>
          ))}
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

          <div className="rail__customizer-bar">
            <div className="rail__themes" role="group" aria-label="Theme">
              {THEMES.map((option) => (
                <button
                  key={option.id}
                  type="button"
                  className={`swatch swatch--${option.id}${theme === option.id ? " is-current" : ""}`}
                  aria-pressed={theme === option.id}
                  title={option.label}
                  aria-label={`${option.label} theme`}
                  onClick={() => selectTheme(option.id)}
                />
              ))}
            </div>

            <button
              type="button"
              className={`rail__cursor-btn${customizerOpen ? " is-active" : ""}`}
              aria-label="Cursor and appearance settings"
              title="Appearance & Cursor"
              aria-expanded={customizerOpen}
              onClick={() => setCustomizerOpen((open) => !open)}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="12" cy="12" r="10" />
                <line x1="22" y1="12" x2="18" y2="12" />
                <line x1="6" y1="12" x2="2" y2="12" />
                <line x1="12" y1="6" x2="12" y2="2" />
                <line x1="12" y1="22" x2="12" y2="18" />
              </svg>
              <span>Cursor</span>
            </button>
          </div>

          {customizerOpen && (
            <div
              className="customizer-popover"
              ref={popoverRef}
              role="dialog"
              aria-label="Appearance and cursor settings"
            >
              <div className="customizer-head">
                <span className="customizer-title">Appearance &amp; Cursor</span>
                <button
                  type="button"
                  className="customizer-close"
                  aria-label="Close appearance settings"
                  onClick={() => setCustomizerOpen(false)}
                >
                  ✕
                </button>
              </div>

              <div className="customizer-section">
                <span className="customizer-label">Theme</span>
                <div className="customizer-themes" role="group" aria-label="Theme options">
                  {THEMES.map((opt) => (
                    <button
                      key={opt.id}
                      type="button"
                      className={`customizer-theme-btn${theme === opt.id ? " is-current" : ""}`}
                      aria-pressed={theme === opt.id}
                      aria-label={`${opt.label} theme`}
                      onClick={() => selectTheme(opt.id)}
                    >
                      <span className={`swatch swatch--${opt.id}`} aria-hidden="true" />
                      <span>{opt.label}</span>
                    </button>
                  ))}
                </div>
              </div>

              <div className="customizer-section">
                <span className="customizer-label">Cursor Style</span>
                <div className="customizer-list" role="group" aria-label="Cursor style options">
                  {CURSOR_STYLES.map((opt) => (
                    <button
                      key={opt.id}
                      type="button"
                      className={`customizer-item${cursorStyle === opt.id ? " is-current" : ""}`}
                      aria-pressed={cursorStyle === opt.id}
                      aria-label={`Cursor style: ${opt.label}`}
                      onClick={() => selectCursorStyle(opt.id)}
                    >
                      <div className="customizer-item__body">
                        <span className="customizer-item__name">{opt.label}</span>
                        <span className="customizer-item__desc">{opt.desc}</span>
                      </div>
                      {cursorStyle === opt.id && (
                        <span className="customizer-item__check" aria-hidden="true">✓</span>
                      )}
                    </button>
                  ))}
                </div>
              </div>

              <div className="customizer-section">
                <span className="customizer-label">Cursor Color &amp; Glow</span>
                <div className="customizer-colors" role="group" aria-label="Cursor color options">
                  {CURSOR_COLORS.map((opt) => (
                    <button
                      key={opt.id}
                      type="button"
                      className={`customizer-color-btn${cursorColor === opt.id ? " is-current" : ""}`}
                      aria-pressed={cursorColor === opt.id}
                      aria-label={`Cursor color: ${opt.label}`}
                      title={opt.label}
                      onClick={() => selectCursorColor(opt.id)}
                    >
                      <span
                        className="customizer-color-chip"
                        style={{ background: opt.preview }}
                        aria-hidden="true"
                      />
                      <span className="customizer-color-name">{opt.label}</span>
                      {cursorColor === opt.id && (
                        <span className="customizer-color-check" aria-hidden="true">✓</span>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}
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
          <span className="topbar__crumb">
            {boot.botName}
            <span aria-hidden="true">/</span>
            <b>{current?.label ?? "Not a page"}</b>
          </span>
          <span className="topbar__spacer" />
          <Pill tone={gatewayTone} pulse={boot.gateway.pulse} title="Gateway state">
            {boot.gateway.label}
          </Pill>
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
