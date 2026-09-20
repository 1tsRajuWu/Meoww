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
  BootData,
  Changes,
  Fleet,
  Ops,
  Overview,
  ServerDetail,
  SystemsMatrix,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

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

  return data as T;
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

  server: async (id: number) => {
    const data = await request<ServerDetail>(`/api/console/server/${id}`);
    return { ...data, server: { ...data.server, icon: drawableImage(data.server.icon) } };
  },
  systems: () => request<SystemsMatrix>("/api/console/systems"),
  ops: () => request<Ops>("/api/console/ops"),
  changes: () => request<Changes>("/api/console/changes"),

  /** One system on one server. The endpoint the dashboard itself uses. */
  toggle: (guildId: number, module: string, enabled: boolean) =>
    request<{ success: true; guild_id: number; publish_at: number | null }>("/api/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guild_id: guildId, module, enabled }),
    }),

  /** Every system on one server at once. */
  toggleAll: (guildId: number, enabled: boolean) =>
    request<{ success: true; guild_id: number; publish_at: number | null }>("/api/toggle-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guild_id: guildId, enabled }),
    }),

  /** What Discord actually holds for one server, after a switch settles. */
  commandScope: (guildId: number) =>
    request<{
      success: true;
      available: number;
      publish: { state?: string; label?: string; [key: string]: unknown };
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
