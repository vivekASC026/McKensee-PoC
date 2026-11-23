/**
 * Changing what the tool decided.
 *
 * Everything the tool worked out is a proposal, including the parts it was
 * confident about, so every column gets the same editor rather than only the
 * ones that reached the queue. The form is built from the generator
 * descriptions the server sends, which means a new generator becomes editable
 * without anything changing here.
 *
 * The rule it proposed is always still visible while you are overriding it, and
 * one button puts it back.
 */

import { useEffect, useMemo, useState } from "react";
import type {
  CatalogPayload,
  GeneratorSpec,
  GeneratorsPayload,
  ParamField,
} from "../lib/api";
import { Banner, Button, Chip } from "./primitives";
import "./RuleEditor.css";

export interface EditableRule {
  table: string;
  column: string;
  element: string | null;
  generator: string | null;
  params: Record<string, unknown>;
  /** What the tool proposed, so it can be shown and restored. */
  proposedGenerator: string | null;
  proposedParams: Record<string, unknown>;
  proposedElement: string | null;
  isStructural: boolean;
  isOverridden: boolean;
}

interface Props {
  rule: EditableRule;
  catalog: CatalogPayload | null;
  generators: GeneratorsPayload | null;
  saving: boolean;
  error: string | null;
  /** An element brings its own rule, so only a custom column sends one. */
  onSave: (patch: {
    element: string | null;
    generator: string | null;
    params: Record<string, unknown> | null;
  }) => void;
  onReset: () => void;
  onCancel: () => void;
}

function trimLines(lines: string[]): string[] {
  return lines.map((line) => line.trim());
}

/** The settings with every list field's lines trimmed, as saved. */
function trimListFields(
  values: Record<string, unknown>,
  spec: GeneratorSpec | undefined,
): Record<string, unknown> {
  const lists = new Set(
    (spec?.fields ?? [])
      .filter((f) => f.type === "lines" || f.type === "numbers")
      .map((f) => f.name),
  );
  return Object.fromEntries(
    Object.entries(values).map(([key, value]) => [
      key,
      lists.has(key) && Array.isArray(value)
        ? value.map((line: unknown) => (typeof line === "string" ? line.trim() : line))
        : value,
    ]),
  );
}

/**
 * A column is given a business element, and the element brings its rule.
 * Only a column with no business element -- Custom -- has its rule chosen
 * here, from the algorithms the engine has.
 */
export function RuleEditor({
  rule,
  catalog,
  generators,
  saving,
  error,
  onSave,
  onReset,
  onCancel,
}: Props) {
  const [element, setElement] = useState(rule.element ?? "");
  const [generator, setGenerator] = useState(rule.generator ?? "pattern");
  const [values, setValues] = useState<Record<string, unknown>>(rule.params ?? {});

  useEffect(() => {
    setElement(rule.element ?? "");
    setGenerator(rule.generator ?? "pattern");
    setValues(rule.params ?? {});
  }, [rule.table, rule.column]);

  const spec: GeneratorSpec | undefined = useMemo(
    () => generators?.generators.find((g) => g.name === generator),
    [generators, generator],
  );

  /** Switching algorithm keeps any setting the new one also understands. */
  function changeGenerator(next: string) {
    const nextSpec = generators?.generators.find((g) => g.name === next);
    const keep = new Set((nextSpec?.fields ?? []).map((f) => f.name));
    const carried = Object.fromEntries(
      Object.entries(values).filter(([key]) => keep.has(key)),
    );
    setGenerator(next);
    setValues(
      next === rule.proposedGenerator
        ? { ...rule.proposedParams }
        : carried,
    );
  }

  const elements = catalog?.elements ?? [];
  const chosen = elements.find((e) => e.name === element);
  const custom = element === "";
  const ruleOf = (candidate: typeof chosen) =>
    !candidate
      ? ""
      : candidate.rule === "reuse"
        ? "Reused from reference data"
        : candidate.rule === "copy"
          ? "Copied from source"
          : [
              generators?.generators.find((g) => g.name === candidate.generator)?.label ??
                candidate.generator,
              candidate.format ? `format ${candidate.format}` : null,
            ].filter(Boolean).join(", ");

  return (
    <div className="editor">
      {rule.isStructural && generators?.structuralWarning && (
        <Banner tone="caution">{generators.structuralWarning}</Banner>
      )}
      {error && <Banner tone="alarm">{error}</Banner>}

      <label className="editor__field">
        <span className="editor__label">Business element</span>
        <select value={element} onChange={(event) => setElement(event.target.value)}>
          <option value="">Custom (no business element)</option>
          {elements.map((candidate) => (
            <option key={candidate.name} value={candidate.name}>
              {candidate.name}
            </option>
          ))}
        </select>
        {chosen && <span className="editor__help">{chosen.description}</span>}
      </label>

      {chosen ? (
        <div className="editor__field">
          <span className="editor__label">Rule</span>
          <span className="editor__rule">
            <Chip tone="catalog" fill="outline">{ruleOf(chosen)}</Chip>
          </span>
          <span className="editor__help">
            To override it for this column only, select Custom.
          </span>
        </div>
      ) : (
        <>
          <label className="editor__field">
            <span className="editor__label">Rule</span>
            <select value={generator} onChange={(event) => changeGenerator(event.target.value)}>
              {(generators?.generators ?? []).map((candidate) => (
                <option key={candidate.name} value={candidate.name}>
                  {candidate.label}
                </option>
              ))}
            </select>
            {spec && <span className="editor__help">{spec.summary}</span>}
          </label>

          {(spec?.fields ?? []).map((field) => (
            <ParamInput
              key={field.name}
              field={field}
              value={values[field.name]}
              onChange={(next) => setValues({ ...values, [field.name]: next })}
            />
          ))}

          {generator !== rule.proposedGenerator && rule.proposedGenerator && (
            <p className="editor__was">
              Proposed rule:{" "}
              {generators?.generators.find((g) => g.name === rule.proposedGenerator)?.label ??
                rule.proposedGenerator}.
            </p>
          )}
        </>
      )}

      <div className="editor__actions">
        <Button
          variant="primary"
          busy={saving}
          onClick={() =>
            onSave(
              custom
                ? { element: null, generator, params: trimListFields(values, spec) }
                : { element, generator: null, params: null },
            )
          }
        >
          Save
        </Button>
        <Button onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        {rule.isOverridden && (
          <Button onClick={onReset} disabled={saving} title="Restore the proposed rule">
            Restore proposed rule
          </Button>
        )}
      </div>
    </div>
  );
}

