/**
 * Load to database, in one step.
 *
 * The dialog says what will happen -- which database, how much of it, which
 * columns the pattern sets -- and Load does all of it: the pattern goes onto
 * the plan, the plan is rebuilt, the rows are loaded and then validated. The
 * result names the run and can delete what it loaded. Generate › Database
 * stays for anyone who wants the plan step by step.
 *
 * Opening the dialog saves nothing and builds no plan: the columns are read
 * from the plan there is, and where none has been built they are worked out
 * when the pattern is loaded. Cancel leaves the saved patterns as they were.
 * Only Load writes rows.
 */

import { useEffect, useId, useRef, useState } from "react";
import { Banner, Button, formatCount, formatPercent } from "./primitives";
import {
  api,
  type CleanupResult,
  type GenerateResult,
  type PlanPayload,
  type ValidateResult,
} from "../lib/api";
import { useAppActions } from "../lib/appActions";
import { useDatabaseGeneration } from "../lib/databaseGeneration";
import { failure } from "./GenerateFileMenu";
import "./actions.css";

/** The reference date ages and tenures are measured from. Never the clock:
 *  a pattern found today has to mean the same thing when it is replayed. */
export const PATTERN_AS_OF = "2026-06-30";

/** Why Load to database is unavailable, said on the disabled button. */
export const LOADS_OFF = "Load to database is off for this installation";

/** Why Load to database waits: there is no target database to load into. */
export const NO_TARGET = "No target database set up";

export interface LoadColumn {
  name: string;
  from: string;
  title?: string;
}

export interface LoadPreview {
  columns: LoadColumn[];
  /** Elements of the pattern no column takes, by name. */
  unmapped?: string[];
  /** No plan is built yet: the columns are worked out when it is loaded. */
  pending?: boolean;
}

/** Said once on a screen whose loads have no target database to go to. */
export function NoTargetNote() {
  const dbLoad = useDatabaseGeneration();
  const { targetReady } = useAppActions();
  if (!dbLoad || targetReady) return null;
  return <p className="panel__hint loadbtn__why">{NO_TARGET}</p>;
}

interface Props {
  /** What is loaded: the pattern's name. */
  name: string;
  /** The pattern's share of the population, offered as how much it sets.
   *  Left out where what is loaded sets every row. */
  share?: number | null;
  /** How much is loaded, where that is stated rather than the plan's rows. */
  amount?: string;
  /** The columns the pattern sets, read without changing anything. */
  preview?: () => Promise<LoadPreview>;
  /** Puts the pattern on the plan; how many column rules it set. */
  apply: (asCohort: boolean) => Promise<number>;
  onClose: () => void;
}

type Stage = "confirm" | "working" | "done";

