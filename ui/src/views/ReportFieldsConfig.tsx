/**
 * Which fields a pattern's counts are made on.
 *
 * Counts and % on a pattern card, the one-sheet download and the saved
 * patterns' export count every value of the fields chosen here. They start
 * from the fields every pattern is described by; any can be switched off, and
 * any column of the denorm table can be added -- it is then counted like the
 * rest, a date by its year and a number with many values in ten bands.
 * Columns with personal data are not offered.
 */

import { useCallback, useEffect, useState } from "react";
import { Banner, Button, Chip } from "../components/primitives";
import { ApiError, api, type Domain, type ReportField } from "../lib/api";
import "./DiscoveryConfig.css";
import { columnName, isChild, patternKeys, termsOf } from "../lib/pack";

export function ReportFieldsConfig() {
  const [fields, setFields] = useState<Record<Domain, ReportField[]> | null>(null);
  const [picked, setPicked] = useState<Record<Domain, string[]>>(
    () => Object.fromEntries(patternKeys().map((key) => [key, []])));
  const [adding, setAdding] = useState<Record<Domain, string>>(
    () => Object.fromEntries(patternKeys().map((key) => [key, ""])));
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const take = (found: Record<Domain, ReportField[]>) => {
    setFields(found);
    setPicked(Object.fromEntries(patternKeys().map((key) => [
      key, (found[key] ?? []).filter((f) => f.chosen).map((f) => f.key)])));
  };

  const load = useCallback(async () => {
    try {
      take(await api.reportFields());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = (entity: Domain, key: string) =>
    setPicked((previous) => {
      const now = previous[entity];
      return {
        ...previous,
        [entity]: now.includes(key) ? now.filter((k) => k !== key) : [...now, key],
      };
    });

  const add = (entity: Domain) => {
    const key = adding[entity];
    if (!key) return;
    setPicked((previous) =>
      previous[entity].includes(key)
        ? previous
        : { ...previous, [entity]: [...previous[entity], key] });
    setAdding((previous) => ({ ...previous, [entity]: "" }));
  };

  const save = async (entity: Domain, reset = false) => {
    setBusy(entity + (reset ? ":reset" : ""));
    setError(null);
    setNote(null);
    try {
      await api.chooseReportFields(entity, reset ? [] : picked[entity], reset);
      await load();
      setNote(
        reset
          ? `${termsOf(entity).One} patterns are counted on the default fields again.`
          : `${termsOf(entity).One} patterns are now counted on ${picked[entity].length} fields.`,
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="panel discoveryconfig" aria-label="Fields counted in patterns">
      <header>
        <h2>Fields counted in patterns</h2>
        <p className="panel__hint">
          Added columns are reported only and are not used in generation.
        </p>
      </header>
      {error && <Banner tone="alarm">{error}</Banner>}
      {note && <Banner tone="affirm">{note}</Banner>}

      <div className="discoveryconfig__entities">
        {patternKeys().map((entity) => {
          const all = fields?.[entity] ?? [];
          const builtins = all.filter((f) => f.builtin);
          const added = picked[entity].filter((k) => !builtins.some((f) => f.key === k));
          const addable = all.filter((f) => !f.builtin && !picked[entity].includes(f.key));
          return (
            <div key={entity} className="discoveryconfig__entity">
              <h3>{termsOf(entity).Many}</h3>
              <fieldset>
                <legend>Default fields</legend>
                {builtins.map((field) => (
                  <label key={field.key} className="discoveryconfig__feature">
                    <input
                      type="checkbox"
                      checked={picked[entity].includes(field.key)}
                      onChange={() => toggle(entity, field.key)}
                    />
                    <span>{field.label}</span>
                  </label>
                ))}
              </fieldset>
              <fieldset>
                <legend>Added columns</legend>
                {added.length === 0 && <span className="panel__hint">None added.</span>}
                {added.map((key) => (
                  <label key={key} className="discoveryconfig__feature">
                    <input type="checkbox" checked onChange={() => toggle(entity, key)} />
                    <span>{all.find((f) => f.key === key)?.label ?? columnName(key.replace(/^col:/, ""))}</span>
                    <Chip fill="outline" tone="neutral">Report only</Chip>
                  </label>
                ))}
                <div className="discoveryconfig__feature">
                  <select
                    aria-label={`Add ${isChild(entity) ? termsOf(entity).many : termsOf(entity).one} pattern data column`}
                    value={adding[entity]}
                    onChange={(e) =>
                      setAdding((previous) => ({ ...previous, [entity]: e.target.value }))}
                  >
                    <option value="">Select a column…</option>
                    {addable.map((f) => (
                      <option key={f.key} value={f.key}>{f.label}</option>
                    ))}
                  </select>
                  <Button onClick={() => add(entity)} disabled={!adding[entity]}>
                    Add
                  </Button>
                </div>
              </fieldset>
              <div className="panel__actions">
                <Button
                  variant="primary"
                  onClick={() => void save(entity)}
                  busy={busy === entity}
                  disabled={!picked[entity].length}
                >
                  Save
                </Button>
                <Button onClick={() => void save(entity, true)} busy={busy === entity + ":reset"}>
                  Reset to defaults
                </Button>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
