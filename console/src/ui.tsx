/** The console's building blocks. Small and deliberate: one card, one switch,
 * one toast, one skeleton — so six pages look like one product.
 *
 * Motion is `motion/react`. Every animation here is a *spring* rather than a
 * duration wherever the thing being animated is following the operator's hand
 * (hover, press, a value changing); timings are reserved for entrances.
 */

import { AnimatePresence, motion } from "motion/react";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import type { ConfigHealth } from "./types";
import { NOT_READ, type Reading } from "./lib/reading";

/* ------------------------------------------------------------------ layout */

export function Card({
  title,
  hint,
  action,
  span,
  children,
  index = 0,
  tone,
}: {
  title?: ReactNode;
  hint?: ReactNode;
  action?: ReactNode;
  span?: 2 | 3 | 4 | 6;
  children: ReactNode;
  /** Position in a staggered reveal. */
  index?: number;
  tone?: "danger" | "warn" | "good";
}) {
  return (
    <motion.section
      className={`card${span ? ` card--span-${span}` : ""}${tone ? ` card--${tone}` : ""}`}
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 210, damping: 26, delay: index * 0.045 }}
    >
      {(title || action) && (
        <header className="card__head">
          <div>
            {title && <h2 className="card__title">{title}</h2>}
            {hint && <p className="card__hint">{hint}</p>}
          </div>
          {action && <div className="card__action">{action}</div>}
        </header>
      )}
      {children}
    </motion.section>
  );
}

export function Stat({
  value,
  label,
  sub,
  index = 0,
}: {
  value: ReactNode;
  label: string;
  sub?: ReactNode;
  index?: number;
}) {
  return (
    <motion.div
      className="stat"
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 220, damping: 26, delay: index * 0.05 }}
    >
      <span className="stat__value">{value}</span>
      <span className="stat__label">{label}</span>
      {sub && <span className="stat__sub">{sub}</span>}
    </motion.div>
  );
}

/**
 * A figure that knows whether it was read.
 *
 * `Stat` prints whatever it is handed, which is right for a count the server
 * always has. A **reading** — who is in voice, how many messages today, how many
 * sessions can be revoked — can legitimately be missing, and the one thing it
 * must never become is a zero: "nobody is in voice" and "the gateway has not
 * answered" are different sentences, and only one of them is true.
 *
 * So a missing figure prints an em dash, carries its reason as the tooltip, and
 * is dimmed rather than hidden — a page that removed the figure would leave the
 * reader wondering what used to be there. The *sentences* live in each page's
 * own gap notice, because only the page knows what it drew.
 */
export function Figure({
  label,
  read,
  value,
  sub,
  index = 0,
}: {
  label: string;
  read: Reading<unknown>;
  value: ReactNode;
  sub?: ReactNode;
  index?: number;
}) {
  return (
    <motion.div
      className={`stat${read.ok ? "" : " stat--unread"}`}
      data-read={read.ok ? "ok" : "missing"}
      title={read.ok ? undefined : read.because}
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 220, damping: 26, delay: index * 0.05 }}
    >
      <span className="stat__value">{read.ok ? value : NOT_READ}</span>
      <span className="stat__label">{label}</span>
      {sub && <span className="stat__sub">{sub}</span>}
    </motion.div>
  );
}

/**
 * The band that says "this part of the page could not be read".
 *
 * Deliberately not an error: nothing is broken, a reading was not taken — the
 * gateway has not answered yet, the intent is off, the database could not be
 * opened. A page that painted this as a failure would send its reader looking
 * for a fault that is not there, and one that painted it as zeros would be
 * inventing numbers. Same shape as the Live page's gap notice, lifted here so
 * the two cannot drift apart.
 */
export function Unread({ title, said }: { title: string; said: ReactNode }) {
  return (
    <motion.div
      className="gaps"
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 200, damping: 26 }}
    >
      <span className="gaps__head">{title}</span>
      <ul>
        <li>{said}</li>
      </ul>
    </motion.div>
  );
}

