/**
 * Whether this installation may generate into a database.
 *
 * Turned off in the configuration file for a customer who is to have files
 * only. Every screen still works -- reading a database, profiling, patterns,
 * files -- but nothing offers to load generated rows into a database.
 */

import { createContext, useContext } from "react";

export const DatabaseGeneration = createContext(true);

export function useDatabaseGeneration(): boolean {
  return useContext(DatabaseGeneration);
}
