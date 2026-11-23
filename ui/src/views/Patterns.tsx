/**
 * Patterns.
 *
 * The analyzer finds groups of members who resemble each other. This screen
 * shows what it found and, for any one of them, what generating that cohort
 * would actually mean: which columns would carry which rules, and which parts
 * of the finding could not be expressed.
 *
 * The second half is the point. A pattern is a claim about a population, and a
 * claim nobody can act on is decoration — so every pattern here is one click
 * away from the rules it implies, and those rules are shown before they are
 * applied rather than after.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  type DownloadFormat,
  type AnalyzePayload,
  type CategoricalDist,
  type ClaimsAnalyzePayload,
  type Highlight,
  type Domain,
  type DiscoveredPattern,
  type FilePattern,
  type Overview,
  type KeptPattern,
  type PatternFilters,
  type SavedPattern,
  type ScopeDistribution,
} from "../lib/api";
import { GenerateFileMenu } from "../components/GenerateFileMenu";
import {
  LoadToDatabaseButton, NoTargetNote, PATTERN_AS_OF, type LoadPreview,
} from "../components/LoadToDatabase";
import "./Patterns.css";
import { DefinePattern } from "./DefinePattern";
import { Discover, SavedPatterns } from "./Discover";
import { PatternDistributions, PatternOverview, claimTypeName, seriesColor } from "./PatternCharts";
import { PatternCounts } from "./PatternCounts";
import { PatternRows } from "./PatternRows";
import { Scenarios } from "./Scenarios";
import { RequirementsFile, StoryAnalysis } from "./Story";
import { columnName, facts, fromPatterns, isChild, linesOf, patternKey, patternKeys, termsOf } from "../lib/pack";

/** The reference date ages and tenures are measured from. Never the clock:
 *  a pattern found today has to mean the same thing when it is replayed. */
const AS_OF = PATTERN_AS_OF;

interface Props {
  // A file is written for a pattern; a database load is planned from one.
  // Only the second writes anything here, so only the first carries the
  // pattern along.
  onApplied: (destination: "file" | "db", pattern?: FilePattern, note?: string) => void;
  domain: Domain;
}

/** A pattern somebody defined by its rules: the scope it states, and its name. */
interface DefinedScope {
  name: string;
  filters: PatternFilters;
}

/** The whole book's patterns, found once per subject and number of patterns:
 *  coming back to the Find screen shows them again without asking the server. */
const foundCache = new Map<string, {
  analysis: AnalyzePayload | ClaimsAnalyzePayload;
  ranWith: { filters: PatternFilters; k: number | null; claimType: string | null };
}>();

/** The number of patterns the whole book was last found in, per subject:
 *  kept when the list is left and come back to. */
const chosenK = new Map<Domain, number | null>();

/** The fields patterns are found on changed: the whole book is found again. */
export function forgetFoundPatterns(): void {
  foundCache.clear();
}

/** Keys in a fixed order, so the same filters always read the same. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value as Record<string, unknown>)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonical((value as Record<string, unknown>)[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

/** A found pattern's identity: what saving it records, so Not saved can tell
 *  a saved pattern by what it is rather than by its name. */
export function patternIdentity(
  domain: Domain, filters: PatternFilters, k: number | null, clusterId: number,
  claimType: string | null,
): string {
  return canonical([domain, filters ?? {}, k ?? null, clusterId, claimType ?? null]);
}

/** Found patterns not shown: saved ones (by identity, else by name where the
 *  server does not say what they were saved from), except those saved during
 *  this visit, which stay where they were. */
export interface HiddenPatterns {
  keys: Set<string> | null;
  names: Set<string> | null;
  except: Set<string>;
  /** The saved patterns are not known yet: no card is shown until they are,
   *  or a saved one would show under Not saved. */
  pending: boolean;
  /** Why the saved patterns could not be read: cards are shown, said as such. */
  failed: string | null;
}

/** What a pattern is searched by: its name and every value it is described by. */
function haystack(pattern: DiscoveredPattern): string {
  const words: string[] = [pattern.name, pattern.weight_band];
  for (const values of Object.values(pattern.descriptions ?? {})) words.push(...values);
  for (const [feature, dist] of Object.entries(pattern.categorical ?? {})) {
    words.push(featureLabel(feature));
    const shares = (dist as CategoricalDist)?.pct ?? dist;
    words.push(...Object.keys(shares ?? {}));
  }
  for (const place of pattern.geography ?? []) {
    words.push(String(place.ADDRESS_CITY ?? ""), String(place.ADDRESS_STATE ?? ""));
  }
  return words.join(" ").toLowerCase();
}

