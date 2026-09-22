/** Access: who is signed in, and what the guards are doing.
 *
 * Two readings that answer one question from two sides, which is why they are
 * one page:
 *
 * * **the ledger** — which sessions exist, where each came from, since when, and
 *   the button that ends one. Before this, the only way to end a session was to
 *   rotate `FLASK_SECRET_KEY` and restart the host, which signs out the browser
 *   you are doing it from.
 * * **the windows** — how many attempts are inside each limit and whether
 *   anybody is currently being refused. "Too many failed attempts" arrives with
 *   no subject; this is the subject, with an address beside it, so the operator
 *   can tell their own mistyped passkey from a stranger at the door.
 *
 * Every reading on this page can be *missing*, and each missing one says so
 * rather than printing a zero — a host whose database cannot be read does not
 * have "nobody signed in", it has "nobody can say", and that difference is the
 * whole reason the payload carries a `read` block.
 */

import { useMemo, useState } from "react";
import { motion } from "motion/react";

import { api } from "../api";
import { ago, compact, duration } from "../lib/format";
import { fromFlag, type Reading } from "../lib/reading";
import { useLive } from "../lib/useLive";
import { Card, Empty, Failure, Figure, Pill, Skeleton, Unread, useToast } from "../ui";
import type { AccessPayload, SessionRow, Window } from "../types";

const INTERVAL = 15_000;

/** How each scope reads to a person. The server's own label is used when the
 * page has not been taught the scope yet — a console one release behind must not
 * print `undefined` about the thing it is showing. */
const SCOPE_HINT: Record<string, string> = {
  global: "The passkey. Reaches every server the bot is in.",
  user: "A Discord sign-in. Reach is the servers that person administers.",
  guild: "A one-time link. Pinned to the one server it was minted for.",
};

const WINDOW_HINT: Record<string, string> = {
  "sign-in": "Failed passkey attempts on a sign-in form.",
  passkey: "Wrong passkeys against the API, from any client.",
  send: "Messages the bot was asked to post, per client.",
};

/** The order the doors are met in: the form, then the API, then the composer.
 *
 * Stated rather than taken from the payload, because Flask answers JSON with its
 * keys sorted — so the order a reader sees would otherwise be alphabetical,
 * which here is `passkey, send, sign-in`: an order that means nothing. */
const WINDOW_ORDER = ["sign-in", "passkey", "send"];

/** A window length as a person says it. `duration()` is built for uptimes
 * ("3m 22s", which is right for "how long has this been up") and wrong for
 * "fifteen minutes" — a limit is a round number, not a stopwatch. */
function windowLabel(seconds: number): string {
  if (seconds >= 3600) {
    const hours = Math.round(seconds / 3600);
    return `${hours} hour${hours === 1 ? "" : "s"}`;
  }
  const minutes = Math.max(1, Math.round(seconds / 60));
  return `${minutes} min`;
}