/** Buttons, links and fields a keyboard reaches inside an element. */
const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function LoadToDatabaseDialog({ name, share, amount, preview, apply, onClose }: Props) {
  const { target, goHistory, databaseChanged } = useAppActions();
  const heading = useId();
  const box = useRef<HTMLDialogElement>(null);
  const title = useRef<HTMLHeadingElement>(null);
  const [stage, setStage] = useState<Stage>("confirm");
  const [step, setStep] = useState("");
  const [columns, setColumns] = useState<LoadPreview | null>(null);
  const [columnsFailed, setColumnsFailed] = useState(false);
  const [plan, setPlan] = useState<PlanPayload | null>(null);
  const [asCohort, setAsCohort] = useState(Boolean(share));
  const [loaded, setLoaded] = useState<GenerateResult | null>(null);
  const [validated, setValidated] = useState<ValidateResult | null>(null);
  const [deleted, setDeleted] = useState<CleanupResult | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const deleteGroup = useRef<HTMLSpanElement>(null);
  const deleteActions = useRef<HTMLDivElement>(null);
  const returnToDelete = useRef(false);
  const deletedNote = useRef<HTMLParagraphElement>(null);

  // A modal dialog: the page behind is inert, focus starts on the title, and
  // goes back to what opened the dialog when it closes.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const dialog = box.current;
    if (dialog && !dialog.open) {
      try {
        dialog.showModal();
      } catch {
        dialog.setAttribute("open", "");
      }
    }
    title.current?.focus();
    return () => {
      if (dialog?.open) dialog.close();
      if (opener && opener.isConnected) opener.focus();
    };
  }, []);

  // Read once, when the dialog opens. Nothing is written: the columns are
  // resolved without applying them, and the plan is read as it stands.
  useEffect(() => {
    let live = true;
    (async () => {
      if (preview) {
        try {
          const found = await preview();
          if (live) setColumns(found);
        } catch (e) {
          if (live) {
            setColumnsFailed(true);
            setError(failure(e));
          }
        }
      }
      try {
        const current = await api.getPlan();
        if (live) setPlan(current);
      } catch {
        // Unknown until loaded: said as such beside Rows.
      }
    })();
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The result's title takes focus once loaded: the Load button is gone.
  useEffect(() => {
    if (stage === "done") title.current?.focus();
  }, [stage]);

  // Deleted: what was deleted takes focus, the confirmation being gone.
  useEffect(() => {
    if (deleted) deletedNote.current?.focus();
  }, [deleted]);

  // The delete confirmation takes focus; Cancel gives it back to its button.
  useEffect(() => {
    if (confirmDelete) {
      const buttons = deleteGroup.current?.querySelectorAll("button");
      buttons?.[buttons.length - 1]?.focus();
    } else if (returnToDelete.current) {
      returnToDelete.current = false;
      const again = [...(deleteActions.current?.querySelectorAll("button") ?? [])]
        .find((b) => b.textContent?.trim() === "Delete loaded rows");
      again?.focus();
    }
  }, [confirmDelete]);

  const busyNow = stage === "working" || deleting;

  // Escape is Cancel, or Close once loaded; never while it works.
  const onCancel = (event: React.SyntheticEvent) => {
    event.preventDefault();
    if (!busyNow) onClose();
  };

  // Closed by the browser itself (a second Escape): closed here too, or
  // opened again while it works.
  const onClosed = () => {
    // Reopened since the close was queued (React's StrictMode remounts the
    // effect in development): not closed.
    if (box.current?.open) return;
    if (busyNow) box.current?.showModal();
    else onClose();
  };

  // Tab and Shift+Tab stay inside the dialog, where a browser lets them out.
  const onKeyDown = (event: React.KeyboardEvent<HTMLDialogElement>) => {
    if (event.key !== "Tab" || !box.current) return;
    const inside = [...box.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (inside.length === 0) return;
    const first = inside[0];
    const last = inside[inside.length - 1];
    const now = document.activeElement;
    if (event.shiftKey && (now === first || now === title.current)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && now === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const load = async () => {
    setStage("working");
    setError(null);
    let wrote = false;
    try {
      setStep("Applying pattern…");
      const set = await apply(asCohort);
      if (set === 0) {
        throw new Error(
          "No element of this pattern maps to a column in the target database.",
        );
      }
      databaseChanged();
      setStep("Rebuilding plan…");
      const rebuilt = await api.buildPlan();
      setPlan(rebuilt);
      setStep("Loading…");
      setLoaded(await api.generate());
      wrote = true;
      databaseChanged();
      setStep("Validating…");
      setValidated(await api.validate());
      setStage("done");
    } catch (e) {
      setError(failure(e));
      setStage(wrote ? "done" : "confirm");
      // The failure is said under the title: focus goes there, not to the body.
      requestAnimationFrame(() => title.current?.focus());
    } finally {
      databaseChanged();
    }
  };

  const remove = async () => {
    setDeleting(true);
    setError(null);
    try {
      setDeleted(await api.cleanup());
      setConfirmDelete(false);
      databaseChanged();
    } catch (e) {
      setError(failure(e));
      // Said under the title (an alert): focus goes there, the Delete
      // pressed having been disabled while it worked.
      requestAnimationFrame(() => title.current?.focus());
    } finally {
      setDeleting(false);
    }
  };

  const rows = plan?.built && !plan.stale ? plan.totalRows ?? null : null;
  const run = loaded?.request?.request ?? null;
  const noColumns = columns !== null && !columns.pending && columns.columns.length === 0;

  return (
    <dialog
      ref={box}
      className="dialog"
      aria-labelledby={heading}
      onCancel={onCancel}
      onClose={onClosed}
      onKeyDown={onKeyDown}
    >
      <h3 id={heading} ref={title} tabIndex={-1} className="dialog__title">
        {stage === "done" ? "Loaded to database" : "Load to database"}
      </h3>
      {error && <Banner tone="alarm">{error}</Banner>}

      {stage !== "done" && (
        <>
          <dl className="dialog__facts">
            <div>
              <dt>Pattern</dt>
              <dd>{name}</dd>
            </div>
            <div>
              <dt>Target</dt>
              <dd>{target}</dd>
            </div>
            <div>
              <dt>Rows</dt>
              <dd className="num">
                {amount ??
                  (rows === null ? "Calculated when loaded" : `${formatCount(rows)} in the plan`)}
              </dd>
            </div>
            {share !== undefined && (
            <div>
              <dt>Applies to</dt>
              <dd>
                <span className="dialog__choice" role="radiogroup" aria-label="Applies to">
                  <label>
                    <input
                      type="radio"
                      name={`${heading}-share`}
                      checked={asCohort}
                      disabled={!share || stage === "working"}
                      onChange={() => setAsCohort(true)}
                    />
                    {share ? `${formatPercent(share)} of rows` : "Pattern share"}
                  </label>
                  <label>
                    <input
                      type="radio"
                      name={`${heading}-share`}
                      checked={!asCohort}
                      disabled={stage === "working"}
                      onChange={() => setAsCohort(false)}
                    />
                    All rows
                  </label>
                </span>
              </dd>
            </div>
            )}
          </dl>

          {preview && (
            <section className="dialog__columns">
              <h4>Columns this pattern sets</h4>
              {columnsFailed ? (
                <p className="panel__hint">Couldn't read columns.</p>
              ) : columns === null ? (
                <p className="panel__hint" role="status">Reading columns…</p>
              ) : columns.pending ? (
                <p className="panel__hint">Calculated when loaded</p>
              ) : noColumns ? (
                <p className="panel__hint">
                  No element of this pattern maps to a column in the target database.
                </p>
              ) : (
                <table className="panel__table">
                  <thead>
                    <tr>
                      <th>Column</th>
                      <th>From</th>
                    </tr>
                  </thead>
                  <tbody>
                    {columns.columns.map((c) => (
                      <tr key={`${c.title ?? c.name}-${c.from}`}>
                        <td title={c.title}>{c.name}</td>
                        <td>{c.from}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {columns?.unmapped && columns.unmapped.length > 0 && (
                <p className="panel__hint">Not mapped: {columns.unmapped.join(", ")}</p>
              )}
            </section>
          )}

          {stage === "working" && <p className="dialog__step" role="status">{step}</p>}

          <div className="dialog__actions">
            <Button
              variant="primary"
              onClick={() => void load()}
              busy={stage === "working"}
              disabled={noColumns || (preview !== undefined && columns === null && !columnsFailed)}
            >
              Load
            </Button>
            <Button onClick={onClose} disabled={stage === "working"}>
              Cancel
            </Button>
          </div>
        </>
      )}

      {stage === "done" && loaded && (
        <>
          <p className="dialog__result" role="status">
            {`Loaded ${formatCount(loaded.rowsWritten)} rows into ${target}`}
            {run ? ` · Run ${run}` : ""}
          </p>
          {validated && (
            <p className={validated.passed ? "dialog__passed" : "dialog__failed"}>
              {validated.passed
                ? `All ${formatCount(validated.checks)} checks passed.`
                : `${formatCount(validated.failed)} of ${formatCount(validated.checks)} checks failed.`}
            </p>
          )}
          {deleted && (
            <p className="dialog__result" role="status" tabIndex={-1} ref={deletedNote}>
              {`Deleted ${formatCount(deleted.total)} loaded rows.`}
            </p>
          )}
          <div className="dialog__actions" ref={deleteActions}>
            <Button variant="primary" onClick={() => { onClose(); goHistory(run); }}>
              View in History
            </Button>
            {confirmDelete ? (
              <span
                className="dialog__confirm"
                role="group"
                aria-label="Delete loaded rows"
                ref={deleteGroup}
              >
                <span>{`Delete ${formatCount(loaded.rowsWritten)} rows?`}</span>
                <Button variant="danger" onClick={() => void remove()} busy={deleting}>
                  Delete
                </Button>
                <Button
                  onClick={() => {
                    returnToDelete.current = true;
                    setConfirmDelete(false);
                  }}
                  disabled={deleting}
                >
                  Cancel
                </Button>
              </span>
            ) : (
              <Button
                variant="danger"
                onClick={() => setConfirmDelete(true)}
                disabled={deleted !== null}
              >
                Delete loaded rows
              </Button>
            )}
            <Button onClick={onClose} disabled={deleting}>
              Close
            </Button>
          </div>
        </>
      )}
    </dialog>
  );
}

/** The Load to database button: the dialog where loads are on and a target is
 *  set up, the reason (as its title) where not. The screen says once that no
 *  target is set up (NoTargetNote), not every button. */
export function LoadToDatabaseButton({
  label = "Load to database", primary = false, disabled = false, ...dialog
}: Omit<Props, "onClose"> & { label?: string; primary?: boolean; disabled?: boolean }) {
  const dbLoad = useDatabaseGeneration();
  const { targetReady } = useAppActions();
  const [open, setOpen] = useState(false);
  const why = !dbLoad ? LOADS_OFF : !targetReady ? NO_TARGET : undefined;
  return (
    <>
      <Button
        variant={primary ? "primary" : "quiet"}
        onClick={() => setOpen(true)}
        disabled={why !== undefined || disabled}
        title={why}
      >
        {label}
      </Button>
      {open && <LoadToDatabaseDialog {...dialog} onClose={() => setOpen(false)} />}
    </>
  );
}
