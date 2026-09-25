/** The front page: what is running, and whether anything needs a hand.
 *
 * Every number here is a reading, stamped with when it was read. Nothing is
 * prose — the operator opening this page has one question ("is anything wrong?")
 * and the page's job is to answer it in a glance, then offer the way in.
 */

import { motion } from "motion/react";
import { useState, type ReactNode } from "react";

import { api } from "../api";
import type { BootData, FigureTile, ServerRow } from "../types";
import { ago, compact, duration, scalarish, uptimeLine } from "../lib/format";
import { Link } from "../lib/router";
import { useLive } from "../lib/useLive";
import { Card, ConfigAttention, Empty, Failure, LivePulseVisual, Pill, RadialGauge, Skeleton, Stat } from "../ui";

export function Overview({ boot, servers }: { boot: BootData; servers: ServerRow[] }) {
  const live = useLive(() => api.overview(), 15_000);
  const [period, setPeriod] = useState("week");

  if (live.loading && !live.data) {
    return (
      <div className="grid">
        <Card span={6} title="Overview">
          <Skeleton rows={5} />
        </Card>
      </div>
    );
  }
  if (live.error && !live.data) {
    return <Failure message={live.error} onRetry={live.refresh} />;
  }

  const data = live.data;
  if (!data) return null;

  const tiles: FigureTile[] = data.figures.tiles[period] ?? [];
  const worst = [...servers].sort((a, b) => a.on / (a.total || 1) - b.on / (b.total || 1))[0];
  const gatewayTone =
    data.gateway.key === "connected" ? "good" : data.gateway.key === "reconnecting" ? "warn" : "danger";

  return (
    <div className="grid">
      <div className="page__head">
        <div>
          <span className="eyebrow">Every server, one screen</span>
          <h1>Overview</h1>
        </div>
        <span className="readstamp">
          {live.refreshing ? "reading…" : `read ${ago((live.readAt ?? 0) / 1000)}`}
        </span>
      </div>

      <Card span={4} index={0}>
        <div className="stats">
          <Stat value={compact(data.totals.servers)} label="servers" index={0} />
          <Stat value={compact(data.totals.members)} label="people reached" index={1} />
          <Stat
            value={`${data.totals.on}/${data.totals.slots}`}
            /* These are switches summed over every server, not systems — the
               registry's own count is on the Gateway card beside it, and one
               screen calling two different numbers "systems" is how an operator
               ends up asking which one is broken. */
            label="switches on"
            sub={`across ${data.totals.servers} server${data.totals.servers === 1 ? "" : "s"}`}
            index={2}
          />
          <Stat value={compact(data.totals.commands)} label="commands" index={3} />
        </div>
        <div className="gauges">
          <RadialGauge
            value={data.totals.on}
            max={data.totals.slots || 1}
            label="Module switches"
            sublabel={`${data.totals.on} of ${data.totals.slots} on`}
          />
          {/* `--good`, not a token of its own: this gauge is the one arc that
              means "reaching everything", and `--good` is the colour the pill
              beside it is drawn with, so the two cannot disagree about what
              green means. It read `--pill-good-text` before, a name this
              stylesheet has never had — and an undefined `var()` in a `stroke`
              resolves to `none`, so this arc was not drawn at all: the browser
              was looked at and the gauge was an empty ring with "40%" in the
              middle of it, beside a Module switches gauge drawing normally. */}
          <RadialGauge
            value={data.totals.servers}
            max={Math.max(data.totals.servers, 5)}
            label="Fleet Reach"
            sublabel={`${data.totals.servers} connected`}
            color="var(--good)"
          />
        </div>
      </Card>

      <Card
        span={2}
        index={1}
        title="Gateway"
        action={<Pill tone={gatewayTone} pulse={data.gateway.pulse}>{data.gateway.label}</Pill>}
      >
        <LivePulseVisual pingMs={data.bot.ping_ms} />
        <dl className="rows">
          <Row label="Process up" value={duration(data.uptime_seconds)} />
          <Row
            label="Latency"
            value={data.bot.ping_ms === null ? "—" : `${data.bot.ping_ms} ms`}
          />
          <Row label="Published now" value={`${data.totals.published} commands`} />
          <Row label="Registry" value={`${data.totals.systems} systems`} />
        </dl>
      </Card>

      <Card
        span={3}
        index={2}
        title="What it has done"
        hint="Live telemetry counter"
        action={
          <div className="segmented">
            {data.figures.periods.map((option) => (
              <button
                key={option.id}
                type="button"
                className={`segmented__item${period === option.id ? " is-current" : ""}`}
                aria-pressed={period === option.id}
                onClick={() => setPeriod(option.id)}
              >
                {option.label}
              </button>
            ))}
          </div>
        }
      >
        {tiles.length ? (
          <div className="stats stats--tight">
            {tiles.map((tile, i) => (
              <Stat key={tile.key} value={tile.value} label={tile.label} index={i} />
            ))}
          </div>
        ) : (
          <Empty
            title="No activity read"
            body="The bot's own tracker has not reported yet — it fills in once the gateway is up."
          />
        )}
      </Card>

      <Card span={3} index={3} title="Needs a look" hint="System diagnostics">
        <dl className="rows">
          <Row
            label="Public site"
            value={
              <HealthWord
                value={scalarish(data.health.site.status)}
                title={scalarish(data.health.site.detail)}
              />
            }
          />
          <Row
            label="Configuration"
            value={
              <HealthWord
                value={scalarish(data.health.config.status)}
                title={scalarish(data.health.config.detail)}
              />
            }
          />
          <Row
            label="Uptime monitors"
            value={
              <HealthWord
                value={uptimeLine(data.health.uptime)}
                title={scalarish(data.health.uptime.detail)}
              />
            }
          />
          <Row
            label="Quietest server"
            value={
              worst ? (
                <Link to={`/servers/${worst.id}`}>
                  {worst.name} · {worst.on}/{worst.total}
                </Link>
              ) : (
                "none"
              )
            }
          />
        </dl>
        {/* The config row above is one word; this is what the word is about. */}
        <ConfigAttention config={data.health.config} />
      </Card>

      <motion.div className="quick" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.2 }}>
        <Link className="btn btn--primary" to="/servers">
          Open the fleet
        </Link>
        <Link className="btn" to="/systems">
          Systems matrix
        </Link>
        <Link className="btn" to="/ops">
          Output tail
        </Link>
        {!data.selling && (
          <span className="quick__note">Nothing is for sale on this deployment.</span>
        )}
      </motion.div>

      <p className="fineprint">
        Signed in as {boot.operator.global_name || boot.operator.username || "the operator"} ·
        this console is the whole fleet and is served only to an operator sign-in.
      </p>
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

function HealthWord({ value, title }: { value: string; title?: string }) {
  // "good" is a short list on purpose: the three modules answer with words like
  // `clean`, `ok` and `attention`, and everything that is not one of those is a
  // state worth looking at rather than a failure to hide.
  const tone = /^(clean|ok|healthy|yes|true|up)$/i.test(value)
    ? "good"
    : /attention|missing|unset|unknown|unconfigured|not |fail|error|down|degraded/i.test(value)
      ? "warn"
      : "neutral";
  return (
    <Pill tone={tone} title={title}>
      {value}
    </Pill>
  );
}