export function Pill({
  children,
  tone = "neutral",
  pulse = false,
  title,
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "warn" | "danger" | "accent";
  pulse?: boolean;
  title?: string;
}) {
  return (
    <span className={`pill pill--${tone}`} title={title}>
      {pulse && <span className="pill__dot" aria-hidden="true" />}
      {children}
    </span>
  );
}

export function Bars({ values, label }: { values: { name: string; value: number }[]; label?: string }) {
  const top = Math.max(1, ...values.map((v) => v.value));
  return (
    <div className="bars" role="img" aria-label={label ?? "command counts"}>
      {values.map((item, i) => (
        <motion.div
          className="bars__row"
          key={item.name}
          initial={{ opacity: 0, x: -8 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ type: "spring", stiffness: 220, damping: 26, delay: i * 0.03 }}
        >
          <span className="bars__name">{item.name}</span>
          <span className="bars__track">
            <motion.span
              className="bars__fill"
              initial={{ scaleX: 0 }}
              animate={{ scaleX: Math.max(0.04, item.value / top) }}
              transition={{ type: "spring", stiffness: 160, damping: 24, delay: 0.1 + i * 0.03 }}
            />
          </span>
          <span className="bars__value">{item.value}</span>
        </motion.div>
      ))}
    </div>
  );
}

export function Switch({
  checked,
  disabled,
  busy,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  busy?: boolean;
  label: string;
  onChange: (next: boolean) => void;
}) {
  return (
    <motion.button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled || busy}
      className={`switch${checked ? " switch--on" : ""}${busy ? " switch--busy" : ""}`}
      onClick={() => onChange(!checked)}
      whileTap={{ scale: 0.94 }}
      transition={{ type: "spring", stiffness: 420, damping: 22 }}
    >
      <motion.span
        className="switch__knob"
        layout
        transition={{ type: "spring", stiffness: 460, damping: 30 }}
      />
    </motion.button>
  );
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="skeleton" aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <motion.div
          className="skeleton__row"
          key={i}
          animate={{ opacity: [0.35, 0.75, 0.35] }}
          transition={{ duration: 1.4, repeat: Infinity, delay: i * 0.12 }}
        />
      ))}
    </div>
  );
}

export function Empty({
  title,
  body,
  action,
}: {
  title: string;
  body: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty__mark" aria-hidden="true">
        ◌
      </span>
      <h3>{title}</h3>
      <p>{body}</p>
      {action}
    </div>
  );
}

