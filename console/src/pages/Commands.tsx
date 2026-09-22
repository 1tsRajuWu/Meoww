/** Every command the bot offers, one row each, and the server it is off in.
 *
 * The question this page answers is the one the fleet and matrix pages cannot:
 * *which commands exist, and is the one I am looking for actually here*. The
 * registry says what the deployment does; the live command tree says what
 * Discord was told; and per server, `command_scope`'s own rule says where a
 * command is in the picker. A command that is registered and published but
 * switched off in a server shows up as **off in that server, by name** — which
 * is the difference between "the bot is missing it" and "this server turned it
 * off", and those two look identical from inside Discord.
 *
 * Nothing here is a total that hides a name: the two cards under the table are
 * the two ways a command list can disagree with itself, each naming the commands
 * it is about.
 */

import { motion } from "motion/react";
import { useMemo, useState } from "react";

import { api } from "../api";
import type { CommandRow } from "../types";
import { compact } from "../lib/format";
import { Link } from "../lib/router";
import { useLive } from "../lib/useLive";
import { Card, Empty, Failure, Pill, Skeleton } from "../ui";

type Filter = "all" | "off" | "core";

export function Commands({ servers }: { servers: { id: string; name: string; icon: string; members: number }[] }) {
  const live = useLive(() => api.commands(), 60_000);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [system, setSystem] = useState("");

  const data = live.data;

  const systems = useMemo(() => {
    if (!data) return [] as { id: string; name: string }[];
    const seen = new Map<string, string>();
    for (const row of data.commands) {
      if (!seen.has(row.system)) seen.set(row.system, row.system_name);
    }
    return [...seen].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name));
  }, [data]);

  const rows = useMemo(() => {
    const all = data?.commands ?? [];
    const needle = query.trim().toLowerCase();
    return all.filter((row) => {
      if (filter === "off" && row.off_in.length === 0) return false;
      if (filter === "core" && row.declared && row.system) return false;
      if (system && row.system !== system) return false;
      if (!needle) return true;
      return (
        row.name.toLowerCase().includes(needle) ||
        row.system_name.toLowerCase().includes(needle) ||
        row.off_in.some((name) => name.toLowerCase().includes(needle))
      );
    });
  }, [data, filter, query, system]);

  if (live.loading && !data) return <Skeleton rows={8} />;
  if (live.error && !data) return <Failure message={live.error} onRetry={live.refresh} />;
  if (!data) return null;

  // The matrix's columns, which is the fleet list when the payload has none.
  const columns = data.servers.length ? data.servers : servers;
  const offSomewhere = data.commands.filter((row) => row.off_in.length > 0).length;

  return (
    <div className="grid">
      <div className="page__head">
        <div>
          <span className="eyebrow">
            {data.totals.declared} commands · {data.totals.published} published
          </span>
          <h1>Commands</h1>
        </div>
        <span className="readstamp">{live.refreshing ? "reading…" : "read now"}</span>
      </div>

      {!data.tree_read && (
        <Card span={6} index={0} tone="warn" title="Discord's copy could not be read">
          <p className="card__body">
            The gateway has not answered with a command tree yet, so this page can show what the
            registry declares and nothing about what Discord was told. The list below is still the
            bot's own; the per-server column is what its switches say. Reload once the bot is up.
          </p>
        </Card>
      )}

      {data.unreadable.length > 0 && (
        <Card span={6} index={0} tone="warn" title="Some server state could not be read">
          <p className="card__body">
            {data.unreadable.length} server(s) returned no module state, so their column reads
            "unreadable" rather than "off". Reload to try again.
          </p>
        </Card>
      )}

      <Card span={6} index={1} title="Every command" hint="Off in a server means absent from its picker.">
        <div className="toolbar">
          <input
            className="input"
            placeholder="Find a command, or a server it is off in…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Filter commands"
          />
          <div className="segmented" role="group" aria-label="Filter">
            {(
              [
                ["all", "All"],
                ["off", `Off somewhere (${offSomewhere})`],
                ["core", "Always on"],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={`segmented__item${filter === key ? " is-current" : ""}`}
                aria-pressed={filter === key}
                onClick={() => setFilter(key)}
              >
                {label}
              </button>
            ))}
          </div>
          <select
            className="input input--select"
            value={system}
            onChange={(event) => setSystem(event.target.value)}
            aria-label="Filter by system"
          >
            <option value="">Every system</option>
            {systems.map((option) => (
              <option key={option.id || "core"} value={option.id}>
                {option.name}
              </option>
            ))}
          </select>
          <span className="toolbar__count">showing {rows.length}</span>
        </div>

        {rows.length === 0 ? (
          <Empty
            title="Nothing matches that filter"
            body="Clear the filter, or try the command's own name."
          />
        ) : (
          <div className="matrix__scroll">
            <table className="matrix cmds">
              <thead>
                <tr>
                  <th scope="col">Command</th>
                  <th scope="col">System</th>
                  {columns.map((server) => (
                    <th scope="col" key={server.id} title={`${server.name} · ${compact(server.members)} members`}>
                      <Link to={`/servers/${server.id}`}>{server.name}</Link>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <CommandRowView key={`${row.system}:${row.name}`} row={row} columns={columns} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card
        span={3}
        index={2}
        tone={data.missing.length ? "danger" : undefined}
        title="Declared, but not published"
        hint="Registered here and absent from Discord's tree."
      >
        {!data.tree_read ? (
          <p className="card__body">Nothing to compare against yet.</p>
        ) : data.missing.length === 0 ? (
          <p className="card__body">
            Every declared command is in the tree — <Pill tone="good">in step</Pill>
          </p>
        ) : (
          <ul className="plain">
            {data.missing.map((name) => (
              <li key={name}>
                <Pill tone="danger">/{name}</Pill>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        span={3}
        index={3}
        tone={data.unregistered.length ? "warn" : undefined}
        title="Published, but not registered"
        hint="In Discord's tree with nothing gating it."
      >
        {data.unregistered.length === 0 ? (
          <p className="card__body">
            Every command in the tree belongs to a system, so every one of them can be switched.
          </p>
        ) : (
          <ul className="plain">
            {data.unregistered.map((name) => (
              <li key={name}>
                <Pill tone="warn">/{name}</Pill>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function CommandRowView({
  row,
  columns,
}: {
  row: CommandRow;
  columns: { id: string; name: string }[];
}) {
  return (
    // No class on the row itself: what a row has to say about a command is said
    // by its cells, and one per server at that. `cmds__row is-off` used to sit
    // here — a name no rule in the sheet ever defined, asserting a state the row
    // does not have (a command off in one server and on in another is not an off
    // row), one line above the dots that report it exactly.
    <motion.tr
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 220, damping: 28 }}
    >
      <th scope="row">
        <code>/{row.name}</code>
        {!row.declared && (
          <>
            {" "}
            <Pill tone="warn" title="Discord offers this and the feature registry does not know it, so no switch takes it away.">
              ungated
            </Pill>
          </>
        )}
      </th>
      <td className="cmds__system">
        <span aria-hidden="true">{row.icon}</span> {row.system_name}
      </td>
      {columns.map((server) => {
        const off = row.off_in.includes(server.name);
        return (
          <td key={`${server.id}:${row.name}`}>
            <span
              className={`cmds__dot${off ? " is-off" : " is-on"}`}
              title={off ? `Off in ${server.name}` : `Available in ${server.name}`}
              aria-label={off ? `Off in ${server.name}` : `Available in ${server.name}`}
            >
              {off ? "off" : "on"}
            </span>
          </td>
        );
      })}
    </motion.tr>
  );
}
