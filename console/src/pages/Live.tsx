/** What one server is *doing*, as opposed to what it is standing on.
 *
 * Every other page in this console answers a question about configuration: which
 * switch is on, which command is published, which balance is in the table. None of
 * them can answer the question an operator asks while looking at a server in
 * Discord — *who is in voice, which channels exist, and is anybody talking* — and
 * that answer is a live reading from the gateway's own caches, true for about as
 * long as it takes to read it.
 *
 * Three rules shape the page:
 *
 * * **A reading that was not taken is never a zero.** A cold process knows no
 *   channels, a deployment without the Presence intent cannot count statuses, a
 *   machine with no `bot.db` has no tracker tables. Each one gets its own
 *   sentence at the top of the page rather than a confident `0` inside a card —
 *   the same rule the landing page's figures follow, for the same reason.
 * * **Ids are text.** Every member and channel id crosses as a string
 *   (`ServerRow.id` documents why); nothing here does arithmetic on one.
 * * **Names are not invented.** Without the Server Members intent the gateway
 *   cannot name a voice state's user, and the page prints the id it does have
 *   instead of a nickname it made up.
 */

import { motion } from "motion/react";
import { useMemo, useState } from "react";

import { api } from "../api";
import type { LiveChannel, LivePayload, VoicePerson } from "../types";
import { ago, compact, duration } from "../lib/format";
import { Link } from "../lib/router";
import { useLive } from "../lib/useLive";
import { Card, Empty, Failure, Pill, Skeleton, Stat } from "../ui";

const VOICE_INTERVAL = 15_000;

/** The sentence for each reading the deployment could not take. Written here
 * rather than on the server because it is a *page* decision — the same payload
 * feeds any future surface, and only this one knows what it drew. */
const GAPS: { key: keyof LivePayload["read"]; said: string }[] = [
  {
    key: "channels",
    said: "The gateway has not answered yet, so this page cannot list channels or voice rooms. It fills in as soon as the handshake finishes.",
  },
  {
    key: "presence",
    said: "Presence is off on this deployment (the Presence intent), so nobody can say who is online. The voice roster below is unaffected: it comes from voice states, which need no such intent.",
  },
  {
    key: "tracker",
    said: "There are no tracker tables on this deployment, so message and voice totals are absent rather than zero — the bot counts them once it has been running.",
  },
];

