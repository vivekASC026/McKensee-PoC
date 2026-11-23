/**
 * A file from a sample: the file is the sample, except the places a config names.
 *
 * The format (X12, FHIR, HL7 v2, CDA) is read off the sample; nothing here depends on it
 * beyond the facts the server sends back (kind, summary, subject, validation).
 *
 * Two parts, top to bottom, the way a person goes through it:
 *
 * 1. A sample uploaded is read at once against the standard configuration of
 *    its format (or a base chosen: a saved config, a masking config file).
 *    Every position is listed with the rule proposed for it; Accept all takes
 *    the suggestions, and Generate file writes from what is on screen with
 *    nothing saved (the run keeps it, so Rerun writes the same file). Save
 *    configuration as keeps it under a name; a master is never changed.
 * 2. Pick a saved config and write the file: an 834 is new members, an 837 is
 *    claims for the members of the pattern chosen above. A saved config is the
 *    person's own: Edit configuration opens it in step 1, where Save changes
 *    writes it back in place, and Delete (confirmed) removes it.
 *
 * On the FHIR, HL7 v2 and CDA tabs (`family`) the screen opens on that
 * format's standard file -- a made-up sample and its master config, as the
 * EDI tab opens on a transaction set -- and lists only that format's configs.
 * A sample of the customer's own replaces the standard one.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Banner, Button, Chip } from "../components/primitives";
import {
  ApiError,
  api,
  type FilePattern,
  type GenConfigSummary,
  type HandedSample,
  type SampleDefault,
  type SampleElement,
  type SamplePosition,
  type SampleVary,
  type SampleReading,
  type SnipReport,
} from "../lib/api";
import { SnipResult } from "./EdiFormats";
import "./EdiFormats.css";
import { isChild, isPrimary, keyOf, linesOf, termsOf, words } from "../lib/pack";

const PREVIEW_LINES = 100;
// Records asked for to fill the preview: enough for 100 lines of any format.
const PREVIEW_RECORDS = 60;
const COPY = "";
// What a format's checks are called on the button.
// The checks read the start of the file, so they answer at once on a large one.
const CHECKED_ROWS = 200;
const CHECKS: Record<string, string> = { snip: "SNIP", fhir: "FHIR", cda: "HL7 v3", hl7v2: "HL7 v2" };

interface Props {
  extra: Record<string, unknown>;
  pattern: FilePattern | null;
  /** One format only (fhir, hl7v2, cda), opened on its standard file. */
  family?: string;
  /** A sample uploaded on another screen, to read here at once. */
  handed?: HandedSample | null;
  onHanded?: () => void;
}

// What each format's tab says the file is.
const FAMILY_WORDS: Record<string, string> = {
  fhir:
    "a FHIR R4 Bundle (members: Patient and Coverage; claims: Claim and Encounter) or a standalone resource (Claim, Coverage, Encounter, Condition, Observation, Practitioner, Organization, RelatedPerson)",
  hl7v2:
    "an HL7 v2 message (ADT admission, DFT charges, ORU lab results, ORM orders, SIU appointments, VXU immunizations, MDM documents, ACK acknowledgments)",
  cda: "an HL7 v3 C-CDA document (CCD) or HL7 v3 message (patient registry add or revise: PRPA_IN201301UV02, PRPA_IN201302UV02)",
  x12: "an X12 834, 835 or 837",
};
const STANDARD = "std:";
// The standard configuration of whatever format the sample is.
const AUTO = "auto";
const STALE = "This page is newer than the server. Restart SDM and reload the page.";

function message(e: unknown): string {
  return e instanceof ApiError ? e.message : String(e);
}

