/**
 * The schema explorer, under one of three lenses.
 *
 * Discover, Profile and Map are three things the tool did, in order, and the
 * rail presents them as three steps. They were rendering one identical table,
 * which made two of those steps a lie: the same screen three times does not
 * become three stages by being reached from three places.
 *
 * They are still one screen, because they are one subject -- the columns of
 * this database -- and splitting the data into three components would mean
 * three searches, three filters and three places to fix a bug. What differs is
 * which evidence is on show, because what each stage produced is different:
 *
 *   Discover  what was read from the structure: the column, its type, the role
 *             it plays. No measurements, because nothing had been measured yet.
 *   Profile   what measuring found: null rates, distinct counts, the fitted
 *             format. The numbers behind every later claim.
 *   Map       what was matched: the element each column was bound to and the
 *             rule that follows from it.
 *
 * Selecting a row opens the full profile beside it regardless of lens -- the
 * detail panel holds everything, because once you have asked about one column
 * you want all of it rather than the third of it this step is about.
 */

import { useEffect, useMemo, useState } from "react";
import type {
  Bucket,
  CatalogPayload,
  DecisionInput,
  GeneratorsPayload,
  Provenance,
  SchemaColumn,
  SchemaPayload,
} from "../lib/api";
import { RuleEditor, type EditableRule } from "../components/RuleEditor";
import {
  BucketMark,
  Button,
  Chip,
  EvidenceList,
  Field,
  PROVENANCE_WORDS,
  RuleChip,
  Share,
  formatCount,
  formatPercent,
  describeParams,
} from "../components/primitives";
import { shortName } from "./Cascade";
import "./Schema.css";

type Filter = "all" | "queued" | Bucket;

/** Which stage of the pipeline is being looked through. */
export type Lens = "discover" | "profile" | "map";

/** The columns each lens shows, in table order. */
const SHOWN: Record<Lens, ReadonlySet<string>> = {
  // Discover and Profile are one screen: what each column is, and what it holds.
  discover: new Set(["name", "type", "role", "nulls", "distinct", "shape"]),
  profile: new Set(["name", "type", "nulls", "distinct", "shape"]),
  map: new Set(["name", "type", "element", "rule"]),
};

const HEADINGS: Record<Lens, { title: string; lede: string }> = {
  discover: {
    title: "Column profile",
    lede: "Type, role, null rate, distinct values and fitted format for every column.",
  },
  profile: {
    title: "Column profile",
    lede: "Null rate, distinct values and inferred format for every column, from a full scan.",
  },
  map: {
    title: "Column map",
    lede: "Business element and rule for each column.",
  },
};

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All columns" },
  { id: "queued", label: "Needs review" },
  { id: "business_key", label: "Keys" },
  { id: "reference", label: "References" },
  { id: "code", label: "Code values" },
  { id: "derivable", label: "Derived" },
  { id: "system", label: "Database-managed" },
];

interface SchemaProps {
  schema: SchemaPayload;
  catalog: CatalogPayload | null;
  generators: GeneratorsPayload | null;
  lens: Lens;
  saving: boolean;
  saveError: string | null;
  onDecide: (decision: DecisionInput) => Promise<void>;
}

// Rows drawn at once. A database of thousands of tables has tens of thousands
// of columns; drawing them all made the page slow to open and every click on a
// row slow to answer. Search and the filters still cover every column.
const SHOWN_ROWS = 1000;

/** A rule's name on screen: the label the server gives its generator. */
function ruleLabel(generators: GeneratorsPayload | null, name: string | null): string {
  if (!name) return "Not set";
  return generators?.generators.find((g) => g.name === name)?.label ?? name.replace(/_/g, " ");
}

