/**
 * Generate file ▾: a pattern's file, downloaded in one step.
 *
 * Choosing a format writes the file with the defaults Generate › Files starts
 * from -- 100 new primary records; their children as the pattern measured
 * them -- as a run of its own, and the confirmation names the run. The pattern is saved first
 * where it is still only an analysis's, because a run is kept against a saved
 * pattern. More options opens Generate › Files with the pattern chosen.
 */

import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  api,
  patternRequest,
  type Domain,
  type FilePattern,
} from "../lib/api";
import { useAppActions } from "../lib/appActions";
import { formatsOf, isChild } from "../lib/pack";
import "./actions.css";

export interface FileFormat {
  label: string;
  /** The /api/export format, as Generate › Files sends it. */
  format: string;
  /** The file name, as Generate › Files names it; the run is added to it. */
  file: string;
  edi?: boolean;
}

/** What each format the screens can write is called, and the file name it is
 *  written under. Which of them a pattern is offered in is the pack's. */
const KNOWN: Record<string, Omit<FileFormat, "format">> = {
  "edi:834": { label: "X12 834", file: "834.txt", edi: true },
  "edi:837P": { label: "X12 837P", file: "837p.txt", edi: true },
  "edi:835": { label: "X12 835", file: "835.txt", edi: true },
  "standard:fhir-members": { label: "FHIR", file: "fhir-members.json" },
  "standard:fhir-claims": { label: "FHIR", file: "fhir-claims.json" },
  "standard:hl7-adt": { label: "HL7 v2", file: "hl7v2-adt.hl7" },
  "standard:hl7-dft": { label: "HL7 v2", file: "hl7v2-dft.hl7" },
  "standard:hl7v3-prpa-add": { label: "HL7 v3", file: "hl7v3-patient-add.zip" },
};

/** The formats a pattern of these records is written in: the pack's, in its
 *  order. A CSV file is named for its records. */
export function fileFormats(domain: Domain): FileFormat[] {
  return formatsOf(domain).flatMap((format) =>
    format === "csv"
      ? [{ label: "CSV", format, file: `${domain}.csv` }]
      : KNOWN[format] ? [{ format, ...KNOWN[format] }] : []);
}

/** New primary records in a file, as Generate › Files starts. */
export const DEFAULT_COUNT = 100;
/** Groups per ST/SE set, as the X12 EDI tab starts. */
const PER_TRANSACTION = 1000;

/** What an export failure says, without the JSON it came wrapped in. */
export function failure(e: unknown): string {
  if (!(e instanceof Error)) return String(e);
  try {
    const body = JSON.parse(e.message);
    if (body && typeof body.detail === "string") return body.detail;
  } catch {
    /* not JSON: the message is the sentence */
  }
  return e.message;
}

/**
 * The export request for one format, with Generate › Files' defaults: a new
 * run started now, 100 new primary records, children for every one of the
 * pattern's as it measured them.
 */
export async function fileRequest(
  pattern: FilePattern,
  domain: Domain,
  format: FileFormat,
  count: number = DEFAULT_COUNT,
): Promise<Record<string, unknown>> {
  const extra: Record<string, unknown> = {
    ...patternRequest(pattern, count),
    request_started: new Date().toISOString(),
  };
  if (format.edi) extra.per_transaction = PER_TRANSACTION;
  if (isChild(domain)) {
    extra.claimed_share = 100;
    if (pattern.kind === "saved") {
      try {
        const measured = await api.claimShape(pattern.id);
        extra.claims_mix = measured.claims_each;
        extra.lines_mix = measured.lines_each;
      } catch {
        // Unmeasured: the pattern's average, as Generate › Files falls back to.
      }
    }
  }
  return extra;
}

interface Props {
  domain: Domain;
  /** The pattern the file is for, saved first where it needs to be. */
  prepare: () => Promise<FilePattern>;
  /** More options: Generate › Files. By default with the prepared pattern. */
  onMore?: () => void | Promise<void>;
  label?: string;
  /** How many primary records the file is for. */
  count?: number;
  primary?: boolean;
  disabled?: boolean;
}

