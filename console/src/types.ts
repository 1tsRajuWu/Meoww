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
  id: number;
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
  servers: { id: number; name: string; icon: string; members: number }[];
  matrix: Record<string, Record<string, boolean>>;
  unreadable: number[];
  always_on: string[];
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
