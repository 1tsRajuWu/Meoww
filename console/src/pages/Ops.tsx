/** Operations: the process's own output, and what it says about itself.
 *
 * The tail is the only place a failure that never reached Discord can be seen
 * without SSH, so it is here — and it is *read repeatedly* rather than frozen at
 * page load, with the age of the read printed beside it. A stale log that looks
 * live is worse than one that admits its age, which is a defect this project has
 * already fixed once on another page.
 */

import { motion } from "motion/react";
import { useMemo, useState, type ReactNode } from "react";

import { api } from "../api";
import { ago, duration, scalarish, uptimeLine } from "../lib/format";
import { useLive } from "../lib/useLive";
import { Card, ConfigAttention, Empty, Failure, Pill, Skeleton } from "../ui";

const LEVELS = [
  { id: "all", label: "Everything", match: null },
  { id: "warn", label: "Warnings+", match: /⚠|warn|error|fail|traceback/i },
  { id: "error", label: "Errors", match: /error|fail|traceback|exception/i },
] as const;

export function OpsPage() {
  const live = useLive(() => api.ops(), 5_000);
  const [level, setLevel] = useState<(typeof LEVELS)[number]["id"]>("all");
  const [paused, setPaused] = useState(false);

  const lines = useMemo(() => {
    const all = live.data?.logs ?? [];
    const found = LEVELS.find((entry) => entry.id === level);
    const filtered = found?.match ? all.filter((line) => found.match?.test(line)) : all;
    // Newest last is how a log reads; the tail we were sent is already in order.
    return filtered.slice(-160);
  }, [level, live.data]);

  if (live.loading && !live.data) return <Skeleton rows={8} />;
  if (live.error && !live.data) return <Failure message={live.error} onRetry={live.refresh} />;

  const data = live.data;
  if (!data) return null;

  const gatewayTone =
    data.gateway.key === "connected" ? "good" : data.gateway.key === "reconnecting" ? "warn" : "danger";

  return (
    <div className="grid">
      <div className="page__head">
        <div>
          <span className="eyebrow">This process, right now</span>
          <h1>Operations</h1>
        </div>
        <span className="readstamp">
          {paused ? "paused" : live.refreshing ? "reading…" : `read ${ago((live.readAt ?? 0) / 1000)}`}
        </span>
      </div>

      <Card
        span={4}
        index={0}
        title="Process"
        action={<Pill tone={gatewayTone} pulse={data.gateway.pulse}>{data.gateway.label}</Pill>}
      >
        <dl className="rows">
          <Row label="Up" value={duration(data.uptime_seconds)} />
          <Row label="Latency" value={data.ping_ms === null ? "—" : `${data.ping_ms} ms`} />
          {/* `status` is the one key all three modules report; the detail is the
              sentence explaining it, kept as the tooltip rather than dropped. */}
          <Row
            label="Public site"
            value={<span title={scalarish(data.health.site.detail)}>{scalarish(data.health.site.status)}</span>}
          />
          <Row
            label="Configuration"
            value={
              <span title={scalarish(data.health.config.detail)}>
                {scalarish(data.health.config.status)}
              </span>
            }
          />
          <Row
            label="Uptime monitors"
            value={
              <span title={scalarish(data.health.uptime.detail)}>
                {uptimeLine(data.health.uptime)}
              </span>
            }
          />
        </dl>
        {/* "attention" with no list is a dead end: this is the list. */}
        <ConfigAttention config={data.health.config} />
      </Card>

      <Card
        span={2}
        index={1}
        title="Output"
        hint="The last 200 lines this process wrote."
        action={
          <div className="segmented">
            {LEVELS.map((entry) => (
              <button
                key={entry.id}
                type="button"
                className={`segmented__item${level === entry.id ? " is-current" : ""}`}
                aria-pressed={level === entry.id}
                onClick={() => setLevel(entry.id)}
              >
                {entry.label}
              </button>
            ))}
          </div>
        }
      >
        <div className="toolbar">
          <button type="button" className="btn btn--ghost" onClick={() => setPaused((p) => !p)}>
            {paused ? "Resume watching" : "Pause"}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => void navigator.clipboard?.writeText(lines.join("\n"))}
          >
            Copy {lines.length} lines
          </button>
        </div>
        {lines.length === 0 ? (
          <Empty
            title="Nothing at this level"
            body="Either the process has been quiet, or nothing has gone wrong. Try Everything."
          />
        ) : (
          <pre className="log" aria-live="off">
            {lines.map((line, i) => (
              <motion.span
                key={`${i}-${line.slice(0, 24)}`}
                className={`log__line${/error|fail|traceback|exception/i.test(line) ? " is-bad" : /⚠|warn/i.test(line) ? " is-warn" : ""}`}
                initial={{ opacity: 0, x: -6 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ type: "spring", stiffness: 300, damping: 30, delay: Math.min(i * 0.004, 0.3) }}
              >
                {line}
              </motion.span>
            ))}
          </pre>
        )}
      </Card>
    </div>
  );
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  );
}
