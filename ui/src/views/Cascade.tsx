/**
 * The cascade.
 *
 * One number at the top, and every row count below it falls out of measured
 * ratios. This is the product's whole argument in one interaction: you are not
 * setting four counts by hand, you are setting one and letting the shape of the
 * real data decide the rest.
 *
 * Each edge carries its own distribution, drawn small. The bar for parents with
 * no children is colored differently because that is precisely the number a
 * flat ratio throws away.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type {
  PlanPayload,
  PlanTable,
  RatioProfile,
  RatioSetting,
  RatioShapesPayload,
  SchemaPayload,
  Settings,
} from "../lib/api";
import { RatioEditor } from "../components/RatioEditor";
import {
  Banner,
  Button,
  Histogram,
  formatCount,
  formatPercent,
  formatRatio,
} from "../components/primitives";
import "./Cascade.css";

/** Providers are real: a run reuses them and never generates one. */
const NEVER_GENERATED = new Set(["dbo.CMC_PRPR_PROV"]);

interface Props {
  plan: PlanPayload;
  schema: SchemaPayload | null;
  settings: Settings;
  busy: boolean;
  ratioShapes: RatioShapesPayload | null;
  saveError: string | null;
  onChangeRoot: (table: string, count: number) => void | Promise<void>;
  onChangeMode: (table: string, mode: string) => void;
  onChangeRatio: (table: string, setting: RatioSetting | null) => Promise<void>;
  onChangeDrivingParent: (table: string, parent: string) => Promise<void>;
  onRebuild: () => void;
  onOpenPreview: () => void;
}

