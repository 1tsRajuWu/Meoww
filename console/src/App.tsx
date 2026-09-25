/** The app: reads the shell's boot data, keeps the fleet list, routes the pages.
 *
 * The fleet is fetched **once here** rather than by each page, because three
 * pages need it (the rail's palette, the server list, the systems matrix) and the
 * one thing worse than an extra request is two screens disagreeing about how many
 * servers there are.
 */

import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { useMemo } from "react";

import { api, setCsrfToken } from "./api";
import type { BootData, ServerRow } from "./types";
import { usePath } from "./lib/router";
import { useLive } from "./lib/useLive";
import { Cursor } from "./app/Cursor";
import { Shell } from "./app/Shell";
import type { PaletteAction } from "./app/CommandPalette";
import { Failure, Skeleton, ToastHost, useToast } from "./ui";
import { Overview } from "./pages/Overview";
import { Live } from "./pages/Live";
import { Servers } from "./pages/Servers";
import { ServerDetail } from "./pages/ServerDetail";
import { Systems } from "./pages/Systems";
import { Commands } from "./pages/Commands";
import { Economy } from "./pages/Economy";
import { OpsPage } from "./pages/Ops";
import { Questions } from "./pages/Questions";
import { Access } from "./pages/Access";
import { Changes } from "./pages/Changes";
import { God } from "./pages/God";
import { Missing } from "./pages/Missing";

export function App({ boot }: { boot: BootData }) {
  // Read by `main.tsx` before the first render, and passed in: it is a fact about
  // the document, so it cannot change without a reload — and reading it in the
  // entry point is what lets a missing payload be answered rather than thrown.
  setCsrfToken(boot.csrfToken);

  const fleet = useLive<ServerRow[]>(
    async () => (await api.fleet()).servers,
    60_000,
  );
  const servers = useMemo(() => fleet.data ?? [], [fleet.data]);

  return (
    // Every animation in this app is a JS spring or tween, and a stylesheet's
    // `prefers-reduced-motion` block cannot reach one: the media query stops CSS
    // transitions, while `motion` keeps writing frame-by-frame inline styles. So
    // the setting is handed to the animation library itself, which then skips
    // transforms and keeps only what still communicates (opacity, colour). The
    // reduced-motion block in `styles.css` stays where it is — it covers the
    // hand-written transitions, and the two together are what the law asks for.
    <MotionConfig reducedMotion="user">
      <ToastHost>
        <Cursor />
        <Router boot={boot} servers={servers} fleetError={fleet.error} fleetLoading={fleet.loading} />
      </ToastHost>
    </MotionConfig>
  );
}

function Router({
  boot,
  servers,
  fleetError,
  fleetLoading,
}: {
  boot: BootData;
  servers: ServerRow[];
  fleetError: string | null;
  fleetLoading: boolean;
}) {
  const path = usePath();
  const toast = useToast();

  const page = useMemo(() => {
    const detail = /^\/servers\/(\d+)$/.exec(path);
    // The id stays **text**. `Number()` here is exactly how
    // `/owner/servers/1542278896148422676` became `/owner/servers/…42700` and
    // then a "No such server" — see `ServerRow.id` in `types.ts`.
    if (detail) return { kind: "server" as const, id: String(detail[1]) };
    if (path === "/live") return { kind: "live" as const };
    if (path === "/servers") return { kind: "servers" as const };
    if (path === "/systems") return { kind: "systems" as const };
    if (path === "/commands") return { kind: "commands" as const };
    if (path === "/economy") return { kind: "economy" as const };
    if (path === "/ops") return { kind: "ops" as const };
    if (path === "/questions") return { kind: "questions" as const };
    if (path === "/access") return { kind: "access" as const };
    if (path === "/changes") return { kind: "changes" as const };
    if (path === "/god") return { kind: "god" as const };
    // `/owner` itself is the Overview; **anything else that matched nothing is
    // not**. This line used to fall through to the Overview for both, so
    // `/owner/nonsense` drew a page that looked correct at an address that was
    // not, with no `aria-current` anywhere to admit it — found by typing one.
    if (path === "" || path === "/") return { kind: "overview" as const };
    return { kind: "missing" as const };
  }, [path]);

  // Only the two destinations that genuinely leave this app are here.
  //
  // There was a "Refresh every reading" action too, and it ran
  // `window.location.reload()` — which is how driving the palette found it: it
  // sat **first**, so opening ⌘K and pressing Return threw the whole app away and
  // reloaded the document. Nothing here needs that. Every page polls, and the
  // two that have a manual control own it themselves.
  const actions: PaletteAction[] = useMemo(
    () => [
      {
        id: "action:docs",
        label: "Open the per-server dashboard",
        hint: "another surface",
        run: () => {
          // A genuinely different page on the same origin, so a real navigation.
          window.location.assign("/dashboard");
        },
      },
      {
        id: "action:site",
        label: "Open the public site",
        hint: "another surface",
        run: () => {
          window.open("https://meow-bot.pages.dev", "_blank", "noopener");
        },
      },
      {
        id: "action:signout",
        label: "Sign out this browser",
        hint: "ends this session",
        run: () => void api.signOut(),
      },
    ],
    [],
  );

  const body = () => {
    if (page.kind === "live") return <Live />;
    if (page.kind === "servers") {
      if (fleetLoading && !servers.length) return <Skeleton rows={6} />;
      if (fleetError && !servers.length) {
        return <Failure message={fleetError} onRetry={() => window.location.reload()} />;
      }
      return <Servers servers={servers} />;
    }
    if (page.kind === "server") {
      return <ServerDetail guildId={page.id} servers={servers} onGone={toast} />;
    }
    if (page.kind === "systems") {
      return <Systems servers={servers} onGone={toast} />;
    }
    if (page.kind === "commands") return <Commands servers={servers} />;
    if (page.kind === "economy") return <Economy onGone={toast} />;
    if (page.kind === "ops") return <OpsPage />;
    if (page.kind === "questions") return <Questions />;
    if (page.kind === "access") return <Access />;
    if (page.kind === "changes") return <Changes />;
    if (page.kind === "god") return <God onGone={toast} />;
    if (page.kind === "missing") return <Missing path={path} />;
    return <Overview boot={boot} servers={servers} />;
  };

  return (
    <Shell boot={boot} servers={servers} actions={actions}>
      <AnimatePresence mode="wait">
        <motion.main
          className="page"
          key={path}
          initial={{ opacity: 0, y: 14, scale: 0.985 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -10, scale: 0.99 }}
          transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
        >
          {body()}
        </motion.main>
      </AnimatePresence>
    </Shell>
  );
}
