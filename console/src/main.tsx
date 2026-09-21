import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./styles.css";
import { readBoot } from "./api";
import { App } from "./App";
import {
  applyCursorColor,
  applyCursorStyle,
  applyTheme,
  currentCursorColor,
  currentCursorStyle,
  currentTheme,
} from "./lib/theme";

// The theme and cursor styles are applied before the first render, so a reload never flashes the
// wrong palette. `console.html` also sets `data-theme="midnight"` in the markup,
// which is what a first visit with nothing in localStorage paints.
applyTheme(currentTheme());
applyCursorStyle(currentCursorStyle());
applyCursorColor(currentCursorColor());

const node = document.getElementById("console-root");

/**
 * The boot payload is read **here** rather than inside the app, so the one case
 * where it is absent can be answered instead of thrown.
 *
 * `console.html` is the only thing that renders it, and it does so because Flask
 * is the only party that can: the CSRF token is minted against a session and the
 * cookie is `HttpOnly`, so a page that loaded `app.js` on its own could never
 * obtain it. That is not hypothetical — the bundle's own `index.html` was
 * reachable at `/static/console/index.html` until the entry point moved to
 * `src/main.tsx`, and opening it produced an empty page and a console error,
 * which is the least useful thing a URL can do.
 */
if (!node) {
  document.body.textContent = "This page has no place for the console to mount.";
} else if (document.getElementById("console-boot")) {
  createRoot(node).render(
    <StrictMode>
      <App boot={readBoot()} />
    </StrictMode>,
  );
} else {
  createRoot(node).render(
    <main className="console-orphan">
      <h1>Open this from the console</h1>
      <p>
        This is the console&rsquo;s script, loaded without the page it belongs to.
        The address to use is <a href="/owner">/owner</a>, which the bot serves —
        and which signs you in first if it needs to.
      </p>
    </main>,
  );
}