export function Schema({
  schema,
  catalog,
  generators,
  lens,
  saving,
  saveError,
  onDecide,
}: SchemaProps) {
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [limit, setLimit] = useState(SHOWN_ROWS);

  const rows = useMemo(() => {
    const all = schema.tables.flatMap((table) => table.columns);
    const needle = search.trim().toLowerCase();
    return all.filter((column) => {
      if (filter === "queued") {
        if (column.bucket === "system") return false;
        const unsure =
          !column.mappingAccepted || (column.confidence && column.confidence !== "high");
        if (!unsure) return false;
      } else if (filter !== "all" && column.bucket !== filter) {
        return false;
      }
      if (!needle) return true;
      return (
        column.name.toLowerCase().includes(needle) ||
        column.table.toLowerCase().includes(needle) ||
        (column.element ?? "").toLowerCase().includes(needle) ||
        (column.generator ?? "").toLowerCase().includes(needle)
      );
    });
  }, [schema, filter, search]);

  useEffect(() => setLimit(SHOWN_ROWS), [schema, filter, search]);

  const current =
    rows.find((row) => `${row.table}.${row.name}` === selected) ?? null;

  function select(key: string) {
    setSelected(selected === key ? null : key);
    setEditing(false);
  }

  const grouped = useMemo(() => {
    const map = new Map<string, SchemaColumn[]>();
    for (const row of rows.slice(0, limit)) {
      const list = map.get(row.table) ?? [];
      list.push(row);
      map.set(row.table, list);
    }
    return map;
  }, [rows, limit]);

  const tableByName = useMemo(
    () => new Map(schema.tables.map((t) => [t.name, t])),
    [schema],
  );

  // Which of the eight columns this lens is about.
  const show = (column: string) => SHOWN[lens].has(column);

  return (
    <div className={"schema" + (current ? " schema--split" : "")}>
      <div className="schema__main">
        <header className="schema__head">
          <div>
            <h2 className="schema__title">{HEADINGS[lens].title}</h2>
            <p className="schema__lede">
              {formatCount(rows.length)} of{" "}
              {formatCount(schema.tables.reduce((n, t) => n + t.columns.length, 0))} columns.{" "}
              {HEADINGS[lens].lede}
            </p>
          </div>
          <input
            className="schema__search"
            value={search}
            placeholder="Search columns, tables or elements"
            onChange={(event) => setSearch(event.target.value)}
            aria-label="Search columns"
          />
        </header>

        <div className="schema__filters" role="tablist">
          {FILTERS.map((option) => (
            <button
              key={option.id}
              role="tab"
              aria-selected={filter === option.id}
              className={"schema__filter" + (filter === option.id ? " is-on" : "")}
              onClick={() => setFilter(option.id)}
            >
              {option.label}
            </button>
          ))}
        </div>

        <div className="schema__scroll">
          {[...grouped.entries()].map(([tableName, columns]) => {
            const table = tableByName.get(tableName);
            const inferred = table?.foreignKeys.filter((fk) => fk.kind === "inferred") ?? [];
            return (
              <section key={tableName} className="schema__group">
                <h3 className="schema__group-head">
                  <span className="mono">{shortName(tableName)}</span>
                  <span className="schema__group-meta num">
                    {formatCount(table?.rowCount)} rows
                  </span>
                  {inferred.length > 0 && (
                    <Chip tone="structural" fill="outline"
                      title={inferred[0].evidence.join("; ")}>
                      inferred key
                    </Chip>
                  )}
                  {table?.triggers.length ? (
                    <Chip tone="caution" fill="outline" title={table.triggers.join(", ")}>
                      trigger
                    </Chip>
                  ) : null}
                </h3>

                <div className="schema__table-wrap">
                <table className="schema__table">
                  <colgroup>
                    {show("name") && <col className="c-name" />}
                    {show("type") && <col className="c-type" />}
                    {show("role") && <col className="c-role" />}
                    {show("element") && <col className="c-element" />}
                    {show("rule") && <col className="c-rule" />}
                    {show("nulls") && <col className="c-nulls" />}
                    {show("distinct") && <col className="c-distinct" />}
                    {show("shape") && <col className="c-shape" />}
                  </colgroup>
                  <thead>
                    <tr>
                      {show("name") && <th>Column</th>}
                      {show("type") && <th>Type</th>}
                      {show("role") && <th>Role</th>}
                      {show("element") && <th>Element</th>}
                      {show("rule") && <th>Rule</th>}
                      {show("nulls") && <th className="num">Nulls</th>}
                      {show("distinct") && <th className="num">Distinct</th>}
                      {show("shape") && <th>Shape</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {columns.map((column) => {
                      const key = `${column.table}.${column.name}`;
                      return (
                        <tr
                          key={key}
                          className={selected === key ? "is-selected" : ""}
                          onClick={() => select(key)}
                          tabIndex={0}
                          onKeyDown={(event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              select(key);
                            }
                          }}
                        >
                          {show("name") && (
                          <td className="mono schema__cell-name">
                            {column.name}
                            {column.decision?.approved && (
                              <span className="schema__yours" title="User override">
                                ●
                              </span>
                            )}
                          </td>
                          )}
                          {show("type") && (
                            <td className="mono schema__cell-type">{column.type}</td>
                          )}
                          {show("role") && (
                            <td><BucketMark bucket={column.bucket} /></td>
                          )}
                          {show("element") && (
                          <td className="schema__cell-element">
                            {column.element ? (
                              <span
                                className={
                                  "mono" +
                                  (column.mappingAccepted ? "" : " schema__maybe")
                                }
                                title={
                                  column.mappingAccepted
                                    ? undefined
                                    : `Closest match (not accepted). ${column.mappingReason}`
                                }
                              >
                                {column.element}
                              </span>
                            ) : (
                              <span className="schema__none">none</span>
                            )}
                          </td>
                          )}
                          {show("rule") && (
                          <td>
                            {column.generator && (
                              <RuleChip
                                provenance={column.provenance}
                                confidence={column.confidence}
                              >
                                {ruleLabel(generators, column.generator)}
                              </RuleChip>
                            )}
                          </td>
                          )}
                          {show("nulls") && (
                            <td className="num schema__cell-num">
                              {column.profile ? formatPercent(column.profile.nullRate) : "—"}
                            </td>
                          )}
                          {show("distinct") && (
                            <td className="num schema__cell-num">
                              {column.profile ? formatCount(column.profile.distinctCount) : "—"}
                            </td>
                          )}
                          {show("shape") && (
                            <td className="mono schema__cell-format">
                              {column.profile?.format?.regex ?? ""}
                            </td>
                          )}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                </div>
              </section>
            );
          })}
          {rows.length > limit && (
            <div className="schema__more">
              <span>
                Showing {formatCount(limit)} of {formatCount(rows.length)} columns. Search or
                apply a filter to narrow the list.
              </span>
              <button
                type="button"
                className="schema__filter"
                onClick={() => setLimit(limit + SHOWN_ROWS)}
              >
                Show {formatCount(Math.min(SHOWN_ROWS, rows.length - limit))} more
              </button>
            </div>
          )}
          {rows.length === 0 && (
            <p className="schema__none schema__none--block">
              No matching columns. Change the filter or clear the search.
            </p>
          )}
        </div>
      </div>

      {current && (
        <ColumnDetail
          column={current}
          catalog={catalog}
          generators={generators}
          editing={editing}
          saving={saving}
          saveError={saveError}
          onEdit={() => setEditing(true)}
          onStopEditing={() => setEditing(false)}
          onDecide={onDecide}
          onClose={() => {
            setSelected(null);
            setEditing(false);
          }}
        />
      )}
    </div>
  );
}

function ColumnDetail({
  column,
  catalog,
  generators,
  editing,
  saving,
  saveError,
  onEdit,
  onStopEditing,
  onDecide,
  onClose,
}: {
  column: SchemaColumn;
  catalog: CatalogPayload | null;
  generators: GeneratorsPayload | null;
  editing: boolean;
  saving: boolean;
  saveError: string | null;
  onEdit: () => void;
  onStopEditing: () => void;
  onDecide: (decision: DecisionInput) => Promise<void>;
  onClose: () => void;
}) {
  const profile = column.profile;
  const overridden = Boolean(column.decision?.approved);

  // An element the tool found but did not accept is a candidate, not an answer.
  // Offering it as the filled-in value would turn one Save into a confirmation
  // of a guess nobody made.
  const element = column.mappingAccepted ? column.element : null;

  // The column carries the rule that will actually run, which is the override
  // when there is one. The proposal is carried alongside it so the editor can
  // show what it replaced and put it back.
  const rule: EditableRule = {
    table: column.table,
    column: column.name,
    element,
    generator: column.generator,
    params: column.params,
    proposedGenerator: column.proposedGenerator,
    proposedParams: column.proposedParams,
    proposedElement: column.proposedElement,
    isStructural: column.provenance === "structural",
    isOverridden: overridden,
  };

  return (
    <aside className="detail" aria-label={`${column.name} detail`}>
      <header className="detail__head">
        <div>
          <div className="detail__name mono">{column.name}</div>
          <div className="detail__where mono">{shortName(column.table)}</div>
        </div>
        <button className="detail__close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </header>

      <div className="detail__body">
        <section className="detail__section">
          <div className="detail__row">
            <BucketMark bucket={column.bucket} />
            <span className="detail__type mono">{column.type}</span>
            {column.nullable && <span className="detail__type">nullable</span>}
          </div>
          <p className="detail__reason">{column.bucketReason}</p>
        </section>

        {profile && (
          <section className="detail__section">
            <h4 className="detail__heading">Profile</h4>
            <Field label="Rows">{formatCount(profile.rowCount)}</Field>
            <Field label="Null rate">
              <Share value={profile.nullRate} tone="physical" />
            </Field>
            <Field label="Distinct">
              {formatCount(profile.distinctCount)}{" "}
              <span className="detail__muted">{profile.cardinality}</span>
            </Field>
            {profile.minimum != null && (
              <Field label="Range">
                <span className="mono">
                  {String(profile.minimum)} … {String(profile.maximum)}
                </span>
              </Field>
            )}
            {profile.minLength != null && (
              <Field label="Length">
                {profile.minLength} to {profile.maxLength}
              </Field>
            )}
            {profile.mean != null && (
              <Field label="Mean">
                {profile.mean.toFixed(2)}
                {profile.stddev != null && (
                  <span className="detail__muted"> ± {profile.stddev.toFixed(2)}</span>
                )}
              </Field>
            )}
            {profile.format && (
              <Field label="Format">
                <span className="mono">{profile.format.regex}</span>{" "}
                <span className="detail__muted">
                  {formatPercent(profile.format.coverage)} coverage
                </span>
              </Field>
            )}
            {profile.checkDigit && (
              <Field label="Check digit">
                {profile.checkDigit.scheme.toUpperCase()}{" "}
                <span className="detail__muted">
                  {formatPercent(profile.checkDigit.share)} of values valid
                </span>
              </Field>
            )}
            {profile.sequence?.is_sequence && (
              <Field label="Sequence">
                increment {profile.sequence.step}, maximum{" "}
                {formatCount(profile.sequence.max)}
              </Field>
            )}
            {profile.distribution && (
              <Field label="Distribution">
                {String(profile.distribution.kind)}
              </Field>
            )}
          </section>
        )}

        {profile && profile.topValues.length > 0 && (
          <section className="detail__section">
            <h4 className="detail__heading">Most common values</h4>
            <ul className="detail__values">
              {profile.topValues.slice(0, 10).map((value, index) => (
                <li key={index}>
                  <span className="mono">
                    {value.value === null || value.value === ""
                      ? "(blank)"
                      : String(value.value)}
                  </span>
                  <Share value={value.share} tone="catalog" />
                </li>
              ))}
            </ul>
          </section>
        )}

        <section className="detail__section">
          <h4 className="detail__heading">Element mapping</h4>
          {column.element ? (
            <>
              <div className="detail__row">
                <span className="detail__element mono">{column.element}</span>
                {column.mappingConfidence && (
                  <Chip
                    tone={column.mappingAccepted ? "learned" : "caution"}
                    fill={column.mappingAccepted ? "solid" : "outline"}
                  >
                    {column.mappingAccepted ? "accepted" : column.mappingConfidence}
                  </Chip>
                )}
              </div>
              {column.element === column.proposedElement ? (
                <>
                  <p className="detail__reason">{column.mappingReason}</p>
                  <EvidenceList items={column.mappingEvidence} />
                </>
              ) : (
                // The evidence below argues for the element the tool picked, not
                // the one a person chose, so it is not shown under their choice.
                <p className="detail__reason">
                  Element set by user. Proposed element:{" "}
                  {column.proposedElement ? (
                    <span className="mono">{column.proposedElement}</span>
                  ) : (
                    "none"
                  )}
                  .
                </p>
              )}
              {column.alternatives.length > 0 && (
                <p className="detail__muted detail__alternatives">
                  Alternatives: {column.alternatives.join(", ")}
                </p>
              )}
            </>
          ) : (
            <p className="detail__reason">
              No catalog element matches this column.
            </p>
          )}
        </section>

        {(column.generator || column.provenance !== "structural") && (
          <section className="detail__section">
            <div className="detail__section-head">
              <h4 className="detail__heading">Rule</h4>
              {!editing && (
                <Button onClick={onEdit}>
                  Edit rule
                </Button>
              )}
            </div>

            {editing ? (
              <RuleEditor
                rule={rule}
                catalog={catalog}
                generators={generators}
                saving={saving}
                error={saveError}
                onSave={async (patch) => {
                  await onDecide({
                    table: column.table,
                    column: column.name,
                    approved: true,
                    ...patch,
                  });
                  onStopEditing();
                }}
                onReset={async () => {
                  await onDecide({
                    table: column.table,
                    column: column.name,
                    reset: true,
                  });
                  onStopEditing();
                }}
                onCancel={onStopEditing}
              />
            ) : (
              <>
                {overridden && (
                  <p className="detail__yours">
                    User override. The proposed rule can be restored.
                  </p>
                )}
                <div className="detail__row">
                  <RuleChip provenance={column.provenance} confidence={column.confidence}>
                    {ruleLabel(generators, column.generator)}
                  </RuleChip>
                  {column.provenance && (
                    <span className="detail__muted">
                      {PROVENANCE_WORDS[column.provenance as Provenance]}
                    </span>
                  )}
                </div>
                {Object.keys(column.params).length > 0 && (
                  <pre className="detail__params">
                    {describeParams(column.params).join("\n")}
                  </pre>
                )}
                <EvidenceList items={column.evidence} />
                {column.note && <p className="detail__note">{column.note}</p>}
              </>
            )}
          </section>
        )}
      </div>
    </aside>
  );
}
