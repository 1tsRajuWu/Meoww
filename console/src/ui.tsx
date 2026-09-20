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
