/**
 * Changing a saved layout.
 *
 * Saved layouts could only be approved and written through, so a field that
 * fell to blank when the sample was read stayed blank for good unless the
 * sample was read again. Here a saved layout's name, delimiter, field widths
 * and the rule behind each field can all be changed.
 *
 * Any change sends it back to review. Its approval was of the fields and rules
 * as they were, and an approval that quietly covered something else would
 * mean nothing.
 */

import { useCallback, useEffect, useState } from "react";
import { Banner, Button } from "../components/primitives";
import {
  ApiError,
  api,
  type AliasOutcome,
  type GenerationRule,
  type LayoutDetail,
  type LayoutField,
} from "../lib/api";
import { DELIMITERS, FieldsTable } from "./LayoutFields";

/** What picking rules taught the repository, said after a save. */
export function aliasNote(result: AliasOutcome): string {
  const taught = result.aliased ?? [];
  const refused = result.not_aliased ?? [];
  return (
    (taught.length
      ? ` ${taught.join(", ")} will bind by alias going forward.`
      : "") +
    (refused.length
      ? ` Not saved as aliases because they already map to another element: ` +
        `${refused.map((r) => r.name).join(", ")}. This layout still uses the selected rules.`
      : "")
  );
}

export function LayoutEditor({
  layoutId,
  rules,
  onClose,
  onSaved,
  approvalRequired = false,
}: {
  layoutId: number;
  rules: GenerationRule[];
  onClose: () => void;
  onSaved: (note: string) => void;
  /** A second approver is configured: a changed layout waits for approval. */
  approvalRequired?: boolean;
}) {
  const [layout, setLayout] = useState<LayoutDetail | null>(null);
  const [name, setName] = useState("");
  const [delimiter, setDelimiter] = useState(",");
  const [fields, setFields] = useState<LayoutField[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .layout(layoutId)
      .then((detail) => {
        if (!live) return;
        setLayout(detail);
        setName(detail.name);
        setDelimiter(detail.delimiter ?? ",");
        setFields(detail.fields);
      })
      .catch((e) => live && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [layoutId]);

  const save = useCallback(async () => {
    if (!layout) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.updateLayout(layoutId, {
        name: name.trim(),
        delimiter: layout.kind === "fixed" ? null : delimiter,
        fields,
      });
      onSaved(
        `${result.name} saved, ${result.n_fields} fields` +
          (result.n_default ? `, ${result.n_default} written blank` : "") +
          (!approvalRequired
            ? "."
            : result.reapproval_required
              ? ". The layout needs approval again before it generates files."
              : ", pending approval.") +
          aliasNote(result),
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [layout, layoutId, name, delimiter, fields, onSaved, approvalRequired]);

  // A copy under a name of its own, leaving this one as it was. It starts in
  // review like any new layout.
  const saveAs = useCallback(async () => {
    if (!layout) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.saveLayout({
        name: name.trim(),
        kind: layout.kind,
        delimiter: layout.kind === "fixed" ? null : delimiter,
        fields,
        sample_name: layout.sample_name,
        purpose: layout.purpose || "generation",
        as_new: true,
      });
      onSaved(
        `Saved as new layout ${result.name}${approvalRequired ? ", pending approval" : ""}. ` +
          `${layout.name} was not changed.` +
          aliasNote(result),
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [layout, name, delimiter, fields, onSaved, approvalRequired]);

  if (!layout) {
    return error ? <Banner tone="alarm">{error}</Banner> : null;
  }

  const fixed = layout.kind === "fixed";
  const blanks = fields.filter((f) => f.is_default || f.blank).length;

  return (
    <section className="designer editor" aria-label={`Edit layout ${layout.name}`}>
      <div className="designer__head">
        <h3>Edit layout: {layout.name}</h3>
        <p className="panel__hint">
          {fixed ? "Fixed width" : "Delimited"}, {fields.length} fields
          {blanks ? `, ${blanks} written blank` : ""}.
          {approvalRequired && layout.status === "approved"
            ? " Saving changes returns the layout to pending approval."
            : ""}
        </p>
      </div>

      {error && <Banner tone="alarm">{error}</Banner>}

      <div className="designer__controls">
        <label className="named designer__name">
          <span>Layout name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        {!fixed && (
          <label className="named">
            <span>Delimiter</span>
            <select
              aria-label="Delimiter"
              value={delimiter}
              onChange={(e) => setDelimiter(e.target.value)}
            >
              {DELIMITERS.map(([value, label]) => (
                <option key={label} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <FieldsTable kind={layout.kind} fields={fields} rules={rules} onChange={setFields} widths />

      <div className="panel__actions">
        <Button variant="primary" onClick={save} busy={busy} disabled={!name.trim()}>
          Save changes
        </Button>
        <Button
          onClick={saveAs}
          busy={busy}
          disabled={!name.trim() || name.trim() === layout.name}
        >
          Save as new layout
        </Button>
        <Button onClick={onClose}>Cancel</Button>
      </div>
    </section>
  );
}