export function GenerateFileMenu({
  domain, prepare, onMore, label = "Generate file", count = DEFAULT_COUNT,
  primary = false, disabled = false,
}: Props) {
  const { notify, goHistory, openFiles } = useAppActions();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const holder = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);

  const items = () => [...(list.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])];
  const close = (refocus: boolean) => {
    setOpen(false);
    if (refocus) trigger.current?.focus();
  };

  // Opened, the first format takes focus, as a menu's first item does.
  useEffect(() => {
    if (open) items()[0]?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (!holder.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  // Up, Down, Home and End move between the items; Escape closes and goes
  // back to the button; Tab leaves the menu closed.
  const onMenuKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const all = items();
    const at = all.indexOf(document.activeElement as HTMLButtonElement);
    const go = (index: number) => {
      event.preventDefault();
      all[(index + all.length) % all.length]?.focus();
    };
    if (event.key === "ArrowDown") go(at + 1);
    else if (event.key === "ArrowUp") go(at - 1);
    else if (event.key === "Home") go(0);
    else if (event.key === "End") go(all.length - 1);
    else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    } else if (event.key === "Tab") setOpen(false);
  };
  const onBlur = (event: React.FocusEvent<HTMLDivElement>) => {
    if (open && !holder.current?.contains(event.relatedTarget as Node | null)) setOpen(false);
  };

  // A failure is said by the app, not here: the pattern may be saved by now
  // and this menu gone from a list that hides saved patterns.
  const write = async (format: FileFormat) => {
    close(true);
    setBusy(true);
    try {
      const pattern = await prepare();
      const extra = await fileRequest(pattern, domain, format, count);
      const run = await api.exportDownload(format.format, domain, format.file, extra);
      notify({
        message: `Created ${format.label} file${run ? ` · Run ${run}` : ""}`,
        action: run ? { label: "View in History", run: () => goHistory(run) } : undefined,
      });
    } catch (e) {
      notify({
        tone: "alarm",
        message: `Couldn't create ${format.label} file: ${
          e instanceof ApiError || e instanceof Error ? failure(e) : String(e)}`,
      });
    } finally {
      setBusy(false);
    }
  };

  const more = async () => {
    close(true);
    try {
      if (onMore) await onMore();
      else openFiles(await prepare());
    } catch (e) {
      notify({ tone: "alarm", message: `Couldn't open more options: ${failure(e)}` });
    }
  };

  return (
    <div className="filemenu" ref={holder} onBlur={onBlur}>
      <button
        ref={trigger}
        type="button"
        className={`btn btn--${primary ? "primary" : "quiet"}${busy ? " btn--busy" : ""}`}
        aria-haspopup="menu"
        aria-expanded={open}
        // Busy, it stays focusable, so focus is not lost while the file is made.
        aria-disabled={busy || undefined}
        disabled={disabled}
        onClick={() => {
          if (!busy) setOpen(!open);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" && !busy && !open) {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        {busy && <span className="btn__spinner" aria-hidden="true" />}
        {label}
        <span className="filemenu__caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div
          ref={list}
          className="filemenu__list"
          role="menu"
          aria-label={label}
          onKeyDown={onMenuKey}
          // Safari does not focus a button it clicks: without this the menu
          // loses focus on mousedown and closes before the click lands.
          onMouseDown={(event) => event.preventDefault()}
        >
          {fileFormats(domain).map((format) => (
            <button
              key={format.format}
              type="button"
              role="menuitem"
              tabIndex={-1}
              className="filemenu__item"
              onClick={() => void write(format)}
            >
              {format.label}
            </button>
          ))}
          <div className="filemenu__rule" role="separator" />
          <button
            type="button"
            role="menuitem"
            tabIndex={-1}
            className="filemenu__item"
            onClick={() => void more()}
          >
            More options…
          </button>
        </div>
      )}
    </div>
  );
}
