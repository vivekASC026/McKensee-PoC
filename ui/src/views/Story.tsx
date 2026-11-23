/**
 * Story to data.
 *
 * A requirement arrives as prose — a test case, a user story, a line in a
 * spreadsheet — and the question is which of the cohorts already found it is
 * asking for. The matching is done against *kept* patterns only, so this screen
 * says so plainly when nothing has been kept: an empty vocabulary matching
 * nothing looks identical to a story nobody understood, and the two want very
 * different responses.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Banner, Button, Chip, Empty, formatPercent } from "../components/primitives";
import {
  ApiError,
  api,
  type Domain,
  type FilePattern,
  type PatternCondition,
  type PatternFeature,
  type StoryMatchPayload,
  type StoryReading,
  type StoryToPlanPayload,
} from "../lib/api";
import { rulesToFilters } from "./DefinePattern";
import { PatternsAnalyze } from "./Patterns";
import { useDatabaseGeneration } from "../lib/databaseGeneration";
import { useAppActions } from "../lib/appActions";
import "./Story.css";
import { fromPatterns, termsOf } from "../lib/pack";

interface StoryProps {
  onApplied: (destination: "file" | "db", pattern?: FilePattern, note?: string) => void;
}

/**
 * A file of requirements: each one matched to the saved patterns it describes,
 * the matches added to the plan, or any one analyzed as the Find box would.
 */
