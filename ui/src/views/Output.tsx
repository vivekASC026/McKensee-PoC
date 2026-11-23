/**
 * File Generation: the shape data leaves in.
 *
 * A layout says how a row is written — fixed-width columns, a delimiter, which
 * rule fills each field. A rule says what fills one. Together they are
 * everything needed to write a file.
 *
 * Every file is written for someone: the plan's run, or a pattern. For a
 * pattern, members are new people carrying its values and claims belong to
 * the members it holds. The choice sits above the tabs because it applies to
 * every format beneath them.
 *
 * A saved layout writes files at once. Where a second approver is configured
 * (Settings › Layout approval), only an approved one does, and the approval is
 * shown with who did it.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Banner, Button, Chip, Empty } from "../components/primitives";
import { EdiFormats } from "./EdiFormats";
import { FromSample, MixInput } from "./FromSample";
import { Scenarios } from "./Scenarios";
import { asFilePattern } from "./Requests";
import { LayoutDesigner } from "./LayoutDesigner";
import { LayoutEditor, aliasNote } from "./LayoutEditor";
import { FieldsTable, layoutKindName, layoutStatusName } from "./LayoutFields";
import {
  ApiError,
  REQUEST_WRITTEN,
  api,
  patternRequest,
  countRequest,
  type ClaimShape,
  type ClaimsCheckPayload,
  type ClaimsChoice,
  type CountChoice,
  type Domain,
  type FilePattern,
  type GenerationRule,
  type HandedSample,
  type Layout,
  type FilePatternOption,
  type LayoutParsePayload,
} from "../lib/api";
import "./Output.css";
import { fromPatterns, hasModule, isChild, isPrimary, keyOf, lineOf, patternKey, patternKeys, phrase, roleOf, termsOf, words } from "../lib/pack";

interface OutputProps {
  pattern: FilePattern | null;
  onPattern: (pattern: FilePattern | null) => void;
  // False where this installation makes files only: there is no plan, so
  // "the plan's run" would only answer with an error.
  planRun?: boolean;
  /** The format tab open, held by the shell so History can open X12 EDI. */
  tab: FileTab;
  onTab: (tab: FileTab) => void;
  /** A sample uploaded elsewhere, to be read on its format's tab. */
  handed?: HandedSample | null;
  onHand: (sample: HandedSample) => void;
  onHanded: () => void;
}

/** The tab a sample of each format is read on. */
export function sampleTab(format: string): FileTab {
  return format === "fhir" || format === "hl7v2" || format === "cda" ? format : "edi-sample";
}

export type FileTab = "layouts" | "edi" | "fhir" | "hl7v2" | "cda" | "edi-sample";

/** Each format tab, and the business pack's modules that offer it: the
 *  health formats appear only where the pack offers them. */
const FILE_TABS: readonly (readonly [FileTab, string, readonly string[]])[] = [
  ["layouts", "Flat file", []],
  ["edi", "X12 EDI", ["x12"]],
  ["fhir", "FHIR", ["fhir"]],
  ["hl7v2", "HL7 v2", ["hl7v2"]],
  ["cda", "HL7 v3", ["cda", "hl7v3"]],
  ["edi-sample", "Custom format", ["x12", "fhir", "hl7v2", "cda", "hl7v3"]],
];

/** What a sample may be, as the pack offers it: "Flat file, X12, FHIR, HL7 v2
 *  or HL7 v3; the format is detected." for health. */
function sampleKinds(): string {
  const kinds = [
    "Flat file",
    ...fileTabs()
      .filter(([id]) => id !== "layouts" && id !== "edi-sample")
      .map(([id, label]) => (id === "edi" ? "X12" : label)),
  ];
  if (kinds.length === 1) return "A flat file: delimited or fixed width.";
  return `${kinds.slice(0, -1).join(", ")} or ${kinds[kinds.length - 1]}; the format is detected.`;
}

/** The format tabs the active pack offers. */
export function fileTabs(): (readonly [FileTab, string, readonly string[]])[] {
  return FILE_TABS.filter(([, , modules]) => modules.length === 0 || modules.some(hasModule));
}

/**
 * Map and Review, for files: the shape of a file and the rule behind each field.
 *
 * Defining a layout -- from a sample, or by hand -- and choosing what fills
 * each field is mapping, the same job Map and Review does for a database's
 * columns. The rules themselves are one list for everything, under Settings:
 * a second copy of that screen here, under another name, read as a second
 * set of rules.
 */
export function FileMapping({
  onRules, onHand,
}: {
  onRules: () => void;
  /** A sample that is not a flat file: read where files are generated. */
  onHand: (sample: HandedSample) => void;
}) {
  return (
    <div className="output">
      <header className="output__head">
        <h2>File layouts</h2>
        <p className="output__lede">
          Each field is filled by the rule for its element, managed in{" "}
          <button type="button" className="output__link" onClick={onRules}>
            Settings › Rules
          </button>
          .
        </p>
      </header>
      <Layouts writing={false} onRules={onRules} extra={{}} onHand={onHand} />
    </div>
  );
}

/**
 * Generation, for files: writing them, for the plan's run or for a pattern.
 *
 * Every file is written for someone: the plan's run, or a pattern. For a
 * pattern, members are new people carrying its values and claims belong to
 * the members it holds. The choice sits above the tabs because it applies to
 * every format beneath them.
 */
