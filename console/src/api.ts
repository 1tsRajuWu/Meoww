/** One place that talks to the server.
 *
 * Two rules are enforced here so no page has to remember them:
 *
 * 1. **Every request carries the CSRF token.** The console's own routes need it
 *    on writes, and the *reused* endpoints (`/api/toggle`, `/api/command-scope`)
 *    validate it on reads too — so sending it always is both simpler and correct.
 * 2. **A failure is a thrown `ApiError` with the server's own sentence.** Flask
 *    answers refusals as `{error: "..."}` in words written for a person
 *    ("Could not save that change. Please try again."), and a UI that replaced
 *    that with "Request failed" would be throwing away the only useful part.
 * 3. **A picture is our own path or it is nothing.** See `drawableImage` — this
 *    is applied here, once, rather than in the pages that happen to draw one.
 */

import type {
  AccessPayload,
  Adjustment,
  BootData,
  Changes,
  CommandsPayload,
  EconomyAction,
  EconomyPayload,
  Fleet,
  LivePayload,
  Ops,
  Overview,
  ServerDetail,
  SystemsMatrix,
} from "./types";
import { ApiError } from "./lib/errors";
import { expectPayload } from "./lib/payload";

export { ApiError };

/** A picture this app is willing to put in an `<img src>`, or `""`.
 *
 * Every icon the console draws is built by the *server*, which returns our own
 * `/api/image?...` — its address rather than Discord's, because `img-src 'self'`
 * is what stops a page whose template was tampered with from dialling out. So
 * today every value is same-origin by construction, and this function changes
 * nothing about what is on screen.
 *
 * It exists because `src` is not decoration: it is an attribute the browser
 * *acts on*, and `javascript:` in it is a script. A CSP does not save a page from
 * the URL its own API handed it, which is exactly how a value that only ever
 * *looked* like a path becomes one. `//host/x` is somebody else's host despite
 * the leading slash, so only a path that is genuinely ours — or an inline image —
 * is let through.
 */
function drawableImage(value: unknown): string {
  const path = typeof value === "string" ? value.trim() : "";
  if (path.startsWith("/") && !path.startsWith("//")) return path;
  if (path.startsWith("data:image/")) return path;
  return "";
}

let csrfToken = "";

/** Called once at boot with the token the shell rendered into the page. */
export function setCsrfToken(token: string): void {
  csrfToken = token;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (csrfToken) headers.set("X-CSRF-Token", csrfToken);

  let response: Response;
  try {
    response = await fetch(path, { credentials: "same-origin", ...init, headers });
  } catch {
    // A network failure is usually the host having gone to sleep, which is a
    // real and recurring state on a free tier — so it is named rather than
    // reported as a generic error.
    throw new ApiError("The host did not answer. It may still be waking up.", 0);
  }

  if (response.status === 401 || response.status === 403) {
    throw new ApiError("Your sign-in is no longer valid. Reload to sign in again.", response.status);
  }

  const text = await response.text();
  let data: unknown;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    throw new ApiError("The server sent something that was not JSON.", response.status);
  }

  if (!response.ok) {
    const message =
      (data as { error?: string }).error ?? `Request failed (${response.status}).`;
    throw new ApiError(message, response.status);
  }

  // The boundary. `data as T` is a promise the compiler cannot keep on its own:
  // the bundle is committed and served from a CDN, so a browser can be running
  // last week's build against this week's server — and a payload that no longer
  // has a key this page needs should arrive as a sentence, not as a crash inside
  // a `.map()`. See `lib/payload.ts` for what this does and does not check.
  return expectPayload<T>(path, data, response.status);
}

