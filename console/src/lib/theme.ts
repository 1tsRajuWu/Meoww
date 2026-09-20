/** The four themes, matching the ones the public site offers.
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
] as const;

export type ThemeId = (typeof THEMES)[number]["id"];

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
