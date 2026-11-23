/**
 * Defining a layout rather than reading one.
 *
 * Reading a sample file is the quicker route and the only one that existed. It
 * assumes two things that are often untrue: that there is a sample, and that
 * the thing being described already has rows in it. A fixed-width layout is
 * usually a specification handed over before any data exists — column, width,
 * position, agreed in a document — and a delimited one may be the same.
 *
 * So the fields are named here, with their widths, and the layout is saved the
 * same way a parsed one is. There is no second kind of layout: this writes the
 * same record a sample would have produced.
 *
 * Fields are added from the rule repository rather than typed. A layout whose
 * names match no generated column writes a file of blanks — a 200 and an empty
 * file, which is the worst way for this to fail. A field starts out named after
 * its element, and can be renamed to what the receiving system calls it: the
 * rule beside it is what fills it, whatever the name. A filler is a field
 * chosen blank.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Banner, Button, Chip, Empty } from "../components/primitives";
import { ApiError, api, type GenerationRule, type LayoutField } from "../lib/api";
import { DELIMITERS, RuleChoice, assignRule } from "./LayoutFields";
import "./LayoutDesigner.css";

type Field = LayoutField & { length: number };

const DEFAULT_WIDTH = 20;
const BLANK = "__blank__";

interface Props {
  onSaved: () => void;
  /** A second approver is configured: a saved layout waits for approval. */
  approvalRequired?: boolean;
  onClose?: () => void;
}

