/**
 * EDI transaction sets: pick one, write it.
 *
 * The formats are descriptions on disk, one file per transaction set, listing
 * segments and the element positions within them. That is the same shape the
 * masking tool this organization already runs uses to address EDI — segment,
 * position, and what goes there — with a rule in place of an algorithm,
 * because one tool changes a value that exists and the other produces one that
 * does not. A position names an element; it never names a column.
 *
 * So this screen does not know about 837 or 835. It lists what is described and
 * writes what is chosen, and a format somebody adds appears here without
 * anything being rebuilt.
 *
 * The file is streamed, not built and then sent: an extract is the one thing
 * here whose size is not bounded by what fits on a screen.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Banner, Button, Chip, Empty } from "../components/primitives";
import {
  ApiError,
  api,
  type EdiFormatSummary,
  type FilePattern,
  type SnipReport,
  type SnipRules,
} from "../lib/api";
import "./EdiFormats.css";
import { isChild, isPrimary, termsOf, words } from "../lib/pack";

const SAMPLE_ROWS = 40;

interface Props {
  /** The pattern part of the request, from the screen above. */
  extra: Record<string, unknown>;
  pattern: FilePattern | null;
}

export function EdiFormats({ extra, pattern }: Props) {
  // What the business pack calls the records, as this tab says them.
  const P = words("primary");
  const C = words("child");
  const [formats, setFormats] = useState<EdiFormatSummary[]>([]);
  const [chosen, setChosen] = useState<string>("");
  const [sample, setSample] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // Groups per ST/SE set. A whole book in one transaction is valid and
  // nobody sends it; trading partners cap the size.
  // 1,000 a transaction by default, which trading partners take; all in one
  // is valid and larger than most accept.
  const [perTransaction, setPerTransaction] = useState(1000);
  const [error, setError] = useState<string | null>(null);
  // SNIP-style checks: the rules in effect, a trading partner, the report.
  const [snip, setSnip] = useState<SnipRules | null>(null);
  // The rules failing to load is said: without them there are no trading
  // partners, so level 7 cannot run.
  const [snipError, setSnipError] = useState<string | null>(null);
  const [partner, setPartner] = useState<string>("");
  const [report, setReport] = useState<SnipReport | null>(null);
  // The whole file by default: "SNIP passed" is said of the file written.
  const [checkRows, setCheckRows] = useState<number>(0);
  // What the report shown was of: so many records of the file, or all of it.
  const [checkedRows, setCheckedRows] = useState<number>(0);
  const upload = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let live = true;
    api
      .snipRules()
      .then((rules) => live && setSnip(rules))
      .catch((e: unknown) => {
        if (!live) return;
        setSnip(null);
        setSnipError(e instanceof ApiError ? e.message : String(e));
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    let live = true;
    api
      .ediFormats()
      .then((payload) => {
        if (!live) return;
        setFormats(payload.formats);
        if (payload.formats.length) setChosen(payload.formats[0].id);
      })
      .catch((e) => live && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, []);

  const format = formats.find((f) => f.id === chosen);

  const preview = useCallback(async () => {
    if (!format) return;
    setBusy("preview");
    setError(null);
    try {
      setSample(
        await api.exportText(`edi:${format.id}`, format.subject, 6, {
          per_transaction: perTransaction || null,
          ...extra,
        }),
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setSample(null);
    } finally {
      setBusy(null);
    }
  }, [format, perTransaction, extra]);

  const write = useCallback(async () => {
    if (!format) return;
    setBusy("write");
    setError(null);
    try {
      // Streamed to the browser as a download rather than held in memory
      // here: the point of the writer is that it never holds the whole file.
      await api.exportDownload(
        `edi:${format.id}`,
        format.subject,
        `${format.id.toLowerCase()}.txt`,
        { per_transaction: perTransaction || null, ...extra },
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [format, perTransaction, extra]);

  const check = useCallback(async () => {
    if (!format) return;
    setBusy("check");
    setError(null);
    try {
      setCheckedRows(checkRows);
      setReport(
        await api.snipCheck(
          {
            format: `edi:${format.id}`,
            subject: format.subject,
            rows: checkRows || null,
            per_transaction: perTransaction || null,
            ...extra,
          },
          partner || null,
        ),
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [format, checkRows, perTransaction, extra, partner]);

  const checkFile = useCallback(
    async (file: File) => {
      setBusy("check-file");
      setError(null);
      try {
        setReport(await api.snipCheckFile(file, partner || null));
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
        if (upload.current) upload.current.value = "";
      }
    },
    [partner],
  );

  if (!formats.length && !error) {
    return (
      <Empty title="No transaction sets">
        No X12 transaction set definitions are installed.
      </Empty>
    );
  }

  return (
    <section className="panel edi">
      {error && <Banner tone="alarm">{error}</Banner>}

      <div className="edi__pick">
        <label className="named">
          <span>Transaction set</span>
          <select value={chosen} onChange={(e) => setChosen(e.target.value)}>
            {formats.map((f) => (
              <option key={f.id} value={f.id}>
                {f.name}
              </option>
            ))}
          </select>
        </label>

        <label className="named">
          <span>{termsOf(format?.subject).Many} per transaction</span>
          <select
            value={perTransaction}
            onChange={(e) => setPerTransaction(Number(e.target.value))}
          >
            <option value={0}>All in one</option>
            <option value={100}>100</option>
            <option value={500}>500</option>
            <option value={1000}>1,000</option>
            <option value={5000}>5,000</option>
          </select>
        </label>

        {format && (
          <div className="edi__facts">
            <Chip tone="catalog">{format.version}</Chip>
            <Chip tone="learned">Source: {termsOf(format.subject).Many}</Chip>
          </div>
        )}
      </div>

      {format?.description && <p className="panel__hint">{format.description}</p>}

      <div className="panel__actions">
        <Button onClick={preview} busy={busy === "preview"}>
          Preview
        </Button>
        <Button variant="primary" onClick={write} busy={busy === "write"}>
          Generate file
        </Button>
        <p className="panel__hint">
          {pattern === null
            ? "Generated from the plan: the same population as Load to database. "
            : pattern.kind === "request"
              ? isChild(format?.subject) && isPrimary(pattern.entity)
                ? `${C.Many} from the selected ${C.many} pattern, for the ${P.many} of ${pattern.name}. `
                : `The ${termsOf(pattern.entity).many} of ${pattern.name}, reproduced as in its first file. `
            : isChild(format?.subject)
              ? `${C.Many} for the existing ${P.many} of "${pattern.name}" (de-identified ${P.many} from Pattern data). `
              : `New ${P.many} generated with the values of "${pattern.name}". `}
          {perTransaction
            ? `Split into ST/SE transaction sets of ${perTransaction.toLocaleString()} records.`
            : "All records in one ST/SE transaction set: valid X12, but larger than most trading partners accept."}
        </p>
      </div>

      <div className="snip">
        <div className="snip__head">
          <h3>SNIP validation</h3>
          <p className="panel__hint">
            SNIP levels 1–7 (level 7 with a trading partner). SNIP-style validation, not certification.
          </p>
        </div>
        <div className="snip__controls">
          <label className="named">
            <span>Records to validate</span>
            <select value={checkRows} onChange={(e) => setCheckRows(Number(e.target.value))}>
              <option value={0}>Entire file</option>
              <option value={50}>First 50</option>
              <option value={200}>First 200</option>
              <option value={1000}>First 1,000</option>
            </select>
          </label>
          <label className="named">
            <span>Trading partner</span>
            <select value={partner} onChange={(e) => setPartner(e.target.value)}>
              <option value="">None (skip level 7)</option>
              {(snip?.partners ?? []).map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <Button variant="primary" onClick={check} busy={busy === "check"} disabled={!format}>
            Run SNIP validation
          </Button>
          <Button onClick={() => upload.current?.click()} busy={busy === "check-file"}>
            Validate uploaded file…
          </Button>
          <input
            ref={upload}
            type="file"
            hidden
            onChange={(e) => e.target.files?.[0] && void checkFile(e.target.files[0])}
          />
        </div>
        {snipError && (
          <Banner tone="caution">
            {`Couldn't load the SNIP rule files, so no trading partner can be chosen and level 7 is skipped: ${snipError}`}
          </Banner>
        )}
        {report && <SnipResult report={report} firstRecords={report.file ? 0 : checkedRows} />}
        {snip && (
          <details className="snip__rules">
            <summary>Active rule files ({snip.rules.length})</summary>
            <ul>
              {snip.rules.map((r) => (
                <li key={r.file}>
                  <Chip tone={r.origin === "shipped" ? "physical" : "user"}>{ORIGIN_NAMES[r.origin] ?? r.origin}</Chip>{" "}
                  <span className="mono">{r.id}</span> {r.version ?? r.name ?? ""} ·{" "}
                  {r.rules} {r.rules === 1 ? "rule" : "rules"}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>

      {sample && (
        <pre className="edi__sample">
          {/* Segments already end with a line break, so splitting on
              lines is enough: adding one per segment double-spaces it. */}
          {sample.split("\n").filter(Boolean).slice(0, SAMPLE_ROWS).join("\n")}
        </pre>
      )}
    </section>
  );
}


const ORIGIN_NAMES: Record<string, string> = {
  shipped: "Standard",
  customer: "Custom",
  partner: "Trading partner",
};

const STATUS_WORDS: Record<string, string> = {
  pass: "Passed",
  fail: "Failed",
  warn: "Warnings",
  partial: "Partially validated",
  not_checked: "Not validated",
};

const STATUS_TONE: Record<string, "learned" | "alarm" | "caution" | "physical"> = {
  pass: "learned",
  fail: "alarm",
  warn: "caution",
  partial: "caution",
  not_checked: "physical",
};

/** One report: a row per SNIP level, findings under the ones that have them. */
export function SnipResult({ report, firstRecords = 0 }: {
  report: SnipReport;
  // Checked only the first so many records of the file, not all of it.
  firstRecords?: number;
}) {
  const [open, setOpen] = useState<number | null>(
    report.levels.find((l) => l.status === "fail")?.level ?? null,
  );
  return (
    <div className="snip__report">
      <p className="snip__summary">
        <strong>{report.passed ? "No errors" : "Errors found"}</strong>
        {": "}
        {report.summary ? (
          report.summary
        ) : (
          <>
            {report.file ? (
              <span className="mono">{report.file}</span>
            ) : firstRecords ? (
              `first ${firstRecords.toLocaleString()} records of the generated file`
            ) : (
              "entire generated file"
            )},{" "}
            {report.transaction} {report.version}, {report.transactions} transaction
            {report.transactions === 1 ? "" : "s"}, {report.segments.toLocaleString()} segments
            {report.partner ? `, trading partner ${report.partner}` : ""}.
          </>
        )}
      </p>
      <table className="snip__levels">
        <tbody>
          {report.levels.map((level) => (
            <tr
              key={level.level}
              className={level.findings.length || level.notes.length ? "snip__row--open" : ""}
              onClick={() => setOpen(open === level.level ? null : level.level)}
            >
              <td className="num">{level.level}</td>
              <td>
                {level.name}
                {open === level.level && (level.findings.length > 0 || level.notes.length > 0) && (
                  <ul className="snip__findings" onClick={(e) => e.stopPropagation()}>
                    {grouped(level.findings).map((g) => (
                      <SnipFinding key={g.rule} group={g} />
                    ))}
                    {level.notes.map((n, i) => (
                      <li key={`n${i}`} className="snip__note">{n}</li>
                    ))}
                  </ul>
                )}
              </td>
              <td>
                <Chip tone={STATUS_TONE[level.status]}>{STATUS_WORDS[level.status]}</Chip>
              </td>
              <td className="num snip__counts">
                {level.rules ? `${level.rules} ${level.rules === 1 ? "rule" : "rules"}` : ""}
                {level.errors ? ` · ${level.errors} ${level.errors === 1 ? "error" : "errors"}` : ""}
                {level.warnings ? ` · ${level.warnings} ${level.warnings === 1 ? "warning" : "warnings"}` : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="panel__hint">{report.disclaimer}</p>
    </div>
  );
}


type FindingRow = SnipReport["levels"][number]["findings"][number];

/** One entry per rule: the first finding, and how many there were. */
function grouped(findings: FindingRow[]): { rule: string; items: FindingRow[] }[] {
  const out = new Map<string, FindingRow[]>();
  for (const f of findings) {
    out.set(f.rule, [...(out.get(f.rule) ?? []), f]);
  }
  return [...out.entries()].map(([rule, items]) => ({ rule, items }));
}

function SnipFinding({ group }: { group: { rule: string; items: FindingRow[] } }) {
  const [all, setAll] = useState(false);
  const [first, ...rest] = group.items;
  const shown = all ? group.items : [first];
  return (
    <li>
      <span className="mono snip__rule">{group.rule}</span> {first.message}
      {group.items.length > 1 && (
        <button className="snip__more" onClick={() => setAll(!all)}>
          {all ? "Show less" : `+${rest.length} more`}
        </button>
      )}
      {shown.map((f, i) => (
        <span key={i} className="snip__occurrence">
          {all && i > 0 ? <span className="snip__again">{f.message}</span> : null}
          {f.segment ? <span className="snip__where">Segment {f.segment}</span> : null}
          {f.raw ? <code className="mono snip__raw">{f.raw}</code> : null}
        </span>
      ))}
    </li>
  );
}
