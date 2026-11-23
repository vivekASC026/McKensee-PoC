/**
 * The generation-rule repository.
 *
 * One rule per element -- DOB, SEX, CLAIM_STATUS -- shared by everything
 * downstream. An EDI position and a layout field both name an element, and its
 * rule fills it. No database column appears here: a column is a fact about one
 * database, and a rule has to work against any of them. The columns that mean
 * an element are kept as its aliases, so a sample file naming one still
 * resolves. An empty repository is not a neutral starting state: every field
 * falls to `default`, and `default` writes blank. That is why this screen leads with
 * filling the repository rather than with a form for adding one rule at a
 * time — the useful first act is to describe the whole run at once.
 *
 * What it is filled from is the plan. The plan already knows every column a
 * run writes and which generator was chosen for each, so the seeded rules
 * describe the run that would actually happen. Seeding again later adds only
 * what is missing, and never touches a rule somebody has since edited.
 *
 * Retiring is not deleting. A layout may still point at a rule, and a dangling
 * reference is worse than an inactive one.
 */

import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { Banner, Button, Chip, Empty, formatCount } from "../components/primitives";
import {
  ApiError,
  api,
  type GenerationRule,
  type NewRule,
  type RulesPayload,
  type SeedPayload,
} from "../lib/api";
import "./Rules.css";

/** What each rule method is called on screen. The stored value stays as is. */
export const METHOD_NAMES: Record<string, string> = {
  pattern: "Format mask",
  lookup: "List",
  sequential: "Sequence",
  random: "Random number",
  constant: "Constant",
  formatted: "Template",
  date: "Date",
  range: "Range",
  default: "Blank",
};

export function methodName(method: string | null | undefined): string {
  const key = method ?? "default";
  return METHOD_NAMES[key] ?? key;
}

