/**
 * decat.pages.dev edge worker — serves the live BRB screen.
 *
 * Cloudflare Pages cannot run Python, so every page route is reverse-proxied
 * to the Flask backend on Hugging Face (same origin from the visitor's view);
 * only /static/* is served from the CDN. No-store rides along from the backend.
 */
const BACKEND_HOST = "1tsrajowo-meow.hf.space";

const PROXY_PATHS = [
  "/",
  "/index.html",
  "/systems",
  "/systems.html",
  "/start",
  "/start.html",
  "/pricing",
  "/pricing.html",
  "/premium",
  "/premium.html",
  "/privacy",
  "/privacy.html",
  "/terms",
  "/terms.html",
  "/ai",
  "/ai.html",
  "/support",
  "/donate",
  "/health",
];

const HOP_HEADERS = [
  "X-Forwarded-For",
  "X-Forwarded-Host",
  "X-Forwarded-Proto",
  "X-Real-IP",
  "Forwarded",
  "CF-Connecting-IP",
];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!PROXY_PATHS.includes(url.pathname)) {
      return env.ASSETS.fetch(request);
    }
    // The backend serves its Gradio shell at "/" — the live screen lives on
    // the Flask routes, so the front door reads through to one of them.
    const backendPath =
      url.pathname === "/" || url.pathname === "/index.html"
        ? "/systems"
        : url.pathname;
    const backendUrl = new URL(request.url);
    backendUrl.hostname = BACKEND_HOST;
    backendUrl.protocol = "https:";
    backendUrl.port = "";
    backendUrl.pathname = backendPath;
    const reqHeaders = new Headers(request.headers);
    for (const hop of HOP_HEADERS) {
      reqHeaders.delete(hop);
    }
    reqHeaders.set("Host", BACKEND_HOST);
    reqHeaders.set("X-Forwarded-Host", url.host);
    reqHeaders.set("X-Forwarded-Proto", "https");
    const visitorIp = request.headers.get("CF-Connecting-IP");
    if (visitorIp) {
      reqHeaders.set("X-Meow-Client-IP", visitorIp);
    }
    const hasBody = !["GET", "HEAD"].includes(request.method);
    let response;
    try {
      response = await fetch(
        new Request(backendUrl.toString(), {
          method: request.method,
          headers: reqHeaders,
          body: hasBody ? request.body : undefined,
          redirect: "manual",
        })
      );
    } catch (err) {
      return new Response("Backend gateway error", {
        status: 502,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }
    const resHeaders = new Headers(response.headers);
    const location = resHeaders.get("Location");
    if (location) {
      try {
        const locUrl = new URL(location, backendUrl.origin);
        if (locUrl.hostname === BACKEND_HOST) {
          resHeaders.set("Location", locUrl.pathname + locUrl.search + locUrl.hash);
        }
      } catch {
        // Relative location, keep as is
      }
    }
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: resHeaders,
    });
  },
};
