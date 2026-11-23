/**
 * Defining a pattern by its rules, rather than waiting for the analyzer to
 * find it.
 *
 * A person often already knows the pattern they want -- members of one group
 * on a PPO plan aged 30 to 45, or professional diabetes claims over a thousand
 * dollars. Here they choose the features that say so and set a rule on each:
 * one of a list of values, or a range.
 *
 * What it defines is a scope, and it is analyzed like any other. Measured, it
 * is the defined pattern itself -- counted against the book and described from
 * the members it matches, ready to save, write to a file or load into a
 * database exactly as a found one. Looked inside, it is split into the
 * patterns the analyzer finds there.
 */

import { useEffect, useMemo, useState } from "react";
import { Banner, Button, Chip, Empty, formatCount } from "../components/primitives";
import {
  ApiError,
  api,
  type Domain,
  type FilePattern,
  type PatternCondition,
  type PatternFeature,
  type PatternFilters,
} from "../lib/api";
import { claimTypeName } from "./PatternCharts";
import { PatternsAnalyze } from "./Patterns";
import "./DefinePattern.css";
import { isChild, phrase, termsOf, words } from "../lib/pack";

interface Props {
  domain: Domain;
  onApplied: (destination: "file" | "db", pattern?: FilePattern, note?: string) => void;
  /** Rules to start from: a requirement read into them, to change. */
  initial?: { name: string; rules: Record<string, PatternCondition> } | null;
  /** A pattern was saved from here. */
  onSaved?: () => void;
}

/** The scope a set of rules defines: hierarchy as filters, the rest as conditions. */
export function rulesToFilters(
  features: PatternFeature[],
  rules: Record<string, PatternCondition>,
): PatternFilters {
  const out: PatternFilters = {};
  const conditions: Record<string, PatternCondition> = {};
  for (const feature of features) {
    const rule = rules[feature.key];
    if (!rule) continue;
    if (feature.scope) {
      const values = rule.in ?? [];
      if (!values.length) continue;
      out[feature.key] = feature.numeric ? values.map(Number) : values;
    } else if (feature.kind === "in") {
      if (rule.in?.length) conditions[feature.key] = { in: rule.in };
    } else if (rule.min !== undefined || rule.max !== undefined) {
      conditions[feature.key] = {
        ...(rule.min !== undefined ? { min: rule.min } : {}),
        ...(rule.max !== undefined ? { max: rule.max } : {}),
      };
    }
  }
  if (Object.keys(conditions).length) out.conditions = conditions;
  return out;
}

