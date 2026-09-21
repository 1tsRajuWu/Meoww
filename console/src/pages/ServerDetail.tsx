/** One server: its systems, and the switches that actually change them.
 *
 * The switch is the whole point of this console, so its behaviour is spelled out:
 *
 * * **Optimistic, then truthful.** The knob moves immediately — a switch that
 *   waits on a round trip feels broken on a host that has been asleep — and if the
 *   server refuses, the knob springs *back* and the server's own sentence is
 *   shown. A toggle that silently reverts is the defect this project has been
 *   reported for twice.
 * * **The publish is named as a wait, not a success.** Saving a switch is not the
 *   same claim as Discord having the new command list; the API returns the stamp
 *   of the publish that was already in flight before this change, and the panel
 *   says "republishing" until a later read shows a newer one.
 */

import { motion } from "motion/react";
import { useEffect, useState } from "react";

import { api, ApiError } from "../api";
import type { ServerAction, ServerDetail as Detail, ServerRow, SystemSwitch } from "../types";
import { ago, compact, iconSrc } from "../lib/format";
import { Link } from "../lib/router";
import { useLive } from "../lib/useLive";
import { Card, Empty, Failure, Pill, Skeleton, Switch } from "../ui";

export function ServerDetail({
  guildId,
  servers,
  onGone,
}: {
  /** The id as text, straight from the address. Parsing it to a number here is
   * how a page opened for one server asked about a different one. */
  guildId: string;
  servers: ServerRow[];
  onGone: (text: string, tone?: "good" | "warn" | "danger") => void;
}) {
  const live = useLive(() => api.server(guildId), null, [guildId]);
  const [systems, setSystems] = useState<SystemSwitch[] | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [publishStamp, setPublishStamp] = useState<number | null>(null);
  /** Which whole-server action is in flight, so every button can stop at once.
   *
   * A name rather than a boolean because the button that is busy says so — and a
   * single flag shared by four buttons that only differ in what they do is how the
   * wrong one ends up looking busy. */
  const [busyAction, setBusyAction] = useState<ServerAction | null>(null);

  // The server's answer is the source of truth; local state exists only so a
  // switch can move before the response lands.
  useEffect(() => {
    if (live.data) {
      setSystems(live.data.systems);
      setPublishStamp(live.data.publish_at);
    }
  }, [live.data]);

  const listRow = servers.find((server) => server.id === guildId);

  const apply = async (target: SystemSwitch, next: boolean) => {
    setBusy((current) => ({ ...current, [target.id]: true }));
    setSystems((current) =>
      current ? current.map((s) => (s.id === target.id ? { ...s, enabled: next } : s)) : current,
    );
    try {
      const answer = await api.toggle(guildId, target.id, next);
      if (answer.republish) {
        onGone(`${target.name} ${next ? "on" : "off"} in ${live.data?.server.name ?? "this server"}.`, "good");
      } else {
        // The switch saved; the *picker* did not change. Saying "on" here would
        // be the wrong claim twice over — the module is on in this console and
        // its commands are still whatever Discord was last told.
        onGone(
          `${target.name} saved, but the command list was not republished — the bot may be restarting. Discord still holds the old list; try the switch again in a moment.`,
          "warn",
        );
      }
      // Read what Discord now holds, so the publish line is a measurement rather
      // than an assumption.
      try {
        const scope = await api.commandScope(guildId);
        setPublishStamp(typeof scope.publish?.at === "number" ? scope.publish.at : publishStamp);
      } catch {
        /* the switch itself succeeded; the publish read is a bonus */
      }
      live.refresh();
    } catch (exc) {
      const message = exc instanceof ApiError ? exc.message : String(exc);
      setSystems((current) =>
        current ? current.map((s) => (s.id === target.id ? { ...s, enabled: !next } : s)) : current,
      );
      onGone(message, "danger");
    } finally {
      setBusy((current) => {
        const nextBusy = { ...current };
        delete nextBusy[target.id];
        return nextBusy;
      });
    }
  };

  /** One whole-server action, confirmed first and reported in the server's words. */
  const act = async (action: ServerAction, confirmText: string) => {
    if (!window.confirm(confirmText)) return;
    setBusyAction(action);
    try {
      const answer = await api.serverAction({ guildId, action });
      onGone(answer.said ?? "Done.", action === "resync" ? "good" : "warn");
      // A leave or a block changes what this page is *about*, so the read is
      // repeated instead of guessed at: a failed leave shows as a server still
      // there, which is the true answer.
      live.refresh();
    } catch (exc) {
      onGone(exc instanceof ApiError ? exc.message : String(exc), "danger");
    } finally {
      setBusyAction(null);
    }
  };

  const bulk = async (enabled: boolean) => {
    if (!systems) return;
    const before = systems;
    setSystems(systems.map((s) => ({ ...s, enabled: enabled && s.allowed })));
    try {
      const answer = await api.toggleAll(guildId, enabled);
      onGone(
        answer.republish
          ? `Every available system ${enabled ? "on" : "off"}. Discord is republishing this server's command list.`
          : `Every available system ${enabled ? "on" : "off"}, but the command list was not republished — the bot may be restarting, so Discord still holds the old list.`,
        answer.republish ? "good" : "warn",
      );
      live.refresh();
    } catch (exc) {
      setSystems(before);
      onGone(exc instanceof ApiError ? exc.message : String(exc), "danger");
    }
  };

  if (live.loading && !live.data) return <Skeleton rows={6} />;
  if (live.error && !live.data) return <Failure message={live.error} onRetry={live.refresh} />;

  const data: Detail | null = live.data;
  if (!data) return null;

  const onCount = systems?.filter((s) => s.enabled).length ?? 0;

  return (
    <div className="grid">
      <div className="page__head">
        <div className="page__title">
          {iconSrc(data.server.icon) ? (
            <img
              className="page__avatar"
              src={iconSrc(data.server.icon)}
              alt=""
              width={44}
              height={44}
            />
          ) : null}
          <div>
            <span className="eyebrow">
              <Link to="/servers">Fleet</Link> · {compact(data.server.members)} members
            </span>
            <h1>{data.server.name}</h1>
          </div>
        </div>
        <span className="readstamp">
          {live.refreshing ? "reading…" : `read ${ago((live.readAt ?? 0) / 1000)}`}
        </span>
      </div>

      {listRow && listRow.off.length > 0 && (
        <Card span={6} index={0} tone="warn" title="Off in this server">
          <p className="card__body">{listRow.off.join(", ")}</p>
        </Card>
      )}

      <Card
        span={6}
        index={1}
        title={`Systems · ${onCount} of ${systems?.length ?? 0} on`}
        hint={
          publishStamp
            ? `Discord's copy was last queued ${ago(publishStamp)}. A switch republishes it — and Discord's own client caches the list, so a command that is newly on here can take a minute (or a reload of Discord) to appear in the picker.`
            : "A switch republishes this server's command list; Discord's own client may hold the old one for a minute after that."
        }
        action={
          <div className="segmented">
            <button type="button" className="segmented__item" onClick={() => void bulk(true)}>
              All on
            </button>
            <button type="button" className="segmented__item" onClick={() => void bulk(false)}>
              All off
            </button>
          </div>
        }
      >
        {!systems?.length ? (
          <Empty title="No switchable systems" body="This deployment exposes none." />
        ) : (
          <ul className="systems">
            {systems.map((system, i) => (
              <motion.li
                key={system.id}
                className={`systems__item${system.enabled ? " is-on" : ""}`}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ type: "spring", stiffness: 240, damping: 28, delay: i * 0.02 }}
              >
                <span className="systems__icon" aria-hidden="true">
                  {system.icon}
                </span>
                <span className="systems__text">
                  <strong>{system.name}</strong>
                  <small>{system.description}</small>
                  <span className="systems__meta">
                    {system.commands.length} command{system.commands.length === 1 ? "" : "s"} ·{" "}
                    {system.category}
                    {!system.allowed && " · needs a plan this server does not have"}
                  </span>
                </span>
                <Switch
                  checked={system.enabled}
                  disabled={!system.allowed}
                  busy={Boolean(busy[system.id])}
                  label={`${system.name} in ${data.server.name}`}
                  onChange={(next) => void apply(system, next)}
                />
              </motion.li>
            ))}
          </ul>
        )}
      </Card>

      <Card span={3} index={2} title="Always on" hint="Not switchable, so not in the list above.">
        <ul className="plain">
          {data.always_on.map((name) => (
            <li key={name}>
              <Pill tone="accent">{name}</Pill>
            </li>
          ))}
        </ul>
      </Card>

      <Card span={3} index={3} title="Act on this server">
        <div className="stack">
          <a className="btn" href={`/dashboard?guild_id=${guildId}`}>
            Open the per-server dashboard
          </a>
          <p className="card__hint">
            The dashboard owns this server's identity, composer and schedule. This console owns
            the switches above, which are the same stored state.
          </p>
        </div>
      </Card>

      {/* The four things only the operator can do, kept off the Systems card
       * above because they are not the same kind of decision: every switch there
       * is reversible from the same switch, and everything here is not. Each one
       * asks first, and the confirmation names the consequence rather than the
       * action — "the bot leaves and an invite can add it again" is the sentence
       * that tells a person which of the two leave buttons they want. */}
      <Card
        span={3}
        index={4}
        tone="danger"
        title="Only you can do this"
        hint="These change the bot's presence in the server, not its settings — which is why a server's own administrator never sees them."
      >
        <div className="stack">
          <button
            type="button"
            className="btn btn--ghost"
            disabled={busyAction !== null}
            onClick={() => void act("resync", "Ask Discord to republish this server's command list?")}
          >
            Republish its commands
          </button>
          <p className="card__hint">
            The fix for a picker holding commands that no longer exist — the bot asked Discord,
            and Discord's own client may cache it for a minute afterwards.
          </p>

          <button
            type="button"
            className="btn btn--ghost"
            disabled={busyAction !== null}
            onClick={() =>
              void act(
                "leave",
                `Make the bot leave ${data.server.name}? Its settings here are kept, and an invite can add it back.`,
              )
            }
          >
            Leave the server
          </button>

          {data.blocked ? (
            <>
              <p className="card__hint">
                On the blocklist{data.blocked.added_by ? ` by ${data.blocked.added_by}` : ""}
                {data.blocked.reason ? ` — “${data.blocked.reason}”` : ""}. An invite is refused
                while it stays there.
              </p>
              <button
                type="button"
                className="btn"
                disabled={busyAction !== null}
                onClick={() =>
                  void act("unblock", `Welcome ${data.server.name} back? An invite can add the bot again.`)
                }
              >
                Take it off the blocklist
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className="btn btn--danger"
                disabled={busyAction !== null}
                onClick={() =>
                  void act(
                    "block",
                    `Make the bot leave ${data.server.name} AND refuse every invite back?\n\nBlocking is the one action here that a re-invite cannot undo.`,
                  )
                }
              >
                Leave and block it
              </button>
              <p className="card__hint">
                Blocking removes the bot and refuses to come back, which is what a re-invite
                cannot walk around.
              </p>
            </>
          )}
        </div>
      </Card>
    </div>
  );
}
