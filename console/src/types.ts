/** The shapes `/api/console/*` returns.
 *
 * Typed by hand rather than generated, because the server is Flask and the
 * contract is five endpoints. If one of them grows a field, add it here and the
 * compiler will point at every place that should care about it.
 */

export type Gateway = {
  /** "Live" | "Reconnecting" | "Bot offline" — the label the server chose. */
  label: string;
  /** A css modifier the server also chose, so the two consoles cannot disagree. */
  css: string;
  /** Whether to draw the pulsing dot. Only true when connected. */
  pulse: boolean;
  connected: boolean;
  started: boolean;
  key: "connected" | "reconnecting" | "offline";
};

export type Totals = {
  servers: number;
  members: number;
  on: number;
  slots: number;
  systems: number;
  switchable: number;
  commands: number;
  published: number;
};

export type FigureTile = { key: string; label: string; value: string };
export type FigurePeriod = { id: string; label: string };

/**
 * The three self-readings, as the modules themselves report them.
 *
 * These are described rather than left as `Record<string, unknown>` because the
 * console reads exactly one field out of each: **`status`**. It is the key all
 * three share — `site_guard` answers `{status, detail}`, `config_audit` answers
 * `{status, unset, attention, detail}` and `uptime_status.snapshot()` answers
 * `{status, at, stale, total, up, down, ...}` — and reading a name one of them
 * does not have is how a row prints an em dash on a page that looks fine.
 */
/**
 * One unset setting, as `config_audit.attention_json()` describes it.
 *
 * Presence only: the module never reads a value, secret or not, so nothing here
 * can carry one.
 */
export type NamedSetting = {
  name: string;
  kind: string;
  what?: string;
  cost?: string;
  secret?: boolean;
};

/**
 * The configuration reading.
 *
 * `health_json` answers with a count and no names, because `/health` is public.
 * The two payloads behind the operator's sign-in add `named`, which is what
 * turns the word "attention" into something the reader can act on.
 */
export type ConfigHealth = {
  status?: string;
  detail?: string;
  unset?: number;
  attention?: number;
  named?: NamedSetting[];
};

export type HealthBlock = {
  uptime: {
    status?: string;
    detail?: string;
    total?: number;
    up?: number;
    down?: number;
    paused?: number;
    ratio_30d?: number | null;
    [key: string]: unknown;
  };
  site: { status?: string; detail?: string; [key: string]: unknown };
  config: ConfigHealth;
};

export type Operator = {
  id?: string;
  username?: string;
  global_name?: string;
  avatar?: string;
};

export type Overview = {
  success: true;
  totals: Totals;
  gateway: Gateway;
  bot: { name: string; ready: boolean; ping_ms: number | null };
  uptime_seconds: number;
  selling: boolean;
  figures: { periods: FigurePeriod[]; tiles: Record<string, FigureTile[]> };
  health: HealthBlock;
  operator: Operator;
};

export type ServerRow = {
  /** **A string, not a number.** Discord ids are 18-19 digits and a `number` is
   * exact to about 16, so an id that round-trips through one comes back as a
   * different server — which is what made every link in this app open a
   * 404 and the systems matrix render "?" in all of its cells. The type here is
   * the guard: an id is opaque text, and nothing may do arithmetic on it. */
  id: string;
  name: string;
  icon: string;
  nick: string;
  members: number;
  on: number;
  total: number;
  known: boolean;
  off: string[];
};

export type Fleet = {
  success: true;
  servers: ServerRow[];
  totals: Totals;
};

export type SystemSwitch = {
  id: string;
  name: string;
  icon: string;
  category: string;
  description: string;
  commands: string[];
  enabled: boolean;
  allowed: boolean;
};

export type SystemCard = Omit<SystemSwitch, "enabled" | "allowed">;

export type ServerDetail = {
  success: true;
  server: ServerRow;
  systems: SystemSwitch[];
  always_on: string[];
  publish_at: number | null;
  /** The blocklist row for this server, or `null` when it is welcome here.
   *
   * The page offers *Block* or *Unblock* from this and from nothing else: the
   * two are not alternatives, and a page that guessed which one applied would
   * be offering to block a server that is already blocked. */
  blocked: { reason: string; added_at: number; added_by: string } | null;
};

