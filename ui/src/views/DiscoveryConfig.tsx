/**
 * Which features system discovered patterns are found on.
 *
 * The clustering groups members (or claim lines) on the fields checked here:
 * the patterns it finds differ by those and by nothing else. Provider features
 * come from the tables beside the denorm -- a member's primary care provider,
 * a claim's provider type and network -- and are offered beside the rest.
 */

import { useCallback, useEffect, useState } from "react";
import { Banner, Button, Chip } from "../components/primitives";
import { ApiError, api, type DiscoveryFeature, type Domain } from "../lib/api";
import { forgetFoundPatterns } from "./Patterns";
import "./DiscoveryConfig.css";
import { keyOf, linesOf, patternKeys, phrase, termsOf, words } from "../lib/pack";

/** The headings the fields are shown under. A function: the record words are
 *  the pack's, read once the screens start. */
const groups = (): ReadonlyArray<readonly [string, (f: DiscoveryFeature) => boolean]> => {
  // Joined from other records, or named like them (the pack's joined_words:
  // health's provider, network and PCP features).
  const named = phrase("joined_words");
  const joined = (f: DiscoveryFeature) =>
    !!f.joined || (named !== "" && new RegExp(named, "i").test(f.key));
  return [
    [phrase("scope_heading"), (f) => f.scope],
    [phrase("joined_heading"), (f) => !f.scope && joined(f)],
    [`${words("primary").One} and ${words("child").one}`, (f) => !f.scope && !joined(f)],
  ];
};

export function DiscoveryConfig() {
  const [features, setFeatures] = useState<Record<Domain, DiscoveryFeature[]> | null>(null);
  const [picked, setPicked] = useState<Record<Domain, Set<string>>>(
    () => Object.fromEntries(patternKeys().map((key) => [key, new Set<string>()])));
  const [busy, setBusy] = useState<Domain | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const found = await api.discoveryFeatures();
      setFeatures(found);
      setPicked(Object.fromEntries(patternKeys().map((key) => [
        key, new Set((found[key] ?? []).filter((f) => f.chosen).map((f) => f.key))])));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = (entity: Domain, key: string) =>
    setPicked((previous) => {
      const next = new Set(previous[entity]);
      next.has(key) ? next.delete(key) : next.add(key);
      return { ...previous, [entity]: next };
    });

  const save = useCallback(
    async (entity: Domain) => {
      setBusy(entity);
      setError(null);
      setNote(null);
      try {
        await api.chooseDiscoveryFeatures(entity, [...picked[entity]]);
        forgetFoundPatterns();
        await load();
        const labels = (features?.[entity] ?? [])
          .filter((f) => picked[entity].has(f.key))
          .map((f) => f.label);
        setNote(`${termsOf(entity).One} patterns are now found using: ${labels.join(", ")}.`);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [picked, features, load],
  );

  const reset = (entity: Domain) =>
    setPicked((previous) => ({
      ...previous,
      [entity]: new Set((features?.[entity] ?? []).filter((f) => f.default).map((f) => f.key)),
    }));

  return (
    <section className="panel discoveryconfig" aria-label="Pattern discovery features">
      <header>
        <h2>Fields used to find patterns</h2>
        <p className="panel__hint">
          Patterns group {words("primary").many} or {linesOf(keyOf("child"))} that share values on
          the selected fields.
        </p>
      </header>
      {error && <Banner tone="alarm">{error}</Banner>}
      {note && <Banner tone="affirm">{note}</Banner>}

      <div className="discoveryconfig__entities">
        {patternKeys().map((entity) => (
          <div key={entity} className="discoveryconfig__entity">
            <h3>{termsOf(entity).Many}</h3>
            {groups().map(([title, belongs]) => {
              const shown = (features?.[entity] ?? []).filter(belongs);
              if (!shown.length) return null;
              return (
                <fieldset key={title}>
                  <legend>{title}</legend>
                  {shown.map((feature) => (
                    <label key={feature.key} className="discoveryconfig__feature">
                      <input
                        type="checkbox"
                        checked={picked[entity].has(feature.key)}
                        onChange={() => toggle(entity, feature.key)}
                      />
                      <span>{feature.label}</span>
                      {feature.kind === "range" && (
                        <Chip fill="outline" tone="neutral">Numeric</Chip>
                      )}
                    </label>
                  ))}
                </fieldset>
              );
            })}
            <div className="panel__actions">
              <Button
                variant="primary"
                onClick={() => void save(entity)}
                busy={busy === entity}
                disabled={!picked[entity].size}
              >
                Save
              </Button>
              <Button onClick={() => reset(entity)}>Reset to defaults</Button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
