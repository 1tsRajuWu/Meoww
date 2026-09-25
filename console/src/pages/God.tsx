/** God level: the four actions that are not about one server.
 *
 * Every other page in this console is scoped to a thing — a server, a wallet, a
 * command list — and a mistake there is a mistake about one server. This one is
 * the exception, and the page is built around that difference rather than in
 * spite of it:
 *
 * * **The lock is drawn, not hidden.** A closed tier still renders, with the
 *   passkey field and a sentence saying what opening it would make available.
 *   A page that vanished while locked would look like a missing feature instead
 *   of a closed door, and the operator would reach for the Discord command that
 *   does the same thing with less of a record.
 * * **The clock is visible.** The unlock is a quarter of an hour, and the page
 *   says how much of it is left — an armed console that does not say so is one
 *   somebody leaves open on a second monitor.
 * * **The reasons are collected before the buttons.** The server refuses a
 *   fleet-wide write without a reason, deliberately: the reason is what the next
 *   person to read the trail has. Asking for it here means the refusal is never
 *   the thing that teaches the rule.
 * * **It says when an action would do nothing.** Pausing a paused bot is not a
 *   dangerous mistake, it is a pointless one, and a button that changes nothing
 *   should look like it.
 */

import { useState } from "react";

import { ApiError, api } from "../api";
import type { GodState } from "../types";
import { ago } from "../lib/format";
import { useLive } from "../lib/useLive";
import { Card, Failure, Pill, Skeleton, Stat } from "../ui";

type Tone = "good" | "warn" | "danger";

