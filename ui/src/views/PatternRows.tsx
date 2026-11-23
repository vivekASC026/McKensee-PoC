/**
 * The rows a pattern was derived from.
 *
 * Everything else about a pattern is a summary: a mean age, a share of the
 * book, a band. This is the thing those summaries are about, and being able to
 * look at it is what makes the claim checkable rather than asserted. It is the
 * same argument as the evidence on a mapping — the number is only worth
 * something if you can get to what produced it.
 *
 * These are real source rows, not generated ones. On this fixture they are
 * synthetic, but the tool is pointed at production-shaped databases and the
 * server sends names, dates of birth and SSNs exactly as they are stored. So
 * the columns the server marks as PHI are covered by default and opening them
 * is a deliberate act.
 *
 * Paged from the server, because rebuilding a pattern's subset means loading
 * and clustering again. Twenty-five rows of a few thousand is the point.
 *
 * Records can be chosen here, across pages, and a file generated from just
 * those: they are saved as a pattern of their own -- their values, their
 * members -- and File Generation opens with it chosen.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Banner, Button, Chip, formatCount } from "../components/primitives";
import {
  ApiError,
  api,
  type DiscoveredPattern,
  type Domain,
  type FilePattern,
  type KeptPattern,
  type PatternFilters,
  type PatternRowsPayload,
} from "../lib/api";
import "./PatternRows.css";
import { columnName, fromPatterns, lineOf, linesOf, termsOf } from "../lib/pack";
import { GenerateFileMenu } from "../components/GenerateFileMenu";
import {
  LoadToDatabaseButton, PATTERN_AS_OF, type LoadPreview,
} from "../components/LoadToDatabase";

const PAGE = 25;

interface Props {
  domain: Domain;
  filters: PatternFilters;
  k: number | null;
  clusterId: number;
  claimType: string | null;
  // Chosen records were saved as a pattern: Generate file's More options.
  onKept?: (kept: KeptPattern) => void;
  // Chosen records were saved as a pattern, by any action on them.
  onSaved?: (kept: KeptPattern) => void;
  // Whether a load of chosen records is offered.
  previewPattern?: DiscoveredPattern;
}

export function PatternRows({
  domain, filters, k, clusterId, claimType, onKept, onSaved, previewPattern,
}: Props) {
  // Records are chosen to write a file or load them from: where the pack writes
  // nothing from a pattern, there is nothing to choose them for.
  const pickable = !!onKept && fromPatterns();
  const [page, setPage] = useState<PatternRowsPayload | null>(null);
  const [offset, setOffset] = useState(0);
  const [showPhi, setShowPhi] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Row names, kept across pages, so a choice survives paging.
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  // The chosen records, saved as a pattern: once for every action on them.
  const keptFor = useRef<{ key: string; kept: KeptPattern } | null>(null);

  const toggle = (key: string) =>
    setChosen((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const keep = async (): Promise<KeptPattern> => {
    const keys = [...chosen].sort();
    const key = JSON.stringify(keys);
    if (keptFor.current?.key === key) return keptFor.current.kept;
    const kept = await api.keepPattern({
      domain, filters, k, cluster_id: clusterId, claim_type: claimType, record_keys: keys,
    });
    keptFor.current = { key, kept };
    onSaved?.(kept);
    return kept;
  };
  const forFile = async (): Promise<FilePattern> => {
    const kept = await keep();
    return {
      kind: "saved", id: kept.pattern_id, name: kept.name, entity: kept.domain,
      members: kept.member_count,
    };
  };
  const kind = termsOf(domain).one;
  // Their values set the rules for every row, as chosen records always have.
  // Opening the dialog saves nothing: the chosen records' own profile is
  // measured (not saved) and its columns read from the plan there is; the
  // records are saved as a pattern only when Load is pressed.
  const loadPreview = previewPattern
    ? async (): Promise<LoadPreview> => {
        const { profile } = await api.patternProfile({
          domain, filters, k, cluster_id: clusterId, claim_type: claimType,
          record_keys: [...chosen].sort(),
        });
        const found = await api.toPlan(profile, kind, PATTERN_AS_OF, false, false);
        // No plan built yet: the columns are worked out when it is loaded.
        if (!found.resolution) return { columns: [], pending: true };
        return {
          columns: found.resolution.targets.map((t) => ({
            name: columnName(t.column), from: t.parameter, title: `${t.table}.${t.column}`,
          })),
        };
      }
    : undefined;
  const loadApply = async () => {
    const kept = await keep();
    return (await api.toPlan(kept.profile, kind, PATTERN_AS_OF, true, false)).applied.length;
  };

  // Each load is numbered; only the latest one's answer is shown, so a slower
  // answer for another pattern or page never replaces the one asked for since.
  const loads = useRef(0);
  const load = useCallback(
    async (at: number) => {
      const mine = ++loads.current;
      setBusy(true);
      setError(null);
      try {
        const rows = await api.patternRows(domain, filters, k, clusterId, claimType, PAGE, at);
        if (mine !== loads.current) return;
        setPage(rows);
        setOffset(at);
      } catch (e) {
        if (mine !== loads.current) return;
        // 409 means the pattern is still a valid record but what it points at
        // has moved. That is worth saying plainly rather than as a status code.
        setError(
          e instanceof ApiError && e.status === 409
            ? "Couldn't load records: Pattern data has changed since this pattern was found. Analyze again to view the current records."
            : e instanceof ApiError
              ? e.message
              : String(e),
        );
        setPage(null);
      } finally {
        if (mine === loads.current) setBusy(false);
      }
    },
    [domain, filters, k, clusterId, claimType],
  );

  // Another pattern: its own records, with nothing chosen or kept from the
  // one before.
  useEffect(() => {
    setChosen(new Set());
    keptFor.current = null;
    void load(0);
  }, [load]);

  if (error) return <Banner tone="alarm">{error}</Banner>;
  if (!page) return <p className="rows__loading">Loading records…</p>;

  const phi = new Set(page.phi_columns);
  const last = Math.min(offset + page.returned, page.total);
  const keys = page.keys ?? [];
  const pageChosen = keys.length > 0 && keys.every((key) => chosen.has(key));
  const togglePage = () =>
    setChosen((previous) => {
      const next = new Set(previous);
      for (const key of keys) {
        if (pageChosen) next.delete(key);
        else next.add(key);
      }
      return next;
    });
  const noun = lineOf(domain);

  return (
    <div className="rows">
      <div className="rows__head">
        <span className="rows__count">
          <span className="num">{formatCount(offset + 1)}</span>–
          <span className="num">{formatCount(last)}</span> of{" "}
          <span className="num">{formatCount(page.total)}</span>{" "}
          {linesOf(domain)} in this pattern
        </span>

        {page.phi_columns.length > 0 && (
          <label className="rows__phi-toggle">
            <input
              type="checkbox"
              checked={showPhi}
              onChange={(e) => setShowPhi(e.target.checked)}
            />
            <span>
              Show {page.phi_columns.length} identifier columns
            </span>
          </label>
        )}

      </div>

      {page.phi_columns.length > 0 && !showPhi && (
        <p className="rows__phi-note">
          Identifier columns are masked.
        </p>
      )}

      {pickable && (
        <div className="rows__choose">
          <span className="rows__chosen">
            {chosen.size === 0
              ? `Select ${noun}s to generate from those records only.`
              : `${formatCount(chosen.size)} ${noun}${chosen.size === 1 ? "" : "s"} selected`}
          </span>
          {chosen.size > 0 && (
            <>
              <GenerateFileMenu
                domain={domain}
                prepare={forFile}
                onMore={async () => onKept?.(await keep())}
              />
              <LoadToDatabaseButton
                name={`${formatCount(chosen.size)} selected ${noun}${chosen.size === 1 ? "" : "s"}`}
                preview={loadPreview}
                apply={loadApply}
              />
              <Button onClick={() => setChosen(new Set())}>
                Clear
              </Button>
            </>
          )}
        </div>
      )}

      <div className="rows__scroll">
        <table className="rows__table">
          <thead>
            <tr>
              {pickable && (
                <th className="rows__pick">
                  <input
                    type="checkbox"
                    checked={pageChosen}
                    onChange={togglePage}
                    aria-label="Select all records on this page"
                  />
                </th>
              )}
              {page.columns.map((column) => (
                <th key={column} className={phi.has(column) ? "rows__th--phi" : ""} title={column}>
                  {columnName(column)}
                  {phi.has(column) && (
                    <Chip tone="caution" fill="outline">
                      ID
                    </Chip>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {page.rows.map((row, index) => (
              <tr
                key={keys[index] ?? offset + index}
                className={keys[index] && chosen.has(keys[index]) ? "rows__tr--chosen" : undefined}
              >
                {pickable && (
                  <td className="rows__pick">
                    {keys[index] && (
                      <input
                        type="checkbox"
                        checked={chosen.has(keys[index])}
                        onChange={() => toggle(keys[index])}
                        aria-label={`Select ${noun} ${keys[index]}`}
                      />
                    )}
                  </td>
                )}
                {page.columns.map((column) => {
                  const hidden = phi.has(column) && !showPhi;
                  const value = row[column];
                  return (
                    <td
                      key={column}
                      className={hidden ? "rows__cell--hidden" : undefined}
                    >
                      {hidden
                        ? "••••"
                        : value === null || value === undefined
                          ? "—"
                          : typeof value === "number" && !Number.isInteger(value)
                            ? String(Math.round(value * 100) / 100)
                            : String(value)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="rows__pager">
        <Button onClick={() => load(Math.max(0, offset - PAGE))} disabled={offset === 0 || busy}>
          Previous
        </Button>
        <Button onClick={() => load(offset + PAGE)} disabled={!page.has_more || busy}>
          Next
        </Button>
        {page.truncated && (
          <span className="rows__truncated">
            Only part of the pattern is shown.
          </span>
        )}
      </div>
    </div>
  );
}
