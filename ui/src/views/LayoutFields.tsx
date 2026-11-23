/**
 * Which rule fills each field of a layout, and the choice of it.
 *
 * A field binds to a rule by name when the names agree, by alias when somebody
 * said they mean the same, and otherwise to nothing, which writes it blank.
 * That last case was the end of the road: a sample calling first name
 * MBR_FNAME wrote a blank column, with no way to say what it was. Here every
 * field's rule can be picked, and a field can be chosen blank on purpose --
 * a filler in a fixed-width record is that, not a failure to match.
 *
 * The same table serves a layout just read from a sample and a saved one being
 * edited, so the two cannot drift apart in what they let a person change.
 */

import { Fragment, useState } from "react";
import { Banner, Button, Chip } from "../components/primitives";
import { ApiError, api, type GenerationRule, type LayoutField, type RulesPayload } from "../lib/api";
import { RuleForm } from "./Rules";

export const DELIMITERS: ReadonlyArray<readonly [string, string]> = [
  [",", "Comma"],
  ["|", "Pipe"],
  ["\t", "Tab"],
  [";", "Semicolon"],
];

/** A layout's kind as a person reads it. */
export function layoutKindName(kind: string): string {
  return kind === "fixed" ? "Fixed width" : kind === "csv" ? "Delimited" : kind;
}

/** A layout's status as a person reads it. */
export function layoutStatusName(status: string): string {
  const names: Record<string, string> = {
    pending_review: "Pending approval",
    approved: "Approved",
    draft: "Draft",
    archived: "Archived",
  };
  return names[status] ?? status.replace(/_/g, " ");
}

/** The field with a rule chosen for it, or chosen blank when rule is null. */
export function assignRule(field: LayoutField, rule: GenerationRule | null): LayoutField {
  if (rule === null) {
    return {
      ...field,
      rule_id: null,
      rule_name: null,
      rule_label: "Left blank",
      gen_method: "default",
      is_default: true,
      blank: true,
      matched_by: "blank",
    };
  }
  const own = field.name.trim().toUpperCase() === rule.field_name.trim().toUpperCase();
  return {
    ...field,
    rule_id: rule.id,
    rule_name: rule.field_name,
    rule_label: rule.field_label ?? rule.field_name,
    gen_method: rule.gen_method,
    is_default: false,
    blank: false,
    // Kept as it was when the same rule is picked again: an alias match stays
    // an alias match, and only a real choice is remembered as one.
    matched_by:
      field.rule_id === rule.id && field.matched_by
        ? field.matched_by
        : own
          ? "exact"
          : "chosen",
  };
}

/** The rule a field is bound to: by id, or, for a hand-defined one, by name. */
export function boundRule(field: LayoutField, rules: GenerationRule[]): GenerationRule | null {
  if (field.blank) return null;
  if (field.rule_id != null) return rules.find((r) => r.id === field.rule_id) ?? null;
  const name = field.name.trim().toUpperCase();
  return rules.find((r) => r.field_name.trim().toUpperCase() === name) ?? null;
}

/** A fixed-width field resized, with every field after it moved along. */
export function resize(fields: LayoutField[], index: number, width: number): LayoutField[] {
  const before = fields[index].length ?? fields[index].max_width ?? 0;
  const by = width - before;
  return fields.map((f, i) => {
    if (i === index) return { ...f, length: width, max_width: width };
    if (i > index && f.start != null) return { ...f, start: f.start + by };
    return f;
  });
}

const HOW: Record<string, [string, "catalog" | "learned" | "user" | "neutral"]> = {
  exact: ["Name match", "catalog"],
  alias: ["Alias match", "learned"],
  chosen: ["Manual", "user"],
  blank: ["Blank", "neutral"],
};

export function RuleChoice({
  field,
  rules,
  onChange,
  onCustom,
  onEdit,
}: {
  field: LayoutField;
  rules: GenerationRule[];
  onChange: (field: LayoutField) => void;
  /** A rule of the field's own, when no business element fits. */
  onCustom?: () => void;
  /** Open the bound rule's editor. */
  onEdit?: () => void;
}) {
  const bound = boundRule(field, rules);
  // A rule that has since been retired still shows, so the choice on screen is
  // the one the layout holds rather than silently a different one.
  const missing = !bound && field.rule_id != null && !field.blank;
  const how = field.blank
    ? HOW.blank
    : bound
      ? HOW[field.matched_by ?? "exact"] ?? HOW.exact
      : null;

  return (
    <span className="fieldrule">
      <select
        aria-label={`Rule for ${field.name}`}
        value={bound ? String(bound.id) : missing ? `gone:${field.rule_id}` : "blank"}
        onChange={(e) => {
          const value = e.target.value;
          if (value === "blank") return onChange(assignRule(field, null));
          if (value === "custom") return onCustom?.();
          const rule = rules.find((r) => String(r.id) === value);
          if (rule) onChange(assignRule(field, rule));
        }}
      >
        <option value="blank">Blank (no value)</option>
        {onCustom && <option value="custom">Custom rule…</option>}
        {missing && (
          <option value={`gone:${field.rule_id}`} disabled>
            {field.rule_label ?? `Rule ${field.rule_id}`} (retired)
          </option>
        )}
        {rules.map((rule) => (
          <option key={rule.id} value={rule.id}>
            {rule.field_name}
            {rule.field_label && rule.field_label !== rule.field_name
              ? ` — ${rule.field_label}`
              : ""}
          </option>
        ))}
      </select>
      {how ? (
        <Chip tone={how[1]} fill="outline">
          {how[0]}
        </Chip>
      ) : (
        <Chip tone="caution" fill="outline" title="No rule matches this field name or alias.">
          No match
        </Chip>
      )}
      {bound && onEdit && (
        <Button onClick={onEdit} title="View or edit the rule for this field">
          Edit rule
        </Button>
      )}
    </span>
  );
}