export function FileGeneration({
  pattern, onPattern, planRun = true, tab: asked, onTab: setTab, handed, onHand, onHanded,
}: OutputProps) {
  const C = words("child");
  const tabs = fileTabs();
  // A tab the pack does not offer (remembered, or opened from History) is the flat file's.
  const tab: FileTab = tabs.some(([id]) => id === asked) ? asked : "layouts";
  // A sample handed over in a format the pack does not offer is said so, not
  // left unread (the server refuses such a sample; this is the screen's half).
  const [refused, setRefused] = useState<string | null>(null);
  useEffect(() => {
    if (!handed) return;
    if (tabs.some(([id]) => id === sampleTab(handed.format))) {
      setRefused(null);
      return;
    }
    setRefused(`${handed.name} is in a format the business pack does not offer.`);
    onHanded();
    // Only when a sample is handed over.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handed]);
  // How many new members a member file for a pattern carries. Claims are not
  // counted here: they belong to the members the pattern already holds.
  const [newMembers, setNewMembers] = useState(100);
  // The run the next download is written under: the moment it started is in
  // its seed, so every download is a new run of new people. The same people
  // again -- an 834's members in a FHIR file -- is Options > Same members as
  // run, which writes under that run instead.
  const [started, setStarted] = useState(() => new Date().toISOString());
  // The run the last download was recorded as.
  const [lastRun, setLastRun] = useState<number | null>(null);
  const forWhom = patternIdentity(pattern);
  // An earlier run chosen: more files for its people, under it.
  const earlier = pattern?.kind === "request" ? pattern : null;
  useEffect(() => {
    setStarted(earlier ? earlier.started : new Date().toISOString());
    setLastRun(null);
    if (earlier) setNewMembers(earlier.members);
    // Only when who the files are for changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [forWhom]);
  // Claims for an earlier request's members: a claims pattern, which members
  // have claims, and the claims' own request.
  const [claims, setClaims] = useState<ClaimsChoice>(() => freshClaims());
  useEffect(() => setClaims(freshClaims()), [forWhom]);
  useEffect(() => {
    const kept = (event: Event) => {
      const id = (event as CustomEvent<{ id: number }>).detail.id;
      const now = new Date().toISOString();
      // Claims for a run's members are a run of their own, linked to it; the
      // next claims file starts another.
      if (earlier && id !== earlier.requestId) {
        setClaims((c) => ({ ...c, requestId: id, started: now }));
        return;
      }
      setLastRun(id);
      // The next download is a new run, unless it is for an earlier run.
      if (!earlier) setStarted(now);
    };
    window.addEventListener(REQUEST_WRITTEN, kept);
    return () => window.removeEventListener(REQUEST_WRITTEN, kept);
  }, [earlier]);
  // The claims pattern whose claims a claims file is written from: the one
  // chosen for an earlier request's members, or the claims pattern written for.
  const claimsPattern =
    isPrimary(earlier?.entity)
      ? claims.patternId
      : pattern?.kind === "saved" && isChild(pattern.entity)
        ? pattern.id
        : null;
  const childWritten =
    isPrimary(earlier?.entity) ||
    (pattern?.kind === "saved" && isChild(pattern.entity)) ||
    (pattern?.kind === "live" && isChild(pattern.domain));
  // How many claims its members have and lines its claims: "as measured" is
  // this, read once and kept with the request.
  const [measured, setMeasured] = useState<ClaimShape | null>(null);
  const [measuring, setMeasuring] = useState(false);
  // Measuring failing is said: "As measured" then has no counts to send, and
  // the file falls back to the pattern average.
  const [measureError, setMeasureError] = useState<string | null>(null);
  useEffect(() => {
    setMeasured(null);
    setMeasureError(null);
    if (claimsPattern === null || !fromPatterns()) return;
    let live = true;
    setMeasuring(true);
    api
      .claimShape(claimsPattern)
      .then((shape) => live && setMeasured(shape))
      .catch((e: unknown) => {
        if (!live) return;
        setMeasured(null);
        setMeasureError(e instanceof ApiError ? e.message : String(e));
      })
      .finally(() => live && setMeasuring(false));
    return () => {
      live = false;
    };
  }, [claimsPattern]);
  const shape = childWritten
    ? {
        claimed_share: claims.everyone ? 100 : claims.share,
        ...countRequest("claims", claims.claims, measured?.claims_each ?? null),
        ...countRequest("lines", claims.lines, measured?.lines_each ?? null),
      }
    : {};
  const extra = earlier
    ? {
        ...patternRequest(pattern, newMembers),
        ...(isPrimary(earlier.entity)
          ? {
              of_request: earlier.requestId,
              pattern_id: claims.patternId ?? undefined,
              request_started: claims.started,
              ...shape,
            }
          : {}),
      }
    : { ...patternRequest(pattern, newMembers), request_started: started, ...shape };

  return (
    <div className="output">
      <header className="output__head">
        <h2>File generation</h2>
      </header>
      {refused && <Banner tone="caution">{refused}</Banner>}

      <ForWhom
        pattern={pattern}
        onPattern={onPattern}
        planRun={planRun}
        newMembers={newMembers}
        onNewMembers={setNewMembers}
        lastRun={lastRun}
      />

      {measureError && childWritten && (
        <Banner tone="caution">
          {`Couldn't measure the ${C.many} pattern, so "As measured in pattern" uses the pattern average: ${measureError} ` +
            "Choose Fixed, Range or Distribution to set the counts yourself, or try again later."}
        </Banner>
      )}
      {earlier && isPrimary(earlier.entity) && (
        <ClaimsFor request={earlier} claims={claims} onClaims={setClaims} measured={measured} measuring={measuring} />
      )}
      {!earlier && childWritten && (
        <section className="claimsfor">
          <h3 className="panel__title">{C.One} settings</h3>
          <ClaimsShape claims={claims} onClaims={setClaims} measured={measured} measuring={measuring} />
        </section>
      )}

      {tabs.length > 1 && (
        <nav className="tabs" role="tablist">
          {tabs.map(([id, label]) => (
            <button
              key={id}
              role="tab"
              aria-selected={tab === id}
              className={"tabs__tab" + (tab === id ? " tabs__tab--on" : "")}
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
        </nav>
      )}

      {tab === "layouts" && <Layouts writing extra={extra} onHand={onHand} />}
      {tab === "edi" && <EdiFormats extra={extra} pattern={pattern} />}
      {(tab === "fhir" || tab === "hl7v2" || tab === "cda") && (
        <FromSample key={tab} extra={extra} pattern={pattern} family={tab}
          handed={handed && sampleTab(handed.format) === tab ? handed : null}
          onHanded={onHanded} />
      )}
      {tab === "edi-sample" && (
        <FromSample extra={extra} pattern={pattern}
          handed={handed && sampleTab(handed.format) === tab ? handed : null}
          onHanded={onHanded} />
      )}
    </div>
  );
}

function freshClaims(): ClaimsChoice {
  return {
    patternId: null, everyone: true, share: 60,
    claims: { mode: "measured", low: 1, high: 3, mix: null },
    lines: { mode: "measured", low: 1, high: 4, mix: null },
    started: new Date().toISOString(), requestId: null,
  };
}

/**
 * Which members have claims, how many claims each, how many lines each claim:
 * as the claims pattern measured them (its own claims' mix), exactly so many,
 * from one count to another, or a mix of one's own.
 */
function ClaimsShape({
  claims, onClaims, measured, measuring,
}: {
  claims: ClaimsChoice;
  onClaims: (claims: ClaimsChoice) => void;
  measured: ClaimShape | null;
  measuring: boolean;
}) {
  const P = words("primary");
  const C = words("child");
  const set = (change: Partial<ClaimsChoice>) => onClaims({ ...claims, ...change });
  return (
    <div className="claimsfor__pick">
      <label className="named">
        <span>{P.Many} with {C.many}</span>
        <select
          value={claims.everyone ? "all" : "share"}
          onChange={(e) => set({ everyone: e.target.value === "all" })}
        >
          <option value="all">All {P.many}</option>
          <option value="share">A percentage of {P.many}</option>
        </select>
      </label>
      {!claims.everyone && (
        <label className="named">
          <span>Percentage of {P.many} (%)</span>
          <input
            type="number"
            min={1}
            max={100}
            value={claims.share}
            onChange={(e) =>
              set({ share: Math.min(100, Math.max(1, Number(e.target.value) || 1)) })}
          />
        </label>
      )}
      <CountInput
        label={`${C.Many} per ${P.one}`}
        count={claims.claims}
        measured={measured?.claims_each ?? null}
        measuring={measuring}
        onCount={(count) => set({ claims: count })}
      />
      <CountInput
        label={`Lines per ${C.one}`}
        count={claims.lines}
        measured={measured?.lines_each ?? null}
        measuring={measuring}
        onCount={(count) => set({ lines: count })}
      />
    </div>
  );
}

function CountInput({
  label, count, measured, measuring, onCount,
}: {
  label: string;
  count: CountChoice;
  measured: [number, number][] | null;
  measuring: boolean;
  onCount: (count: CountChoice) => void;
}) {
  const C = words("child");
  const set = (change: Partial<CountChoice>) => onCount({ ...count, ...change });
  const whole = (text: string) => Math.min(50, Math.max(1, Number(text) || 1));
  return (
    <div className="claimsfor__count">
      <label className="named">
        <span>{label}</span>
        <select
          value={count.mode}
          onChange={(e) => {
            const mode = e.target.value as CountChoice["mode"];
            set({ mode, mix: mode === "mix" ? count.mix ?? measured ?? [[1, 60], [2, 30], [3, 10]]
                                            : count.mix });
          }}
        >
          <option value="measured">As measured in pattern</option>
          <option value="exactly">Fixed</option>
          <option value="range">Range</option>
          <option value="mix">Distribution</option>
        </select>
      </label>
      {count.mode === "measured" && (
        <span className="panel__hint mono">
          {measured ? measured.map(([n, s]) => `${n}:${s}`).join(", ")
            : measuring ? `Measuring pattern ${C.many}…` : "Pattern average"}
        </span>
      )}
      {count.mode === "exactly" && (
        <input type="number" min={1} max={50} aria-label={`${label}, fixed`}
               value={count.low} onChange={(e) => set({ low: whole(e.target.value) })} />
      )}
      {count.mode === "range" && (
        <span>
          <input type="number" min={1} max={50} aria-label={`${label}, minimum`}
                 value={count.low} onChange={(e) => set({ low: whole(e.target.value) })} />
          {" to "}
          <input type="number" min={1} max={50} aria-label={`${label}, maximum`}
                 value={count.high} onChange={(e) => set({ high: whole(e.target.value) })} />
        </span>
      )}
      {count.mode === "mix" && (
        <MixInput label={`${label}, distribution`} mix={count.mix} onMix={(mix) => set({ mix })} />
      )}
    </div>
  );
}

const FEATURE_NAMES: Record<string, string> = { SEX: "Gender", DOB: "Age" };

/**
 * Claims for an earlier request's members: a claims pattern, superseded by
 * the members wherever both say something of a member, and which of them
 * have claims. Before anything is written it says what the members override
 * and how many fall outside what the claims pattern was found on.
 */
function ClaimsFor({
  request, claims, onClaims, measured, measuring,
}: {
  request: Extract<FilePattern, { kind: "request" }>;
  claims: ClaimsChoice;
  onClaims: (claims: ClaimsChoice) => void;
  measured: ClaimShape | null;
  measuring: boolean;
}) {
  const P = words("primary");
  const C = words("child");
  const [patterns, setPatterns] = useState<FilePatternOption[]>([]);
  const [check, setCheck] = useState<ClaimsCheckPayload | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const set = (change: Partial<ClaimsChoice>) => onClaims({ ...claims, ...change });

  useEffect(() => {
    let live = true;
    if (!fromPatterns()) return;
    api
      .patternsForFiles()
      .then((p) => live && setPatterns(p.patterns.filter((x) => isChild(x.domain))))
      .catch((e: unknown) => {
        if (!live) return;
        setPatterns([]);
        setListError(e instanceof ApiError ? e.message : String(e));
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    setCheck(null);
    setError(null);
    if (claims.patternId === null) return;
    let live = true;
    setChecking(true);
    api
      .claimsCheck(request.requestId, claims.patternId)
      .then((payload) => live && setCheck(payload))
      .catch((e) => live && setError(e instanceof ApiError ? e.message : String(e)))
      .finally(() => live && setChecking(false));
    return () => {
      live = false;
    };
  }, [request.requestId, claims.patternId]);

  return (
    <section className="claimsfor">
      <h3 className="panel__title">{C.Many} for run {P.many}</h3>
      <div className="claimsfor__pick">
        <label className="named">
          <span>{C.Many} pattern</span>
          <select
            value={claims.patternId ?? ""}
            onChange={(e) =>
              set({ patternId: e.target.value ? Number(e.target.value) : null, requestId: null,
                    started: new Date().toISOString() })}
          >
            <option value="">Select a {C.many} pattern</option>
            {patterns.map((p) => (
              <option key={p.pattern_id} value={p.pattern_id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <ClaimsShape claims={claims} onClaims={onClaims} measured={measured} measuring={measuring} />

      {listError && (
        <Banner tone="alarm">{`Couldn't load the ${C.many} patterns: ${listError}`}</Banner>
      )}
      {error && <Banner tone="alarm">{error}</Banner>}
      {checking && <p className="panel__hint">Comparing run {P.many} with the {C.many} pattern…</p>}
      {check && (
        <>
          <ul className="claimsfor__features">
            {check.features.map((f) => (
              <li key={f.element}>
                <strong>{FEATURE_NAMES[f.element] ?? f.element}</strong>: {C.many} pattern{" "}
                {f.pattern}; run {P.many} {f.request}. {P.One} values take precedence.
              </li>
            ))}
          </ul>
          {check.outside > 0 && (
            <Banner tone="caution">
              {check.features
                .filter((f) => f.outside > 0)
                .map((f) => `${f.outside} of ${check.members} ${P.many} fall outside the ${C.many} ` +
                  `pattern's ${(FEATURE_NAMES[f.element] ?? f.element).toLowerCase()} range (${f.pattern})`)
                .join("; ")}
              . Their {C.many} retain the {P.many}&apos; values; {phrase("outside_range_risk")}
            </Banner>
          )}
        </>
      )}

      {claims.requestId !== null && (
        <p className="forwhom__recorded" role="status">
          {`Recorded as run ${claims.requestId}, linked to run ${request.requestId}`}
        </p>
      )}
    </section>
  );
}

/** What a request is for, as far as starting a new one goes. */
function patternIdentity(pattern: FilePattern | null): string {
  if (pattern === null) return "plan";
  if (pattern.kind === "saved") return `saved:${pattern.id}`;
  if (pattern.kind === "request") return `request:${pattern.requestId}`;
  if (pattern.kind === "scenarios") {
    return "scenarios:" + pattern.scenarios.map((s) => `${s.pattern_id}x${s.count}`).join("-");
  }
  return `live:${pattern.domain}:${JSON.stringify(pattern.filters)}:${pattern.k}:` +
    `${pattern.pattern.cluster_id}:${pattern.claimType}`;
}

function entityName(entity: string): string {
  return roleOf(entity) ? termsOf(entity).Many : entity;
}

function moment(iso: string): string {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
}

/* ------------------------------------------------------------------ */

function ForWhom({
  pattern, onPattern, planRun, newMembers, onNewMembers, lastRun,
}: {
  pattern: FilePattern | null;
  onPattern: (pattern: FilePattern | null) => void;
  planRun: boolean;
  newMembers: number;
  onNewMembers: (count: number) => void;
  lastRun: number | null;
}) {
  const P = words("primary");
  const C = words("child");
  const [saved, setSaved] = useState<FilePatternOption[]>([]);
  // A saved pattern split into scenarios here, for an EDI or flat file of
  // only the chosen ones -- the same split Patterns offers.
  const [splitting, setSplitting] = useState<Extract<FilePattern, { kind: "saved" }> | null>(null);
  const [split, setSplit] = useState<string[] | null>(null);
  // Earlier runs: more files for the people one of them already has.
  const [earlier, setEarlier] = useState<FilePattern[]>([]);
  const [showOptions, setShowOptions] = useState(false);
  // What the files were for before an earlier run was chosen, to go back to.
  const before = useRef<FilePattern | null>(null);
  // A list that failed to load is said, not shown as an empty one.
  const [savedError, setSavedError] = useState<string | null>(null);
  const [earlierError, setEarlierError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    const said = (e: unknown) => (e instanceof ApiError ? e.message : String(e));
    setSavedError(null);
    setEarlierError(null);
    // Where the pack writes nothing from a pattern, no file is for one.
    if (fromPatterns()) {
      api
        .patternsForFiles()
        .then((payload) => live && setSaved(payload.patterns))
        .catch((e: unknown) => {
          if (!live) return;
          setSaved([]);
          setSavedError(said(e));
        });
    } else {
      setSaved([]);
    }
    api
      .generationRequests()
      .then((payload) => live && setEarlier(
        payload.requests
          .filter((r) => r.pattern_key !== "plan" && r.subject !== "load").map(asFilePattern)))
      .catch((e: unknown) => {
        if (!live) return;
        setEarlier([]);
        setEarlierError(said(e));
      });
    return () => {
      live = false;
    };
  }, [lastRun]);

  // No plan here: a file is for a pattern, the latest saved one until
  // another is chosen.
  useEffect(() => {
    if (planRun || pattern !== null || saved.length === 0 || !fromPatterns()) return;
    const row = saved[0];
    onPattern({
      kind: "saved", id: row.pattern_id, name: row.name, entity: row.domain,
      members: row.member_count,
    });
  }, [planRun, pattern, saved, onPattern]);

  const value =
    pattern === null
      ? ""
      : pattern.kind === "saved"
        ? `saved:${pattern.id}`
        : pattern.kind === "request"
          ? `request:${pattern.requestId}`
          : pattern.kind === "scenarios"
            ? "scenarios"
            : "live";
  const isEarlier = (p: FilePattern): p is Extract<FilePattern, { kind: "request" }> =>
    p.kind === "request";
  const claimLines = pattern?.kind === "live" && isChild(pattern.domain);

  return (
    <section className="forwhom">
      <label className="named">
        <span>Pattern</span>
        <select
          aria-label="Pattern"
          value={value}
          onChange={(e) => {
            const chosen = e.target.value;
            setSplitting(null);
            if (!chosen) return onPattern(null);
            if (chosen === "live" || chosen === "scenarios") return;
            if (chosen.startsWith("request:")) return;
            const id = Number(chosen.split(":")[1]);
            const row = saved.find((p) => p.pattern_id === id);
            if (row) {
              onPattern({
                kind: "saved",
                id,
                name: row.name,
                entity: row.domain,
                members: row.member_count,
              });
            }
          }}
        >
          {planRun ? (
            <option value="">Plan</option>
          ) : (
            pattern === null && <option value="">Select a pattern</option>
          )}
          {pattern?.kind === "live" && (
            <option value="live">{pattern.name} (unsaved)</option>
          )}
          {pattern?.kind === "scenarios" && (
            <option value="scenarios">{pattern.name}</option>
          )}
          {/* Chosen on Patterns: named at once, before the saved list arrives. */}
          {pattern?.kind === "saved" && !saved.some((p) => p.pattern_id === pattern.id) && (
            <option value={`saved:${pattern.id}`}>
              {pattern.name} ({entityName(pattern.entity)})
            </option>
          )}
          {saved.length > 0 && (
            <optgroup label="Saved patterns">
              {saved.map((p) => (
                <option key={p.pattern_id} value={`saved:${p.pattern_id}`}>
                  {p.name} ({entityName(p.domain)})
                </option>
              ))}
            </optgroup>
          )}
          {pattern?.kind === "request" && (
            <option value={`request:${pattern.requestId}`}>{pattern.name}</option>
          )}
        </select>
      </label>

      {pattern && pattern.kind !== "request" && (
        <label className="named">
          <span>New {P.many}</span>
          <input
            type="number"
            min={1}
            max={100000}
            value={newMembers}
            onChange={(e) => onNewMembers(Math.max(1, Number(e.target.value) || 1))}
          />
        </label>
      )}

      {pattern?.kind === "saved" && splitting === null && (
        <Button onClick={() => { setSplit(null); setSplitting(pattern); }}>
          Scenarios
        </Button>
      )}

      {savedError && (
        <Banner tone="alarm">{`Couldn't load the saved patterns: ${savedError}`}</Banner>
      )}
      {earlierError && (
        <Banner tone="caution">
          {`Couldn't load earlier runs, so "Same ${P.many} as run" is not offered: ${earlierError}`}
        </Banner>
      )}
      <p className="panel__hint forwhom__hint">
        {pattern === null && !planRun && savedError
          ? "Saved patterns could not be loaded. Reopen this screen to try again."
          : pattern === null && !planRun
          ? "Save a pattern on the Patterns screen to generate files from it."
          : pattern === null
          ? `${P.Many} and ${C.many} from the plan, the same as Load to database writes.`
          : pattern.kind === "request"
          ? `Same ${pattern.members.toLocaleString()} ${termsOf(pattern.entity).many} as run ${pattern.requestId}, ` +
            "in every format, regardless of later changes to rules or the pattern."
          : `${P.One} files contain ${newMembers.toLocaleString()} new ${P.many} generated with this pattern's values. ` +
            `${C.Many} files contain ${C.many} for the pattern's existing ${P.many}` +
            (claimLines ? "" : ` (${pattern.members.toLocaleString()})`) +
            ". Elements not in the pattern use their rules."}
      </p>

      {lastRun !== null && (
        <p className="forwhom__recorded" role="status">Recorded as run {lastRun}</p>
      )}

      {earlier.length > 0 && (
        <details
          className="forwhom__options"
          open={showOptions || pattern?.kind === "request"}
          onToggle={(e) => setShowOptions(e.currentTarget.open)}
        >
          <summary>Options</summary>
          <label className="named">
            <span>Same {P.many} as run</span>
            <select
              aria-label={`Same ${P.many} as run`}
              value={pattern?.kind === "request" ? String(pattern.requestId) : ""}
              onChange={(e) => {
                if (!e.target.value) {
                  onPattern(before.current);
                  return;
                }
                const found = earlier.filter(isEarlier)
                  .find((r) => r.requestId === Number(e.target.value));
                if (!found) return;
                if (pattern?.kind !== "request") before.current = pattern;
                onPattern(found);
              }}
            >
              <option value="">New {P.many} for each download</option>
              {pattern?.kind === "request" &&
                !earlier.filter(isEarlier).some((r) => r.requestId === pattern.requestId) && (
                  <option value={String(pattern.requestId)}>{pattern.name}</option>
                )}
              {earlier.filter(isEarlier).map((r) => (
                <option key={r.requestId} value={String(r.requestId)}>
                  {r.name} · {r.members.toLocaleString()} {r.entity} · {moment(r.started)}
                </option>
              ))}
            </select>
          </label>
        </details>
      )}

      {splitting && (
        <div className="forwhom__scenarios">
          <Scenarios
            domain={patternKey(splitting.entity)}
            name={splitting.name}
            filters={{}}
            k={null}
            clusterId={0}
            claimType={null}
            split={split}
            onSplit={setSplit}
            patternId={splitting.id}
            fileOnly
            onApplied={(_destination, chosen) => {
              if (chosen) onPattern(chosen);
              setSplitting(null);
            }}
          />
          <Button onClick={() => setSplitting(null)}>Close scenarios</Button>
        </div>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ */

function Layouts({
  onRules, extra, writing, onHand,
}: {
  onRules?: () => void;
  extra: Record<string, unknown>;
  // Writing files through layouts too (Generate › Files › Flat file), or only
  // defining them (Layouts and samples). Both define, edit and delete.
  writing: boolean;
  // A sample uploaded here that is not a flat file (X12, FHIR, HL7).
  onHand: (sample: HandedSample) => void;
}) {
  const [layouts, setLayouts] = useState<Layout[]>([]);
  const [rules, setRules] = useState<GenerationRule[]>([]);
  const [parsed, setParsed] = useState<LayoutParsePayload | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  // Members or claims: what one row of the file is.
  const [subject, setSubject] = useState<Domain>(() => keyOf("primary"));
  // The saved layout open for editing, if any.
  const [editing, setEditing] = useState<number | null>(null);
  // The saved layout whose Drop is waiting to be confirmed.
  const [dropping, setDropping] = useState<number | null>(null);
  // A layout must be approved to write files: only with a second approver.
  const [approvalRequired, setApprovalRequired] = useState(false);
  // Who approves: their name is typed to confirm an approval.
  const [approver, setApprover] = useState<string | null>(null);
  // Why the approval setting couldn't be read, when it couldn't.
  const [approverError, setApproverError] = useState<string | null>(null);
  // The layout whose Approve is waiting for the approver's name, and the name typed.
  const [approving, setApproving] = useState<number | null>(null);
  const [typed, setTyped] = useState("");
  // Where keyboard focus goes once a row's confirmation opens or closes.
  const [refocus, setRefocus] = useState<{ id: number; label: string } | null>(null);
  const cells = useRef(new Map<number, HTMLElement>());
  const [designing, setDesigning] = useState(false);
  // The files in the folder named on the Folder tab, any of which is a sample.
  const [folderFiles, setFolderFiles] = useState<string[]>([]);
  // A failed folder listing, said on its own: not an action's result.
  const [folderError, setFolderError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const listings = useRef(0);
  const listFolder = useCallback(() => {
    const mine = ++listings.current;
    api
      .fileSource()
      .then((source) => {
        if (mine !== listings.current) return;
        setFolderFiles(source.files.map((f) => f.name));
        setFolderError(null);
      })
      .catch((e: unknown) => {
        if (mine === listings.current) {
          setFolderError(`Couldn't list the folder's files: ${e instanceof ApiError ? e.message : String(e)}`);
        }
      });
  }, []);
  useEffect(() => {
    listFolder();
    return () => {
      listings.current += 1;
    };
  }, [listFolder]);

  const readFromFolder = useCallback(async (fileName: string) => {
    if (!fileName) return;
    setBusy("parse");
    setError(null);
    setSaved(null);
    try {
      const payload = await api.layoutParseFolderFile(fileName);
      setParsed(payload);
      setName(fileName.replace(/\.[^.]+$/, ""));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, []);

  const load = useCallback(async () => {
    // Who approves is read on its own: when it can't be read the layouts
    // still list, and Approve says the setting can't be read.
    void api
      .layoutApproval()
      .then((a) => {
        setApprover(a.secondApprover);
        setApproverError(null);
      })
      .catch((e: unknown) => {
        setApprover(null);
        setApproverError(e instanceof ApiError ? e.message : String(e));
      });
    try {
      const [l, r] = await Promise.all([api.layouts(), api.layoutRules()]);
      setLayouts(l.layouts);
      setApprovalRequired(l.approvalRequired === true);
      setRules(r.rules);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!refocus) return;
    const cell = cells.current.get(refocus.id);
    const target = refocus.label === "input"
      ? cell?.querySelector<HTMLElement>("input")
      : Array.from(cell?.querySelectorAll<HTMLButtonElement>("button") ?? []).find(
          (b) => b.textContent?.trim() === refocus.label,
        );
    target?.focus();
    setRefocus(null);
  }, [refocus]);

  const upload = useCallback(async (file: File) => {
    setBusy("parse");
    setError(null);
    setSaved(null);
    try {
      // The format is read off the sample: an X12, FHIR or HL7 file is read
      // where files are modeled on samples; a flat file is read as a layout.
      const text = await file.text();
      const found = await api.sampleDetect(text);
      if (found.format !== "flat") {
        onHand({ name: file.name, text, format: found.format });
        return;
      }
      const payload = await api.layoutParse(file);
      setParsed(payload);
      // A name is needed to save, and the file's own name is the one the
      // person already has in mind. They can change it before saving.
      setName(payload.sample_name?.replace(/\.[^.]+$/, "") ?? "");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [onHand]);

  const save = useCallback(async () => {
    if (!parsed || !name.trim()) return;
    setBusy("save");
    setError(null);
    try {
      const result = await api.saveLayout({
        name: name.trim(),
        kind: parsed.kind,
        delimiter: parsed.delimiter ?? null,
        fields: parsed.fields,
        sample_name: parsed.sample_name ?? null,
        purpose: "generation",
      });
      setSaved(
        `Saved "${result.name}"` +
        (approvalRequired ? ` (${layoutStatusName(result.status).toLowerCase()})` : "") +
        `, ${result.n_fields} fields` +
        (result.n_default ? `, ${result.n_default} left blank` : "") +
        (approvalRequired ? ". Approve it to generate files." : ".") +
        aliasNote(result),
      );
      setParsed(null);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [parsed, name, load, approvalRequired]);

  // The approver's name typed matches the one set: the server's rule (spaces
  // at either end ignored, Unicode's default lower-casing, as Python's lower()).
  const named = (approver ?? "").trim().toLowerCase();
  const confirmed = named !== "" && typed.trim().toLowerCase() === named;

  const approve = useCallback(
    async (id: number) => {
      setBusy(`approve-${id}`);
      setError(null);
      try {
        await api.approveLayout(id, typed.trim());
        setApproving(null);
        setTyped("");
        await load();
        setRefocus({ id, label: "Edit" });
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [load, typed],
  );

  const drop = useCallback(
    async (layout: Layout) => {
      setBusy(`drop-${layout.id}`);
      setError(null);
      setSaved(null);
      try {
        const result = await api.dropLayout(layout.id);
        setSaved(
          result.action === "archived"
            ? `${layout.name} deleted. Its approval record is kept.`
            : `${layout.name} deleted.`,
        );
        if (editing === layout.id) setEditing(null);
        setDropping(null);
        await load();
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [editing, load],
  );

  // A layout that cannot be written through is a description of a file nobody
  // can produce. The export already took a layout id; nothing offered one.
  const writeFile = useCallback(
    async (layout: Layout) => {
      setBusy(`write-${layout.id}`);
      setError(null);
      setSaved(null);
      try {
        // Only a comma-separated file is a .csv; pipe or tab is plain text.
        const comma = layout.kind !== "fixed" && (layout.delimiter ?? ",") === ",";
        const extension = comma ? "csv" : "txt";
        await api.exportDownload(
          layout.kind === "fixed" ? "fixed" : "csv",
          subject,
          `${layout.name.replace(/[^A-Za-z0-9._-]+/g, "-").toLowerCase()}.${extension}`,
          { layout_id: layout.id, ...extra },
        );
        setSaved(
          `File generated from layout ${layout.name}, one row per ` +
            `${lineOf(subject)}.`,
        );
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [subject, extra],
  );

  return (
    <section className="panel">
      {folderError && (
        <Banner tone="alarm">
          {`${folderError} `}
          <Button onClick={listFolder}>Try again</Button>
        </Banner>
      )}
      {error && <Banner tone="alarm">{error}</Banner>}
      {saved && <Banner tone="affirm">{saved}</Banner>}

      <>
      <div className="panel__actions">
        <input
          ref={input}
          type="file"
          accept=".txt,.csv,.dat,.tsv,.edi,.x12,.834,.835,.837,.hl7,.json,.xml"
          aria-label="Sample to upload"
          hidden
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void upload(file);
            e.target.value = "";
          }}
        />
        {folderFiles.length > 0 && (
          <label className="named">
            <span>File from source folder</span>
            <select value="" onChange={(e) => void readFromFolder(e.target.value)}>
              <option value="">Select a file</option>
              {folderFiles.map((file) => (
                <option key={file} value={file}>
                  {file}
                </option>
              ))}
            </select>
          </label>
        )}
        <Button
          variant={writing || parsed || designing ? undefined : "primary"}
          onClick={() => input.current?.click()}
          busy={busy === "parse"}
        >
          {folderFiles.length > 0 ? "Upload other sample" : "Upload sample"}
        </Button>
        <Button onClick={() => setDesigning(!designing)}>
          New layout
        </Button>
        <p className="panel__hint">{sampleKinds()}</p>
      </div>

      {designing && (
        <LayoutDesigner
          onSaved={load}
          approvalRequired={approvalRequired}
          onClose={() => setDesigning(false)}
        />
      )}

      {parsed && (
        <div className="parsed">
          <h3>
            Detected format: {layoutKindName(parsed.kind)}
            {parsed.delimiter ? `, delimiter "${parsed.delimiter}"` : ""} ·{" "}
            <span className="num">{parsed.fields.length}</span> fields
          </h3>
          <FieldsTable
            kind={parsed.kind}
            fields={parsed.fields}
            rules={rules}
            onChange={(fields) =>
              setParsed({
                ...parsed,
                fields,
                n_default: fields.filter((f) => f.is_default).length,
              })
            }
          />
          {parsed.n_default ? (
            <p className="panel__hint">
              {`${parsed.n_default} of ${parsed.fields.length} fields will be left blank.`}
            </p>
          ) : null}
          {/* A field falls to the default because no rule carries its name.
              Saying so, next to the count, is the difference between a number
              and something a person can act on. */}
          {(parsed.n_default ?? 0) > 0 && rules.length === 0 && (
            <Banner tone="caution">
              No rules are defined yet, so every field above is blank.{" "}
              <button className="linked" onClick={onRules}>
                Add rules from the plan
              </button>
              , then read this file again.
            </Banner>
          )}

          <div className="panel__actions">
            <label className="named">
              <span>Name</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Layout name"
              />
            </label>
            <Button
              variant="primary"
              onClick={save}
              busy={busy === "save"}
              disabled={!name.trim()}
            >
              Save layout
            </Button>
          </div>
        </div>
      )}
      </>

      <div className="layouts__head">
        <h3 className="panel__title">
          Saved file layouts <span className="num">{layouts.length}</span>
        </h3>
        {writing && (
        <label className="named">
          <span>One row per</span>
          <select
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
          >
            {patternKeys().map((key) => (
              <option key={key} value={key}>{termsOf(key).Line ?? termsOf(key).One}</option>
            ))}
          </select>
        </label>
        )}
      </div>
      {layouts.length === 0 ? (
        <Empty title="No file layouts yet">Upload a sample or create a new layout.</Empty>
      ) : (
        <table className="panel__table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Type</th>
              {approvalRequired && <th>Status</th>}
              <th />
            </tr>
          </thead>
          <tbody>
            {layouts.map((layout) => (
              <tr key={layout.id}>
                <td>{layout.name}</td>
                <td>{layoutKindName(layout.kind)}</td>
                {approvalRequired && (
                  <td>
                    <Chip tone={layout.status === "approved" ? "affirm" : "caution"}>
                      {layoutStatusName(layout.status)}
                    </Chip>
                    {layout.approved_by && (
                      <span
                        className="panel__by"
                        title={layout.confirmed_by
                          ? `Confirmed at this computer by ${layout.confirmed_by}`
                          : undefined}
                      >
                        {" "}by {layout.approved_by}
                      </span>
                    )}
                  </td>
                )}
                <td
                  className="right"
                  ref={(cell) => {
                    if (cell) cells.current.set(layout.id, cell);
                    else cells.current.delete(layout.id);
                  }}
                >
                  {/* A saved layout writes files; with a second approver, only
                      once approved, and Approve is offered until then. */}
                  {writing && dropping !== layout.id && approving !== layout.id &&
                    (!approvalRequired || layout.status === "approved") && (
                      <>
                        <Button
                          onClick={() => writeFile(layout)}
                          busy={busy === `write-${layout.id}`}
                        >
                          Generate file
                        </Button>{" "}
                      </>
                    )}
                  {dropping === layout.id ? (
                    <>
                      <Button
                        variant="danger"
                        onClick={() => drop(layout)}
                        busy={busy === `drop-${layout.id}`}
                      >
                        {`Delete ${layout.name}`}
                      </Button>{" "}
                      <Button
                        onClick={() => {
                          setDropping(null);
                          setRefocus({ id: layout.id, label: "Delete" });
                        }}
                      >
                        Cancel
                      </Button>
                    </>
                  ) : approving === layout.id ? (
                    <span className="approve" role="group" aria-label="Approve layout">
                      {approver === null && (
                        <span className="panel__hint">
                          {approverError
                            ? `Couldn't read the approval setting: ${approverError} `
                            : "The approval setting can't be read. "}
                          Set it again in Settings › Layout approval.
                        </span>
                      )}
                      <label className="named">
                        <span>{`Type ${approver ?? "the approver's name"} to approve`}</span>
                        <input
                          value={typed}
                          autoComplete="off"
                          onChange={(e) => setTyped(e.target.value)}
                          onKeyDown={(e) => {
                            // Enter approves once, never again while it is sent.
                            if (e.key === "Enter" && confirmed && busy === null) {
                              void approve(layout.id);
                            }
                            if (e.key === "Escape") {
                              setApproving(null);
                              setRefocus({ id: layout.id, label: "Approve" });
                            }
                          }}
                        />
                      </label>{" "}
                      <Button
                        variant="primary"
                        onClick={() => approve(layout.id)}
                        disabled={!confirmed}
                        busy={busy === `approve-${layout.id}`}
                      >
                        Approve
                      </Button>{" "}
                      <Button
                        onClick={() => {
                          setApproving(null);
                          setRefocus({ id: layout.id, label: "Approve" });
                        }}
                      >
                        Cancel
                      </Button>
                    </span>
                  ) : (
                    <>
                      <Button
                        onClick={() => setEditing(editing === layout.id ? null : layout.id)}
                      >
                        {editing === layout.id ? "Close" : "Edit"}
                      </Button>{" "}
                      <Button
                        onClick={() => {
                          setDropping(layout.id);
                          setRefocus({ id: layout.id, label: "Cancel" });
                        }}
                      >
                        Delete
                      </Button>
                      {approvalRequired && layout.status !== "approved" && (
                        <>
                          {" "}
                          <Button
                            onClick={() => {
                              setApproving(layout.id);
                              setTyped("");
                              setRefocus({ id: layout.id, label: "input" });
                            }}
                          >
                            Approve
                          </Button>
                        </>
                      )}
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {editing !== null && (
        <LayoutEditor
          key={editing}
          layoutId={editing}
          approvalRequired={approvalRequired}
          rules={rules}
          onClose={() => setEditing(null)}
          onSaved={(note) => {
            setSaved(note);
            setEditing(null);
            void load();
          }}
        />
      )}

      <p className="panel__hint">
        <span className="num">{rules.length}</span> elements have rules.
      </p>
    </section>
  );
}

/* ------------------------------------------------------------------ */