export function RequirementsFile({ onAnalyze }: { onAnalyze: (story: string) => void }) {
  const dbLoad = useDatabaseGeneration();
  const { databaseChanged } = useAppActions();
  const [stories, setStories] = useState<string[]>([]);
  const [matches, setMatches] = useState<StoryMatchPayload | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [chosen, setChosen] = useState<Set<number>>(new Set());
  const [volume, setVolume] = useState(1000);
  const [planned, setPlanned] = useState<StoryToPlanPayload | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const upload = useCallback(async (file: File) => {
    setBusy("parse");
    setError(null);
    setNotice(null);
    setMatches(null);
    try {
      const payload = await api.storyParse(file);
      setStories(payload.stories);
      setFileName(file.name);
      if (payload.warning) setNotice(payload.warning);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setStories([]);
    } finally {
      setBusy(null);
    }
  }, []);

  const match = useCallback(async () => {
    setBusy("match");
    setError(null);
    try {
      const payload = await api.storyMatch(stories);
      setMatches(payload);
      setChosen(new Set());
      setPlanned(null);
      if (payload.warning) setNotice(payload.warning);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [stories]);

  const toPlan = useCallback(
    async (apply: boolean) => {
      setBusy("plan");
      setError(null);
      try {
        setPlanned(await api.storyToPlan([...chosen], volume, apply));
        if (apply) {
          // The plan on Generate › Database no longer holds every segment.
          databaseChanged();
          setNotice("Patterns added to the plan as segments.");
        }
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [chosen, volume, databaseChanged],
  );

  const toggle = useCallback((patternId: number) => {
    setChosen((previous) => {
      const next = new Set(previous);
      next.has(patternId) ? next.delete(patternId) : next.add(patternId);
      return next;
    });
    setPlanned(null);
  }, []);

  return (
    <div className="story">
      <section className="story__upload" aria-label="Requirements file">
        <input
          ref={input}
          type="file"
          accept=".txt,.csv,.xlsx,.json,.docx"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void upload(file);
          }}
          hidden
        />
        {(fileName || busy === "parse") && (
          <Button onClick={() => input.current?.click()} busy={busy === "parse"}>
            Upload file
          </Button>
        )}
        {fileName && (
          <span className="story__file">
            {fileName}: <strong className="num">{stories.length}</strong> requirements
          </span>
        )}
        {stories.length > 0 && (
          <Button onClick={match} busy={busy === "match"}>
            Match to saved patterns
          </Button>
        )}
      </section>

      {error && <Banner tone="alarm">{error}</Banner>}
      {notice && <Banner tone="caution">{notice}</Banner>}

      {!fileName && !busy && (
        <Empty
          title="No requirements file"
          action={
            <Button onClick={() => input.current?.click()}>Upload file</Button>
          }
        >
          Text, CSV, Excel, JSON or Word: each requirement is matched to saved patterns.
        </Empty>
      )}

      {stories.length > 0 && !matches && (
        <ul className="story__list">
          {stories.slice(0, 20).map((story, index) => (
            <li key={index} className="story__row">
              <span className="story__index num">{index + 1}</span>
              <span className="story__text">{story}</span>
              <Button onClick={() => onAnalyze(story)}>Find patterns</Button>
            </li>
          ))}
          {stories.length > 20 && (
            <li className="story__more">
              {stories.length - 20} more (all will be matched)
            </li>
          )}
        </ul>
      )}

      {matches && (
        <div className="story__matches">
          {matches.stories.map((entry) => (
            <article key={entry.index} className="match">
              <p className="match__story">
                {entry.story}{" "}
                <Button onClick={() => onAnalyze(entry.story)}>Find patterns</Button>
              </p>
              {entry.candidates.length === 0 ? (
                <p className="match__none">
                  No saved pattern matches this requirement.
                  {entry.keywords.length > 0 && (
                    <> Keywords: {entry.keywords.join(", ")}.</>
                  )}
                </p>
              ) : (
                <ul className="match__candidates">
                  {entry.candidates.map((candidate) => (
                    <li key={candidate.pattern_id}>
                      <label className="match__pick">
                        <input
                          type="checkbox"
                          checked={chosen.has(candidate.pattern_id)}
                          onChange={() => toggle(candidate.pattern_id)}
                        />
                        <span className="match__name">{candidate.name}</span>
                      </label>
                      <Chip
                        tone={candidate.match_score >= 0.6 ? "learned" : "caution"}
                        fill={candidate.match_score >= 0.6 ? "solid" : "outline"}
                      >
                        {formatPercent(candidate.match_score)} match
                      </Chip>
                      {candidate.scope_levels?.[0] && (
                        <span className="match__why">{candidate.scope_levels[0]}</span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </article>
          ))}
        </div>
      )}

      {matches && dbLoad && fromPatterns() && (
        <section className="story__plan">
          <h3>Add to plan</h3>
          <div className="story__controls">
            <label className="named">
              <span>Rows</span>
              <input
                type="number"
                min={1}
                value={volume}
                onChange={(e) => setVolume(Math.max(1, Number(e.target.value) || 1))}
              />
            </label>
            <Button
              onClick={() => toPlan(false)}
              busy={busy === "plan"}
              disabled={chosen.size === 0}
            >
              Allocate {volume.toLocaleString()} across {chosen.size || "no"}{" "}
              {chosen.size === 1 ? "pattern" : "patterns"}
            </Button>
            {planned && (
              <Button
                onClick={() => toPlan(true)}
                busy={busy === "plan"}
                disabled={!planned.patterns.some((p) => p.targets.length)}
              >
                Add to plan
              </Button>
            )}
          </div>

          {planned && (
            <table className="story__allocation">
              <thead>
                <tr>
                  <th>Pattern</th>
                  <th className="right">Rows</th>
                  <th className="right">Share</th>
                  <th className="right">Columns</th>
                </tr>
              </thead>
              <tbody>
                {planned.patterns.map((entry) => (
                  <tr key={entry.pattern_id}>
                    <td>{entry.pattern}</td>
                    <td className="right num">{entry.rows.toLocaleString()}</td>
                    <td className="right num">
                      {entry.share !== null ? formatPercent(entry.share) : "—"}
                    </td>
                    <td className="right num">{entry.targets.length}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
    </div>
  );
}

/**
 * One requirement, analyzed: read into the rules it states, and the patterns
 * inside them found straight away. What was read is shown with the words it
 * came from, because a reading can be wrong.
 */
export function StoryAnalysis({
  story, onApplied, onClose, onSaved, onDomain,
}: {
  story: string;
  onApplied: StoryProps["onApplied"];
  onClose: () => void;
  /** A pattern was saved from here. */
  onSaved?: () => void;
  /** The subject the requirement is about, for the Find screen's chip. */
  onDomain?: (domain: Domain) => void;
}) {
  const [reading, setReading] = useState<StoryReading | null>(null);
  const [features, setFeatures] = useState<PatternFeature[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Try again reads the same requirement once more.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    setError(null);
    setReading(null);
    api
      .storyRead(story)
      .then(async (read) => {
        const catalog = await api.patternFeatures(read.entity);
        if (!live) return;
        setReading(read);
        setFeatures(catalog.features);
      })
      .catch((e) => live && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [story, attempt]);

  // The subject the requirement is about goes on the Subject chip -- the
  // primary records as well as their children, so a requirement about the
  // primary records after one about children is shown as about them.
  const entity = reading?.entity;
  useEffect(() => {
    if (entity) onDomain?.(entity);
    // Only when a reading arrives: choosing a subject afterwards is the person's.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entity]);

  const header = (
    <header className="story__analysis-head">
      <div>
        <h3>Patterns for this requirement</h3>
        <p className="panel__hint">{story}</p>
      </div>
      <Button onClick={onClose}>Close</Button>
    </header>
  );

  if (error) {
    return (
      <section className="story__analysis" aria-label="Requirement analysis">
        {header}
        <Banner tone="alarm">
          {error}{" "}
          <Button onClick={() => setAttempt((n) => n + 1)}>Try again</Button>
        </Banner>
      </section>
    );
  }
  if (!reading) {
    return (
      <section className="story__analysis" aria-label="Requirement analysis">
        {header}
        <p className="panel__hint" role="status">Analyzing…</p>
      </section>
    );
  }

  const valueLabel = (key: string, value: string) =>
    features.find((f) => f.key === key)?.options?.find((o) => o.value === value)?.label ?? value;
  const describe = (key: string, rule: PatternCondition) =>
    rule.in
      ? // Plans can share a name under different IDs; each name once.
        [...new Set(rule.in.map((v) => valueLabel(key, v)))].join(", ")
      : rule.min !== undefined && rule.max !== undefined
        ? `${rule.min} to ${rule.max}`
        : rule.min !== undefined
          ? `${rule.min} or more`
          : `up to ${rule.max}`;
  const filters = rulesToFilters(features, reading.rules);
  const name = story.length > 80 ? `${story.slice(0, 77)}...` : story;
  const population = termsOf(reading.entity).many;

  return (
    <section className="story__analysis" aria-label="Requirement analysis">
      {header}
      {reading.found.length === 0 ? (
        <p className="panel__hint">
          No criteria were identified in the requirement. Showing patterns across all{" "}
          {population}.
        </p>
      ) : (
        <ul className="story__rules">
          {reading.found.map((found) => (
            <li key={found.feature}>
              <strong>{found.label}</strong>
              <span>{describe(found.feature, found.rule)}</span>
              <Chip fill="outline" title="Source text">
                {`"${found.from}"`}
              </Chip>
            </li>
          ))}
        </ul>
      )}
      <PatternsAnalyze
        domain={reading.entity}
        onApplied={onApplied}
        defined={{ name, filters }}
        autoRun
        onSaved={onSaved}
      />
    </section>
  );
}
