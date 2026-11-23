/**
 * A pattern's scenarios, and generating either every one of them or only the
 * ones chosen.
 *
 * One pattern is usually several test scenarios: the same kind of member on
 * different plans, or of each gender and age band; the same kind of claim for
 * each diagnosis and age band. The pattern's own rows say which combinations of
 * the chosen features occur in it and how often.
 *
 * Every scenario is the pattern itself: its rows hold each scenario at its real
 * share, the rare ones the list does not show included, so it is generated as
 * one. Chosen scenarios go into one output -- one database load, each a cohort
 * at its share of the volume, or one file holding every scenario's records,
 * each tagged with its scenario.
 *
 * What the pattern is split on is the analysis's one choice: the counts and
 * every download follow it.
 */

import { useCallback, useEffect, useState } from "react";
import { Banner, Button, Empty, formatCount, formatPercent } from "../components/primitives";
import {
  ApiError,
  api,
  type Domain,
  type FilePattern,
  type PatternFilters,
  type ScenarioList,
} from "../lib/api";
import { GenerateFileMenu } from "../components/GenerateFileMenu";
import { LoadToDatabaseButton } from "../components/LoadToDatabase";
import "./Scenarios.css";
import { fromPatterns, termsOf } from "../lib/pack";

interface Props {
  domain: Domain;
  name: string;
  filters: PatternFilters;
  k: number | null;
  clusterId: number;
  claimType: string | null;
  // The features the analysis's patterns are split on, and changing them.
  split: string[] | null;
  onSplit: (keys: string[]) => void;
  onApplied: (destination: "file" | "db", pattern?: FilePattern, note?: string) => void;
  // A saved pattern, split from File Generation: its rows are read by its id,
  // and only a file is offered there.
  patternId?: number | null;
  fileOnly?: boolean;
}

type Which = "every" | "chosen";

