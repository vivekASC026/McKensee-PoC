/**
 * Source setup: day 0 at a new source.
 *
 * The tabs are the work in order. Sources names the database and reads it;
 * Landscape says what the read found; Hierarchy is where the person decides
 * what the records are, what they are called and which levels group them;
 * Tables lists the source tables those choices need. Criteria says which
 * rows the extract keeps; Structure lists the tables it fills, each column
 * with its privacy class; Approval submits the plan and approves it; Build
 * writes the pack; Load creates its tables in the SDM database and fills
 * them from the source, again on demand. The tool proposes, at low
 * confidence, and the person decides.
 *
 * Every edit answers with the whole plan, its tables derived again, and the
 * screen shows that answer as it stands: the table list always matches the
 * hierarchy on screen. An answer to a request that is no longer the latest
 * (another source chosen, a later edit made) is dropped. Controls stay
 * focusable while a request runs (aria-disabled), focus returns to the
 * control that made the edit, and what it changed is announced.
 *
 * Nothing here shows a value from the source, only names of tables and
 * columns, counts, and the engine's own labels.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Banner, Button, Chip, Empty, Field, formatCount, formatPercent } from "../components/primitives";
import {
  ApiError,
  api,
  type AccelAlternative,
  type AccelApproval,
  type AccelCriterion,
  type AccelDenormColumn,
  type AccelDenormTable,
  type AccelEdit,
  type AccelFeature,
  type AccelFiles,
  type AccelGrainLink,
  type AccelJob,
  type AccelJobResult,
  type AccelLevel,
  type AccelLoadStatus,
  type AccelCheck,
  type AccelStoreTable,
  type AccelPlan,
  type AccelPrimaryJoin,
  type AccelPrivacy,
  type AccelRole,
  type AccelSource,
  type AccelSummary,
  type AccelTable,
  type AccelView,
} from "../lib/api";
import "./SourceSetup.css";

export type SourceTab =
  | "sources"
  | "landscape"
  | "hierarchy"
  | "tables"
  | "criteria"
  | "structure"
  | "approval"
  | "build"
  | "load";

export const SOURCE_TABS: [SourceTab, string][] = [
  ["sources", "Sources"],
  ["landscape", "Landscape"],
  ["hierarchy", "Hierarchy"],
  ["tables", "Tables"],
  ["criteria", "Criteria"],
  ["structure", "Structure"],
  ["approval", "Approval"],
  ["build", "Build"],
  ["load", "Load"],
];

// The tabs that show the proposal: each needs one.
const PLAN_TABS: SourceTab[] = [
  "hierarchy", "tables", "criteria", "structure", "approval", "build", "load",
];

const ROLES: AccelRole[] = ["primary", "child"];

// What a level's reason says about the level before it, as the engine
// writes it (sdg/accelerator/hierarchy.py NEST_FLAG, UNKNOWN_FLAG, ORDER_FLAG).
const NEST_FLAG = "does not nest under ";
const UNKNOWN_FLAG = "not known to nest under ";
const ORDER_FLAG = "placed by its count, nesting not known against ";

const POLL_MS = 1000;
// Polls that fail in a row before the screen says it lost contact; it keeps
// trying, each wait longer, up to the longest.
const POLL_TROUBLE = 3;
const POLL_LONGEST_MS = 15_000;

// The screen's own words for a login that can write: the engine's message
// names a command-line flag, and this screen has a button instead.
const WRITE_LOGIN_SAID = "This login can change data in the source. SDM only reads from it.";

// The source last chosen, so coming back opens on it. Browser storage can be
// blocked or empty; then nothing is chosen.
const CHOSEN = "sdm.source-setup.source";
function recalled(): string | null {
  try {
    return localStorage.getItem(CHOSEN);
  } catch {
    return null;
  }
}
function keepChoice(name: string) {
  try {
    localStorage.setItem(CHOSEN, name);
  } catch {
    /* not kept; nothing depends on it */
  }
}

function describe(error: unknown): string {
  const said = error instanceof Error ? error.message : String(error);
  return said.replace(/^restart_lost:\s*/, "");
}

function capitalized(words: string): string {
  return words ? words[0].toUpperCase() + words.slice(1) : words;
}

/** What the person calls a role's records, or the neutral words. */
function roleWords(plan: AccelPlan | null, role: AccelRole): string {
  const many = plan?.record_kinds[role]?.name.many ?? "";
  if (many) return capitalized(many);
  return role === "primary" ? "Primary records" : "Child records";
}

function byOrder(levels: AccelLevel[]): AccelLevel[] {
  return [...levels].sort((a, b) => a.order - b.order);
}

/** A stable element id for a control, so focus can return to it after an edit. */
function controlId(what: string, role: AccelRole, id = ""): string {
  return `src-${what}-${role}-${id}`.replace(/[^A-Za-z0-9_-]/g, "_");
}

function tablesSaid(view: AccelView): string {
  const count = view.plan?.tables.length ?? 0;
  return `Tables updated: ${count} ${count === 1 ? "table" : "tables"}.`;
}

/** A level's reason, with what it says about nesting taken out to be shown apart. */
function splitReason(reason: string): { text: string; notNested: string | null; unknown: string | null } {
  let notNested: string | null = null;
  let unknown: string | null = null;
  const kept: string[] = [];
  for (const part of reason.split("; ")) {
    if (part.startsWith(NEST_FLAG)) notNested = part.slice(NEST_FLAG.length);
    else if (part.startsWith(UNKNOWN_FLAG)) unknown = part.slice(UNKNOWN_FLAG.length);
    else if (part.startsWith(ORDER_FLAG)) unknown = unknown ?? part.slice(ORDER_FLAG.length);
    else kept.push(part);
  }
  return { text: kept.join("; "), notNested, unknown };
}

type Pending = "read" | "propose" | "measure" | "load";

// What the button says to go on with a login that can write.
const GO_ON: Record<Pending, string> = {
  read: "Read with this login",
  propose: "Propose with this login",
  measure: "Measure with this login",
  load: "Load with this login",
};

/** What a job is doing, by its label (sdg/api/routers/accelerator.py). */
function jobWords(label: string): string {
  if (label.startsWith("Propose")) return "Proposing";
  if (label.startsWith("Measure")) return "Measuring";
  if (label.startsWith("Build")) return "Building";
  if (label.startsWith("Load")) return "Loading";
  return "Reading";
}

/** Where focus goes once an edit is answered (`failed`: where it goes when
 *  the edit is refused, the control used by default), what is announced, and
 *  what is done once it is made. */
export interface EditAfter {
  focus: string;
  failed?: string;
  says: (view: AccelView) => string;
  done?: () => void;
}

/** The same, for submit, approve and withdraw. */
interface ActAfter {
  focus: string;
  failed?: string;
  says: (approval: AccelApproval) => string;
}

