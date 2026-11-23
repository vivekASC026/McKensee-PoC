/**
 * The installation's business pack, as the screens use it: what its records
 * are called, the formats it offers and the business names of its columns.
 *
 * Read once, before the first screen is drawn (main.tsx), and fixed for the
 * session: a pack is chosen per installation (SDG_PACK), never on screen. So
 * these are plain functions rather than a context -- every screen, and every
 * helper outside one, reads the same words.
 *
 * Code asks by role ("primary", "child"), never by an entity's name: under
 * another pack the primary records are not members.
 */

export type Role = "primary" | "child" | "parent_of_primary";

/** Every form of a record's name, stated outright in the pack's glossary. */
export interface Terms {
  one: string;
  many: string;
  One: string;
  Many: string;
  /** A child record's lines, where it has them. */
  line?: string;
  lines?: string;
  Line?: string;
  Lines?: string;
  /** What each type of these records is called, by its code ("P": "Professional"). */
  types?: Record<string, string>;
  /** The headline counts shown above their patterns: the overview's name for
   *  each, and its label (none: the records' own name, Many). */
  counts?: [string, string | null][];
  /** How deep a walk of these records' scope goes, said at each depth
   *  ("Groups", "Groups and categories"...; a bank: "Branches"...). */
  depths?: string[];
}

/** A feature the pattern charts draw as a range. */
export interface ChartRange {
  label: string;
  unit: string;
  money?: boolean;
}

export interface PackEntity {
  key: string;
  role: Role;
  parent: string | null;
  terms: Terms;
}

export interface Pack {
  id: string;
  name: string;
  entities: PackEntity[];
  /** The file formats offered for each record key. */
  formats: Record<string, string[]>;
  modules: string[];
  glossary: Record<string, unknown>;
  /** The filter a child analysis is narrowed to one type of record by
   *  (store.type_filter), where the pack has one. */
  type_filter?: string | null;
  /** Which of a pattern's measures its card states. */
  facts?: Facts;
}

/** A pattern card's measures: the first age found, a tenure in months, and an
 *  amount ([its key, its label]). */
export interface Facts {
  age?: string[];
  tenure?: string;
  amount?: [string, string];
  /** The child records' type, as a pattern's categorical mix names it. */
  type?: string;
  /** The child records' most frequent codes: [counts key, descriptions key
   *  (or none), caption]. */
  top?: [string, string | null, string];
}

let PACK: Pack | null = null;
let NAMES: Record<string, string> = {};

async function fetched<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: ${response.status} ${response.statusText}`);
  return response.json() as Promise<T>;
}

/** Read the pack and its column names. main.tsx waits for it. */
export async function loadPack(): Promise<void> {
  const [pack, names] = await Promise.all([
    fetched<Pack>("/api/pack"),
    fetched<Record<string, string>>("/api/pack/column-names"),
  ]);
  PACK = pack;
  NAMES = names;
}

function pack(): Pack {
  if (!PACK) throw new Error("The business pack is read before the screens are drawn.");
  return PACK;
}

function entity(role: Role): PackEntity {
  const found = pack().entities.find((e) => e.role === role);
  if (!found) throw new Error(`The ${pack().name} pack has no ${role} records.`);
  return found;
}

/** The key the server knows a role's records by ("members" in health). */
export function keyOf(role: Role): string {
  return entity(role).key;
}

/** The filter that narrows a child analysis to one type of record, if any. */
export function typeFilter(): string | null {
  return pack().type_filter ?? null;
}

/** Which of a pattern's measures its card states. */
export function facts(): Facts {
  return pack().facts ?? {};
}

/** Whether files and plans are written from patterns (the pack's own
 *  module): without it, pattern screens analyze and keep, and write nothing. */
export function fromPatterns(): boolean {
  return hasModule("pattern_generation");
}

/** The role a record key plays. */
export function roleOf(key: string): Role | undefined {
  return pack().entities.find((e) => e.key === key)?.role;
}

/** What a role's records are called: words("child").Many is "Claims" in health. */
export function words(role: Role): Terms {
  return entity(role).terms;
}

/** What the records with this key are called; the primary records' words
 *  where the key is not given (a screen not yet told which records). */
export function termsOf(key: string | null | undefined): Terms {
  const found = key == null ? undefined : pack().entities.find((e) => e.key === key);
  return (found ?? entity("primary")).terms;
}

/** What one row of these records' patterns is called: a child record's line
 *  where it has lines ("claim line"), else the record ("member"). */
export function lineOf(key: string | null | undefined): string {
  const terms = termsOf(key);
  return terms.line ?? terms.one;
}

/** lineOf, plural ("claim lines", "members"). */
export function linesOf(key: string | null | undefined): string {
  const terms = termsOf(key);
  return terms.lines ?? terms.many;
}

/** Whether these records are the primary ones (the people, in health). */
export function isPrimary(key: string | null | undefined): boolean {
  return key != null && roleOf(key) === "primary";
}

/** Whether these records are children of the primary ones. */
export function isChild(key: string | null | undefined): boolean {
  return key != null && roleOf(key) === "child";
}

/** The key of the records patterns of this entity are found for: a child
 *  entity's, else the primary records' (an entity the server names that the
 *  screens show as one of the two). */
export function patternKey(key: string | null | undefined): string {
  return isChild(key) ? keyOf("child") : keyOf("primary");
}

/** The keys patterns are found for: the primary records, then their children. */
export function patternKeys(): string[] {
  return [keyOf("primary"), keyOf("child")];
}

/** A whole phrase from the pack's glossary, where composing one from the
 *  record words reads badly ("a group, plan, age range or diagnosis"). */
export function phrase(key: string): string {
  const phrases = (pack().glossary.phrases ?? {}) as Record<string, string>;
  const found = phrases[key];
  if (typeof found !== "string") throw new Error(`The ${pack().name} pack has no phrase "${key}".`);
  return found;
}

/** The features the pattern charts draw as ranges, in the pack's order. */
export function chartRanges(): Record<string, ChartRange> {
  return ((pack().glossary.charts as { ranges?: Record<string, ChartRange> })?.ranges) ?? {};
}

/** The features the pattern charts draw as mixes of values, and their names. */
export function chartMixes(): Record<string, string> {
  return ((pack().glossary.charts as { mixes?: Record<string, string> })?.mixes) ?? {};
}

/** What a child record's type is called, by its code; the code where unnamed. */
export function typeName(code: string): string {
  return words("child").types?.[code] ?? code;
}

/** Whether the pack has records in this role. */
export function hasRole(role: Role): boolean {
  return pack().entities.some((e) => e.role === role);
}

/** The file formats the pack offers for these records, in its order. */
export function formatsOf(key: string): string[] {
  return pack().formats[key] ?? [];
}

/** Whether the pack offers this file format for a role's records (the
 *  format's id as the server names it: "edi:834", "csv"). */
export function offers(role: Role, format: string): boolean {
  return (pack().formats[keyOf(role)] ?? []).includes(format);
}

/** What the installation's business pack is called ("Retail banking"). */
export function packName(): string {
  return pack().name;
}

/** Whether the pack has this module (a health-only format, or pattern analysis). */
export function hasModule(module: string): boolean {
  return pack().modules.includes(module);
}

/** What a person calls a column ("Product ID" for PDPD_ID), from the pack.
 *  The column itself where the pack has no name for it. */
export function columnName(column: string): string {
  return NAMES[column] ?? NAMES[column.toUpperCase()] ?? column;
}