export function DefinePattern({ domain, onApplied, initial, onSaved }: Props) {
  const [features, setFeatures] = useState<PatternFeature[]>([]);
  const [chosen, setChosen] = useState<string[]>(Object.keys(initial?.rules ?? {}));
  const [rules, setRules] = useState<Record<string, PatternCondition>>(initial?.rules ?? {});
  const [name, setName] = useState(initial?.name ?? "");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .patternFeatures(domain)
      .then((payload) => live && setFeatures(payload.features))
      .catch((e) => live && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [domain]);

  const filters = useMemo(() => rulesToFilters(features, rules), [features, rules]);
  const stated = Object.keys(filters).length > 0;
  const offer = features.filter((f) => !chosen.includes(f.key));

  const put = (key: string, rule: PatternCondition | null) =>
    setRules((previous) => {
      const next = { ...previous };
      if (rule === null) delete next[key];
      else next[key] = rule;
      return next;
    });

  return (
    <div className="define">
      <header className="define__head">
        <p className="patterns__lede">
          Add a {termsOf(domain).one} field and set its criteria.
        </p>
      </header>

      {error && <Banner tone="alarm">{error}</Banner>}

      <section className="define__rules">
        <label className="named define__name">
          <span>Pattern name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Enter a pattern name"
          />
        </label>

        <label className="named">
          <span>Add field</span>
          <select
            value=""
            onChange={(e) => {
              const key = e.target.value;
              if (key) setChosen((previous) => [...previous, key]);
            }}
            disabled={!offer.length}
          >
            <option value="">{offer.length ? "Select field" : "All fields added"}</option>
            {offer.map((feature) => (
              <option key={feature.key} value={feature.key}>
                {feature.label}
              </option>
            ))}
          </select>
        </label>

        {chosen.length === 0 ? (
          <Empty title="No criteria yet">
            Add a field, such as {phrase("field_examples")}, and select its values.
          </Empty>
        ) : (
          <ul className="define__list">
            {chosen.map((key) => {
              const feature = features.find((f) => f.key === key);
              if (!feature) return null;
              return (
                <li key={key} className="define__rule">
                  <div className="define__rule-head">
                    <strong>{feature.label}</strong>
                    <Chip fill="outline">{feature.kind === "in" ? "One of" : "Range"}</Chip>
                    <button
                      className="linked"
                      onClick={() => {
                        setChosen((previous) => previous.filter((k) => k !== key));
                        put(key, null);
                      }}
                    >
                      Delete
                    </button>
                  </div>
                  {feature.kind === "in" ? (
                    <ValuesRule
                      feature={feature}
                      rule={rules[key]}
                      onChange={(rule) => put(key, rule)}
                    />
                  ) : (
                    <RangeRule
                      feature={feature}
                      rule={rules[key]}
                      onChange={(rule) => put(key, rule)}
                    />
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {stated && (
        <Variants
          domain={domain}
          features={features}
          rules={rules}
          name={name.trim() || "Custom pattern"}
        />
      )}

      {stated ? (
        <PatternsAnalyze
          key={JSON.stringify(filters)}
          domain={domain}
          onApplied={onApplied}
          defined={{ name: name.trim(), filters }}
          onSaved={onSaved}
        />
      ) : (
        chosen.length > 0 && (
          <p className="panel__hint">Set criteria for at least one field to measure the pattern.</p>
        )
      )}
    </div>
  );
}

function ValuesRule({
  feature,
  rule,
  onChange,
}: {
  feature: PatternFeature;
  rule: PatternCondition | undefined;
  onChange: (rule: PatternCondition | null) => void;
}) {
  const [find, setFind] = useState("");
  const picked = new Set(rule?.in ?? []);
  const options = feature.options ?? [];
  const shown = options.filter((o) =>
    `${o.value} ${o.label ?? ""}`.toLowerCase().includes(find.trim().toLowerCase()),
  );
  const toggle = (value: string) => {
    const next = new Set(picked);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    onChange(next.size ? { in: [...next] } : null);
  };

  return (
    <div className="define__values">
      {options.length > 8 && (
        <input
          className="define__find"
          aria-label={`Search ${feature.label.toLowerCase()}`}
          placeholder={`Search ${feature.label.toLowerCase()}`}
          value={find}
          onChange={(e) => setFind(e.target.value)}
        />
      )}
      <div className="define__options" role="group" aria-label={feature.label}>
        {shown.map((option) => (
          <label key={option.value} className="define__option">
            <input
              type="checkbox"
              aria-label={option.label ? `${option.value} ${option.label}` : option.value}
              checked={picked.has(option.value)}
              onChange={() => toggle(option.value)}
            />
            <span className="mono">{option.value}</span>
            {option.label && <span className="define__option-label">{option.label}</span>}
            <span className="define__count num">{formatCount(option.count)}</span>
          </label>
        ))}
        {shown.length === 0 && <span className="panel__hint">No matches.</span>}
      </div>
    </div>
  );
}

function RangeRule({
  feature,
  rule,
  onChange,
}: {
  feature: PatternFeature;
  rule: PatternCondition | undefined;
  onChange: (rule: PatternCondition | null) => void;
}) {
  const set = (end: "min" | "max", text: string) => {
    const next: PatternCondition = { ...(rule ?? {}) };
    if (text.trim() === "") delete next[end];
    else next[end] = Number(text);
    onChange(next.min === undefined && next.max === undefined ? null : next);
  };
  const hint =
    feature.min != null && feature.max != null
      ? `Pattern data range: ${formatCount(feature.min)} to ${formatCount(feature.max)}`
      : feature.unit
        ? `in ${feature.unit}`
        : "";

  return (
    <div className="define__range">
      <label className="named">
        <span>From</span>
        <input
          type="number"
          aria-label={`${feature.label} from`}
          value={rule?.min ?? ""}
          onChange={(e) => set("min", e.target.value)}
        />
      </label>
      <label className="named">
        <span>To</span>
        <input
          type="number"
          aria-label={`${feature.label} to`}
          value={rule?.max ?? ""}
          onChange={(e) => set("max", e.target.value)}
        />
      </label>
      {hint && <span className="panel__hint">{hint}</span>}
    </div>
  );
}

/** At most this many variants at once: past it, nobody reads the list. */
const MAX_VARIANTS = 60;

interface Variant {
  name: string;
  filters: PatternFilters;
  count: number | null;
  error?: string;
  // Measuring it failed (not "no matches"): its count is unknown.
  failed?: boolean;
  // How many patterns were saved for it: one, or one per claim type.
  saved?: number;
  parts?: { cluster_id: number; type: string | null }[];
}

/**
 * One scenario, many: the pattern defined above, varied over the values of a
 * feature or two.
 *
 * A tester with one good scenario usually needs it across every plan, or
 * every age band, or both. Each variant is the rules above with that feature
 * set to one value; each is measured against the book, so the ones nothing
 * meets are visible as such, and any or all of them are saved as patterns.
 */
function Variants({
  domain,
  features,
  rules,
  name,
}: {
  domain: Domain;
  features: PatternFeature[];
  rules: Record<string, PatternCondition>;
  name: string;
}) {
  const listable = features.filter((f) => f.kind === "in");
  const [first, setFirst] = useState("");
  const [second, setSecond] = useState("");
  const [firstValues, setFirstValues] = useState<string[]>([]);
  const [secondValues, setSecondValues] = useState<string[]>([]);
  const [variants, setVariants] = useState<Variant[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const feature = (key: string) => features.find((f) => f.key === key);
  const label = (key: string, value: string) => {
    const option = feature(key)?.options?.find((o) => o.value === value);
    return option?.label ? option.label : value;
  };

  const combinations = useMemo(() => {
    if (!first || !firstValues.length) return [];
    const out: { key: string; value: string }[][] = [];
    for (const a of firstValues) {
      if (second && secondValues.length) {
        for (const b of secondValues) {
          out.push([{ key: first, value: a }, { key: second, value: b }]);
        }
      } else {
        out.push([{ key: first, value: a }]);
      }
    }
    return out;
  }, [first, second, firstValues, secondValues]);

  const make = async () => {
    setBusy("make");
    setError(null);
    const made: Variant[] = combinations.slice(0, MAX_VARIANTS).map((combo) => {
      const varied = { ...rules };
      for (const { key, value } of combo) varied[key] = { in: [value] };
      return {
        name: `${name} · ${combo.map(({ key, value }) => `${feature(key)?.label} ${label(key, value)}`).join(" · ")}`,
        filters: rulesToFilters(features, varied),
        count: null,
      };
    });
    setVariants(made);
    // Measured one after another: each is an analysis, and a burst of sixty
    // at once would queue behind each other anyway.
    for (let index = 0; index < made.length; index += 1) {
      const variant = made[index];
      try {
        const result =
          isChild(domain)
            ? await api.analyzeClaims(variant.filters, 1, null)
            : await api.analyze(variant.filters, 1);
        variant.count =
          "subset_claims" in result ? result.subset_claims : result.subset_members;
        // Claims of several types are one pattern of each, each saved.
        variant.parts = result.patterns.map((p) => ({
          cluster_id: p.cluster_id,
          type: result.patterns.length > 1 ? claimTypeName(p) : null,
        }));
      } catch (e) {
        variant.error = e instanceof ApiError ? e.message : String(e);
        // Nothing meeting the rules is an answer (the server's 422), not a
        // failure; anything else leaves the count unknown and says so.
        if (e instanceof ApiError && e.status === 422) variant.count = 0;
        else variant.failed = true;
      }
      setVariants([...made]);
    }
    const failures = made.filter((v) => v.failed);
    if (failures.length > 0) {
      setError(
        `Couldn't measure ${failures.length} of ${made.length} variants: ${failures[0].error} ` +
          "Make them again to retry.",
      );
    }
    setBusy(null);
  };

  const save = async (index: number) => {
    const variant = variants[index];
    if (!variant || !variant.count) return;
    setBusy(`save-${index}`);
    try {
      let saved = 0;
      for (const part of variant.parts ?? [{ cluster_id: 0, type: null }]) {
        await api.keepPattern({
          domain, filters: variant.filters, k: 1, cluster_id: part.cluster_id, claim_type: null,
          name: part.type ? `${variant.name} · ${part.type}` : variant.name,
          origin: "user_defined",
        });
        saved += 1;
      }
      setVariants((previous) =>
        previous.map((v, i) => (i === index ? { ...v, saved } : v)),
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const saveAll = async () => {
    for (let index = 0; index < variants.length; index += 1) {
      if (variants[index].count && !variants[index].saved) await save(index);
    }
  };

  const picker = (
    key: string,
    values: string[],
    setValues: (values: string[]) => void,
  ) => {
    const options = feature(key)?.options ?? [];
    return (
      <div className="define__options define__options--short" role="group" aria-label={`Vary ${feature(key)?.label}`}>
        {options.map((option) => (
          <label key={option.value} className="define__option">
            <input
              type="checkbox"
              aria-label={option.label ? `${option.value} ${option.label}` : option.value}
              checked={values.includes(option.value)}
              onChange={() =>
                setValues(
                  values.includes(option.value)
                    ? values.filter((v) => v !== option.value)
                    : [...values, option.value],
                )
              }
            />
            <span className="mono">{option.value}</span>
            {option.label && <span className="define__option-label">{option.label}</span>}
          </label>
        ))}
      </div>
    );
  };

  const ready = variants.filter((v) => v.count && !v.saved).length;

  return (
    <section className="define__variants" aria-label="Variants">
      <h3>Variants</h3>
      <p className="panel__hint">
        One pattern for each value of one or two fields, each measured against the book of business.
      </p>
      {error && <Banner tone="alarm">{error}</Banner>}
      <div className="define__vary">
        <div>
          <label className="named">
            <span>Vary by</span>
            <select
              value={first}
              onChange={(e) => {
                setFirst(e.target.value);
                setFirstValues([]);
                setVariants([]);
              }}
            >
              <option value="">Select field</option>
              {listable.map((f) => (
                <option key={f.key} value={f.key}>
                  {f.label}
                </option>
              ))}
            </select>
          </label>
          {first && picker(first, firstValues, setFirstValues)}
        </div>
        {first && (
          <div>
            <label className="named">
              <span>Then by</span>
              <select
                value={second}
                onChange={(e) => {
                  setSecond(e.target.value);
                  setSecondValues([]);
                  setVariants([]);
                }}
              >
                <option value="">None</option>
                {listable
                  .filter((f) => f.key !== first)
                  .map((f) => (
                    <option key={f.key} value={f.key}>
                      {f.label}
                    </option>
                  ))}
              </select>
            </label>
            {second && picker(second, secondValues, setSecondValues)}
          </div>
        )}
      </div>
      <div className="panel__actions">
        <Button
          onClick={make}
          busy={busy === "make"}
          disabled={!combinations.length}
        >
          {combinations.length
            ? `Create ${Math.min(combinations.length, MAX_VARIANTS)} variant${combinations.length === 1 ? "" : "s"}`
            : "Create variants"}
        </Button>
        {combinations.length > MAX_VARIANTS && (
          <span className="panel__hint">Only the first {MAX_VARIANTS} variants are created.</span>
        )}
        {ready > 0 && busy === null && (
          <Button onClick={saveAll}>
            {`Save all ${ready}`}
          </Button>
        )}
      </div>
      {variants.length > 0 && (
        <table className="panel__table define__variant-table">
          <thead>
            <tr>
              <th>Variant</th>
              <th className="right">{termsOf(domain).Many}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {variants.map((variant, index) => (
              <tr key={variant.name}>
                <td>{variant.name}</td>
                <td className="right num">
                  {variant.failed ? "—" : variant.count === null ? "…" : formatCount(variant.count)}
                </td>
                <td className="right">
                  {variant.saved ? (
                    <Chip tone="affirm">
                      {variant.saved > 1 ? `Saved (${variant.saved} ${words("child").one} types)` : "Saved"}
                    </Chip>
                  ) : variant.failed ? (
                    <Chip tone="alarm" fill="outline" title={variant.error}>
                      Couldn't measure
                    </Chip>
                  ) : variant.count === 0 ? (
                    <Chip tone="caution" fill="outline" title={variant.error}>
                      No matches
                    </Chip>
                  ) : variant.count ? (
                    <Button onClick={() => save(index)} busy={busy === `save-${index}`}>
                      Save
                    </Button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