export function SourceSetup({ tab, onTab }: { tab: SourceTab; onTab: (tab: SourceTab) => void }) {
  const [list, setList] = useState<AccelSource[] | null>(null);
  const [chosen, setChosen] = useState<string | null>(recalled);
  const [job, setJob] = useState<AccelJob | null>(null);
  const [lostContact, setLostContact] = useState(false);
  const [summary, setSummary] = useState<AccelSummary | null>(null);
  const [view, setView] = useState<AccelView | null>(null);
  const [loading, setLoading] = useState(false);
  // Why the chosen source's saved read and proposal could not be fetched.
  const [loadFailed, setLoadFailed] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A login that can write was refused: what to do again, as it was asked,
  // with the person's go-ahead.
  const [loginRefused, setLoginRefused] = useState<{ then: Pending; force: boolean } | null>(null);
  const [adding, setAdding] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const [role, setRole] = useState<AccelRole>("primary");
  const [refocus, setRefocus] = useState<string | null>(null);
  const [announced, setAnnounced] = useState("");
  // Where the plan stands in approval, read from the store; and the built
  // files, with why they cannot be listed when they cannot.
  const [approval, setApproval] = useState<AccelApproval | null>(null);
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const [files, setFiles] = useState<AccelFiles | null>(null);
  const [filesSaid, setFilesSaid] = useState<{ tone: "caution" | "alarm"; text: string } | null>(
    null,
  );
  // What the last build of the chosen source handed back, and why the last
  // one failed or was refused: both said in the Build panel.
  const [built, setBuilt] = useState<AccelJobResult | null>(null);
  const [buildError, setBuildError] = useState<string | null>(null);
  // The Load tab: the store's state for the built pack (or why it cannot be
  // read), what the last load handed back, and why one failed or was refused.
  const [loadStatus, setLoadStatus] = useState<AccelLoadStatus | null>(null);
  const [loadSaid, setLoadSaid] = useState<{ kind: string | null; text: string } | null>(null);
  const [loaded, setLoaded] = useState<AccelJobResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // The open tab, for work that finishes after it was changed.
  const tabNow = useRef<SourceTab>(tab);

  // Every load and edit takes a number; an answer whose number is no longer
  // the latest, or whose source is no longer the one chosen, is dropped.
  const sequence = useRef(0);
  const chosenNow = useRef<string | null>(chosen);
  const failures = useRef(0);
  const approvalSequence = useRef(0);
  const filesSequence = useRef(0);
  const loadSequence = useRef(0);

  // A source never read, or not proposed yet, has no approval to show:
  // the tabs say so themselves.
  const loadApproval = useCallback(async (name: string) => {
    const mine = ++approvalSequence.current;
    try {
      const found = await api.accelApproval(name);
      if (mine !== approvalSequence.current || name !== chosenNow.current) return;
      setApproval(found);
      setApprovalError(null);
    } catch (e) {
      if (mine !== approvalSequence.current || name !== chosenNow.current) return;
      const none = e instanceof ApiError && (e.refusal === "not_read" || e.refusal === "no_proposal");
      // Nothing to approve: nothing is shown. Any other failure keeps what
      // was shown last, with the reason beside it.
      if (none) setApproval(null);
      setApprovalError(none ? null : describe(e));
    }
  }, []);

  // A pack not named yet, or no folder for packs set, is said as what to do.
  const loadFiles = useCallback(async (name: string) => {
    const mine = ++filesSequence.current;
    try {
      const found = await api.accelFiles(name);
      if (mine !== filesSequence.current || name !== chosenNow.current) return;
      setFiles(found);
      setFilesSaid(null);
    } catch (e) {
      if (mine !== filesSequence.current || name !== chosenNow.current) return;
      setFiles(null);
      const guidance = e instanceof ApiError && (e.refusal === "packs_dir" || e.refusal === "pack_id");
      setFilesSaid({ tone: guidance ? "caution" : "alarm", text: describe(e) });
    }
  }, []);

  // Not built yet is said as what to do; anything else, with Try again.
  const readLoad = useCallback(async (name: string) => {
    const mine = ++loadSequence.current;
    try {
      const found = await api.accelLoadStatus(name);
      if (mine !== loadSequence.current || name !== chosenNow.current) return;
      setLoadStatus(found);
      setLoadSaid(null);
    } catch (e) {
      if (mine !== loadSequence.current || name !== chosenNow.current) return;
      setLoadStatus(null);
      setLoadSaid({ kind: e instanceof ApiError ? (e.refusal ?? null) : null, text: describe(e) });
    }
  }, []);

  const loadSources = useCallback(async () => {
    try {
      const found = (await api.accelSources()).sources;
      setList(found);
      const name = chosenNow.current;
      if (name && !found.some((s) => s.name === name)) {
        // Added for an earlier session, or no longer named in .env.
        setChosen(null);
        try {
          localStorage.removeItem(CHOSEN);
        } catch {
          /* nothing kept to remove */
        }
      }
    } catch (e) {
      setError(describe(e));
    }
  }, []);

  const loadSource = useCallback(async (name: string) => {
    const mine = ++sequence.current;
    setLoading(true);
    try {
      const [read, held] = await Promise.all([api.accelLandscape(name), api.accelProposal(name)]);
      if (mine !== sequence.current || name !== chosenNow.current) return;
      setSummary(read.summary);
      setView(held);
      setLoadFailed(null);
      if (held.plan) void loadApproval(name);
    } catch (e) {
      if (mine !== sequence.current || name !== chosenNow.current) return;
      // What was shown stays; the failure is said with a way to try again.
      setLoadFailed(describe(e));
    } finally {
      if (mine === sequence.current) setLoading(false);
    }
  }, [loadApproval]);

  // On arrival: the sources, and a read still in flight to attach to.
  useEffect(() => {
    void loadSources();
    api
      .accelLatestJob()
      .then((latest) => {
        if (!("id" in latest)) return;
        // In flight: attached to. Ended while the screen was away: its outcome
        // is said, for the source it was about.
        if (latest.state === "running" || latest.source === chosenNow.current) setJob(latest);
      })
      .catch((e: unknown) => setError(describe(e)));
  }, [loadSources]);

  useEffect(() => {
    chosenNow.current = chosen;
    setSummary(null);
    setView(null);
    setLoadFailed(null);
    setEditError(null);
    setApproval(null);
    setApprovalError(null);
    setFiles(null);
    setFilesSaid(null);
    setBuilt(null);
    setBuildError(null);
    setLoadStatus(null);
    setLoadSaid(null);
    setLoaded(null);
    setLoadError(null);
    if (chosen) void loadSource(chosen);
  }, [chosen, loadSource]);

  // Where the plan stands is read again whenever Approval or Build is opened
  // (it changes when someone approves elsewhere), and when the page is shown
  // again; the first load reads it already.
  useEffect(() => {
    const before = tabNow.current;
    tabNow.current = tab;
    if (before !== tab && (tab === "approval" || tab === "build") && chosenNow.current) {
      void loadApproval(chosenNow.current);
    }
  }, [tab, loadApproval]);
  useEffect(() => {
    const shown = () => {
      const name = chosenNow.current;
      if (document.visibilityState === "visible" && name &&
          (tabNow.current === "approval" || tabNow.current === "build")) {
        void loadApproval(name);
      }
    };
    document.addEventListener("visibilitychange", shown);
    return () => document.removeEventListener("visibilitychange", shown);
  }, [loadApproval]);

  // The built files are listed whenever Build is opened, and the store's
  // tables read whenever Load is.
  useEffect(() => {
    if (tab === "build" && chosen) void loadFiles(chosen);
    if (tab === "load" && chosen) void readLoad(chosen);
  }, [tab, chosen, loadFiles, readLoad]);

  // Polled while it runs, waiting longer after each failed poll; the job is
  // never given up on for a failed poll, only when the server no longer
  // knows it. Once it ends, what it changed is read again.
  // The same words said twice are announced twice: the region is cleared
  // first, and the words set once that has been rendered.
  const announceTimer = useRef<number | undefined>(undefined);
  const announce = useCallback((words: string) => {
    setAnnounced("");
    window.clearTimeout(announceTimer.current);
    announceTimer.current = window.setTimeout(() => setAnnounced(words), 50);
  }, []);
  useEffect(() => () => window.clearTimeout(announceTimer.current), []);

  const settled = useRef<string | null>(null);
  useEffect(() => {
    if (!job) return;
    if (job.state === "running") {
      const wait = failures.current
        ? Math.min(POLL_MS * 2 ** failures.current, POLL_LONGEST_MS)
        : POLL_MS;
      const next = window.setTimeout(() => {
        api
          .accelJob(job.id)
          .then((polled) => {
            failures.current = 0;
            setLostContact(false);
            setJob(polled);
          })
          .catch((e: unknown) => {
            if (e instanceof ApiError && e.status === 404) {
              failures.current = 0;
              setLostContact(false);
              setError(describe(e));
              setJob(null);
              // What the lost job may have changed is read again.
              void loadSources();
              const name = chosenNow.current;
              if (name) {
                void loadSource(name);
                void loadApproval(name);
                void loadFiles(name);
                void readLoad(name);
              }
              announce("Lost track of the job: the server restarted.");
              return;
            }
            failures.current += 1;
            if (failures.current >= POLL_TROUBLE) setLostContact(true);
            setJob({ ...job });
          });
      }, wait);
      return () => window.clearTimeout(next);
    }
    if (settled.current === job.id) return;
    settled.current = job.id;
    const source = job.source ?? job.result?.source ?? null;
    const building = job.label.startsWith("Build");
    const measuring = job.label.startsWith("Measure");
    const loadingJob = job.label.startsWith("Load");
    const here = Boolean(source) && source === chosenNow.current;
    if (job.state === "failed") {
      const said = job.error ?? `${jobWords(job.label)} failed.`;
      if (building && here) {
        // Said in the Build panel, where Build pack is the way to try again.
        setBuildError(said);
        announce(`The pack was not built. ${said}`);
      } else if (loadingJob && here) {
        // Said in the Load panel, where the load button tries again.
        setLoadError(said);
        announce(`Nothing was loaded. ${said}`);
      } else {
        // A failure of another source's job is said as that source's.
        setError(source && !here ? `${source}: ${said}` : said);
        if (here) announce(`${jobWords(job.label)} failed.`);
      }
    }
    void loadSources();
    if (source && here) {
      void loadSource(source);
      if (building) {
        const result = job.state === "done" ? job.result : null;
        setBuilt(result);
        void loadFiles(source);
        if (result) {
          announce(result.placed
            ? `The ${result.pack_id ?? ""} pack is built and in place.`
            : "SDM would not start on the pack, so it was not put in place.");
          if (tabNow.current === "build") setRefocus(BUILD_RESULT);
        }
      }
      if (loadingJob) {
        const result = job.state === "done" ? job.result : null;
        setLoaded(result);
        void readLoad(source);
        if (result) {
          const failedChecks = (result.checks ?? []).filter((c) => !c.passed).length;
          announce(result.stopped
            ? "The load was stopped; the tables after it keep their rows."
            : result.failed
              ? `${result.failed} ${result.failed === 1 ? "table was" : "tables were"} not loaded.`
              : failedChecks
                ? `Loaded. ${failedChecks} ${failedChecks === 1 ? "check needs" : "checks need"} a look.`
                : "Loaded, and every check passed.");
          if (tabNow.current === "load") setRefocus(LOAD_RESULT);
        }
      }
      if (measuring && job.state === "done") {
        const count = job.result?.measured ?? 0;
        announce(`Shares measured for ${count} ${count === 1 ? "criterion" : "criteria"}.`);
      }
    }
  }, [job, loadSources, loadSource, loadFiles, readLoad, loadApproval, announce]);

  // Focus returns to the control that made the edit once its answer is shown.
  // When that control is gone (its card swapped away), focus goes to the
  // heading of the panel the edit was made in.
  useEffect(() => {
    if (!refocus || editing !== null) return;
    const panel = [LEVELS_HEADING, HIERARCHY_HEADING, CRITERIA_HEADING, STRUCTURE_HEADING,
      APPROVAL_HEADING, BUILD_HEADING, LOAD_HEADING]
      .map((id) => document.getElementById(id))
      .find((found) => found !== null);
    (document.getElementById(refocus) ?? panel)?.focus();
    setRefocus(null);
  }, [refocus, editing, view]);


  const running = job?.state === "running";
  // Only the chosen source's work waits on its job; another source can be
  // looked at and changed meanwhile.
  const runningHere = running && job?.source === chosen;
  const busy = editing !== null || loading;

  // A read or proposal is never started while an edit is in flight; the
  // buttons that start one say so (aria-disabled), so no click is lost unseen.
  async function start(then: Pending | "build", allowWriteLogin: boolean, force = false) {
    if (!chosen || editing !== null) return;
    setError(null);
    setLoginRefused(null);
    if (then === "build") {
      setBuilt(null);
      setBuildError(null);
    }
    if (then === "load") {
      setLoaded(null);
      setLoadError(null);
    }
    try {
      setJob(
        then === "read"
          ? await api.accelRead(chosen, allowWriteLogin)
          : then === "propose"
            ? await api.accelPropose(chosen, force, allowWriteLogin)
            : then === "measure"
              ? await api.accelMeasure(chosen, allowWriteLogin)
              : then === "load"
                ? await api.accelLoad(chosen, allowWriteLogin)
                : await api.accelBuild(chosen),
      );
      announce(`${jobWords(then === "build" ? "Build" : then === "load" ? "Load"
        : then === "measure" ? "Measure" : then === "propose" ? "Propose" : "Read")} ${chosen} started.`);
    } catch (e) {
      if (then === "build") {
        // Said in the Build panel; where the plan stands may have changed.
        setBuildError(describe(e));
        announce("The pack was not built.");
        void loadApproval(chosen);
      } else if (e instanceof ApiError && e.refusal === "write_login") {
        setLoginRefused({ then, force });
        announce(`${WRITE_LOGIN_SAID} ${GO_ON[then]} goes on.`);
        setRefocus(GO_ON_ID);
      } else if (then === "load") {
        // Said in the Load panel; the store's tables may have changed.
        setLoadError(describe(e));
        announce("Nothing was loaded.");
        void readLoad(chosen);
      } else {
        setError(describe(e));
      }
    }
  }

  async function add() {
    setAddError(null);
    try {
      const found = (await api.accelAddSource(adding)).sources;
      setList(found);
      const name = adding.trim().toUpperCase();
      if (found.some((s) => s.name === name)) choose(name);
      setAdding("");
    } catch (e) {
      setAddError(describe(e));
    }
  }

  function choose(name: string) {
    if (busy) return;
    chosenNow.current = name;
    setChosen(name);
    keepChoice(name);
    setError(null);
    setLoginRefused(null);
  }

  // Whether the edit was sent: not while another change or a load is in flight.
  function edit(key: string, change: AccelEdit, after: EditAfter): boolean {
    if (!chosen || busy) return false;
    void send(chosen, key, change, after);
    return true;
  }

  async function send(name: string, key: string, change: AccelEdit, after: EditAfter) {
    const mine = ++sequence.current;
    let refused = false;
    setEditing(key);
    setEditError(null);
    try {
      const next = await api.accelEdit(name, change);
      // A later load shows the plan; where it stands in approval is read
      // again all the same, since this change may have dropped an approval.
      if (mine !== sequence.current || name !== chosenNow.current) {
        if (name === chosenNow.current) void loadApproval(name);
        return;
      }
      setView(next);
      announce(after.says(next));
      after.done?.();
      // Any change drops an approval given to the plan as it was, and a
      // build made from it no longer stands for the plan.
      setBuilt(null);
      void loadApproval(name);
    } catch (e) {
      refused = true;
      if (mine !== sequence.current || name !== chosenNow.current) {
        if (name === chosenNow.current) void loadApproval(name);
        return;
      }
      setEditError(describe(e));
      announce("The change was not made.");
      if (e instanceof ApiError && (e.refusal === "approval" || e.refusal === "changed")) {
        void loadApproval(name);
      }
    } finally {
      setEditing((held) => (held === key ? null : held));
      setRefocus(refused ? (after.failed ?? after.focus) : after.focus);
    }
  }

  // Submit, approve or withdraw: the answer is where the plan stands now.
  async function act(key: string, call: (name: string) => Promise<AccelApproval>, after: ActAfter) {
    if (!chosen || busy) return;
    const name = chosen;
    const mine = ++sequence.current;
    let refused = false;
    setEditing(key);
    setEditError(null);
    try {
      const next = await call(name);
      if (mine !== sequence.current || name !== chosenNow.current) {
        if (name === chosenNow.current) void loadApproval(name);
        return;
      }
      // An older read of the approval, still in flight, is dropped.
      approvalSequence.current += 1;
      setApproval(next);
      setApprovalError(null);
      if (next.status !== "approved") setBuilt(null);
      announce(after.says(next));
    } catch (e) {
      refused = true;
      if (mine !== sequence.current || name !== chosenNow.current) {
        if (name === chosenNow.current) void loadApproval(name);
        return;
      }
      setEditError(describe(e));
      announce("The change was not made.");
      void loadApproval(name);
    } finally {
      setEditing((held) => (held === key ? null : held));
      setRefocus(refused ? (after.failed ?? after.focus) : after.focus);
    }
  }

  const plan = view?.plan ?? null;
  const source = list?.find((s) => s.name === chosen) ?? null;
  // Nothing fetched yet for the chosen source: being fetched, or it failed
  // (said above) -- never shown as a source that was not read.
  const unsettled = chosen !== null && !summary && !view && (loading || loadFailed !== null);
  const fetching = loading ? (
    <p className="panel__hint">Reading what was saved for {chosen}…</p>
  ) : null;

  return (
    <div className="srcsetup">
      <div className="sr-only" role="status" aria-live="polite">
        {announced}
      </div>
      {error && <Banner tone="alarm">{error}</Banner>}
      {loginRefused && (
        <Banner tone="caution">
          <div className="srcsetup__said">
            <p>
              {WRITE_LOGIN_SAID}
              {loginRefused.force && " Proposing again replaces your choices."}
            </p>
            <div className="srcsetup__inline">
              <Button
                variant="primary"
                id={GO_ON_ID}
                disabled={editing !== null}
                keepFocus
                onClick={() => void start(loginRefused.then, true, loginRefused.force)}
              >
                {GO_ON[loginRefused.then]}
              </Button>
              <span>A login that can only read is the safer choice.</span>
            </div>
          </div>
        </Banner>
      )}

      {tab === "sources" && (
        <SourcesTab
          list={list}
          chosen={chosen}
          source={source}
          job={job}
          lostContact={lostContact}
          summary={summary}
          busy={busy}
          editing={editing !== null}
          adding={adding}
          addError={addError}
          onAdding={setAdding}
          onAdd={() => void add()}
          onChoose={choose}
          onRead={() => void start("read", false)}
          onHierarchy={() => onTab("hierarchy")}
        />
      )}
      {loadFailed && chosen && (
        <Banner tone="alarm">
          <div className="srcsetup__said">
            <p>What was saved for {chosen} could not be fetched. {loadFailed}</p>
            <div className="srcsetup__inline">
              <Button onClick={() => void loadSource(chosen)}>Try again</Button>
            </div>
          </div>
        </Banner>
      )}
      {tab === "landscape" &&
        (summary ? (
          <LandscapeTab source={chosen ?? ""} summary={summary} />
        ) : unsettled ? (
          fetching
        ) : (
          <NotRead chosen={chosen} onSources={() => onTab("sources")} />
        ))}
      {PLAN_TABS.includes(tab) && !plan && unsettled && fetching}
      {PLAN_TABS.includes(tab) && !plan && !unsettled && (
        summary && chosen ? (
          <Empty
            title="No proposal yet"
            action={
              <Button variant="primary" busy={runningHere}
                disabled={running || editing !== null} keepFocus
                onClick={() => void start("propose", false)}>
                Propose
              </Button>
            }
          >
            Propose record kinds, levels and the tables they need from the read of {chosen}.
          </Empty>
        ) : (
          <NotRead chosen={chosen} onSources={() => onTab("sources")} />
        )
      )}
      {PLAN_TABS.includes(tab) && plan && view?.stale && (
        <Banner tone="caution">
          <div className="srcsetup__said">
            <p id={STALE_REASON}>{view.stale_reason}</p>
            <div className="srcsetup__inline">
              <Button variant="primary" busy={runningHere}
                disabled={running || editing !== null} keepFocus
                onClick={() => void start("propose", false, true)}>
                Propose again
              </Button>
            </div>
          </div>
        </Banner>
      )}
      {tab === "hierarchy" && plan && (
        <HierarchyTab
          plan={plan}
          view={view}
          role={plan.record_kinds[role] ? role : "primary"}
          onRole={setRole}
          editing={editing}
          editError={editError}
          locked={Boolean(view?.stale) || runningHere || busy}
          onEdit={edit}
        />
      )}
      {tab === "tables" && plan && <TablesTab plan={plan} />}
      {tab === "criteria" && plan && (
        <CriteriaTab
          plan={plan}
          estimated={view?.estimated ?? []}
          job={job}
          lostContact={lostContact}
          running={running}
          runningHere={runningHere}
          editing={editing}
          editError={editError}
          locked={Boolean(view?.stale) || runningHere || busy}
          stale={Boolean(view?.stale)}
          onEdit={edit}
          onMeasure={() => void start("measure", false)}
        />
      )}
      {tab === "structure" && plan && (
        <StructureTab
          plan={plan}
          view={view}
          editing={editing}
          editError={editError}
          locked={Boolean(view?.stale) || runningHere || busy}
          stale={Boolean(view?.stale)}
          onEdit={edit}
        />
      )}
      {tab === "approval" && plan && (
        <ApprovalTab
          plan={plan}
          approval={approval}
          approvalError={approvalError}
          editing={editing}
          editError={editError}
          locked={Boolean(view?.stale) || runningHere || busy}
          stale={Boolean(view?.stale)}
          busy={runningHere || busy}
          onEdit={edit}
          onAct={(key, call, after) => void act(key, call, after)}
          onRetry={() => chosen && void loadApproval(chosen)}
        />
      )}
      {tab === "build" && plan && chosen && (
        <BuildTab
          key={chosen}
          source={chosen}
          approval={approval}
          approvalError={approvalError}
          files={files}
          filesSaid={filesSaid}
          built={built}
          buildError={buildError}
          job={job}
          lostContact={lostContact}
          running={running}
          runningHere={runningHere}
          locked={Boolean(view?.stale) || runningHere || busy}
          stale={Boolean(view?.stale)}
          onSaid={announce}
          onBuild={() => void start("build", false)}
          onApproval={() => onTab("approval")}
          onRetry={() => {
            void loadApproval(chosen);
            void loadFiles(chosen);
          }}
          onLoad={() => onTab("load")}
        />
      )}
      {tab === "load" && plan && chosen && (
        <LoadTab
          key={chosen}
          source={chosen}
          plan={plan}
          status={loadStatus}
          statusSaid={loadSaid}
          loaded={loaded}
          loadError={loadError}
          job={job}
          lostContact={lostContact}
          running={running}
          runningHere={runningHere}
          editing={editing !== null}
          onSaid={announce}
          onStatus={(next) => {
            loadSequence.current += 1;
            setLoadStatus(next);
            setLoadSaid(null);
          }}
          onLoad={() => void start("load", false)}
          onBuild={() => onTab("build")}
          onRetry={() => void readLoad(chosen)}
        />
      )}
    </div>
  );
}

