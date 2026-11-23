/**
 * A pattern's counts: every field, every value, how many and what share --
 * and every scenario, one row each. The numbers the one-sheet export holds,
 * on the card. Each field says whether generation refers to it, so what a
 * pattern carries into generated data is read here rather than guessed.
 *
 * Nothing is left out: a field with many values (diagnoses, ZIPs) opens to
 * show all of them, and a scenario of one member is listed like any other.
 */

import { useEffect, useState } from "react";
import { Banner, Button, formatCount, formatPercent } from "../components/primitives";
import {
  ApiError,
  api,
  DOWNLOAD_FORMATS,
  type Domain,
  type DownloadFormat,
  type PatternFilters,
  type PatternReport,
  type PatternReportRow,
} from "../lib/api";
import "./PatternCounts.css";

interface Props {
  domain: Domain;
  name: string;
  filters: PatternFilters;
  k: number | null;
  clusterId: number;
  claimType: string | null;
  // The features the scenarios are split on: the Scenarios panel's choice.
  split: string[] | null;
}

// A field with more values than this shows them on request.
const FIRST = 8;

export function PatternCounts({ domain, name, filters, k, clusterId, claimType, split }: Props) {
  const splitOn = split ? { scenario_keys: split } : {};
  const splitKey = JSON.stringify(split);
  const [report, setReport] = useState<PatternReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<DownloadFormat | null>(null);

  // Exactly what the panel shows, as a file: every field, every value, every scenario.
  const exportIt = async (format: DownloadFormat) => {
    setExporting(format);
    setError(null);
    try {
      const stem = name.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-|-$/g, "").toLowerCase();
      await api.patternReportSheet(
        { domain, filters, k, cluster_id: clusterId, claim_type: claimType, name, format,
          ...splitOn },
        `${stem || "pattern"}.${format}`,
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setExporting(null);
    }
  };

  useEffect(() => {
    let live = true;
    setReport(null);
    setError(null);
    api
      .patternReport({ domain, filters, k, cluster_id: clusterId, claim_type: claimType,
        ...splitOn })
      .then((r) => live && setReport(r))
      .catch((e) => live && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      live = false;
    };
    // splitOn is rebuilt every render; splitKey says when it changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [domain, filters, k, clusterId, claimType, splitKey]);

  if (error && !report) return <Banner tone="alarm">{error}</Banner>;
  if (!report) return <p className="counts__wait">Counting…</p>;

  const fields: { key: string; label: string; rows: PatternReportRow[] }[] = [];
  for (const row of report.rows) {
    const last = fields[fields.length - 1];
    if (last && last.key === row.field) last.rows.push(row);
    else fields.push({ key: row.field, label: row.field_label, rows: [row] });
  }

  return (
    <section className="counts" aria-label="Counts and percentages">
      <div className="counts__head">
        <h4>Counts and %</h4>
        <div className="counts__export" role="group" aria-label="Download">
          <span className="counts__download">Download as</span>
          {DOWNLOAD_FORMATS.map((f) => (
            <Button
              key={f.value}
              onClick={() => void exportIt(f.value)}
              busy={exporting === f.value}
              disabled={exporting !== null && exporting !== f.value}
            >
              {f.label}
            </Button>
          ))}
        </div>
      </div>
      {error && <Banner tone="alarm">{error}</Banner>}
      <p className="counts__note">
        Counted over all <strong className="num">{formatCount(report.size)}</strong>{" "}
        {report.size_basis} in the pattern.
      </p>
      <div className="counts__fields">
        {fields.map((f) => (
          <Field key={f.key} label={f.label} rows={f.rows} />
        ))}
      </div>
      {report.scenarios.length > 0 && (
        <div className="counts__scenarios">
          <h5>
            Scenarios by {report.scenario_keys.map((s) => s.label.toLowerCase()).join(", ")}
          </h5>
          <table className="counts__table">
            <thead>
              <tr>
                <th scope="col">Scenario</th>
                <th scope="col" className="num">Count</th>
                <th scope="col" className="num">% of pattern</th>
              </tr>
            </thead>
            <tbody>
              {report.scenarios.map((s) => (
                <tr key={s.label}>
                  <td>{s.label}</td>
                  <td className="num">{formatCount(s.count)}</td>
                  <td className="num">{formatPercent(s.pct, 1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Field({ label, rows }: { label: string; rows: PatternReportRow[] }) {
  const [all, setAll] = useState(false);
  const shown = all ? rows : rows.slice(0, FIRST);
  const first = rows[0];
  return (
    <div className="counts__field">
      <h5>
        {label}
        <span className="counts__basis">
          {" "}
          of {formatCount(first.of)} {first.basis}
        </span>
      </h5>
      <p className="counts__generation">Generated data: {first.generation || "—"}</p>
      <table className="counts__table">
        <tbody>
          {shown.map((r) => (
            <tr key={r.value}>
              <td>
                {r.value}
                {r.value_label && <span className="counts__label"> {r.value_label}</span>}
              </td>
              <td className="num">{formatCount(r.count)}</td>
              <td className="num">{formatPercent(r.pct, 1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > FIRST && (
        <Button onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `Show all ${rows.length}`}
        </Button>
      )}
    </div>
  );
}
