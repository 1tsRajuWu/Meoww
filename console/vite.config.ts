import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * The console builds to fixed filenames under `static/console/` on purpose.
 *
 * Flask renders the shell itself (it is the thing that knows the CSRF token and
 * who is signed in), so it has to name the final assets in a `<script>` tag. A
 * content-hashed bundle would need a manifest read on every request; two stable
 * names plus a cache-busting stamp Flask reads from the file's modification time
 * is the same guarantee with none of that. `modulePreload.polyfill` is off
 * because Vite's polyfill is an **inline** script and every page here runs under
 * `script-src 'self'`.
 */
export default defineConfig({
  plugins: [react()],
  base: "/static/console/",
  build: {
    outDir: "../static/console",
    emptyOutDir: true,
    target: "es2022",
    cssCodeSplit: false,
    modulePreload: { polyfill: false },
    sourcemap: false,
    rollupOptions: {
      // The build starts from the **module**, not from `index.html`.
      //
      // With the HTML as the entry, Vite emits a copy of it into the bundle — and
      // a file in `static/` is served, so `console/index.html`, whose own comment
      // says it exists only so `vite dev` has an entry point, was publicly
      // reachable. Opening it loaded `app.js` with no `console-boot` payload
      // beside it, so the app threw and painted an empty page. `vite dev` still
      // serves the root `index.html` (that is what dev does), so the file keeps
      // its job and loses its public one.
      input: "src/main.tsx",
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "chunk-[name].js",
        assetFileNames: (info) => {
          const name = (info as { name?: string; names?: string[] }).name
            ?? (info as { names?: string[] }).names?.[0]
            ?? "";
          return name.endsWith(".css") ? "app.css" : "assets/[name][extname]";
        },
      },
    },
  },
});
