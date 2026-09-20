/** The fleet, as a list you can search and sort.
 *
 * The row is designed around the one thing an operator scans for: a server
 * something is wrong in. So the systems bar is per row, and the systems that are
 * *off* are named on the row rather than counted — "3 of 11" says something is
 * wrong, the names say what to fix.
 */

import { motion } from "motion/react";
import { useMemo, useState } from "react";

import type { ServerRow } from "../types";
import { compact } from "../lib/format";
import { Link } from "../lib/router";
import { Card, Empty, Pill } from "../ui";

type SortKey = "members" | "name" | "off";

export function Servers({ servers }: { servers: ServerRow[] }) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("members");

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = needle
      ? servers.filter(
          (server) =>
            server.name.toLowerCase().includes(needle) ||
            String(server.id).includes(needle) ||
            server.off.some((name) => name.toLowerCase().includes(needle)),
        )
      : servers;
    const sorted = [...filtered];
    sorted.sort((a, b) => {
      if (sort === "name") return a.name.localeCompare(b.name);
      if (sort === "off") return b.off.length - a.off.length || b.members - a.members;
      return b.members - a.members;
    });
    return sorted;
  }, [query, servers, sort]);

  return (
    <div className="grid">
      <div className="page__head">
        <div>
          <span className="eyebrow">
            {servers.length} server{servers.length === 1 ? "" : "s"}
          </span>
          <h1>Fleet</h1>
        </div>
      </div>

      <Card span={6} index={0} title="Controls">
        <div className="toolbar">
          <input
            id="fleet-filter"
            className="input"
            placeholder="Filter by name, id, or a system that is off…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Filter servers"
          />
          <div className="segmented" role="group" aria-label="Sort">
            {(
              [
                ["members", "Most people"],
                ["off", "Most broken"],
                ["name", "Name"],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={`segmented__item${sort === key ? " is-current" : ""}`}
                aria-pressed={sort === key}
                onClick={() => setSort(key)}
              >
                {label}
              </button>
            ))}
          </div>
          <span className="toolbar__count" id="fleet-filter-count">
            showing {rows.length}
          </span>
        </div>
      </Card>

      {rows.length === 0 ? (
        <Card span={6} index={1}>
          <Empty
            title={servers.length ? "Nothing matches that filter" : "The bot is in no servers yet"}
            body={
              servers.length
                ? "Clear the filter to see all of them."
                : "Add it to a server and it will appear here within a moment of joining."
            }
            action={
              servers.length ? (
                <button type="button" className="btn" onClick={() => setQuery("")}>
                  Clear the filter
                </button>
              ) : null
            }
          />
        </Card>
      ) : (
        <div className="fleet" role="list">
          {rows.map((server, i) => (
            <motion.div
              role="listitem"
              key={server.id}
              className="fleet-row"
              data-name={server.name}
              data-id={server.id}
              data-members={server.members}
              data-off={server.off.length}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ type: "spring", stiffness: 240, damping: 26, delay: i * 0.025 }}
            >
              <Link to={`/servers/${server.id}`} className="fleet-row__link">
                <span className="fleet-row__avatar">
                  {server.icon ? (
                    <img src={server.icon} alt="" width={36} height={36} />
                  ) : (
                    <span className="fleet-row__initial">{server.name.slice(0, 1)}</span>
                  )}
                </span>

                <span className="fleet-row__name">
                  <strong>{server.name}</strong>
                  <small>
                    {compact(server.members)} members
                    {server.nick ? ` · called ${server.nick}` : ""}
                  </small>
                </span>

                <span className="fleet-row__systems">
                  <span className="meter" aria-hidden="true">
                    <motion.span
                      className={`meter__fill${server.on === 0 ? " is-empty" : ""}`}
                      initial={{ scaleX: 0 }}
                      animate={{ scaleX: server.total ? server.on / server.total : 0 }}
                      transition={{ type: "spring", stiffness: 170, damping: 24, delay: 0.1 }}
                    />
                  </span>
                  <small>
                    {server.known ? `${server.on} of ${server.total} on` : "state unreadable"}
                  </small>
                </span>

                <span className="fleet-row__off">
                  {server.off.length === 0 ? (
                    <Pill tone="good">every system on</Pill>
                  ) : (
                    <Pill tone={server.off.length > server.total / 2 ? "danger" : "warn"}>
                      {server.off.length} off
                    </Pill>
                  )}
                </span>
              </Link>

              {server.off.length > 0 && (
                <p className="fleet-row__names">Off: {server.off.join(", ")}</p>
              )}
            </motion.div>
          ))}
        </div>
      )}
    </div>
  );
}