export function Scenarios({
  domain, name, filters, k, clusterId, claimType, split, onSplit, onApplied,
  patternId = null, fileOnly = false,
}: Props) {
  const [dimensions, setDimensions] = useState<{ key: string; label: string }[]>([]);
  const keys = split ?? [];
  const [list, setList] = useState<ScenarioList | null>(null);
  const [which, setWhich] = useState<Which>("every");
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [volume, setVolume] = useState(1000);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .scenarioDimensions(domain)
      .then((d) => {
        if (!live) return;
        setDimensions(d.dimensions);
        // No split chosen yet: the usual one for this kind of pattern.
        if (split === null) onSplit(d.defaults);
      })
      .catch((e) => live && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [domain]);

  // A list made on other features is not this split's list.
  const splitKey = JSON.stringify(keys);
  useEffect(() => {
    setList(null);
    setPicked(new Set());
  }, [splitKey]);

  const address = {
    domain, filters, k, cluster_id: clusterId, claim_type: claimType, pattern_id: patternId,
  };

  const load = useCallback(async () => {
    setBusy("list");
    setError(null);
    try {
      const found = await api.scenarios({ ...address, dimensions: keys });
      setList(found);
      setPicked(new Set());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [splitKey, domain, filters, k, clusterId, claimType, patternId]);

  const every = which === "every";
  const chosen = list ? list.scenarios.filter((_, i) => picked.has(i)) : [];
  const chosenRows = chosen.reduce((n, s) => n + s.rows, 0) || 1;
  // Each scenario's share of the volume: its share of the chosen rows.
  const counts = chosen.map((s) => Math.max(1, Math.round((volume * s.rows) / chosenRows)));
  const allCount = list ? list.scenarios.length + list.more : 0;
  const ready = every ? allCount > 0 : chosen.length > 0;

  const keep = useCallback(async () => {
    const kept = await api.keepScenarios({
      ...address, dimensions: keys, name,
      scenarios: chosen.map((s) => s.values),
    });
    return kept.kept;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [splitKey, chosen, name, domain, filters, k, clusterId, claimType, patternId]);

  // Every scenario: the pattern itself, whose rows hold each at its share.
  // A saved pattern is already kept.
  const keepWhole = useCallback(
    () => patternId !== null
      ? Promise.resolve({ pattern_id: patternId })
      : api.keepPattern({ ...address, name }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [name, domain, filters, k, clusterId, claimType, patternId],
  );

  // Into the plan: every scenario as the pattern itself, or the chosen ones
  // as segments of one load. How many column rules that set.
  const toPlan = async () => {
    if (every) {
      const whole = await keepWhole();
      return (await api.storyToPlan([whole.pattern_id], volume, true)).applied?.length ?? 0;
    }
    const kept = await keep();
    const payload = await api.storyToPlan(kept.map((p) => p.pattern_id), volume, true);
    return payload.applied?.length ?? 0;
  };

  // What a file of these scenarios is written for: the pattern itself, whose
  // rows hold every scenario at its share, or the chosen ones each counted.
  const target = async (): Promise<FilePattern> => {
    if (every) {
      const whole = await keepWhole();
      return {
        kind: "saved",
        id: whole.pattern_id,
        name: `All scenarios of ${name}`,
        entity: domain,
        members: volume,
      };
    }
    const kept = await keep();
    return {
      kind: "scenarios",
      name: `${kept.length} scenarios of ${name}`,
      entity: domain,
      scenarios: kept.map((p, i) => ({
        pattern_id: p.pattern_id, label: p.label, count: counts[i] ?? 1,
      })),
      members: volume,
    };
  };

  // Generate › Files with them chosen: More options, or File Generation's own split.
  const toFile = async () => {
    setBusy("file");
    setError(null);
    try {
      onApplied("file", await target());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const howMany = every ? `all ${formatCount(allCount)}` : `${chosen.length}`;

  return (
    <section className="scenarios" aria-label="Scenarios">
      <h4>Scenarios</h4>
      {error && <Banner tone="alarm">{error}</Banner>}
      <div className="scenarios__dims" role="group" aria-label="Split by">
        <span className="panel__hint">Split by</span>
        {dimensions.map((d) => (
          <label key={d.key} className="scenarios__dim">
            <input
              type="checkbox"
              checked={keys.includes(d.key)}
              onChange={() =>
                onSplit(
                  keys.includes(d.key) ? keys.filter((key) => key !== d.key) : [...keys, d.key],
                )
              }
            />
            {d.label}
          </label>
        ))}
        <Button onClick={load} busy={busy === "list"} disabled={!keys.length}>
          List scenarios
        </Button>
      </div>
      <p className="panel__hint">
        Counts and downloads for all patterns in this analysis use the same split.
      </p>

      {list &&
        (list.scenarios.length === 0 ? (
          <Empty title="No scenarios">No records in this pattern have values for the selected fields.</Empty>
        ) : (
          <>
            {/* Scenarios are chosen to write or load them: where the pack
                writes nothing from a pattern, the list is counts alone. */}
            {fromPatterns() && (
            <div className="scenarios__which" role="radiogroup" aria-label="Which scenarios">
              <label>
                <input
                  type="radio"
                  name={`which-${clusterId}`}
                  checked={every}
                  onChange={() => setWhich("every")}
                />
                {`All scenarios (${formatCount(allCount)})`}
              </label>
              <label>
                <input
                  type="radio"
                  name={`which-${clusterId}`}
                  checked={!every}
                  onChange={() => {
                    setWhich("chosen");
                    setPicked(new Set());
                  }}
                />
                Selected scenarios only
              </label>
            </div>
            )}
            <table className="panel__table scenarios__table">
              <thead>
                <tr>
                  {fromPatterns() && (
                  <th>
                    <input
                      type="checkbox"
                      aria-label="Select all scenarios"
                      disabled={every}
                      checked={every || picked.size === list.scenarios.length}
                      onChange={() =>
                        setPicked(
                          picked.size === list.scenarios.length
                            ? new Set()
                            : new Set(list.scenarios.map((_, i) => i)),
                        )
                      }
                    />
                  </th>
                  )}
                  <th>Scenario</th>
                  <th className="right">Rows</th>
                  <th className="right">Share</th>
                </tr>
              </thead>
              <tbody>
                {list.scenarios.map((scenario, index) => (
                  <tr key={scenario.label}>
                    {fromPatterns() && (
                    <td>
                      <input
                        type="checkbox"
                        aria-label={scenario.label}
                        disabled={every}
                        checked={every || picked.has(index)}
                        onChange={() =>
                          setPicked((previous) => {
                            const next = new Set(previous);
                            if (next.has(index)) next.delete(index);
                            else next.add(index);
                            return next;
                          })
                        }
                      />
                    </td>
                    )}
                    <td>{scenario.label}</td>
                    <td className="right num">{formatCount(scenario.rows)}</td>
                    <td className="right num">{formatPercent(scenario.share, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {list.more > 0 && (
              <p className="panel__hint">
                {every
                  ? `${formatCount(list.more)} less frequent scenarios are not listed but are included.`
                  : `${formatCount(list.more)} less frequent scenarios are not listed.`}
              </p>
            )}
            {fromPatterns() && (
            <div className="panel__actions">
              <label className="named">
                <span>Total {termsOf(domain).many}</span>
                <input
                  type="number"
                  min={every ? 1 : chosen.length || 1}
                  value={volume}
                  onChange={(e) => setVolume(Math.max(1, Number(e.target.value) || 1))}
                />
              </label>
              {fileOnly ? (
                <Button
                  variant="primary"
                  onClick={toFile}
                  busy={busy === "file"}
                  disabled={!ready || busy !== null}
                >
                  {`Generate ${howMany} as one file`}
                </Button>
              ) : (
                <>
                  <GenerateFileMenu
                    domain={domain}
                    label={`Generate ${howMany} as one file`}
                    prepare={target}
                    onMore={toFile}
                    count={volume}
                    disabled={!ready || busy !== null}
                  />
                  <LoadToDatabaseButton
                    label={`Load ${howMany} to database`}
                    name={`${every ? "All" : chosen.length} scenarios of ${name}`}
                    amount={`${formatCount(volume)} ${termsOf(domain).many}`}
                    apply={toPlan}
                    disabled={!ready || busy !== null}
                  />
                </>
              )}
              <span className="panel__hint">
                {every
                  ? "Each scenario is generated at its actual share of the total."
                  : "Each selected scenario is generated in proportion to its share of the selected rows."}
              </span>
            </div>
            )}
          </>
        ))}
    </section>
  );
}
