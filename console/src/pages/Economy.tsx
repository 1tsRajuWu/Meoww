/** One server's economy: what is in circulation, who holds it, and the one place
 * this console can move coins.
 *
 * Two rules shape this page, and both come from defects the bot's own commands
 * have already paid for:
 *
 * * **A debit is never silently clamped.** `/eco-admin remove` was rewritten
 *   because `MAX(0, wallet + amount)` confirmed a removal that never happened —
 *   it reported "removed 10,000" for a wallet holding 50. So the server refuses
 *   an over-withdrawal, this page shows its sentence, and there is no path here
 *   that rounds a balance down and says it worked.
 * * **Three numbers, not one.** Wallet + bank is what *members* hold; the Meow
 *   Fee is banked rather than burned (`utils/meowfee.py`), so coins that have
 *   left every member's balance are still in the server. A card that printed one
 *   total and called it "in circulation" would be a number that does not add up.
 *
 * Everything here is per *server*, because that is what an economy is — the
 * balances table is keyed by `(user_id, guild_id)`, and the same person holds
 * different coins in each one.
 */

import { motion } from "motion/react";
import { useEffect, useMemo, useState } from "react";

import { api, ApiError } from "../api";
import type { EconomyAction, EconomyPayload } from "../types";
import { compact } from "../lib/format";
import { Link } from "../lib/router";
import { useLive } from "../lib/useLive";
import { Card, Empty, Failure, Pill, Skeleton, Stat } from "../ui";

const ACTIONS: { id: EconomyAction; label: string; verb: string }[] = [
  { id: "wallet_add", label: "Add · wallet", verb: "added to" },
  { id: "wallet_remove", label: "Remove · wallet", verb: "removed from" },
  { id: "bank_add", label: "Add · bank", verb: "added to" },
  { id: "bank_remove", label: "Remove · bank", verb: "removed from" },
  { id: "set", label: "Set exact", verb: "set for" },
];

