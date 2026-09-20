/** Every system, against every server — the one screen that answers "where is
 * this off?" without opening eleven pages.
 *
 * A cell is a real switch, and it is the same endpoint the per-server page calls,
 * so the two cannot disagree about how to change something. The matrix does not
 * shrink to fit: it scrolls sideways, because eleven servers at an unreadable size
 * is worse than eleven servers you push sideways.
 */

import { useState } from "react";

import { api, ApiError } from "../api";
import type { ServerRow, SystemsMatrix } from "../types";
import { compact } from "../lib/format";
import { Link } from "../lib/router";
import { useLive } from "../lib/useLive";
import { Bars, Card, Empty, Failure, Pill, Skeleton, Switch } from "../ui";

export function Systems({
  servers,
  onGone,
}: {
  servers: ServerRow[];
  onGone: (text: string, tone?: "good" | "warn" | "danger") => void;
}) {
  const live = useLive(() => api.systems(), 60_000);
  const [cells, setCells] = useState<Record<string, boolean> | null>(null);
  const [busy, setBusy] = useState<string>("");

  if (live.loading && !live.data) return <Skeleton rows={8} />;
  if (live.error && !live.data) return <Failure message={live.error} onRetry={live.refresh} />;

  const data: SystemsMatrix | null = live.data;
  if (!data) return null;

  const matrix = cells ?? flatten(data.matrix);
  const columns = data.servers.length ? data.servers : servers.map((s) => ({ id: s.id, name: s.name, icon: s.icon, members: s.members }));

  const flip = async (guildId: number, moduleId: string, next: boolean) => {
    const key = `${guildId}:${moduleId}`;
    setBusy(key);
    const before = matrix[key] ?? false;
    setCells({ ...matrix, [key]: next });
    try {
      await api.toggle(guildId, moduleId, next);
    } catch (exc) {
      setCells({ ...matrix, [key]: before });
      onGone(exc instanceof ApiError ? exc.message : String(exc), "danger");
    } finally {
      setBusy("");
    }
  };

  const bars = data.systems.map((system) => ({
    name: system.name,
    value: system.commands.length,
  }));

  return (
    <div className="grid">
      <div className="page__head">
        <div>
          <span className="eyebrow">
            {data.systems.length} systems · {columns.length} servers
          </span>
          <h1>Systems</h1>
        </div>
        <span className="readstamp">{live.refreshing ? "reading…" : "read now"}</span>
      </div>

      {data.unreadable.length > 0 && (
        <Card span={6} index={0} tone="warn" title="Some server state could not be read">
          <p className="card__body">
            {data.unreadable.length} server(s) returned no module state, so their column is
            blank rather than all-off. Reload to try again.
          </p>
        </Card>
      )}

      <Card span={6} index={1} title="Where every system stands">
        {columns.length === 0 || data.systems.length === 0 ? (
          <Empty title="Nothing to show" body="No servers, or no switchable systems." />
        ) : (
          <div className="matrix__scroll">
            <table className="matrix">
              <thead>
                <tr>
                  <th scope="col">System</th>
                  {columns.map((server) => (
                    <th scope="col" key={server.id} title={`${server.name} · ${compact(server.members)} members`}>
                      <Link to={`/servers/${server.id}`}>{server.name}</Link>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.systems.map((system) => (
                  <tr key={system.id}>
                    <th scope="row">
                      <span aria-hidden="true">{system.icon}</span> {system.name}
                      <small>{system.commands.length} commands</small>
                    </th>
                    {columns.map((server) => {
                      const key = `${server.id}:${system.id}`;
                      const value = matrix[key];
                      return (
                        <td key={key}>
                          {value === undefined ? (
                            <span className="matrix__unknown" title="state unreadable">
                              ?
                            </span>
                          ) : (
                            <Switch
                              checked={value}
                              busy={busy === key}
                              label={`${system.name} in ${server.name}`}
                              onChange={(next) => void flip(server.id, system.id, next)}
                            />
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card span={3} index={2} title="Size of each system" hint="Commands each one gates.">
        <Bars values={bars} label="commands per system" />
      </Card>

      <Card span={3} index={3} title="Always on" hint="The difference between the two counts.">
        <ul className="plain">
          {data.always_on.map((name) => (
            <li key={name}>
              <Pill tone="accent">{name}</Pill>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

/** `{guild: {module: bool}}` flattened to `{guild:module: bool}` for lookup. */
function flatten(matrix: SystemsMatrix["matrix"]): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  for (const [guild, systems] of Object.entries(matrix)) {
    for (const [moduleId, enabled] of Object.entries(systems)) {
      out[`${guild}:${moduleId}`] = enabled;
    }
  }
  return out;
}