/** The four things the operator can do to a server as a whole.
 *
 * `leave` and `block` both make the bot leave; they differ in whether an invite
 * can add it back. `resync` changes nothing about the server and is the fix for
 * the one failure only visible from here — a command list Discord is holding an
 * old copy of. */
export type ServerAction = "leave" | "block" | "unblock" | "resync";

export type SystemsMatrix = {
  success: true;
  systems: SystemCard[];
  /** Ids are strings here for the reason spelled out on `ServerRow.id` — and
   * this is the one payload where it matters most, because the matrix is
   * *keyed* by them and a rounded key matches nothing. */
  servers: { id: string; name: string; icon: string; members: number }[];
  matrix: Record<string, Record<string, boolean>>;
  unreadable: string[];
  always_on: string[];
};

/**
 * One member's holding in one server, as the economy page reads it.
 *
 * `user_id` is text for the same reason `ServerRow.id` is: a member id is a
 * snowflake too, and the app must never do arithmetic on one.
 */
export type BalanceRow = {
  user_id: string;
  /** Their display name where the gateway knows it, else `""`. */
  name: string;
  wallet: number;
  bank: number;
  net: number;
};

/** The actions the adjustment form may post. `set` is not a move — it writes
 * exact balances, which is the owner-only `/eco-admin set` in Discord. */
export type EconomyAction = "wallet_add" | "wallet_remove" | "bank_add" | "bank_remove" | "set";

export type EconomyPayload = {
  success: true;
  server: ServerRow;
  servers: { id: string; name: string; icon: string; members: number }[];
  totals: {
    accounts: number;
    wallet: number;
    bank: number;
    /** Wallet + bank. What members hold, which is not the same as what is in
     * the server — see `meow_bank`. */
    circulation: number;
    /** Coins the Meow Fee has banked. Still in the server, no longer in a
     * member's balance, so a total that omitted it would not add up. */
    meow_bank: number;
  };
  top: BalanceRow[];
  show_limit: number;
  fee: { enabled: boolean; rate: string; min: number; max: number };
  actions: EconomyAction[];
  ceiling: number;
};

export type Adjustment = {
  success: true;
  guild_id: string;
  user_id: string;
  action: EconomyAction;
  wallet: number;
  bank: number;
};

/**
 * One root command, as the registry and the live tree together describe it.
 *
 * The registry says what this deployment *does*; the tree says what Discord was
 * actually *told*. `published` is `null` when the tree could not be read at all
 * (a sleeping gateway answers with an empty list, which is not the same claim as
 * "none of these are published").
 */
export type CommandRow = {
  name: string;
  /** The system that gates it, or `""` when it is always offered. */
  system: string;
  system_name: string;
  icon: string;
  /** Whether the feature registry declares it at all. */
  declared: boolean;
  published: boolean | null;
  /** Server names where this command is not in the picker. */
  off_in: string[];
};

export type CommandsPayload = {
  success: true;
  commands: CommandRow[];
  servers: { id: string; name: string; icon: string; members: number }[];
  always_on: string[];
  tree_read: boolean;
  /** Declared by the registry, absent from the tree — a command that cannot be typed. */
  missing: string[];
  /** In the tree, unknown to the registry — a command no switch can take away. */
  unregistered: string[];
  unreadable: string[];
  totals: { commands: number; declared: number; published: number };
};

export type Ops = {
  success: true;
  logs: string[];
  uptime_seconds: number;
  gateway: Gateway;
  ping_ms: number | null;
  health: HealthBlock;
};

export type ChangeEntry = {
  version: string;
  icon: string;
  lead: string;
  body: string;
};

export type Changes = {
  success: true;
  entries: ChangeEntry[];
  total: number;
};

/** One person sitting in a voice channel, with the flags that matter from
 * outside the channel. `id` is text for the usual reason. */
export type VoicePerson = {
  id: string;
  /** Their display name, or `""` when the member cache cannot resolve them —
   * which is the normal state without the Server Members intent. */
  name: string;
  bot: boolean;
  muted: boolean;
  deafened: boolean;
  streaming: boolean;
  video: boolean;
  suppressed: boolean;
};