export function Failure({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="failure" role="alert">
      <strong>That reading failed.</strong>
      <p>{message}</p>
      {onRetry && (
        <button type="button" className="btn btn--ghost" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

/** The names behind the word "attention", one collapsed line each.
 *
 * `config_audit` counts and deliberately does not name, because `/health` is
 * public; the two console payloads carry `named` because they sit behind the
 * operator's sign-in. This is the piece that spends it: a card reporting
 * "attention" and stopping there sends its reader off to look somewhere else,
 * and the settings that are actually costing them features go unnamed in the
 * place they were already reading.
 *
 * Each row opens to the *cost* rather than the description, because the cost is
 * the symptom an operator would otherwise be left to guess at. Five names are
 * scannable; five sentences of consequence are a wall, so they stay shut until
 * asked for. Renders nothing at all when there is nothing to name.
 */
export function ConfigAttention({ config }: { config?: ConfigHealth }) {
  const named = config?.named ?? [];
  if (named.length === 0) return null;

  return (
    <div className="config-missing">
      <span className="config-missing__head">
        {named.length} setting{named.length === 1 ? "" : "s"} this deployment was never given — a
        feature that is off without saying so, which is why the word above is not "ok".
      </span>
      {named.map((item) => (
        <details className="config-missing__item" key={item.name}>
          <summary>
            <code>{item.name}</code>
            <span className="config-missing__kind">{item.kind}</span>
          </summary>
          <p>{item.cost || item.what}</p>
        </details>
      ))}
      <span className="config-missing__foot">
        Names only — no value is read, logged or sent to this page.
      </span>
    </div>
  );
}

/* ------------------------------------------------------------------- toasts */

type Toast = { id: number; text: string; tone: "good" | "warn" | "danger" };

const Toasts = createContext<(text: string, tone?: Toast["tone"]) => void>(() => {});

export function useToast() {
  return useContext(Toasts);
}

export function ToastHost({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);

  const push = useCallback((text: string, tone: Toast["tone"] = "good") => {
    const id = Date.now() + Math.random();
    setItems((current) => [...current, { id, text, tone }]);
    window.setTimeout(() => {
      setItems((current) => current.filter((item) => item.id !== id));
    }, tone === "danger" ? 8000 : 4000);
  }, []);

  const value = useMemo(() => push, [push]);

  return (
    <Toasts.Provider value={value}>
      {children}
      <div className="toasts" aria-live="polite" aria-atomic="false">
        <AnimatePresence>
          {items.map((item) => (
            <motion.div
              key={item.id}
              className={`toast toast--${item.tone}`}
              layout
              initial={{ opacity: 0, y: 18, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 8, scale: 0.98 }}
              transition={{ type: "spring", stiffness: 320, damping: 28 }}
              onClick={() => setItems((current) => current.filter((one) => one.id !== item.id))}
            >
              {item.text}
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </Toasts.Provider>
  );
}

export function RadialGauge({
  value,
  max,
  label,
  sublabel,
  color,
}: {
  value: number;
  max: number;
  label: string;
  sublabel?: string;
  color?: string;
}) {
  const pct = Math.min(100, Math.max(0, max > 0 ? (value / max) * 100 : 0));
  const r = 26;
  const circ = 2 * Math.PI * r;
  const offset = circ - (pct / 100) * circ;
  const strokeColor = color ?? "var(--accent)";

  return (
    <div className="gauge">
      <div className="gauge__visual">
        <svg className="gauge__svg" width="68" height="68" viewBox="0 0 68 68">
          <circle cx="34" cy="34" r={r} className="gauge__bg" strokeWidth="5" />
          <motion.circle
            cx="34"
            cy="34"
            r={r}
            className="gauge__val"
            strokeWidth="5"
            stroke={strokeColor}
            strokeDasharray={circ}
            initial={{ strokeDashoffset: circ }}
            animate={{ strokeDashoffset: offset }}
            transition={{ duration: 1.1, ease: [0.16, 1, 0.3, 1] }}
          />
        </svg>
        <span className="gauge__pct">{Math.round(pct)}%</span>
      </div>
      <div className="gauge__meta">
        <span className="gauge__label">{label}</span>
        {sublabel && <span className="gauge__sub">{sublabel}</span>}
      </div>
    </div>
  );
}

export function LivePulseVisual({ pingMs }: { pingMs: number | null }) {
  return (
    <div className="live-pulse">
      <div className="live-pulse__head">
        <span className="live-pulse__label">Telemetry Link</span>
        <span className="live-pulse__val">{pingMs !== null ? `${pingMs} ms` : "Standby"}</span>
      </div>
      <div className="live-pulse__track">
        <svg className="live-pulse__svg" viewBox="0 0 300 36" preserveAspectRatio="none">
          <line x1="0" y1="18" x2="300" y2="18" className="live-pulse__grid" />
          <motion.path
            d="M0,18 L60,18 L75,6 L90,30 L105,12 L120,24 L135,18 L300,18"
            fill="none"
            className="live-pulse__line"
            stroke="var(--accent)"
            strokeWidth="2"
            initial={{ pathLength: 0, opacity: 0.3 }}
            animate={{ pathLength: 1, opacity: [0.5, 1, 0.5] }}
            transition={{ duration: 2, repeat: Infinity, ease: "linear" }}
          />
        </svg>
      </div>
    </div>
  );
}