export function Cascade({
  plan,
  schema,
  settings,
  busy,
  ratioShapes,
  saveError,
  onChangeRoot,
  onChangeMode,
  onChangeRatio,
  onChangeDrivingParent,
  onRebuild,
  onOpenPreview,
}: Props) {
  // Only one edge is edited at a time: they share the total below them, so two
  // open forms would be two answers to the same question.
  const [editingEdge, setEditingEdge] = useState<string | null>(null);
  const tables = plan.tables ?? [];
  const chain = useMemo(() => orderChain(tables, plan.generationOrder ?? []), [tables, plan]);
  const ratiosByChild = useMemo(() => {
    const map = new Map<string, RatioProfile>();
    for (const ratio of schema?.ratios ?? []) {
      map.set(`${ratio.child_table}|${ratio.parent_table}`, ratio);
    }
    return map;
  }, [schema]);

  const roots = chain.filter((t) => t.mode === "generate" && t.drivingParent === null);
  const root = roots[0];
  const rootName = root?.name ?? Object.keys(settings.rootCounts)[0] ?? "";
  // The number in the box has to be the number that produced the counts below
  // it, so it comes from the plan rather than from the saved setting. The two
  // can differ when a plan was last built elsewhere, and showing the setting
  // would put two different answers to the same question on one screen.
  const planned = root?.count ?? settings.rootCounts[rootName] ?? 5000;

  const [draft, setDraft] = useState(String(planned));
  // The count being sent, so Enter followed by leaving the box saves once;
  // cleared when the save ends, so a failed one can be tried again.
  const sent = useRef<number | null>(null);
  useEffect(() => {
    setDraft(String(planned));
    sent.current = null;
  }, [planned]);

  const digits = draft.replace(/[^0-9]/g, "");
  const parsed = Number(digits);
  // An emptied box or one with no digits is not a count of zero.
  const valid = digits !== "" && Number.isSafeInteger(parsed) && parsed >= 1;
  const dirty = valid && parsed !== planned;
  const apply = (table: string) => {
    if (!dirty || sent.current === parsed) return;
    sent.current = parsed;
    void Promise.resolve(onChangeRoot(table, parsed)).finally(() => {
      sent.current = null;
    });
  };

  const generated = chain.filter((t) => t.mode === "generate");
  const other = tables.filter((t) => t.mode !== "generate");
  // Reference data is the system's approved configuration -- plans, providers,
  // code sets -- and is kept as it is; transactions are what a load makes,
  // pointing at it. Said up front, because it is the first thing anyone who
  // owns that configuration asks.
  const kept = other.filter((t) => t.mode === "reuse");
  const leftAlone = other.filter((t) => t.mode === "exclude");

  return (
    <div className="cascade">
      <header className="cascade__head">
        <div>
          <h2 className="cascade__title">Volume and distribution</h2>
          <p className="cascade__lede">
            {generated.some((table) => table.ratioOverride)
              ? "Set the root record count. Each child count below follows the measured children-per-parent ratio, except where a custom ratio is set."
              : "Set the root record count. Each child count below follows the measured children-per-parent ratio."}
          </p>
        </div>
        <div className="cascade__total">
          <div className="cascade__total-value num">{formatCount(plan.totalRows)}</div>
          <div className="cascade__total-label">rows in this plan</div>
        </div>
      </header>

      <dl className="cascade__kinds" aria-label="Table handling by type">
        <div>
          <dt>Transactional — generated</dt>
          <dd>
            <span className="num">{generated.length}</span> table
            {generated.length === 1 ? "" : "s"}: new rows that reference existing reference data
          </dd>
        </div>
        <div>
          <dt>Reference — kept as is</dt>
          <dd>
            <span className="num">{kept.length}</span> table{kept.length === 1 ? "" : "s"}:
            existing rows only; nothing is added or changed
          </dd>
        </div>
        {leftAlone.length > 0 && (
          <div>
            <dt>Excluded</dt>
            <dd>
              <span className="num">{leftAlone.length}</span> table
              {leftAlone.length === 1 ? "" : "s"}
            </dd>
          </div>
        )}
      </dl>

      {(plan.blocking ?? 0) > 0 && (
        <Banner tone="alarm">
          <span>
            {plan.blocking} blocking issue{plan.blocking === 1 ? "" : "s"} must be resolved
            before this plan can run. See Rules below.
          </span>
        </Banner>
      )}

      <div className="cascade__canvas">
        {generated.map((table, index) => {
          const ratio = table.drivingParent
            ? ratiosByChild.get(`${table.name}|${table.drivingParent}`)
            : undefined;
          const isRoot = table.drivingParent === null;
          return (
            <div className="cascade__node" key={table.name}>
              {index > 0 && (
                <div className="cascade__edge">
                  <div className="cascade__edge-line" aria-hidden="true" />
                  <div className="cascade__edge-body">
                    {editingEdge === table.name ? (
                      <RatioEditor
                        parent={shortName(table.drivingParent)}
                        shapes={ratioShapes}
                        current={settings.ratios?.[table.name] ?? null}
                        saving={busy}
                        error={saveError}
                        onSave={async (setting) => {
                          await onChangeRatio(table.name, setting);
                          setEditingEdge(null);
                        }}
                        onCancel={() => setEditingEdge(null)}
                      />
                    ) : (
                      <>
                        <div className="cascade__edge-ratio num">
                          {formatRatio(table.ratioMean)}
                          {/* The parent in "per X" is the driving parent, so
                              where there is a choice it is made here rather
                              than somewhere else that calls it a setting. */}
                          {table.parents.length > 1 ? (
                            <label className="cascade__edge-per cascade__edge-parent">
                              per{" "}
                              <select
                                value={table.drivingParent ?? ""}
                                disabled={busy}
                                onChange={(e) =>
                                  void onChangeDrivingParent(table.name, e.target.value)
                                }
                                aria-label={`Driving parent for ${shortName(table.name)}`}
                              >
                                {table.parents.map((parent) => (
                                  <option key={parent} value={parent}>
                                    {shortName(parent)}
                                  </option>
                                ))}
                              </select>
                            </label>
                          ) : (
                            <span className="cascade__edge-per">
                              per {shortName(table.drivingParent)}
                            </span>
                          )}
                          <button
                            className="cascade__edge-change"
                            onClick={() => setEditingEdge(table.name)}
                          >
                            Edit ratio
                          </button>
                        </div>
                        <div className="cascade__edge-measure">
                          {/* The measured histogram is not what will run once
                              someone has stated a shape, so it is not drawn
                              under a number it no longer describes. */}
                          {ratio && !table.ratioOverride && (
                            <Histogram bins={ratio.histogram} height={42} />
                          )}
                          <p className="cascade__edge-evidence">
                            {table.ratioOverride
                              ? describeStated(table)
                              : ratio
                                ? ratio.evidence
                                : "Fixed ratio: the measured variation is small enough for a single value."}
                          </p>
                        </div>
                      </>
                    )}
                  </div>
                </div>
              )}

              <div className={"cascade__table" + (isRoot ? " cascade__table--root" : "")}>
                <div className="cascade__table-name mono">{shortName(table.name)}</div>
                {isRoot ? (
                  <form
                    className="cascade__root-input"
                    onSubmit={(event) => {
                      event.preventDefault();
                      apply(table.name);
                    }}
                  >
                    <input
                      className="num"
                      value={draft}
                      inputMode="numeric"
                      aria-label={`Record count for ${shortName(table.name)}`}
                      aria-invalid={!valid}
                      onChange={(event) => setDraft(event.target.value)}
                      onBlur={() => apply(table.name)}
                    />
                    <span className="cascade__root-hint">
                      {!valid ? "Enter a whole number of 1 or more" : dirty ? "Press Enter to apply" : "rows"}
                    </span>
                  </form>
                ) : (
                  <div className="cascade__table-count num">
                    {formatCount(table.effectiveCount)}
                    {/* As the editable counts above say it. */}
                    <span className="cascade__root-hint"> rows</span>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      <div className="cascade__actions">
        <Button variant="primary" onClick={onOpenPreview} disabled={busy}>
          Preview
        </Button>
        <Button onClick={onRebuild} busy={busy}>
          Rebuild plan
        </Button>
      </div>

      {other.length > 0 && (
        <section className="cascade__aside">
          <h3 className="cascade__aside-title">Reference data</h3>
          <p className="cascade__aside-lede">
            A load never adds to or changes these tables; generated rows reference
            their existing rows.
          </p>
          <ul className="cascade__aside-list">
            {other.map((table) => (
              <li key={table.name}>
                <span className="mono">{shortName(table.name)}</span>
                <span
                  className={
                    "cascade__kind" + (table.mode === "reuse" ? " cascade__kind--kept" : "")
                  }
                >
                  {table.mode === "reuse" ? "reference — kept as is" : "excluded"}
                </span>
                <select
                  value={table.mode}
                  onChange={(event) => onChangeMode(table.name, event.target.value)}
                  aria-label={`Handling for ${shortName(table.name)}`}
                >
                  <option value="generate" disabled={NEVER_GENERATED.has(table.name)}>
                    Generate rows
                  </option>
                  <option value="reuse">Reuse existing rows</option>
                  <option value="exclude">Exclude</option>
                </select>
                {plan.references?.[table.name] && (
                  <span className="cascade__aside-detail">
                    {plan.references[table.name].describe}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {plan.references && Object.keys(plan.references).length > 0 && (
        <section className="cascade__aside">
          <h3 className="cascade__aside-title">Loaded reference keys</h3>
          <ul className="cascade__refs">
            {Object.values(plan.references).map((reference) => (
              <li key={reference.table}>
                <span className="mono">{reference.table.replace("element:", "")}</span>
                <span className="num">{formatCount(reference.size)}</span>
                {reference.filter ? (
                  <span className="cascade__refs-filter">
                    {reference.filter}: {formatCount(reference.size)} of{" "}
                    {formatCount(reference.available)}
                  </span>
                ) : (
                  <span className="cascade__refs-filter">all rows loaded</span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {schema && (
        <CascadeFootnote
          ratio={schema.ratios.find((r) => r.zero_share > 0.05)}
        />
      )}
    </div>
  );
}

function CascadeFootnote({ ratio }: { ratio?: RatioProfile }) {
  if (!ratio) return null;
  return (
    <p className="cascade__footnote">
      <span className="cascade__footnote-mark" aria-hidden="true" />
      {formatPercent(ratio.zero_share)} of {shortName(ratio.parent_table)} rows have no{" "}
      {shortName(ratio.child_table)} rows. A flat ratio would assign every parent the
      same count, and tests would never cover a parent with none.
    </p>
  );
}

/** What a stated ratio says, in the same voice the measured evidence uses. */
function describeStated(table: PlanTable): string {
  const ratio = table.ratio;
  if (!ratio) return "Custom ratio.";
  const shape =
    ratio.kind === "fixed"
      ? `${ratio.value} per parent`
      : `between ${ratio.min} and ${ratio.max} per parent`;
  const childless =
    ratio.zero_share != null
      ? `, ${formatPercent(ratio.zero_share)} with none`
      : "";
  return `Custom ratio: ${shape}${childless}. The measured ratio is retained and can be restored.`;
}

/** Generation order, restricted to the driving-parent chain. */
function orderChain(tables: PlanTable[], order: string[]): PlanTable[] {
  const byName = new Map(tables.map((t) => [t.name, t]));
  const chained = order.map((name) => byName.get(name)).filter(Boolean) as PlanTable[];
  const rest = tables.filter((t) => !order.includes(t.name));
  return [...chained, ...rest];
}

export function shortName(name: string | null | undefined): string {
  if (!name) return "—";
  return name.includes(".") ? name.slice(name.lastIndexOf(".") + 1) : name;
}