/** One channel of a server, as the Live page reads it. */
export type LiveChannel = {
  id: string;
  name: string;
  kind: "text" | "voice" | "forum";
  category: string;
  category_id: string;
  position: number;
  topic: string;
  nsfw: boolean;
  /** Seconds; `0` when there is none. */
  slowmode: number;
  /** The voice channel's own cap, or `null` for "no limit". */
  limit: number | null;
  /** Who is in it, for a voice channel; empty otherwise. */
  people: VoicePerson[];
  messages_today: number;
  messages_week: number;
  voice_seconds_week: number;
};

/** Somebody on their own in a voice room, or live in one.
 *
 * `seconds` is how long the companion's own clock has had this person alone, and
 * it is **`null` when that is not known** rather than `0` — the clock starts on a
 * voice event, so a room this process has watched since it booted carries an age
 * and one it inherited does not.
 */
export type CompanyRoom = {
  channel_id: string;
  channel: string;
  category: string;
  person_id: string;
  person: string;
  streaming: boolean;
  video: boolean;
  seconds: number | null;
};

/** A person streaming or on camera, and the room they are doing it in. */
export type CompanyLive = {
  id: string;
  name: string;
  channel_id: string;
  channel: string;
  streaming: boolean;
  video: boolean;
};

/**
 * What the voice companion is doing, as the console reads it.
 *
 * Two facts are deliberately *not* here, because they do not exist: a bot cannot
 * watch a Go Live stream (Discord has no API for it, and the gateway carries no
 * video), and a reading of "alone" is about the room rather than about the person
 * — so `live[].streaming` is the fact that somebody is live, never a picture.
 */
export type CompanyPayload = {
  /** `loaded` is about the process rather than the server: a deployment where
   * the companion never loaded still has live voice rooms (they come from the
   * gateway) and no clock of its own. */
  read: { rooms: boolean; loaded: boolean; settings: boolean };
  settings: { mode: string; greeting: boolean; on: boolean } | null;
  /** Voice rooms whose only occupant is one person, alone right now. */
  rooms: CompanyRoom[];
  /** Everybody streaming or on camera in this server. */
  live: CompanyLive[];
  /** Where the companion itself is sitting, if it is sitting anywhere. */
  sitting: {
    guild_id: string;
    guild: string;
    channel_id: string;
    channel: string;
    person_id: string;
    person: string;
    since: number;
    streaming: boolean;
    video: boolean;
    greeted: boolean;
    reason: string;
    /** True when the companion is connected somewhere *else* — a different
     * server — which this page says rather than drawing as if it were here. */
    elsewhere?: boolean;
  } | null;
  grace_seconds: number;
  cooldown_seconds: number;
  blocked_seconds: number;
  left_us: string[];
  /** The :pastelpink: picture the greeting carries. */
  emoji: string;
  emoji_name: string;
};

/**
 * What one server is *doing*, as `/api/console/live` answers it.
 *
 * `read` is the important half of this payload and it is not decoration. Every
 * number here is a live reading from caches that are legitimately empty — a cold
 * process knows no channels, a deployment without the Presence intent cannot
 * count statuses, a build machine has no tracker tables. Each of those is a
 * different sentence on the page, and painting a zero over any of them would be
 * inventing a measurement.
 */
