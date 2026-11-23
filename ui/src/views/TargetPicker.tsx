/**
 * Where generated rows go.
 *
 * Until this existed, pointing the tool at another database meant editing
 * `.env` and restarting it. Everything underneath already worked — a database
 * the tool has never seen can be discovered, profiled, planned against and
 * written to — but there was no way to say which one without touching a file.
 *
 * The databases are listed rather than typed. The server already knows their
 * names, and a typo in a database name fails three steps later with an error
 * about something else.
 *
 * A target on another server is configured here too. Windows authentication
 * is offered first because it is the common case on a domain-joined machine
 * and the one where nothing has to store a password. Where a password is
 * needed it is written to the working directory, which is gitignored and never
 * packaged — said plainly on the form rather than left to be discovered.
 *
 * The analysis store is not in the list. It holds the extracts, the saved
 * patterns and the baselines this tool reads, and generating into it would put
 * synthetic rows beside the measurements. Leaving it out beats showing it with
 * a warning nobody reads.
 */

import { useCallback, useEffect, useState } from "react";
import { Banner, Button } from "../components/primitives";
import {
  ApiError,
  api,
  type DatabaseRow,
  type OnboardedDatabase,
  type StoredTarget,
} from "../lib/api";
import "./TargetPicker.css";

interface Props {
  onChanged: () => void;
}

