/** The nine themes, matching the ones the public site offers.
 *
 * They live here as names and one hex each — the values themselves are in
 * `styles.css` under `[data-theme="..."]`, because a theme is a set of colours and
 * a browser is the right thing to resolve them. All this module owns is *which*
 * one is chosen and remembering it, and the one thing CSS cannot do: repaint the
 * address bar, which takes a `<meta name="theme-color">` and therefore a script.
 */

export const THEMES = [
  { id: "midnight", label: "Midnight", hex: "#08080a" },
  { id: "sakura", label: "Sakura", hex: "#140d12" },
  { id: "forest", label: "Forest", hex: "#0a1210" },
  { id: "ember", label: "Ember", hex: "#140e0a" },
  { id: "sunset", label: "Sunset Flare", hex: "#140a10" },
  { id: "plasma", label: "Cyber Plasma", hex: "#120a1c" },
  { id: "aurora", label: "Aurora Borealis", hex: "#061313" },
  { id: "cosmic", label: "Cosmic Nebula", hex: "#10091e" },
  { id: "synthwave", label: "Synthwave", hex: "#0e0a1a" },
] as const;

export type ThemeId = (typeof THEMES)[number]["id"];

/* --------------------------------------------------------------- the mark */

/**
 * The themes that ship their **own** brand mark.
 *
 * `Shell` draws `/static/logo-<theme>.svg`, and that is a *promise the file
 * exists* — one the console could not keep. Five of the nine themes went looking
 * for `logo-aurora.svg`, `logo-cosmic.svg`, `logo-plasma.svg`, `logo-sunset.svg`
 * and `logo-synthwave.svg`, none of which is in `static/`, so the rail drew the
 * browser's broken-image glyph next to the bot's own name. The four themes below
 * are the ones with a file; **everything else falls back to `logo.svg`**, which is
 * the mark the product is actually branded with.
 *
 * The list is here rather than in the component because this module is what owns
 * the theme vocabulary, and it is checked rather than trusted: `tests/test_design_laws.py`
 * walks `THEMES` against `static/` and fails on a theme that has a logo but is not
 * listed, or is listed without one — which is the shape the original bug had, and
 * the reason it went unnoticed for two releases.
 */
export const THEMED_LOGOS: ThemeId[] = ["sakura", "forest", "ember"];

/**
 * The brand mark for a theme: its own if one was drawn, the default otherwise.
 *
 * A single door on purpose. The path used to be built at the call site, so the
 * component decided what existed, and nothing could check it.
 */
export function logoFor(id: ThemeId): string {
  return THEMED_LOGOS.includes(id) ? `/static/logo-${id}.svg` : "/static/logo.svg";
}

/* Named in full rather than `KEY`, which is what a secret scanner is looking
 * for: a variable called `KEY` assigned a string reads, to a scanner and to
 * somebody skimming, exactly like a credential. This is the name of a
 * `localStorage` entry — the theme this browser chose — and saying so in the
 * name is both more honest and one fewer false alarm drowning the real ones. */
const STORAGE_KEY = "meow-console-theme";

export function currentTheme(): ThemeId {
  const saved = window.localStorage.getItem(STORAGE_KEY);
  const found = THEMES.find((theme) => theme.id === saved);
  return found ? found.id : "midnight";
}

export function applyTheme(id: ThemeId): void {
  document.documentElement.dataset.theme = id;
  window.localStorage.setItem(STORAGE_KEY, id);
  const meta = document.querySelector('meta[name="theme-color"]');
  const hex = THEMES.find((theme) => theme.id === id)?.hex;
  if (meta && hex) meta.setAttribute("content", hex);
}

/* ----------------------------------------------------------- cursor styles */

export const CURSOR_STYLES = [
  { id: "sparkle", label: "Sparkle Swarm", desc: "Twinkling star cluster with celestial constellation flare" },
  { id: "sword", label: "Cyber Blade", desc: "16-bit energy katana with slash attack on click" },
  { id: "radar", label: "Tactical Radar HUD", desc: "Conic scanning beam with target-locking brackets" },
  { id: "blob", label: "Velocity Blob", desc: "Squash & stretch liquid mercury drop with jelly splat" },
  { id: "vortex", label: "Quantum Sacred Vortex", desc: "Dual counter-rotating sacred runes with singularity collapse" },
  { id: "system", label: "Classic System", desc: "Standard operating system pointer" },
] as const;

export type CursorStyleId = (typeof CURSOR_STYLES)[number]["id"];