export function LayoutDesigner({ onSaved, approvalRequired = false, onClose }: Props) {
  const [kind, setKind] = useState<"fixed" | "csv">("fixed");
  const [delimiter, setDelimiter] = useState(",");
  const [name, setName] = useState("");
  const [fields, setFields] = useState<Field[]>([]);
  const [known, setKnown] = useState<GenerationRule[]>([]);
  const [picking, setPicking] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  // The rules failing to load is said as that, not as "no rules defined".
  const [rulesError, setRulesError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .rules()
      .then((payload) => live && setKnown(payload.rules.filter((r) => r.active !== false)))
      .catch((e: unknown) => {
        if (live) setRulesError(e instanceof ApiError ? e.message : String(e));
      });
    return () => {
      live = false;
    };
  }, []);

  const taken = useMemo(
    () => new Set(fields.map((f) => f.rule_id).filter((id) => id != null)),
    [fields],
  );
  const offer = known.filter((rule) => !taken.has(rule.id));

  const add = useCallback(
    (choice: string) => {
      if (!choice) return;
      if (choice === BLANK) {
        setFields((previous) => {
          let n = 1;
          while (previous.some((f) => f.name === `FILLER_${n}`)) n += 1;
          return [...previous, { ...assignRule({ name: `FILLER_${n}` }, null), length: 1 }];
        });
      } else {
        const rule = known.find((r) => String(r.id) === choice);
        if (!rule || taken.has(rule.id)) return;
        setFields((previous) => [
          ...previous,
          {
            ...assignRule({ name: rule.field_name }, rule),
            length: Number(rule.length) || DEFAULT_WIDTH,
          },
        ]);
      }
      setPicking("");
    },
    [known, taken],
  );

  const move = useCallback((index: number, by: number) => {
    setFields((previous) => {
      const next = [...previous];
      const to = index + by;
      if (to < 0 || to >= next.length) return previous;
      [next[index], next[to]] = [next[to], next[index]];
      return next;
    });
  }, []);

  const save = useCallback(async () => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      // Positions are derived, not typed. A person editing a width should not
      // have to restate every start after it, and a layout whose starts and
      // lengths disagree writes rows nobody can parse.
      let start = 0;
      const payload = fields.map((field) => {
        const record = {
          ...field,
          name: field.name.trim(),
          // Renamed from its element, the field's name is a choice somebody
          // made, and is remembered as one.
          matched_by:
            !field.blank &&
            field.rule_name &&
            field.name.trim().toUpperCase() !== field.rule_name.toUpperCase()
              ? ("chosen" as const)
              : field.matched_by,
          start,
          length: field.length,
          max_width: field.length,
          type: "text",
        };
        start += field.length;
        return record;
      });

      await api.saveLayout({
        name: name.trim(),
        kind,
        delimiter: kind === "csv" ? delimiter : null,
        fields: payload,
        sample_name: null,
        purpose: "generation",
      });
      setNote(`${name.trim()} saved${approvalRequired ? ", pending approval" : ""}.`);
      setName("");
      setFields([]);
      onSaved();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [fields, kind, delimiter, name, onSaved, approvalRequired]);

  const width = fields.reduce((total, f) => total + f.length, 0);

  return (
    <section className="designer">
      <div className="designer__head">
        <h3>New layout</h3>
        <p className="panel__hint">A field with no rule is written blank.</p>
      </div>

      {error && <Banner tone="alarm">{error}</Banner>}
      {note && <Banner tone="affirm">{note}</Banner>}

      <div className="designer__controls">
        <div className="designer__modes" role="group" aria-label="Layout type">
          {(
            [
              ["fixed", "Fixed width"],
              ["csv", "Delimited"],
            ] as const
          ).map(([id, label]) => (
            <button
              key={id}
              className={"designer__mode" + (kind === id ? " designer__mode--on" : "")}
              onClick={() => setKind(id)}
            >
              {label}
            </button>
          ))}
        </div>

        {kind === "csv" && (
          <label className="named">
            <span>Delimiter</span>
            <select value={delimiter} onChange={(e) => setDelimiter(e.target.value)}>
              {DELIMITERS.map(([value, label]) => (
                <option key={label} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        )}

        <label className="named designer__name">
          <span>Layout name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g., Eligibility extract"
          />
        </label>
      </div>

      <div className="designer__add">
        <label className="named">
          <span>Add a field</span>
          <select
            value={picking}
            onChange={(e) => add(e.target.value)}
          >
            <option value="">
              {offer.length ? "Select an element" : "All elements added"}
            </option>
            <option value={BLANK}>Filler (blank field)</option>
            {offer.map((rule) => (
              <option key={rule.id} value={rule.id}>
                {rule.field_name}
                {rule.field_label && rule.field_label !== rule.field_name
                  ? ` — ${rule.field_label}`
                  : ""}
              </option>
            ))}
          </select>
        </label>
        {rulesError ? (
          <Chip tone="alarm">
            {`Couldn't load the rules: ${rulesError} Reopen the designer to try again.`}
          </Chip>
        ) : known.length === 0 && (
          <Chip tone="caution">
            No rules defined. Add rules under Settings › Rules.
          </Chip>
        )}
      </div>

      {fields.length === 0 ? (
        <Empty title="No fields yet">
          Add fields in the order they appear in the file.
        </Empty>
      ) : (
        <>
          <table className="panel__table designer__table">
            <thead>
              <tr>
                <th>#</th>
                <th>Field</th>
                <th>Rule</th>
                {kind === "fixed" && <th className="right">Length</th>}
                {kind === "fixed" && <th className="right">Start</th>}
                <th />
              </tr>
            </thead>
            <tbody>
              {fields.map((field, index) => (
                <tr key={index}>
                  <td className="num">{index + 1}</td>
                  <td>
                    <input
                      className="designer__field mono"
                      aria-label={`Name of field ${index + 1}`}
                      value={field.name}
                      onChange={(e) =>
                        setFields((previous) =>
                          previous.map((f, i) =>
                            i === index ? { ...f, name: e.target.value } : f,
                          ),
                        )
                      }
                    />
                  </td>
                  <td>
                    <RuleChoice
                      field={field}
                      rules={known}
                      onChange={(chosen) =>
                        setFields((previous) =>
                          previous.map((f, i) =>
                            i === index ? { ...chosen, length: f.length } : f,
                          ),
                        )
                      }
                    />
                  </td>
                  {kind === "fixed" && (
                    <td className="right">
                      <input
                        className="designer__width"
                        type="number"
                        min={1}
                        max={255}
                        value={field.length}
                        onChange={(e) =>
                          setFields((previous) =>
                            previous.map((f, i) =>
                              i === index
                                ? { ...f, length: Math.max(1, Number(e.target.value) || 1) }
                                : f,
                            ),
                          )
                        }
                      />
                    </td>
                  )}
                  {kind === "fixed" && (
                    <td className="right num">
                      {fields.slice(0, index).reduce((n, f) => n + f.length, 0)}
                    </td>
                  )}
                  <td className="right designer__row-actions">
                    <button aria-label="Move up" onClick={() => move(index, -1)} disabled={index === 0}>
                      ↑
                    </button>
                    <button
                      aria-label="Move down"
                      onClick={() => move(index, 1)}
                      disabled={index === fields.length - 1}
                    >
                      ↓
                    </button>
                    <button
                      aria-label="Delete field"
                      onClick={() =>
                        setFields((previous) => previous.filter((_, i) => i !== index))
                      }
                    >
                      ✕
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <p className="panel__hint">
            {kind === "fixed"
              ? `${fields.length} fields, record length ${width} characters.`
              : `${fields.length} fields, delimiter: ${
                  DELIMITERS.find(([v]) => v === delimiter)?.[1] ?? delimiter
                }.`}
          </p>
        </>
      )}

      <div className="panel__actions">
        <Button
          variant="primary"
          onClick={save}
          busy={busy}
          disabled={!name.trim() || fields.length === 0}
        >
          Save layout
        </Button>
        {onClose && <Button onClick={onClose}>Close</Button>}
      </div>
    </section>
  );
}