export function TargetPicker({ onChanged }: Props) {
  const [rows, setRows] = useState<DatabaseRow[]>([]);
  const [server, setServer] = useState("");
  const [target, setTarget] = useState("");
  const [chosen, setChosen] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  // A target somewhere else entirely.
  const [elsewhere, setElsewhere] = useState(false);
  const [stored, setStored] = useState<StoredTarget | null>(null);
  const [onboarded, setOnboarded] = useState<OnboardedDatabase[]>([]);
  // Offboarding asks once on the page before it acts.
  const [confirming, setConfirming] = useState<string | null>(null);
  const [form, setForm] = useState({
    server: "",
    database: "",
    trusted: true,
    user: "",
    password: "",
  });

  const load = useCallback(async () => {
    try {
      const [payload, configured, known] = await Promise.all([
        api.databases(),
        api.readTarget(),
        api.onboarded(),
      ]);
      setOnboarded(known.databases);
      setRows(payload.databases);
      setServer(payload.server);
      setTarget(payload.target);
      setChosen(payload.target);
      setStored(configured);
      if (configured.configured) {
        setElsewhere(true);
        setForm((previous) => ({
          ...previous,
          server: configured.server ?? "",
          database: configured.database ?? "",
          trusted: configured.trusted ?? true,
          user: configured.user ?? "",
        }));
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const choose = useCallback(async () => {
    setBusy("choose");
    setError(null);
    setNote(null);
    try {
      const result = await api.setTarget(chosen);
      setNote(
        `Target set to ${result.target} on ${result.server ?? server}. ` +
          `Run discovery to update the plan.`,
      );
      await load();
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [chosen, server, load, onChanged]);

  const connect = useCallback(
    async (save: boolean) => {
      setBusy(save ? "save" : "test");
      setError(null);
      setNote(null);
      try {
        const config = {
          database: form.database.trim(),
          server: form.server.trim() || undefined,
          trusted: form.trusted,
          user: form.trusted ? undefined : form.user.trim(),
          password: form.trusted ? undefined : form.password,
        };
        if (!save) {
          const reached = await api.testTarget(config);
          setNote(`Connected to ${reached.db} on ${reached.server}.`);
          return;
        }
        const result = await api.saveTarget(config);
        setNote(
          `Target set to ${result.target} on ${result.server}. ` +
            `Run discovery to update the plan.`,
        );
        setForm((previous) => ({ ...previous, password: "" }));
        await load();
        onChanged();
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [form, load, onChanged],
  );

  const forget = useCallback(async () => {
    setBusy("forget");
    setError(null);
    try {
      const result = await api.forgetTarget();
      setNote(`Connection deleted. Target restored to ${result.target} on ${result.server}.`);
      setElsewhere(false);
      setForm((previous) => ({ ...previous, password: "" }));
      await load();
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [load, onChanged]);

  const offboard = useCallback(
    async (db: OnboardedDatabase) => {
      setBusy(`offboard-${db.key}`);
      setError(null);
      setNote(null);
      try {
        const result = await api.offboard(db.key);
        setNote(
          `${db.database} deleted from SDM. Patterns are not affected. Target: ` +
            `${result.workingOn.database}.`,
        );
        setConfirming(null);
        await load();
        onChanged();
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [load, onChanged],
  );

  const picked = rows.find((row) => row.name === chosen);
  const changing = chosen !== target;

  return (
    <section className="target">
      <div className="target__head">
        <h3>Target database</h3>
        <p className="target__lede">
          Discovery, the plan and loaded rows apply to the selected database.
          Patterns stay in Pattern data.
        </p>
      </div>

      {error && <Banner tone="alarm">{error}</Banner>}
      {note && <Banner tone="affirm">{note}</Banner>}

      <div className="target__modes" role="group" aria-label="Target location">
        {([[false, server ? `Local server (${server})` : "Local server"], [true, "Remote server"]] as const).map(
          ([mode, label]) => (
            <button
              key={String(mode)}
              className={"target__mode" + (elsewhere === mode ? " target__mode--on" : "")}
              onClick={() => setElsewhere(mode)}
            >
              {label}
            </button>
          ),
        )}
      </div>

      {!elsewhere ? (
        <>
          <div className="target__row">
            <label className="named">
              <span>Database</span>
              <select
                value={chosen}
                onChange={(e) => setChosen(e.target.value)}
                disabled={busy !== null || rows.length === 0}
              >
                {rows.map((row) => (
                  <option key={row.name} value={row.name}
                    disabled={!row.online || row.protected}>
                    {row.name}
                    {row.isSource ? " (Pattern data)" : ""}
                    {row.protected ? " (production, protected)" : ""}
                    {row.online ? "" : " (offline)"}
                  </option>
                ))}
              </select>
            </label>

            <Button
              variant={changing ? "primary" : undefined}
              onClick={choose}
              busy={busy === "choose"}
              disabled={!changing || !picked?.online}
            >
              {changing ? "Use database" : "In use"}
            </Button>
          </div>

          {changing && picked && (
            <p className="target__hint">
              {picked.isSource
                ? "Holds Pattern data. Loaded rows are written beside its rows; Delete removes only the loaded rows."
                : "Work on the current database is kept and restored if you switch back."}
            </p>
          )}
        </>
      ) : (
        <>
          <div className="target__form">
            <label className="named">
              <span>Server</span>
              <input
                value={form.server}
                onChange={(e) => setForm({ ...form, server: e.target.value })}
                placeholder={server || "host\instance"}
              />
            </label>
            <label className="named">
              <span>Database</span>
              <input
                value={form.database}
                onChange={(e) => setForm({ ...form, database: e.target.value })}
                placeholder="Target database name"
              />
            </label>
          </div>

          <div className="target__auth">
            <label className="target__check">
              <input
                type="checkbox"
                checked={form.trusted}
                onChange={(e) => setForm({ ...form, trusted: e.target.checked })}
              />
              <span>Windows authentication</span>
            </label>
            {!form.trusted && (
              <div className="target__form">
                <label className="named">
                  <span>User</span>
                  <input
                    value={form.user}
                    onChange={(e) => setForm({ ...form, user: e.target.value })}
                    autoComplete="off"
                  />
                </label>
                <label className="named">
                  <span>Password</span>
                  <input
                    type="password"
                    value={form.password}
                    onChange={(e) => setForm({ ...form, password: e.target.value })}
                    autoComplete="new-password"
                  />
                </label>
              </div>
            )}
          </div>

          <p className="target__hint">
            {form.trusted
              ? "Uses the Windows account running SDM. No credentials are stored."
              : "The password is stored in plain text on this machine. Use Windows authentication to avoid storing it."}
          </p>

          <div className="target__row">
            <Button
              onClick={() => connect(false)}
              busy={busy === "test"}
              disabled={!form.database.trim()}
            >
              Test connection
            </Button>
            <Button
              variant="primary"
              onClick={() => connect(true)}
              busy={busy === "save"}
              disabled={!form.database.trim()}
            >
              Use connection
            </Button>
            {stored?.configured && (
              <Button onClick={forget} busy={busy === "forget"}>
                Forget connection
              </Button>
            )}
          </div>

          {stored?.configured && (
            <p className="target__hint">
              Current target:{" "}
              <strong className="mono">{stored.database}</strong> on{" "}
              <strong className="mono">{stored.server}</strong>
              {stored.trusted ? ", using Windows authentication." : "."}
            </p>
          )}
        </>
      )}

      {onboarded.length > 0 && (
        <div className="target__onboarded">
          <h4>Onboarded databases</h4>
          <p className="target__hint">
            Deleting removes a database&apos;s schema, profile, mappings, decisions and
            plan from SDM. The database itself and patterns are not affected.
          </p>
          <ul>
            {onboarded.map((db) => (
              <li key={db.key} className="target__onboarded-row">
                <span className="mono">{db.database}</span>
                <span className="target__onboarded-meta">
                  {db.server}
                  {db.current ? " · current target" : ""}
                  {db.planned ? " · plan built" : ""}
                  {db.rowsInIt ? " · contains generated rows" : ""}
                </span>
                {confirming === db.key ? (
                  <>
                    <Button
                      variant="primary"
                      onClick={() => offboard(db)}
                      busy={busy === `offboard-${db.key}`}
                    >
                      Delete {db.database} from SDM
                    </Button>
                    <Button onClick={() => setConfirming(null)} disabled={busy !== null}>
                      Cancel
                    </Button>
                  </>
                ) : (
                  <Button
                    onClick={() => setConfirming(db.key)}
                    disabled={busy !== null || db.rowsInIt}
                    title={
                      db.rowsInIt
                        ? "Delete the loaded rows first (Generate › Database › Delete loaded rows)."
                        : undefined
                    }
                  >
                    Delete
                  </Button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