export function God({
  onGone,
}: {
  onGone: (text: string, tone?: Tone) => void;
}) {
  const live = useLive(() => api.god(), 15_000, []);
  const data: GodState | null = live.data;

  const [passkey, setPasskey] = useState("");
  const [busy, setBusy] = useState("");
  const [reason, setReason] = useState("");

  async function run(work: () => Promise<{ said?: string | null }>, label: string, tone: Tone) {
    setBusy(label);
    try {
      const result = await work();
      onGone(result.said || "Done.", tone);
      setReason("");
      live.refresh();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : String(error);
      onGone(message, "danger");
      // A refusal that was *about the lock* is a state change worth redrawing:
      // the tier may have expired between the last poll and the click.
      live.refresh();
    } finally {
      setBusy("");
    }
  }

  if (live.loading && !data) return <Skeleton rows={6} />;
  if (live.error && !data) return <Failure message={live.error} onRetry={live.refresh} />;
  if (!data) return null;

  const minutesLeft = Math.floor(data.remaining_seconds / 60);
  const secondsLeft = data.remaining_seconds % 60;

  return (
    <div className="grid">
      <div className="page__head">
        <div>
          <span className="eyebrow">
            {data.totals.servers} server{data.totals.servers === 1 ? "" : "s"} ·{" "}
            {data.totals.members.toLocaleString()} members
          </span>
          <h1>God level</h1>
        </div>
        <span className="readstamp">
          {data.locked ? "locked" : `${minutesLeft}m ${String(secondsLeft).padStart(2, "0")}s left`}
        </span>
      </div>

      {data.locked ? (
        <Card
          span={6}
          index={0}
          title="Locked"
          hint="These four actions change every server at once, so they stay closed until the owner passkey is typed again — being signed in is a different claim."
        >
          <div className="toolbar">
            <input
              className="input"
              type="password"
              value={passkey}
              autoComplete="current-password"
              placeholder="Owner passkey"
              aria-label="Owner passkey"
              onChange={(event) => setPasskey(event.target.value)}
              onKeyDown={(event) => {
                if (event.key !== "Enter") return;
                void run(
                  () => api.godUnlock(passkey),
                  "unlock",
                  "good",
                );
                setPasskey("");
              }}
            />
            <button
              type="button"
              className="btn btn--primary"
              disabled={!passkey || busy === "unlock"}
              onClick={() => {
                void run(() => api.godUnlock(passkey), "unlock", "good");
                setPasskey("");
              }}
            >
              {busy === "unlock" ? "Checking…" : "Unlock"}
            </button>
          </div>
          <p className="card__hint">
            Wrong passkeys are counted against the same limit as the front door, so a
            guess here costs an attempt there — and every refusal is written to the
            audit trail below.
          </p>
        </Card>
      ) : (
        <Card
          span={6}
          index={0}
          title="Open"
          hint={`Closes by itself in ${data.unlock_seconds / 60} minutes, or press Lock now — a tier that stays armed is the state this exists to avoid.`}
          tone="warn"
        >
          <div className="toolbar">
            <button
              type="button"
              className="btn btn--ghost"
              disabled={busy === "lock"}
              onClick={() => void run(() => api.godLock().then((r) => ({ said: "God level closed." , ...r})), "lock", "good")}
            >
              Lock now
            </button>
            <span className="toolbar__count">Unlocked for this browser session.</span>
          </div>
        </Card>
      )}

      <Card
        span={6}
        index={1}
        title="The bot, everywhere"
        hint="Read from the database, not from memory: a pause survives the restart it is usually covering."
      >
        <div className="stats stats--tight">
          <Stat
            label="State"
            value={data.paused.paused ? "Paused" : "Answering"}
            sub={data.paused.paused ? "commands refused" : "commands answered"}
          />
          <Stat label="By" value={data.paused.by || "—"} />
          <Stat
            label="Since"
            value={data.paused.since ? ago(Date.now() / 1000 - data.paused.since) : "—"}
          />
        </div>
        <p className="card__hint">{data.paused_sentence}</p>
        {!data.paused.read && (
          <p className="card__hint" data-tone="warn">
            The flag could not be read just now, which is treated as <strong>running</strong> —
            a database error must not be able to silence every server.
          </p>
        )}
      </Card>

      <Card
        span={6}
        index={2}
        title="What these would do"
        hint="Every one of them writes to the audit trail with the reason below."
      >
        {/* The console's own convention for a field's name: `aria-label` on the
            control itself, because a placeholder vanishes the moment somebody
            types and the page has no form to wrap it in. */}
        <div className="toolbar">
          <input
            id="god-reason"
            className="input"
            value={reason}
            aria-label="Reason for this change"
            placeholder="What is happening that needs this?"
            onChange={(event) => setReason(event.target.value)}
          />
        </div>
        <div className="god-actions">
          {data.actions.map((action) => {
            const disabled = Boolean(busy) || data.locked || (action.reason_required && !reason);
            // Chosen here rather than inline in the `className`: the two that
            // take something away are the ones drawn as dangers, and a ternary
            // inside the attribute puts the *ids* it compares into the class
            // scan as if they were style names.
            const look = action.id === "pause" || action.id === "fleet_off" ? "btn--danger" : "btn--primary";
            return (
              <div className="god-action" key={action.id}>
                <div className="god-action__text">
                  <strong>
                    {action.label}
                    {action.is_set ? <Pill tone="neutral">already true</Pill> : null}
                  </strong>
                  <span>{action.blurb}</span>
                </div>
                <button
                  type="button"
                  className={`btn ${look}`}
                  disabled={disabled}
                  title={
                    data.locked
                      ? "Unlock god level first."
                      : disabled && action.reason_required && !reason
                        ? "Give a reason first."
                        : undefined
                  }
                  onClick={() => void run(() => api.godAct(action.id, reason), action.id, "warn")}
                >
                  {busy === action.id ? "Working…" : action.label.split(",")[0]}
                </button>
              </div>
            );
          })}
        </div>
      </Card>

      <Card
        span={6}
        index={3}
        title="The trail"
        hint="The last few god-level actions, read back from the website's own audit table — it outlives this process, which is the point of it."
      >
        {data.recent.length === 0 ? (
          <p className="card__hint">Nothing at this level has been done yet.</p>
        ) : (
          <ul className="god-trail">
            {data.recent.map((row, index) => (
              <li key={`${row.at}-${index}`}>
                <span className={`dot ${row.ok ? "dot--good" : "dot--danger"}`} aria-hidden="true" />
                <span className="god-trail__action">{row.action.replace(/^god_/, "")}</span>
                <span className="god-trail__detail">{row.detail}</span>
                <span className="god-trail__who">
                  {row.actor_name || "operator"} · {ago(Date.now() / 1000 - row.at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        span={6}
        index={4}
        title="What the fleet switches would touch"
        hint="One write per server per system. A server whose admin switched a system off gets it back on with the fleet-on action, which is why that one asks for a reason too."
      >
        <div className="god-fleet">
          {data.fleet.map((server) => (
            <span className="pill" key={server.id}>
              {server.name} · {server.on}/{server.total}
            </span>
          ))}
        </div>
      </Card>
    </div>
  );
}