export function PatternsAnalyze({
  onApplied,
  domain,
  defined,
  autoRun = false,
  onConfig,
  query = "",
  bands,
  hide,
  onSaved,
  onRequirement,
}: Props & {
  defined?: DefinedScope;
  autoRun?: boolean;
  onConfig?: () => void;
  /** Free text the patterns shown must contain (name or values). */
  query?: string;
  /** Size bands shown; none chosen shows every band. */
  bands?: Set<string>;
  /** Patterns not shown: the saved ones, where only unsaved are asked for. */
  hide?: HiddenPatterns;
  /** A pattern was saved from here: a whole found pattern names its identity. */
  onSaved?: (identity?: string) => void;
  /** The search text, read as a requirement instead. */
  onRequirement?: () => void;
}) {
  // What the clustering finds patterns on, as chosen on the Config tab.
  const [foundOn, setFoundOn] = useState<string[]>([]);
  const [foundOnError, setFoundOnError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setFoundOnError(null);
    api
      .discoveryFeatures()
      .then((all) => live && setFoundOn(all[domain].filter((f) => f.chosen).map((f) => f.label)))
      .catch((e: unknown) => {
        if (!live) return;
        setFoundOn([]);
        setFoundOnError(e instanceof ApiError ? e.message : String(e));
      });
    return () => {
      live = false;
    };
  }, [domain]);
  // What the store holds of these records: their headline counts, and their
  // most frequent values where the pack names any (claims' diagnoses).
  const [overview, setOverview] = useState<Overview | null>(null);
  const [highlights, setHighlights] = useState<Highlight[]>([]);
  // The whole book's number of patterns survives leaving the list.
  const remembersK = !defined && autoRun;
  const [k, setKState] = useState<number | null>(() =>
    remembersK ? chosenK.get(domain) ?? null : null);
  const setK = (next: number | null) => {
    if (remembersK) chosenK.set(domain, next);
    setKState(next);
  };
  // Claim type is a Criteria field now; the analysis is never split by one here.
  const claimType: string | null = null;

  const [analysis, setAnalysis] = useState<AnalyzePayload | ClaimsAnalyzePayload | null>(
    null,
  );
  const [opened, setOpened] = useState<number | null>(null);
  // What every pattern of the analysis is split into scenarios on: one choice,
  // which the scenario list, the counts and every download all follow.
  const [split, setSplit] = useState<string[] | null>(null);
  // The split failing to load is said: without it, scenarios are left out of
  // the lists and every download.
  const [splitError, setSplitError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setSplit(null);
    setSplitError(null);
    api
      .scenarioDimensions(domain)
      .then((d) => live && setSplit(d.defaults))
      .catch((e: unknown) => {
        if (!live) return;
        setSplit(null);
        setSplitError(e instanceof ApiError ? e.message : String(e));
      });
    return () => {
      live = false;
    };
  }, [domain]);

  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The book's summary failing is said on its own: the patterns may be fine.
  const [overviewError, setOverviewError] = useState<string | null>(null);

  // The whole book, or the scope a definition or requirement states.
  const filters = useMemo<PatternFilters>(() => defined?.filters ?? {}, [defined]);
  // What the analysis on screen was run with. Its patterns are rebuilt from
  // exactly this -- not from whatever the controls say after it ran.
  const [ranWith, setRanWith] = useState<{
    filters: PatternFilters;
    k: number | null;
    claimType: string | null;
  }>({ filters: {}, k: null, claimType: null });

  // The book's size, said beside the whole book's patterns, and its most
  // frequent values below them. Read for the records being looked at.
  useEffect(() => {
    setOverview(null);
    setHighlights([]);
    setOverviewError(null);
    if (defined) return;
    let live = true;
    const failed = (e: unknown) =>
      live && setOverviewError(e instanceof ApiError ? e.message : String(e));
    // Each on its own: highlights that fail leave the counts standing.
    api.overview(domain).then((counts) => live && setOverview(counts)).catch(failed);
    api
      .highlights(domain, 6)
      .then((ranked) => live && setHighlights(ranked.filter((h) => h.codes.length > 0)))
      .catch(failed);
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [domain]);

  // Each analysis is numbered; only the latest one's answer is shown, so a
  // slower earlier run never replaces the analysis on screen (and what its
  // downloads and Generate file act on).
  const analyses = useRef(0);
  useEffect(() => {
    // A run still going for the domain before is dropped, and its spinner too.
    analyses.current += 1;
    setAnalysis(null);
    setBusy((was) => (was === "measure" || was === "analyze" ? null : was));
  }, [domain]);

  // k of 1 is the whole scope as one pattern: a defined pattern, measured.
  const analyze = useCallback(async (only?: number) => {
    const mine = ++analyses.current;
    const runK = only ?? k;
    // The whole book, found before: shown again at once.
    const cacheKey = `${domain}|${runK}`;
    const cached = !defined && autoRun ? foundCache.get(cacheKey) : undefined;
    if (cached) {
      // Shown at once, replacing any run still going.
      setBusy((was) => (was === "measure" || was === "analyze" ? null : was));
      setError(null);
      setOpened(null);
      setAnalysis(cached.analysis);
      setRanWith(cached.ranWith);
      return;
    }
    setBusy(only === 1 ? "measure" : "analyze");
    setError(null);
    setOpened(null);
    try {
      const found =
        isChild(domain)
          ? await api.analyzeClaims(filters, runK, claimType)
          : await api.analyze(filters, runK);
      const ran = { filters, k: runK, claimType };
      if (!defined && autoRun) foundCache.set(cacheKey, { analysis: found, ranWith: ran });
      if (mine !== analyses.current) return;
      setAnalysis(found);
      setRanWith(ran);
      if (only === 1) setOpened(0);
    } catch (e) {
      if (mine !== analyses.current) return;
      setError(e instanceof ApiError ? e.message : String(e));
      setAnalysis(null);
    } finally {
      if (mine === analyses.current) setBusy(null);
    }
  }, [filters, k, domain, claimType, defined, autoRun]);
  const measured = analysis?.method === "whole_scope";

  // Every pattern on screen, its values and scenarios counted, on one sheet.
  const downloadSheet = async (format: DownloadFormat) => {
    setBusy(`sheet-${format}`);
    setError(null);
    try {
      await api.patternReportSheet(
        {
          domain,
          filters: ranWith.filters,
          k: ranWith.k,
          claim_type: ranWith.claimType,
          ...(split ? { scenario_keys: split } : {}),
          format,
        },
        `${domain}-patterns.${format}`,
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  // Handed a scope to analyze straight away, as a requirement is: the patterns
  // inside it, found once, with nothing to press first.
  const started = useRef(false);
  useEffect(() => {
    if (!autoRun || started.current) return;
    started.current = true;
    void analyze();
  }, [autoRun, analyze]);
  // The whole book found again when the number of patterns changes: no button.
  const lastK = useRef(k);
  useEffect(() => {
    if (lastK.current === k) return;
    lastK.current = k;
    if (!defined && autoRun) void analyze();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [k]);

  // What failed to load beside the patterns, said where the patterns are.
  const loadNotes = (
    <>
      {splitError && (
        <Banner tone="caution">
          {`Couldn't load the scenario split, so scenarios are left out of the patterns and downloads: ${splitError}`}
        </Banner>
      )}
      {foundOnError && (
        <Banner tone="caution">
          {`Couldn't load the fields patterns are found on: ${foundOnError}`}
        </Banner>
      )}
    </>
  );

  // Finding failed: found again from the same place, with nothing changed.
  const failed = error && !analysis && (
    <Banner tone="alarm">
      {error}{" "}
      <Button onClick={() => void analyze()}>Try again</Button>
    </Banner>
  );

  // More options: Generate › Files with this pattern chosen. Nothing is
  // written: the file reads the pattern itself. Members in it are new people
  // with these values; claims in it are for the members this pattern holds.
  const openFiles = useCallback(
    (pattern: DiscoveredPattern) => {
      onApplied("file", {
        kind: "live",
        name:
          measured && defined?.name && analysis?.patterns.length === 1
            ? defined.name
            : pattern.name,
        domain,
        pattern,
        filters: ranWith.filters,
        k: ranWith.k,
        claimType: ranWith.claimType,
        members: pattern.size,
      });
    },
    [onApplied, domain, ranWith, measured, defined, analysis],
  );

  if (defined && autoRun) {
    return (
      <div className="patterns">
        {busy === "analyze" && <p className="panel__hint" role="status">Analyzing…</p>}
        {failed || (error && <Banner tone="alarm">{error}</Banner>)}
        {loadNotes}
        {results()}
      </div>
    );
  }

  if (defined) {
    // Defining a pattern has its own header and rules above; what is left is
    // to measure it or look inside it.
    return (
      <div className="patterns">
        <section className="define__run">
          <Button
            onClick={() => analyze(1)}
            busy={busy === "measure"}
            disabled={busy === "analyze"}
          >
            Measure pattern
          </Button>
          <label className="named">
            <span>Number of patterns</span>
            <select
              value={k ?? ""}
              onChange={(e) => setK(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">Automatic</option>
              {[2, 3, 4, 5, 6, 8, 10].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
          <Button
            onClick={() => analyze()}
            busy={busy === "analyze"}
            disabled={busy === "measure"}
          >
            Find patterns within
          </Button>
        </section>
        {failed || (error && <Banner tone="alarm">{error}</Banner>)}
        {loadNotes}
        {results()}
      </div>
    );
  }

  return (
    <div className="patterns">
      <div className="patterns__head">
        <p className="patterns__lede">
          {foundOn.length ? <>Patterns by <strong>{foundOn.join(", ")}</strong>.</> : null}
          {onConfig && (
            <>
              {" "}
              <button type="button" className="linklike" onClick={onConfig}>
                Change fields
              </button>
            </>
          )}
        </p>
        {overview && (
          <dl className="patterns__scope">
            {(termsOf(domain).counts ?? []).map(([count, label]) => (
              <div key={count}>
                <dt>{label ?? termsOf(domain).Many}</dt>
                <dd className="num">{formatCount(overview[count] ?? 0)}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>

      {failed || (error && <Banner tone="alarm">{error}</Banner>)}
      {overviewError && (
        <Banner tone="caution">{`Couldn't load the summary: ${overviewError}`}</Banner>
      )}
      {loadNotes}

      {busy === "analyze" && (
        <p className="patterns__loading" role="status">
          <span className="btn__spinner" aria-hidden="true" /> Finding patterns…
        </p>
      )}

      {!analysis && !busy && !error && (
        <Empty
          title="No patterns yet"
          action={<Button onClick={() => analyze()}>Find patterns</Button>}
        >
          Patterns are found across all {termsOf(domain).many}.
        </Empty>
      )}

      {results()}

      {highlights.length > 0 && (
        <section className="clinical">
          {highlights.map((highlight) => (
            <div key={highlight.key}>
              <h3>{highlight.label}</h3>
              <ul>
                {highlight.codes.map((code) => (
                  <li key={code.code}>
                    <code className="mono">{code.code}</code>
                    <span className="clinical__desc">{code.description ?? "-"}</span>
                    <span className="clinical__n num">{formatCount(code.n)}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </section>
      )}
    </div>
  );

  function results() {
    if (!analysis) return null;
    // A measured definition is named what its author called it. Holding
    // claims of several types it is one pattern of each, named for its type.
    const several = analysis.patterns.length > 1;
    const shown =
      measured && defined?.name
        ? analysis.patterns.map((p) => {
            const type = several ? claimTypeName(p) : null;
            return { ...p, name: type ? `${defined.name} · ${type}` : defined.name };
          })
        : analysis.patterns;
    // What the Find screen's search and chips leave showing; each keeps its
    // place in the analysis, which is its color in every chart.
    const wanted = query.trim().toLowerCase();
    const identity = (pattern: DiscoveredPattern) =>
      patternIdentity(domain, ranWith.filters, ranWith.k, pattern.cluster_id, ranWith.claimType);
    const hidden = (pattern: DiscoveredPattern) => {
      if (!hide) return false;
      const id = identity(pattern);
      if (hide.except.has(id)) return false;
      return Boolean(hide.keys?.has(id)) || Boolean(hide.names?.has(pattern.name));
    };
    const visible = hide?.pending ? [] : shown
      .map((pattern, index) => ({ pattern, index }))
      .filter(
        ({ pattern }) =>
          (!bands?.size || bands.has(pattern.weight_band)) &&
          !hidden(pattern) &&
          (!wanted || haystack(pattern).includes(wanted)),
      );
    return (
        <>
          {measured ? (
            <p className="patterns__summary">
              <strong className="num">
                {formatCount(
                  "subset_claims" in analysis
                    ? analysis.subset_claims
                    : analysis.subset_members,
                )}
              </strong>{" "}
              {termsOf(domain).many} meet these rules
              {defined?.name ? ` (${defined.name})` : ""}.{" "}
              {several
                ? `They span ${analysis.patterns.length} ${termsOf(domain).one} types, shown as one pattern per ${termsOf(domain).one} type.`
                : ""}
            </p>
          ) : (
          <p className="patterns__summary">
            <strong className="num">
              {formatCount(
                "subset_claims" in analysis
                  ? analysis.subset_claims
                  : analysis.subset_members,
              )}
            </strong>{" "}
            {termsOf(domain).many} in scope, split into{" "}
            <strong className="num">{analysis.n_patterns}</strong>{" "}
            patterns.
            {!defined && (
              <label className="patterns__k">
                <span>Number of patterns</span>
                <select
                  value={k ?? ""}
                  onChange={(e) => setK(e.target.value ? Number(e.target.value) : null)}
                  disabled={busy === "analyze"}
                >
                  <option value="">Automatic</option>
                  {[2, 3, 4, 5, 6, 8, 10].map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </p>
          )}

          <div className="patterns__export">
            <span>Download all pattern counts as</span>
            {DOWNLOAD_FORMATS.map((f) => (
              <Button
                key={f.value}
                onClick={() => void downloadSheet(f.value)}
                busy={busy === `sheet-${f.value}`}
              >
                {f.label}
              </Button>
            ))}
          </div>

          <PatternOverview
            patterns={shown}
            distributions={analysis.distributions}
            unit={linesOf(domain)}
            selected={opened}
            onSelect={(cid) => {
              setOpened(cid);
              // Selecting in a chart opens the card; bring it into view so the
              // detail is where the eye goes next.
              if (cid !== null) {
                requestAnimationFrame(() =>
                  document
                    .getElementById(`pattern-${cid}`)
                    ?.scrollIntoView({ behavior: "smooth", block: "nearest" }),
                );
              }
            }}
          />

          {hide?.pending && shown.length > 0 && (
            <p className="patterns__summary" role="status">
              Checking which patterns are saved…
            </p>
          )}
          {hide?.failed && (
            <p className="patterns__summary" role="status">
              Saved patterns could not be read ({hide.failed}), so saved ones may be listed
              here too.
            </p>
          )}
          {!hide?.pending && visible.length === 0 && shown.length > 0 && (
            <Empty
              title="No matching patterns"
              action={
                wanted && onRequirement ? (
                  <Button onClick={onRequirement}>Find as requirement</Button>
                ) : undefined
              }
            >
              {wanted
                ? `No pattern name or value contains "${query.trim()}".`
                : "No pattern matches the chosen filters."}
            </Empty>
          )}

          <ul className="patterns__list">
            {visible.map(({ pattern, index }) => (
              <PatternCard
                key={pattern.cluster_id}
                pattern={pattern}
                index={index}
                patterns={shown}
                distributions={analysis.distributions}
                open={opened === pattern.cluster_id}
                onToggle={() =>
                  setOpened(opened === pattern.cluster_id ? null : pattern.cluster_id)
                }
                onMore={() => openFiles(pattern)}
                domain={domain}
                filters={ranWith.filters}
                k={ranWith.k}
                claimType={ranWith.claimType}
                split={split}
                onSplit={setSplit}
                onApplied={onApplied}
                keepAs={
                  measured && defined
                    ? { name: defined.name ? pattern.name : undefined, origin: "user_defined" }
                    : undefined
                }
                onKept={(kept, open, whole) => {
                  onSaved?.(whole ? identity(pattern) : undefined);
                  if (!open) return;
                  onApplied("file", {
                    kind: "saved",
                    id: kept.pattern_id,
                    name: kept.name,
                    entity: kept.domain,
                    members: kept.member_count,
                  });
                }}
              />
            ))}
          </ul>
        </>
    );
  }
}

interface CardProps {
  pattern: DiscoveredPattern;
  // Its place in the analysis, which is its color in every chart.
  index: number;
  patterns: DiscoveredPattern[];
  distributions?: Record<string, ScopeDistribution>;
  domain: Domain;
  // What addresses this pattern's subset. A pattern from the profiler has no
  // id -- it is the output of an analysis -- so the analysis is the address.
  filters: PatternFilters;
  k: number | null;
  claimType: string | null;
  open: boolean;
  onToggle: () => void;
  // Generate file's More options: Generate › Files with this pattern chosen.
  onMore: () => void;
  // A pattern was saved; `open` asks for File Generation with it chosen;
  // `whole` says it is the whole pattern, not chosen records of it.
  onKept: (kept: KeptPattern, open: boolean, whole: boolean) => void;
  // Saved under this name and origin: a pattern somebody defined.
  keepAs?: { name?: string; origin: string };
  // Scenarios of this pattern, generated together, go on to a file or a load.
  onApplied: (destination: "file" | "db", pattern?: FilePattern, note?: string) => void;
  // What the analysis's patterns are split into scenarios on, and changing it.
  split: string[] | null;
  onSplit: (keys: string[]) => void;
}

function PatternCard({
  pattern, index, patterns, distributions,
  open, onToggle, onMore, onKept, domain, filters, k, claimType,
  keepAs, onApplied, split, onSplit,
}: CardProps) {
  const [showRows, setShowRows] = useState(false);
  const [showScenarios, setShowScenarios] = useState(false);
  const [showCounts, setShowCounts] = useState(false);
  const [saved, setSaved] = useState<KeptPattern | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  // The name it is saved under: the one it would be given, changed in place.
  const autoName = keepAs?.name ?? pattern.name;
  const [saveName, setSaveName] = useState(autoName);
  const kind = termsOf(domain).one;

  // Keeps the whole pattern, so it is listed under Saved patterns and a run
  // can be kept against it. Saved once: a second call finds the first.
  const keep = async (): Promise<KeptPattern> => {
    if (saved) return saved;
    const named = saveName.trim();
    const kept = await api.keepPattern({
      domain, filters, k, cluster_id: pattern.cluster_id, claim_type: claimType,
      ...(keepAs ?? {}),
      ...(named && named !== autoName ? { name: named } : {}),
    });
    setSaved(kept);
    onKept(kept, false, true);
    return kept;
  };
  const save = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      await keep();
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };
  // Generate file: the saved pattern, saved now where it is not yet.
  const forFile = async (): Promise<FilePattern> => {
    const kept = await keep();
    return {
      kind: "saved", id: kept.pattern_id, name: kept.name, entity: kept.domain,
      members: kept.member_count,
    };
  };
  // Load to database: the columns the pattern sets, then the pattern on the plan.
  const loadPreview = async (): Promise<LoadPreview> => {
    const found = await api.toPlan(pattern, kind, AS_OF, false, false);
    // No plan built yet: the columns are worked out when it is loaded.
    if (!found.resolution) return { columns: [], pending: true };
    return {
      columns: found.resolution.targets.map((t) => ({
        name: columnName(t.column), from: fromWords(t.parameter), title: `${t.table}.${t.column}`,
      })),
      unmapped: found.resolution.rejections.map((r) => fromWords(r.parameter).split(":")[0]),
    };
  };
  const loadApply = async (asCohort: boolean) =>
    (await api.toPlan(pattern, kind, AS_OF, true, asCohort)).applied.length;
  // The two analyzers name the same ideas differently, because one measures
  // over the primary records and the other over the child records' lines:
  // the pack says which measure is which.
  const measures = facts();
  const age = (measures.age ?? []).map((key) => pattern.continuous?.[key]).find(Boolean);
  const tenure = measures.tenure ? pattern.continuous?.[measures.tenure] : undefined;
  const charge = measures.amount ? pattern.continuous?.[measures.amount[0]] : undefined;

  return (
    <li id={`pattern-${pattern.cluster_id}`} className={"pattern" + (open ? " pattern--open" : "")}>
      <button className="pattern__head" onClick={onToggle} aria-expanded={open}>
        <span className="pattern__key" style={{ background: seriesColor(index) }} aria-hidden="true" />
        <span className="pattern__name">{pattern.name}</span>
        <span className="pattern__size num">
          {isChild(domain) && pattern.n_claims !== undefined
            ? `${formatCount(pattern.n_claims)} ${termsOf(domain).many} · ${formatCount(pattern.size)} lines`
            : `${formatCount(pattern.size)} ${linesOf(domain)}`}
        </span>
        <Share value={pattern.pct_of_lines ?? pattern.pct_of_subset} />
        <Chip tone={pattern.rare ? "caution" : "learned"}>
          {pattern.weight_band.charAt(0).toUpperCase() + pattern.weight_band.slice(1)}
        </Chip>
      </button>

      <div className="pattern__facts">
        {age && (
          <Fact label="Age">
            {Math.round(age.min)}–{Math.round(age.max)}
            <span className="pattern__mean"> mean {age.mean.toFixed(1)}</span>
          </Fact>
        )}
        {tenure && (
          <Fact label="Tenure">
            {Math.round(tenure.min)}–{Math.round(tenure.max)}
            <span className="pattern__mean"> months</span>
          </Fact>
        )}
        {charge && (
          <Fact label={measures.amount?.[1] ?? "Amount"}>
            {Math.round(charge.min)}–{Math.round(charge.max)}
            <span className="pattern__mean"> mean {charge.mean.toFixed(0)}</span>
          </Fact>
        )}
        {(pattern.geography?.length ?? 0) > 0 && (
          <Fact label="Locations">
            {pattern.geography!.length.toLocaleString()} ZIPs
            <span className="pattern__mean">
              {" "}top {String(pattern.geography![0].ADDRESS_CITY ?? "")}{" "}
              {String(pattern.geography![0].ADDRESS_STATE ?? "")}
            </span>
          </Fact>
        )}
        {Object.entries(pattern.categorical ?? {})
          // A feature with no values in these rows (an empty column) says nothing.
          .filter(([, dist]) => topShares(dist).length > 0)
          .map(([feature, dist]) => (
            <Fact key={feature} label={featureLabel(feature)}>
              <span className="fact__shares">
                {topShares(dist).map(([value, share], at) => (
                  <span key={value} className="fact__share">
                    {at > 0 && <span className="sr-only">, </span>}
                    {value}
                    &nbsp;
                    <span className="pattern__mean">{share}</span>
                  </span>
                ))}
              </span>
            </Fact>
          ))}
      </div>

      {open && (
        <PatternDistributions
          pattern={pattern}
          index={index}
          patterns={patterns}
          distributions={distributions}
        />
      )}

      {open && (
        <div className="pattern__plan">
          <div className="pattern__actions">
            {fromPatterns() && (
              <>
                <GenerateFileMenu domain={domain} prepare={forFile} onMore={onMore} />
                <LoadToDatabaseButton
                  name={saved?.name ?? pattern.name}
                  share={pattern.pct_of_total}
                  preview={loadPreview}
                  apply={loadApply}
                />
              </>
            )}
            <Button onClick={() => setShowRows(!showRows)}>
              {showRows ? "Hide source records" : "Show source records"}
            </Button>
            <Button onClick={() => setShowScenarios(!showScenarios)}>
              {showScenarios ? "Hide scenarios" : "Scenarios"}
            </Button>
            <Button onClick={() => setShowCounts(!showCounts)}>
              {showCounts ? "Hide counts" : "Counts and %"}
            </Button>
            {saved ? (
              <Button disabled>Saved</Button>
            ) : (
              <form
                className="savename"
                onSubmit={(e) => {
                  e.preventDefault();
                  void save();
                }}
              >
                <input
                  aria-label="Save as"
                  value={saveName}
                  onChange={(e) => setSaveName(e.target.value)}
                  disabled={saving}
                />
                <Button type="submit" busy={saving} disabled={!saveName.trim()}>
                  Save pattern
                </Button>
              </form>
            )}
            {saved && (
              <p className="pattern__caveat">
                {saved.created === false && !saved.restored
                  ? `Already saved as "${saved.name}".`
                  : `Saved as "${saved.name}".`}
              </p>
            )}
          </div>
          {saveError && <Banner tone="alarm">{saveError}</Banner>}

          {showCounts && (
            <PatternCounts
              domain={domain}
              name={pattern.name}
              filters={filters}
              k={k}
              clusterId={pattern.cluster_id}
              claimType={claimType}
              split={split}
            />
          )}

          {showScenarios && (
            <Scenarios
              domain={domain}
              name={pattern.name}
              filters={filters}
              k={k}
              clusterId={pattern.cluster_id}
              claimType={claimType}
              split={split}
              onSplit={onSplit}
              onApplied={onApplied}
            />
          )}

          {showRows && (
            <PatternRows
              domain={domain}
              filters={filters}
              k={k}
              clusterId={pattern.cluster_id}
              claimType={claimType}
              previewPattern={pattern}
              onKept={(kept) => onKept(kept, true, false)}
              onSaved={(kept) => onKept(kept, false, false)}
            />
          )}
        </div>
      )}
    </li>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="fact">
      <span className="fact__label">{label}</span>
      <span className="fact__value num">{children}</span>
    </div>
  );
}

const FEATURE_WORDS: Record<string, string> = {
  MEME_SEX: "Sex",
  MEME_MARITAL_STATUS: "Marital status",
  MEME_REL: "Relationship",
  gender: "Sex",
  claim_status: "Status",
  claim_type: "Type",
  network: "Network",
};

// What the plan calls a pattern's value, where it is not a person's word. The
// plan matches on these names, so they are reworded only where they are shown.
const FROM_WORDS: Record<string, string> = {
  "Group Key": "Group",
  "Coverage Plan Id": "Plan ID",
  "Product Code": "Product ID",
  "Address Zip": "ZIP",
  "Address City": "City",
  "Address State": "State",
};

/** "Coverage Plan Id: 3 combinations" as "Plan ID: 3 combinations"; a FACETS
 *  column named by its code ("CSPI_ID: ...") by its business name. */
function fromWords(stated: string): string {
  const at = stated.indexOf(":");
  const label = at < 0 ? stated : stated.slice(0, at);
  const rest = at < 0 ? "" : stated.slice(at);
  const word = FROM_WORDS[label] ?? (/^[A-Z0-9_]+$/.test(label) ? columnName(label) : label);
  return word + rest;
}

function featureLabel(feature: string): string {
  return FEATURE_WORDS[feature] ?? columnName(feature);
}

/** The two largest shares, which is what distinguishes one cluster from another,
 *  as [value, "27%"] pairs: each shown as its own unit, so "1 27% 5 21%" never
 *  reads as one run of numbers.
 *
 *  The member analyzer nests them under `pct`; the claims analyzer gives a flat
 *  mapping of value to share. Both mean the same thing, and reading only the
 *  first left every claims pattern showing a label with nothing beside it. */
function topShares(dist: CategoricalDist | Record<string, number>): [string, string][] {
  const shares: Record<string, number> =
    (dist as CategoricalDist)?.pct ??
    (Object.fromEntries(
      Object.entries(dist ?? {}).filter(([, v]) => typeof v === "number"),
    ) as Record<string, number>);
  const total = Object.values(shares).reduce((sum, v) => sum + v, 0) || 1;
  return Object.entries(shares)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 2)
    .map(([key, share]): [string, string] => [key, `${Math.round((share / total) * 100)}%`]);
}


/** Size bands, as each pattern's card names its own. */
const BANDS: [string, string][] = [
  ["very high", "Very high"],
  ["high", "High"],
  ["medium", "Medium"],
  ["low", "Low"],
  ["rare", "Rare"],
];

type Tool = "criteria" | "scan" | "file";
type Show = "all" | "unsaved" | "saved";

/** A row of filter chips: toggle buttons under one small label. */
function ChipRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="findchips" role="group" aria-label={label}>
      <span className="findchips__label">{label}</span>
      {children}
    </div>
  );
}

function FilterChip({
  on, onClick, children, disabled, title,
}: {
  on: boolean;
  onClick: () => void;
  children: React.ReactNode;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      className={"findchip" + (on ? " findchip--on" : "")}
      aria-pressed={on}
      onClick={onClick}
      disabled={disabled}
      title={title}
    >
      {children}
    </button>
  );
}

/**
 * Find: every way to a pattern on one screen.
 *
 * The whole book's patterns show on open. One box searches them by name and
 * value and, submitted, reads a requirement. Chips filter by subject, size and
 * saved; Criteria, Full book scan and Requirements file are chips too, each
 * showing its results in the same place. Saved patterns list beneath.
 */
export function Patterns({
  onApplied,
  domain,
  onDomain,
  claimsAvailable = true,
  childCheckError = null,
  onRetryChildCheck,
  onConfigure,
}: Props & {
  onDomain?: (domain: Domain) => void;
  claimsAvailable?: boolean;
  childCheckError?: string | null;
  onRetryChildCheck?: () => void;
  onConfigure?: () => void;
}) {
  const [text, setText] = useState("");
  // The search text submitted: read as a requirement, its patterns shown.
  const [requirement, setRequirement] = useState<string | null>(null);
  // The subject a requirement was read as (claims, say), shown on the Subject
  // chip while it is open; closed, the subject chosen is back.
  const [readAs, setReadAs] = useState<Domain | null>(null);
  useEffect(() => {
    if (!requirement) setReadAs(null);
  }, [requirement]);
  const subject = requirement && readAs ? readAs : domain;
  const [tool, setTool] = useState<Tool | null>(null);
  const [show, setShow] = useState<Show>("all");
  const [bands, setBands] = useState<Set<string>>(new Set());

  const [saved, setSaved] = useState<SavedPattern[] | null>(null);
  const [savedError, setSavedError] = useState<string | null>(null);
  const loadSaved = useCallback(async () => {
    try {
      setSaved((await api.saved()).patterns);
      setSavedError(null);
    } catch (e) {
      setSavedError(e instanceof ApiError ? e.message : String(e));
    }
  }, []);
  useEffect(() => {
    void loadSaved();
  }, [loadSaved]);
  const mine = useMemo(
    () => (saved ?? []).filter((s) => patternKey(s.entity) === domain),
    [saved, domain],
  );
  // Saved patterns, by what they were saved from where the server says (a
  // pattern saved as chosen records is not the whole one), else by name.
  const savedKeys = useMemo(() => {
    if (!mine.some((s) => s.saved_from !== undefined)) return null;
    return new Set(
      mine
        .filter((s) => s.saved_from && !s.saved_from.records)
        .map((s) => {
          const from = s.saved_from!;
          return patternIdentity(from.domain, from.filters, from.k, from.cluster_id, from.claim_type);
        }),
    );
  }, [mine]);
  // Saved before SDM recorded what a pattern was saved from: by name.
  const savedNames = useMemo(
    () => new Set(mine.filter((s) => s.saved_from == null).map((s) => s.name)),
    [mine],
  );
  // Found patterns saved during this visit stay where they are under Not saved,
  // so a card being written to a file does not vanish part way.
  const [keptHere, setKeptHere] = useState<Set<string>>(new Set());
  const savedHere = useCallback(
    (identity?: string) => {
      if (identity) setKeptHere((previous) => new Set(previous).add(identity));
      void loadSaved();
    },
    [loadSaved],
  );
  // Find pressed again on the same text reads it again.
  const [asked, setAsked] = useState(0);

  const find = () => {
    const wanted = text.trim();
    setTool(null);
    // A new requirement is read afresh: the subject an earlier one was read
    // as is not this one's.
    setReadAs(null);
    setRequirement(wanted || null);
    setAsked((n) => n + 1);
    if (!wanted) setShow("all");
  };
  const pickTool = (next: Tool) => {
    setRequirement(null);
    setTool(next);
  };
  const pickShow = (next: Show) => {
    setRequirement(null);
    setTool(null);
    setShow(next);
  };
  const toggleBand = (band: string) => {
    setRequirement(null);
    setTool(null);
    setBands((previous) => {
      const next = new Set(previous);
      if (next.has(band)) next.delete(band);
      else next.add(band);
      return next;
    });
  };
  const browsing = !tool && !requirement;

  return (
    <div className="find">
      <form
        className="find__bar"
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          find();
        }}
      >
        <input
          type="search"
          aria-label="Search or requirement"
          placeholder="Search patterns or enter a requirement"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            if (!e.target.value.trim()) setRequirement(null);
          }}
        />
        <Button variant="primary" type="submit">
          Find
        </Button>
      </form>

      <div className="find__chips">
        <ChipRow label="Subject">
          {patternKeys().map((option) => (
            <FilterChip
              key={option}
              on={subject === option}
              onClick={() => {
                // Another subject closes an open requirement, and its sentence
                // leaves the box with it: left there, it would search the list.
                if (option !== subject && requirement !== null) {
                  setRequirement(null);
                  setText("");
                }
                onDomain?.(option);
              }}
              disabled={isChild(option) && !claimsAvailable}
              title={
                isChild(option) && !claimsAvailable
                  ? childCheckError
                    ? `Couldn't check for ${termsOf(option).many} in Pattern data: ${childCheckError}`
                    : `No ${termsOf(option).many} in Pattern data`
                  : undefined
              }
            >
              {termsOf(option).Many}
            </FilterChip>
          ))}
        </ChipRow>
        {childCheckError &&
          patternKeys()
            .filter((option) => isChild(option))
            .map((option) => (
              <Banner key={option} tone="caution">
                {`Couldn't check for ${termsOf(option).many} in Pattern data, so they can't be chosen: ${childCheckError} `}
                {onRetryChildCheck && <Button onClick={onRetryChildCheck}>Try again</Button>}
              </Banner>
            ))}
        <ChipRow label="Show">
          <FilterChip on={browsing && show === "all"} onClick={() => pickShow("all")}>
            All
          </FilterChip>
          <FilterChip on={browsing && show === "unsaved"} onClick={() => pickShow("unsaved")}>
            Not saved
          </FilterChip>
          <FilterChip on={browsing && show === "saved"} onClick={() => pickShow("saved")}>
            {saved ? `Saved (${mine.length})` : "Saved"}
          </FilterChip>
        </ChipRow>
        <ChipRow label="Size">
          {BANDS.map(([band, label]) => (
            <FilterChip key={band} on={bands.has(band)} onClick={() => toggleBand(band)}>
              {label}
            </FilterChip>
          ))}
        </ChipRow>
        <ChipRow label="More">
          <FilterChip on={tool === "criteria"} onClick={() => pickTool("criteria")}>
            Criteria
          </FilterChip>
          <FilterChip on={tool === "scan"} onClick={() => pickTool("scan")}>
            Full book scan
          </FilterChip>
          <FilterChip on={tool === "file"} onClick={() => pickTool("file")}>
            Requirements file
          </FilterChip>
        </ChipRow>
      </div>
      {/* Once for the screen, not beside every Load to database. */}
      <NoTargetNote />

      {requirement ? (
        <StoryAnalysis
          key={`${requirement}#${asked}`}
          story={requirement}
          onApplied={onApplied}
          onSaved={loadSaved}
          onDomain={setReadAs}
          onClose={() => {
            setRequirement(null);
            setText("");
          }}
        />
      ) : tool === "criteria" ? (
        <DefinePattern key={domain} onApplied={onApplied} domain={domain} onSaved={loadSaved} />
      ) : tool === "scan" ? (
        <Discover
          key={domain}
          domain={domain}
          onApplied={onApplied}
          query={text}
          onSaved={loadSaved}
        />
      ) : tool === "file" ? (
        <RequirementsFile
          onAnalyze={(story) => {
            setText(story);
            setTool(null);
            setReadAs(null);
            setRequirement(story);
            setAsked((n) => n + 1);
          }}
        />
      ) : (
        <>
          {show !== "saved" && (
            <PatternsAnalyze
              key={domain}
              autoRun
              onApplied={onApplied}
              domain={domain}
              onConfig={onConfigure}
              query={text}
              bands={bands}
              hide={show === "unsaved"
                ? {
                    keys: savedKeys,
                    names: savedNames,
                    except: keptHere,
                    pending: saved === null && savedError === null,
                    failed: saved === null ? savedError : null,
                  }
                : undefined}
              onSaved={savedHere}
              onRequirement={find}
            />
          )}
          {show !== "unsaved" && (
            <SavedPatterns
              // Its own key: PatternsAnalyze beside it is keyed by the subject too.
              key={`saved-${domain}`}
              domain={domain}
              saved={saved === null ? null : mine}
              error={savedError}
              reload={loadSaved}
              onApplied={onApplied}
              query={text}
              onShowAll={() => pickShow("all")}
            />
          )}
        </>
      )}
    </div>
  );
}