export function Rules() {
  const [payload, setPayload] = useState<RulesPayload | null>(null);
  const [seed, setSeed] = useState<SeedPayload | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [editing, setEditing] = useState<number | null>(null);
  const [showRetired, setShowRetired] = useState(false);
  const [filter, setFilter] = useState("");

  const load = useCallback(async () => {
    try {
      // The rules are shown whatever the plan's state.
      const [rules, preview] = await Promise.all([
        api.rules(),
        api.seedPreview().catch(() => null),
      ]);
      setPayload(rules);
      setSeed(preview);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const fill = useCallback(async () => {
    setBusy("seed");
    setError(null);
    setNote(null);
    try {
      const out = await api.seedRules();
      setNote(
        out.added.length
          ? `Added ${out.added.length} rules from the plan.` +
            (out.failed.length ? ` Couldn't save ${out.failed.length}.` : "")
          : "No rules added. Every element in the plan already has a rule.",
      );
      if (out.failed.length) setError(out.failed.join("\n"));
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [load]);

  const retire = useCallback(
    async (rule: GenerationRule) => {
      setBusy(`active-${rule.id}`);
      setError(null);
      try {
        await api.setRuleActive(rule.id, !(rule.active ?? true));
        await load();
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  const methods = payload?.methods ?? {};
  const rules = payload?.rules ?? [];

  const shown = useMemo(() => {
    const needle = filter.trim().toUpperCase();
    return rules.filter((rule) => {
      if (!showRetired && rule.active === false) return false;
      if (!needle) return true;
      return (
        rule.field_name.toUpperCase().includes(needle) ||
        (rule.field_label ?? "").toUpperCase().includes(needle) ||
        (rule.field_category ?? "").toUpperCase().includes(needle)
      );
    });
  }, [rules, filter, showRetired]);

  const retired = rules.filter((r) => r.active === false).length;

  return (
    <section className="panel rules">
      {error && <Banner tone="alarm">{error}</Banner>}
      {note && <Banner tone="affirm">{note}</Banner>}

      {/* The count that explains the blanks on the Layouts tab. */}
      {seed && seed.has_plan === false && (
        <div className="rules__seed">
          <div>
            <h3>Rules from the plan</h3>
            <p className="panel__hint">
              No plan has been built for this database. Build the plan to add
              rules for its elements.
            </p>
          </div>
        </div>
      )}
      {seed && seed.has_plan !== false && (
        <div className="rules__seed">
          <div>
            <h3>Rules from the plan</h3>
            <p className="panel__hint">
              The plan includes{" "}
              <span className="num">{formatCount(seed.n_columns)}</span> elements.{" "}
              {seed.n_missing === 0 ? (
                <>All of them have a rule.</>
              ) : (
                <>
                  <span className="num">{formatCount(seed.n_missing)}</span> have no
                  rule. Layout fields mapped to these elements are written blank.
                </>
              )}
            </p>
          </div>
          <Button
            variant={seed.n_missing ? "primary" : undefined}
            onClick={fill}
            busy={busy === "seed"}
            disabled={seed.n_missing === 0}
          >
            {seed.n_missing
              ? `Add ${formatCount(seed.n_missing)} missing rules`
              : "No missing rules"}
          </Button>
        </div>
      )}

      {rules.length === 0 ? (
        <Empty title="No rules yet">
          Until a rule exists, every layout field is written blank.
        </Empty>
      ) : (
        <>
          <div className="rules__controls">
            <label className="named">
              <span>Find</span>
              <input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Element, description, or category"
              />
            </label>
            <label className="rules__toggle">
              <input
                type="checkbox"
                checked={showRetired}
                onChange={(e) => setShowRetired(e.target.checked)}
              />
              <span>
                Show retired{retired ? ` (${retired})` : ""}
              </span>
            </label>
            <span className="rules__count num">
              {/* Says what it counts: alone at the row's end it read as the
                  retired checkbox's number. */}
              Showing {formatCount(shown.length)} of {formatCount(rules.length)} rules
            </span>
          </div>

          <table className="panel__table rules__table">
            <thead>
              <tr>
                <th>Element</th>
                <th>Description</th>
                <th>Rule</th>
                <th>Used by</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {shown.map((rule) => (
                <RuleRow
                  key={rule.id}
                  rule={rule}
                  methods={methods}
                  lookups={payload?.lookups ?? []}
                  open={editing === rule.id}
                  busy={busy}
                  onToggle={() => setEditing(editing === rule.id ? null : rule.id)}
                  onRetire={() => retire(rule)}
                  onSaved={async (message) => {
                    setNote(message);
                    setEditing(null);
                    await load();
                  }}
                  onError={setError}
                  setBusy={setBusy}
                />
              ))}
            </tbody>
          </table>
        </>
      )}

      <NewRuleForm
        methods={methods}
        lookups={payload?.lookups ?? []}
        onAdded={async (message) => {
          setNote(message);
          await load();
        }}
        onError={setError}
      />
    </section>
  );
}

/* ------------------------------------------------------------------ */

interface RowProps {
  rule: GenerationRule;
  methods: Record<string, string>;
  lookups: string[];
  open: boolean;
  busy: string | null;
  onToggle: () => void;
  onRetire: () => void;
  onSaved: (message: string) => Promise<void>;
  onError: (message: string) => void;
  setBusy: (key: string | null) => void;
}

function RuleRow({
  rule, methods, lookups, open, busy, onToggle, onRetire, onSaved, onError,
}: RowProps) {
  const inactive = rule.active === false;
  return (
    <>
      <tr className={inactive ? "rules__row rules__row--retired" : "rules__row"}>
        <td className="mono">{rule.field_name}</td>
        <td>
          {rule.field_label ?? rule.field_name}
          {rule.field_category && (
            <span className="rules__category">{rule.field_category}</span>
          )}
        </td>
        <td>
          <Chip tone={rule.gen_method === "default" ? "caution" : "catalog"}>
            {methodName(rule.gen_method)}
          </Chip>
          {inactive && (
            <Chip tone="caution" fill="outline">
              Retired
            </Chip>
          )}
        </td>
        <td className="rules__used">
          {rule.used_by?.length ? rule.used_by.join(", ") : <span className="dim">—</span>}
        </td>
        <td className="right">
          <Button onClick={onToggle}>{open ? "Close" : "Edit"}</Button>
        </td>
      </tr>

      {open && (
        <tr className="rules__editor">
          <td colSpan={5}>
            <RuleForm rule={rule} methods={methods} lookups={lookups} onSaved={onSaved}
                      onError={onError}>
              <Button onClick={onRetire} busy={busy === `active-${rule.id}`}>
                {inactive ? "Restore" : "Retire"}
              </Button>
            </RuleForm>
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * One rule's settings, its sample values and its other names: the editor the
 * Rules tab opens, and the one a layout field opens for a Custom rule.
 */
export function RuleForm({
  rule, methods, lookups, onSaved, onError, children,
}: {
  rule: GenerationRule;
  methods: Record<string, string>;
  lookups: string[];
  onSaved: (message: string) => Promise<void>;
  onError: (message: string) => void;
  /** Further actions beside Save. */
  children?: ReactNode;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [label, setLabel] = useState(rule.field_label ?? "");
  const [method, setMethod] = useState(rule.gen_method ?? "default");
  const [lookupSource, setLookupSource] = useState(rule.lookup_source ?? "");
  const [domain, setDomain] = useState(rule.domain ?? "");
  const [weights, setWeights] = useState(rule.weights ?? "");
  // Lowest and highest: a range's bounds, or a date's days either side of its
  // day. A date rule that only says how far back it reaches reads as that.
  const reachesBack = rule.gen_method === "date" && rule.min_value == null && rule.start_value != null;
  const [low, setLow] = useState(
    reachesBack ? String(-Math.max(Number(rule.start_value) - 1, 0))
      : rule.min_value == null ? "" : String(rule.min_value),
  );
  const [high, setHigh] = useState(
    reachesBack ? "0" : rule.max_value == null ? "" : String(rule.max_value),
  );
  const [prefix, setPrefix] = useState(rule.prefix ?? "");
  const [width, setWidth] = useState(rule.length == null || rule.length === 0 ? "" : String(rule.length));
  const [sample, setSample] = useState<{ values?: string[]; error?: string } | null>(null);
  const [places, setPlaces] = useState(rule.decimals == null ? "" : String(rule.decimals));
  // Only a rule that carries a measured shape can keep one; the shape itself
  // is the plan's measurement and is not typed in.
  const measured = rule.mean_value != null && rule.stddev_value != null;
  const [spread, setSpread] = useState(rule.distribution ?? "uniform");
  const [format, setFormat] = useState(rule.format_pattern ?? "");
  const [start, setStart] = useState(rule.start_value == null ? "" : String(rule.start_value));
  const [alias, setAlias] = useState("");

  // Only what the method reads. Clearing the rest on every save was how a
  // lookup's list was lost by editing its label.
  const draft = useCallback((): Record<string, unknown> => {
    // Empty clears the setting; anything else must read as a number, or the
    // save stops and says which box, rather than clearing it.
    const number = (text: string, what: string) => {
      if (text.trim() === "") return null;
      const value = Number(text);
      if (!Number.isFinite(value)) {
        throw new Error(`${what} must be a number. Correct it, or clear the box to remove it.`);
      }
      return value;
    };
    const changes: Record<string, unknown> = {
      field_label: label.trim() || rule.field_name,
      gen_method: method,
    };
    if (method === "lookup") {
      changes.domain = domain.trim() || null;
      changes.weights = weights.trim() || null;
      changes.lookup_source = lookupSource || null;
    } else if (method === "constant") {
      changes.domain = domain.trim() || null;
    } else if (method === "range") {
      changes.min_value = number(low, "Minimum");
      changes.max_value = number(high, "Maximum");
      changes.decimals = number(places, "Decimal places");
      if (measured) changes.distribution = spread;
    } else if (method === "pattern") {
      changes.format_pattern = format.trim() || null;
    } else if (method === "date") {
      changes.format_pattern = format.trim() || null;
      changes.min_value = number(low, "Offset from (days)");
      changes.max_value = number(high, "Offset to (days)");
    } else if (method === "sequential" || method === "random") {
      if (method === "sequential") changes.start_value = number(start, "Start value");
      changes.prefix = prefix.trim() || null;
      changes.length = number(width, "Length");
    } else if (method === "formatted") {
      changes.format_pattern = format.trim() || null;
      changes.domain = domain.trim() || null;
    }
    return changes;
  }, [rule, label, method, lookupSource, domain, weights, low, high, places, spread, measured,
      format, start, prefix, width]);

  const trySample = useCallback(async () => {
    setBusy(`sample-${rule.id}`);
    try {
      const found = await api.sampleRule({
        field_name: rule.field_name, element: rule.element ?? rule.field_name,
        data_type: rule.data_type, postfix: rule.postfix, decimals: rule.decimals,
        mean_value: rule.mean_value, stddev_value: rule.stddev_value,
        start_value: rule.start_value, prefix: rule.prefix, length: rule.length,
        ...draft(),
      } as Partial<NewRule> & { field_name: string });
      setSample({ values: found.values });
    } catch (e) {
      setSample({ error: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setBusy(null);
    }
  }, [rule, draft, setBusy]);

  const save = useCallback(async () => {
    setBusy(`save-${rule.id}`);
    try {
      await api.updateRule(rule.id, draft() as Partial<NewRule>);
      await onSaved(`${rule.field_name} updated.`);
    } catch (e) {
      onError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [rule, draft, onSaved, onError, setBusy]);

  const attachAlias = useCallback(async () => {
    const name = alias.trim();
    if (!name) return;
    setBusy(`alias-${rule.id}`);
    try {
      await api.addAlias(rule.id, name);
      setAlias("");
      await onSaved(`Alias ${name} added to ${rule.field_name}.`);
    } catch (e) {
      onError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [rule, alias, onSaved, onError, setBusy]);

  return (
    <div className="rules__form">
            <p className="panel__hint">
              {methods[method] ?? ""}
            </p>

            <div className="rules__fields">
              <label className="named">
                <span>Description</span>
                <input value={label} onChange={(e) => setLabel(e.target.value)} />
              </label>

              <label className="named">
                <span>Rule</span>
                <select value={method} onChange={(e) => setMethod(e.target.value)}>
                  {Object.keys(methods).map((name) => (
                    <option key={name} value={name}>
                      {methodName(name)}
                    </option>
                  ))}
                </select>
              </label>

              {method === "lookup" && (
                <>
                  <label className="named rules__wide">
                    <span>Values</span>
                    <input
                      value={domain}
                      onChange={(e) => setDomain(e.target.value)}
                      placeholder="F,M"
                    />
                  </label>
                  <label className="named rules__wide">
                    <span>Weights</span>
                    <input
                      value={weights}
                      onChange={(e) => setWeights(e.target.value)}
                      placeholder="0.54,0.46 (blank for equal weights)"
                    />
                  </label>
                  {lookups.length > 0 && (
                    <label className="named">
                      <span>Lookup list</span>
                      <select
                        value={lookupSource}
                        onChange={(e) => setLookupSource(e.target.value)}
                      >
                        <option value="">None</option>
                        {lookups.map((name) => (
                          <option key={name} value={name}>
                            {name}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                </>
              )}

              {method === "constant" && (
                <label className="named">
                  <span>Value</span>
                  <input
                    value={domain}
                    onChange={(e) => setDomain(e.target.value)}
                    placeholder="Same value on every row"
                  />
                </label>
              )}

              {method === "range" && (
                <>
                  <label className="named">
                    <span>Minimum</span>
                    <input value={low} onChange={(e) => setLow(e.target.value)} inputMode="decimal" />
                  </label>
                  <label className="named">
                    <span>Maximum</span>
                    <input value={high} onChange={(e) => setHigh(e.target.value)} inputMode="decimal" />
                  </label>
                  <label className="named">
                    <span>Decimal places</span>
                    <input value={places} onChange={(e) => setPlaces(e.target.value)} inputMode="numeric" />
                  </label>
                  {measured && (
                    <label className="named">
                      <span>Distribution</span>
                      <select value={spread} onChange={(e) => setSpread(e.target.value)}>
                        <option value="lognormal">Log-normal</option>
                        <option value="normal">Normal</option>
                        <option value="uniform">Uniform</option>
                      </select>
                    </label>
                  )}
                </>
              )}

              {method === "pattern" && (
                <label className="named rules__wide">
                  <span>Format mask</span>
                  <input
                    className="mono"
                    value={format}
                    onChange={(e) => setFormat(e.target.value)}
                    placeholder="[A-Z]{3}\d{8}"
                  />
                </label>
              )}

              {method === "date" && (
                <>
                  <label className="named">
                    <span>Base date</span>
                    <input value={format} onChange={(e) => setFormat(e.target.value)} placeholder="2026-06-30" />
                  </label>
                  <label className="named">
                    <span>Offset from (days)</span>
                    <input value={low} onChange={(e) => setLow(e.target.value)} inputMode="numeric" placeholder="-3650" />
                  </label>
                  <label className="named">
                    <span>Offset to (days)</span>
                    <input value={high} onChange={(e) => setHigh(e.target.value)} inputMode="numeric" placeholder="0" />
                  </label>
                </>
              )}

              {(method === "sequential" || method === "random") && (
                <>
                  {method === "sequential" && (
                    <label className="named">
                      <span>Start value</span>
                      <input value={start} onChange={(e) => setStart(e.target.value)} inputMode="numeric" />
                    </label>
                  )}
                  <label className="named">
                    <span>Prefix</span>
                    <input value={prefix} onChange={(e) => setPrefix(e.target.value)} placeholder="EMP" />
                  </label>
                  <label className="named">
                    <span>Length</span>
                    <input value={width} onChange={(e) => setWidth(e.target.value)} inputMode="numeric" placeholder="10" />
                  </label>
                </>
              )}

              {method === "formatted" && (
                <>
                  <label className="named rules__wide">
                    <span>Template</span>
                    <input
                      className="mono"
                      value={format}
                      onChange={(e) => setFormat(e.target.value)}
                      placeholder="{FIRST_NAME}.{LAST_NAME}@example.com"
                    />
                  </label>
                  <label className="named">
                    <span>Email domain</span>
                    <input value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="example.com" />
                  </label>
                </>
              )}
            </div>

            {/* One element, many names: a customer layout calls it FIRST_NM. */}
            <div className="rules__aliases">
              <span className="rules__aliases-label">Aliases</span>
              {rule.aliases?.length ? (
                rule.aliases.map((name) => (
                  <Chip key={name} tone="user" fill="outline">
                    {name}
                  </Chip>
                ))
              ) : (
                <span className="dim">None</span>
              )}
              <input
                value={alias}
                onChange={(e) => setAlias(e.target.value)}
                placeholder="Column name with the same meaning"
              />
              <Button onClick={attachAlias} busy={busy === `alias-${rule.id}`}>
                Add alias
              </Button>
            </div>

            <div className="rules__sample">
              <Button onClick={trySample} busy={busy === `sample-${rule.id}`}>
                Preview
              </Button>
              {sample?.values && (
                <span className="mono">
                  {sample.values.map((v) => (v === "" ? "(blank)" : v)).join("  ·  ")}
                </span>
              )}
              {sample?.error && <Banner tone="alarm">{sample.error}</Banner>}
            </div>

            <div className="panel__actions">
              <Button variant="primary" onClick={save} busy={busy === `save-${rule.id}`}>
                Save
              </Button>
              {children}
            </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function NewRuleForm({
  methods, lookups, onAdded, onError,
}: {
  methods: Record<string, string>;
  lookups: string[];
  onAdded: (message: string) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [label, setLabel] = useState("");
  const [method, setMethod] = useState("pattern");
  const [lookupSource, setLookupSource] = useState("");
  const [busy, setBusy] = useState(false);

  const add = useCallback(async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      await api.createRule({
        field_name: name.trim().toUpperCase(),
        field_label: label.trim() || null,
        gen_method: method,
        lookup_source: method === "lookup" ? lookupSource || null : null,
      });
      await onAdded(`${name.trim().toUpperCase()} added.`);
      setName("");
      setLabel("");
      setOpen(false);
    } catch (e) {
      onError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [name, label, method, lookupSource, onAdded, onError]);

  if (!open) {
    return (
      <div className="panel__actions">
        <Button onClick={() => setOpen(true)}>Add rule</Button>
      </div>
    );
  }

  return (
    <div className="rules__new">
      <div className="rules__fields">
        <label className="named">
          <span>Element</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="GROUP_NUMBER"
          />
        </label>
        <label className="named">
          <span>Description</span>
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="Business name"
          />
        </label>
        <label className="named">
          <span>Rule</span>
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            {Object.keys(methods).map((m) => (
              <option key={m} value={m}>
                {methodName(m)}
              </option>
            ))}
          </select>
        </label>
        {method === "lookup" && (
          <label className="named">
            <span>Lookup list</span>
            <select value={lookupSource} onChange={(e) => setLookupSource(e.target.value)}>
              <option value="">Select a list</option>
              {lookups.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <div className="panel__actions">
        <Button variant="primary" onClick={add} busy={busy} disabled={!name.trim()}>
          Add rule
        </Button>
        <Button onClick={() => setOpen(false)}>Cancel</Button>
      </div>
    </div>
  );
}
