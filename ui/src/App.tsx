/**
 * The shell.
 *
 * State is deliberately plain: this is one operator working one database, so
 * there is no cache layer and no optimistic updating. Every action that changes
 * something on disk refetches what it changed, because being right matters more
 * here than being instant.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Rail, SCREENS, TOOLS, type Screen, type Tool } from "./components/Rail";
import { Banner, Button, Empty } from "./components/primitives";
import { Cascade } from "./views/Cascade";
import { FileGeneration, FileMapping, sampleTab, type FileTab } from "./views/Output";
import { LayoutApproval } from "./views/LayoutApproval";
import { FileSource, FilesProfile } from "./views/Files";
import { PlanView } from "./views/PlanView";
import { GenerationRequests } from "./views/Requests";
import { Algorithms } from "./views/Algorithms";
import { DiscoveryConfig } from "./views/DiscoveryConfig";
import { ReportFieldsConfig } from "./views/ReportFieldsConfig";
import { Patterns } from "./views/Patterns";
import { Run } from "./views/Run";
import { Schema } from "./views/Schema";
import { Setup, type ReadResults } from "./views/Setup";
import { SOURCE_TABS, SourceSetup, type SourceTab } from "./views/SourceSetup";
import {
  ApiError,
  api,
  type AppState,
  type CatalogPayload,
  type CleanupResult,
  type HandedSample,
  type DecisionInput,
  type Domain,
  type FilePattern,
  type DryRunPayload,
  type GeneratorsPayload,
  type GenerateResult,
  type PlanPayload,
  type RatioSetting,
  type RatioShapesPayload,
  type RunPayload,
  type SchemaPayload,
  type ValidateResult,
} from "./lib/api";
import { DatabaseGeneration } from "./lib/databaseGeneration";
import { AppActionsContext, type AppActions, type Toast } from "./lib/appActions";
import "./App.css";
import { hasModule, keyOf, packName } from "./lib/pack";

// The left menu is by job: Patterns, Generate, History, Layouts and samples,
// Database setup. A database and files are no longer two parallel workflows
// through the same steps; each job holds both where it has both, as tabs.

type GenTab = "files" | "database";
type SetupTab = "connection" | "profile" | "map";
type LayoutsTab = "layouts" | "folder" | "profile";

const GEN_TABS: [GenTab, string][] = [
  ["files", "Files"],
  ["database", "Database"],
];
const SETUP_TABS: [SetupTab, string][] = [
  ["connection", "Connection"],
  ["profile", "Profile"],
  ["map", "Map"],
];
const LAYOUTS_TABS: [LayoutsTab, string][] = [
  ["layouts", "File layouts"],
  ["folder", "Folder"],
  ["profile", "File profile"],
];

// Setting up a database is done in order, so each of its tabs offers the next.
const SETUP_NEXT: Record<SetupTab, string> = {
  connection: "Next: Profile →",
  profile: "Next: Map →",
  map: "Next: Generate →",
};

/** A screen's title: its name on the left menu. */
function title(screen: Screen): string {
  return SCREENS[screen];
}

// Where the person was, so coming back opens there. Browser storage can be
// blocked or empty, or hold a place from before the menu was by job; then the
// app opens on Patterns.
const PLACE = "sdm.place";
interface Place {
  screen?: Screen;
  genTab?: GenTab;
  setupTab?: SetupTab;
  layoutsTab?: LayoutsTab;
  sourceTab?: SourceTab;
}
function oneOf<T extends string>(value: unknown, allowed: readonly T[]): T | undefined {
  return allowed.includes(value as T) ? (value as T) : undefined;
}
function recalled(): Place {
  try {
    const kept = JSON.parse(localStorage.getItem(PLACE) ?? "{}") ?? {};
    return {
      screen: oneOf(kept.screen, Object.keys(SCREENS) as Screen[]),
      genTab: oneOf(kept.genTab, GEN_TABS.map(([id]) => id)),
      setupTab: oneOf(kept.setupTab, SETUP_TABS.map(([id]) => id)),
      layoutsTab: oneOf(kept.layoutsTab, LAYOUTS_TABS.map(([id]) => id)),
      sourceTab: oneOf(kept.sourceTab, SOURCE_TABS.map(([id]) => id)),
    };
  } catch {
    return {};
  }
}
function remember(place: Place) {
  try {
    localStorage.setItem(PLACE, JSON.stringify(place));
  } catch {
    /* not kept; nothing depends on it */
  }
}

