import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./styles.css";
import { App } from "./App";
import { applyTheme, currentTheme } from "./lib/theme";

// The theme is applied before the first render, so a reload never flashes the
// wrong palette. `console.html` also sets `data-theme="midnight"` in the markup,
// which is what a first visit with nothing in localStorage paints.
applyTheme(currentTheme());

const node = document.getElementById("console-root");
if (!node) {
  throw new Error("console-root is missing from the shell.");
}

createRoot(node).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
