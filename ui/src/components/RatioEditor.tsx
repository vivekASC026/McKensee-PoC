/**
 * Changing how many children each parent gets.
 *
 * The measured shape is a histogram with a long tail, which is the honest
 * answer and not something anyone should retype. So this does not offer to edit
 * the histogram: it offers to replace it with a shape a person can state in a
 * sentence, and to put the measured one back.
 *
 * The childless share is offered with both replacement shapes, because a flat
 * ratio with no childless share is the thing that makes generated data look
 * unlike the real thing.
 */

import { useState } from "react";
import type { ParamField, RatioSetting, RatioShapesPayload } from "../lib/api";
import { Banner, Button } from "./primitives";
import "./RuleEditor.css";

interface Props {
  parent: string;
  shapes: RatioShapesPayload | null;
  current: RatioSetting | null;
  saving: boolean;
  error: string | null;
  onSave: (setting: RatioSetting | null) => void;
  onCancel: () => void;
}

export function RatioEditor({
  parent,
  shapes,
  current,
  saving,
  error,
  onSave,
  onCancel,
}: Props) {
  const [kind, setKind] = useState(current?.kind ?? "measured");
  const [values, setValues] = useState<Record<string, unknown>>(() => {
    const { kind: _kind, ...rest } = current ?? {};
    return rest;
  });

  const spec = shapes?.shapes.find((s) => s.name === kind);

  return (
    <div className="editor editor--compact">
      {error && <Banner tone="alarm">{error}</Banner>}

      <label className="editor__field">
        <span className="editor__label">Children per {parent}</span>
        <select value={kind} onChange={(event) => setKind(event.target.value)}>
          {(shapes?.shapes ?? []).map((shape) => (
            <option key={shape.name} value={shape.name}>
              {shape.label}
            </option>
          ))}
        </select>
        {spec && <span className="editor__help">{spec.summary}</span>}
      </label>

      {(spec?.fields ?? []).map((field) => (
        <NumberInput
          key={field.name}
          field={field}
          value={values[field.name]}
          onChange={(next) => setValues({ ...values, [field.name]: next })}
        />
      ))}

      <div className="editor__actions">
        <Button
          variant="primary"
          busy={saving}
          onClick={() =>
            onSave(kind === "measured" ? null : { kind, ...values })
          }
        >
          Save
        </Button>
        <Button onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function NumberInput({
  field,
  value,
  onChange,
}: {
  field: ParamField;
  value: unknown;
  onChange: (next: unknown) => void;
}) {
  return (
    <label className="editor__field">
      <span className="editor__label">
        {field.label}
        {field.required && <span className="editor__required"> required</span>}
      </span>
      <input
        className="num"
        type="number"
        step={field.type === "integer" ? 1 : "any"}
        value={value === null || value === undefined ? "" : String(value)}
        onChange={(event) =>
          onChange(event.target.value === "" ? null : event.target.value)
        }
      />
      {field.help && <span className="editor__help">{field.help}</span>}
    </label>
  );
}