export function FromSample({ extra, pattern, family, handed, onHanded }: Props) {
  // What the business pack calls the records, as this tab says them.
  const P = words("primary");
  const C = words("child");
  const [allConfigs, setConfigs] = useState<GenConfigSummary[]>([]);
  const configs = useMemo(
    () => (family ? allConfigs.filter((c) => c.format === family) : allConfigs),
    [allConfigs, family],
  );
  const [defaults, setDefaults] = useState<SampleDefault[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  // 1. Modeling a config on a sample.
  const [sample, setSample] = useState<{ name: string; text: string } | null>(null);
  const [master, setMaster] = useState<{ name: string; text: string } | null>(null);
  // auto | file | none | <config id> | std:<standard file id>
  const [startFrom, setStartFrom] = useState<string>(family ? "" : AUTO);
  const [reading, setReading] = useState<SampleReading | null>(null);
  const [rows, setRows] = useState<SamplePosition[]>([]);
  const [vary, setVary] = useState<SampleVary[]>([]);
  const [adding, setAdding] = useState("");
  const [everything, setEverything] = useState(false);
  const [name, setName] = useState("");
  const [saved, setSaved] = useState<string | null>(null);
  const sampleInput = useRef<HTMLInputElement>(null);
  const masterInput = useRef<HTMLInputElement>(null);
  // Only the latest reading is shown: one asked for earlier may answer later.
  const readings = useRef(0);

  // 2. Writing from a saved config.
  const [chosen, setChosen] = useState<number | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [report, setReport] = useState<SnipReport | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const loadConfigs = useCallback(async () => {
    try {
      const payload = await api.sampleConfigs();
      setConfigs(payload.configs);
      return payload.configs;
    } catch (e) {
      setError(message(e));
      return [];
    }
  }, []);

  useEffect(() => {
    void loadConfigs();
  }, [loadConfigs]);

  useEffect(() => {
    let live = true;
    api
      .sampleDefaults()
      .then((payload) => {
        if (!live) return;
        const mine = payload.defaults.filter((d) => !family || d.format === family);
        setDefaults(mine);
        // A format's tab opens on its first standard file, read at once.
        if (family && mine.length) setStartFrom((now) => now || STANDARD + mine[0].id);
        if (family && !mine.length) setError(STALE);
      })
      .catch(() => {
        if (!live) return;
        setDefaults([]);
        // The page is newer than the SDM serving it: the standard files came
        // with this version, so an SDM started before it has none to offer.
        if (family) setError(STALE);
      });
    return () => {
      live = false;
    };
  }, [family]);

  // A sample of one's own is read against its format's standard, unless a
  // saved config is being changed. A configuration file chosen for an earlier
  // sample (perhaps of another format) is dropped; choose one again after.
  const takeSample = useCallback((taken: { name: string; text: string }) => {
    setSample(taken);
    setStartFrom((now) => {
      if (/^\d+$/.test(now)) return now;
      setMaster(null);
      return AUTO;
    });
  }, []);

  // Uploaded on another screen: read here as if uploaded here.
  useEffect(() => {
    if (!handed) return;
    takeSample({ name: handed.name, text: handed.text });
    onHanded?.();
  }, [handed, onHanded, takeSample]);

  const readFile = (file: File, set: (v: { name: string; text: string }) => void) => {
    file
      .text()
      .then((text) => set({ name: file.name, text }))
      .catch((e) => setError(`Couldn't read ${file.name}: ${message(e)}`));
  };

  const read = useCallback(async () => {
    const mine = ++readings.current;
    setBusy("read");
    setError(null);
    setSaved(null);
    // What is on screen is the last reading: cleared, so nothing is chosen on
    // it in the moment before this one's places arrive and replace it.
    setReading(null);
    setRows([]);
    setVary([]);
    try {
      const standard = startFrom.startsWith(STANDARD) ? startFrom.slice(STANDARD.length) : null;
      const fromSaved = /^\d+$/.test(startFrom);
      const result = await api.sampleRead({
        sample: sample?.text ?? null,
        sampleName: sample?.name ?? null,
        master: startFrom === "file" ? (master?.text ?? null) : null,
        masterName: startFrom === "file" ? (master?.name ?? null) : null,
        configId: fromSaved ? Number(startFrom) : null,
        defaultId: standard,
        auto: startFrom === AUTO,
      });
      if (mine !== readings.current) return;
      setReading(result);
      setRows(result.rows);
      setVary(result.vary ?? []);
      const base = (result.sampleName ?? "sample").replace(/\.[^.]+$/, "");
      // A saved config read back keeps its own name, ready for Save changes.
      const own = fromSaved ? configs.find((c) => c.id === Number(startFrom)) : undefined;
      setName(own ? own.name : `${result.kind} ${base}`);
    } catch (e) {
      if (mine !== readings.current) return;
      setError(message(e));
      setReading(null);
    } finally {
      if (mine === readings.current) setBusy(null);
    }
  }, [sample, master, startFrom, configs]);

  const choose = (index: number, algorithm: string) => {
    setSaved(null);
    setRows((current) =>
      current.map((r, i) => (i === index ? { ...r, algorithm: algorithm || null } : r)),
    );
  };

  const acceptSuggestions = () => {
    setSaved(null);
    setRows((current) => current.map((r) => ({ ...r, algorithm: r.algorithm ?? r.suggested })));
  };

  // Where the sample comes from when none of one's own was chosen: the saved
  // config or standard file read.
  const standardId = startFrom.startsWith(STANDARD) ? startFrom.slice(STANDARD.length) : null;
  const savedId = /^\d+$/.test(startFrom) ? Number(startFrom) : null;

  const save = useCallback(async () => {
    if (!reading) return;
    setBusy("save");
    setError(null);
    try {
      // The sample travels with the config: read from a saved config or a
      // standard file, it is that one's sample unless another was chosen.
      const result = await api.sampleConfigSave({
        name,
        sample: sample?.text ?? null,
        sampleName: reading.sampleName,
        basedOn: reading.basedOn,
        rows,
        vary,
        fromConfigId: savedId !== null && !sample ? savedId : null,
        fromDefaultId: standardId && !sample ? standardId : null,
      });
      setSaved(result.name);
      await loadConfigs();
      setChosen(result.id);
      setConfirmDelete(false);
      setPreview(null);
      setReport(null);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(null);
    }
  }, [reading, sample, savedId, standardId, name, rows, vary, loadConfigs]);

  // The saved config being changed, when step 1 was read from one.
  const editing = savedId !== null ? (configs.find((c) => c.id === savedId) ?? null) : null;

  const saveChanges = useCallback(async () => {
    if (!reading || !editing) return;
    setBusy("update");
    setError(null);
    try {
      const result = await api.sampleConfigUpdate(editing.id, {
        name,
        sample: sample?.text ?? null,
        sampleName: sample?.name ?? reading.sampleName,
        rows,
        vary,
      });
      setSaved(result.name);
      await loadConfigs();
      setChosen(result.id);
      setConfirmDelete(false);
      setPreview(null);
      setReport(null);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(null);
    }
  }, [reading, editing, sample, name, rows, vary, loadConfigs]);

  // Generate file from step 1: what is on screen, accepted and not saved. The
  // run keeps the configuration and facts about the sample, never an uploaded
  // sample's text: Rerun writes the same file when the sample came from a
  // standard file or a saved configuration (and is unchanged there), and says
  // it can't for an uploaded one.
  const writeUnsaved = useCallback(async () => {
    if (!reading) return;
    setBusy("write-unsaved");
    setError(null);
    try {
      const base = (reading.sampleName ?? "sample").replace(/\.[^.]+$/, "");
      // Recorded as a run, which the pattern's line above names.
      await api.exportDownload(
        "unsaved",
        reading.subject,
        `${reading.kind}-${base}${reading.extension ?? ".txt"}`.replace(/\s+/g, "_"),
        {
          ...extra,
          sample_config: {
            sample: sample?.text ?? null,
            sampleName: reading.sampleName,
            basedOn: reading.basedOn,
            rows,
            vary,
            fromConfigId: savedId !== null && !sample ? savedId : null,
            fromDefaultId: standardId && !sample ? standardId : null,
          },
        },
      );
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(null);
    }
  }, [reading, extra, sample, rows, vary, savedId, standardId]);

  const stepOne = useRef<HTMLDivElement>(null);
  // Opens a saved config in step 1, where its places are changed and saved back.
  const edit = (id: number) => {
    setSample(null);
    setSaved(null);
    setStartFrom(String(id));
    stepOne.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const config = configs.find((c) => c.id === chosen) ?? null;
  const format = config ? `sample:${config.id}` : "";
  const subject = config?.subject ?? keyOf("child");

  const showFirst = useCallback(async () => {
    if (!config) return;
    setBusy("preview");
    setError(null);
    try {
      setPreview(await api.exportText(format, subject, PREVIEW_RECORDS, extra));
    } catch (e) {
      setError(message(e));
      setPreview(null);
    } finally {
      setBusy(null);
    }
  }, [config, format, subject, extra]);

  const write = useCallback(async () => {
    if (!config) return;
    setBusy("write");
    setError(null);
    try {
      await api.exportDownload(format, subject, `${config.kind}-${config.name}${config.extension}`, extra);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(null);
    }
  }, [config, format, subject, extra]);

  const check = useCallback(async () => {
    if (!config) return;
    setBusy("check");
    setError(null);
    try {
      const exportRequest = { format, subject, rows: CHECKED_ROWS, ...extra };
      setReport(
        config.validation === "snip"
          ? await api.snipCheck(exportRequest, null)
          : await api.sampleCheck(exportRequest),
      );
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(null);
    }
  }, [config, format, subject, extra]);

  const remove = useCallback(async () => {
    if (!config) return;
    setBusy("remove");
    setError(null);
    try {
      await api.sampleConfigDelete(config.id);
      // Step 1 was changing the config just removed: there is nothing left to save to.
      if (startFrom === String(config.id)) {
        setStartFrom(family ? "" : AUTO);
        setReading(null);
        setRows([]);
      }
      setChosen(null);
      setConfirmDelete(false);
      setPreview(null);
      setReport(null);
      await loadConfigs();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(null);
    }
  }, [config, loadConfigs, startFrom, family]);

  const shown = useMemo(
    () =>
      rows
        .map((row, index) => ({ row, index }))
        .filter(({ row }) => everything || row.algorithm || row.config_name || row.suggested),
    [rows, everything],
  );
  const generated = rows.filter((r) => r.algorithm).length;
  // A place the format keeps as the sample has it (not the member's) needs no answer.
  const unanswered = rows.filter((r) => r.config_name && !r.algorithm && !r.kept).length;
  const suggestions = rows.filter((r) => !r.algorithm && r.suggested).length;
  // The master's own key for each place, shown only when the master has them.
  const configNames = rows.some((r) => r.config_name);
  const standardFile = standardId ? (defaults.find((d) => d.id === standardId) ?? null) : null;
  // The standard the sample was read against, when it was found for it.
  const detected =
    startFrom === AUTO && reading?.defaultId
      ? (defaults.find((d) => d.id === reading.defaultId) ?? null)
      : null;
  // Step 1 writes a file of its own while what it shows is not a saved config.
  const unsavedWrite = Boolean(reading) && !editing && saved === null && generated > 0;

  // Read as soon as there is something to read: a standard file or a saved
  // config chosen, a sample uploaded (against its format's standard, a
  // configuration file once one is chosen, or nothing). No Analyze step.
  useEffect(() => {
    const ready =
      startFrom.startsWith(STANDARD) ||
      /^\d+$/.test(startFrom) ||
      (sample !== null && (startFrom === AUTO || startFrom === "none" ||
        (startFrom === "file" && master !== null)));
    if (ready) {
      void read();
    } else {
      // Nothing to read yet under the base now chosen (a configuration file
      // not yet chosen, or no sample): the last reading was made under another
      // base, so it goes, with any reading still on its way.
      readings.current++;
      setBusy((now) => (now === "read" ? null : now));
      setReading(null);
      setRows([]);
      setVary([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startFrom, sample, master]);

  return (
    <section className="panel edi edi-sample">
      {error && <Banner tone="alarm">{error}</Banner>}

      <div className="edi-sample__step" ref={stepOne}>
        <h3>1. {family ? "Configure mapping" : "Sample file"}</h3>
        <p className="panel__hint">
          {family
            ? `The file is ${FAMILY_WORDS[family] ?? "the sample"}, modeled on the standard sample or your own. `
            : "X12 834, 835 or 837, FHIR, HL7 v2 or HL7 v3; the format is detected. "}
          Use a masked or synthetic sample, never a production file.
        </p>
        <div className="edi__pick">
          <label className="named">
            <span>Sample file</span>
            <Button
              variant={reading || family || config ? undefined : "primary"}
              onClick={() => sampleInput.current?.click()}
            >
              {sample
                ? sample.name
                : standardFile
                  ? `${standardFile.sampleName} (standard) · Upload your own…`
                  : "Upload sample…"}
            </Button>
            <input
              ref={sampleInput}
              type="file"
              hidden
              aria-label="Sample file"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) readFile(file, takeSample);
                e.target.value = "";
              }}
            />
          </label>
          {sample && family && defaults.length > 0 && (
            <Button
              onClick={() => {
                setSample(null);
                setStartFrom(STANDARD + defaults[0].id);
              }}
            >
              Use standard sample
            </Button>
          )}
          <label className="named">
            <span>{family ? "Master layout" : "Base configuration"}</span>
            <select
              aria-label={family ? "Master layout" : "Base configuration"}
              value={startFrom}
              onChange={(e) => setStartFrom(e.target.value)}
            >
              {startFrom === "" && <option value="">Select…</option>}
              {(sample || !family) && (
                <option value={AUTO}>
                  {detected ? `Standard: ${detected.name}` : "Standard for the sample's format"}
                </option>
              )}
              {defaults.map((d) => (
                <option key={d.id} value={STANDARD + d.id}>
                  Standard: {d.name}
                </option>
              ))}
              {configs.map((c) => (
                <option key={c.id} value={String(c.id)}>
                  Saved: {c.name}
                </option>
              ))}
              <option value="file">Configuration file (masking or generation)</option>
              <option value="none">None (suggestions only)</option>
            </select>
          </label>
          {startFrom === "file" && (
            <label className="named">
              <span>Configuration file</span>
              <Button onClick={() => masterInput.current?.click()}>
                {master ? master.name : "Choose configuration file…"}
              </Button>
              <input
                ref={masterInput}
                type="file"
                hidden
                aria-label="Configuration file"
                accept=".json"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) readFile(file, setMaster);
                  e.target.value = "";
                }}
              />
            </label>
          )}
          {busy === "read" && (
            <p className="panel__hint" role="status">
              Analyzing sample…
            </p>
          )}
        </div>

        {reading && (
          <>
            <div className="edi__facts">
              <Chip tone="catalog">
                {reading.kind} {reading.version}
              </Chip>
              <Chip tone="learned">{reading.summary}</Chip>
              {reading.basedOn && <Chip tone="user">Based on {reading.basedOn}</Chip>}
              <Chip tone={unanswered ? "caution" : "affirm"}>
                {generated} generated
                {unanswered ? ` · ${unanswered} need a rule` : ""}
              </Chip>
            </div>

            <div className="edi-sample__tools">
              {suggestions > 0 && (
                <>
                  <Chip tone="caution">
                    {suggestions} {suggestions === 1 ? "suggestion" : "suggestions"}
                  </Chip>
                  <Button onClick={acceptSuggestions}>Accept all</Button>
                </>
              )}
              <label className="check">
                <input
                  type="checkbox"
                  checked={everything}
                  onChange={(e) => setEverything(e.target.checked)}
                />{" "}
                Show all sample positions
              </label>
            </div>

            <table className="edi-sample__table">
              <thead>
                <tr>
                  <th>Position</th>
                  <th>Sample value</th>
                  {configNames && <th>Element</th>}
                  <th>Rule</th>
                </tr>
              </thead>
              <tbody>
                {shown.map(({ row, index }) => (
                  <tr key={index} className={row.algorithm ? "edi-sample__row--on" : ""}>
                    <td className="mono">{row.label}</td>
                    <td className="mono edi-sample__value">{row.example}</td>
                    {configNames && <td className="mono">{row.config_name ?? ""}</td>}
                    <td>
                      <select
                        aria-label={`Rule for ${row.label}`}
                        value={row.algorithm ?? COPY}
                        onChange={(e) => choose(index, e.target.value)}
                      >
                        <option value={COPY}>Copy from sample</option>
                        {row.suggested && (
                          <option value={row.suggested}>{row.suggested} (suggested)</option>
                        )}
                        {reading.algorithms
                          .filter((a) => a !== row.suggested)
                          .map((a) => (
                            <option key={a} value={a}>
                              {a}
                            </option>
                          ))}
                      </select>
                      <div className="edi-sample__why">{row.reason}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {reading.structure && (
              <VaryTable
                structure={reading.structure}
                vary={vary}
                onVary={(next) => {
                  setSaved(null);
                  setVary(next);
                }}
                adding={adding}
                onAdding={setAdding}
              />
            )}

            <div className="panel__actions">
              {unsavedWrite && (
                <Button variant="primary" onClick={writeUnsaved} busy={busy === "write-unsaved"}>
                  Generate file
                </Button>
              )}
              <label className="named">
                <span>{editing ? "Configuration name" : "New configuration name"}</span>
                <input value={name} onChange={(e) => setName(e.target.value)} />
              </label>
              {editing && (
                <Button
                  variant="primary"
                  onClick={saveChanges}
                  busy={busy === "update"}
                  disabled={!name.trim() || generated === 0}
                >
                  Save changes
                </Button>
              )}
              <Button
                onClick={save}
                busy={busy === "save"}
                disabled={!name.trim() || generated === 0}
              >
                Save configuration as
              </Button>
              {saved && <Chip tone="affirm">Saved as {saved}</Chip>}
            </div>
          </>
        )}
      </div>

      <div className="edi-sample__step">
        <h3>2. Generate from saved configuration</h3>
        {configs.length === 0 ? (
          <p className="panel__hint">No sample configurations saved.</p>
        ) : (
          <>
            <div className="edi__pick">
              <label className="named">
                <span>Sample configuration</span>
                <select
                  aria-label="Sample configuration"
                  value={chosen ?? ""}
                  onChange={(e) => {
                    setChosen(e.target.value ? Number(e.target.value) : null);
                    setConfirmDelete(false);
                    setPreview(null);
                    setReport(null);
                  }}
                >
                  <option value="">Select…</option>
                  {configs.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </label>
              {config && (
                <div className="edi__facts">
                  <Chip tone="catalog">
                    {config.kind} {config.version}
                  </Chip>
                  <Chip tone="learned">{config.positions} generated positions</Chip>
                  {config.based_on && <Chip tone="user">Based on {config.based_on}</Chip>}
                  {config.sample_name && <Chip tone="physical">Sample: {config.sample_name}</Chip>}
                </div>
              )}
            </div>
            {config && (
              <div className="panel__actions">
                <Button onClick={showFirst} busy={busy === "preview"}>
                  Preview
                </Button>
                <Button
                  variant={unsavedWrite || editing ? undefined : "primary"}
                  onClick={write}
                  busy={busy === "write"}
                >
                  Generate file
                </Button>
                {config.validation && (
                  <Button onClick={check} busy={busy === "check"}>
                    Run {CHECKS[config.validation] ?? config.validation} validation
                  </Button>
                )}
                <Button onClick={() => edit(config.id)}>Edit configuration</Button>
                {confirmDelete ? (
                  <>
                    <Button variant="danger" onClick={remove} busy={busy === "remove"}>
                      {`Delete ${config.name}`}
                    </Button>
                    <Button onClick={() => setConfirmDelete(false)}>Cancel</Button>
                  </>
                ) : (
                  <Button variant="danger" onClick={() => setConfirmDelete(true)}>
                    Delete configuration
                  </Button>
                )}
                <p className="panel__hint">
                  {pattern?.kind === "request"
                    ? isChild(config.subject) && isPrimary(pattern.entity)
                      ? `${C.Many} from the selected ${C.many} pattern for the ${P.many} of ${pattern.name}.`
                      : `The same ${termsOf(pattern.entity).many} as ${pattern.name}.`
                    : isPrimary(config.subject)
                    ? pattern
                      ? `New ${P.many} with the values of "${pattern.name}".`
                      : `New ${P.many} from the plan.`
                    : pattern
                      ? `${C.Many} for the existing ${P.many} of "${pattern.name}".`
                      : `${C.Many} from the plan.`}{" "}
                  All other values are copied from the sample.
                </p>
              </div>
            )}
          </>
        )}
        {report && <SnipResult report={report} />}
        {report && (
          <p className="panel__hint">
            Validated the first {CHECKED_ROWS} {linesOf(subject)} of the file.
          </p>
        )}
        {preview && (
          <pre className="edi__sample">
            {/* HL7 v2 ends a segment with a carriage return alone. */}
            {preview.split(/\r\n|\r|\n/).filter(Boolean).slice(0, PREVIEW_LINES).join("\n")}
          </pre>
        )}
      </div>
    </section>
  );
}

type OccursMode = "sample" | "exactly" | "range" | "mix";

function occursMode(v: SampleVary): OccursMode {
  if (v.mix && v.mix.length) return "mix";
  if (v.min === null) return "sample";
  return v.min === v.max ? "exactly" : "range";
}

function occursAs(mode: OccursMode, v: SampleVary): Partial<SampleVary> {
  const low = v.min ?? 1;
  if (mode === "sample") return { min: null, max: null, mix: null };
  if (mode === "exactly") return { min: low, max: low, mix: null };
  if (mode === "range") return { min: low, max: Math.max(low + 1, v.max ?? 3), mix: null };
  return { min: null, max: null, mix: v.mix?.length ? v.mix : [[1, 50], [2, 30], [3, 20]] };
}

/** "1:50, 2:30, 3:20": how many times, each in its share. */
export function MixInput({
  label, mix, onMix,
}: {
  label: string;
  mix: [number, number][] | null;
  onMix: (mix: [number, number][]) => void;
}) {
  const shown = (mix ?? []).map(([c, s]) => `${c}:${s}`).join(", ");
  const [text, setText] = useState(shown);
  useEffect(() => setText(shown), [shown]);
  const read = (value: string): [number, number][] | null => {
    const pairs = value.split(",").map((p) => p.trim()).filter(Boolean).map((p) => {
      const [c, s] = p.split(":").map((x) => Number(x.trim().replace("%", "")));
      return [c, s] as [number, number];
    });
    return pairs.length && pairs.every(([c, s]) => Number.isInteger(c) && c >= 0 && c <= 50 && s >= 0)
      ? pairs
      : null;
  };
  const bad = text.trim() !== "" && read(text) === null;
  return (
    <input
      className="mono"
      aria-label={label}
      aria-invalid={bad}
      placeholder="1:50, 2:30, 3:20"
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const parsed = read(e.target.value);
        if (parsed) onMix(parsed);
      }}
      title={bad ? "Enter count:percentage pairs, e.g., 1:50, 2:30, 3:20" : undefined}
    />
  );
}

/**
 * Elements that are in some documents and not others, or occur more often in
 * one than another. Without a row here every document has every element of
 * the sample, as often as the sample has it.
 */
function VaryTable({
  structure,
  vary,
  onVary,
  adding,
  onAdding,
}: {
  structure: SampleElement[];
  vary: SampleVary[];
  onVary: (next: SampleVary[]) => void;
  adding: string;
  onAdding: (at: string) => void;
}) {
  const known = new Map(structure.map((e) => [e.at, e]));
  const offered = structure.filter((e) => !vary.some((v) => v.at === e.at));
  const change = (index: number, patch: Partial<SampleVary>) =>
    onVary(vary.map((v, i) => (i === index ? { ...v, ...patch } : v)));
  const number = (text: string) => (text.trim() === "" ? null : Number(text));
  const add = () => {
    const element = known.get(adding.trim());
    if (!element || vary.some((v) => v.at === element.at)) return;
    onVary([
      ...vary,
      element.repeats
        ? { at: element.at, present: 100, min: 1, max: Math.max(3, element.count) }
        : { at: element.at, present: 70, min: null, max: null },
    ]);
    onAdding("");
  };
  return (
    <div className="edi-sample__vary">
      <h4>Optional and repeating elements</h4>
      <p className="panel__hint">
        A distribution such as 1:50, 2:30, 3:20 means once in 50% of documents, twice in 30%,
        three times in 20%.
      </p>
      {vary.length > 0 && (
        <table className="edi-sample__table">
          <thead>
            <tr>
              <th>Element</th>
              <th>% of documents</th>
              <th>Occurrences</th>
              <th />
              <th />
            </tr>
          </thead>
          <tbody>
            {vary.map((v, index) => (
              <tr key={v.at}>
                <td className="mono">
                  {v.at}
                  {known.has(v.at) && (
                    <div className="edi-sample__why">
                      {known.get(v.at)!.count === 1
                        ? "Once in sample"
                        : `Up to ${known.get(v.at)!.count} times in sample`}
                    </div>
                  )}
                </td>
                <td>
                  <input
                    type="number"
                    min={0}
                    max={100}
                    aria-label={`Share of documents with ${v.at}`}
                    value={v.present}
                    onChange={(e) => change(index, { present: Number(e.target.value) })}
                  />
                </td>
                <td>
                  <select
                    aria-label={`How many times ${v.at} occurs`}
                    value={occursMode(v)}
                    onChange={(e) => change(index, occursAs(e.target.value as OccursMode, v))}
                  >
                    <option value="sample">As in sample</option>
                    <option value="exactly">Fixed</option>
                    <option value="range">Range</option>
                    <option value="mix">Distribution</option>
                  </select>
                </td>
                <td>
                  {occursMode(v) === "exactly" && (
                    <input
                      type="number"
                      min={1}
                      max={50}
                      aria-label={`Exact count of ${v.at}`}
                      value={v.min ?? ""}
                      onChange={(e) => {
                        const n = number(e.target.value);
                        change(index, { min: n, max: n });
                      }}
                    />
                  )}
                  {occursMode(v) === "range" && (
                    <>
                      <input
                        type="number"
                        min={1}
                        max={50}
                        aria-label={`Fewest times ${v.at} occurs`}
                        value={v.min ?? ""}
                        onChange={(e) => change(index, { min: number(e.target.value) })}
                      />{" "}
                      to{" "}
                      <input
                        type="number"
                        min={1}
                        max={50}
                        aria-label={`Most times ${v.at} occurs`}
                        value={v.max ?? ""}
                        onChange={(e) => change(index, { max: number(e.target.value) })}
                      />
                    </>
                  )}
                  {occursMode(v) === "mix" && (
                    <MixInput
                      label={`Mix of times ${v.at} occurs`}
                      mix={v.mix ?? null}
                      onMix={(mix) => change(index, { mix })}
                    />
                  )}
                </td>
                <td>
                  <Button onClick={() => onVary(vary.filter((_, i) => i !== index))}>
                    Delete
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="edi-sample__tools">
        <label className="named">
          <span>Element</span>
          <input
            list="vary-elements"
            className="mono edi-sample__vary-pick"
            placeholder="Search elements (e.g., telecom)"
            aria-label="Element to make optional or repeat"
            value={adding}
            onChange={(e) => onAdding(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && add()}
          />
          <datalist id="vary-elements">
            {offered.map((e) => (
              <option key={e.at} value={e.at}>
                {e.repeats ? `Repeats, up to ${e.count} in sample` : "Once in sample"}
              </option>
            ))}
          </datalist>
        </label>
        <Button onClick={add} disabled={!known.has(adding.trim())}>
          Add
        </Button>
      </div>
    </div>
  );
}
