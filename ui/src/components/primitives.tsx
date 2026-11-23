/**
 * The small pieces every screen shares.
 *
 * Two encodings run through the whole interface and never overlap. Hue says
 * where a rule came from. Fill says how sure the tool is. A filled chip is
 * settled, an outlined one is a proposal, a dashed one is a guess, and the
 * color tells you which layer produced it either way.
 */

import type { ReactNode } from "react";
import type { Bucket, Confidence, Evidence, Provenance } from "../lib/api";
import "./primitives.css";

export function Chip({
  tone = "neutral",
  fill = "solid",
  children,
  title,
}: {
  tone?: Provenance | "neutral" | "alarm" | "caution" | "affirm";
  fill?: "solid" | "outline" | "dashed";
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`chip chip--${tone} chip--${fill}`} title={title}>
      {children}
    </span>
  );
}

const CONFIDENCE_FILL: Record<Confidence, "solid" | "outline" | "dashed"> = {
  high: "solid",
  medium: "outline",
  low: "dashed",
};

const CONFIDENCE_WORDS: Record<Confidence, string> = {
  high: "High confidence",
  medium: "Medium confidence; review recommended",
  low: "Low confidence; verify before use",
};

/** Where a rule came from, and how sure the tool is, in one mark. */
export function RuleChip({
  provenance,
  confidence,
  children,
}: {
  provenance: Provenance | null;
  confidence: Confidence | null;
  children: ReactNode;
}) {
  const fill = confidence ? CONFIDENCE_FILL[confidence] : "dashed";
  const title = [
    provenance ? `${PROVENANCE_WORDS[provenance]}` : null,
    confidence ? CONFIDENCE_WORDS[confidence] : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <Chip tone={provenance ?? "neutral"} fill={fill} title={title}>
      {children}
    </Chip>
  );
}

export const PROVENANCE_WORDS: Record<Provenance, string> = {
  structural: "Schema constraint",
  catalog: "Catalog rule",
  learned: "Learned from data",
  user: "User override",
  physical: "Column type default",
};

export const BUCKET_WORDS: Record<Bucket, string> = {
  system: "Database-generated value",
  reference: "Foreign key reference",
  code: "Code set value",
  derivable: "Derived from other columns",
  business_key: "Business key",
  free: "Other",
};

export function BucketMark({ bucket }: { bucket: Bucket }) {
  return (
    <span className={`bucket bucket--${bucket}`} title={BUCKET_WORDS[bucket]}>
      {bucket.replace("_", " ")}
    </span>
  );
}

/** A share, drawn as a bar so a column of them can be read at a glance. */
export function Share({ value, tone = "learned" }: { value: number; tone?: string }) {
  const percent = Math.max(0, Math.min(1, value)) * 100;
  return (
    <span className="share" title={`${percent.toFixed(1)}%`}>
      <span className="share__track">
        <span
          className="share__fill"
          style={{ width: `${percent}%`, background: `var(--${tone})` }}
        />
      </span>
      <span className="share__label num">{formatPercent(value)}</span>
    </span>
  );
}

/**
 * The measured shape of a distribution, drawn small.
 *
 * This is the evidence behind a ratio, and it belongs next to the ratio rather
 * than a click away: the whole argument for the tool is that the shape is not
 * flat.
 */
export function Histogram({
  bins,
  height = 34,
  highlightZero = true,
}: {
  bins: { children: number; weight: number }[];
  height?: number;
  highlightZero?: boolean;
}) {
  if (!bins.length) return null;
  const peak = Math.max(...bins.map((b) => b.weight)) || 1;
  return (
    <span className="histogram" style={{ height }} aria-hidden="true">
      {bins.map((bin, index) => (
        <span
          key={index}
          className={
            "histogram__bar" +
            (highlightZero && bin.children === 0 ? " histogram__bar--zero" : "")
          }
          style={{ height: `${Math.max(2, (bin.weight / peak) * 100)}%` }}
          title={`${bin.children} → ${formatPercent(bin.weight)}`}
        />
      ))}
    </span>
  );
}