export const CURSOR_COLORS = [
  {
    id: "theme",
    label: "Theme Match",
    desc: "Adaptive to active theme palette",
    group: "accent",
    accent: "var(--accent)",
    accentRgb: "124, 140, 255",
    glow: "rgba(124, 140, 255, 0.45)",
    grad: "linear-gradient(135deg, var(--accent) 0%, #ffffff 100%)",
    preview: "linear-gradient(135deg, #7c8cff, #ff74c1)",
  },
  {
    id: "cyan",
    label: "Electric Cyan",
    desc: "Neon cyan (#00f3ff)",
    group: "solid",
    accent: "#00f3ff",
    accentRgb: "0, 243, 255",
    glow: "rgba(0, 243, 255, 0.45)",
    grad: "linear-gradient(135deg, #00f3ff 0%, #00a8ff 100%)",
    preview: "#00f3ff",
  },
  {
    id: "magenta",
    label: "Neon Magenta",
    desc: "Vibrant cyber magenta (#ff007f)",
    group: "solid",
    accent: "#ff007f",
    accentRgb: "255, 0, 127",
    glow: "rgba(255, 0, 127, 0.45)",
    grad: "linear-gradient(135deg, #ff007f 0%, #ff4b93 100%)",
    preview: "#ff007f",
  },
  {
    id: "lime",
    label: "Matrix Lime",
    desc: "High-voltage lime (#00ff88)",
    group: "solid",
    accent: "#00ff88",
    accentRgb: "0, 255, 136",
    glow: "rgba(0, 255, 136, 0.45)",
    grad: "linear-gradient(135deg, #00ff88 0%, #00cc6a 100%)",
    preview: "#00ff88",
  },
  {
    id: "gold",
    label: "Solar Gold",
    desc: "Radiant cyber gold (#ffb700)",
    group: "solid",
    accent: "#ffb700",
    accentRgb: "255, 183, 0",
    glow: "rgba(255, 183, 0, 0.45)",
    grad: "linear-gradient(135deg, #ffb700 0%, #ff8800 100%)",
    preview: "#ffb700",
  },
  {
    id: "gradient-sunset",
    label: "Sunset Flare",
    desc: "Pink to orange to gold",
    group: "gradient",
    accent: "#ff007f",
    accentRgb: "255, 0, 127",
    glow: "rgba(255, 94, 0, 0.55)",
    grad: "linear-gradient(135deg, #ff007f 0%, #ff8c00 50%, #ffe600 100%)",
    preview: "linear-gradient(135deg, #ff007f, #ff8c00, #ffe600)",
  },
  {
    id: "gradient-plasma",
    label: "Hyper Plasma",
    desc: "Deep violet to electric cyan",
    group: "gradient",
    accent: "#00f3ff",
    accentRgb: "0, 243, 255",
    glow: "rgba(191, 0, 255, 0.55)",
    grad: "linear-gradient(135deg, #bf00ff 0%, #7928ca 50%, #00f3ff 100%)",
    preview: "linear-gradient(135deg, #bf00ff, #00f3ff)",
  },
  {
    id: "gradient-aurora",
    label: "Aurora Borealis",
    desc: "Emerald green to cyan to violet",
    group: "gradient",
    accent: "#00ff88",
    accentRgb: "0, 255, 136",
    glow: "rgba(0, 243, 255, 0.55)",
    grad: "linear-gradient(135deg, #00ff88 0%, #00f3ff 50%, #7000ff 100%)",
    preview: "linear-gradient(135deg, #00ff88, #00f3ff, #7000ff)",
  },
  {
    id: "gradient-cosmic",
    label: "Cosmic Nebula",
    desc: "Magenta to purple to cyber cyan",
    group: "gradient",
    accent: "#ff007f",
    accentRgb: "255, 0, 127",
    glow: "rgba(121, 40, 202, 0.55)",
    grad: "linear-gradient(135deg, #ff007f 0%, #7928ca 50%, #00f3ff 100%)",
    preview: "linear-gradient(135deg, #ff007f, #7928ca, #00f3ff)",
  },
] as const;

export type CursorColorId = (typeof CURSOR_COLORS)[number]["id"];

const CURSOR_STYLE_KEY = "meow-cursor-style";
const CURSOR_COLOR_KEY = "meow-cursor-color";

export function currentCursorStyle(): CursorStyleId {
  try {
    let saved = window.localStorage.getItem(CURSOR_STYLE_KEY);
    if (saved === "crosshair" || saved === "ring" || saved === "halo" || saved === "reticle") saved = "radar";
    if (saved === "blade" || saved === "stealth") saved = "sword";
    if (saved === "diamond") saved = "sparkle";
    if (saved === "orbit" || saved === "nova") saved = "vortex";
    if (saved === "comet" || saved === "bubble") saved = "blob";
    const found = CURSOR_STYLES.find((c) => c.id === saved);
    return found ? found.id : "sparkle";
  } catch {
    return "sparkle";
  }
}

export function applyCursorStyle(id: CursorStyleId): void {
  try {
    window.localStorage.setItem(CURSOR_STYLE_KEY, id);
  } catch {}
  document.documentElement.setAttribute("data-cursor-style", id);
  if (id === "system") {
    document.documentElement.classList.remove("has-meow-cursor");
  } else {
    document.documentElement.classList.add("has-meow-cursor");
  }
}

export function currentCursorColor(): CursorColorId {
  try {
    const saved = window.localStorage.getItem(CURSOR_COLOR_KEY);
    const found = CURSOR_COLORS.find((c) => c.id === saved);
    return found ? found.id : "theme";
  } catch {
    return "theme";
  }
}

export function applyCursorColor(id: CursorColorId): void {
  try {
    window.localStorage.setItem(CURSOR_COLOR_KEY, id);
  } catch {}
  document.documentElement.setAttribute("data-cursor-color", id);
  const found = CURSOR_COLORS.find((c) => c.id === id);
  if (found) {
    if (found.id === "theme") {
      document.documentElement.style.removeProperty("--cursor-accent");
      document.documentElement.style.removeProperty("--cursor-accent-rgb");
      document.documentElement.style.removeProperty("--cursor-glow");
      document.documentElement.style.removeProperty("--cursor-grad");
    } else {
      document.documentElement.style.setProperty("--cursor-accent", found.accent);
      document.documentElement.style.setProperty("--cursor-accent-rgb", found.accentRgb);
      document.documentElement.style.setProperty("--cursor-glow", found.glow);
      document.documentElement.style.setProperty("--cursor-grad", found.grad);
    }
  }
}

