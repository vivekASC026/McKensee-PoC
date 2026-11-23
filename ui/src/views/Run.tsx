/**
 * Preview, generate, check, remove.
 *
 * The four things you do once the plan is settled, in the order you do them.
 * The preview is first because looking before writing is the point of having
 * one, and cleanup is last and deliberately quiet.
 */

import { useState } from "react";
import type {
  CleanupResult,
  DryRunPayload,
  GenerateResult,
  PlanPayload,
  RunPayload,
  ValidateResult,
} from "../lib/api";
import {
  Banner,
  Button,
  Chip,
  Empty,
  formatCount,
} from "../components/primitives";
import { shortName } from "./Cascade";
import "./Run.css";

interface Props {
  plan: PlanPayload;
  run: RunPayload | null;
  preview: DryRunPayload | null;
  generated: GenerateResult | null;
  validation: ValidateResult | null;
  cleaned: CleanupResult | null;
  busy: string | null;
  error: string | null;
  onPreview: () => void;
  onGenerate: () => void;
  onValidate: () => void;
  onCleanup: () => void;
}

export function Run(props: Props) {
  const {
    plan, run, preview, generated, validation, cleaned,
    busy, error, onPreview, onGenerate, onValidate, onCleanup,
  } = props;

  const blocked = (plan.blocking ?? 0) > 0;
  const hasRun = run?.hasRun ?? false;

  return (
    <div className="run">
      <header className="run__head">
        <div>
          <h2 className="run__title">Load to database</h2>
          <p className="run__lede">
            {formatCount(plan.totalRows)} rows, seed{" "}
            <span className="mono">{plan.meta?.seed}</span>. The same seed produces
            identical data on any machine.
          </p>
        </div>
      </header>

      {error && <Banner tone="alarm">{error}</Banner>}
      {blocked && (
        <Banner tone="alarm">
          The plan has {plan.blocking} blocking warning
          {plan.blocking === 1 ? "" : "s"}. Loading is off until they are
          resolved.
        </Banner>
      )}

      <div className="run__steps">
        <Action
          title="Preview"
          body="A few rows per table. Nothing is written."
          button="Preview"
          busy={busy === "preview"}
          onClick={onPreview}
        />
        <Action
          title="Load"
          body={`Inserts ${formatCount(plan.totalRows)} rows into ${
            run?.manifest?.run.database ?? "the target database"
          }, parent tables first.`}
          button="Load to database"
          primary
          disabled={blocked}
          busy={busy === "generate"}
          onClick={onGenerate}
        />
        <Action
          title="Validate"
          body="Checks the loaded rows against the plan."
          button="Validate"
          disabled={!hasRun}
          busy={busy === "validate"}
          onClick={onValidate}
        />
        <Action
          title="Delete"
          body="Deletes only the rows this run inserted."
          button="Delete loaded rows"
          danger
          disabled={!hasRun}
          busy={busy === "cleanup"}
          onClick={onCleanup}
        />
      </div>

      {preview && <Preview preview={preview} />}

      {generated && (
        <section className="run__result">
          <h3 className="run__result-title">Loaded</h3>
          <table className="run__table">
            <thead>
              <tr>
                <th>Table</th>
                <th className="num">Rows</th>
                <th>Keys</th>
              </tr>
            </thead>
            <tbody>
              {generated.tables.map((table) => (
                <tr key={table.table}>
                  <td className="mono">{shortName(table.table)}</td>
                  <td className="num">{formatCount(table.rows_written)}</td>
                  <td>
                    {table.identity_insert ? (
                      <Chip tone="structural" fill="outline"
                        title="Keys are assigned during the run so child rows can reference them.">
                        assigned at load
                      </Chip>
                    ) : (
                      <span className="run__muted">from plan</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="run__total num">
            {formatCount(generated.rowsWritten)} rows in {generated.seconds}s
          </p>
          {generated.request && (
            <p className="run__muted">
              Recorded as run {generated.request.request}. Rerun it from History.
            </p>
          )}
          {generated.triggersFired.length > 0 && (
            <Banner tone="caution">
              <span>
                {generated.triggersFired.join(", ")} fired during the load. Rows that
                triggers wrote to other tables are not deleted by cleanup.
              </span>
            </Banner>
          )}
        </section>
      )}

      {validation && <Validation validation={validation} />}

      {cleaned && (
        <section className="run__result">
          <h3 className="run__result-title">Deleted</h3>
          <ul className="run__cleaned">
            {Object.entries(cleaned.deleted).map(([table, count]) => (
              <li key={table}>
                <span className="mono">{shortName(table)}</span>
                <span className="num">{formatCount(count)}</span>
              </li>
            ))}
          </ul>
          <p className="run__total num">
            {formatCount(cleaned.total)} rows deleted.
          </p>
        </section>
      )}
    </div>
  );
}

function Action({
  title,
  body,
  button,
  onClick,
  busy,
  disabled,
  primary,
  danger,
}: {
  title: string;
  body: string;
  button: string;
  onClick: () => void;
  busy?: boolean;
  disabled?: boolean;
  primary?: boolean;
  danger?: boolean;
}) {
  return (
    <div className="run__step">
      <h3 className="run__step-title">{title}</h3>
      <p className="run__step-body">{body}</p>
      <Button
        variant={primary ? "primary" : danger ? "danger" : "quiet"}
        onClick={onClick}
        busy={busy}
        disabled={disabled}
      >
        {button}
      </Button>
    </div>
  );
}

function Preview({ preview }: { preview: DryRunPayload }) {
  const [active, setActive] = useState(preview.tables[0]?.name ?? "");
  const table = preview.tables.find((t) => t.name === active) ?? preview.tables[0];
  if (!table) return <Empty title="No preview data" />;

  return (
    <section className="run__result">
      <h3 className="run__result-title">Sample rows</h3>
      <div className="run__tabs" role="tablist">
        {preview.tables.map((candidate) => (
          <button
            key={candidate.name}
            role="tab"
            aria-selected={candidate.name === table.name}
            className={"run__tab" + (candidate.name === table.name ? " is-on" : "")}
            onClick={() => setActive(candidate.name)}
          >
            {shortName(candidate.name)}
            <span className="run__tab-count num">{formatCount(candidate.generated)}</span>
          </button>
        ))}
      </div>
      <div className="run__grid-wrap">
        <table className="run__grid">
          <thead>
            <tr>
              {table.columns.map((column) => (
                <th key={column} className="mono">{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row, index) => (
              <tr key={index}>
                {table.columns.map((column) => (
                  <td key={column} className="mono">
                    {row[column] === null || row[column] === undefined ? (
                      <span className="run__null">null</span>
                    ) : (
                      String(row[column])
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Validation({ validation }: { validation: ValidateResult }) {
  const [showAll, setShowAll] = useState(false);
  const failures = validation.results.filter((r) => !r.passed);
  const shown = showAll ? validation.results : failures;

  return (
    <section className="run__result">
      <h3 className="run__result-title">Validation results</h3>
      <Banner tone={validation.passed ? "affirm" : "alarm"}>
        <span>
          {validation.checks - validation.failed} of {validation.checks} checks passed
          {validation.passed
            ? ". The data conforms to the plan."
            : `. ${validation.failed} did not.`}
        </span>
      </Banner>

      <div className="run__kinds">
        {Object.entries(validation.by_kind).map(([kind, counts]) => (
          <div key={kind} className="run__kind">
            <span className="run__kind-name">{kind.replace(/_/g, " ")}</span>
            <span className={"run__kind-count num" + (counts.failed ? " is-bad" : "")}>
              {counts.passed}
              {counts.failed > 0 && ` / ${counts.failed} failed`}
            </span>
          </div>
        ))}
      </div>

      {(shown.length > 0 || !showAll) && (
        <>
          <button className="run__toggle" onClick={() => setShowAll(!showAll)}>
            {showAll ? "Show failures only" : `Show all ${validation.checks} checks`}
          </button>
          <ul className="run__checks">
            {shown.map((check, index) => (
              <li key={index} className={check.passed ? "" : "is-bad"}>
                <span className="run__check-mark" aria-hidden="true">
                  {check.passed ? "✓" : "✕"}
                </span>
                <span className="run__check-name">{check.name}</span>
                <span className="run__check-detail">{check.detail}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
