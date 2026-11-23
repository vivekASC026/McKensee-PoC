/**
 * Full book scan, and the saved patterns: two parts of the Find screen.
 *
 * The scan walks the hierarchy itself and ranks what it finds by how much of
 * the book each cohort covers -- "what is in this book at all", which can
 * surface something nobody thought to look for. A scan already cached shows
 * at once; the first walk at a depth is slow, so it waits to be asked for.
 *
 * Saved patterns come from every route (found, criteria, scan, requirement),
 * so they are listed on their own beneath the Find screen's results.
 */

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import {
  Banner,
  Button,
  Chip,
  Empty,
  Share,
  formatCount,
} from "../components/primitives";
import {
  ApiError,
  api,
  DOWNLOAD_FORMATS,
  type DiscoveredRow,
  type DownloadFormat,
  type DiscoveryPayload,
  type Domain,
  type FilePattern,
  type SavedPattern,
} from "../lib/api";
import { Scenarios } from "./Scenarios";
import { GenerateFileMenu } from "../components/GenerateFileMenu";
import { LoadToDatabaseButton } from "../components/LoadToDatabase";
import "./Discover.css";
import { fromPatterns, isChild, isPrimary, patternKey, termsOf } from "../lib/pack";

type OnApplied = (destination: "file" | "db", pattern?: FilePattern, note?: string) => void;

/** The pattern whose scenarios are open. */
interface Splitting {
  // The row it opened under: a scanned pattern can be listed in more than one.
  row: string;
  id: number;
  name: string;
  entity: Domain;
}

const subject = (entity: string): Domain => patternKey(entity);

/** A scanned pattern's row: the same pattern can be found in more than one. */
const scanKey = (row: { pattern_id: number | null; cluster_id: number | null; name: string }) =>
  `${row.pattern_id}-${row.cluster_id}-${row.name}`;

/** A saved pattern's scenarios, "Generate N as one file" among them. */
function ScenarioPanel({
  splitting, onClose, onApplied,
}: {
  splitting: Splitting;
  onClose: () => void;
  onApplied: OnApplied;
}) {
  const [split, setSplit] = useState<string[] | null>(null);
  // Opened under the pattern's own row, and brought into view: a panel that
  // opens out of sight looks like a button that does nothing.
  const panel = useRef<HTMLElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    panel.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    heading.current?.focus({ preventScroll: true });
  }, [splitting.id]);
  return (
    <section ref={panel} className="discover__scenarios"
      aria-label={`Scenarios of ${splitting.name}`}>
      <div className="kept__head">
        <h3 ref={heading} tabIndex={-1}>{splitting.name}</h3>
        <Button onClick={onClose}>Close scenarios</Button>
      </div>
      <Scenarios
        key={splitting.id}
        domain={splitting.entity}
        name={splitting.name}
        filters={{}}
        k={null}
        clusterId={0}
        claimType={null}
        split={split}
        onSplit={setSplit}
        patternId={splitting.id}
        onApplied={onApplied}
      />
    </section>
  );
}

/** Whether a row holds the search text in its name or where it sits. */
function matches(query: string, ...words: (string | null | undefined)[]): boolean {
  const wanted = query.trim().toLowerCase();
  return !wanted || words.join(" ").toLowerCase().includes(wanted);
}