export function Live() {
  const [guildId, setGuildId] = useState<string>("");
  const live = useLive(() => api.live(guildId || undefined), VOICE_INTERVAL, [guildId]);
  const data: LivePayload | null = live.data;
  const [query, setQuery] = useState("");

  const voice = useMemo(
    () => (data?.channels ?? []).filter((channel) => channel.kind === "voice"),
    [data],
  );
  const chatter = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (data?.channels ?? [])
      .filter((channel) => channel.kind !== "voice")
      .filter(
        (channel) =>
          !needle ||
          channel.name.toLowerCase().includes(needle) ||
          channel.category.toLowerCase().includes(needle),
      )
      .sort((a, b) => b.messages_week - a.messages_week || a.name.localeCompare(b.name));
  }, [data, query]);

  if (live.loading && !data) return <Skeleton rows={8} />;
  if (live.error && !data) return <Failure message={live.error} onRetry={live.refresh} />;
  if (!data) return null;

  const { totals, read } = data;
  const gaps = GAPS.filter((gap) => !read[gap.key]);
  const occupied = voice.filter((channel) => channel.people.length > 0);
  const quiet = voice.filter((channel) => channel.people.length === 0);

  return (
    <div className="grid">
      <div className="page__head">
        <div>
          <span className="eyebrow">
            {data.server.name} · {compact(data.server.members)} members
          </span>
          <h1>Live</h1>
        </div>
        <span className="readstamp">
          {live.refreshing ? "reading…" : `read ${ago(data.at)}`}
        </span>
      </div>

      {gaps.length > 0 && (
        <motion.div
          className="gaps"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ type: "spring", stiffness: 200, damping: 26 }}
        >
          <span className="gaps__head">What this page could not read</span>
          <ul>
            {gaps.map((gap) => (
              <li key={gap.key}>{gap.said}</li>
            ))}
          </ul>
        </motion.div>
      )}

      <Card span={6} index={0} title="Which server" hint="Every reading below is about one server, because a voice room is.">
        <div className="toolbar">
          <select
            className="input input--select"
            value={data.server.id}
            onChange={(event) => setGuildId(event.target.value)}
            aria-label="Choose a server"
          >
            {data.servers.map((server) => (
              <option key={server.id} value={server.id}>
                {server.name} · {compact(server.members)}
              </option>
            ))}
          </select>
          <Link className="btn btn--ghost" to={`/servers/${data.server.id}`}>
            Open this server
          </Link>
          <span className="toolbar__count">
            {totals.channels} channels{data.exceeded ? ` · ${data.exceeded} past the listing cap` : ""}
          </span>
        </div>
      </Card>

      <Card span={6} index={1} title="At a glance" hint="Live where the gateway can answer, counted where the tracker can.">
        <div className="stats stats--tight stats--bracket">
          <Stat value={read.voice ? compact(totals.in_voice) : "—"} label="in voice" index={0} />
          <Stat value={compact(totals.channels)} label="channels" index={1} />
          <Stat value={read.tracker ? compact(totals.messages_today) : "—"} label="messages today" index={2} />
          <Stat value={read.tracker ? compact(totals.messages_week) : "—"} label="messages · 7d" index={3} />
          <Stat
            value={read.tracker ? duration(totals.voice_seconds_week) : "—"}
            label="voice · 7d"
            index={4}
          />
        </div>
      </Card>

      <Card
        span={4}
        index={2}
        title="Voice rooms"
        hint="Occupied rooms first, then every room standing empty."
        action={<Pill tone={totals.in_voice ? "accent" : "neutral"}>{totals.in_voice} in voice</Pill>}
      >
        {!read.channels ? (
          <Empty
            title="No channel list yet"
            body="The gateway has not answered, so the rooms cannot be read. This fills in on its own."
          />
        ) : voice.length === 0 ? (
          <Empty title="This server has no voice channels" body="Nothing to sit in — which is worth knowing on its own." />
        ) : (
          <div className="rooms">
            {[...occupied, ...quiet].map((channel, index) => (
              <Room key={channel.id} channel={channel} index={index} />
            ))}
          </div>
        )}
      </Card>

      <div className="stack-col">
        <Card span={2} index={3} title="Who is around" hint="Needs the Presence intent.">
          {data.presence ? (
            <dl className="rows">
              <dt>Online</dt>
              <dd>{data.presence.online}</dd>
              <dt>Idle</dt>
              <dd>{data.presence.idle}</dd>
              <dt>Do not disturb</dt>
              <dd>{data.presence.dnd}</dd>
              <dt>Offline</dt>
              <dd>{data.presence.offline}</dd>
              <dt>Streaming</dt>
              <dd>{data.presence.streaming}</dd>
            </dl>
          ) : (
            <Empty
              title="Presence is off"
              body="Discord only reports who is online when the bot is granted the Presence intent, so this deployment counts nobody rather than everybody as offline."
            />
          )}
        </Card>

        <Card
          span={2}
          index={4}
          title="Where the bot is"
          hint="Its own voice connections, if it holds any."
        >
          {data.bot_voice.length === 0 ? (
            <Empty title="Not in a voice room" body="The bot holds no voice connection anywhere right now." />
          ) : (
            <dl className="rows">
              {data.bot_voice.map((client) => (
                <BotVoice key={`${client.guild_id}-${client.channel}`} client={client} />
              ))}
            </dl>
          )}
          <dl className="rows">
            <dt>Gateway ping</dt>
            <dd>{data.gateway.ping_ms === null ? "—" : `${data.gateway.ping_ms} ms`}</dd>
          </dl>
        </Card>
      </div>

      <Card
        span={6}
        index={5}
        title="Where the talking is"
        hint={
          read.tracker
            ? `Text and forum channels, busiest week first. Counted since ${totals.tracker_since}.`
            : "The tracker could not be read, so there is nothing to rank."
        }
        action={
          <input
            className="input"
            placeholder="Filter channels…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Filter channels"
          />
        }
      >
        {!read.tracker ? (
          <Empty
            title="No activity to read"
            body="This deployment has no tracker tables, so every channel here would be a zero nobody measured."
          />
        ) : chatter.length === 0 ? (
          <Empty
            title={data.channels.length ? "Nothing matches that filter" : "No channels to list"}
            body={
              data.channels.length
                ? "Clear the filter to see every channel again."
                : "The gateway has not handed this process a channel list yet."
            }
          />
        ) : (
          <div className="matrix__scroll">
            <table className="matrix">
              <thead>
                <tr>
                  <th scope="col">Channel</th>
                  <th scope="col">Category</th>
                  <th scope="col">Today</th>
                  <th scope="col">7 days</th>
                  <th scope="col">Voice · 7 days</th>
                </tr>
              </thead>
              <tbody>
                {chatter.map((channel, index) => (
                  <motion.tr
                    key={channel.id}
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ type: "spring", stiffness: 220, damping: 28, delay: Math.min(index * 0.015, 0.3) }}
                  >
                    <td>
                      <span className="chan">
                        <span className="chan__sigil" aria-hidden="true">
                          {channel.kind === "forum" ? "▤" : "#"}
                        </span>
                        {channel.name}
                        {channel.nsfw && <Pill tone="warn">nsfw</Pill>}
                        {channel.slowmode > 0 && (
                          <span className="chan__note">slow {channel.slowmode}s</span>
                        )}
                      </span>
                    </td>
                    <td className="dim">{channel.category || "—"}</td>
                    <td className="num">{channel.messages_today.toLocaleString()}</td>
                    <td className="num">{channel.messages_week.toLocaleString()}</td>
                    <td className="num">
                      {channel.voice_seconds_week ? duration(channel.voice_seconds_week) : "—"}
                    </td>
                  </motion.tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card span={3} index={6} title="Talking most" hint="Messages this week, with the voice time beside it.">
        {data.talkers.length === 0 ? (
          <Empty
            title="Nobody ranked yet"
            body="Either the week has been quiet, or the members who talked have left the server — a member who has gone is counted in the totals and not shown under a name."
          />
        ) : (
          <ol className="talkers">
            {data.talkers.map((talker, index) => (
              <motion.li
                key={talker.id}
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ type: "spring", stiffness: 240, damping: 26, delay: index * 0.03 }}
              >
                <span className="talkers__rank">{String(index + 1).padStart(2, "0")}</span>
                <span className="talkers__name">
                  {talker.name || `Member ${talker.id}`}
                  {!talker.name && <span className="chan__note">name not cached</span>}
                </span>
                <span className="talkers__value">{compact(talker.messages)}</span>
                <span className="talkers__sub">
                  {talker.voice_seconds ? duration(talker.voice_seconds) : "no voice"}
                </span>
              </motion.li>
            ))}
          </ol>
        )}
      </Card>

      <Card span={3} index={7} title="Reading this page" hint="What each column is, so the numbers cannot be misread.">
        <ul className="notes">
          <li>
            <b>Voice rooms</b> are the gateway's own voice states, so they are live to the
            second — and they are the one reading that is right even with every intent off.
          </li>
          <li>
            <b>Messages</b> come from the tracker's tables, the same ones <code>/activity</code>{" "}
            reads: today is one UTC day, and 7 days is today plus the six before it.
          </li>
          <li>
            <b>Absent is not zero.</b> Where a reading could not be taken the page says so at the
            top and prints an em dash in the card.
          </li>
        </ul>
      </Card>
    </div>
  );
}