export function Economy({
  onGone,
}: {
  onGone: (text: string, tone?: "good" | "warn" | "danger") => void;
}) {
  const [guildId, setGuildId] = useState<string>("");
  const live = useLive(() => api.economy(guildId || undefined), 30_000, [guildId]);
  const data: EconomyPayload | null = live.data;

  const [query, setQuery] = useState("");
  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const all = data?.top ?? [];
    if (!needle) return all;
    return all.filter(
      (row) => row.user_id.includes(needle) || row.name.toLowerCase().includes(needle),
    );
  }, [data, query]);

  if (live.loading && !data) return <Skeleton rows={8} />;
  if (live.error && !data) return <Failure message={live.error} onRetry={live.refresh} />;
  if (!data) return null;

  return (
    <div className="grid">
      <div className="page__head">
        <div>
          <span className="eyebrow">
            {data.server.name} · {compact(data.server.members)} members
          </span>
          <h1>Economy</h1>
        </div>
        <span className="readstamp">{live.refreshing ? "reading…" : "read now"}</span>
      </div>

      <Card span={6} index={0} title="Which server" hint="Balances are per server — one member holds coins in each.">
        <div className="toolbar">
          <select
            className="input input--select"
            value={data.server.id}
            onChange={(event) => setGuildId(event.target.value)}
            aria-label="Choose a server"
          >
            {data.servers.map((server) => (
              <option key={server.id} value={server.id}>
                {server.name} · {compact(server.members)}
              </option>
            ))}
          </select>
          <Link className="btn btn--ghost" to={`/servers/${data.server.id}`}>
            Open this server
          </Link>
          <span className="toolbar__count">
            {data.totals.accounts.toLocaleString()} account
            {data.totals.accounts === 1 ? "" : "s"} with coins
          </span>
        </div>
      </Card>

      <Card span={4} index={1} title="In circulation" hint="What members hold, and what the bank has collected.">
        <div className="stats stats--tight">
          <Stat value={compact(data.totals.circulation)} label="members hold" index={0} />
          <Stat value={compact(data.totals.wallet)} label="in wallets" index={1} />
          <Stat value={compact(data.totals.bank)} label="in banks" index={2} />
          <Stat value={compact(data.totals.meow_bank)} label="Meow Bank" index={3} />
        </div>
      </Card>

      <Card span={2} index={2} title="Meow Fee" hint="Charged on a transfer, on top of what was promised.">
        <dl className="rows">
          <dt>Fee</dt>
          <dd>
            {data.fee.enabled ? (
              <Pill tone="accent">{data.fee.rate}</Pill>
            ) : (
              <Pill tone="neutral">off here</Pill>
            )}
          </dd>
          <dt>Bounds</dt>
          <dd>
            {data.fee.min} 🪙 – {data.fee.max.toLocaleString()} 🪙
          </dd>
          <dt>Banked so far</dt>
          <dd>{data.totals.meow_bank.toLocaleString()} 🪙</dd>
        </dl>
        <p className="card__hint">
          Banked, not burned: the bank's coins are still in this server, which is why they are a
          column of their own above.
        </p>
      </Card>

      <Card
        span={6}
        index={3}
        title="Richest here"
        hint={`The top ${data.show_limit} by net worth, read from the same table /balance reads.`}
        action={
          <input
            className="input"
            placeholder="Filter by name or member id…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Filter members"
          />
        }
      >
        {rows.length === 0 ? (
          <Empty
            title={data.top.length ? "Nothing matches that filter" : "No balances yet"}
            body={
              data.top.length
                ? "Clear the filter to see the whole list."
                : "Nobody in this server has earned coins yet — the first /daily creates their row."
            }
          />
        ) : (
          <div className="matrix__scroll">
            <table className="matrix">
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">Member</th>
                  <th scope="col">Wallet</th>
                  <th scope="col">Bank</th>
                  <th scope="col">Net worth</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => (
                  <motion.tr
                    key={row.user_id}
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ type: "spring", stiffness: 220, damping: 28, delay: index * 0.02 }}
                  >
                    <td className="cmds__system">{index + 1}</td>
                    <th scope="row">
                      {row.name || <span className="cmds__system">unknown name</span>}
                      <small>
                        <code>{row.user_id}</code>
                      </small>
                    </th>
                    <td>{row.wallet.toLocaleString()}</td>
                    <td>{row.bank.toLocaleString()}</td>
                    <td>
                      <strong>{row.net.toLocaleString()}</strong>
                    </td>
                  </motion.tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <AdjustPanel
        serverId={data.server.id}
        serverName={data.server.name}
        ceiling={data.ceiling}
        onDone={(text) => {
          onGone(text, "good");
          live.refresh();
        }}
        onGone={onGone}
      />
    </div>
  );
}

/** The one control on this console that moves money.
 *
 * Optimistic is wrong here, so it is not: the balances this page prints come
 * back from the server's own answer, and a refusal is shown with the number that
 * caused it. The button says what it will do, because "Apply" over a form with
 * five modes is a button nobody can predict.
 */
