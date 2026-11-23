/**
 * The left menu: the jobs, then the settings.
 *
 * Everything in it stays where it is on every screen. Controls that came and
 * went -- a Members/Claims switch shown only on Patterns, settings buttons in
 * the header that slid along as the header's own buttons changed -- moved the
 * thing a person was about to click, and the click landed on something else.
 *
 * Each item is a job a person comes to do: find a pattern, generate from it,
 * look back at what was generated, keep the layouts files are written in, set
 * up the database, and set up a new source (day 0 at a new customer). None of them waits on another, so there is no line
 * through them and no numbering.
 */

import "./Rail.css";
import { Logo } from "./Logo";
import { hasModule } from "../lib/pack";

export type Screen = "patterns" | "generate" | "history" | "layouts" | "setup" | "source";
export type Tool = "configure" | "algorithms" | "approval";

/** The jobs, in the order they are listed, by the title of each screen. */
export const SCREENS: Record<Screen, string> = {
  patterns: "Patterns",
  generate: "Generate",
  history: "History",
  layouts: "Layouts and samples",
  setup: "Database setup",
  source: "Source setup",
};

/** The settings apart from the jobs, by what they set. */
export const TOOLS: Record<Tool, string> = {
  configure: "Pattern settings",
  algorithms: "Rules",
  approval: "Layout approval",
};

interface Props {
  active: Screen;
  onSelect: (screen: Screen) => void;
  /** A short fact under an item, where one helps. */
  details?: Partial<Record<Screen, string>>;
  tool: Tool | null;
  onTool: (tool: Tool) => void;
  database: string;
  server: string;
  onDatabase: () => void;
  theme: "light" | "dark";
  onToggleTheme: () => void;
}

export function Rail({
  active,
  onSelect,
  details = {},
  tool,
  onTool,
  database,
  server,
  onDatabase,
  theme,
  onToggleTheme,
}: Props) {
  return (
    <nav className="rail" aria-label="Main">
      <div className="rail__head">
        <Logo />
      </div>

      <ol className="rail__steps">
        {(Object.keys(SCREENS) as Screen[]).map((id) => {
          const isActive = !tool && id === active;
          return (
            <li key={id}>
              <button
                className={"rail__step" + (isActive ? " rail__step--active" : "")}
                onClick={() => onSelect(id)}
                aria-current={isActive ? "page" : undefined}
              >
                <span className="rail__body">
                  <span className="rail__label">{SCREENS[id]}</span>
                  {details[id] && <span className="rail__detail num">{details[id]}</span>}
                </span>
              </button>
            </li>
          );
        })}
      </ol>

      <div className="rail__settings" role="navigation" aria-label="Settings">
        <span className="rail__side-label">Settings</span>
        {(Object.keys(TOOLS) as Tool[])
          // Pattern settings set what patterns are found on: none without pattern analysis.
          .filter((id) => id !== "configure" || hasModule("pattern_analysis"))
          .map((id) => (
            <button
              key={id}
              className={"rail__setting" + (tool === id ? " rail__setting--on" : "")}
              aria-current={tool === id ? "page" : undefined}
              onClick={() => onTool(id)}
            >
              {TOOLS[id]}
            </button>
          ))}
      </div>

      <div className="rail__foot">
        <button className="rail__target" onClick={onDatabase} title="Database setup › Connection">
          <span className="rail__target-db mono">{database}</span>
          <span className="rail__target-server">{server}</span>
        </button>
        <button
          className="rail__theme"
          onClick={onToggleTheme}
          title={theme === "light" ? "Switch to dark theme" : "Switch to light theme"}
        >
          {theme === "light" ? "Dark" : "Light"}
        </button>
      </div>
    </nav>
  );
}