export function Access() {
  const live = useLive(() => api.access(), INTERVAL);
  const toast = useToast();
  const [busy, setBusy] = useState<number | "all" | null>(null);

  const data: AccessPayload | null = live.data;
  const rowCount = data?.sessions.rows.length ?? 0;

  const others = useMemo(
    () => (data?.sessions.rows ?? []).filter((row) => row.state === "live" && !row.current),
    [data],
  );

  if (live.loading && !data) return <Skeleton rows={8} />;
  if (live.error && !data) return <Failure message={live.error} onRetry={live.refresh} />;
  if (!data) return null;

  const ledger = data.sessions;
  const readSessions = fromFlag<AccessPayload["sessions"]>(
    data.read.sessions?.ok ? { sessions: true } : undefined,
    "sessions",
    ledger,
    data.read.sessions?.because || "The ledger could not be read.",
  );

  const endOne = async (row: SessionRow) => {
    setBusy(row.id);
    try {
      const result = await api.revokeSession(row.id);
      toast(result.note || "Ended.", result.revoked ? "good" : "warn");
      live.refresh();
    } catch (exc) {
      toast(exc instanceof Error ? exc.message : String(exc), "danger");
    } finally {
      setBusy(null);
    }
  };

  const endOthers = async () => {
    setBusy("all");
    try {
      const result = await api.revokeOtherSessions();
      toast(
        result.revoked === 1
          ? "One other session ended."
          : `${result.revoked} other sessions ended.`,
        result.revoked ? "good" : "warn",
      );
      live.refresh();
    } catch (exc) {
      toast(exc instanceof Error ? exc.message : String(exc), "danger");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="grid">
      <div className="page__head">
        <div>
          <span className="eyebrow">Who is in, and what is being refused</span>
          <h1>Access</h1>
        </div>
        <span className="readstamp">
          {live.refreshing ? "reading…" : `read ${ago((live.readAt ?? 0) / 1000)}`}
        </span>
      </div>

      {!ledger.available && <Unread title="The session ledger could not be read" said={ledger.because} />}

      <Card
        span={6}
        index={0}
        title="Signed in"
        hint="One row per sign-in. The id is masked: it is a credential, and this page's job is to name a row."
        action={
          <div className="toolbar">
            <span className="toolbar__count">
              {readSessions.ok ? `${compact(ledger.live)} live` : "— live"}
              {readSessions.ok && ledger.revoked ? ` · ${compact(ledger.revoked)} ended` : ""}
            </span>
            <button
              type="button"
              className="btn btn--ghost"
              disabled={others.length === 0 || busy === "all"}
              onClick={() => void endOthers()}
            >
              {busy === "all" ? "Ending…" : `End every other session${others.length ? ` (${others.length})` : ""}`}
            </button>
          </div>
        }
      >
        {!readSessions.ok ? (
          <Empty
            title="Nobody can say"
            body="The database could not be opened on this host, so this is not a list of nobody — it is the absence of a list. Signing in again may fix it if the file was briefly locked."
          />
        ) : rowCount === 0 ? (
          <Empty
            title="No sessions on record"
            body="Nothing is signed in, or every row has aged out — the ledger keeps a session until it has gone eight hours without being used."
          />
        ) : (
          <div className="sessions">
            {ledger.rows.map((row, index) => (
              <Session key={row.id} row={row} index={index} busy={busy} onEnd={endOne} />
            ))}
          </div>
        )}
        {!data.doors.ledger_writable && (
          <p className="card__foot">
            This host cannot write to the ledger right now, so a sign-in that happens now
            works but cannot be revoked until it can.
          </p>
        )}
      </Card>

      <div className="stack-col">
        <Card
          span={2}
          index={1}
          title="Guard windows"
          hint="Per process: a restart forgets them, and the counts below are this process's own."
        >
          <div className="windows">
            {Object.keys(data.limits)
              .sort(
                (a, b) =>
                  (WINDOW_ORDER.indexOf(a) + 1 || 99) - (WINDOW_ORDER.indexOf(b) + 1 || 99),
              )
              .map((name, index) => {
                const window = data.limits[name];
                // A window the server stopped reporting is not drawn as empty:
                // the page's own record of what it expected would be a guess.
                return window ? (
                  <WindowRow key={name} name={name} window={window} index={index} />
                ) : null;
              })}
          </div>
          <p className="card__foot">
            Uptime {duration(data.process.uptime_seconds)} — the history these counts carry is
            no longer than that.
          </p>
        </Card>

        <Card span={2} index={2} title="Doors" hint="What is switched on, and what each switch means.">
          <dl className="rows">
            <dt>Passkey</dt>
            <dd>
              <Pill tone={data.doors.passkey_enabled ? "good" : "danger"}>
                {data.doors.passkey_enabled ? "set" : "not set"}
              </Pill>
            </dd>
            <dt>Discord sign-in</dt>
            <dd>
              <span title="A Discord sign-in for the owner's account opens the console on its own when this is on.">
                {data.doors.discord_elevation ? "opens the console" : "asks for the passkey"}
              </span>
            </dd>
            <dt>Visitor address</dt>
            <dd>
              <span title="The owner site's worker names the visitor it forwarded, signed with the shared secret. Without it every visitor on that host shares one window.">
                {data.doors.visitor_address_signed ? "signed" : "not signed"}
              </span>
            </dd>
          </dl>
          {!data.doors.visitor_address_signed && (
            <p className="card__foot">
              Without the signature, sign-in limits on the owner host count every visitor as
              one client — one person's five wrong passkeys close the door for everybody.
              Set <code>OWNER_PROXY_SECRET</code> in both places.
            </p>
          )}
        </Card>
      </div>
    </div>
  );
}

function Session({
  row,
  index,
  busy,
  onEnd,
}: {
  row: SessionRow;
  index: number;
  busy: number | "all" | null;
  onEnd: (row: SessionRow) => Promise<void>;
}) {
  const live = row.state === "live";
  const tone = row.state === "live" ? "accent" : row.state === "expired" ? "warn" : "neutral";
  return (
    <motion.div
      className={`session${row.current ? " session--current" : ""}`}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 240, damping: 28, delay: index * 0.03 }}
    >
      <div className="session__main">
        <span
          className="session__scope"
          title={SCOPE_HINT[row.scope] ?? "A scope this build does not know."}
        >
          {row.label || row.scope || "unknown"}
          {/* The marker is only added when the label does not already say it:
              a harness that labels its own row "this browser" was drawn as
              "this browser this browser", which is how a page reads when two
              parts of it write the same sentence. */}
          {row.current && !/this browser/i.test(row.label) && (
            <span className="session__you"> · this browser</span>
          )}
        </span>
        <span className="session__meta">
          {row.scope || "unknown scope"} · {row.ip || "address not recorded"} ·{" "}
          {row.agent || "unknown client"} · in since {ago(row.started_at)} · last used{" "}
          {ago(row.seen_at)}
        </span>
        {row.guild_id && <span className="session__meta">server {row.guild_id}</span>}
      </div>
      <div className="session__side">
        <Pill tone={tone}>{row.state}</Pill>
        {row.current ? (
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => void api.signOut()}
          >
            Sign out
          </button>
        ) : (
          <button
            type="button"
            className="btn btn--ghost"
            disabled={!live || busy === row.id}
            title={live ? "End this session now." : "It has already ended."}
            onClick={() => void onEnd(row)}
          >
            {busy === row.id ? "Ending…" : "End"}
          </button>
        )}
      </div>
    </motion.div>
  );
}