function AdjustPanel({
  serverId,
  serverName,
  ceiling,
  onDone,
  onGone,
}: {
  serverId: string;
  serverName: string;
  ceiling: number;
  onDone: (text: string) => void;
  onGone: (text: string, tone?: "good" | "warn" | "danger") => void;
}) {
  const [userId, setUserId] = useState("");
  const [action, setAction] = useState<EconomyAction>("wallet_add");
  const [amount, setAmount] = useState("");
  const [wallet, setWallet] = useState("");
  const [bank, setBank] = useState("");
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState<{ userId: string; wallet: number; bank: number } | null>(null);

  // A server change is a different set of balances, so the panel forgets what it
  // was about rather than re-labelling a stale reading with a new server's name.
  useEffect(() => {
    setLast(null);
  }, [serverId]);

  const isSet = action === "set";
  const wholeNumber = (raw: string): number | undefined => {
    const trimmed = raw.trim();
    if (!/^\d+$/.test(trimmed)) return undefined;
    return Number(trimmed);
  };

  const submit = async () => {
    const id = userId.trim();
    if (!/^\d{15,20}$/.test(id)) {
      onGone("That is not a member id — paste the 17-18 digit id, or use Copy ID in Discord.", "danger");
      return;
    }
    if (!isSet) {
      const value = wholeNumber(amount);
      if (value === undefined || value <= 0) {
        onGone("Enter a whole number of coins.", "danger");
        return;
      }
      if (value > ceiling) {
        onGone(`That is larger than this bot can hold — ${ceiling.toLocaleString()} is the ceiling.`, "danger");
        return;
      }
    }
    setBusy(true);
    try {
      const result = await api.adjust({
        guildId: serverId,
        userId: id,
        action,
        amount: isSet ? undefined : wholeNumber(amount),
        wallet: isSet && wallet.trim() !== "" ? wholeNumber(wallet) : undefined,
        bank: isSet && bank.trim() !== "" ? wholeNumber(bank) : undefined,
      });
      const verb = ACTIONS.find((option) => option.id === action)?.verb ?? "changed for";
      const moved = isSet
        ? `wallet ${result.wallet.toLocaleString()}, bank ${result.bank.toLocaleString()}`
        : `${amount.trim()} 🪙`;
      setLast({ userId: result.user_id, wallet: result.wallet, bank: result.bank });
      onDone(`${moved} ${verb} ${id} in ${serverName}.`);
    } catch (exc) {
      onGone(exc instanceof ApiError ? exc.message : String(exc), "danger");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      span={6}
      index={4}
      title="Adjust a balance"
      hint="Writes straight to the bot's own table. You are the operator, so there is no approval step — and every change is written to the Ops tail."
    >
      <div className="toolbar">
        <input
          className="input"
          placeholder="Member id (17-18 digits)"
          value={userId}
          onChange={(event) => setUserId(event.target.value)}
          inputMode="numeric"
          aria-label="Member id"
        />
        <div className="segmented" role="group" aria-label="Action">
          {ACTIONS.map((option) => (
            <button
              key={option.id}
              type="button"
              className={`segmented__item${action === option.id ? " is-current" : ""}`}
              aria-pressed={action === option.id}
              onClick={() => setAction(option.id)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <div className="toolbar">
        {isSet ? (
          <>
            <input
              className="input"
              placeholder="Exact wallet (blank leaves it)"
              value={wallet}
              onChange={(event) => setWallet(event.target.value)}
              inputMode="numeric"
              aria-label="Exact wallet balance"
            />
            <input
              className="input"
              placeholder="Exact bank (blank leaves it)"
              value={bank}
              onChange={(event) => setBank(event.target.value)}
              inputMode="numeric"
              aria-label="Exact bank balance"
            />
          </>
        ) : (
          <input
            className="input"
            placeholder="Coins"
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
            inputMode="numeric"
            aria-label="Amount"
          />
        )}
        <button type="button" className="btn btn--primary" disabled={busy} onClick={() => void submit()}>
          {busy ? "Saving…" : ACTIONS.find((option) => option.id === action)?.label ?? "Apply"}
        </button>
      </div>

      {last && (
        <p className="card__body">
          Last change: <code>{last.userId}</code> now holds {last.wallet.toLocaleString()} 🪙 in
          their wallet and {last.bank.toLocaleString()} 🪙 in the bank, in {serverName}.
        </p>
      )}

      <p className="card__hint">
        Removing more than a balance holds is refused rather than rounded down, and a refusal names
        the balance that refused it. The bot has no ceiling of its own; {" "}
        {ceiling.toLocaleString()} is here only to stop a mistyped paste.
      </p>
    </Card>
  );
}
