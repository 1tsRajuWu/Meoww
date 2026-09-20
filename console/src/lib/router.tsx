/** A ~60-line router, instead of a dependency.
 *
 * The console has six addresses and no nested layouts worth a router library.
 * What it does need is that every address is a *real* URL — the operator
 * bookmarks `/owner/ops`, and the server serves the same shell for any path
 * under `/owner`, so pushing state and listening to `popstate` is the whole job.
 */

import { useCallback, useEffect, useState, type AnchorHTMLAttributes, type MouseEvent, type ReactNode } from "react";

/** Where the console is mounted. Keys off the server's own route table. */
export const BASE = "/owner";

/** The path below `/owner`: `""`, `"/servers"`, `"/servers/123"`. */
export function usePath(): string {
  const [path, setPath] = useState(() => currentPath());

  useEffect(() => {
    const onPop = () => setPath(currentPath());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  return path;
}

function currentPath(): string {
  const full = window.location.pathname;
  if (!full.startsWith(BASE)) return "/";
  const rest = full.slice(BASE.length);
  return rest === "" ? "" : rest.replace(/\/+$/, "");
}

/** Move to a console address. Pushes history; Back then works as expected. */
export function navigate(to: string): void {
  const target = `${BASE}${to}`;
  if (window.location.pathname === target) return;
  window.history.pushState(null, "", target);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

/** A link that goes through the router rather than reloading the bundle. */
export function Link({
  to,
  className,
  children,
  onClick,
  ...rest
}: {
  to: string;
  className?: string;
  children: ReactNode;
  onClick?: () => void;
} & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href" | "onClick">) {
  const handle = useCallback(
    (event: MouseEvent<HTMLAnchorElement>) => {
      // Leave modified clicks alone: cmd-click should open a new tab like it does
      // on any other link on the web.
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
        return;
      }
      event.preventDefault();
      onClick?.();
      navigate(to);
    },
    [to, onClick],
  );

  return (
    <a href={`${BASE}${to}`} className={className} onClick={handle} {...rest}>
      {children}
    </a>
  );
}