function WindowRow({ name, window, index }: { name: string; window: Window; index: number }) {
  const blocked = window.blocked.length > 0;
  return (
    <motion.div
      className="window"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 240, damping: 28, delay: index * 0.04 }}
    >
      <div className="window__head">
        <span className="window__name">{name}</span>
        <Pill tone={blocked ? "danger" : "neutral"} pulse={blocked}>
          {blocked ? `${window.blocked.length} blocked` : "clear"}
        </Pill>
      </div>
      <span className="window__hint">
        {WINDOW_HINT[name] ?? "A sliding window over attempts."}
      </span>
      <div className="stats stats--tight">
        <Figure
          label="limit"
          read={hardReading(window.limit)}
          value={compact(window.limit)}
          sub={`per ${windowLabel(window.window_seconds)}`}
          index={0}
        />
        <Figure
          label="tracked"
          read={hardReading(window.tracked)}
          value={compact(window.tracked)}
          sub="addresses"
          index={1}
        />
        <Figure
          label="refused"
          read={hardReading(window.refused)}
          value={compact(window.refused)}
          sub="this process"
          index={2}
        />
      </div>
      {blocked && (
        <ul className="window__blocked">
          {window.blocked.map((row) => (
            <li key={row.key}>
              <code>{row.key}</code> · {row.attempts} attempts · frees in{" "}
              {duration(Math.max(0, row.seconds_left))}
            </li>
          ))}
        </ul>
      )}
    </motion.div>
  );
}

/** A number the server always sends, as a `Reading` — so a figure the page can
 * count on and a figure it cannot are drawn by the same component, and a page
 * that later loses one has only to change this call. */
function hardReading<T>(value: T): Reading<T> {
  return { ok: true, value, because: "" };
}
