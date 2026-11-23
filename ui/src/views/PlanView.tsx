/**
 * The resolved plan.
 *
 * Every column, the rule that won, and what it beat. The provenance bar across
 * the top is the honest summary of where this plan's rules actually came from:
 * how much the schema forced, how much the catalog governs, how much was
 * measured, and how much a person decided.
 */

import { Fragment, useState } from "react";
import type { PlanField, PlanPayload, Provenance } from "../lib/api";
import {
  Banner,
  Chip,
  EvidenceList,
  PROVENANCE_WORDS,
  RuleChip,
  describeParams,
  formatCount,
  formatPercent,
} from "../components/primitives";
import { shortName } from "./Cascade";
import "./PlanView.css";

const ORDER: Provenance[] = ["structural", "catalog", "learned", "user", "physical"];

export function PlanView({ plan }: { plan: PlanPayload }) {
  const [open, setOpen] = useState<string | null>(null);
  const tables = (plan.tables ?? []).filter((t) => t.mode === "generate");
  const total = Object.values(plan.provenance ?? {}).reduce((a, b) => a + b, 0);
  const warnings = plan.warnings ?? [];
  const blocking = warnings.filter((w) => w.severity === "block");
  const advisory = warnings.filter((w) => w.severity === "warn");

  return (
    <div className="planview">
      <header className="planview__head">
        <div>
          <h2 className="planview__title">Rules</h2>
          <p className="planview__lede">
            The rule for each column, its source, and the rules it overrides.
          </p>
        </div>
      </header>

      <section className="planview__provenance">
        <div className="planview__bar" role="img" aria-label="Rules by source">
          {ORDER.map((key) => {
            const count = plan.provenance?.[key] ?? 0;
            if (!count) return null;
            return (
              <div
                key={key}
                className={`planview__bar-part planview__bar-part--${key}`}
                style={{ flexGrow: count }}
                title={`${count} ${PROVENANCE_WORDS[key].toLowerCase()}`}
              />
            );
          })}
        </div>
        <ul className="planview__legend">
          {ORDER.map((key) => {
            const count = plan.provenance?.[key] ?? 0;
            if (!count) return null;
            return (
              <li key={key}>
                <span className={`planview__swatch planview__swatch--${key}`} />
                <span className="planview__legend-count num">{count}</span>
                <span className="planview__legend-label">{PROVENANCE_WORDS[key]}</span>
                <span className="planview__legend-share num">
                  {formatPercent(count / Math.max(1, total))}
                </span>
              </li>
            );
          })}
        </ul>
      </section>

      {blocking.length > 0 && (
        <Banner tone="alarm">
          <div>
            <strong>
              {blocking.length} blocking issue{blocking.length === 1 ? "" : "s"} must be
              resolved before the plan can run.
            </strong>
            <ul className="planview__warnings">
              {blocking.map((warning, index) => (
                <li key={index}>{warning.message}</li>
              ))}
            </ul>
          </div>
        </Banner>
      )}
      {advisory.length > 0 && (
        <Banner tone="caution">
          <div>
            <strong>Warnings</strong>
            <ul className="planview__warnings">
              {advisory.map((warning, index) => (
                <li key={index}>{warning.message}</li>
              ))}
            </ul>
          </div>
        </Banner>
      )}

      {tables.map((table) => (
        <section className="planview__table" key={table.name}>
          <h3 className="planview__table-head">
            <span className="mono">{shortName(table.name)}</span>
            <span className="planview__table-meta num">
              {formatCount(table.effectiveCount)} rows
            </span>
            {table.drivingParent && (
              <span className="planview__table-meta">
                driven by {shortName(table.drivingParent)}
              </span>
            )}
          </h3>
          <table className="planview__grid">
            <thead>
              <tr>
                <th>Column</th>
                <th>Element</th>
                <th>Rule</th>
                <th>Parameters</th>
                <th>Overrides</th>
              </tr>
            </thead>
            <tbody>
              {table.fields.map((field) => {
                const key = `${table.name}.${field.name}`;
                const expanded = open === key;
                const toggle = () => setOpen(expanded ? null : key);
                return (
                  <Fragment key={key}>
                    <tr
                      className={expanded ? "is-open" : ""}
                      tabIndex={0}
                      aria-expanded={expanded}
                      onClick={toggle}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          toggle();
                        }
                      }}
                    >
                      <td className="mono planview__col">
                        {field.name}
                        {field.userOverride && (
                          <Chip tone="user" fill="solid">override</Chip>
                        )}
                      </td>
                      <td className="mono planview__element">{field.element ?? "—"}</td>
                      <td>
                        <RuleChip
                          provenance={field.provenance}
                          confidence={field.confidence}
                        >
                          {field.generator.replace("_", " ")}
                        </RuleChip>
                      </td>
                      <td className="planview__params mono">
                        {describeParams(field.params, 3).join("   ") || "—"}
                      </td>
                      <td className="planview__beat">
                        {field.overrode.length > 0 ? (
                          <span title={field.overrode[0].reason}>
                            {field.overrode
                              .map((o) => PROVENANCE_WORDS[o.provenance].toLowerCase())
                              .join(", ")}
                          </span>
                        ) : (
                          ""
                        )}
                      </td>
                    </tr>
                    {expanded && (
                      <tr className="planview__expanded" key={`${key}-detail`}>
                        <td colSpan={5}>
                          <FieldDetail field={field} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}

function FieldDetail({ field }: { field: PlanField }) {
  return (
    <div className="planview__detail">
      <div className="planview__detail-main">
        <EvidenceList items={field.evidence} />
        {field.overrode.length > 0 && (
          <div className="planview__overrode">
            {field.overrode.map((overrode, index) => (
              <p key={index}>
                Overrides the{" "}
                <span className={`planview__inline planview__inline--${overrode.provenance}`}>
                  {PROVENANCE_WORDS[overrode.provenance].toLowerCase()}
                </span>{" "}
                rule <span className="mono">{overrode.generator}</span>: {overrode.reason}.
              </p>
            ))}
          </div>
        )}
      </div>
      <dl className="planview__detail-side">
        <div>
          <dt>Type</dt>
          <dd className="mono">{field.dataType ?? "—"}</dd>
        </div>
        <div>
          <dt>Role</dt>
          <dd>{field.bucket.replace(/_/g, " ")}</dd>
        </div>
        {field.coverage != null && (
          <div>
            <dt>Coverage</dt>
            <dd className="num">{formatPercent(field.coverage)}</dd>
          </div>
        )}
        {field.nullRate != null && (
          <div>
            <dt>Null rate</dt>
            <dd className="num">{formatPercent(field.nullRate)}</dd>
          </div>
        )}
      </dl>
    </div>
  );
}
