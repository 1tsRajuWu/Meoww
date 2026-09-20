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
  fleet: () => request<Fleet>("/api/console/fleet"),
  server: (id: number) => request<ServerDetail>(`/api/console/server/${id}`),
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