function NotRead({ chosen, onSources }: { chosen: string | null; onSources: () => void }) {
  return (
    <Empty
      title={chosen ? `${chosen} is not read yet` : "No source chosen"}
      action={
        <Button variant="primary" onClick={onSources}>
          Go to Sources
        </Button>
      }
    >
      Choose a source and read it under Sources.
    </Empty>
  );
}

/* ------------------------------------------------------------------ */
/* Sources                                                             */
/* ------------------------------------------------------------------ */

function SourcesTab({
  list, chosen, source, job, lostContact, summary, busy, editing, adding, addError, onAdding,
  onAdd, onChoose, onRead, onHierarchy,
}: {
  list: AccelSource[] | null;
  chosen: string | null;
  source: AccelSource | null;
  job: AccelJob | null;
  lostContact: boolean;
  summary: AccelSummary | null;
  busy: boolean;
  /** An edit is in flight: nothing is started until it is answered. */
  editing: boolean;
  adding: string;
  addError: string | null;
  onAdding: (name: string) => void;
  onAdd: () => void;
  onChoose: (name: string) => void;
  onRead: () => void;
  onHierarchy: () => void;
}) {
  const running = job?.state === "running";
  const runningHere = running && job?.source === chosen;
  const elsewhere = running && !runningHere;
  return (
    <section className="panel" aria-label="Sources">
      <header>
        <h2 className="panel__title">Sources</h2>
        <p className="panel__hint">
          Sources are set up by your administrator, each with its own server and login. A source
          is only read: every statement sent to it is a SELECT.
        </p>
      </header>

      {list === null ? (
        <p className="panel__hint">Loading sources…</p>
      ) : list.length === 0 ? (
        <p className="panel__hint">No source is set up yet. Add one by name below.</p>
      ) : (
        <div className="srcsources" role="radiogroup" aria-label="Source">
          {list.map((item) => (
            <label
              key={item.name}
              className={"srcsource" + (item.name === chosen ? " srcsource--on" : "")}
            >
              <input
                type="radio"
                name="source"
                checked={item.name === chosen}
                aria-disabled={busy || undefined}
                onChange={() => onChoose(item.name)}
              />
              <span className="srcsource__name mono">{item.name}</span>
              <span className="srcsource__state">
                {item.proposed ? (
                  <Chip tone="affirm" fill="outline">Proposal saved</Chip>
                ) : item.read ? (
                  <Chip fill="outline">Read</Chip>
                ) : (
                  <Chip fill="dashed">Not read</Chip>
                )}
              </span>
              <span className="srcsource__facts">
                {item.database && (
                  <span>
                    Database <span className="mono">{item.database}</span> on{" "}
                    <span className="mono">{item.server}</span>
                  </span>
                )}
                <span>Signs in with {item.login}</span>
                {item.origins.includes("added") && <span>Added this session</span>}
              </span>
              {item.problem && <span className="srcsource__problem">{item.problem}</span>}
            </label>
          ))}
        </div>
      )}

      <form
        className="srcsetup__add"
        onSubmit={(event) => {
          event.preventDefault();
          if (adding.trim()) onAdd();
        }}
      >
        <label className="srcsetup__addfield">
          <span className="field__label">Add a source by name</span>
          <input
            className="input mono"
            value={adding}
            maxLength={32}
            onChange={(event) => onAdding(event.target.value)}
          />
        </label>
        <Button type="submit" disabled={!adding.trim()}>
          Add
        </Button>
        <span className="panel__hint">
          For this session only. Its server and login are the ones your administrator set up; no
          password is typed here.
        </span>
      </form>
      {addError && <Banner tone="alarm">{addError}</Banner>}

      <div className="panel__actions">
        <Button
          variant="primary"
          onClick={onRead}
          busy={runningHere}
          disabled={!chosen || Boolean(source?.problem) || elsewhere || editing}
          keepFocus
        >
          Read source
        </Button>
        <span className="panel__hint">
          {!chosen
            ? "Choose a source first."
            : elsewhere
              ? `${job?.source ?? "Another source"} is being read; one source is read at a time.`
              : source?.proposed
                ? `Reads ${chosen} again. The saved proposal and its choices are kept; propose again under Hierarchy to use the new read.`
                : `Reads the tables, columns and keys of ${chosen}, profiles the tables that matter, then proposes its hierarchy.`}
        </span>
      </div>

      {job && running && <Progress job={job} lostContact={lostContact} />}

      {summary && chosen && !runningHere && (
        <section className="srcsetup__result" aria-label="Read result">
          <h3 className="srcsetup__subtitle">What the read of {chosen} found</h3>
          <Counts summary={summary} />
          <Hubs hubs={summary.hubs.slice(0, 5)} />
          <Skipped skipped={summary.skipped} shown={5} />
          <div>
            <Button variant="primary" onClick={onHierarchy}>
              Open hierarchy
            </Button>
          </div>
        </section>
      )}
    </section>
  );
}

function Progress({ job, lostContact }: { job: AccelJob; lostContact: boolean }) {
  const total = job.total ?? 0;
  return (
    <div className="srcprogress">
      <span className="srcprogress__label">
        {jobWords(job.label)}{" "}
        <span className="mono">{job.source ?? ""}</span>
      </span>
      <span>{job.note || "Starting"}</span>
      {total > 0 && (
        <progress className="srcprogress__bar" value={job.done} max={total} aria-label="Progress" />
      )}
      <span className="srcprogress__facts num">
        {total > 0 && `Step ${Math.min(job.done + 1, total)} of ${total} · `}
        {Math.round(job.elapsed_seconds)} s
      </span>
      {lostContact && (
        <span className="srcprogress__trouble" role="status">
          Lost contact with the server; retrying.
        </span>
      )}
    </div>
  );
}