export const api = {
  overview: () => request<Overview>("/api/console/overview"),

  /** The fleet, with every server's picture passed through the one rule above. */
  fleet: async () => {
    const data = await request<Fleet>("/api/console/fleet");
    return {
      ...data,
      servers: data.servers.map((server) => ({ ...server, icon: drawableImage(server.icon) })),
    };
  },

  /** `id` is the string the fleet handed us — never a number. See `ServerRow.id`. */
  server: async (id: string) => {
    const data = await request<ServerDetail>(`/api/console/server/${id}`);
    return { ...data, server: { ...data.server, icon: drawableImage(data.server.icon) } };
  },
  systems: () => request<SystemsMatrix>("/api/console/systems"),

  /**
   * What one server is doing right now — channels, voice rooms, chatter.
   *
   * `guildId` is omitted for "the biggest one", exactly as the economy endpoint
   * does it, so the first read of a page the operator has not chosen a server on
   * still answers something true.
   */
  live: (guildId?: string) =>
    request<LivePayload>(
      `/api/console/live${guildId ? `?guild_id=${encodeURIComponent(guildId)}` : ""}`,
    ),
  commands: () => request<CommandsPayload>("/api/console/commands"),

  /** One server's economy. `guildId` is omitted for "the biggest one". */
  economy: (guildId?: string, limit = 25) =>
    request<EconomyPayload>(
      `/api/console/economy?limit=${limit}${guildId ? `&guild_id=${encodeURIComponent(guildId)}` : ""}`,
    ),

  /**
   * Move coins, or be refused in words that name the number that refused them.
   *
   * A POST rather than a query string because it writes: the CSRF token goes on
   * every request this app makes, and this is the one endpoint where that is
   * not a formality — it moves money in the bot's own database.
   */
  adjust: (body: {
    guildId: string;
    userId: string;
    action: EconomyAction;
    amount?: number;
    wallet?: number;
    bank?: number;
  }) =>
    request<Adjustment>("/api/console/economy/adjust", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        guild_id: body.guildId,
        user_id: body.userId,
        action: body.action,
        amount: body.amount,
        wallet: body.wallet,
        bank: body.bank,
      }),
    }),
  ops: () => request<Ops>("/api/console/ops"),
  changes: () => request<Changes>("/api/console/changes"),

  /**
   * Who is signed in, and what the three guard windows are doing.
   *
   * Read on its own interval by the Access page, and refreshed by hand after a
   * revocation — the list is a *claim about right now*, and a revocation that
   * left its own row on screen would be an interface arguing with itself.
   */
  access: () => request<AccessPayload>("/api/console/access"),

  /**
   * End a session by the row id the list showed, or every other one.
   *
   * A POST with the row id rather than a method on the sid: the page never
   * receives a usable session id (the server masks it), and it should not — a
   * screen that printed one would be a way in rather than a way out.
   */
  revokeSession: (id: number) =>
    request<{ success: true; revoked: boolean; note?: string }>(
      "/api/console/access/revoke",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      },
    ),

  revokeOtherSessions: () =>
    request<{ success: true; revoked: number; scope: "all" }>(
      "/api/console/access/revoke",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ all: true }),
      },
    ),

  /** One system on one server. The endpoint the dashboard itself uses. */
  toggle: (guildId: string, module: string, enabled: boolean) =>
    request<{ success: true; guild_id: string; publish_at: number | null }>("/api/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guild_id: guildId, module, enabled }),
    }),

  /** Every system on one server at once. */
  toggleAll: (guildId: string, enabled: boolean) =>
    request<{ success: true; guild_id: string; publish_at: number | null }>("/api/toggle-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guild_id: guildId, enabled }),
    }),

  /**
   * Sign this browser out, then leave for the sign-in page.
   *
   * The one request here that does not go through `request()`: `/logout`
   * answers a redirect to an HTML page, not JSON, so there is no payload to
   * check and nothing to hand back. The CSRF token still travels — the route
   * refuses a bare GET, which is what stops any site making a link sign the
   * operator out.
   */
  signOut: async () => {
    const headers = new Headers();
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
    try {
      await fetch("/logout", { method: "POST", credentials: "same-origin", headers });
    } finally {
      // Even a failed request leaves the page: the alternative is a console that
      // looks signed in and is not.
      window.location.assign("/owner/login");
    }
  },

  /** What Discord actually holds for one server, after a switch settles. */
  commandScope: (guildId: string) =>
    request<{
      success: true;
      available: number;
      publish: { state?: string; label?: string; at?: number; [key: string]: unknown };
      [key: string]: unknown;
    }>(`/api/command-scope?guild_id=${guildId}`),
};

/** The shell's own state, read once from the page it rendered. */
export function readBoot(): BootData {
  const node = document.getElementById("console-boot");
  if (!node) {
    throw new Error("console-boot is missing from the shell.");
  }
  return JSON.parse(node.textContent ?? "{}") as BootData;
}
