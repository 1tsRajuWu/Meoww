/** What changed, read from the project's own `CHANGELOG.md`.
 *
 * The same reader the public hub uses, so the two surfaces quote one file rather
 * than two summaries of it. The entry's leading emoji is its icon and its `lead`
 * is the claim — this page does not editorialise on top of a changelog that is
 * already written in full sentences.
 */

import { motion } from "motion/react";

import { api } from "../api";
import { Link } from "../lib/router";
import { useLive } from "../lib/useLive";
import { Card, Empty, Failure, Pill, Skeleton } from "../ui";

export function Changes() {
  const live = useLive(() => api.changes(), null);

  if (live.loading && !live.data) return <Skeleton rows={8} />;
  if (live.error && !live.data) return <Failure message={live.error} onRetry={live.refresh} />;

  const data = live.data;
  if (!data) return null;

  return (
    <div className="grid">
      <div className="page__head">
        <div>
          <span className="eyebrow">
            {data.total} entr{data.total === 1 ? "y" : "ies"} in the file
          </span>
          <h1>What changed</h1>
        </div>
      </div>

      {data.entries.length === 0 ? (
        <Card span={6} index={0}>
          <Empty
            title="No changelog on this deployment"
            body="CHANGELOG.md is missing or empty, which is a history this build cannot show rather than an error."
          />
        </Card>
      ) : (
        <div className="changes">
          {data.entries.map((entry, i) => (
            <motion.article
              className="change"
              key={`${entry.version}-${i}`}
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ type: "spring", stiffness: 200, damping: 26, delay: Math.min(i * 0.03, 0.5) }}
            >
              <header>
                <span className="change__icon" aria-hidden="true">
                  {entry.icon || "•"}
                </span>
                <h2>{entry.version}</h2>
              </header>
              {entry.lead && <p className="change__lead">{entry.lead}</p>}
              {entry.body && <p className="change__body">{entry.body}</p>}
            </motion.article>
          ))}
        </div>
      )}

      <Card span={3} index={1} title="The file itself">
        <p className="card__body">
          This page quotes <code>CHANGELOG.md</code> from the running deployment, so a change that
          is committed but not deployed does not appear here.
        </p>
        <div className="stack">
          <Link className="btn btn--ghost" to="/ops">
            Open the output tail
          </Link>
        </div>
      </Card>

      <Card span={3} index={2} title="How this deployment was built">
        <ul className="plain">
          <li>
            <Pill tone="accent">Console</Pill> a bundled app (React + Vite) served only to an operator sign-in
          </li>
          <li>
            <Pill>Dashboard</Pill> one server at a time, for that server's own admins
          </li>
          <li>
            <Pill>Site</Pill> public, read-only, at meow-bot.pages.dev
          </li>
        </ul>
      </Card>
    </div>
  );
}
