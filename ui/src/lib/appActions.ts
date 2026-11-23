/**
 * What a pattern's one-step actions need from the shell.
 *
 * Generate file and Load to database finish where they were pressed: the
 * confirmation names the run and offers History, and a load tells the shell
 * its plan and run are no longer the ones on screen.
 */

import { createContext, useContext } from "react";
import type { FilePattern } from "./api";

/** A confirmation with one action, shown until it is dismissed or replaced. */
export interface Toast {
  message: string;
  action?: { label: string; run: () => void };
  /** A failure: said as an alert and kept until dismissed. */
  tone?: "alarm";
}

export interface AppActions {
  /** The target database a load writes into. */
  target: string;
  /** Whether a target database is set up, so a load has somewhere to go. */
  targetReady: boolean;
  notify: (toast: Toast) => void;
  /** History, with this run brought into view. */
  goHistory: (runId?: number | null) => void;
  /** Generate › Files with this pattern chosen: More options. */
  openFiles: (pattern: FilePattern) => void;
  /** The plan, run and rules changed outside Generate › Database. */
  databaseChanged: () => void;
}

const nothing = () => undefined;

export const AppActionsContext = createContext<AppActions>({
  target: "",
  targetReady: true,
  notify: nothing,
  goHistory: nothing,
  openFiles: nothing,
  databaseChanged: nothing,
});

export function useAppActions(): AppActions {
  return useContext(AppActionsContext);
}