function Counts({ summary }: { summary: AccelSummary }) {
  const counts: [string, number][] = [
    ["Tables", summary.tables],
    ["Columns", summary.columns],
    ["Keys declared", summary.declared_keys],
    ["Keys found", summary.inferred_keys],
    ["Tables profiled", summary.shortlisted],
  ];
  return (
    <dl className="srccounts">
      {counts.map(([label, value]) => (
        <div key={label} className="srccounts__item">
          <dt>{label}</dt>
          <dd className="num">{formatCount(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Hubs({ hubs }: { hubs: AccelSummary["hubs"] }) {
  if (!hubs.length) return null;
  return (
    <div>
      <h4 className="srcsetup__minor">Most referenced tables</h4>
      <div className="srcsetup__scroll">
        <table className="panel__table srcsetup__narrow">
          <thead>
            <tr>
              <th>Table</th>
              <th className="right">Referenced by</th>
              <th className="right">Rows</th>
            </tr>
          </thead>
          <tbody>
            {hubs.map((hub) => (
              <tr key={hub.table}>
                <td className="mono">{hub.table}</td>
                <td className="right num">{formatCount(hub.referenced_by)}</td>
                <td className="right num">{formatCount(hub.rows)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Skipped({ skipped, shown }: { skipped: AccelSummary["skipped"]; shown?: number }) {
  if (!skipped.length) {
    return <p className="panel__hint">Nothing was left out of the profile.</p>;
  }
  const listed = shown ? skipped.slice(0, shown) : skipped;
  return (
    <div>
      <h4 className="srcsetup__minor">Left out of the profile ({formatCount(skipped.length)})</h4>
      <ul className="srclist">
        {listed.map((item, index) => (
          <li key={`${item.table}-${item.analysis}-${index}`}>
            <span className="mono">{item.table}</span> {item.analysis.replace(/_/g, " ")}: {item.reason}
          </li>
        ))}
      </ul>
      {shown && skipped.length > shown && (
        <p className="panel__hint">And {formatCount(skipped.length - shown)} more, under Landscape.</p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Landscape                                                           */
/* ------------------------------------------------------------------ */

function LandscapeTab({ source, summary }: { source: string; summary: AccelSummary }) {
  const pairs = Object.entries(summary.date_pair_tables);
  const people = Object.entries(summary.person_like_tables);
  return (
    <section className="panel" aria-label="Landscape">
      <header>
        <h2 className="panel__title">Landscape of {source}</h2>
        <p className="panel__hint">
          What the last read found: structure and keys of every table, and a profile of the
          tables that matter.
          {summary.seconds != null && ` The read took ${Math.round(summary.seconds)} s.`}
          {summary.login_can_write && " The login used can change data in the source; only reads were sent."}
        </p>
      </header>
      <Counts summary={summary} />
      <Hubs hubs={summary.hubs} />
      <div className="srcsetup__columns">
        <div>
          <h4 className="srcsetup__minor">Code tables ({formatCount(summary.code_tables.length)})</h4>
          {summary.code_tables.length ? (
            <ul className="srclist mono">
              {summary.code_tables.map((name) => (
                <li key={name}>{name}</li>
              ))}
            </ul>
          ) : (
            <p className="panel__hint">None found.</p>
          )}
        </div>
        <div>
          <h4 className="srcsetup__minor">Tables with start and end dates ({formatCount(pairs.length)})</h4>
          {pairs.length ? (
            <ul className="srclist">
              {pairs.map(([table, spans]) => (
                <li key={table}>
                  <span className="mono">{table}</span>:{" "}
                  {spans.map((span) => `${span.start} to ${span.end}`).join(", ")}
                </li>
              ))}
            </ul>
          ) : (
            <p className="panel__hint">None found.</p>
          )}
        </div>
        <div>
          <h4 className="srcsetup__minor">Tables holding people ({formatCount(people.length)})</h4>
          {people.length ? (
            <ul className="srclist">
              {people.map(([table, columns]) => (
                <li key={table}>
                  <span className="mono">{table}</span>: {columns.join(", ")}
                </li>
              ))}
            </ul>
          ) : (
            <p className="panel__hint">None found.</p>
          )}
        </div>
      </div>
      <Skipped skipped={summary.skipped} />
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Hierarchy                                                           */
/* ------------------------------------------------------------------ */

/** Make an edit; false when it was not sent (another change is in flight). */
type OnEdit = (key: string, change: AccelEdit, after: EditAfter) => boolean;

const ROLE_PANEL = "src-rolepanel";
const HIERARCHY_HEADING = "src-hierarchy-heading";
const LEVELS_HEADING = "src-levels-heading";
const roleTabId = (role: AccelRole) => `src-roletab-${role}`;

function HierarchyTab({
  plan, view, role, onRole, editing, editError, locked, onEdit,
}: {
  plan: AccelPlan;
  view: AccelView | null;
  role: AccelRole;
  onRole: (role: AccelRole) => void;
  editing: string | null;
  editError: string | null;
  locked: boolean;
  onEdit: OnEdit;
}) {
  const roles = ROLES.filter((r) => plan.record_kinds[r]);
  const levels = byOrder(plan.hierarchies[role]?.levels ?? []);
  const selected = levels.filter((lv) => lv.selected);
  const unselected = levels.filter((lv) => !lv.selected);
  const alternatives = view?.alternatives[role] ?? [];

  // Left and Right, Home and End move between the record kinds, as tabs do.
  const move = (event: React.KeyboardEvent<HTMLElement>) => {
    const at = roles.indexOf(role);
    const to =
      event.key === "ArrowRight" ? (at + 1) % roles.length
        : event.key === "ArrowLeft" ? (at - 1 + roles.length) % roles.length
          : event.key === "Home" ? 0
            : event.key === "End" ? roles.length - 1
              : -1;
    if (to < 0) return;
    event.preventDefault();
    onRole(roles[to]);
    requestAnimationFrame(() => document.getElementById(roleTabId(roles[to]))?.focus());
  };

  return (
    <section className="panel" aria-label="Hierarchy">
      <header>
        <h2 className="panel__title" id={HIERARCHY_HEADING} tabIndex={-1}>Record kinds</h2>
        <p className="panel__hint">
          Proposed from the source's structure alone, at low confidence: confirm each record
          table or choose another, and name its records.
        </p>
      </header>
      {editError && <Banner tone="alarm">{editError}</Banner>}
      <div className="srckinds">
        {ROLES.map((r) => (
          <RecordKind key={r} plan={plan} role={r} locked={locked} editing={editing}
            onEdit={onEdit} />
        ))}
      </div>

      {roles.length > 0 && (
        <>
          <div className="tabs srcsetup__roles" role="tablist" aria-label="Record kind"
            onKeyDown={move}>
            {roles.map((r) => (
              <button
                key={r}
                id={roleTabId(r)}
                role="tab"
                aria-selected={r === role}
                aria-controls={ROLE_PANEL}
                tabIndex={r === role ? 0 : -1}
                className={"tabs__tab" + (r === role ? " tabs__tab--on" : "")}
                onClick={() => onRole(r)}
              >
                {roleWords(plan, r)}
              </button>
            ))}
          </div>

          <div id={ROLE_PANEL} role="tabpanel" aria-labelledby={roleTabId(role)}>
            <h3 className="srcsetup__subtitle" id={LEVELS_HEADING} tabIndex={-1}>
              Levels of {roleWords(plan, role).toLowerCase()}
            </h3>
            <p className="panel__hint">
              Selected levels group the records, broadest first. A walk level's groups are walked
              one by one; a narrowing level splits them further.
            </p>
            {selected.length === 0 && <p className="panel__hint">No level is selected.</p>}
            <ol className="srclevels">
              {selected.map((level, index) => (
                <LevelCard
                  key={level.id}
                  role={role}
                  level={level}
                  position={index + 1}
                  first={index === 0}
                  last={index === selected.length - 1}
                  selectedIds={selected.map((lv) => lv.id)}
                  unselected={unselected}
                  alternatives={alternatives}
                  locked={locked}
                  busy={editing === level.id}
                  onEdit={onEdit}
                />
              ))}
            </ol>
            {unselected.length > 0 && (
              <>
                <h4 className="srcsetup__minor">Not selected ({formatCount(unselected.length)})</h4>
                <ul className="srclevels srclevels--off">
                  {unselected.map((level) => (
                    <LevelCard
                      key={level.id}
                      role={role}
                      level={level}
                      position={null}
                      first
                      last
                      selectedIds={selected.map((lv) => lv.id)}
                      unselected={[]}
                      alternatives={[]}
                      locked={locked}
                      busy={editing === level.id}
                      onEdit={onEdit}
                    />
                  ))}
                </ul>
              </>
            )}

            <Features role={role} features={plan.hierarchies[role]?.features ?? []}
              locked={locked} onEdit={onEdit} />
          </div>
        </>
      )}

      {plan.notes.length > 0 && (
        <details className="srcnotes">
          <summary>Notes from the proposal ({formatCount(plan.notes.length)})</summary>
          <ul className="srclist">
            {plan.notes.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function RecordKind({
  plan, role, locked, editing, onEdit,
}: {
  plan: AccelPlan;
  role: AccelRole;
  locked: boolean;
  editing: string | null;
  onEdit: OnEdit;
}) {
  const kind = plan.record_kinds[role];
  const candidates = plan.candidates[role] ?? [];
  const heading = roleWords(plan, role);
  const [one, setOne] = useState(kind?.name.one ?? "");
  const [many, setMany] = useState(kind?.name.many ?? "");
  // What the server holds is what the fields start from, after every answer.
  useEffect(() => {
    setOne(kind?.name.one ?? "");
    setMany(kind?.name.many ?? "");
  }, [kind?.name.one, kind?.name.many, kind?.table]);

  if (!kind) {
    return (
      <section className="srckind" aria-label={heading}>
        <h3 className="srckind__title">{heading}</h3>
        <p className="panel__hint">
          {role === "primary"
            ? "None proposed: no table of the source holds rows."
            : "None proposed: no table keys to the primary record table with more than one row each."}
        </p>
        {candidates.length > 0 && (
          <ChooseRecord role={role} current="" candidates={candidates} locked={locked}
            onEdit={onEdit} />
        )}
      </section>
    );
  }
  const reasons = candidates.find((c) => c.table === kind.table)?.reasons ?? [];
  const nameKey = `name-${role}`;
  const nameId = controlId("name", role);
  const canName = !locked && Boolean(one.trim()) && Boolean(many.trim());
  return (
    <section className="srckind" aria-label={heading}>
      <div className="srckind__head">
        <h3 className="srckind__title">{heading}</h3>
        <Chip tone="caution" fill="dashed" title="Proposed from structure alone; confirm or choose another">
          Low confidence
        </Chip>
      </div>
      <ChooseRecord role={role} current={kind.table} candidates={candidates} locked={locked}
        onEdit={onEdit} />
      <div className="srckind__facts">
        <Field label="Lines">
          <span className="mono">{kind.lines_table ?? "None"}</span>
        </Field>
        <Field label="Key">
          <span className="mono">{kind.key_columns.join(", ") || "None"}</span>
        </Field>
      </div>
      {reasons.length > 0 && <p className="srckind__why">Proposed because: {reasons.join("; ")}.</p>}
      <form
        className="srckind__name"
        onSubmit={(event) => {
          event.preventDefault();
          if (!canName) return;
          onEdit(nameKey, { path: "name", body: { role, one, many } }, {
            focus: nameId,
            says: (view) => `Name saved: ${view.plan?.record_kinds[role]?.name.many ?? many}.`,
          });
        }}
      >
        <label className="srckind__namefield">
          <span className="field__label">Name of one</span>
          <input className="input" value={one} maxLength={60} aria-disabled={locked || undefined}
            readOnly={locked} onChange={(event) => setOne(event.target.value)} />
        </label>
        <label className="srckind__namefield">
          <span className="field__label">Name of many</span>
          <input className="input" value={many} maxLength={60} aria-disabled={locked || undefined}
            readOnly={locked} onChange={(event) => setMany(event.target.value)} />
        </label>
        <Button type="submit" id={nameId} busy={editing === nameKey} disabled={!canName} keepFocus>
          Save name
        </Button>
      </form>
    </section>
  );
}

function ChooseRecord({
  role, current, candidates, locked, onEdit,
}: {
  role: AccelRole;
  current: string;
  candidates: AccelPlan["candidates"][AccelRole];
  locked: boolean;
  onEdit: OnEdit;
}) {
  const id = controlId("record", role);
  return (
    <label className="srckind__table">
      <span className="field__label">Record table</span>
      <select
        id={id}
        className="input mono"
        value={current}
        aria-disabled={locked || undefined}
        onChange={(event) => {
          if (locked) return;
          const table = event.target.value;
          onEdit(`record-${role}`, { path: "choose-record", body: { role, table } }, {
            focus: id,
            says: (view) => `Record table is now ${table}. ${tablesSaid(view)}`,
          });
        }}
      >
        {!current && <option value="">Choose a table</option>}
        {candidates.map((c) => (
          <option key={c.table} value={c.table}>
            {c.table} (score {c.score.toFixed(2)})
          </option>
        ))}
      </select>
    </label>
  );
}

function LevelCard({
  role, level, position, first, last, selectedIds, unselected, alternatives, locked, busy, onEdit,
}: {
  role: AccelRole;
  level: AccelLevel;
  position: number | null;
  first: boolean;
  last: boolean;
  selectedIds: string[];
  unselected: AccelLevel[];
  alternatives: AccelAlternative[];
  locked: boolean;
  busy: boolean;
  onEdit: OnEdit;
}) {
  const said = splitReason(level.reason);
  const pickId = controlId("pick", role, level.id);
  const nameId = controlId("level", role, level.id);
  const move = (by: number) => {
    if (locked) return;
    const ids = [...selectedIds];
    const at = ids.indexOf(level.id);
    [ids[at], ids[at + by]] = [ids[at + by], ids[at]];
    onEdit(level.id, { path: "reorder", body: { role, ids } }, {
      focus: controlId(by < 0 ? "up" : "down", role, level.id),
      says: () => `${level.column} moved to position ${at + by + 1}.`,
    });
  };
  // Swapped for: a level proposed and not selected, or a column not yet a
  // level; those on this level's own table first.
  const own = (table: string) => (table === level.table ? 0 : 1);
  const offered = [...unselected].sort((a, b) => own(a.table) - own(b.table));
  const others = [...alternatives].sort((a, b) => own(a.table) - own(b.table));
  const swapId = controlId("swap", role, level.id);
  return (
    <li className={"srclevel" + (level.selected ? "" : " srclevel--off")} data-level={level.id}
      aria-labelledby={nameId}>
      <div className="srclevel__head">
        {position !== null && <span className="srclevel__n num">{position}</span>}
        <span className="srclevel__name" id={nameId}>
          <span className="mono">{level.column}</span>
          <span className="srclevel__on"> on </span>
          <span className="mono">{level.table}</span>
        </span>
        <Chip fill="outline" tone={level.kind === "walk" ? "structural" : "neutral"}>
          {level.kind === "walk" ? "Walk level" : "Narrowing level"}
        </Chip>
        <label className="srclevel__pick">
          <input
            id={pickId}
            type="checkbox"
            checked={level.selected}
            aria-disabled={locked || undefined}
            onChange={(event) => {
              if (locked) return;
              const on = event.target.checked;
              onEdit(level.id, {
                path: "select-level",
                body: { role, level_id: level.id, selected: on },
              }, {
                focus: pickId,
                says: (view) => `${level.column} ${on ? "selected" : "deselected"}. ${tablesSaid(view)}`,
              });
            }}
          />
          Selected
        </label>
      </div>
      <dl className="srclevel__facts">
        <div>
          <dt>Values</dt>
          <dd className="num">{formatCount(level.distinct)}</dd>
        </div>
        <div>
          <dt>Records each</dt>
          <dd className="num">{formatCount(level.per_value)}</dd>
        </div>
        <div>
          <dt>Label</dt>
          <dd className="mono">{level.label_column ?? "None"}</dd>
        </div>
        <div>
          <dt>Confidence</dt>
          <dd className="num">{formatPercent(level.confidence)}</dd>
        </div>
      </dl>
      {said.text && <p className="srclevel__reason">{said.text}</p>}
      {(said.notNested || said.unknown) && (
        <div className="srclevel__notes">
          {said.notNested && (
            <Chip tone="caution" fill="outline">Does not nest under {said.notNested}</Chip>
          )}
          {said.unknown && <Chip fill="dashed">Nesting not known against {said.unknown}</Chip>}
        </div>
      )}
      {level.selected && (
        <div className="srclevel__actions">
          <Button id={controlId("up", role, level.id)} onClick={() => move(-1)}
            disabled={locked || first} busy={busy} keepFocus label={`Move ${level.column} up`}>
            Move up
          </Button>
          <Button id={controlId("down", role, level.id)} onClick={() => move(1)}
            disabled={locked || last} keepFocus label={`Move ${level.column} down`}>
            Move down
          </Button>
          {(offered.length > 0 || others.length > 0) && (
            <label className="srclevel__swap">
              <span className="field__label">Swap for</span>
              <select
                id={swapId}
                className="input"
                value=""
                aria-disabled={locked || undefined}
                onChange={(event) => {
                  if (locked || !event.target.value) return;
                  const [table, column] = JSON.parse(event.target.value) as [string, string];
                  onEdit(level.id, {
                    path: "swap",
                    body: { role, level_id: level.id, table, column },
                  }, {
                    focus: controlId("pick", role, `${table}.${column}`),
                    says: (view) => `${level.column} swapped for ${column}. ${tablesSaid(view)}`,
                  });
                }}
              >
                <option value="">Choose a level</option>
                {offered.length > 0 && (
                  <optgroup label="Proposed, not selected">
                    {offered.map((lv) => (
                      <option key={lv.id} value={JSON.stringify([lv.table, lv.column])}>
                        {lv.column} on {lv.table} ({formatCount(lv.distinct)} values)
                      </option>
                    ))}
                  </optgroup>
                )}
                {others.length > 0 && (
                  <optgroup label="Other columns">
                    {others.map((alt) => (
                      <option key={alt.id} value={JSON.stringify([alt.table, alt.column])}>
                        {alt.column} on {alt.table} ({formatCount(alt.distinct)} values)
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </label>
          )}
        </div>
      )}
    </li>
  );
}

function Features({
  role, features, locked, onEdit,
}: {
  role: AccelRole;
  features: AccelFeature[];
  locked: boolean;
  onEdit: OnEdit;
}) {
  return (
    <div className="srcfeatures">
      <h3 className="srcsetup__subtitle">Features</h3>
      <p className="panel__hint">What a pattern of these records is described by.</p>
      {features.length === 0 ? (
        <p className="panel__hint">None proposed.</p>
      ) : (
        <div className="srcsetup__scroll">
          <table className="panel__table">
            <thead>
              <tr>
                <th>Use</th>
                <th>Column</th>
                <th>Kind</th>
                <th className="right">Values</th>
                <th className="right">Empty</th>
                <th>Why</th>
              </tr>
            </thead>
            <tbody>
              {features.map((feature) => {
                const id = controlId("feature", role, feature.id);
                return (
                  <tr key={feature.id}>
                    <td>
                      <input
                        id={id}
                        type="checkbox"
                        aria-label={`Use ${feature.column}`}
                        checked={feature.selected}
                        aria-disabled={locked || undefined}
                        onChange={(event) => {
                          if (locked) return;
                          const on = event.target.checked;
                          onEdit(`feature-${feature.id}`, {
                            path: "select-feature",
                            body: { role, feature_id: feature.id, selected: on },
                          }, {
                            focus: id,
                            says: () => `${feature.column} ${on ? "used" : "not used"}.`,
                          });
                        }}
                      />
                    </td>
                    <td className="mono">{feature.column}</td>
                    <td>
                      {feature.derive
                        ? `${capitalized(feature.derive.unit)} since ${feature.derive.from}`
                        : feature.kind === "in"
                          ? "Set of values"
                          : "Range"}
                    </td>
                    <td className="right num">{formatCount(feature.distinct)}</td>
                    <td className="right num">{formatPercent(feature.null_rate)}</td>
                    <td>{feature.reason}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Tables                                                              */
/* ------------------------------------------------------------------ */

const SERVES_AS: Record<string, string> = {
  record: "Records",
  lines: "Lines",
  level: "Level",
  label: "Label",
  bridge: "Link to a level",
};

function TablesTab({ plan }: { plan: AccelPlan }) {
  const column = (levelId: string | null) => (levelId ? levelId.split(".").pop() : null);
  const serves = (table: AccelTable) =>
    table.serves.map((s) => {
      const what = SERVES_AS[s.as] ?? s.as;
      const level = column(s.level);
      return `${roleWords(plan, s.role)}: ${what}${level && s.as !== "record" ? ` ${level}` : ""}`;
    });
  return (
    <section className="panel" aria-label="Source tables">
      <header>
        <h2 className="panel__title">
          Source tables <span className="num">{formatCount(plan.tables.length)}</span>
        </h2>
        <p className="panel__hint">
          Derived from the record kinds and the selected levels, never typed: a table leaves when
          nothing selected needs it. Every change under Hierarchy realigns this list.
        </p>
      </header>
      <div className="srcsetup__scroll">
        <table className="panel__table srctables">
          <thead>
            <tr>
              <th>Table</th>
              <th>Serves</th>
              <th>Reached by</th>
            </tr>
          </thead>
          <tbody>
            {plan.tables.map((table) => (
              <tr key={table.table}>
                <td className="mono">{table.table}</td>
                <td>
                  <ul className="srclist">
                    {serves(table).map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                </td>
                <td>
                  {table.joins.length === 0 ? (
                    <span className="panel__hint">The record itself</span>
                  ) : (
                    <ul className="srclist">
                      {table.joins.map((join, index) => (
                        <li key={index}>
                          <span className="mono">
                            {join.from_table}.{join.from_columns.join(" + ")} → {join.to_table}.
                            {join.to_columns.join(" + ")}
                          </span>{" "}
                          <span className="panel__hint">
                            {join.kind}
                            {join.coverage != null && `, ${formatPercent(join.coverage)} found`}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Criteria                                                            */
/* ------------------------------------------------------------------ */

const CRITERIA_HEADING = "src-criteria-heading";
const STRUCTURE_HEADING = "src-structure-heading";
const APPROVAL_HEADING = "src-approval-heading";
const BUILD_HEADING = "src-build-heading";
// The words a locked control points to when the proposal is older than the read.
const STALE_REASON = "src-stale-reason";
// What a build's result is said in, focused when the build ends.
const BUILD_RESULT = "src-build-result";
const LOAD_HEADING = "src-load-heading";
const GO_ON_ID = "src-go-on";
const LOAD_RESULT = "src-load-result";

/** A control's reason for being locked, when it is the proposal's age. */
function staleWhy(stale: boolean): string | undefined {
  return stale ? STALE_REASON : undefined;
}

// How the notes of the last measure begin, as the engine writes them
// (sdg/accelerator/criteria.py NOT_MEASURED, SAMPLED).
const MEASURE_NOTES = ["Not measured", "Measured on a sample"];

// A window's months, at most (sdg/accelerator/criteria.py MONTHS_MAX).
const MONTHS_MAX = 600;

const CRITERION_KIND: Record<AccelCriterion["kind"], string> = {
  in_force: "In force",
  window: "Time window",
  values: "Values",
};

/** What a criterion keeps, in words: names of columns, and only the values
 *  the engine kept or the person typed. */
function keepsWords(criterion: AccelCriterion): string {
  const [first = ""] = criterion.columns;
  if (criterion.kind === "in_force") {
    return `In force today, by ${criterion.columns.filter(Boolean).join(" and ")}`;
  }
  if (criterion.kind === "window") return `The last ${criterion.months ?? 0} months of ${first}`;
  const listed = criterion.values.length ? criterion.values.join(", ") : "no values yet";
  return `${first} ${criterion.op === "in" ? "is one of" : "is none of"}: ${listed}`;
}

function CriteriaTab({
  plan, estimated, job, lostContact, running, runningHere, editing, editError, locked, stale,
  onEdit, onMeasure,
}: {
  plan: AccelPlan;
  estimated: string[];
  job: AccelJob | null;
  lostContact: boolean;
  running: boolean;
  runningHere: boolean;
  editing: string | null;
  editError: string | null;
  locked: boolean;
  stale: boolean;
  onEdit: OnEdit;
  onMeasure: () => void;
}) {
  const roles = ROLES.filter((r) => plan.record_kinds[r]);
  const sampled = new Set(estimated);
  const any = roles.some((r) => (plan.criteria?.[r] ?? []).length > 0);
  const measuring = runningHere && Boolean(job?.label.startsWith("Measure"));
  const notes = plan.notes.filter((note) => MEASURE_NOTES.some((start) => note.startsWith(start)));
  return (
    <section className="panel" aria-label="Criteria">
      <header>
        <h2 className="panel__title" id={CRITERIA_HEADING} tabIndex={-1}>Criteria</h2>
        <p className="panel__hint">
          Which rows the extract keeps. Each criterion is proposed from what the read found and
          applies only when selected; with none selected, the extract keeps every row. A share
          marked Estimated was counted on a sample of a large table.
        </p>
      </header>
      {editError && <Banner tone="alarm">{editError}</Banner>}
      {roles.map((role) => {
        const criteria = plan.criteria?.[role] ?? [];
        return (
          <section key={role} aria-label={roleWords(plan, role)}>
            <h3 className="srcsetup__subtitle">{roleWords(plan, role)}</h3>
            {criteria.length === 0 ? (
              <p className="panel__hint">None proposed: the extract keeps every row.</p>
            ) : (
              <ul className="srclevels">
                {criteria.map((criterion) => (
                  <CriterionCard key={criterion.id} criterion={criterion}
                    estimated={sampled.has(criterion.id)} locked={locked} stale={stale}
                    editing={editing} onEdit={onEdit} />
                ))}
              </ul>
            )}
          </section>
        );
      })}
      <div className="panel__actions">
        <Button variant="primary" id="src-measure" busy={measuring}
          disabled={running || locked || !any} keepFocus describedBy={staleWhy(stale)}
          onClick={onMeasure}>
          Measure shares
        </Button>
        <span className="panel__hint">
          {running && !runningHere
            ? `${job?.source ?? "Another source"} is being worked on; one job runs at a time.`
            : "Counts, in the source, the share of rows each criterion keeps: one count each. " +
              "A large table is counted on a sample, and its share is marked estimated."}
        </span>
      </div>
      {measuring && job && <Progress job={job} lostContact={lostContact} />}
      {notes.length > 0 && (
        <details className="srcnotes">
          <summary>Notes from the last measure ({formatCount(notes.length)})</summary>
          <ul className="srclist">
            {notes.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function CriterionCard({
  criterion, estimated, locked, stale, editing, onEdit,
}: {
  criterion: AccelCriterion;
  estimated: boolean;
  locked: boolean;
  stale: boolean;
  editing: string | null;
  onEdit: OnEdit;
}) {
  const joined = criterion.values.join("\n");
  const [values, setValues] = useState(joined);
  const [op, setOp] = useState(criterion.op);
  const [months, setMonths] = useState(String(criterion.months ?? ""));
  // What the server holds is what the fields start from, after every answer.
  useEffect(() => {
    setValues(joined);
    setOp(criterion.op);
    setMonths(String(criterion.months ?? ""));
  }, [joined, criterion.op, criterion.months]);

  const column = criterion.columns[0];
  const words = keepsWords(criterion);
  const key = `criterion-${criterion.id}`;
  const nameId = controlId("criterion", criterion.role, criterion.id);
  const pickId = controlId("criterion-pick", criterion.role, criterion.id);
  const saveId = controlId("criterion-save", criterion.role, criterion.id);
  const whyId = controlId("criterion-why", criterion.role, criterion.id);
  const empty = criterion.kind === "values" && criterion.values.length === 0;
  // A values criterion is selected only once it has values.
  const pickOff = locked || (empty && !criterion.selected);
  const pickWhy = [empty ? whyId : "", staleWhy(stale) ?? ""].filter(Boolean).join(" ") || undefined;
  const typed = values.split("\n").map((v) => v.trim()).filter(Boolean);
  const wanted = Number(months);
  const canSave =
    !locked &&
    (criterion.kind === "values"
      ? typed.length > 0 && (typed.join("\n") !== joined || op !== criterion.op)
      : criterion.kind === "window"
        ? Number.isInteger(wanted) && wanted >= 1 && wanted <= MONTHS_MAX &&
          wanted !== criterion.months
        : false);
  const save = () => {
    if (!canSave) return;
    onEdit(key, {
      path: "criteria/set",
      body: criterion.kind === "values"
        ? { id: criterion.id, values: typed, op }
        : { id: criterion.id, months: wanted },
    }, {
      focus: saveId,
      says: () => criterion.kind === "values" ? `Values for ${column} saved.` : `Months for ${column} saved.`,
    });
  };
  return (
    <li className="srclevel" data-criterion={criterion.id} aria-labelledby={nameId}>
      <div className="srclevel__head">
        <span className="srclevel__name" id={nameId}>{words}</span>
        <Chip fill="outline">{CRITERION_KIND[criterion.kind]}</Chip>
        <label className="srclevel__pick">
          <input
            id={pickId}
            type="checkbox"
            checked={criterion.selected}
            aria-label={`Selected: ${words}`}
            aria-disabled={pickOff || undefined}
            aria-describedby={pickWhy}
            onChange={(event) => {
              if (pickOff) return;
              const on = event.target.checked;
              onEdit(key, { path: "criteria/select", body: { id: criterion.id, selected: on } }, {
                focus: pickId,
                says: () => `${words}: ${on ? "selected" : "deselected"}.`,
              });
            }}
          />
          Selected
        </label>
      </div>
      <dl className="srclevel__facts">
        <div>
          <dt>Table</dt>
          <dd className="mono">{criterion.table}</dd>
        </div>
        <div>
          <dt>Share kept</dt>
          <dd className="num">
            {criterion.share_kept == null ? "Not measured" : formatPercent(criterion.share_kept, 1)}
            {estimated && criterion.share_kept != null && (
              <>
                {" "}
                <Chip tone="caution" fill="dashed" title="Counted on a sample of the table">
                  Estimated
                </Chip>
              </>
            )}
          </dd>
        </div>
      </dl>
      {criterion.reason && <p className="srclevel__reason">{criterion.reason}</p>}
      {empty && (
        <p className="panel__hint" id={whyId}>Type the values to keep before selecting it.</p>
      )}
      {criterion.kind !== "in_force" && (
        <form
          className="srccriterion__edit"
          onSubmit={(event) => {
            event.preventDefault();
            save();
          }}
        >
          {criterion.kind === "values" ? (
            <>
              <label className="srccriterion__values">
                <span className="field__label">Values, one per line</span>
                <textarea className="input mono" rows={3} value={values}
                  aria-label={`Values of ${column} to keep, one per line`}
                  aria-describedby={staleWhy(stale)}
                  aria-disabled={locked || undefined} readOnly={locked}
                  onChange={(event) => setValues(event.target.value)} />
              </label>
              <label className="srckind__table">
                <span className="field__label">Keep rows whose value is</span>
                <select className="input" value={op}
                  aria-label={`Keep rows whose ${column} is`}
                  aria-describedby={staleWhy(stale)}
                  aria-disabled={locked || undefined}
                  onChange={(event) => {
                    if (!locked) setOp(event.target.value === "not_in" ? "not_in" : "in");
                  }}>
                  <option value="in">One of these</option>
                  <option value="not_in">None of these</option>
                </select>
              </label>
            </>
          ) : (
            <label className="srckind__table">
              <span className="field__label">Months</span>
              <input className="input num" type="number" min={1} max={MONTHS_MAX} value={months}
                aria-label={`Months of ${column} to keep`}
                aria-describedby={staleWhy(stale)}
                aria-disabled={locked || undefined} readOnly={locked}
                onChange={(event) => setMonths(event.target.value)} />
            </label>
          )}
          <Button type="submit" id={saveId} busy={editing === key} disabled={!canSave} keepFocus
            describedBy={staleWhy(stale)}
            label={`${criterion.kind === "values" ? "Save values" : "Save months"} for ${column}`}>
            {criterion.kind === "values" ? "Save values" : "Save months"}
          </Button>
        </form>
      )}
    </li>
  );
}

/* ------------------------------------------------------------------ */
/* Structure                                                           */
/* ------------------------------------------------------------------ */

const SERVES_WORDS: Record<AccelDenormColumn["serves"], string> = {
  key: "Record key",
  lines_key: "Line key",
  level_key: "Level key",
  level_label: "Level label",
  feature: "Feature",
  derive_input: "Input of a derived feature",
  extra: "Added by you",
};

const PRIVACY_WORDS: Record<AccelPrivacy, string> = {
  kept: "Kept",
  withheld: "Withheld",
  surrogate: "Surrogate",
};
const PRIVACY: AccelPrivacy[] = ["kept", "withheld", "surrogate"];
const JOIN_SELECT = "src-primary-join";
const JOIN_SAVE = "src-primary-join-save";

// Columns proposed withheld only for what their name or values suggest, which
// the person may overrule: an added column, a level's label, a feature.
const WORD_SERVES = ["extra", "level_label", "feature"];

/** Keeping this column needs the person's word that it holds nothing
 *  personal: a key or level proposed as a surrogate, or an added column, a
 *  label or a feature proposed withheld because it may say who a row is
 *  about (and not locked). */
function needsWord(column: AccelDenormColumn): boolean {
  if (column.locked || column.confirmed_not_personal) return false;
  return column.proposed === "surrogate" ||
    (WORD_SERVES.includes(column.serves) && column.proposed === "withheld");
}

function joinWords(join: AccelPrimaryJoin): string {
  return `${join.from_table}.${join.from_columns.join(" + ")} → ${join.to_table}.` +
    join.to_columns.join(" + ");
}

function StructureTab({
  plan, view, editing, editError, locked, stale, onEdit,
}: {
  plan: AccelPlan;
  view: AccelView | null;
  editing: string | null;
  editError: string | null;
  locked: boolean;
  stale: boolean;
  onEdit: OnEdit;
}) {
  const structure = plan.structure ?? [];
  const joins = view?.primary_joins ?? [];
  return (
    <section className="panel" aria-label="Structure">
      <header>
        <h2 className="panel__title" id={STRUCTURE_HEADING} tabIndex={-1}>Structure</h2>
        <p className="panel__hint">
          The tables the extract fills, one for each record kind. Every column has a privacy
          class: a kept column is copied, a withheld one is left empty, and a surrogate (a
          made-up key that replaces the real one, the same in both tables) stands in for a key.
          A locked column identifies a person and is never kept.
        </p>
      </header>
      {editError && <Banner tone="alarm">{editError}</Banner>}
      {joins.length > 1 && (
        <PrimaryJoin plan={plan} joins={joins} editing={editing} locked={locked} stale={stale}
          onEdit={onEdit} />
      )}
      {structure.length === 0 && (
        <p className="panel__hint">No table yet: choose the record kinds under Hierarchy.</p>
      )}
      {structure.map((table) => (
        <DenormTableView key={table.name} plan={plan} table={table}
          grain={view?.grain?.[table.role] ?? []} editing={editing} locked={locked}
          stale={stale} onEdit={onEdit} />
      ))}
    </section>
  );
}

function PrimaryJoin({
  plan, joins, editing, locked, stale, onEdit,
}: {
  plan: AccelPlan;
  joins: AccelPrimaryJoin[];
  editing: string | null;
  locked: boolean;
  stale: boolean;
  onEdit: OnEdit;
}) {
  const named = joins.findIndex((j) => j.named);
  const namedCount = joins.filter((j) => j.named).length;
  const [choice, setChoice] = useState(named >= 0 ? String(named) : "");
  useEffect(() => setChoice(named >= 0 ? String(named) : ""), [named, joins.length]);
  const chosen = choice === "" ? null : joins[Number(choice)] ?? null;
  const canUse = !locked && chosen !== null && Number(choice) !== named;
  const child = roleWords(plan, "child");
  const primary = roleWords(plan, "primary");
  return (
    <section className="srcjoin" aria-labelledby="src-join-heading">
      <h3 className="srcsetup__subtitle" id="src-join-heading">
        How {child.toLowerCase()} join {primary.toLowerCase()}
      </h3>
      {namedCount !== 1 && (
        <Banner tone="caution">
          The {child.toLowerCase()} table links to the {primary.toLowerCase()} table by{" "}
          {joins.length} keys. Choose the one it follows: the extract needs it to keep the{" "}
          {child.toLowerCase()} of the {primary.toLowerCase()} it keeps.
        </Banner>
      )}
      <form
        className="srcsetup__add"
        onSubmit={(event) => {
          event.preventDefault();
          if (!canUse || chosen === null) return;
          onEdit("primary-join", {
            path: "join",
            body: {
              role: "child",
              from_table: chosen.from_table,
              from_columns: chosen.from_columns,
              to_table: chosen.to_table,
              to_columns: chosen.to_columns,
            },
          }, {
            focus: JOIN_SAVE,
            failed: JOIN_SELECT,
            says: () => `${child} now join ${primary.toLowerCase()} by ${chosen.from_columns.join(" + ")}.`,
          });
        }}
      >
        <label className="srckind__table">
          <span className="field__label">Join</span>
          <select id={JOIN_SELECT} className="input mono" value={choice}
            aria-label={`Join from ${child.toLowerCase()} to ${primary.toLowerCase()}`}
            aria-describedby={staleWhy(stale)} aria-disabled={locked || undefined}
            onChange={(event) => {
              if (!locked) setChoice(event.target.value);
            }}>
            {named < 0 && <option value="">Choose a join</option>}
            {joins.map((join, index) => (
              <option key={joinWords(join)} value={String(index)}>
                {joinWords(join)}{join.named ? " (named)" : ""}
              </option>
            ))}
          </select>
        </label>
        <Button type="submit" id={JOIN_SAVE} busy={editing === "primary-join"} disabled={!canUse}
          keepFocus describedBy={staleWhy(stale)}>
          Use this join
        </Button>
      </form>
    </section>
  );
}

function DenormTableView({
  plan, table, grain, editing, locked, stale, onEdit,
}: {
  plan: AccelPlan;
  table: AccelDenormTable;
  grain: AccelGrainLink[];
  editing: string | null;
  locked: boolean;
  stale: boolean;
  onEdit: OnEdit;
}) {
  const one = table.grain === "lines" ? "line" : plan.record_kinds[table.role]?.name.one || "record";
  // A column is added from a table the record kind already reads.
  const reads = plan.tables
    .filter((t) => t.serves.some((s) => s.role === table.role))
    .map((t) => t.table);
  const [from, setFrom] = useState(reads[0] ?? "");
  const [column, setColumn] = useState("");
  const readsKey = reads.join(" ");
  useEffect(() => {
    if (!readsKey.split(" ").includes(from)) setFrom(readsKey.split(" ")[0] ?? "");
  }, [readsKey, from]);
  const headId = controlId("denorm", table.role);
  const addId = controlId("extra", table.role);
  const key = `extra-${table.role}`;
  const canAdd = !locked && Boolean(from) && Boolean(column.trim());
  return (
    <section className="srcdenorm" aria-labelledby={headId}>
      <h3 className="srcsetup__subtitle" id={headId}>
        <span className="mono">{table.name}</span>
      </h3>
      <p className="panel__hint">
        {roleWords(plan, table.role)}: one row per {one}.
      </p>
      {table.pick.map((pick) => (
        <p className="panel__hint" key={pick.table}>
          Takes the row of <span className="mono">{pick.table}</span> in force today, with the
          latest <span className="mono">{pick.start}</span>.
        </p>
      ))}
      {grain.map((link) => (
        <GrainLink key={link.table} role={table.role} one={one} link={link} editing={editing}
          locked={locked} stale={stale} onEdit={onEdit} />
      ))}
      <div className="srcsetup__scroll">
        <table className="panel__table srcstructure">
          <thead>
            <tr>
              <th>Column</th>
              <th>Privacy</th>
              <th>From</th>
              <th>Type</th>
              <th>Serves</th>
              <th>Why</th>
            </tr>
          </thead>
          <tbody>
            {table.columns.map((held) => (
              <StructureRow key={`${held.source_table}.${held.source_column}`}
                role={table.role} column={held} editing={editing} locked={locked} stale={stale}
                onEdit={onEdit} />
            ))}
          </tbody>
        </table>
      </div>
      <form
        className="srcsetup__add"
        onSubmit={(event) => {
          event.preventDefault();
          if (!canAdd) return;
          const named = column.trim();
          onEdit(key, { path: "structure/extra", body: { role: table.role, table: from, column: named } }, {
            focus: addId,
            says: () => `${named} added to ${table.name}.`,
            done: () => setColumn(""),
          });
        }}
      >
        <label className="srckind__table">
          <span className="field__label">Add a column from</span>
          <select className="input mono" value={from}
            aria-label={`Table to add a column to ${table.name} from`}
            aria-describedby={staleWhy(stale)} aria-disabled={locked || undefined}
            onChange={(event) => {
              if (!locked) setFrom(event.target.value);
            }}>
            {reads.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </select>
        </label>
        <label className="srcsetup__addfield">
          <span className="field__label">Column</span>
          <input className="input mono" value={column} maxLength={128}
            aria-label={`Column to add to ${table.name}`}
            aria-describedby={staleWhy(stale)}
            aria-disabled={locked || undefined} readOnly={locked}
            onChange={(event) => setColumn(event.target.value)} />
        </label>
        <Button type="submit" id={addId} busy={editing === key} disabled={!canAdd} keepFocus
          describedBy={staleWhy(stale)} label={`Add column to ${table.name}`}>
          Add column
        </Button>
      </form>
    </section>
  );
}

function GrainLink({
  role, one, link, editing, locked, stale, onEdit,
}: {
  role: AccelRole;
  one: string;
  link: AccelGrainLink;
  editing: string | null;
  locked: boolean;
  stale: boolean;
  onEdit: OnEdit;
}) {
  const per = link.levels.length ? link.levels.join(", ") : link.table;
  const id = controlId("grain", role, link.table);
  const key = `grain-${role}-${link.table}`;
  const accept = (on: boolean) => {
    if (locked) return;
    onEdit(key, { path: "structure/grain", body: { role, table: link.table, accept: on } }, {
      focus: id,
      says: () => on
        ? `Accepted: each ${one} may appear once per ${per}.`
        : `Taken back: the extract refuses ${link.table} again.`,
    });
  };
  return link.accepted ? (
    <div className="srcgrain" data-grain={link.table}>
      <span>
        Accepted: each {one} may appear once per <span className="mono">{per}</span>, through{" "}
        <span className="mono">{link.table}</span>.
      </span>
      <Button id={id} busy={editing === key} disabled={locked} keepFocus
        describedBy={staleWhy(stale)} label={`Take back the acceptance for ${link.table}`}
        onClick={() => accept(false)}>
        Take back
      </Button>
    </div>
  ) : (
    <Banner tone="caution">
      <div className="srcsetup__said" data-grain={link.table}>
        <p>
          <span className="mono">{per}</span> is reached through{" "}
          <span className="mono">{link.table}</span>, a link with no dates to pick one row by, so
          each {one} would appear once per row there. The extract refuses it until you accept
          that.
        </p>
        <div className="srcsetup__inline">
          <Button id={id} busy={editing === key} disabled={locked} keepFocus
            describedBy={staleWhy(stale)} onClick={() => accept(true)}>
            {`Accept: each ${one} may appear once per ${per}`}
          </Button>
        </div>
      </div>
    </Banner>
  );
}

function StructureRow({
  role, column, editing, locked, stale, onEdit,
}: {
  role: AccelRole;
  column: AccelDenormColumn;
  editing: string | null;
  locked: boolean;
  stale: boolean;
  onEdit: OnEdit;
}) {
  const where = `${column.source_table}.${column.source_column}`;
  const id = controlId("privacy", role, where);
  const whyId = controlId("privacy-why", role, where);
  const sureId = controlId("privacy-sure", role, where);
  const keepId = controlId("privacy-keep", role, where);
  const key = `privacy-${role}-${where}`;
  const off = locked || column.locked;
  // The choice is a draft until it is committed (on leaving the list, or
  // Enter), so stepping through it with the arrow keys sends nothing.
  const [draft, setDraft] = useState<AccelPrivacy>(column.privacy);
  const [sure, setSure] = useState(false);
  // The choice last sent: a refused one is not sent again on leaving the list.
  const [tried, setTried] = useState<AccelPrivacy | null>(null);
  // Whether the last change came from stepping with the keyboard.
  const stepping = useRef(false);
  useEffect(() => {
    setDraft(column.privacy);
    setSure(false);
    setTried(null);
  }, [column.privacy]);
  const confirming = draft === "kept" && draft !== column.privacy && needsWord(column);
  const pending = draft !== column.privacy;
  const commit = (privacy: AccelPrivacy, word: boolean) => {
    if (off || privacy === column.privacy || editing === key) return;
    const sent = onEdit(key, {
      path: "structure/privacy",
      body: {
        table: column.source_table,
        column: column.source_column,
        privacy,
        ...(word ? { confirm_not_personal: true } : {}),
      },
    }, {
      focus: id,
      says: () => `${column.name} is now ${PRIVACY_WORDS[privacy].toLowerCase()}.`,
    });
    setTried(sent ? privacy : null);
  };
  const askId = controlId("privacy-ask", role, where);
  const why = [column.locked ? whyId : "", confirming ? askId : "", staleWhy(stale) ?? ""]
    .filter(Boolean).join(" ");
  return (
    <tr data-column={where}>
      <td className="mono">{column.name}</td>
      <td>
        <span className="srcprivacy">
          <select
            id={id}
            className="input"
            value={draft}
            aria-label={`Privacy of ${column.name}`}
            aria-disabled={off || undefined}
            aria-busy={editing === key || undefined}
            aria-describedby={why || undefined}
            title={column.locked ? `Locked: ${column.reason}` : undefined}
            onChange={(event) => {
              if (off) return;
              const privacy = PRIVACY.find((p) => p === event.target.value);
              if (!privacy) return;
              setDraft(privacy);
              setTried(null);
              const byKeys = stepping.current;
              stepping.current = false;
              // Picked from the list: saved at once, unless keeping it needs
              // the person's word first.
              const needs = privacy === "kept" && privacy !== column.privacy && needsWord(column);
              if (!byKeys && !needs) commit(privacy, false);
            }}
            onBlur={() => {
              if (!confirming && draft !== tried) commit(draft, false);
            }}
            onKeyDown={(event) => {
              stepping.current = ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home",
                "End", "PageUp", "PageDown"].includes(event.key) || event.key.length === 1;
              if (event.key === "Enter" && !confirming) {
                event.preventDefault();
                commit(draft, false);
              }
              if (event.key === "Escape") setDraft(column.privacy);
            }}
          >
            {PRIVACY.map((p) => (
              <option key={p} value={p}>{PRIVACY_WORDS[p]}</option>
            ))}
          </select>
          {column.locked && <Chip tone="caution" fill="outline">Locked</Chip>}
        </span>
        {column.privacy !== column.proposed && !pending && (
          <span className="panel__hint srcprivacy__was">
            Proposed {PRIVACY_WORDS[column.proposed].toLowerCase()}
          </span>
        )}
        {confirming && (
          <span className="srcprivacy__sure" role="group" aria-label={`Keep ${column.name}`}>
            <span className="sr-only" id={askId}>
              Keeping it needs your word that it holds nothing personal.
            </span>
            <label className="srclevel__pick">
              <input id={sureId} type="checkbox" checked={sure}
                onChange={(event) => setSure(event.target.checked)} />
              This column holds nothing personal
            </label>
            <Button id={keepId} variant="primary" busy={editing === key} disabled={!sure || off}
              keepFocus label={`Keep ${column.name}`} onClick={() => {
                if (sure) commit("kept", true);
              }}>
              Keep column
            </Button>
            <Button onClick={() => {
              setDraft(column.privacy);
              requestAnimationFrame(() => document.getElementById(id)?.focus());
            }}>
              Cancel
            </Button>
          </span>
        )}
        {pending && !confirming && editing !== key && (
          <span className="panel__hint srcprivacy__was">
            {tried === draft
              ? "Not saved: the change was refused. Choose again, or press Escape."
              : "Not saved yet: press Enter or leave the list to save."}
          </span>
        )}
      </td>
      <td className="mono">{where}</td>
      <td className="mono">{column.sql_type}</td>
      <td>{SERVES_WORDS[column.serves] ?? column.serves}</td>
      <td id={whyId}>{column.reason}</td>
    </tr>
  );
}

/* ------------------------------------------------------------------ */
/* Approval                                                            */
/* ------------------------------------------------------------------ */

type OnAct = (key: string, call: (name: string) => Promise<AccelApproval>, after: ActAfter) => void;

const STATUS_WORDS: Record<AccelApproval["status"], string> = {
  draft: "Draft",
  pending_review: "Waiting for review",
  approved: "Approved",
  changed: "Changed since it was submitted",
};
const STATUS_TONE: Record<AccelApproval["status"], "neutral" | "caution" | "affirm"> = {
  draft: "neutral",
  pending_review: "caution",
  approved: "affirm",
  changed: "caution",
};

const PACK_SAVE = "src-pack-save";
const PACK_RULE = "src-pack-rule";
const SUBMIT_ID = "src-submit";
const APPROVE_ID = "src-approve";
const CONFIRM_ID = "src-approve-name";
const WITHDRAW_ID = "src-withdraw";

/** A time the store recorded, in the reader's own way of writing it. */
function when(at: string | null): string {
  if (!at) return "";
  const parsed = new Date(at);
  return Number.isNaN(parsed.getTime()) ? at : parsed.toLocaleString();
}

function ApprovalTab({
  plan, approval, approvalError, editing, editError, locked, stale, busy, onEdit, onAct,
  onRetry,
}: {
  plan: AccelPlan;
  approval: AccelApproval | null;
  approvalError: string | null;
  editing: string | null;
  editError: string | null;
  locked: boolean;
  stale: boolean;
  /** Something is in flight: what withdrawing waits for (the proposal's age does not stop it). */
  busy: boolean;
  onEdit: OnEdit;
  onAct: OnAct;
  onRetry: () => void;
}) {
  const [packId, setPackId] = useState(plan.pack?.id ?? "");
  const [packName, setPackName] = useState(plan.pack?.name ?? "");
  useEffect(() => {
    setPackId(plan.pack?.id ?? "");
    setPackName(plan.pack?.name ?? "");
  }, [plan.pack?.id, plan.pack?.name]);
  // Approve, waiting for the second approver's name; and the name typed.
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState("");
  const status = approval?.status ?? "draft";
  useEffect(() => {
    if (status !== "pending_review") {
      setConfirming(false);
      setTyped("");
    }
  }, [status]);
  useEffect(() => {
    if (confirming) document.getElementById(CONFIRM_ID)?.focus();
  }, [confirming]);

  const canName = !locked && Boolean(packId.trim()) && Boolean(packName.trim());
  const nameForm = (
    <form
      className="srckind__name"
      onSubmit={(event) => {
        event.preventDefault();
        if (!canName) return;
        onEdit("pack", { path: "pack-name", body: { id: packId.trim(), name: packName } }, {
          focus: PACK_SAVE,
          says: (view) => `Pack named ${view.plan?.pack.name ?? packName}.`,
        });
      }}
    >
      <label className="srckind__namefield">
        <span className="field__label">Pack id</span>
        <input className="input mono" value={packId} maxLength={32}
          aria-describedby={[PACK_RULE, staleWhy(stale) ?? ""].filter(Boolean).join(" ")}
          aria-disabled={locked || undefined} readOnly={locked}
          onChange={(event) => setPackId(event.target.value)} />
      </label>
      <label className="srckind__namefield">
        <span className="field__label">Pack name</span>
        <input className="input" value={packName} maxLength={60}
          aria-describedby={staleWhy(stale)}
          aria-disabled={locked || undefined} readOnly={locked}
          onChange={(event) => setPackName(event.target.value)} />
      </label>
      <Button type="submit" id={PACK_SAVE} busy={editing === "pack"} disabled={!canName} keepFocus
        describedBy={staleWhy(stale)}>
        Save pack name
      </Button>
    </form>
  );

  if (!approval) {
    return (
      <section className="panel" aria-label="Approval">
        <header>
          <h2 className="panel__title" id={APPROVAL_HEADING} tabIndex={-1}>Approval</h2>
        </header>
        {approvalError ? (
          <Banner tone="alarm">
            <div className="srcsetup__said">
              <p>{approvalError}</p>
              <div className="srcsetup__inline">
                <Button onClick={onRetry}>Try again</Button>
              </div>
            </div>
          </Banner>
        ) : (
          <p className="panel__hint">Reading where the plan stands…</p>
        )}
      </section>
    );
  }

  const needsName = approval.second_approver !== null || approval.approver_unreadable;
  const named = (approval.second_approver ?? "").trim().toLowerCase();
  // The server's rule: spaces at either end ignored, letters compared lower-cased.
  const confirmed = named !== "" && typed.trim().toLowerCase() === named;
  const open = status === "draft" || status === "changed";
  const canSubmit = !locked && open && approval.problems.length === 0;

  const submit = () => {
    if (!canSubmit) return;
    onAct("submit", (name) => api.accelSubmit(name), {
      focus: APPROVE_ID,
      failed: SUBMIT_ID,
      says: (next) => `Plan ${STATUS_WORDS[next.status].toLowerCase()}.`,
    });
  };
  const approve = () => {
    if (locked || status !== "pending_review" || (needsName && !confirmed)) return;
    onAct("approve", (name) => api.accelApprove(name, approval.plan_hash, needsName ? typed.trim() : null), {
      focus: WITHDRAW_ID,
      failed: confirming ? CONFIRM_ID : APPROVE_ID,
      says: (next) => next.status === "approved" ? `Plan approved by ${next.approved_by ?? ""}.` : "Not approved.",
    });
  };
  // A plan once submitted can be withdrawn even when the proposal is older
  // than the read: that only takes it back to draft.
  const withdraw = () => {
    if (busy || open) return;
    onAct("withdraw", (name) => api.accelWithdraw(name), {
      focus: SUBMIT_ID,
      failed: WITHDRAW_ID,
      says: () => "Plan withdrawn: it is a draft again.",
    });
  };

  return (
    <section className="panel" aria-label="Approval">
      <header>
        <h2 className="panel__title" id={APPROVAL_HEADING} tabIndex={-1}>Approval</h2>
        <p className="panel__hint">
          A plan is built only once it is approved, and only as it was approved: any change sends
          it back to draft.
        </p>
      </header>
      {editError && <Banner tone="alarm">{editError}</Banner>}
      {approvalError && (
        <Banner tone="alarm">
          <div className="srcsetup__said">
            <p>{approvalError}</p>
            <div className="srcsetup__inline">
              <Button onClick={onRetry}>Try again</Button>
            </div>
          </div>
        </Banner>
      )}
      {approval.approver_unreadable && (
        <Banner tone="caution">
          The approval setting can't be read, so approving asks for the second approver's name.
          Set it again in Settings › Layout approval.
        </Banner>
      )}

      <dl className="srclevel__facts srcstatus">
        <div>
          <dt>Status</dt>
          <dd>
            <Chip tone={STATUS_TONE[status]} fill="outline">{STATUS_WORDS[status]}</Chip>
          </dd>
        </div>
        {approval.submitted_by && (
          <div>
            <dt>Submitted by</dt>
            <dd>{approval.submitted_by}, {when(approval.submitted_at)}</dd>
          </div>
        )}
        {approval.approved_by && (
          <div>
            <dt>Approved by</dt>
            <dd>{approval.approved_by}, {when(approval.approved_at)}</dd>
          </div>
        )}
        {approval.confirmed_by && approval.confirmed_by !== approval.approved_by && (
          <div>
            <dt>Confirmed by</dt>
            <dd>{approval.confirmed_by}</dd>
          </div>
        )}
        {approval.built_at && (
          <div>
            <dt>Built</dt>
            <dd>{when(approval.built_at)}</dd>
          </div>
        )}
      </dl>
      {status === "changed" && (
        <Banner tone="caution">
          The plan saved is no longer the one submitted, so its review no longer holds. Submit it
          again.
        </Banner>
      )}

      <h3 className="srcsetup__subtitle">Pack</h3>
      {nameForm}
      <p className="panel__hint" id={PACK_RULE}>
        The id is the pack's folder name: lowercase letters, digits and _, starting with a letter.
      </p>

      <h3 className="srcsetup__subtitle">Before submitting</h3>
      {approval.problems.length ? (
        <ul className="srclist srcproblems">
          {approval.problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      ) : (
        <p className="panel__hint">Nothing is left to do: the plan can be submitted.</p>
      )}

      <div className="panel__actions">
        {open && (
          <Button variant="primary" id={SUBMIT_ID} busy={editing === "submit"}
            disabled={!canSubmit} keepFocus describedBy={staleWhy(stale)} onClick={submit}>
            Submit
          </Button>
        )}
        {status === "pending_review" && !confirming && (
          <Button variant="primary" id={APPROVE_ID} busy={editing === "approve"} disabled={locked}
            keepFocus describedBy={staleWhy(stale)} onClick={() => {
              if (locked) return;
              if (needsName) {
                setTyped("");
                setConfirming(true);
              } else {
                approve();
              }
            }}>
            Approve
          </Button>
        )}
        {!open && (
          <Button id={WITHDRAW_ID} busy={editing === "withdraw"} disabled={busy} keepFocus
            onClick={withdraw}>
            Withdraw
          </Button>
        )}
        {open || status !== "pending_review" || needsName ? (
          <span className="panel__hint">
            {open
              ? "Submitting asks for review of the plan as it is saved now."
              : status === "pending_review"
                ? `${approval.second_approver ?? "The second approver"} approves it, typing their name to confirm.`
                : "Withdrawing takes the plan back to draft."}
          </span>
        ) : (
          <span className="srchint--caution">
            No second approver is set, so the plan is approved in your name. To have someone else
            approve, name them in Settings › Layout approval.
          </span>
        )}
      </div>
      {confirming && status === "pending_review" && (
        <div className="srcsetup__inline" role="group" aria-label="Approve the plan">
          <label className="srckind__namefield">
            <span className="field__label">
              {`Type ${approval.second_approver ?? "the approver's name"} to approve`}
            </span>
            <input
              id={CONFIRM_ID}
              className="input"
              value={typed}
              autoComplete="off"
              maxLength={100}
              onChange={(event) => setTyped(event.target.value)}
              onKeyDown={(event) => {
                // Enter approves once, never again while it is sent.
                if (event.key === "Enter" && confirmed && editing === null) approve();
                if (event.key === "Escape") {
                  setConfirming(false);
                  requestAnimationFrame(() => document.getElementById(APPROVE_ID)?.focus());
                }
              }}
            />
          </label>
          <Button variant="primary" busy={editing === "approve"} disabled={!confirmed || locked}
            keepFocus onClick={approve}>
            Approve
          </Button>
          <Button onClick={() => {
            setConfirming(false);
            requestAnimationFrame(() => document.getElementById(APPROVE_ID)?.focus());
          }}>
            Cancel
          </Button>
        </div>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Build                                                               */
/* ------------------------------------------------------------------ */

const VIEWER_HEADING = "src-viewer-heading";
const FILES_HEADING = "src-files-heading";
const viewId = (name: string) => `src-view-${name}`.replace(/[^A-Za-z0-9_-]/g, "_");

function BuildTab({
  source, approval, approvalError, files, filesSaid, built, buildError, job, lostContact,
  running, runningHere, locked, stale, onBuild, onApproval, onRetry, onSaid, onLoad,
}: {
  source: string;
  approval: AccelApproval | null;
  approvalError: string | null;
  files: AccelFiles | null;
  filesSaid: { tone: "caution" | "alarm"; text: string } | null;
  built: AccelJobResult | null;
  buildError: string | null;
  job: AccelJob | null;
  lostContact: boolean;
  running: boolean;
  runningHere: boolean;
  locked: boolean;
  stale: boolean;
  onBuild: () => void;
  onApproval: () => void;
  onRetry: () => void;
  /** Announced in the screen's live region. */
  onSaid: (words: string) => void;
  onLoad: () => void;
}) {
  const [shown, setShown] = useState<{ name: string; text: string } | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const viewing = useRef(0);
  const viewer = useRef<HTMLElement | null>(null);
  // A new list (after a build) may hold other text: what was shown is closed,
  // and focus inside it goes to the Files heading rather than the page.
  useEffect(() => {
    viewing.current += 1;
    if (viewer.current?.contains(document.activeElement)) {
      requestAnimationFrame(() => document.getElementById(FILES_HEADING)?.focus());
    }
    setShown(null);
    setOpening(null);
  }, [files]);

  async function view(name: string) {
    const mine = ++viewing.current;
    setOpening(name);
    setFileError(null);
    try {
      const text = await api.accelFileText(source, name);
      if (mine !== viewing.current) return;
      setFileError(null);
      setShown({ name, text });
      requestAnimationFrame(() => document.getElementById(VIEWER_HEADING)?.focus());
    } catch (e) {
      if (mine !== viewing.current) return;
      setFileError(describe(e));
    } finally {
      if (mine === viewing.current) setOpening(null);
    }
  }

  // One save at a time for each file: a second click while it saves does nothing.
  async function download(name: string) {
    if (saving !== null) return;
    setSaving(name);
    setFileError(null);
    try {
      await api.accelFileDownload(source, name);
      onSaid(`${name} downloaded.`);
    } catch (e) {
      setFileError(describe(e));
      onSaid(`${name} was not downloaded.`);
    } finally {
      setSaving(null);
    }
  }

  function close() {
    if (!shown) return;
    const back = viewId(shown.name);
    setShown(null);
    setFileError(null);
    requestAnimationFrame(() => document.getElementById(back)?.focus());
  }

  const heading = (
    <header>
      <h2 className="panel__title" id={BUILD_HEADING} tabIndex={-1}>Build</h2>
      <p className="panel__hint">
        Writes the pack's files from the approved plan, then starts SDM on them in a process of its
        own. The pack is put in place only if SDM starts on it.
      </p>
    </header>
  );
  if (!approval) {
    return (
      <section className="panel" aria-label="Build">
        {heading}
        {approvalError ? (
          <Banner tone="alarm">
            <div className="srcsetup__said">
              <p>{approvalError}</p>
              <div className="srcsetup__inline">
                <Button onClick={onRetry}>Try again</Button>
              </div>
            </div>
          </Banner>
        ) : (
          <p className="panel__hint">Reading where the plan stands…</p>
        )}
      </section>
    );
  }
  if (approval.status !== "approved") {
    return (
      <section className="panel" aria-label="Build">
        {heading}
        {buildError && <Banner tone="alarm">{buildError}</Banner>}
        <Empty
          title="The plan is not approved yet"
          action={<Button variant="primary" onClick={onApproval}>Go to Approval</Button>}
        >
          A pack is built only from an approved plan, as it was approved.
        </Empty>
      </section>
    );
  }

  const building = runningHere && Boolean(job?.label.startsWith("Build"));
  const reread = approvalError && (
    <Banner tone="alarm">
      <div className="srcsetup__said">
        <p>{approvalError}</p>
        <div className="srcsetup__inline">
          <Button onClick={onRetry}>Try again</Button>
        </div>
      </div>
    </Banner>
  );
  const packsDir = built?.packs_dir ?? files?.packs_dir ?? "";
  const packId = built?.pack_id ?? files?.pack_id ?? approval.pack.id ?? "";
  const anyBuilt = Boolean(files?.files.some((f) => f.built));
  return (
    <section className="panel" aria-label="Build">
      {heading}
      {reread}
      <div className="panel__actions">
        <Button variant="primary" id="src-build" busy={building} disabled={running || locked}
          keepFocus describedBy={staleWhy(stale)} onClick={onBuild}>
          Build pack
        </Button>
        <span className="panel__hint">
          {running && !runningHere
            ? `${job?.source ?? "Another source"} is being worked on; one job runs at a time.`
            : buildError
              ? "Build pack tries again."
              : approval.built_at
                ? `Last built ${when(approval.built_at)}. Building again keeps the earlier build beside it.`
                : `Builds the ${packId} pack.`}
        </span>
      </div>
      {building && job && <Progress job={job} lostContact={lostContact} />}
      {buildError && !building && (
        <div id={BUILD_RESULT} tabIndex={-1}>
          <Banner tone="alarm">{buildError}</Banner>
        </div>
      )}
      {built && built.placed === false && (
        <div id={BUILD_RESULT} tabIndex={-1}>
          <Banner tone="alarm">
            <div className="srcsetup__said">
              <p>SDM would not start on the pack, so it was not put in place:</p>
              <ul className="srclist">
                {(built.problems ?? []).map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </div>
          </Banner>
        </div>
      )}
      {built?.placed && (
        <div id={BUILD_RESULT} tabIndex={-1}>
          <Banner tone="affirm">The {built.pack_id} pack is built and in place.</Banner>
        </div>
      )}

      <h3 className="srcsetup__subtitle" id={FILES_HEADING} tabIndex={-1}>Files</h3>
      {filesSaid && <Banner tone={filesSaid.tone}>{filesSaid.text}</Banner>}
      {fileError && <Banner tone="alarm">{fileError}</Banner>}
      {files && (
        <>
          <p className="panel__hint">
            In <span className="mono">{files.folder}</span>
          </p>
          <div className="srcsetup__scroll">
            <table className="panel__table srcsetup__narrow srcfiles">
              <thead>
                <tr>
                  <th>File</th>
                  <th className="right">Size</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {files.files.map((file) => (
                  <tr key={file.name} data-file={file.name}>
                    <td className="mono">{file.name}</td>
                    <td className="right num">
                      {file.built ? `${formatCount(file.bytes)} bytes` : "Not built"}
                    </td>
                    <td>
                      {file.built && (
                        <span className="srcfiles__actions">
                          <Button id={viewId(file.name)} busy={opening === file.name} keepFocus
                            label={`View ${file.name}`} onClick={() => void view(file.name)}>
                            View
                          </Button>
                          <Button busy={saving === file.name} disabled={saving !== null}
                            keepFocus label={`Download ${file.name}`}
                            onClick={() => void download(file.name)}>
                            Download
                          </Button>
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {shown && (
        <section
          className="srcviewer"
          aria-labelledby={VIEWER_HEADING}
          ref={viewer}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              close();
            }
          }}
        >
          <div className="srcviewer__head">
            <h4 className="srcsetup__minor mono" id={VIEWER_HEADING} tabIndex={-1}>{shown.name}</h4>
            <Button label={`Close ${shown.name}`} onClick={close}>Close</Button>
          </div>
          <pre className="srcviewer__text mono" tabIndex={0} role="region"
            aria-label={`Text of ${shown.name}`}>{shown.text}</pre>
        </section>
      )}

      {(anyBuilt || built?.placed) && (
        <>
          <h3 className="srcsetup__subtitle">Next</h3>
          <div className="panel__actions">
            <Button variant="primary" onClick={onLoad}>Go to Load</Button>
            <span className="panel__hint">
              Creates the pack's tables in the SDM database and fills them from {source}.
            </span>
          </div>
          <h3 className="srcsetup__subtitle">Or install the pack by hand</h3>
          <ol className="srcsteps">
            <li>
              In the .env file, set{" "}
              <code className="mono">SDG_PACKS_DIR={packsDir || "<the packs folder>"}</code> and{" "}
              <code className="mono">SDG_PACK={packId}</code>.
            </li>
            <li>
              Create the pack's tables in the SDM database: run the table setup
              (<code className="mono">scripts\customer\setup_tables.cmd</code>), or give{" "}
              <code className="mono">sql/store_tables.sql</code> and{" "}
              <code className="mono">sql/store_indexes.sql</code> to your DBA.
            </li>
            <li>
              Set <code className="mono">SDG_EXTRACT_SOURCE={source}</code> in the .env file.
              README.txt says which connection settings the extract reads the source through.
            </li>
            <li>
              Run the extract (<code className="mono">scripts\customer\extract.cmd</code>). It reads{" "}
              <code className="mono">extract/{source}.sql</code> and fills the tables.
            </li>
            <li>Restart SDM, so it reads the new pack.</li>
          </ol>
        </>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Load                                                                */
/* ------------------------------------------------------------------ */

const STORE_TABLES = "sql/store_tables.sql";

/** What the store says of one built table. */
function tableState(table: AccelStoreTable): string {
  if (table.foreign) {
    return table.other_source
      ? "Not this build's table: filled from another source"
      : table.extra.length
        ? `Not this build's table: it has ${table.extra.join(", ")}`
        : `Not this build's table: ${table.differs.join(", ")} of another type`;
  }
  if (!table.exists) return "Not created";
  const said: string[] = [];
  if (table.lacking.length) {
    said.push(`Lacks ${table.lacking.length} ${table.lacking.length === 1 ? "column" : "columns"}: ` +
      table.lacking.join(", "));
  }
  if (table.differs.length) {
    said.push(`${table.differs.join(", ")} of another type than the build: the DBA alters ` +
      (table.differs.length === 1 ? "it" : "them"));
  }
  return said.length ? said.join(". ") : "Ready";
}

/** The columns an earlier build left, as statements to give the DBA: shown
 *  and copied, never run from SDM. */
function DropsForDba({ drops, onSaid }: { drops: string[]; onSaid: (words: string) => void }) {
  const text = drops.join("\n");
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      onSaid("The statements are copied.");
    } catch {
      onSaid("The statements could not be copied; select them and copy them instead.");
    }
  }
  return (
    <div className="srcdrops">
      <h4 className="srcsetup__minor">Columns no longer in the build</h4>
      <p className="panel__hint">
        They stay in the tables and are loaded empty. To remove them, give these statements to
        the DBA; SDM does not run them.
      </p>
      <pre className="srcviewer__text mono" tabIndex={0} role="region"
        aria-label="Statements for the DBA">{text}</pre>
      <div className="panel__actions">
        <Button onClick={() => void copy()}>Copy statements</Button>
      </div>
    </div>
  );
}

function checkWords(check: AccelCheck["check"]): string {
  return { rows: "Rows", key: "One row per key", filled: "Filled", meets: "Meets its record" }[check];
}

function LoadTab({
  source, plan, status, statusSaid, loaded, loadError, job, lostContact, running, runningHere,
  editing, onSaid, onStatus, onLoad, onBuild, onRetry,
}: {
  source: string;
  plan: AccelPlan;
  status: AccelLoadStatus | null;
  statusSaid: { kind: string | null; text: string } | null;
  loaded: AccelJobResult | null;
  loadError: string | null;
  job: AccelJob | null;
  lostContact: boolean;
  running: boolean;
  runningHere: boolean;
  editing: boolean;
  onSaid: (words: string) => void;
  onStatus: (status: AccelLoadStatus) => void;
  onLoad: () => void;
  onBuild: () => void;
  onRetry: () => void;
}) {
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);

  const loadingHere = runningHere && Boolean(job?.label.startsWith("Load"));
  useEffect(() => {
    if (!loadingHere) setStopping(false);
  }, [loadingHere]);

  const heading = (
    <header>
      <h2 className="panel__title" id={LOAD_HEADING} tabIndex={-1}>Load</h2>
      <p className="panel__hint">
        Creates the pack's tables in the SDM database and fills them from {source}. Refresh
        reads {source} again, whenever you choose.
      </p>
    </header>
  );

  if (!status) {
    return (
      <section className="panel" aria-label="Load">
        {heading}
        {statusSaid?.kind === "not_built" ? (
          <Empty
            title="The pack is not built yet"
            action={<Button variant="primary" onClick={onBuild}>Go to Build</Button>}
          >
            {statusSaid.text}
          </Empty>
        ) : statusSaid ? (
          <Banner tone="alarm">
            <div className="srcsetup__said">
              <p>{statusSaid.text}</p>
              <div className="srcsetup__inline">
                <Button onClick={onRetry}>Try again</Button>
              </div>
            </div>
          </Banner>
        ) : (
          <p className="panel__hint">Reading the SDM database…</p>
        )}
      </section>
    );
  }

  const { built, store } = status;
  const scriptEdited = built.edited.includes(STORE_TABLES);
  const anyForeign = store.tables.some((t) => t.foreign);
  const anyThere = store.tables.some((t) => t.exists);
  // What Create tables can do: make a table, or add the columns one lacks.
  const creatable = store.tables.some((t) => !t.foreign && (!t.exists || t.lacking.length > 0));
  const forDba = !store.may_create || scriptEdited;

  async function create() {
    if (creating) return;
    setCreating(true);
    setCreateError(null);
    try {
      const done = await api.accelCreateTables(source);
      onStatus({ ...status!, built: done.built, store: done.store });
      onSaid(done.store.ready ? "The tables are ready." : "Some tables are not ready yet.");
      requestAnimationFrame(() =>
        (document.getElementById("src-load") ?? document.getElementById(LOAD_HEADING))?.focus());
    } catch (e) {
      setCreateError(describe(e));
      onSaid("The tables were not created.");
      onRetry();
      requestAnimationFrame(() => document.getElementById(LOAD_HEADING)?.focus());
    } finally {
      setCreating(false);
    }
  }

  async function download() {
    if (saving) return;
    setSaving(true);
    try {
      await api.accelFileDownload(source, STORE_TABLES);
      onSaid(`${STORE_TABLES} downloaded.`);
    } catch (e) {
      setCreateError(describe(e));
      onSaid(`${STORE_TABLES} was not downloaded.`);
    } finally {
      setSaving(false);
    }
  }

  async function stop() {
    if (!job || stopping) return;
    setStopping(true);
    setStopError(null);
    try {
      await api.accelStop(job.id);
      onSaid("Stopping after the table being loaded.");
    } catch (e) {
      setStopping(false);
      setStopError(describe(e));
    }
  }

  const roleOf = (role: AccelRole) => capitalized(roleWords(plan, role));
  const refresh = status.loads.length > 0;
  const failedChecks = (loaded?.checks ?? []).filter((c) => !c.passed);
  return (
    <section className="panel" aria-label="Load">
      {heading}
      <p className="panel__hint">
        The <span className="mono">{built.pack_id}</span> pack, built{" "}
        {when(built.built_at)}{built.built_by ? ` by ${built.built_by}` : ""}.
      </p>
      {built.edited.length > 0 && (
        <Banner tone="caution">
          Edited since the build: <span className="mono">{built.edited.join(", ")}</span>.
          {scriptEdited && " An edited table script is the DBA's to run."}
        </Banner>
      )}

      <h3 className="srcsetup__subtitle">Tables</h3>
      <p className="panel__hint">
        In <span className="mono">{store.database}</span>, schema{" "}
        <span className="mono">{store.schema}</span>.
      </p>
      <div className="srcsetup__scroll">
        <table className="panel__table srcsetup__narrow srcload">
          <thead>
            <tr>
              <th>Table</th>
              <th>Records</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {store.tables.map((table) => (
              <tr key={table.name} data-table={table.name}>
                <td className="mono">{table.name}</td>
                <td>{roleOf(table.role)}</td>
                <td>
                  <Chip tone={table.ready ? "affirm" : table.foreign ? "alarm" : "caution"}>
                    {table.ready ? "Ready" : table.foreign ? "Not this build's" : table.exists ? "Incomplete" : "Not created"}
                  </Chip>{" "}
                  {!table.ready && <span>{tableState(table)}</span>}
                  {table.ready && table.extra.length > 0 && (
                    <span>Keeps {table.extra.join(", ")} from an earlier build; loaded empty.</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {createError && <Banner tone="alarm">{createError}</Banner>}
      {!store.ready && !anyForeign && creatable && (
        forDba ? (
          <div className="panel__actions">
            <Button busy={saving} disabled={saving} keepFocus onClick={() => void download()}>
              Download {STORE_TABLES}
            </Button>
            <span className="panel__hint">
              {scriptEdited
                ? "Give the edited script to the DBA to run, then come back here to load."
                : "This login may not create tables here. Give the script to the DBA to run, then come back here to load."}
            </span>
          </div>
        ) : (
          <div className="panel__actions">
            <Button variant="primary" id="src-create-tables" busy={creating}
              disabled={creating || running || editing} keepFocus onClick={() => void create()}>
              {anyThere ? "Add missing columns" : "Create tables"}
            </Button>
            <span className="panel__hint">
              {running && !runningHere
                ? `${job?.source ?? "Another source"} is being worked on; one job runs at a time.`
                : "Adds only: nothing in the database is dropped or changed."}
            </span>
          </div>
        )
      )}
      {store.drops.length > 0 && <DropsForDba drops={store.drops} onSaid={onSaid} />}
      {anyForeign && (
        <Banner tone="alarm">
          A table this pack names is already in the SDM database as another one. Nothing is
          changed in it from here: name the pack's tables differently, or ask the DBA.
        </Banner>
      )}

      <h3 className="srcsetup__subtitle">{refresh ? "Refresh" : "Load"}</h3>
      {store.ready ? (
        <>
          <div className="panel__actions">
            <Button variant="primary" id="src-load" busy={loadingHere}
              disabled={running || editing} keepFocus onClick={onLoad}>
              {refresh ? "Refresh" : "Load now"}
            </Button>
            {loadingHere && (
              <Button busy={stopping} disabled={stopping} keepFocus onClick={() => void stop()}>
                Stop
              </Button>
            )}
            <span className="panel__hint">
              {running && !runningHere
                ? `${job?.source ?? "Another source"} is being worked on; one job runs at a time.`
                : loadingHere
                  ? "Stop finishes the table being loaded; the tables after it keep their rows."
                  : `Reads every row the plan selects from ${source}: run it off-peak, or against a replica.`}
            </span>
          </div>
          {stopError && <Banner tone="alarm">{stopError}</Banner>}
          {loadingHere && job && <Progress job={job} lostContact={lostContact} />}
        </>
      ) : (
        <p className="panel__hint">The tables must be ready before they are loaded.</p>
      )}
      {loadError && !loadingHere && (
        <div id={LOAD_RESULT} tabIndex={-1}>
          <Banner tone="alarm">{loadError}</Banner>
        </div>
      )}
      {loaded?.load && !loadingHere && (
        <div id={LOAD_RESULT} tabIndex={-1} className="srcload__result">
          <Banner tone={loaded.stopped || loaded.failed || failedChecks.length ? "caution" : "affirm"}>
            {loaded.stopped
              ? "The load was stopped. The tables it did not reach keep their rows."
              : loaded.failed
                ? `${loaded.failed} ${loaded.failed === 1 ? "table was" : "tables were"} not loaded; ${loaded.failed === 1 ? "it keeps" : "they keep"} the rows it had.`
                : `Loaded ${formatCount(loaded.load.rows)} rows in ${Math.round(loaded.load.seconds)} s.`}
          </Banner>
          <div className="srcsetup__scroll">
            <table className="panel__table srcsetup__narrow srcload">
              <thead>
                <tr>
                  <th>Table</th>
                  <th className="right">Rows</th>
                  <th className="right">Seconds</th>
                  <th>Note</th>
                </tr>
              </thead>
              <tbody>
                {loaded.load.tables.map((t) => (
                  <tr key={t.table}>
                    <td className="mono">{t.table}</td>
                    <td className="right num">{t.error ? "" : formatCount(t.rows)}</td>
                    <td className="right num">{t.error ? "" : Math.round(t.seconds)}</td>
                    <td>{t.error ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(loaded.checks ?? []).length > 0 && (
            <>
              <h4 className="srcsetup__minor">Checks</h4>
              <ul className="srclist srcchecks">
                {(loaded.checks ?? []).map((c, i) => (
                  <li key={`${c.table}-${c.check}-${i}`}>
                    <Chip tone={c.passed ? "affirm" : "caution"}>
                      {c.passed ? "Passed" : "Needs a look"}
                    </Chip>{" "}
                    <span className="mono">{c.table}</span> · {checkWords(c.check)}: {c.said}
                  </li>
                ))}
              </ul>
            </>
          )}
          {(loaded.notes ?? []).length > 0 && (
            <ul className="srclist">
              {(loaded.notes ?? []).map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {!status.analyzing && status.loads.length > 0 && (
        <Banner tone="neutral">
          To analyze these tables, set <code className="mono">SDG_PACK={built.pack_id}</code> in
          the .env file and restart SDM. It runs on the{" "}
          <span className="mono">{status.running_pack}</span> pack now.
        </Banner>
      )}

      {status.loads.length > 0 && (
        <>
          <h3 className="srcsetup__subtitle">Last loads</h3>
          <div className="srcsetup__scroll">
            <table className="panel__table srcsetup__narrow srcload">
              <thead>
                <tr>
                  <th>Finished</th>
                  <th>Table</th>
                  <th className="right">Rows</th>
                  <th>Note</th>
                </tr>
              </thead>
              <tbody>
                {status.loads.map((row, i) => (
                  <tr key={`${row.finished_at}-${row.table}-${i}`}>
                    <td>{when(row.finished_at)}</td>
                    <td className="mono">{row.table}</td>
                    <td className="right num">{row.error ? "" : formatCount(row.rows)}</td>
                    <td>{row.error ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