export default function App() {
  const [state, setState] = useState<AppState | null>(null);
  const [place] = useState(recalled);
  // A pack without pattern analysis opens on Generate, even where Patterns
  // was the screen last open: Patterns there only says it is not offered.
  const [screen, setScreen] = useState<Screen>(() => {
    const analysis = hasModule("pattern_analysis");
    const kept = place.screen === "patterns" && !analysis ? undefined : place.screen;
    return kept ?? (analysis ? "patterns" : "generate");
  });
  const [genTab, setGenTab] = useState<GenTab>(place.genTab ?? "files");
  const [setupTab, setSetupTab] = useState<SetupTab>(place.setupTab ?? "connection");
  const [layoutsTab, setLayoutsTab] = useState<LayoutsTab>(place.layoutsTab ?? "layouts");
  const [sourceTab, setSourceTab] = useState<SourceTab>(place.sourceTab ?? "sources");
  // The format open under Generate › Files, held here so History can open X12 EDI.
  const [fileTab, setFileTab] = useState<FileTab>("layouts");
  // A sample uploaded where flat files are (Layouts and samples, Flat file)
  // that is X12, FHIR or HL7: read at once on its format's tab under Generate.
  const [handed, setHanded] = useState<HandedSample | null>(null);
  const handOver = useCallback((sample: HandedSample) => {
    setHanded(sample);
    setTool(null);
    setScreen("generate");
    setGenTab("files");
    setFileTab(sampleTab(sample.format));
  }, []);
  const handedOver = useCallback(() => setHanded(null), []);
  // The top menu: settings that stand apart from the workflow on the rail.
  const [tool, setTool] = useState<Tool | null>(null);
  // DB Generation holds two things in order: the shape of the load, then
  // the load. They were two rail steps, which made sizing a run and
  // running it feel like different jobs.
  const [dbTab, setDbTab] = useState<"plan" | "generate">("plan");
  const [theme, setTheme] = useState<"light" | "dark">("light");
  // Which half of the data every screen is asking about. Held here so the
  // choice follows you from Patterns to Discover to a requirement, instead of
  // being remade on each.
  const [domain, setDomain] = useState<Domain>(() => keyOf("primary"));
  // The pattern a file is being written for, carried from Patterns to File
  // Generation. Nothing means the plan's own run.
  const [filePattern, setFilePattern] = useState<FilePattern | null>(null);
  const [claimsAvailable, setClaimsAvailable] = useState(false);
  // Why the check failed, when it did: not the same as "none held".
  const [childCheckError, setChildCheckError] = useState<string | null>(null);

  const [schema, setSchema] = useState<SchemaPayload | null>(null);
  const [plan, setPlan] = useState<PlanPayload | null>(null);
  const [catalog, setCatalog] = useState<CatalogPayload | null>(null);
  const [generators, setGenerators] = useState<GeneratorsPayload | null>(null);
  const [ratioShapes, setRatioShapes] = useState<RatioShapesPayload | null>(null);
  const [run, setRun] = useState<RunPayload | null>(null);

  const [preview, setPreview] = useState<DryRunPayload | null>(null);
  const [generated, setGenerated] = useState<GenerateResult | null>(null);
  const [validation, setValidation] = useState<ValidateResult | null>(null);
  const [cleaned, setCleaned] = useState<CleanupResult | null>(null);

  const [readResults, setReadResults] = useState<ReadResults>({});
  const [running, setRunning] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Said on arrival at Generate › Database after a pattern was applied from Patterns.
  const [notice, setNotice] = useState<string | null>(null);
  // An edit that was refused is reported next to the form that made it, not
  // at the top of the page where it would be missed.
  const [saveError, setSaveError] = useState<string | null>(null);
  // What a one-step action on a pattern did: the file and its run, say.
  const [toast, setToast] = useState<Toast | null>(null);
  // The run History brings into view, when it was opened from a confirmation.
  const [historyFocus, setHistoryFocus] = useState<number | null>(null);

  // A confirmation stays long enough to act on, then goes by itself -- not
  // while the pointer or the keyboard is on it, and a failure never by itself.
  const [toastHeld, setToastHeld] = useState(false);
  // A new toast (or none) starts unheld: one removed under the pointer never
  // sees the pointer leave.
  useEffect(() => setToastHeld(false), [toast]);
  useEffect(() => {
    if (!toast || toastHeld || toast.tone === "alarm") return;
    const gone = window.setTimeout(() => setToast(null), 15_000);
    return () => window.clearTimeout(gone);
  }, [toast, toastHeld]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  // Asked again each time Patterns opens, and on Try again: one failed
  // check does not leave the subject off until the page is reloaded.
  const childChecks = useRef(0);
  const checkChildAvailable = useCallback(() => {
    if (!hasModule("pattern_analysis")) return;
    const mine = ++childChecks.current;
    api
      .childAvailable()
      .then((r) => {
        if (mine !== childChecks.current) return;
        setClaimsAvailable(Boolean(r.available));
        setChildCheckError(null);
      })
      .catch((e: unknown) => {
        if (mine !== childChecks.current) return;
        setClaimsAvailable(false);
        setChildCheckError(describe(e));
      });
  }, []);
  const onPatterns = screen === "patterns";
  useEffect(() => {
    if (onPatterns) checkChildAvailable();
  }, [checkChildAvailable, onPatterns]);

  const refreshState = useCallback(async () => {
    const next = await api.state();
    setState(next);
    return next;
  }, []);

  useEffect(() => {
    // The app opens on Patterns, or where the person last was: patterns come
    // from the pattern store, which needs no read of a database first.
    refreshState().catch((err) => setError(describe(err)));
  }, [refreshState]);

  useEffect(() => {
    if (state) remember({ screen, genTab, setupTab, layoutsTab, sourceTab });
  }, [state, screen, genTab, setupTab, layoutsTab, sourceTab]);

  // The database load: Generate's Database tab.
  const dbRun = screen === "generate" && genTab === "database";
  // A database's columns: Database setup's Profile and Map tabs.
  const columns = screen === "setup" && (setupTab === "profile" || setupTab === "map");

  /** Load whatever the chosen screen needs, once. */
  useEffect(() => {
    if (!state) return;
    const wants = async () => {
      try {
        if (columns && !schema) setSchema(await api.schema());
        // Editing is available from more than one screen, so the pieces it
        // needs are loaded once and kept.
        if (!catalog) setCatalog(await api.catalog());
        if (!generators) setGenerators(await api.generators());
        if (dbRun && !ratioShapes) setRatioShapes(await api.ratioShapes());
        if (dbRun && !plan) setPlan(await api.getPlan());
        if (dbRun && !schema) setSchema(await api.schema());
        if (dbRun && !run) setRun(await api.run());
      } catch (err) {
        setError(describe(err));
      }
    };
    void wants();
  }, [columns, dbRun, state, schema, plan, catalog, generators, ratioShapes, run]);

  async function readDatabase() {
    setError(null);
    setReadResults({});
    const stages: [string, () => Promise<unknown>][] = [
      ["Discovering schema", api.discover],
      ["Profiling data", api.profile],
      ["Mapping to catalog", api.map],
      ["Proposing rules", api.review],
    ];
    const keys = ["discover", "profile", "map", "review"] as const;

    try {
      await api.connect();
      for (let index = 0; index < stages.length; index += 1) {
        const [label, call] = stages[index];
        setRunning(label);
        const result = await call();
        setReadResults((prev) => ({ ...prev, [keys[index]]: result }));
        await refreshState();
      }
      setSchema(null);
      setPlan(null);
      setScreen("setup");
      setSetupTab("profile");
    } catch (err) {
      setError(describe(err));
    } finally {
      setRunning(null);
    }
  }

  async function buildPlan() {
    setBusy("plan");
    setError(null);
    try {
      setPlan(await api.buildPlan());
      // Sample rows and checks of the plan before say nothing about this one.
      setPreview(null);
      setGenerated(null);
      setValidation(null);
      await refreshState();
    } catch (err) {
      setError(describe(err));
      // Not tried again by itself: the error says what to fix first.
      planFailed.current = true;
    } finally {
      setBusy(null);
    }
  }

  // The plan builds itself. Arriving at the database load with no plan, or one
  // built before a rule, a pattern or a count changed, meant going back to
  // press a button whose only purpose was to be pressed; now it is done on
  // arrival, and the button stays for rebuilding on purpose.
  const planFailed = useRef(false);
  useEffect(() => {
    if (!dbRun) planFailed.current = false;
  }, [dbRun]);
  const reviewedNow = state?.steps.find((s) => s.id === "map")?.done ?? false;
  useEffect(() => {
    if (!dbRun || !reviewedNow || !plan || busy || planFailed.current) return;
    // No database load here, so no plan for one.
    if (state?.databaseGeneration === false) return;
    if (!plan.built || plan.stale) void buildPlan();
    // buildPlan is recreated every render and changes nothing this depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dbRun, reviewedNow, plan, busy]);

  async function changeRoot(table: string, count: number) {
    if (!state) return;
    if (!Number.isSafeInteger(count) || count < 1) {
      setError(`The record count for ${table} must be a whole number of 1 or more.`);
      return;
    }
    const rootCounts = { ...state.settings.rootCounts, [table]: count };
    setBusy("plan");
    // A previous failure is not news once the retry is under way.
    setError(null);
    try {
      await api.saveSettings({ rootCounts });
      setPlan(await api.buildPlan());
      await refreshState();
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(null);
    }
  }

  /**
   * Replace the measured children-per-parent shape, or put it back.
   *
   * The whole map is sent because settings merge one key at a time, so a
   * partial map would drop every other table's shape.
   */
  async function changeRatio(table: string, setting: RatioSetting | null) {
    if (!state) return;
    const ratios = { ...(state.settings.ratios ?? {}), [table]: setting };
    setBusy("plan");
    setSaveError(null);
    try {
      await api.saveSettings({ ratios });
      setPlan(await api.buildPlan());
      await refreshState();
    } catch (err) {
      setSaveError(describe(err));
      throw err;
    } finally {
      setBusy(null);
    }
  }

  /**
   * Choose which parent a table is counted against.
   *
   * A table with several parents is driven by the deepest one by default,
   * because that is the most specific: a claim belongs to a member far more
   * than it belongs to a product. Where that reading is wrong, this is where
   * it is corrected.
   *
   * The stated ratio goes with it. A ratio is children-per-*this*-parent, so
   * one measured against members is not a number about products, and keeping
   * it would silently apply it to a relationship it never described.
   */
  async function changeDrivingParent(table: string, parent: string) {
    if (!state) return;
    const drivingParents = { ...(state.settings.drivingParents ?? {}), [table]: parent };
    const ratios = { ...(state.settings.ratios ?? {}) };
    delete ratios[table];
    setBusy("plan");
    setSaveError(null);
    try {
      await api.saveSettings({ drivingParents, ratios });
      setPlan(await api.buildPlan());
      await refreshState();
    } catch (err) {
      setSaveError(describe(err));
      throw err;
    } finally {
      setBusy(null);
    }
  }

  async function changeMode(table: string, mode: string) {
    if (!state) return;
    const modes = { ...state.settings.modes, [table]: mode };
    setBusy("plan");
    // A previous failure is not news once the retry is under way.
    setError(null);
    try {
      await api.saveSettings({ modes });
      setPlan(await api.buildPlan());
      await refreshState();
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(null);
    }
  }

  /**
   * Record one decision about one column and refetch what it changed.
   *
   * Any column can be decided, not only the ones in the queue: a rule the tool
   * worked out is still a proposal.
   */
  async function record(decision: DecisionInput) {
    setBusy("decide");
    setSaveError(null);
    setError(null);
    try {
      await api.decide(decision);
      setSchema(await api.schema());
      // A decision changes the plan, so the one on screen is now stale.
      setPlan(null);
      await refreshState();
    } catch (err) {
      setSaveError(describe(err));
      throw err;
    } finally {
      setBusy(null);
    }
  }

  async function action<T>(name: string, call: () => Promise<T>, keep: (v: T) => void) {
    setBusy(name);
    setError(null);
    try {
      keep(await call());
      if (name !== "preview") {
        setRun(await api.run());
        await refreshState();
      }
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(null);
    }
  }

  if (!state) {
    return (
      <div className="boot">
        {error ? <Banner tone="alarm">{error}</Banner> : <span className="boot__spin" />}
      </div>
    );
  }

  const reviewed = state.steps.find((s) => s.id === "map")?.done ?? false;
  // Off where this installation is to make files only.
  const dbLoad = state.databaseGeneration !== false;
  // Rows in the database from a run of the plan on screen, rather than from a
  // run of an earlier plan the person has since changed.
  const finished = Date.parse(run?.manifest?.run.finished ?? "");
  const created = Date.parse(plan?.meta?.created ?? "");
  const runOfThisPlan =
    Boolean(run?.hasRun) && !Number.isNaN(finished) && !Number.isNaN(created) && finished >= created;

  // Database setup: the target's name once it is connected.
  const connected = state.steps.find((s) => s.id === "connect")?.done ?? false;
  // Arriving at Generate opens Files on its first format, as it always has;
  // History asks for X12 EDI.
  const goGenerate = (tab: GenTab, format: FileTab = "layouts") => {
    setTool(null);
    setScreen("generate");
    setGenTab(tab);
    setFileTab(format);
  };
  const goSetup = (tab: SetupTab) => {
    setTool(null);
    setScreen("setup");
    setSetupTab(tab);
  };
  // The tab whose panel the page shows, where the screen has tabs.
  const tabbed =
    screen === "generate" ? tabId("Generate", genTab)
      : screen === "layouts" ? tabId("Layouts and samples", layoutsTab)
        : screen === "setup" ? tabId("Database setup", setupTab)
          : screen === "source" ? tabId("Source setup", sourceTab)
            : null;
  const actions: AppActions = {
    target: state.database,
    targetReady: connected && Boolean(state.database),
    notify: setToast,
    goHistory: (runId) => {
      setToast(null);
      setTool(null);
      setHistoryFocus(runId ?? null);
      setScreen("history");
    },
    openFiles: (pattern) => {
      setToast(null);
      setFilePattern(pattern);
      goGenerate("files");
    },
    // A load from Patterns changed the rules, the plan and the rows: what
    // Generate › Database shows is read again when it is next opened.
    databaseChanged: () => {
      setPlan(null);
      setRun(null);
      setPreview(null);
      setGenerated(null);
      setValidation(null);
      setCleaned(null);
      void refreshState();
    },
  };

  return (
    <DatabaseGeneration.Provider value={dbLoad}>
    <AppActionsContext.Provider value={actions}>
    <div className="app">
      <Rail
        active={screen}
        onSelect={(next) => {
          setTool(null);
          setToast(null);
          setHistoryFocus(null);
          if (next !== screen) setFileTab("layouts");
          setScreen(next);
        }}
        details={{ setup: connected ? state.database : "Not connected" }}
        tool={tool}
        onTool={setTool}
        database={state.database}
        server={state.server}
        onDatabase={() => goSetup("connection")}
        theme={theme}
        onToggleTheme={() => setTheme(theme === "light" ? "dark" : "light")}
      />

      <main className="app__main">
        <header className="app__bar">
          <h1 className="app__heading">{tool ? TOOLS[tool] : title(screen)}</h1>
          {tool && (
            <div className="app__bar-actions">
              <Button onClick={() => setTool(null)}>{`← Back to ${title(screen)}`}</Button>
            </div>
          )}
          {!tool && (
          <div className="app__bar-actions">
            {/* Setting up a database goes in order: each tab offers the next,
                and Connection does once the database is read. */}
            {screen === "setup" && (setupTab !== "connection" || reviewed) && (
              <Button
                variant="primary"
                onClick={() => {
                  if (setupTab === "connection") setSetupTab("profile");
                  else if (setupTab === "profile") setSetupTab("map");
                  else goGenerate("database");
                }}
              >
                {SETUP_NEXT[setupTab]}
              </Button>
            )}
          </div>
          )}
        </header>

        <div className="app__body">
          {tool === "configure" && (
            <>
              <DiscoveryConfig />
              <ReportFieldsConfig />
            </>
          )}
          {tool === "algorithms" && <Algorithms />}
          {tool === "approval" && <LayoutApproval />}
          {!tool && (
          <>
          {screen === "generate" && (
            <PageTabs label="Generate" tabs={GEN_TABS} active={genTab} onSelect={setGenTab} />
          )}
          {screen === "layouts" && (
            <PageTabs label="Layouts and samples" tabs={LAYOUTS_TABS} active={layoutsTab}
              onSelect={setLayoutsTab} />
          )}
          {screen === "setup" && (
            <PageTabs label="Database setup" tabs={SETUP_TABS} active={setupTab}
              onSelect={setSetupTab} />
          )}
          {screen === "source" && (
            <PageTabs label="Source setup" tabs={SOURCE_TABS} active={sourceTab}
              onSelect={setSourceTab} />
          )}

          <div
            className="app__panel"
            id={PAGE_PANEL}
            role={tabbed ? "tabpanel" : undefined}
            aria-labelledby={tabbed ?? undefined}
          >

          {error && !(screen === "setup" && setupTab === "connection") && (
            <Banner tone="alarm">{error}</Banner>
          )}
          {notice && dbRun && <Banner tone="affirm">{notice}</Banner>}

          {screen === "layouts" && layoutsTab === "layouts" && (
            <FileMapping onRules={() => setTool("algorithms")} onHand={handOver} />
          )}
          {screen === "layouts" && layoutsTab === "folder" && <FileSource />}
          {screen === "layouts" && layoutsTab === "profile" && <FilesProfile />}

          {screen === "setup" && setupTab === "connection" && (
            <Setup
              database={state.database}
              referenceDatabase={state.referenceDatabase}
              landscape={state.landscape ?? state.database}
              server={state.server}
              steps={state.steps}
              running={running}
              error={error}
              results={readResults}
              onRead={readDatabase}
              isProtected={state.workingProtected === true}
              onPatterns={
                hasModule("pattern_analysis") ? () => setScreen("patterns") : undefined
              }
              // Only the header facts change. Re-reading the source would be
              // minutes of work for a question nobody asked.
              onTargetChanged={() => void refreshState()}
            />
          )}

          {columns &&
            (schema ? (
              <Schema
                lens={setupTab === "map" ? "map" : "discover"}
                schema={schema}
                catalog={catalog}
                generators={generators}
                saving={busy === "decide"}
                saveError={saveError}
                onDecide={record}
              />
            ) : (
              <NotYet reviewed={reviewed} onGo={() => setSetupTab("connection")} />
            ))}

          {screen === "source" && <SourceSetup tab={sourceTab} onTab={setSourceTab} />}

          {screen === "history" && (
            <GenerationRequests
              focus={historyFocus}
              onUse={(chosen) => {
                // More files for a run's people: X12 EDI, written for that run.
                setFilePattern(chosen);
                goGenerate("files", "edi");
              }}
            />
          )}

          {screen === "patterns" && !hasModule("pattern_analysis") && (
            <Empty
              title="Pattern analysis is not offered yet"
              action={
                <Button variant="primary" onClick={() => goGenerate("files")}>
                  Generate file
                </Button>
              }
            >
              The {packName()} business pack does not offer pattern analysis yet. Files are
              generated from the database's plan under Generate.
            </Empty>
          )}

          {screen === "patterns" && hasModule("pattern_analysis") && (
            <Patterns
              onConfigure={() => setTool("configure")}
              domain={domain}
              onDomain={setDomain}
              claimsAvailable={claimsAvailable}
              childCheckError={childCheckError}
              onRetryChildCheck={checkChildAvailable}
              onApplied={(destination, pattern, note) => {
                setNotice(note ?? null);
                // A pattern is applied in order to produce something. Taking
                // the person to the thing they chose beats telling them where
                // to go next.
                if (destination === "file") {
                  // Nothing was written: the file reads the pattern itself.
                  setFilePattern(pattern ?? null);
                  goGenerate("files");
                  return;
                }
                goGenerate("database");
                if (destination === "db") setDbTab("plan");
                // The pattern wrote review decisions, so the plan on screen is
                // now stale. Clearing it makes the Plan screen rebuild rather
                // than show a cascade the decisions no longer describe.
                setPlan(null);
                void refreshState();
              }}
            />
          )}

          {dbRun && !dbLoad && (
            <Empty
              title="Load to database is off"
              action={
                <Button variant="primary" onClick={() => setGenTab("files")}>
                  Generate file
                </Button>
              }
            >
              This installation generates files only.
            </Empty>
          )}

          {dbRun && dbLoad && (
            <GenerationSteps
              plan={plan}
              planning={busy === "plan"}
              preview={preview !== null}
              written={generated !== null || runOfThisPlan}
              checked={validation !== null || (runOfThisPlan && Boolean(run?.manifest?.validation))}
              tab={dbTab}
              onPlan={() => setDbTab("plan")}
              onRows={() => setDbTab("generate")}
            />
          )}

          {dbRun && dbLoad && dbTab === "plan" &&
            (plan?.built ? (
              <>
                <Cascade
                  plan={plan}
                  schema={schema}
                  settings={state.settings}
                  busy={busy === "plan"}
                  ratioShapes={ratioShapes}
                  saveError={saveError}
                  onChangeRoot={changeRoot}
                  onChangeMode={changeMode}
                  onChangeRatio={changeRatio}
                  onChangeDrivingParent={changeDrivingParent}
                  onRebuild={buildPlan}
                  onOpenPreview={() => {
                    setDbTab("generate");
                    void action("preview", () => api.dryRun(6), setPreview);
                  }}
                />
                <div className="app__divider" />
                <PlanView plan={plan} />
              </>
            ) : busy === "plan" ? (
              <Empty title="Building plan…">
                Resolving each column to a single rule and calculating the row
                count for each table.
              </Empty>
            ) : (
              <Empty
                title={reviewed ? "No plan yet" : "Schema not discovered"}
                action={
                  reviewed ? (
                    <Button variant="primary" onClick={buildPlan} busy={busy === "plan"}>
                      Build plan
                    </Button>
                  ) : (
                    <Button variant="primary" onClick={() => goSetup("connection")}>
                      Go to Database setup
                    </Button>
                  )
                }
              >
                {reviewed
                  ? "Resolve each column to a single rule and calculate the row count for each table."
                  : "Discover and profile the schema before building a plan."}
              </Empty>
            ))}

          {screen === "generate" && genTab === "files" && (
            <FileGeneration pattern={filePattern} onPattern={setFilePattern}
              planRun={state.databaseGeneration !== false} tab={fileTab} onTab={setFileTab}
              handed={handed} onHand={handOver} onHanded={handedOver} />
          )}

          {dbRun && dbLoad && dbTab === "generate" &&
            (plan?.built ? (
              <Run
                plan={plan}
                run={run}
                preview={preview}
                generated={generated}
                validation={validation}
                cleaned={cleaned}
                busy={busy}
                error={null}
                onPreview={() => action("preview", () => api.dryRun(6), setPreview)}
                onGenerate={() =>
                  action("generate", api.generate, ((value: GenerateResult) => {
                    setGenerated(value);
                    setValidation(null);
                    setCleaned(null);
                  }))
                }
                onValidate={() =>
                  action("validate", api.validate, setValidation)
                }
                onCleanup={() =>
                  action("cleanup", api.cleanup, ((value: CleanupResult) => {
                    setCleaned(value);
                    setGenerated(null);
                    setValidation(null);
                  }))
                }
              />
            ) : (
              <Empty title="No plan yet">
                Build a plan before loading the database.

              </Empty>
            ))}
          </div>
          </>
          )}
        </div>
      </main>
      {toast && (
        <div
          className={"toast" + (toast.tone === "alarm" ? " toast--alarm" : "")}
          role={toast.tone === "alarm" ? "alert" : "status"}
          onMouseEnter={() => setToastHeld(true)}
          onMouseLeave={() => setToastHeld(false)}
          onFocus={() => setToastHeld(true)}
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
              setToastHeld(false);
            }
          }}
        >
          <span className="toast__message">{toast.message}</span>
          {toast.action && (
            <button type="button" className="toast__action" onClick={toast.action.run}>
              {toast.action.label}
            </button>
          )}
          <button
            type="button"
            className="toast__close"
            aria-label="Dismiss"
            onClick={() => {
              setToast(null);
              setToastHeld(false);
            }}
          >
            ×
          </button>
        </div>
      )}
    </div>
    </AppActionsContext.Provider>
    </DatabaseGeneration.Provider>
  );
}

