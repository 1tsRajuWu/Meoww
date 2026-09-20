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
};

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

export type BootData = {
  csrfToken: string;
  operator: Operator;
  botName: string;
  gateway: Gateway;
  pages: { id: string; label: string; href: string; current: boolean }[];
};