export function Discover({
  domain, onApplied, query = "", onSaved,
}: {
  domain: Domain;
  onApplied?: OnApplied;
  /** The Find screen's search text: rows shown hold it. */
  query?: string;
  /** A row was saved here. */
  onSaved?: () => void;
}) {
  const [splitting, setSplitting] = useState<Splitting | null>(null);
  const [depth, setDepth] = useState(1);
  // Each record kind is walked as deep as its scope goes: a depth past it
  // (a bank's customers have one level) comes back to the deepest there is.
  const deepest = (termsOf(domain).depths ?? [""]).length;
  useEffect(() => {
    setDepth((d) => Math.min(d, deepest));
  }, [deepest]);
  const [found, setFound] = useState<DiscoveryPayload | null>(null);
  const [saved, setSaved] = useState<SavedPattern[]>([]);
  // A scan runs on its own flag: saving a row while a scan runs must not
  // clear the scan's state.
  const [scanning, setScanning] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadSaved = useCallback(async () => {
    try {
      setSaved((await api.saved()).patterns);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, []);
  useEffect(() => {
    void loadSaved();
  }, [loadSaved]);

  // Each scan is numbered: an answer to an earlier one (another domain or
  // depth) is dropped, never shown under the one chosen since.
  const walks = useRef(0);
  const walk = useCallback(async () => {
    const mine = ++walks.current;
    setScanning(true);
    setError(null);
    try {
      const scanned = await api.discovery(domain, depth);
      if (mine === walks.current) setFound(scanned);
    } catch (e) {
      if (mine !== walks.current) return;
      setError(e instanceof ApiError ? e.message : String(e));
      setFound(null);
    } finally {
      if (mine === walks.current) setScanning(false);
    }
  }, [domain, depth]);

  // A scan cached at this depth shows at once; a first scan waits to be run.
  useEffect(() => {
    let live = true;
    // A scan still running for the domain or depth before is dropped.
    walks.current += 1;
    setScanning(false);
    setFound(null);
    setError(null);
    api
      .discoveryStatus(domain, depth)
      .then((status) => {
        if (live && status.cached) void walk();
      })
      .catch((e: unknown) => {
        if (!live) return;
        setError(
          `Couldn't check for an earlier scan: ${e instanceof ApiError ? e.message : String(e)} ` +
            "Run the scan to see patterns.",
        );
      });
    return () => {
      live = false;
    };
  }, [domain, depth, walk]);

  const keep = useCallback(
    async (row: DiscoveredRow) => {
      setBusy(`keep-${row.pattern_id}`);
      setError(null);
      try {
        // The walk stored every pattern it found; keeping one marks that row
        // saved. Saving through the member analyzer instead turned a kept
        // claims pattern into a member pattern of the same scope.
        if (row.pattern_id != null) {
          await api.bookmark(row.pattern_id);
        } else if (isPrimary(domain)) {
          await api.savePattern(row.combo, null, row.cluster_id);
        } else {
          throw new Error(
            `Couldn't save this ${termsOf(domain).many} pattern: the scan didn't store it. Run the scan again.`,
          );
        }
        await loadSaved();
        onSaved?.();
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [loadSaved, domain, onSaved],
  );

  // A file is written for a saved pattern: a scan's row is saved first, in
  // the same click, as a card's Generate file saves its pattern.
  const forFile = async (row: DiscoveredRow, kept: boolean): Promise<FilePattern> => {
    if (!kept) {
      await api.bookmark(row.pattern_id);
      void loadSaved();
      onSaved?.();
    }
    // As Written for lists it: its saved name and the members it holds (a
    // claims row's size counts claims).
    const listed = (await api.patternsForFiles()).patterns
      .find((p) => p.pattern_id === row.pattern_id);
    return {
      kind: "saved", id: row.pattern_id, name: listed?.name ?? row.name,
      entity: listed?.domain ?? row.entity, members: listed?.member_count ?? row.size,
    };
  };

  // By id first: a claims pattern is stored as "[Claims] <name>", so its name
  // never matched the row's. The name still catches a member pattern saved
  // through the analyzer before the walk stored it.
  const keptIds = new Set(saved.map((s) => s.pattern_id));
  const keptNames = new Set(saved.map((s) => s.name));
  const rows = (found?.patterns ?? []).filter((row) =>
    matches(query, row.name, ...(row.scope ?? [])),
  );
  // Scenarios open under a row the search has since hidden: closed, so they
  // do not come back on their own when the search is cleared.
  const shownKeys = rows.map(scanKey).join("|");
  useEffect(() => {
    if (splitting && !rows.some((row) => scanKey(row) === splitting.row)) setSplitting(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKeys]);

  return (
    <div className="discover">
      <section className="discover__controls" aria-label="Full book scan">
        <label>
          <span>Depth</span>
          <select
            value={depth}
            onChange={(e) => setDepth(Number(e.target.value))}
            disabled={scanning}
          >
            {(termsOf(domain).depths ?? [termsOf(domain).Many]).map((said, at) => (
              <option key={said} value={at + 1}>
                {said}
              </option>
            ))}
          </select>
        </label>

        {(found || scanning) && (
          <Button onClick={walk} busy={scanning}>
            {found ? "Rerun scan" : "Run scan"}
          </Button>
        )}
        {scanning && !found && (
          <span className="discover__warning" role="status">
            A first scan at a depth takes several minutes.
          </span>
        )}
      </section>

      {error && <Banner tone="alarm">{error}</Banner>}

      {found && (
        <>
          <p className="discover__summary">
            <strong className="num">{found.n_discovered}</strong> patterns across{" "}
            <strong className="num">{found.combinations_processed}</strong>{" "}
            combinations, out of{" "}
            <strong className="num">{formatCount(found.total_population)}</strong>{" "}
            {termsOf(domain).many}.
          </p>

          <div className="discover__scroll">
          <table className="discover__table">
            <thead>
              <tr>
                <th>Pattern</th>
                <th>Scope</th>
                <th className="right">Size</th>
                <th>Share of book</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const kept = keptIds.has(row.pattern_id) || keptNames.has(row.name);
                const rowKey = scanKey(row);
                const open = splitting?.row === rowKey;
                return (
                  <Fragment key={rowKey}>
                  <tr>
                    <td className="discover__name">{row.name}</td>
                    <td className="discover__where">
                      {(row.scope ?? []).join(" › ") || "Entire book"}
                    </td>
                    <td className="right num">{formatCount(row.size)}</td>
                    <td>
                      <Share value={row.pct_of_population} />
                    </td>
                    <td className="right discover__actions">
                      {onApplied && row.pattern_id != null && fromPatterns() && (
                        <GenerateFileMenu
                          domain={subject(row.entity)}
                          prepare={() => forFile(row, kept)}
                        />
                      )}
                      {onApplied && row.pattern_id != null && (
                        <Button
                          expanded={open}
                          onClick={() =>
                            setSplitting(open ? null : {
                              row: rowKey, id: row.pattern_id, name: row.name,
                              entity: subject(row.entity),
                            })
                          }
                        >
                          {open ? "Hide scenarios" : "Scenarios"}
                        </Button>
                      )}
                      {kept ? (
                        <Chip tone="affirm">Saved</Chip>
                      ) : (
                        <Button
                          onClick={() => keep(row)}
                          busy={busy === `keep-${row.pattern_id}`}
                        >
                          Save
                        </Button>
                      )}
                    </td>
                  </tr>
                  {open && splitting && onApplied && (
                    <tr className="discover__scenariorow">
                      <td colSpan={5}>
                        <ScenarioPanel
                          splitting={splitting}
                          onClose={() => setSplitting(null)}
                          onApplied={onApplied}
                        />
                      </td>
                    </tr>
                  )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
          </div>
          {rows.length === 0 && (
            <p className="discover__none">No scanned pattern contains "{query.trim()}".</p>
          )}
        </>
      )}

      {!found && !scanning && (
        <Empty title="No scan yet" action={<Button onClick={walk}>Run scan</Button>}>
          A scan ranks patterns across the entire book of business.
        </Empty>
      )}
    </div>
  );
}

/** How many a saved pattern loads, where nothing else says. */
const SAVED_LOAD_VOLUME = 1000;

export function SavedPatterns({
  domain, saved, error: listError, reload, onApplied, query = "", onShowAll,
}: {
  domain: Domain;
  /** The saved patterns of this subject; null while they load. */
  saved: SavedPattern[] | null;
  error?: string | null;
  reload: () => Promise<void>;
  onApplied?: OnApplied;
  /** The Find screen's search text: rows shown hold it. */
  query?: string;
  /** Back to every pattern, from an empty list. */
  onShowAll?: () => void;
}) {
  const [splitting, setSplitting] = useState<Splitting | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Kept patterns chosen for export, and the file type.
  const [chosen, setChosen] = useState<Set<number>>(new Set());
  const [format, setFormat] = useState<DownloadFormat>("xlsx");
  // The row whose Delete waits to be confirmed.
  const [confirming, setConfirming] = useState<number | null>(null);

  const listed = (saved ?? []).filter((row) =>
    matches(query, row.name, ...(row.scope ?? [])),
  );
  // Scenarios of a pattern the search hides, or that was deleted: closed.
  const listedIds = listed.map((row) => row.pattern_id).join("|");
  useEffect(() => {
    if (splitting && !listed.some((row) => row.pattern_id === splitting.id)) setSplitting(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listedIds]);
  // Only what is ticked and still shown is downloaded: a row hidden by the
  // search, or deleted, is not.
  const chosenShown = listed.filter((row) => chosen.has(row.pattern_id)).map((row) => row.pattern_id);

  // The confirmation takes focus at its Cancel; Cancel gives it back to Delete.
  const list = useRef<HTMLUListElement>(null);
  const backTo = useRef<number | null>(null);
  useEffect(() => {
    const rowOf = (id: number) => list.current?.querySelector(`[data-pattern="${id}"]`);
    if (confirming !== null) {
      const buttons = rowOf(confirming)?.querySelectorAll(".kept__confirm button");
      (buttons?.[buttons.length - 1] as HTMLElement | undefined)?.focus();
    } else if (backTo.current !== null) {
      const again = [...(rowOf(backTo.current)?.querySelectorAll("button") ?? [])]
        .find((b) => b.textContent?.trim() === "Delete");
      again?.focus();
      backTo.current = null;
    }
  }, [confirming]);

  // Kept patterns, counted on one sheet: every field and value, every scenario.
  const exportKept = async (ids: number[]) => {
    setBusy("export");
    setError(null);
    try {
      await api.patternReportSheet(
        { domain, filters: {}, k: null, pattern_ids: ids, format },
        `saved-patterns.${format}`,
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };
  const pick = (id: number) =>
    setChosen((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  // The pattern last deleted: Delete only unmarks it, so Undo marks it again.
  const [removed, setRemoved] = useState<{ id: number; name: string } | null>(null);
  const drop = async (patternId: number, name: string) => {
    setBusy(`drop-${patternId}`);
    try {
      await api.unsave(patternId);
      setRemoved({ id: patternId, name });
      setConfirming(null);
      setChosen((previous) => {
        const next = new Set(previous);
        next.delete(patternId);
        return next;
      });
      await reload();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };
  const undoDrop = async () => {
    if (!removed) return;
    setBusy("undo");
    try {
      await api.bookmark(removed.id);
      setRemoved(null);
      await reload();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="discover__kept" aria-label="Saved patterns">
      <div className="kept__head">
        <h3>Saved</h3>
        {listed.length > 0 && (
          <div className="kept__export">
            <label className="named">
              <span>Download as</span>
              <select
                aria-label="Download saved patterns as"
                value={format}
                onChange={(e) => setFormat(e.target.value as DownloadFormat)}
              >
                {DOWNLOAD_FORMATS.map((f) => (
                  <option key={f.value} value={f.value}>
                    {f.label}
                  </option>
                ))}
              </select>
            </label>
            <Button
              onClick={() => void exportKept(chosenShown)}
              busy={busy === "export"}
              disabled={chosenShown.length === 0}
            >
              Download {chosenShown.length || ""} selected
            </Button>
            <Button
              onClick={() => void exportKept(listed.map((s) => s.pattern_id))}
              busy={busy === "export"}
            >
              Download all
            </Button>
          </div>
        )}
      </div>
      {(error || listError) && <Banner tone="alarm">{error ?? listError}</Banner>}
      {removed && (
        <Banner tone="neutral">
          {removed.name} deleted from Saved.{" "}
          <Button onClick={() => void undoDrop()} busy={busy === "undo"}>
            Undo
          </Button>
        </Banner>
      )}
      {saved === null ? (
        !listError && <p className="discover__none" role="status">Loading saved patterns…</p>
      ) : saved.length === 0 ? (
        <Empty
          title="No saved patterns"
          action={onShowAll ? <Button onClick={onShowAll}>Show all patterns</Button> : undefined}
        >
          Patterns you save are listed here.
        </Empty>
      ) : listed.length === 0 ? (
        <p className="discover__none">No saved pattern contains "{query.trim()}".</p>
      ) : (
        <ul className="kept" ref={list}>
          {listed.map((row) => (
            <Fragment key={row.pattern_id}>
            <li className="kept__row" data-pattern={row.pattern_id}>
              <input
                type="checkbox"
                aria-label={`Select ${row.name} for export`}
                checked={chosen.has(row.pattern_id)}
                onChange={() => pick(row.pattern_id)}
              />
              <span className="kept__name">{row.name}</span>
              <Chip tone={isChild(row.entity) ? "catalog" : "learned"} fill="outline">
                {termsOf(patternKey(row.entity)).Many}
              </Chip>
              {row.origin === "user_defined" && (
                <Chip tone="user" fill="outline">Custom</Chip>
              )}
              {row.archived && (
                <Chip tone="neutral" fill="outline">Archived: earlier build, read-only</Chip>
              )}
              <span className="kept__where">
                {(row.scope_levels ?? [])
                  .slice(0, 2)
                  .filter((p) => p && p !== "All")
                  .join(" › ")}
              </span>
              <span className="kept__size num">{formatCount(row.member_count)}</span>
              {confirming === row.pattern_id ? (
                <span className="kept__confirm">
                  <span>Delete this pattern?</span>
                  <Button
                    variant="danger"
                    onClick={() => void drop(row.pattern_id, row.name)}
                    busy={busy === `drop-${row.pattern_id}`}
                  >
                    Delete
                  </Button>
                  <Button
                    onClick={() => {
                      backTo.current = row.pattern_id;
                      setConfirming(null);
                    }}
                  >
                    Cancel
                  </Button>
                </span>
              ) : (
                <>
                  {onApplied && fromPatterns() && !row.archived && (
                    <GenerateFileMenu
                      domain={subject(row.entity)}
                      prepare={async () => ({
                        kind: "saved", id: row.pattern_id, name: row.name,
                        entity: row.entity, members: row.member_count,
                      })}
                    />
                  )}
                  {fromPatterns() && !row.archived && (
                    <LoadToDatabaseButton
                      name={row.name}
                      amount={`${formatCount(SAVED_LOAD_VOLUME)} ${subject(row.entity)}`}
                      apply={async () =>
                        (await api.storyToPlan([row.pattern_id], SAVED_LOAD_VOLUME, true))
                          .applied?.length ?? 0
                      }
                    />
                  )}
                  {onApplied && !row.archived && (
                    <Button
                      expanded={splitting?.id === row.pattern_id}
                      onClick={() =>
                        setSplitting(splitting?.id === row.pattern_id ? null : {
                          row: String(row.pattern_id), id: row.pattern_id, name: row.name,
                          entity: subject(row.entity),
                        })
                      }
                    >
                      {splitting?.id === row.pattern_id ? "Hide scenarios" : "Scenarios"}
                    </Button>
                  )}
                  {!row.archived && (
                    <Button onClick={() => setConfirming(row.pattern_id)}>Delete</Button>
                  )}
                </>
              )}
            </li>
            {splitting?.id === row.pattern_id && onApplied && (
              <li className="kept__scenarios">
                <ScenarioPanel
                  splitting={splitting}
                  onClose={() => setSplitting(null)}
                  onApplied={onApplied}
                />
              </li>
            )}
            </Fragment>
          ))}
        </ul>
      )}
    </section>
  );
}