/** The id of a job's tab, and of the panel its tabs control. */
const tabId = (label: string, id: string) =>
  `pagetab-${label}-${id}`.toLowerCase().replace(/[^a-z0-9-]+/g, "-");
const PAGE_PANEL = "pagepanel";

/** The tabs a job's screen holds, above everything on it. Left and Right,
 *  Home and End move between them, as tabs do. */
function PageTabs<T extends string>({
  label, tabs, active, onSelect,
}: {
  label: string;
  tabs: [T, string][];
  active: T;
  onSelect: (tab: T) => void;
}) {
  const move = (event: React.KeyboardEvent<HTMLElement>) => {
    const at = tabs.findIndex(([id]) => id === active);
    const to =
      event.key === "ArrowRight" ? (at + 1) % tabs.length
        : event.key === "ArrowLeft" ? (at - 1 + tabs.length) % tabs.length
          : event.key === "Home" ? 0
            : event.key === "End" ? tabs.length - 1
              : -1;
    if (to < 0) return;
    event.preventDefault();
    onSelect(tabs[to][0]);
    requestAnimationFrame(() => document.getElementById(tabId(label, tabs[to][0]))?.focus());
  };
  return (
    <nav className="tabs pagetabs" role="tablist" aria-label={label} onKeyDown={move}>
      {tabs.map(([id, name]) => (
        <button
          key={id}
          id={tabId(label, id)}
          role="tab"
          aria-selected={active === id}
          aria-controls={PAGE_PANEL}
          tabIndex={active === id ? 0 : -1}
          className={"tabs__tab" + (active === id ? " tabs__tab--on" : "")}
          onClick={() => onSelect(id)}
        >
          {name}
        </button>
      ))}
    </nav>
  );
}

