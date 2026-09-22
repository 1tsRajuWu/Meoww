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
import type { CompanyRoom, LiveChannel, LivePayload, VoicePerson } from "../types";
import { ago, compact, duration } from "../lib/format";
import { fromFlag } from "../lib/reading";
import { Link } from "../lib/router";
import { useLive } from "../lib/useLive";
import { Card, Empty, Failure, Figure, Pill, Skeleton } from "../ui";

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
    key: "voice",
    said: "This server has no voice channels, so there are no rooms to read. The dashes beside the voice figures mean there is nothing to count, not that counting failed.",
  },
  {
    key: "presence",
    said: "Presence is off on this deployment (the Presence intent), so nobody can say who is online — and the counts are absent rather than zero on purpose. Without the intent Discord reports every member as offline, so a card of figures would say \"1 online, 35 offline\" for a server full of people, the one being the bot. To turn it on: Discord Developer Portal → your app → Bot → Privileged Gateway Intents → Presence Intent, then set ENABLE_PRESENCE_INTENT=1 on this service. The voice roster below is unaffected: it comes from voice states, which need no such intent.",
  },
  {
    key: "tracker",
    said: "There are no tracker tables on this deployment, so message and voice totals are absent rather than zero — the bot counts them once it has been running.",
  },
  {
    key: "company",
    said: "The voice companion is not loaded on this deployment, so nobody's own clock is running and it is sitting anywhere only if another feature put it there. The rooms below are still live — they come from the gateway, not from the companion.",
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
  // One sentence per flag, so a dash and its explanation always come from the
  // same entry in `GAPS` rather than from a literal typed beside a number.
  const explain = (key: keyof LivePayload["read"]) =>
    GAPS.find((gap) => gap.key === key)?.said ?? "This reading was not taken.";
  // Every figure goes through the same `Reading`, whether or not it can be
  // missing: a count that can never be absent is one *call* away from one that
  // can, and the two should not need different components to be drawn honestly.
  const figures = {
    channels: fromFlag(read, "channels", totals.channels, explain("channels")),
    inVoice: fromFlag(read, "voice", totals.in_voice, explain("voice")),
    messagesToday: fromFlag(read, "tracker", totals.messages_today, explain("tracker")),
    messagesWeek: fromFlag(read, "tracker", totals.messages_week, explain("tracker")),
    voiceWeek: fromFlag(read, "tracker", totals.voice_seconds_week, explain("tracker")),
  };
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
          <Figure label="in voice" read={figures.inVoice} value={compact(totals.in_voice)} index={0} />
          <Figure label="channels" read={figures.channels} value={compact(totals.channels)} index={1} />
          <Figure
            label="messages today"
            read={figures.messagesToday}
            value={compact(totals.messages_today)}
            index={2}
          />
          <Figure
            label="messages · 7d"
            read={figures.messagesWeek}
            value={compact(totals.messages_week)}
            index={3}
          />
          <Figure
            label="voice · 7d"
            read={figures.voiceWeek}
            value={duration(totals.voice_seconds_week)}
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

      <Companion data={data} />

      <MusicPanel data={data} />

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
              body={'Discord only reports who is online when the bot is granted the Presence intent, and this deployment does not ask for it — so the honest answer is nothing, not "everybody is offline". Enable Presence Intent in the Discord Developer Portal, set ENABLE_PRESENCE_INTENT=1, and these five lines fill in.'}
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

/** The music player, and — the half nobody can otherwise discover — whether
 * this host is able to play at all.
 *
 * The two used to be the same page's blind spot: a deployment with no ffmpeg, no
 * libopus and no `davey` looked exactly like a server where nobody had asked for
 * a song, because both render as an idle player. So the panel leads with the
 * capability reading, which comes from the same probe the boot line,
 * `/music doctor` and `/api/voice-debug` read — and every *missing* reason is
 * printed rather than summarised, because each one is a different fix.
 *
 * The stream path is the third reading and the one that cannot be inferred from
 * the other two: every capability present, every station URL in the catalogue
 * healthy, and this host firewalled off from the outside world. It carries the age
 * of the reading because it comes from the weekly pass rather than from a request
 * this page made — the honest thing to draw for "asked two days ago" is two days
 * ago.
 */
function MusicPanel({ data }: { data: LivePayload }) {
  const music = data.music;
  const caps = music?.caps ?? null;
  const player = music?.player ?? null;
  const stream = music?.stream ?? null;
  const playable = caps?.playable ?? null;
  const asked = stream?.checked_seconds_ago;

  return (
    <Card
      span={6}
      index={11}
      title="Music & radio"
      hint="What is queued here, what is found, and what this host can actually do for voice."
      action={
        <>
          {playable === null ? (
            <Pill tone="warn">not probed</Pill>
          ) : playable ? (
            <Pill tone="good">can play</Pill>
          ) : (
            <Pill tone="warn">cannot play</Pill>
          )}
          {stream && stream.status !== "ok" && stream.status !== "unknown" && (
            <Pill tone={stream.status === "unreachable" ? "warn" : "danger"}>
              {stream.status === "unreachable" ? "no route out" : "cannot reach a station"}
            </Pill>
          )}
        </>
      }
    >
      <dl className="rows">
        <dt>ffmpeg</dt>
        <dd>
          {caps?.ffmpeg ? (
            /* The file, not the road to it: this value is an absolute path
               through the host's own home directory, which printed the operator's
               username in a definition list and wrapped to three lines of
               uppercase. What the line answers is *which build* — the directory
               is one hover away for anyone who needs it. */
            <>
              {caps.ffmpeg_source ?? "found"}{" "}
              <span className="chan__note" title={caps.ffmpeg}>
                {caps.ffmpeg.split(/[\\/]/).pop()}
              </span>
            </>
          ) : (
            "missing — nothing can be decoded"
          )}
        </dd>
        <dt>libopus</dt>
        <dd>
          {caps?.live_volume
            ? "loaded — the volume changes without restarting the track"
            : "not loaded — the volume is baked into the stream and a change re-renders it"}
        </dd>
        <dt>DAVE</dt>
        <dd>
          {caps?.dave
            ? `${caps.dave_version ?? "installed"} — required for any voice connection`
            : "missing — Discord refuses a voice connection without it"}
        </dd>
        <dt>Stations</dt>
        <dd>
          {stream === null ? (
            "not reported by this backend"
          ) : stream.status === "ok" ? (
            <>
              this host can open a stream
              {asked !== null && asked !== undefined ? (
                <span className="chan__note"> opened one {duration(asked)} ago</span>
              ) : null}
            </>
          ) : stream.status === "unknown" ? (
            <>
              none opened since this process started{" "}
              <span className="chan__note">
                the weekly pass opens one, and <code>/music doctor</code> opens one now
              </span>
            </>
          ) : stream.status === "unreachable" ? (
            <>
              could not be opened — {stream.detail}
              <span className="chan__note">
                this is the host's own network rather than one station
              </span>
            </>
          ) : (
            <>a station would not open — {stream.detail}</>
          )}
        </dd>
      </dl>

      {caps?.missing?.length ? (
        <ul className="alone">
          {caps.missing.map((reason) => (
            <li key={reason}>{reason.replace(/\*\*/g, "")}</li>
          ))}
        </ul>
      ) : null}

      <dl className="rows">
        <dt>Player</dt>
        <dd>
          {player
            ? player.connected
              ? `🔊 ${player.channel ?? "a voice channel"} · ${
                  player.playing ? (player.paused ? "paused" : "playing") : "idle"
                }`
              : "connected nowhere right now"
            : "no player yet — nobody has asked for a station since the last restart"}
        </dd>
        {player?.current && (
          <>
            <dt>Playing</dt>
            <dd>
              {player.current.title}
              {player.current.requester ? ` <span className="chan__note">for ${player.current.requester}</span>` : null}
            </dd>
            <dt>Engine</dt>
            <dd>{player.engine || "starting"}</dd>
            <dt>Queue</dt>
            <dd>
              {player.queue} track{player.queue === 1 ? "" : "s"}
              {player.queue_preview?.length ? ` — next: ${player.queue_preview[0]}` : ""}
            </dd>
            <dt>Volume · filter</dt>
            <dd>
              {Math.round(player.volume * 100)}% · {player.filter}
            </dd>
          </>
        )}
      </dl>
      <p className="alone__foot">
        A station that goes quiet is reported rather than skipped: the bot sends a card naming how long
        it played, and the operator log gets one entry per reason — so "one station is down" can be
        told apart from "this host lost its route out", which is the difference the stream row above
        is drawn for.
      </p>
    </Card>
  );
}

/** The voice companion, as somebody running the bot needs to see it.
 *
 * Three readings, in the order the questions get asked: **who is on their own**
 * (the trigger), **who is live** (the thing the bot cannot watch), and **where the
 * bot is sitting** (the action it took, and why). The middle one exists because
 * pretending otherwise would be the one dishonest thing on this page — Discord
 * gives a bot no way into a Go Live stream, so what is shown is the fact
 * (`self_stream` on somebody's voice state) and never a picture of the stream.
 */
function Companion({ data }: { data: LivePayload }) {
  const company = data.company;
  const rooms = company?.rooms ?? [];
  const live = company?.live ?? [];
  const sitting = company?.sitting ?? null;
  const settings = company?.settings ?? null;

  return (
    <>
    <Card
      span={4}
      index={8}
      title="Alone in voice"
      hint="One person in a room with nobody else, read from the gateway's own voice states."
      action={
        rooms.length ? (
          <Pill tone="accent">{rooms.length} alone</Pill>
        ) : (
          <Pill tone="neutral">nobody</Pill>
        )
      }
    >
      {rooms.length === 0 ? (
        <Empty
          title="Nobody is on their own"
          body="No voice room in this server holds exactly one person. The list is live to the second, so this is a reading rather than a stored state."
        />
      ) : (
        <ul className="alone">
          {rooms.map((room, index) => (
            <AloneRow key={room.channel_id} room={room} index={index} />
          ))}
        </ul>
      )}
      <p className="alone__foot">
        {settings ? (
          <>
            Companion is <b>{settings.mode}</b>
            {settings.greeting ? ", and it says hello when it arrives" : ", and it arrives silently"}.
            {" "}It waits {company.grace_seconds}s before it moves, and stays out of a room for{" "}
            {Math.round(company.cooldown_seconds / 60)} minutes after somebody walks away from it.
          </>
        ) : (
          "This deployment could not read the companion's settings, so this is the reading it runs with by default: it joins, and it says hello."
        )}
      </p>
    </Card>

    <div className="stack-col">
      <Card
        span={2}
        index={9}
        title="Live right now"
        hint="Streaming or on camera, in a voice room somewhere in this server."
        action={<Pill tone={live.length ? "good" : "neutral"}>{live.length}</Pill>}
      >
        {live.length === 0 ? (
          <Empty
            title="Nobody is streaming"
            body="Not one voice state in this server has self_stream set. The companion shows this rather than a stream, because a bot is not allowed to watch a Go Live broadcast — Discord has no API for it."
          />
        ) : (
          <ul className="live-list">
            {live.map((person) => (
              <li key={`${person.channel_id}-${person.id}`}>
                <span className="live-list__dot" aria-hidden="true" />
                <span className="live-list__name">
                  {person.name || `Member ${person.id}`}
                </span>
                <span className="live-list__where">
                  {person.streaming ? "streaming" : "on camera"} in {person.channel}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="alone__foot">
          <b>Why there is no player here.</b> Discord exposes a bot's voice connection but not a
          Go Live stream: there is no endpoint for it and the gateway never carries the video, so
          the honest thing to show is who is live rather than a picture that would be invented.
        </p>
      </Card>

      <Card
        span={2}
        index={10}
        title="Where the bot is sitting"
        hint="Its companion connection, and which person it went to sit with."
      >
        {!sitting ? (
          <Empty
            title="Not sitting with anybody"
            body="Either nobody is alone, or the bot is somewhere else entirely. It holds one voice connection at a time, and never takes the socket from another feature."
          />
        ) : (
          <dl className="rows">
            <dt>Sitting with</dt>
            <dd>{sitting.person || `Member ${sitting.person_id}`}</dd>
            <dt>In</dt>
            <dd>
              🔊 {sitting.channel}
              {sitting.elsewhere && <span className="chan__note">on another server</span>}
            </dd>
            <dt>There for</dt>
            <dd>{duration(Math.max(0, data.at - sitting.since))}</dd>
            <dt>Because</dt>
            <dd>{sitting.reason}</dd>
            <dt>Said hello</dt>
            <dd>{sitting.greeted ? "yes" : "no — the server asked it to arrive quietly"}</dd>
            <dt>They are</dt>
            <dd>
              {sitting.streaming ? "streaming (shown above, not watched)" : sitting.video ? "on camera" : "not live"}
            </dd>
          </dl>
        )}
        {company && company.blocked_seconds > 0 && (
          <p className="alone__foot">
            It could not connect to a room and is standing down for{" "}
            {duration(company.blocked_seconds)} — a failed handshake retried every cycle is how a bot
            gets rate-limited at the voice gateway.
          </p>
        )}
      </Card>
    </div>
    </>
  );
}

function AloneRow({ room, index }: { room: CompanyRoom; index: number }) {
  const waiting = room.seconds;
  return (
    <motion.li
      className="alone__row"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 230, damping: 26, delay: Math.min(index * 0.03, 0.3) }}
    >
      <span className="alone__who">{room.person || `Member ${room.person_id}`}</span>
      <span className="alone__where">🔊 {room.channel}</span>
      <span className="alone__meta">
        {room.streaming && <span className="person__flag">live</span>}
        {room.video && <span className="person__flag">video</span>}
        {/* A clock this process did not start says nothing rather than zero. */}
        <span className="chan__note">
          {waiting === null ? "alone for a while" : `alone ${duration(waiting)}`}
        </span>
      </span>
    </motion.li>
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