/** The fields of a layout, each with its rule, and widths where they matter. */
export function FieldsTable({
  kind,
  fields,
  rules,
  onChange,
  widths = false,
}: {
  kind: string;
  fields: LayoutField[];
  rules: GenerationRule[];
  onChange: (fields: LayoutField[]) => void;
  /** Whether a fixed-width field's width can be changed here. */
  widths?: boolean;
}) {
  const fixed = kind === "fixed";
  const put = (index: number, field: LayoutField) =>
    onChange(fields.map((f, i) => (i === index ? field : f)));

  // The rule editor opened under one field, and the repository it edits.
  const [open, setOpen] = useState<number | null>(null);
  const [meta, setMeta] = useState<RulesPayload | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const known = meta ? meta.rules.filter((r) => r.active !== false) : rules;
  const columns = 3 + (fixed ? 2 : 0);

  const reload = async () => {
    const next = await api.rules();
    setMeta(next);
    return next;
  };

  const edit = async (index: number) => {
    setProblem(null);
    try {
      if (!meta) await reload();
      setOpen(open === index ? null : index);
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  };

  // No business element fits: the field gets a rule of its own, named for it,
  // and its editor opens to say how the values are made.
  const custom = async (index: number) => {
    const field = fields[index];
    const name = field.name.trim().toUpperCase().replace(/[^A-Z0-9_]+/g, "_");
    setProblem(null);
    try {
      if (!known.some((r) => r.field_name.toUpperCase() === name)) {
        await api.createRule({ field_name: name, field_label: field.name,
                               gen_method: "pattern", element: name });
      }
      const next = await reload();
      const rule = next.rules.find((r) => r.field_name.toUpperCase() === name);
      if (rule) put(index, assignRule(field, rule));
      setOpen(index);
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  };

  return (
    <>
    {problem && <Banner tone="alarm">{problem}</Banner>}
    <table className="panel__table fields">
      <thead>
        <tr>
          <th>Field</th>
          {fixed && <th className="right">Start</th>}
          {fixed && <th className="right">Length</th>}
          <th>Sample value</th>
          <th>Rule</th>
        </tr>
      </thead>
      <tbody>
        {fields.map((field, index) => {
          const bound = boundRule(field, known);
          return (
          <Fragment key={`${index}-${field.name}`}>
          <tr>
            <td className="mono">
              {field.name}
              {field.date_format && (
                <span className="fields__format"> {field.date_format}</span>
              )}
            </td>
            {fixed && <td className="right num">{field.start ?? "—"}</td>}
            {fixed && (
              <td className="right num">
                {widths ? (
                  <input
                    className="designer__width"
                    type="number"
                    min={1}
                    max={255}
                    aria-label={`Width of ${field.name}`}
                    value={field.length ?? field.max_width ?? 1}
                    onChange={(e) =>
                      onChange(resize(fields, index, Math.max(1, Number(e.target.value) || 1)))
                    }
                  />
                ) : (
                  field.length ?? field.max_width ?? "—"
                )}
              </td>
            )}
            <td className="mono fields__sample">{field.samples?.[0] ?? ""}</td>
            <td>
              <RuleChoice
                field={field}
                rules={known}
                onChange={(f) => put(index, f)}
                onCustom={() => void custom(index)}
                onEdit={() => void edit(index)}
              />
            </td>
          </tr>
          {open === index && bound && meta && (
            <tr className="fields__rule">
              <td colSpan={columns}>
                <RuleForm
                  key={bound.id}
                  rule={meta.rules.find((r) => r.id === bound.id) ?? bound}
                  methods={meta.methods}
                  lookups={meta.lookups}
                  onSaved={async () => {
                    await reload();
                  }}
                  onError={setProblem}
                >
                  <Button onClick={() => setOpen(null)}>Done</Button>
                </RuleForm>
              </td>
            </tr>
          )}
          </Fragment>
          );
        })}
      </tbody>
    </table>
    </>
  );
}