function NotYet({ reviewed, onGo }: { reviewed: boolean; onGo: () => void }) {
  return (
    <Empty
      title="Schema not discovered"
      action={
        <Button variant="primary" onClick={onGo}>
          Go to Connection
        </Button>
      }
    >
      {reviewed
        ? "Loading schema…"
        : "Connect to a database to discover and profile its schema."}
    </Empty>
  );
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

/**
 * The database load, as the four things it is, in order.
 *
 * It was two tabs -- the plan, and writing the rows -- with nothing to say
 * which had been done. Each step here says whether it is done, and the one
 * to do next is marked, so the way through reads straight down the strip.
 */
function GenerationSteps({
  plan, planning, preview, written, checked, tab, onPlan, onRows,
}: {
  plan: PlanPayload | null;
  planning: boolean;
  preview: boolean;
  written: boolean;
  checked: boolean;
  tab: "plan" | "generate";
  onPlan: () => void;
  onRows: () => void;
}) {
  const planned = Boolean(plan?.built) && !plan?.stale && !planning;
  const steps: { label: string; done: boolean; detail: string; go: () => void }[] = [
    {
      label: "Plan",
      done: planned,
      detail: planning
        ? "building…"
        : planned
          ? `${(plan?.totalRows ?? 0).toLocaleString()} rows`
          : "not built",
      go: onPlan,
    },
    { label: "Preview", done: preview, detail: preview ? "reviewed" : "", go: onRows },
    { label: "Load", done: written, detail: written ? "loaded" : "", go: onRows },
    { label: "Validate", done: checked, detail: checked ? "validated" : "", go: onRows },
  ];
  const next = steps.findIndex((step) => !step.done);
  const here = tab === "plan" ? 0 : Math.max(1, next === -1 ? 3 : next);

  return (
    <ol className="gensteps" aria-label="Database load steps">
      {steps.map((step, index) => (
        <li key={step.label}>
          <button
            className={
              "gensteps__step" +
              (step.done ? " gensteps__step--done" : "") +
              (index === here ? " gensteps__step--here" : "") +
              (index === next ? " gensteps__step--next" : "")
            }
            aria-current={index === here ? "step" : undefined}
            onClick={step.go}
          >
            <span className="gensteps__n">{step.done ? "✓" : index + 1}</span>
            <span className="gensteps__label">{step.label}</span>
            {step.detail && <span className="gensteps__detail">{step.detail}</span>}
          </button>
        </li>
      ))}
    </ol>
  );
}