export type LivePayload = {
  success: true;
  server: ServerRow;
  servers: { id: string; name: string; icon: string; members: number }[];
  gateway: Gateway & { connected: boolean; started: boolean; ping_ms: number | null };
  read: {
    channels: boolean;
    voice: boolean;
    presence: boolean;
    tracker: boolean;
    /** Whether the voice companion is running on this deployment. */
    company: boolean;
  };
  /** The voice companion: who is alone, who is live, where the bot is sitting. */
  company: CompanyPayload;
  presence: {
    online: number;
    idle: number;
    dnd: number;
    offline: number;
    streaming: number;
    total: number;
  } | null;
  channels: LiveChannel[];
  /** Channels past the payload's cap, which are counted rather than listed. */
  exceeded: number;
  talkers: { id: string; name: string; messages: number; voice_seconds: number }[];
  bot_voice: {
    guild_id: string;
    guild: string;
    channel: string;
    connected: boolean;
    playing: boolean;
    paused: boolean;
  }[];
  /**
   * What the music player is doing here, whether this host can play at all, and
   * whether the extractor can still find anything.
   *
   * All three in one block because "nobody has asked for a song", "music cannot
   * play on this host" and "music cannot find the song" used to look identical on
   * this page — and the second and third are the faults an operator can actually
   * fix, in different ways. `caps.playable` is `null` when the probe could not be
   * run at all, which is a third state and is drawn as one.
   *
   * `extractor` is read from the weekly pass rather than asked live (an
   * extraction is a network round trip and this page polls), so it carries the
   * age of the reading and an `unknown` state for a process that has not run one.
   */
  music: {
    read: { player: boolean; caps: boolean };
    extractor: {
      status: "ok" | "failed" | "unreachable" | "unknown";
      detail: string;
      checked_seconds_ago: number | null;
    } | null;
    why: string;
    caps: {
      playable: boolean | null;
      quality?: string;
      ffmpeg?: string | null;
      ffmpeg_source?: string;
      opus?: boolean;
      opus_source?: string;
      live_volume?: boolean;
      dave?: boolean;
      dave_version?: string | null;
      dave_problem?: string | null;
      missing?: string[];
    } | null;
    player: {
      guild_id: number;
      connected: boolean;
      channel: string | null;
      playing: boolean;
      paused: boolean;
      current: {
        title: string;
        provider: string;
        duration: number;
        requester: string;
        identity: string;
      } | null;
      engine: string;
      position: number;
      volume: number;
      filter: string;
      loop: string;
      queue: number;
      queue_preview: string[];
      is_247: boolean;
      error?: string;
    } | null;
  };
  totals: {
    channels: number;
    text: number;
    voice: number;
    forums: number;
    in_voice: number;
    messages_today: number;
    messages_week: number;
    voice_seconds_week: number;
    tracker_since: string;
  };
  at: number;
};

/**
 * One signed-in session, as the ledger lists it.
 *
 * `sid` is **masked** by the server — `abc123…9f4c` — because a session id is a
 * bearer credential: the page's job is to name a row to revoke, and a console
 * that printed the whole value would make its own screen a way in.
 */
export type SessionRow = {
  id: number;
  sid: string;
  scope: string;
  /** Ids as text, for the usual reason — a snowflake is not a number. */
  guild_id: string;
  user_id: string;
  label: string;
  ip: string;
  agent: string;
  started_at: number;
  seen_at: number;
  expires_at: number;
  revoked_at: number | null;
  state: "live" | "expired" | "revoked";
  /** Whether this is the browser asking for the list. */
  current: boolean;
};

/**
 * The ledger, or the reason there is not one.
 *
 * `available: false` is not "nobody is signed in" — it is "nobody can say",
 * which is a different sentence and the only honest one on a host whose
 * database could not be read.
 */
export type SessionLedger = {
  available: boolean;
  because: string;
  idle_ttl_seconds: number;
  rows: SessionRow[];
  live: number;
  revoked: number;
};

/** One guard window, as `utils/ratelimit.py` reports it. */
export type Window = {
  name: string;
  window_seconds: number;
  limit: number;
  /** Keys inside the window right now — normally addresses. */
  tracked: number;
  attempts: number;
  blocked: { key: string; attempts: number; frees_at: number; seconds_left: number }[];
  refused: number;
  last_refused_at: number | null;
};

/** The two switches that decide whether a limit counts people or a proxy. */
export type Doors = {
  passkey_enabled: boolean;
  discord_elevation: boolean;
  visitor_address_signed: boolean;
  ledger_writable: boolean;
};

export type AccessPayload = {
  success: true;
  sessions: SessionLedger;
  limits: Record<string, Window>;
  doors: Doors;
  process: { uptime_seconds: number; started_at: number; now: number };
  read: { sessions: { ok: boolean; because: string } };
};

export type BootData = {
  csrfToken: string;
  operator: Operator;
  botName: string;
  gateway: Gateway;
  /** The rail's pages. `group` is the heading the server placed this page under
   * (`OWNER_GROUPS`) — empty when it placed it in none, which draws it
   * ungrouped rather than hiding it. */
  pages: { id: string; label: string; href: string; current: boolean; group?: string }[];
};