function ParamInput({
  field,
  value,
  onChange,
}: {
  field: ParamField;
  value: unknown;
  onChange: (next: unknown) => void;
}) {
  // A list of records, such as the ZIP dictionary, is not something to retype
  // into a textarea. It is shown as read-only with a count, and left alone.
  const isRecordList =
    Array.isArray(value) && value.length > 0 && typeof value[0] === "object";

  if (isRecordList) {
    const list = value as Record<string, unknown>[];
    return (
      <div className="editor__field">
        <span className="editor__label">{field.label}</span>
        <div className="editor__records">
          <Chip tone="catalog" fill="outline">
            {list.length.toLocaleString()} records
          </Chip>
          <span className="editor__help">
            Read-only.
          </span>
        </div>
      </div>
    );
  }

  if (field.type === "boolean") {
    return (
      <label className="editor__check">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span>
          <span className="editor__label editor__label--inline">{field.label}</span>
          {field.help && <span className="editor__help">{field.help}</span>}
        </span>
      </label>
    );
  }

  if (field.type === "select") {
    return (
      <label className="editor__field">
        <span className="editor__label">
          {field.label}
          {field.required && <span className="editor__required"> required</span>}
        </span>
        <select
          value={String(value ?? "")}
          onChange={(event) => onChange(event.target.value || null)}
        >
          <option value="">Not set</option>
          {(field.options ?? []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        {field.help && <span className="editor__help">{field.help}</span>}
      </label>
    );
  }

  if (field.type === "lines" || field.type === "numbers") {
    const text = Array.isArray(value) ? value.join("\n") : String(value ?? "");
    return (
      <label className="editor__field">
        <span className="editor__label">
          {field.label}
          {Array.isArray(value) && value.length > 0 && (
            <span className="editor__count"> {value.length.toLocaleString()}</span>
          )}
          {field.required && <span className="editor__required"> required</span>}
        </span>
        <textarea
          rows={Math.min(8, Math.max(3, text.split("\n").length))}
          value={text}
          // Lines are kept as typed so a space between words survives; they
          // are trimmed when the box is left and again when the rule is saved.
          onChange={(event) => onChange(event.target.value.split("\n"))}
          onBlur={(event) => onChange(trimLines(event.target.value.split("\n")))}
        />
        {field.help && <span className="editor__help">{field.help}</span>}
      </label>
    );
  }

  return (
    <label className="editor__field">
      <span className="editor__label">
        {field.label}
        {field.required && <span className="editor__required"> required</span>}
      </span>
      <input
        className={field.name === "regex" || field.name === "expression" ? "mono" : ""}
        type={field.type === "number" || field.type === "integer" ? "number" : "text"}
        step={field.type === "integer" ? 1 : "any"}
        value={value === null || value === undefined ? "" : String(value)}
        placeholder={field.placeholder}
        onChange={(event) =>
          onChange(event.target.value === "" ? null : event.target.value)
        }
      />
      {field.help && <span className="editor__help">{field.help}</span>}
    </label>
  );
}