export function Field({
  label,
  children,
  wide = false,
}: {
  label: string;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className={"field" + (wide ? " field--wide" : "")}>
      <div className="field__label">{label}</div>
      <div className="field__value">{children}</div>
    </div>
  );
}

/** Evidence, always shown in full. It is the product's whole argument. */
export function EvidenceList({ items }: { items: Evidence[] }) {
  if (!items.length) return null;
  return (
    <ul className="evidence">
      {items.map((item, index) => (
        <li key={index}>
          <span className="evidence__kind">{item.kind.replace(/_/g, " ")}</span>
          <span className="evidence__detail">{item.detail}</span>
          {item.coverage != null && (
            <span className="evidence__coverage num">{formatPercent(item.coverage)}</span>
          )}
        </li>
      ))}
    </ul>
  );
}

export function Button({
  children,
  onClick,
  variant = "quiet",
  disabled,
  busy,
  title,
  type = "button",
  keepFocus = false,
  describedBy,
  expanded,
  id,
  label,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "quiet" | "danger";
  disabled?: boolean;
  busy?: boolean;
  title?: string;
  type?: "button" | "submit";
  /** Unavailable or busy, it stays focusable (aria-disabled): focus is not
   *  lost while it works, and its title (the reason) can still be reached. */
  keepFocus?: boolean;
  /** The id of the text that says why it is unavailable. */
  describedBy?: string;
  /** For a button that opens a panel: whether it is open. */
  expanded?: boolean;
  id?: string;
  /** Its accessible name, where the visible words alone do not say what it acts on. */
  label?: string;
}) {
  const off = Boolean(disabled || busy);
  return (
    <button
      type={type}
      className={`btn btn--${variant}${busy ? " btn--busy" : ""}`}
      onClick={keepFocus && off ? undefined : onClick}
      disabled={keepFocus ? undefined : off}
      aria-disabled={keepFocus && off ? true : undefined}
      aria-busy={busy || undefined}
      aria-describedby={describedBy}
      aria-expanded={expanded}
      aria-label={label}
      id={id}
      title={title}
    >
      {busy && <span className="btn__spinner" aria-hidden="true" />}
      {children}
    </button>
  );
}

export function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <h3 className="empty__title">{title}</h3>
      {children && <p className="empty__body">{children}</p>}
      {action && <div className="empty__action">{action}</div>}
    </div>
  );
}

export function Banner({
  tone,
  children,
}: {
  tone: "alarm" | "caution" | "affirm" | "neutral";
  children: ReactNode;
}) {
  // An alarm is announced as it appears; the others are read in place.
  return (
    <div className={`banner banner--${tone}`} role={tone === "alarm" ? "alert" : undefined}>
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Formatting                                                          */
/* ------------------------------------------------------------------ */

export function formatCount(value: number | null | undefined): string {
  if (value == null) return "—";
  return value.toLocaleString("en-US");
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value == null) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatRatio(value: number | null | undefined): string {
  if (value == null) return "—";
  return value.toFixed(2);
}

/** Parameters read better as one line each than as pretty-printed JSON. */
export function describeParams(params: Record<string, unknown>, limit = 6): string[] {
  return Object.entries(params).map(([key, value]) => {
    if (Array.isArray(value)) {
      const head = value.slice(0, limit).map(short).join(", ");
      const rest = value.length > limit ? `, … ${(value.length - limit).toLocaleString()} more` : "";
      return `${key}: [${head}${rest}]`;
    }
    return `${key}: ${short(value)}`;
  });
}

function short(value: unknown): string {
  if (value === null || value === undefined) return "null";
  // Quoted, but not JSON-escaped: a regex is the commonest value here and
  // showing it as \d{10} rather than \d{10} makes it unreadable.
  if (typeof value === "string") return `"${value}"`;
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).slice(0, 3);
    return `{${entries.map(([k, v]) => `${k}=${String(v)}`).join(", ")}}`;
  }
  return String(value);
}