function Room({ channel, index }: { channel: LiveChannel; index: number }) {
  return (
    <motion.div
      className={`room${channel.people.length ? " room--live" : ""}`}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 230, damping: 26, delay: Math.min(index * 0.02, 0.3) }}
    >
      <div className="room__head">
        <span className="room__name">
          <span className="room__sigil" aria-hidden="true">
            🔊
          </span>
          {channel.name}
        </span>
        <span className="room__count">
          {channel.people.length}
          {channel.limit ? ` / ${channel.limit}` : ""}
        </span>
      </div>
      {channel.category && <span className="room__category">{channel.category}</span>}
      {channel.people.length > 0 ? (
        <ul className="room__people">
          {channel.people.map((person) => (
            <Person key={person.id} person={person} />
          ))}
        </ul>
      ) : (
        <span className="room__empty">empty</span>
      )}
    </motion.div>
  );
}

function Person({ person }: { person: VoicePerson }) {
  const flags: string[] = [];
  if (person.streaming) flags.push("live");
  if (person.video) flags.push("video");
  if (person.muted) flags.push("muted");
  if (person.deafened) flags.push("deafened");
  if (person.suppressed) flags.push("waiting");
  return (
    <li className="person" title={flags.join(" · ") || "in the room"}>
      <span className="person__dot" aria-hidden="true" />
      {person.name || `Member ${person.id}`}
      {person.bot && <span className="person__bot">bot</span>}
      {flags.map((flag) => (
        <span className="person__flag" key={flag}>
          {flag}
        </span>
      ))}
    </li>
  );
}

function BotVoice({
  client,
}: {
  client: LivePayload["bot_voice"][number];
}) {
  return (
    <>
      <dt>{client.guild}</dt>
      <dd>
        🔊 {client.channel} · {client.playing ? (client.paused ? "paused" : "playing") : client.connected ? "idle" : "gone"}
      </dd>
    </>
  );
}
