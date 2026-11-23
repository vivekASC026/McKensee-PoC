/**
 * Patterns, drawn.
 *
 * Two views with one job each. The overview lines every pattern up against the
 * others -- how the scope divides, where each one sits on age or charge, and
 * what sets it apart -- and selecting a pattern there highlights it in every
 * chart at once. The detail view is one pattern's own distributions against
 * the scope it was found in.
 *
 * Every comparison is against "everyone in scope". The patterns split the scope
 * completely, so its mix is their size-weighted mix and needs no second query;
 * its continuous distributions come from the analysis, on the same bins as each
 * pattern's, so the two can be laid over each other directly.
 *
 * Color is identity and follows the pattern, by its place in the list. Text
 * stays in ink; every colored mark is also named in words.
 */

import { useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  formatCount,
  formatPercent,
} from "../components/primitives";
import type {
  CategoricalDist,
  ClaimsPattern,
  DiscoveredPattern,
  ScopeDistribution,
} from "../lib/api";
import "./PatternCharts.css";
import { chartMixes, chartRanges, facts, keyOf, linesOf, typeName } from "../lib/pack";

// ---------------------------------------------------------------------------
// Shared reading of a pattern
// ---------------------------------------------------------------------------

/** Eight validated slots; past that a pattern is gray and named, never a
 *  generated ninth hue. */
export function seriesColor(index: number): string {
  return index < 8 ? `var(--series-${index + 1})` : "var(--physical)";
}


/** The two analyzers spell a categorical mix differently; both are shares. */
export function sharesOf(dist: CategoricalDist | Record<string, number> | undefined) {
  if (!dist) return {} as Record<string, number>;
  const raw: Record<string, number> =
    (dist as CategoricalDist).pct ??
    (Object.fromEntries(
      Object.entries(dist).filter(([, v]) => typeof v === "number"),
    ) as Record<string, number>);
  const total = Object.values(raw).reduce((s, v) => s + v, 0) || 1;
  return Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, v / total]));
}


/** A child pattern's record type, named, when it holds only one. */
export function claimTypeName(pattern: DiscoveredPattern): string | null {
  const type = facts().type;
  const types = Object.keys(sharesOf(type ? pattern.categorical?.[type] : undefined));
  if (types.length !== 1) return null;
  const code = types[0].trim();
  return typeName(code);
}

/** Everyone in scope: the patterns' mixes weighted by their size. */
function everyoneMix(patterns: DiscoveredPattern[], feature: string) {
  const total = patterns.reduce((s, p) => s + p.size, 0) || 1;
  const out: Record<string, number> = {};
  for (const p of patterns) {
    const shares = sharesOf(p.categorical?.[feature]);
    for (const [value, share] of Object.entries(shares)) {
      out[value] = (out[value] ?? 0) + (share * p.size) / total;
    }
  }
  return out;
}

function fmtValue(value: number, money?: boolean): string {
  // `+ 0` turns a rounded -0 into 0, which otherwise prints as "-0".
  const rounded = (Math.abs(value) >= 100 ? Math.round(value) : Math.round(value * 10) / 10) + 0;
  return money ? `$${rounded.toLocaleString()}` : rounded.toLocaleString();
}

