/**
 * The cold start.
 *
 * An empty screen is an invitation to act, so this one has a single action and
 * says plainly what it will do and what it will not touch. The steps report
 * what they found as they go, because a person watching a database being read
 * wants to know it is reading the right one.
 */

import type { ReactNode } from "react";
import type { DiscoverResult, MapResult, ProfileResult, ReviewResult, Step } from "../lib/api";
import { Banner, Button, Chip, formatCount, formatPercent } from "../components/primitives";
import { TargetPicker } from "./TargetPicker";
import "./Setup.css";

export interface ReadResults {
  discover?: DiscoverResult;
  profile?: ProfileResult;
  map?: MapResult;
  review?: ReviewResult;
}

interface Props {
  database: string;
  referenceDatabase: string;
  landscape: string;
  server: string;
  steps: Step[];
  running: string | null;
  error: string | null;
  results: ReadResults;
  onRead: () => void;
  onTargetChanged: () => void;
  // The database worked on is production: it is never read from here.
  isProtected?: boolean;
  onPatterns?: () => void;
}

// `running` is the status App reports while the stage is under way.
const STAGES = [
  { id: "discover", label: "Discover schema", running: "Discovering schema", note: "Tables, keys and constraints" },
  { id: "profile", label: "Profile data", running: "Profiling data", note: "Formats, distributions and ratios" },
  { id: "map", label: "Map to catalog", running: "Mapping to catalog", note: "Element matches with supporting evidence" },
  { id: "review", label: "Propose rules", running: "Proposing rules", note: "Unresolved columns are queued for review" },
];

export function Setup({
  database,
  referenceDatabase,
  landscape,
  server,
  steps,
  running,
  error,
  results,
  onRead,
  onTargetChanged,
  isProtected = false,
  onPatterns,
}: Props) {
  const done = steps.find((s) => s.id === "map")?.done ?? false;
  const busy = running !== null;

  return (
    <div className="setup">
      <div className="setup__intro">
        <h2 className="setup__title">Data source</h2>
        <p className="setup__lede">
          Select the database to profile and generate into. Discovery is read-only.
        </p>

        <dl className="setup__target">
          <div>
            <dt>Target</dt>
            <dd className="mono">{database}</dd>
          </div>
          <div>
            <dt>Reference database</dt>
            <dd className="mono">{referenceDatabase}</dd>
          </div>
          <div>
            <dt>Server</dt>
            <dd className="mono">{server}</dd>
          </div>
          <div>
            <dt>Pattern data</dt>
            <dd className="mono">{landscape}</dd>
          </div>
        </dl>

        {error && <Banner tone="alarm">{error}</Banner>}

        {isProtected ? (
          <div className="setup__action">
            <p className="setup__lede">
              {database} is a protected production database and is not read here.
              {onPatterns && <> Patterns come from Pattern data ({landscape}).</>} To profile
              and load a database, select a test database below.
            </p>
            {onPatterns && (
              <Button variant="primary" onClick={onPatterns}>
                Go to Patterns
              </Button>
            )}
          </div>
        ) : (
          <div className="setup__action">
            <Button variant="primary" onClick={onRead} busy={busy}>
              {done ? "Rerun discovery" : "Discover schema"}
            </Button>
            {busy && <span className="setup__running">{running}…</span>}
          </div>
        )}

        <TargetPicker onChanged={onTargetChanged} />
      </div>

      <ol className="setup__stages">
        {STAGES.map((stage) => {
          // Reading and measuring are one step on the rail and two stages of
          // the read, so each stage asks the merged step its own half.
          const merged = steps.find((s) => s.id === "discover");
          const matched = steps.find((s) => s.id === "map");
          const step = steps.find((s) => s.id === stage.id);
          const done =
            stage.id === "discover"
              ? merged?.read
              : stage.id === "profile"
                ? merged?.measured
                : stage.id === "map"
                  ? matched?.mapped
                  : stage.id === "review"
                    ? matched?.proposed
                    : step?.done;
          const active = running === stage.running;
          return (
            <li
              key={stage.id}
              className={
                "setup__stage" +
                (done ? " is-done" : "") +
                (active ? " is-active" : "")
              }
            >
              <div className="setup__stage-head">
                <span className="setup__stage-label">{stage.label}</span>
                {step?.done && !active && (
                  <Chip tone="learned" fill="solid">
                    {step.detail}
                  </Chip>
                )}
                {active && <span className="setup__stage-spin" aria-hidden="true" />}
              </div>
              <p className="setup__stage-note">{stage.note}</p>
              <StageDetail id={stage.id} results={results} />
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function StageDetail({ id, results }: { id: string; results: ReadResults }) {
  if (id === "discover" && results.discover) {
    const { inferred, declared_foreign_keys, tables, columns } = results.discover;
    return (
      <Detail>
        <p>
          {tables} tables, {columns} columns, {declared_foreign_keys} declared foreign keys.
        </p>
        {inferred.map((key) => (
          <p key={`${key.childTable}.${key.childColumn}`} className="setup__found">
            Inferred foreign key:{" "}
            <span className="mono">
              {key.childTable.split(".").pop()}.{key.childColumn}
            </span>{" "}
            references{" "}
            <span className="mono">
              {key.parentTable.split(".").pop()}.{key.parentColumn}
            </span>{" "}
            ({formatPercent(key.coverage)} of {formatCount(key.distinctValues)} values
            match).
          </p>
        ))}
      </Detail>
    );
  }
  if (id === "profile" && results.profile) {
    const p = results.profile;
    return (
      <Detail>
        <p>
          {p.formatsFitted} formats inferred, {p.dependencies} column dependencies,{" "}
          {p.orderings} date orderings, {p.ratios} parent-child ratios.
        </p>
        {p.skipped.length > 0 && (
          <p className="setup__found">
            Skipped (time limit reached): {p.skipped.map((s) => s.analysis).join(", ")}.
          </p>
        )}
      </Detail>
    );
  }
  if (id === "map" && results.map) {
    const m = results.map;
    return (
      <Detail>
        <p>
          {m.high} of {m.mappable_columns} columns mapped to an element with high
          confidence. {m.system_columns} columns are database-managed and need no
          element.
        </p>
      </Detail>
    );
  }
  if (id === "review" && results.review) {
    const r = results.review;
    return (
      <Detail>
        <p>
          {r.proposals} rules proposed, {r.byConfidence.high ?? 0} with high confidence.
          {r.queued > 0
            ? ` ${r.queued} columns need review.`
            : " No columns need review."}
        </p>
      </Detail>
    );
  }
  return null;
}

function Detail({ children }: { children: ReactNode }) {
  return <div className="setup__stage-detail">{children}</div>;
}