/** A handful of round ticks across [low, high]. */
function ticks(low: number, high: number, wanted = 5): number[] {
  const span = high - low || 1;
  const raw = span / wanted;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((f) => f * power).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(low / step) * step; v <= high + 1e-9; v += step) {
    out.push(Math.round(v * 1e6) / 1e6);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Tooltip: one, positioned at the pointer; every value it shows is also
// written somewhere on the chart or in the cards.
// ---------------------------------------------------------------------------

interface Tip {
  x: number;
  y: number;
  body: ReactNode;
}

function useTip() {
  const [tip, setTip] = useState<Tip | null>(null);
  const bind = (body: ReactNode) => ({
    onPointerMove: (e: React.PointerEvent) =>
      setTip({ x: e.clientX, y: e.clientY, body }),
    onPointerLeave: () => setTip(null),
    onFocus: (e: React.FocusEvent) => {
      const r = (e.target as HTMLElement).getBoundingClientRect();
      setTip({ x: r.left + r.width / 2, y: r.bottom, body });
    },
    onBlur: () => setTip(null),
  });
  // Into the body, not beside the chart: a card that clips its overflow, or
  // any ancestor that makes its own containing block, would hide it.
  const layer = tip
    ? createPortal(
        <div className="pc-tip" style={{ left: tip.x + 14, top: tip.y + 14 }} role="status">
          {tip.body}
        </div>,
        document.body,
      )
    : null;
  return { bind, layer };
}

function TipRow({ color, value, label }: { color?: string; value: string; label: string }) {
  return (
    <div className="pc-tip__row">
      {color && <span className="pc-tip__key" style={{ background: color }} />}
      <strong className="num">{value}</strong>
      <span>{label}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The overview: every pattern at once
// ---------------------------------------------------------------------------

interface OverviewProps {
  patterns: DiscoveredPattern[];
  distributions?: Record<string, ScopeDistribution>;
  unit: string;
  selected: number | null;
  onSelect: (clusterId: number | null) => void;
}

export function PatternOverview({ patterns, distributions, unit, selected, onSelect }: OverviewProps) {
  const { bind, layer } = useTip();
  const total = patterns.reduce((s, p) => s + p.size, 0) || 1;
  const dim = (cid: number) => selected !== null && selected !== cid;
  const toggle = (cid: number) => onSelect(selected === cid ? null : cid);

  const continuous = useMemo(
    () =>
      Object.keys(chartRanges()).filter((f) => patterns.some((p) => p.continuous?.[f])),
    [patterns],
  );

  return (
    <section className="pc" aria-label="Pattern comparison">
      {/* A. How the scope divides */}
      <div className="pc__block">
        <h3 className="pc__title">
          Distribution of {formatCount(total)} {unit} across patterns
        </h3>
        <div className="pc-share" role="list">
          {patterns.map((p, i) => {
            const share = p.size / total;
            return (
              <button
                key={p.cluster_id}
                role="listitem"
                className={"pc-share__seg" + (dim(p.cluster_id) ? " is-dim" : "")}
                style={{ flexGrow: share, background: seriesColor(i) }}
                aria-label={`${p.name}: ${formatPercent(share)}`}
                aria-pressed={selected === p.cluster_id}
                onClick={() => toggle(p.cluster_id)}
                {...bind(
                  <>
                    <TipRow color={seriesColor(i)} value={formatPercent(share)} label={p.name} />
                    <div className="pc-tip__note">{formatCount(p.size)} {unit}</div>
                  </>,
                )}
              />
            );
          })}
        </div>
        <ul className="pc-legend">
          {patterns.map((p, i) => (
            <li key={p.cluster_id}>
              <button
                className={"pc-legend__item" + (dim(p.cluster_id) ? " is-dim" : "")}
                aria-pressed={selected === p.cluster_id}
                onClick={() => toggle(p.cluster_id)}
              >
                <span className="pc-legend__key" style={{ background: seriesColor(i) }} />
                <span className="pc-legend__name">{p.name}</span>
                <span className="pc-legend__value num">{formatPercent(p.size / total)}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      {/* B. Where each sits */}
      {continuous.length > 0 && (
        <div className="pc__block">
          <h3 className="pc__title">Range by pattern</h3>
          <p className="pc__note">
            Thin line: minimum to maximum. Thick band: mean ± one standard deviation. Dot:
            mean.
          </p>
          <div className="pc-ranges">
            {continuous.map((feature) => (
              <RangeStrips
                key={feature}
                feature={feature}
                patterns={patterns}
                scope={distributions?.[feature]}
                dim={dim}
                onPick={toggle}
                bind={bind}
              />
            ))}
          </div>
        </div>
      )}

      {/* C. What sets each apart */}
      <Differences patterns={patterns} dim={dim} onPick={toggle} bind={bind} />

      {layer}
    </section>
  );
}

type Bind = ReturnType<typeof useTip>["bind"];

function RangeStrips({
  feature, patterns, scope, dim, onPick, bind,
}: {
  feature: string;
  patterns: DiscoveredPattern[];
  scope?: ScopeDistribution;
  dim: (cid: number) => boolean;
  onPick: (cid: number) => void;
  bind: Bind;
}) {
  const meta = chartRanges()[feature];
  const rows = patterns
    .map((p, i) => ({ p, i, s: p.continuous?.[feature] }))
    .filter((r) => r.s);
  const lows = rows.map((r) => r.s!.min);
  const highs = rows.map((r) => r.s!.max);
  // The axis stops at the scope's 99th percentile when one is known, so one
  // extreme charge does not flatten every band into the left edge.
  const low = Math.min(...lows, scope ? scope.edges[0] : Infinity);
  const high = scope ? scope.edges[scope.edges.length - 1] : Math.max(...highs);
  const at = (v: number) => `${((Math.min(Math.max(v, low), high) - low) / (high - low || 1)) * 100}%`;
  const total = rows.reduce((s, r) => s + r.p.size, 0) || 1;
  const everyoneMean = rows.reduce((s, r) => s + r.s!.mean * r.p.size, 0) / total;
  const axis = ticks(low, high);

  return (
    <div className="pc-range">
      <div className="pc-range__label">
        {meta.label} <span className="pc-range__unit">{meta.money ? "" : meta.unit}</span>
      </div>
      {rows.map(({ p, i, s }) => {
        const stats = s!;
        const bandLow = Math.max(stats.min, stats.mean - stats.std);
        const bandHigh = Math.min(stats.max, stats.mean + stats.std);
        return (
          <button
            key={p.cluster_id}
            className={"pc-range__row" + (dim(p.cluster_id) ? " is-dim" : "")}
            onClick={() => onPick(p.cluster_id)}
            {...bind(
              <>
                <TipRow color={seriesColor(i)} value={fmtValue(stats.mean, meta.money)} label={`mean · ${p.name}`} />
                <div className="pc-tip__note">
                  Range {fmtValue(stats.min, meta.money)} to {fmtValue(stats.max, meta.money)}; ±1 SD{" "}
                  {fmtValue(bandLow, meta.money)}–{fmtValue(bandHigh, meta.money)}
                </div>
              </>,
            )}
          >
            <span className="pc-range__name">{p.name}</span>
            <span className="pc-range__track">
              <span
                className="pc-range__span"
                style={{ left: at(stats.min), right: `calc(100% - ${at(stats.max)})`, background: seriesColor(i) }}
              />
              {stats.max > high && <span className="pc-range__more" style={{ color: seriesColor(i) }}>›</span>}
              <span
                className="pc-range__band"
                style={{ left: at(bandLow), right: `calc(100% - ${at(bandHigh)})`, background: seriesColor(i) }}
              />
              <span className="pc-range__mean" style={{ left: at(stats.mean), borderColor: seriesColor(i) }} />
            </span>
          </button>
        );
      })}
      <div className="pc-range__row pc-range__row--all">
        <span className="pc-range__name">All in scope</span>
        <span className="pc-range__track">
          <span
            className="pc-range__span pc-range__span--all"
            style={{ left: at(Math.min(...lows)), right: `calc(100% - ${at(Math.max(...highs))})` }}
          />
          <span className="pc-range__mean pc-range__mean--all" style={{ left: at(everyoneMean) }} />
        </span>
      </div>
      <div className="pc-range__axis">
        <span className="pc-range__name" />
        <span className="pc-range__ticks">
          {axis.map((v) => (
            <span key={v} className="pc-range__tick num" style={{ left: at(v) }}>
              {fmtValue(v, meta.money)}
            </span>
          ))}
        </span>
      </div>
    </div>
  );
}

/** Pattern share minus everyone's, as a cell color: blue where the pattern
 *  holds more of a value than the scope does, red where it holds less, gray
 *  where it is the same. Full strength at 25 points. */
function divergeColor(diff: number): string {
  const strength = Math.min(1, Math.abs(diff) / 0.25) * 70;
  const pole = diff >= 0 ? "var(--diverge-up)" : "var(--diverge-down)";
  return `color-mix(in oklab, ${pole} ${strength.toFixed(0)}%, var(--diverge-mid))`;
}

function Differences({
  patterns, dim, onPick, bind,
}: {
  patterns: DiscoveredPattern[];
  dim: (cid: number) => boolean;
  onPick: (cid: number) => void;
  bind: Bind;
}) {
  const columns = useMemo(() => {
    const features = Object.keys(chartMixes()).filter((f) =>
      patterns.some((p) => p.categorical?.[f]),
    );
    return features.map((feature) => {
      const everyone = everyoneMix(patterns, feature);
      // The values that matter most to the scope, at most six per feature.
      const values = Object.entries(everyone)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 6)
        .map(([v]) => v);
      return { feature, everyone, values };
    });
  }, [patterns]);

  if (columns.length === 0) return null;

  return (
    <div className="pc__block">
      <h3 className="pc__title">Distinguishing values by pattern</h3>
      <p className="pc__note">
        Each cell shows the pattern's share (%). Blue: above the overall scope; red: below;
        gray: similar.
      </p>
      <div className="pc-diff" role="table">
        <div className="pc-diff__row pc-diff__row--head" role="row">
          <span className="pc-diff__name" role="columnheader" />
          {columns.map((c) => (
            <span key={c.feature} className="pc-diff__group" role="columnheader">
              <span className="pc-diff__feature">{chartMixes()[c.feature]}</span>
              <span className="pc-diff__values">
                {c.values.map((v) => (
                  <span key={v} className="pc-diff__value mono">{v}</span>
                ))}
              </span>
            </span>
          ))}
        </div>
        {patterns.map((p, i) => (
          <button
            key={p.cluster_id}
            className={"pc-diff__row" + (dim(p.cluster_id) ? " is-dim" : "")}
            role="row"
            onClick={() => onPick(p.cluster_id)}
          >
            <span className="pc-diff__name" role="rowheader">
              <span className="pc-legend__key" style={{ background: seriesColor(i) }} />
              {p.name}
            </span>
            {columns.map((c) => {
              const shares = sharesOf(p.categorical?.[c.feature]);
              return (
                <span key={c.feature} className="pc-diff__values">
                  {c.values.map((v) => {
                    const mine = shares[v] ?? 0;
                    const all = c.everyone[v] ?? 0;
                    const diff = mine - all;
                    return (
                      <span
                        key={v}
                        role="cell"
                        className="pc-diff__cell num"
                        style={{ background: divergeColor(diff) }}
                        {...bind(
                          <>
                            <TipRow color={seriesColor(i)} value={formatPercent(mine)} label={`${chartMixes()[c.feature]} ${v} · ${p.name}`} />
                            <div className="pc-tip__note">
                              All in scope: {formatPercent(all)} ({diff >= 0 ? "+" : "−"}
                              {Math.abs(diff * 100).toFixed(0)} pts)
                            </div>
                          </>,
                        )}
                      >
                        {Math.round(mine * 100)}
                      </span>
                    );
                  })}
                </span>
              );
            })}
          </button>
        ))}
        <div className="pc-diff__row pc-diff__row--all" role="row">
          <span className="pc-diff__name" role="rowheader">All in scope</span>
          {columns.map((c) => (
            <span key={c.feature} className="pc-diff__values">
              {c.values.map((v) => (
                <span key={v} role="cell" className="pc-diff__cell pc-diff__cell--all num">
                  {Math.round((c.everyone[v] ?? 0) * 100)}
                </span>
              ))}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// One pattern, in detail
// ---------------------------------------------------------------------------

interface DetailProps {
  pattern: DiscoveredPattern | ClaimsPattern;
  index: number;
  patterns: DiscoveredPattern[];
  distributions?: Record<string, ScopeDistribution>;
}

export function PatternDistributions({ pattern, index, patterns, distributions }: DetailProps) {
  const { bind, layer } = useTip();
  const color = seriesColor(index);
  const histograms = pattern.histograms ?? {};
  const shapes = Object.keys(chartRanges()).filter((f) => histograms[f] && distributions?.[f]);
  const mixes = Object.keys(chartMixes()).filter((f) => pattern.categorical?.[f]);
  const topKey = facts().top?.[0];
  const dx = topKey ? ((pattern as unknown as Record<string, unknown>)[topKey] as
    Record<string, number> | undefined) : undefined;

  return (
    <section className="pd" aria-label={`Distributions of ${pattern.name}`}>
      <div className="pd__legend">
        <span><span className="pc-legend__key" style={{ background: color }} /> This pattern</span>
        <span><span className="pd__line-key" /> All in scope</span>
      </div>

      {shapes.length === 0 && (
        <p className="pc__note">
          No distributions for this pattern. Analyze again to view them.
        </p>
      )}

      <div className="pd__grid">
        {shapes.map((feature) => (
          <Histogram
            key={feature}
            feature={feature}
            counts={histograms[feature]}
            scope={distributions![feature]}
            color={color}
            bind={bind}
          />
        ))}
        {mixes.map((feature) => (
          <Mix
            key={feature}
            feature={feature}
            mine={sharesOf(pattern.categorical[feature])}
            everyone={everyoneMix(patterns, feature)}
            color={color}
            bind={bind}
          />
        ))}
        {dx && Object.keys(dx).length > 0 && (
          <Diagnoses pattern={pattern as ClaimsPattern} color={color} bind={bind} />
        )}
      </div>
      {layer}
    </section>
  );
}

function Histogram({
  feature, counts, scope, color, bind,
}: {
  feature: string;
  counts: number[];
  scope: ScopeDistribution;
  color: string;
  bind: Bind;
}) {
  const meta = chartRanges()[feature];
  const mineTotal = counts.reduce((s, c) => s + c, 0) || 1;
  const allTotal = scope.counts.reduce((s, c) => s + c, 0) || 1;
  const mine = counts.map((c) => c / mineTotal);
  const all = scope.counts.map((c) => c / allTotal);
  const top = Math.max(...mine, ...all) || 1;
  const n = counts.length;
  const edges = scope.edges;
  const label = (i: number) =>
    i === n - 1 && scope.open_top
      ? `${fmtValue(edges[i], meta.money)}+`
      : `${fmtValue(edges[i], meta.money)}–${fmtValue(edges[i + 1], meta.money)}`;
  // Everyone's distribution as a step line over the bars, in the same units.
  const points = all
    .flatMap((v, i) => [`${(i / n) * 100},${100 - (v / top) * 100}`, `${((i + 1) / n) * 100},${100 - (v / top) * 100}`])
    .join(" ");
  const grid = ticks(0, top, 3).filter((v) => v > 0);
  const every = Math.ceil(n / 6);

  return (
    <figure className="pd-hist">
      <figcaption className="pd__caption">
        {meta.label}
        {!meta.money && <span className="pc-range__unit"> {meta.unit}</span>}
      </figcaption>
      <div className="pd-hist__plot">
        {grid.map((g) => (
          <span key={g} className="pd-hist__grid" style={{ bottom: `${(g / top) * 100}%` }}>
            <span className="pd-hist__gridlabel num">{Math.round(g * 100)}%</span>
          </span>
        ))}
        <div className="pd-hist__bars">
          {mine.map((v, i) => (
            <span
              key={i}
              className="pd-hist__bin"
              tabIndex={0}
              {...bind(
                <>
                  <TipRow color={color} value={formatPercent(v)} label="of this pattern" />
                  <TipRow color="var(--ink-faint)" value={formatPercent(all[i])} label="of all in scope" />
                  <div className="pc-tip__note">{label(i)}{meta.money ? "" : ` ${meta.unit}`}</div>
                </>,
              )}
            >
              <span className="pd-hist__bar" style={{ height: `${(v / top) * 100}%`, background: color }} />
            </span>
          ))}
        </div>
        <svg className="pd-hist__line" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
          <polyline points={points} vectorEffect="non-scaling-stroke" />
        </svg>
      </div>
      <div className="pd-hist__axis">
        {edges.slice(0, n).map((e, i) =>
          i % every === 0 ? (
            <span key={i} className="pd-hist__tick num" style={{ left: `${(i / n) * 100}%` }}>
              {fmtValue(e, meta.money)}
            </span>
          ) : null,
        )}
        <span className="pd-hist__tick pd-hist__tick--end num">
          {fmtValue(edges[n], meta.money)}
          {scope.open_top ? "+" : ""}
        </span>
      </div>
    </figure>
  );
}

function Mix({
  feature, mine, everyone, color, bind,
}: {
  feature: string;
  mine: Record<string, number>;
  everyone: Record<string, number>;
  color: string;
  bind: Bind;
}) {
  const values = Array.from(new Set([...Object.keys(everyone), ...Object.keys(mine)]))
    .sort((a, b) => (everyone[b] ?? 0) - (everyone[a] ?? 0))
    .slice(0, 8);
  const top = Math.max(...values.map((v) => Math.max(mine[v] ?? 0, everyone[v] ?? 0))) || 1;
  return (
    <figure className="pd-mix">
      <figcaption className="pd__caption">{chartMixes()[feature]}</figcaption>
      {values.map((v) => {
        const a = mine[v] ?? 0;
        const b = everyone[v] ?? 0;
        return (
          <div
            key={v}
            className="pd-mix__row"
            tabIndex={0}
            {...bind(
              <>
                <TipRow color={color} value={formatPercent(a)} label={`${v} in this pattern`} />
                <TipRow color="var(--ink-faint)" value={formatPercent(b)} label="overall in scope" />
              </>,
            )}
          >
            <span className="pd-mix__value mono">{v}</span>
            <span className="pd-mix__track">
              <span className="pd-mix__bar" style={{ width: `${(a / top) * 100}%`, background: color }} />
              <span className="pd-mix__mark" style={{ left: `${(b / top) * 100}%` }} />
            </span>
            <span className="pd-mix__num num">{Math.round(a * 100)}%</span>
          </div>
        );
      })}
    </figure>
  );
}

function Diagnoses({ pattern, color, bind }: { pattern: ClaimsPattern; color: string; bind: Bind }) {
  // The pack's most frequent codes (health: diagnoses), with their names.
  const [countsKey, namesKey, caption] = facts().top ?? ["", null, ""];
  const held = pattern as unknown as Record<string, Record<string, number> | undefined>;
  const entries = Object.entries(held[countsKey] ?? {}).slice(0, 8);
  const top = Math.max(...entries.map(([, c]) => c)) || 1;
  const names = ((namesKey ? held[namesKey] : undefined) ?? {}) as unknown as Record<string, string>;
  return (
    <figure className="pd-mix pd-mix--wide">
      <figcaption className="pd__caption">{caption} (share of {linesOf(keyOf("child"))})</figcaption>
      {entries.map(([code, count]) => (
        <div
          key={code}
          className="pd-mix__row"
          tabIndex={0}
          {...bind(
            <>
              <TipRow color={color} value={formatPercent(count / pattern.size)} label={names[code] ?? code} />
              <div className="pc-tip__note">{formatCount(count)} {linesOf(keyOf("child"))} · {code}</div>
            </>,
          )}
        >
          <span className="pd-mix__value pd-mix__value--long">{names[code] ?? code}</span>
          <span className="pd-mix__track">
            <span className="pd-mix__bar" style={{ width: `${(count / top) * 100}%`, background: color }} />
          </span>
          <span className="pd-mix__num num">{Math.round((count / pattern.size) * 100)}%</span>
        </div>
      ))}
    </figure>
  );
}
