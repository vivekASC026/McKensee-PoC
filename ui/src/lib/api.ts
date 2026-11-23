/**
 * Talking to the engine.
 *
 * Every pipeline step is a POST that runs real work against a real database, so
 * failures are expected and are surfaced with whatever the server said rather
 * than a generic message.
 */

import { isChild, keyOf, typeFilter } from "./pack";

export class ApiError extends Error {
  status: number;
  /** What kind of refusal it is, where the server says and the screen offers
   *  a way on: Source setup's "write_login". */
  refusal: string | null;
  constructor(message: string, status: number, refusal: string | null = null) {
    super(message);
    this.status = status;
    this.refusal = refusal;
  }
}

/** A failed response as an error: the server's sentence (its `detail`), else
 * its text, else the status line. */
async function failure(response: Response): Promise<ApiError> {
  const text = await response.text().catch(() => "");
  let detail = text || `${response.status} ${response.statusText}`;
  let refusal: string | null = null;
  try {
    const body: unknown = JSON.parse(text);
    const said = detailOf(body);
    if (said) detail = said;
    if (typeof body === "object" && body !== null) {
      const kind = (body as { refusal?: unknown }).refusal;
      if (typeof kind === "string") refusal = kind;
    }
  } catch {
    /* not JSON: the text is what we have */
  }
  return new ApiError(detail, response.status, refusal);
}

/** The sentence in a FastAPI error body: `detail` as a string, or a 422's
 * list of validation errors joined into one line. Null when there is none. */
function detailOf(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const lines = detail.map((item: unknown) => {
      if (typeof item !== "object" || item === null) return String(item);
      const { msg, loc } = item as { msg?: unknown; loc?: unknown };
      const where = Array.isArray(loc) ? loc.filter((p) => p !== "body").join(".") : "";
      const said = typeof msg === "string" ? msg : JSON.stringify(item);
      return where ? `${where}: ${said}` : said;
    });
    return lines.length ? lines.join("; ") : null;
  }
  return null;
}

/** A successful response's JSON body. The shape is the endpoint's contract and
 * is not checked field by field, but a body that is not JSON at all fails here,
 * at its cause, naming the endpoint. */
async function json<T>(response: Response): Promise<T> {
  const text = await response.text();
  try {
    return JSON.parse(text) as T;
  } catch {
    const where = new URL(response.url || "/", window.location.origin).pathname;
    throw new ApiError(
      `The server's answer from ${where} was not readable. ` +
        "Try again; if the problem persists, check the server log.",
      response.status,
    );
  }
}

/** The name a download's header gives: its real name (RFC 5987
 * `filename*`) when there is one, else the plain `filename`. */
function headerName(response: Response): string | null {
  const header = response.headers.get("Content-Disposition") ?? "";
  const real = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (real) {
    try {
      return decodeURIComponent(real[1]);
    } catch {
      /* not encoded as it should be: the plain name follows */
    }
  }
  return /filename="([^"]+)"/.exec(header)?.[1] ?? null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) throw await failure(response);
  return json<T>(response);
}

/** What a download was kept under: Generate › Files listens for it. */
export const REQUEST_WRITTEN = "sdm:request-written";

async function saveDownload(
  response: Response,
  filename: string,
  announce = true,
): Promise<number | null> {
  let blob: Blob;
  try {
    blob = await response.blob();
  } catch {
    // The server cut the file off: something failed after it had begun.
    throw new ApiError(
      `Couldn't download ${filename}; the file was not saved and no run was recorded. ` +
        "Generate it again; if the problem persists, check the server log.",
      0,
    );
  }
  const url = URL.createObjectURL(blob);
  const kept = response.headers.get("X-SDM-Request");
  const link = document.createElement("a");
  link.href = url;
  // A new file is named for its request, so a folder of 834s says which is
  // which: 834-request-12.txt, not 834 (3).txt.
  link.download = kept && announce ? withRequest(filename, kept) : filename;
  link.click();
  URL.revokeObjectURL(url);
  if (kept && announce) {
    window.dispatchEvent(new CustomEvent(REQUEST_WRITTEN, { detail: { id: Number(kept) } }));
  }
  return kept ? Number(kept) : null;
}

function withRequest(filename: string, request: string): string {
  if (filename.includes(`request-${request}`)) return filename;
  const dot = filename.lastIndexOf(".");
  return dot > 0
    ? `${filename.slice(0, dot)}-request-${request}${filename.slice(dot)}`
    : `${filename}-request-${request}`;
}

export interface GenerationRequestFile {
  id: number;
  request_id: number;
  file_name: string;
  format: string;
  subject: string;
  sdm_version: string | null;
  sdm_build: string | null;
  fingerprint: string;
  bytes: number;
  replay_of: number | null;
  matched: boolean | null;
  // Written again and different: what the store holds differently since.
  changed?: string[];
  written_by: string | null;
  written_at: string;
  // A database load: how many rows, into which database.
  rows: number | null;
  target: string | null;
  // Whether Rerun can write it again, and the sentence saying why not.
  rerunnable?: boolean;
  rerun_refusal?: string | null;
}

export interface GenerationRequest {
  id: number;
  pattern_key: string;
  started: string;
  pattern_id: number | null;
  pattern_name: string | null;
  subject: string | null;
  seed: string;
  created_by: string | null;
  created_at: string;
  // Claims for this earlier request's members.
  parent_id: number | null;
  files: GenerationRequestFile[];
  // Its people: how many, and members or claims (its first file's).
  members: number | null;
  of: string | null;
}

export interface ClaimsCheckPayload {
  members: number;
  outside: number;
  features: { element: string; pattern: string; request: string; outside: number }[];
}

/** How many of something -- claims a member has, lines a claim has. */
export interface CountChoice {
  mode: "measured" | "exactly" | "range" | "mix";
  low: number;
  high: number;
  mix: [number, number][] | null;
}

/** Claims for an earlier request's members: what the screen chose. */
export interface ClaimsChoice {
  patternId: number | null;
  everyone: boolean;
  share: number;
  claims: CountChoice;
  lines: CountChoice;
  started: string;
  requestId: number | null;
}

/** A claims pattern's own claims: how many each member has, lines each claim. */
export interface ClaimShape {
  claims_each: [number, number][];
  lines_each: [number, number][];
  members: number;
  claims: number;
}

/** The export fields a count choice asks for: claims_min, claims_max, claims_mix. */
export function countRequest(
  prefix: "claims" | "lines",
  count: CountChoice,
  measured: [number, number][] | null,
): Record<string, unknown> {
  if (count.mode === "measured") return measured ? { [`${prefix}_mix`]: measured } : {};
  if (count.mode === "mix") return count.mix ? { [`${prefix}_mix`]: count.mix } : {};
  if (count.mode === "exactly") {
    return { [`${prefix}_min`]: count.low, [`${prefix}_max`]: count.low };
  }
  return { [`${prefix}_min`]: count.low, [`${prefix}_max`]: count.high };
}

export interface GenerationRequestsPayload {
  requests: GenerationRequest[];
  build: string;
  version: string;
}

export const api = {
  state: () => request<AppState>("/api/state"),
  connect: () => request<ConnectResult>("/api/connect", { method: "POST" }),
  discover: () => request<DiscoverResult>("/api/discover", { method: "POST" }),
  profile: () => request<ProfileResult>("/api/profile", { method: "POST" }),
  map: () => request<MapResult>("/api/map", { method: "POST" }),
  review: () => request<ReviewResult>("/api/review", { method: "POST" }),

  schema: () => request<SchemaPayload>("/api/schema"),
  catalog: () => request<CatalogPayload>("/api/catalog"),
  generators: () => request<GeneratorsPayload>("/api/generators"),
  ratioShapes: () => request<RatioShapesPayload>("/api/ratio-shapes"),

  decide: (decision: DecisionInput) =>
    request<unknown>("/api/decisions", {
      method: "POST",
      body: JSON.stringify(decision),
    }),

  saveSettings: (values: Partial<Settings>) =>
    request<Settings>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(values),
    }),

  buildPlan: () => request<PlanPayload>("/api/plan", { method: "POST" }),
  getPlan: () => request<PlanPayload>("/api/plan"),

  dryRun: (rows = 6) =>
    request<DryRunPayload>(`/api/dryrun?rows=${rows}`, { method: "POST" }),
  generate: () => request<GenerateResult>("/api/generate", { method: "POST" }),
  validate: () => request<ValidateResult>("/api/validate", { method: "POST" }),
  cleanup: () => request<CleanupResult>("/api/cleanup", { method: "POST" }),
  run: () => request<RunPayload>("/api/run"),

  /* The pattern analyzer, by the business pack's entity keys: what the store
     holds of a record, its patterns, and what a chosen one means for generation. */
  /** A record's headline counts, by their names in the pack (n_members, n_claims). */
  overview: (entity: Domain) => request<Overview>(`/api/entities/${entity}/overview`),
  /** A record's most frequent values, as the pack names them; none where it names none. */
  highlights: (entity: Domain, n = 8) =>
    request<Highlight[]>(`/api/entities/${entity}/highlights?n=${n}`),
  analyze: (filters: PatternFilters, k: number | null) =>
    request<AnalyzePayload>(`/api/entities/${keyOf("primary")}/analyze`, {
      method: "POST",
      body: JSON.stringify({ filters, k }),
    }),
  /* The claims analyzer takes its filters flat rather than nested, because it
     clusters over claim lines rather than over members. */
  analyzeClaims: (filters: PatternFilters, k: number | null, claimType?: string | null) =>
    request<ClaimsAnalyzePayload>(`/api/entities/${keyOf("child")}/analyze`, {
      method: "POST",
      body: JSON.stringify({ ...childBody(filters, claimType), k }),
    }),
  // What a pattern can be defined by, and the values each feature takes.
  /** Every feature of members and claims, and which ones patterns are found on. */
  discoveryFeatures: () =>
    request<Record<Domain, DiscoveryFeature[]>>("/api/discovery-features"),
  chooseDiscoveryFeatures: (entity: Domain, features: string[]) =>
    request<Record<string, DiscoveryFeature[]>>("/api/discovery-features", {
      method: "PUT",
      body: JSON.stringify({ entity, features }),
    }),
  // Which fields a pattern's counts are made on: built-in, and denorm columns.
  reportFields: () => request<Record<Domain, ReportField[]>>("/api/report-fields"),
  chooseReportFields: (entity: Domain, fields: string[], reset = false) =>
    request<Record<string, ReportField[]>>("/api/report-fields", {
      method: "PUT",
      body: JSON.stringify({ entity, fields, reset }),
    }),
  patternFeatures: (entity: Domain) =>
    request<{ entity: Domain; features: PatternFeature[] }>(
      `/api/patterns/features?entity=${entity}`,
    ),
  toPlan: (
    pattern: DiscoveredPattern,
    kind: string,
    asOf: string,
    apply: boolean,
    asCohort = false,
  ) =>
    request<ToPlanPayload>("/api/patterns/to-plan", {
      method: "POST",
      body: JSON.stringify({ pattern, kind, as_of: asOf, apply, as_cohort: asCohort }),
    }),

  /** Whether the store holds any of the child records. */
  childAvailable: () =>
    request<{ available: boolean }>(`/api/entities/${keyOf("child")}/available`),

  /* System discovery: the largest patterns across the whole book, found
     without anyone choosing a scope first. */
  discovery: (entity: Domain, maxDepth: number) =>
    request<DiscoveryPayload>(
      `/api/discovery?entity=${entity}&max_depth=${maxDepth}`,
    ),
  // Whether a scan at this depth is cached: answered without scanning.
  discoveryStatus: (entity: Domain, maxDepth: number) =>
    request<{ entity: string; cached: boolean }>(
      `/api/discovery/status?entity=${entity}&max_depth=${maxDepth}`,
    ),
  savePattern: (filters: PatternFilters, k: number | null, clusterId: number) =>
    request<SaveResult>(`/api/entities/${keyOf("primary")}/save`, {
      method: "POST",
      body: JSON.stringify({ filters, k, cluster_id: clusterId }),
    }),
  saved: () => request<{ patterns: SavedPattern[] }>("/api/discovery/saved"),
  // Every stored pattern, bookmarked or not: what a file can be written for.
  patternsForFiles: () =>
    request<{ patterns: FilePatternOption[] }>("/api/patterns/for-files"),
  onboarded: () =>
    request<{ databases: OnboardedDatabase[] }>("/api/databases/onboarded"),
  offboard: (key: string) =>
    request<{ offboarded: string; workingOn: { database: string; server: string } }>(
      "/api/databases/offboard",
      { method: "POST", body: JSON.stringify({ key }) },
    ),
  snipRules: () => request<SnipRules>("/api/edi/snip"),
  snipCheck: (exportRequest: Record<string, unknown>, partner: string | null) =>
    request<SnipReport>("/api/edi/snip/check", {
      method: "POST",
      body: JSON.stringify({ export: exportRequest, partner }),
    }),
  // A file from a sample checked as its format checks (FHIR: models, references, codes).
  sampleCheck: (exportRequest: Record<string, unknown>) =>
    request<SnipReport>("/api/samples/check", {
      method: "POST",
      body: JSON.stringify({ export: exportRequest }),
    }),
  snipCheckFile: (file: File, partner: string | null) => {
    const form = new FormData();
    form.append("file", file);
    if (partner) form.append("partner", partner);
    return fetch("/api/edi/snip/check-file", { method: "POST", body: form }).then(
      unwrap<SnipReport>,
    );
  },
  keepPattern: (body: KeepPatternRequest) =>
    request<KeptPattern>("/api/patterns/keep", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // The profile saving would store (chosen records' own values), not saved.
  patternProfile: (body: KeepPatternRequest) =>
    request<{ profile: DiscoveredPattern }>("/api/patterns/profile", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Marks a pattern the walk already stored as saved, members or claims alike.
  bookmark: (patternId: number) =>
    request<unknown>("/api/discovery/save", {
      method: "POST",
      body: JSON.stringify({ pattern_id: patternId }),
    }),
  unsave: (patternId: number) =>
    request<unknown>("/api/discovery/unsave", {
      method: "POST",
      body: JSON.stringify({ pattern_id: patternId }),
    }),

  /* Story to data: a requirement in prose, matched against saved patterns. */
  storyParse: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return fetch("/api/story/parse", { method: "POST", body: form }).then(unwrap<StoryParsePayload>);
  },
  // A pattern's scenarios: the combinations of chosen features its rows hold.
  scenarioDimensions: (entity: Domain) =>
    request<{ dimensions: { key: string; label: string }[]; defaults: string[] }>(
      `/api/patterns/scenario-dimensions?entity=${entity}`,
    ),
  scenarios: (body: ScenarioRequest) =>
    request<ScenarioList>("/api/patterns/scenarios", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // A pattern's counts: every field and value, every scenario, with %.
  patternReport: (body: PatternReportRequest) =>
    request<PatternReport>("/api/patterns/report", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Every pattern of an analysis on one sheet (.xlsx), downloaded.
  patternReportSheet: async (body: PatternReportRequest, filename: string) => {
    const response = await fetch("/api/patterns/report.xlsx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw await failure(response);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  },
  keepScenarios: (body: ScenarioRequest & { name: string; scenarios: Record<string, string>[] }) =>
    request<{ kept: (KeptPattern & { label: string })[] }>("/api/patterns/scenarios/keep", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // A requirement read into the rules of a pattern, to analyze like one.
  storyRead: (story: string) =>
    request<StoryReading>("/api/story/read", {
      method: "POST",
      body: JSON.stringify({ story }),
    }),
  storyMatch: (stories: string[]) =>
    request<StoryMatchPayload>("/api/story/match", {
      method: "POST",
      body: JSON.stringify({ stories }),
    }),

  storyToPlan: (patternIds: number[], totalVolume: number, apply: boolean) =>
    request<StoryToPlanPayload>("/api/story/to-plan", {
      method: "POST",
      body: JSON.stringify({
        pattern_ids: patternIds, total_volume: totalVolume, apply,
      }),
    }),

  /* Layouts: the shape data leaves in. */
  // approvalRequired: a second approver is configured, so a layout writes
  // files only once approved.
  layouts: () => request<{ layouts: Layout[]; approvalRequired: boolean }>("/api/layouts"),
  layoutApproval: () => request<LayoutApproval>("/api/layouts/approval"),
  setLayoutApproval: (secondApprover: string | null) =>
    request<LayoutApproval>("/api/layouts/approval", {
      method: "PUT",
      body: JSON.stringify({ secondApprover }),
    }),
  layoutRules: () => request<{ rules: GenerationRule[] }>("/api/layouts/rules"),

  // The rule repository. Layouts bind to these by column name, so an empty
  // repository is why a parsed layout falls to blanks.
  // The denominator every weight band is read against, and the background
  // walk that computes it.
  // The real rows a pattern describes. Both roles answer in the same shape,
  // so one call serves both; the body is the role's.
  patternRows: (
    domain: Domain,
    filters: PatternFilters,
    k: number | null,
    clusterId: number,
    claimType: string | null,
    limit = 25,
    offset = 0,
  ) =>
    isChild(domain)
      ? request<PatternRowsPayload>(`/api/entities/${domain}/rows`, {
          method: "POST",
          body: JSON.stringify({
            ...childBody(filters, claimType),
            k,
            cluster_id: clusterId,
            limit,
            offset,
          }),
        })
      : request<PatternRowsPayload>(`/api/entities/${domain}/rows`, {
          method: "POST",
          body: JSON.stringify({ filters, k, cluster_id: clusterId, limit, offset }),
        }),

  // Which database generation writes to. Any on this server; the roles all
  // resolve against the one SQL Server this installation is configured for.
  // EDI transaction sets, described on disk rather than compiled in.
  ediFormats: () => request<{ formats: EdiFormatSummary[] }>("/api/edi/formats"),
  // A file modeled on a sample: read it against a master config, save as new.
  // The format is read off the sample.
  sampleRead: (body: {
    sample?: string | null;
    sampleName?: string | null;
    master?: string | null;
    masterName?: string | null;
    configId?: number | null;
    defaultId?: string | null;
    /** No master named: read against the standard of the sample's format. */
    auto?: boolean;
  }) =>
    request<SampleReading>("/api/samples/read", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  sampleConfigs: () => request<{ configs: GenConfigSummary[] }>("/api/samples/configs"),
  // What an uploaded sample is: a format a file is modeled on, or a flat file.
  sampleDetect: (sample: string) =>
    request<SampleDetected>("/api/samples/detect", {
      method: "POST",
      body: JSON.stringify({ sample }),
    }),
  // The standard sample and master config of each format.
  sampleDefaults: () => request<{ defaults: SampleDefault[] }>("/api/samples/defaults"),
  sampleConfigSave: (body: {
    name: string;
    sample: string | null;
    sampleName: string | null;
    basedOn: string | null;
    rows: SamplePosition[];
    vary?: SampleVary[];
    fromConfigId: number | null;
    fromDefaultId?: string | null;
  }) =>
    request<{ id: number; name: string; created: boolean }>("/api/samples/configs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // A saved config changed in place; its sample stays unless another is sent.
  sampleConfigUpdate: (id: number, body: {
    name: string;
    sample: string | null;
    sampleName: string | null;
    rows: SamplePosition[];
    vary?: SampleVary[];
  }) =>
    request<{ id: number; name: string; updated: boolean }>(`/api/samples/configs/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  sampleConfigDelete: (id: number) =>
    request<{ deleted: number }>(`/api/samples/configs/${id}`, { method: "DELETE" }),
  exportText: (
    format: string,
    subject: string,
    rows = 6,
    extra: Record<string, unknown> = {},
  ) =>
    fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format, subject, rows, root_limit: 40, packaged: false, ...extra }),
    }).then(async (r) => {
      if (!r.ok) throw await failure(r);
      return r.text();
    }),
  exportDownload: async (
    format: string,
    subject: string,
    filename: string,
    extra: Record<string, unknown> = {},
  ): Promise<number | null> => {
    // A download is kept in its generation request's record; a preview or a
    // check is not. Returns the run it was recorded under.
    const response = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format, subject, ...extra, record: true, file_name: filename }),
    });
    if (!response.ok) throw await failure(response);
    return saveDownload(response, filename);
  },
  generationRequests: (filters: { day?: string; pattern_id?: number; user?: string } = {}) => {
    const query = new URLSearchParams();
    if (filters.day) query.set("day", filters.day);
    if (filters.pattern_id != null) query.set("pattern_id", String(filters.pattern_id));
    if (filters.user) query.set("user", filters.user);
    const text = query.toString();
    return request<GenerationRequestsPayload>(
      "/api/generation-requests" + (text ? `?${text}` : ""),
    );
  },
  claimShape: (patternId: number) => request<ClaimShape>(`/api/patterns/${patternId}/claim-shape`),
  // What a claims pattern says of members that a request's members supersede.
  claimsCheck: (requestId: number, patternId: number) =>
    request<ClaimsCheckPayload>(`/api/generation-requests/${requestId}/claims-check`, {
      method: "POST",
      body: JSON.stringify({ pattern_id: patternId }),
    }),
  // A file written again from its request's record alone.
  generationFileAgain: async (fileId: number, filename: string) => {
    const response = await fetch(`/api/generation-requests/files/${fileId}/again`, {
      method: "POST",
    });
    if (!response.ok) throw await failure(response);
    // A load regenerated is a zip named by the server.
    // An earlier request's file, not the one being written on the tabs.
    await saveDownload(response, headerName(response) ?? filename, false);
  },

  databases: () => request<DatabasesPayload>("/api/databases"),
  // A target somewhere other than the configured server. Tested before it is
  // saved, because a server and a password typed into a form are usually
  // nearly right rather than right.
  testTarget: (config: TargetConnection) =>
    request<{ db: string; server: string }>("/api/target/test", {
      method: "POST",
      body: JSON.stringify(config),
    }),
  saveTarget: (config: TargetConnection) =>
    request<{ target: string; server: string; trusted: boolean }>(
      "/api/target/connection",
      { method: "POST", body: JSON.stringify(config) },
    ),
  readTarget: () => request<StoredTarget>("/api/target/connection"),
  forgetTarget: () =>
    request<{ target: string; server: string }>("/api/target/connection", {
      method: "DELETE",
    }),
  setTarget: (database: string) =>
    request<{ target: string; server?: string }>("/api/target", {
      method: "POST",
      body: JSON.stringify({ database }),
    }),

  rules: () => request<RulesPayload>("/api/rules"),
  seedPreview: () => request<SeedPayload>("/api/rules/seed"),
  seedRules: () =>
    request<SeedPayload & { ok: boolean; added: string[]; failed: string[] }>(
      "/api/rules/seed",
      { method: "POST", body: JSON.stringify({ created_by: "ui" }) },
    ),
  createRule: (body: NewRule) =>
    request<GenerationRule>("/api/rules", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateRule: (ruleId: number, changes: Partial<NewRule>) =>
    request<GenerationRule>(`/api/rules/${ruleId}`, {
      method: "PUT",
      body: JSON.stringify(changes),
    }),
  /** A few values of a rule as it stands, made the way a file makes them. */
  sampleRule: (rule: Partial<NewRule> & { field_name: string; rows?: number }) =>
    request<{ values: string[] }>("/api/rules/sample", {
      method: "POST",
      body: JSON.stringify(rule),
    }),
  setRuleActive: (ruleId: number, active: boolean) =>
    request<unknown>(`/api/rules/${ruleId}/active`, {
      method: "POST",
      body: JSON.stringify({ active }),
    }),
  addAlias: (ruleId: number, columnName: string) =>
    request<unknown>(`/api/rules/${ruleId}/aliases`, {
      method: "POST",
      body: JSON.stringify({ column_name: columnName }),
    }),
  layoutParse: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return fetch("/api/layouts/parse", { method: "POST", body: form }).then(unwrap<LayoutParsePayload>);
  },
  saveLayout: (body: SaveLayoutBody) =>
    request<SavedLayoutResult>("/api/layouts/save", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  layout: (layoutId: number) => request<LayoutDetail>(`/api/layouts/${layoutId}`),
  // Files as a source: the folder they are read from, and each file read.
  fileSource: () => request<FileSource>("/api/files/source"),
  setFileSource: (folder: string) =>
    request<FileSource>("/api/files/source", {
      method: "PUT",
      body: JSON.stringify({ folder }),
    }),
  fileProfiles: () => request<{ folder: string | null; files: FileProfile[] }>("/api/files/profile"),
  // A file in the folder read as a layout sample, as an upload would be.
  layoutParseFolderFile: (name: string) =>
    request<LayoutParsePayload>("/api/files/parse", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  // Any edit sends the layout back to review: its approval was of the fields
  // as they were.
  updateLayout: (layoutId: number, body: LayoutEditBody) =>
    request<LayoutEditResult>(`/api/layouts/${layoutId}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  // Approved layouts are archived, keeping their approval; others are deleted.
  // The answer says which.
  dropLayout: (layoutId: number) =>
    request<{ action: "archived" | "deleted"; name?: string }>(`/api/layouts/${layoutId}`, {
      method: "DELETE",
    }),
  // approver: the second approver's name, typed to confirm the approval.
  approveLayout: (layoutId: number, approver?: string) =>
    request<unknown>("/api/layouts/approve", {
      method: "POST",
      body: JSON.stringify({ layout_id: layoutId, approver: approver ?? null }),
    }),

  // Source setup: name a source, read it, shape its proposal. Every edit
  // answers with the whole plan, its tables derived again.
  accelSources: () => request<{ sources: AccelSource[] }>("/api/accelerator/sources"),
  accelAddSource: (name: string) =>
    request<{ sources: AccelSource[] }>("/api/accelerator/sources", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  accelRead: (source: string, allowWriteLogin: boolean) =>
    request<AccelJob>(`/api/accelerator/sources/${encodeURIComponent(source)}/read`, {
      method: "POST",
      body: JSON.stringify({ allow_write_login: allowWriteLogin }),
    }),
  accelPropose: (source: string, force: boolean, allowWriteLogin: boolean) =>
    request<AccelJob>(`/api/accelerator/sources/${encodeURIComponent(source)}/propose`, {
      method: "POST",
      body: JSON.stringify({ force, allow_write_login: allowWriteLogin }),
    }),
  accelLatestJob: () => request<AccelJob | { state: "none" }>("/api/accelerator/jobs/latest"),
  accelJob: (id: string) =>
    request<AccelJob>(`/api/accelerator/jobs/${encodeURIComponent(id)}`),
  accelLandscape: (source: string) =>
    request<{ source: string; summary: AccelSummary | null }>(
      `/api/accelerator/sources/${encodeURIComponent(source)}/landscape`,
    ),
  accelProposal: (source: string) =>
    request<AccelView>(`/api/accelerator/sources/${encodeURIComponent(source)}/proposal`),
  accelEdit: (source: string, edit: AccelEdit) =>
    request<AccelView>(`${accelBase(source)}/${accelEditPath(edit.path)}`, {
      method: "POST",
      body: JSON.stringify(edit.body),
    }),
  // Counting the share of rows each criterion keeps reads the source: a job.
  accelMeasure: (source: string, allowWriteLogin: boolean) =>
    request<AccelJob>(`${accelBase(source)}/criteria/measure`, {
      method: "POST",
      body: JSON.stringify({ allow_write_login: allowWriteLogin }),
    }),
  accelApproval: (source: string) => request<AccelApproval>(`${accelBase(source)}/approval`),
  accelSubmit: (source: string) =>
    request<AccelApproval>(`${accelBase(source)}/submit`, { method: "POST" }),
  // planHash: the plan the screen showed; approver: the second approver's
  // name, typed to confirm, when one is set.
  accelApprove: (source: string, planHash: string, approver: string | null) =>
    request<AccelApproval>(`${accelBase(source)}/approve`, {
      method: "POST",
      body: JSON.stringify({ plan_hash: planHash, approver }),
    }),
  accelWithdraw: (source: string) =>
    request<AccelApproval>(`${accelBase(source)}/withdraw`, { method: "POST" }),
  accelBuild: (source: string) =>
    request<AccelJob>(`${accelBase(source)}/build`, { method: "POST" }),
  accelFiles: (source: string) => request<AccelFiles>(`${accelBase(source)}/files`),
  accelLoadStatus: (source: string) => request<AccelLoadStatus>(`${accelBase(source)}/load`),
  accelCreateTables: (source: string) =>
    request<{ built: AccelBuilt; store: AccelStore }>(`${accelBase(source)}/store-tables`, {
      method: "POST",
    }),
  accelLoad: (source: string, allowWriteLogin: boolean) =>
    request<AccelJob>(`${accelBase(source)}/load`, {
      method: "POST",
      body: JSON.stringify({ allow_write_login: allowWriteLogin }),
    }),
  accelStop: (id: string) =>
    request<AccelJob & { asked: boolean }>(`/api/accelerator/jobs/${encodeURIComponent(id)}/stop`, {
      method: "POST",
    }),
  // A built file's text, shown as text: never rendered.
  accelFileText: async (source: string, name: string): Promise<string> => {
    const response = await fetch(accelFileUrl(source, name));
    if (!response.ok) throw await failure(response);
    return response.text();
  },
  accelFileDownload: async (source: string, name: string): Promise<void> => {
    const response = await fetch(`${accelFileUrl(source, name)}?download=true`);
    if (!response.ok) throw await failure(response);
    await saveDownload(response, name.split("/").pop() ?? name, false);
  },
};

function accelBase(source: string): string {
  return `/api/accelerator/sources/${encodeURIComponent(source)}`;
}

/** Where an edit is sent: the proposal's own edits under proposal/, the
 *  criteria, structure and pack name under their own names. */
function accelEditPath(path: AccelEdit["path"]): string {
  return path.startsWith("criteria/") || path.startsWith("structure/") || path === "pack-name"
    ? path
    : `proposal/${path}`;
}

/** A built file, by its name from the list a build writes. */
function accelFileUrl(source: string, name: string): string {
  return `${accelBase(source)}/files/${name.split("/").map(encodeURIComponent).join("/")}`;
}

/* Source setup ----------------------------------------------------------- */

export type AccelRole = "primary" | "child";

/** A source the screen offers: named in .env, added this session, or read before. */
export interface AccelSource {
  name: string;
  origins: ("env" | "added" | "read")[];
  database: string | null;
  server: string | null;
  /** How it signs in, never the login itself. */
  login: string;
  /** Why it cannot be read as it is set up, when it cannot. */
  problem: string | null;
  read: boolean;
  read_on: string | null;
  proposed: boolean;
}

export interface AccelJob {
  id: string;
  /** The source the job reads. */
  source: string | null;
  kind: string;
  label: string;
  state: "running" | "done" | "failed" | "canceled";
  done: number;
  total: number | null;
  note: string;
  elapsed_seconds: number;
  eta_seconds: number | null;
  result: AccelJobResult | null;
  error: string | null;
}

/** What a job handed back: a read or proposal, a measure, or a build. */
export interface AccelJobResult {
  source: string;
  proposed?: boolean;
  summary?: AccelSummary;
  /** A measure: how many criteria have a share now, and its notes. */
  measured?: number;
  notes?: string[];
  /** A build: whether the pack was put in place, where, and why not. */
  pack_id?: string;
  placed?: boolean;
  folder?: string | null;
  packs_dir?: string | null;
  files?: string[];
  problems?: string[];
  /** A load: what each table got, whether it was stopped, and the checks. */
  load?: {
    seconds: number;
    rows: number;
    tables: { table: string; rows: number; skipped: number; seconds: number; error: string | null }[];
  };
  stopped?: boolean;
  failed?: number;
  checks?: AccelCheck[];
}

/** One count-only check of a loaded table. */
export interface AccelCheck {
  check: "rows" | "key" | "filled" | "meets";
  table: string;
  passed: boolean;
  said: string;
}

/** The pack a source's plan built, as it is on disk now. */
export interface AccelBuilt {
  pack_id: string;
  folder: string;
  built_by: string | null;
  built_at: string | null;
  /** Files changed since the build. */
  edited: string[];
}

/** A built table as the store has it. */
export interface AccelStoreTable {
  name: string;
  role: AccelRole;
  exists: boolean;
  lacking: string[];
  differs: string[];
  extra: string[];
  other_source: boolean;
  /** In the store as someone else's: never changed from here. */
  foreign: boolean;
  ready: boolean;
}

export interface AccelStore {
  schema: string;
  database: string;
  tables: AccelStoreTable[];
  ready: boolean;
  may_create: boolean;
  /** For the DBA: the columns an earlier build left in this source's tables. */
  drops: string[];
}

/** What the Load tab shows. */
export interface AccelLoadStatus {
  source: string;
  built: AccelBuilt;
  store: AccelStore;
  running_pack: string;
  /** Whether SDM runs on the built pack (analysis needs it). */
  analyzing: boolean;
  loads: { table: string; rows: number; error: string | null; finished_at: string | null }[];
}

/** What a read of a source found: counts and names only. */
export interface AccelSummary {
  tables: number;
  columns: number;
  declared_keys: number;
  inferred_keys: number;
  shortlisted: number;
  skipped: { table: string; analysis: string; reason: string }[];
  hubs: { table: string; referenced_by: number; rows: number }[];
  code_tables: string[];
  date_pair_tables: Record<string, { start: string; end: string; placeholders: number | null }[]>;
  person_like_tables: Record<string, string[]>;
  login_can_write: boolean;
  seconds: number | null;
}

export interface AccelJoin {
  from_table: string;
  from_columns: string[];
  to_table: string;
  to_columns: string[];
  kind: string;
  coverage: number | null;
}

export interface AccelLevel {
  id: string;
  table: string;
  column: string;
  label_table: string | null;
  label_column: string | null;
  path: AccelJoin[];
  distinct: number;
  per_value: number;
  kind: "walk" | "narrow";
  confidence: number;
  reason: string;
  selected: boolean;
  order: number;
}

export interface AccelFeature {
  id: string;
  column: string;
  kind: "in" | "range";
  derive: { op: string; from: string; unit: string } | null;
  distinct: number;
  null_rate: number;
  confidence: number;
  reason: string;
  selected: boolean;
}

export interface AccelCandidate {
  table: string;
  score: number;
  reasons: string[];
  lines_table: string | null;
}

export interface AccelRecordKind {
  table: string;
  key_columns: string[];
  lines_table: string | null;
  name: { one: string; many: string };
}

export interface AccelTable {
  table: string;
  serves: { role: AccelRole; level: string | null; as: string }[];
  joins: AccelJoin[];
}

export interface AccelAlternative {
  id: string;
  table: string;
  column: string;
  kind: "walk" | "narrow";
  distinct: number;
  label_column: string | null;
  confidence: number;
  reason: string;
}

export interface AccelPlan {
  source: string;
  read_at: string;
  status: string;
  record_kinds: Record<AccelRole, AccelRecordKind | null>;
  candidates: Record<AccelRole, AccelCandidate[]>;
  hierarchies: Record<AccelRole, { levels: AccelLevel[]; features: AccelFeature[] }>;
  tables: AccelTable[];
  notes: string[];
  criteria: Record<AccelRole, AccelCriterion[]>;
  structure: AccelDenormTable[];
  pack: { id: string | null; name: string | null };
}

/** Which rows the extract keeps. Values are only those the engine kept
 *  from the profile or the person typed. */
export interface AccelCriterion {
  id: string;
  role: AccelRole;
  table: string;
  kind: "in_force" | "window" | "values";
  columns: string[];
  values: string[];
  months: number | null;
  op: "in" | "not_in";
  selected: boolean;
  /** The share of rows it keeps, once measured. */
  share_kept: number | null;
  reason: string;
}

export type AccelPrivacy = "kept" | "withheld" | "surrogate";

export interface AccelDenormColumn {
  name: string;
  source_table: string;
  source_column: string;
  sql_type: string;
  serves: "key" | "lines_key" | "level_key" | "level_label" | "feature" | "derive_input" | "extra";
  privacy: AccelPrivacy;
  proposed: AccelPrivacy;
  reason: string;
  /** Never kept: it identifies a person. */
  locked: boolean;
  joins: boolean;
  /** The person's word that a key proposed as a surrogate holds nothing personal. */
  confirmed_not_personal: boolean;
}

/** A link table a record kind's levels enter with no dates to pick a row
 *  by: the records repeat once per link row, until the person accepts it. */
export interface AccelGrainLink {
  table: string;
  levels: string[];
  accepted: boolean;
}

/** A join from the child's record table to the primary's. */
export interface AccelPrimaryJoin {
  from_table: string;
  from_columns: string[];
  to_table: string;
  to_columns: string[];
  kind: string;
  /** The one the person named. */
  named: boolean;
}

/** A table the extract fills, one for each record kind. */
export interface AccelDenormTable {
  role: AccelRole;
  name: string;
  grain: "record" | "lines";
  columns: AccelDenormColumn[];
  pick: { table: string; start: string; end: string }[];
}

/** The proposal as the screen shows it; `plan` is null before one is made. */
export interface AccelView {
  source: string;
  plan: AccelPlan | null;
  stale: boolean;
  stale_reason: string | null;
  alternatives: Partial<Record<AccelRole, AccelAlternative[]>>;
  /** Criteria whose share was counted on a sample: an estimate. */
  estimated: string[];
  grain: Record<AccelRole, AccelGrainLink[]>;
  primary_joins: AccelPrimaryJoin[];
}

/** Where the plan stands. "approved" only while the plan saved is the one
 *  approved (the server checks its hash); "changed" when it is not. */
export interface AccelApproval {
  source: string;
  stale: boolean;
  status: "draft" | "pending_review" | "approved" | "changed";
  plan_hash: string;
  submitted_by: string | null;
  submitted_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
  confirmed_by: string | null;
  built_at: string | null;
  /** What is left to do before the plan can be submitted. */
  problems: string[];
  second_approver: string | null;
  approver_unreadable: boolean;
  pack: { id: string | null; name: string | null };
}

/** The files a build writes, each with whether it is built. */
export interface AccelFiles {
  source: string;
  pack_id: string;
  folder: string;
  packs_dir: string;
  files: { name: string; built: boolean; bytes: number | null }[];
}

/** One edit to a proposal, by the route that makes it. */
export type AccelEdit =
  | { path: "choose-record"; body: { role: AccelRole; table: string } }
  | { path: "select-level"; body: { role: AccelRole; level_id: string; selected: boolean } }
  | { path: "reorder"; body: { role: AccelRole; ids: string[] } }
  | { path: "swap"; body: { role: AccelRole; level_id: string; table: string; column: string } }
  | { path: "name"; body: { role: AccelRole; one: string; many: string } }
  | { path: "select-feature"; body: { role: AccelRole; feature_id: string; selected: boolean } }
  | { path: "criteria/select"; body: { id: string; selected: boolean } }
  | {
      path: "criteria/set";
      body: { id: string; values?: string[]; op?: "in" | "not_in"; months?: number };
    }
  | {
      path: "structure/privacy";
      body: { table: string; column: string; privacy: AccelPrivacy; confirm_not_personal?: boolean };
    }
  | { path: "structure/grain"; body: { role: AccelRole; table: string; accept: boolean } }
  | {
      path: "join";
      body: {
        role: AccelRole;
        from_table: string;
        from_columns: string[];
        to_table: string;
        to_columns: string[];
      };
    }
  | { path: "structure/extra"; body: { role: AccelRole; table: string; column: string } }
  | { path: "pack-name"; body: { id: string; name: string } };

/** An upload goes out as multipart, so it cannot use the JSON helper. */
async function unwrap<T>(response: Response): Promise<T> {
  if (!response.ok) throw await failure(response);
  return json<T>(response);
}

/* ------------------------------------------------------------------ */
/* Shapes                                                              */
/* ------------------------------------------------------------------ */

export type StepId =
  | "connect"
  | "discover"
  | "profile"
  | "map"
  | "review"
  | "patterns"
  | "output"
  | "run";

export interface Step {
  id: StepId;
  label: string;
  done: boolean;
  detail: string;
  value?: number | null;
  /** Discover and Profile: whether the structure was read, and measured. */
  read?: boolean;
  measured?: boolean;
  /** Map & Review: whether columns were matched, and rules proposed. */
  mapped?: boolean;
  proposed?: boolean;
}

export interface RatioSetting {
  kind?: string;
  value?: unknown;
  min?: unknown;
  max?: unknown;
  zero_share?: unknown;
}

export interface Settings {
  rootCounts: Record<string, number>;
  modes: Record<string, string>;
  drivingParents: Record<string, string>;
  /** Children per parent, where someone replaced what was measured. */
  ratios: Record<string, RatioSetting | null>;
  seed: string;
  planName: string;
}

export interface AppState {
  version: string;
  database: string;
  referenceDatabase: string;
  /** The database the analysis store, and so every pattern, was read from. */
  landscape?: string;
  server: string;
  steps: Step[];
  settings: Settings;
  /** Off where this installation is to make files only. */
  databaseGeneration?: boolean;
  /** The database worked on is production: never read or written from here. */
  workingProtected?: boolean;
}

export interface ConnectResult {
  checks: { connection: string; ok: boolean; db?: string; error?: string }[];
}

export interface InferredKey {
  childTable: string;
  childColumn: string;
  parentTable: string;
  parentColumn: string;
  coverage: number;
  distinctValues: number;
  evidence: string[];
}

export interface DiscoverResult {
  tables: number;
  columns: number;
  declared_foreign_keys: number;
  inferred_foreign_keys: number;
  buckets: Record<string, number>;
  inferred: InferredKey[];
  seconds: number;
}

export interface ProfileResult {
  columns: number;
  formatsFitted: number;
  dependencies: number;
  orderings: number;
  ratios: number;
  skipped: { table: string; analysis: string; reason: string }[];
  seconds: number;
}

export interface MapResult {
  auto_map_rate: number;
  high: number;
  medium: number;
  low: number;
  unmapped: number;
  mappable_columns: number;
  system_columns: number;
  high_confidence_by_evidence: Record<string, number>;
  seconds: number;
}

export interface ReviewResult {
  proposals: number;
  byConfidence: Record<string, number>;
  queued: number;
  seconds: number;
}

export type Provenance =
  | "structural"
  | "catalog"
  | "learned"
  | "user"
  | "physical";
export type Confidence = "high" | "medium" | "low";
export type Bucket =
  | "system"
  | "reference"
  | "code"
  | "derivable"
  | "business_key"
  | "free";

export interface Evidence {
  kind: string;
  detail: string;
  coverage?: number | null;
  score?: number | null;
}

export interface TopValue {
  value: string | number | boolean | null;
  count: number;
  share: number;
}

export interface ColumnProfile {
  rowCount: number;
  nullRate: number;
  distinctCount: number;
  distinctRatio: number;
  cardinality: string;
  minimum: string | number | null;
  maximum: string | number | null;
  mean: number | null;
  stddev: number | null;
  minLength: number | null;
  maxLength: number | null;
  format: { regex: string; coverage: number; kind: string } | null;
  checkDigit: { scheme: string; share: number } | null;
  sequence: { is_sequence: boolean; step: number; max: number; gaps: number } | null;
  distribution: Record<string, unknown> | null;
  topValues: TopValue[];
}

export interface SchemaColumn {
  table: string;
  name: string;
  type: string;
  nullable: boolean;
  bucket: Bucket;
  bucketReason: string;
  isPrimaryKey: boolean;
  isIdentity: boolean;
  isComputed: boolean;
  profile: ColumnProfile | null;
  element: string | null;
  /** What a person has already said about this column, if anything. */
  decision: DecisionInput | null;
  mappingConfidence: Confidence | null;
  mappingAccepted: boolean;
  mappingReason: string;
  mappingEvidence: Evidence[];
  alternatives: string[];
  generator: string | null;
  params: Record<string, unknown>;
  /** What the tool worked out, kept whole so an override can be undone. */
  proposedElement: string | null;
  proposedGenerator: string | null;
  proposedParams: Record<string, unknown>;
  provenance: Provenance | null;
  confidence: Confidence | null;
  coverage: number | null;
  evidence: Evidence[];
  note: string;
}

export interface ForeignKeyInfo {
  kind: "declared" | "inferred";
  name: string | null;
  columns: string[];
  parentTable: string;
  parentColumns: string[];
  coverage: number | null;
  evidence: string[];
}

export interface SchemaTable {
  name: string;
  schema: string;
  table: string;
  rowCount: number;
  primaryKey: string[];
  triggers: string[];
  foreignKeys: ForeignKeyInfo[];
  columns: SchemaColumn[];
}

export interface RatioProfile {
  child_table: string;
  child_column: string;
  parent_table: string;
  parent_column: string;
  kind: string;
  parents: number;
  children: number;
  minimum: number;
  maximum: number;
  mean: number;
  stddev: number;
  median: number;
  p90: number;
  median_nonzero: number;
  p90_nonzero: number;
  zero_share: number;
  histogram: { children: number; weight: number }[];
  proposal: { kind: string; value?: number; reason: string };
  evidence: string;
}

export interface SchemaPayload {
  tables: SchemaTable[];
  dependencies: {
    table: string;
    determinant: string;
    dependent: string;
    distinct_determinant: number;
  }[];
  orderings: {
    kind: string;
    table: string;
    earlier: string;
    later: string;
    share_in_order: number;
    gap_min: number | null;
    gap_max: number | null;
  }[];
  ratios: RatioProfile[];
}


export interface DecisionInput {
  table: string;
  column: string;
  approved?: boolean;
  element?: string | null;
  generator?: string | null;
  params?: Record<string, unknown> | null;
  /** Drop the override and go back to what the tool worked out. */
  reset?: boolean;
}

export interface ParamField {
  name: string;
  label: string;
  type: "text" | "number" | "integer" | "boolean" | "select" | "lines" | "numbers";
  required?: boolean;
  help?: string;
  placeholder?: string;
  options?: string[];
}

export interface GeneratorSpec {
  name: string;
  label: string;
  summary: string;
  fields: ParamField[];
}

export interface GeneratorsPayload {
  generators: GeneratorSpec[];
  structuralWarning: string;
}

export interface RatioShapesPayload {
  shapes: GeneratorSpec[];
}

export interface PlanField {
  name: string;
  bucket: Bucket;
  element: string | null;
  generator: string;
  params: Record<string, unknown>;
  provenance: Provenance;
  confidence: Confidence | null;
  coverage: number | null;
  userOverride: boolean;
  nullRate: number | null;
  dataType: string | null;
  evidence: Evidence[];
  overrode: { provenance: Provenance; generator: string; reason: string }[];
}

export interface PlanTable {
  name: string;
  mode: "generate" | "reuse" | "exclude";
  level: number | null;
  drivingParent: string | null;
  /** Every parent this table could be driven by, the builder's own list. */
  parents: string[];
  count: number | null;
  effectiveCount: number | null;
  ratioMean: number | null;
  /** True when the shape was stated by hand rather than measured. */
  ratioOverride: boolean;
  ratio: {
    parent: string;
    kind: string;
    value?: number;
    zero_share?: number;
    min?: number;
    max?: number;
    histogram?: { children: number; weight: number }[];
    evidence?: Evidence[];
    provenance?: Provenance;
  } | null;
  fields: PlanField[];
}

export interface PlanWarning {
  code: string;
  severity: "block" | "warn";
  message: string;
  table?: string;
  field?: string;
}

export interface ReferenceSetInfo {
  table: string;
  size: number;
  available: number;
  strategy: string;
  filter: string | null;
  describe: string;
}

export interface PlanPayload {
  built: boolean;
  /** Built before a rule, a pattern or a count changed; rebuilt on arrival. */
  stale?: boolean;
  meta?: { name: string; version: string; seed: string; created: string };
  generationOrder?: string[];
  tables?: PlanTable[];
  warnings?: PlanWarning[];
  blocking?: number;
  provenance?: Record<string, number>;
  totalRows?: number;
  references?: Record<string, ReferenceSetInfo>;
}

export interface DryRunPayload {
  tables: {
    name: string;
    columns: string[];
    rows: Record<string, unknown>[];
    generated: number;
  }[];
  seconds: number;
}

export interface GenerateResult {
  tables: { table: string; rows_written: number; identity_insert: boolean }[];
  rowsWritten: number;
  seconds: number;
  triggersFired: string[];
  // The load kept for audit: its request, on the Requests tab.
  request: { request: number; file: number } | null;
}

export interface ValidateResult {
  checks: number;
  failed: number;
  passed: boolean;
  by_kind: Record<string, { passed: number; failed: number }>;
  results: {
    name: string;
    kind: string;
    passed: boolean;
    detail: string;
    table: string | null;
    column: string | null;
  }[];
}

export interface CleanupResult {
  deleted: Record<string, number>;
  total: number;
  triggersFired: string[];
}

export interface RunPayload {
  hasRun: boolean;
  manifest: {
    run: { rows_written: number; seconds: number; database: string; finished: string };
    plan: { seed: string; name: string; version: string };
    tables: {
      table: string;
      rows_written: number;
      identity_insert: boolean;
      triggers_fired: string[];
    }[];
    validation?: { checks: number; failed: number; passed: boolean };
  } | null;
}

export interface CatalogElement {
  name: string;
  description: string;
  domain: string;
  rule: "generate" | "reuse" | "copy";
  generator: string | null;
  format: string | null;
  pii: "none" | "identifier" | "sensitive";
  consistencyScope: string;
  aliases: string[];
  checkDigit: string | null;
  reuseDomain: Record<string, unknown> | null;
  usedBy: string[];
  valueCount: number;
}

export interface CatalogPayload {
  organization: string;
  version: string;
  summary: Record<string, unknown>;
  elements: CatalogElement[];
}


/* ------------------------------------------------------------------ */
/* The pattern analyzer                                                */
/* ------------------------------------------------------------------ */

export interface Option {
  key: string;
  label: string;
}

/** A record's headline counts, by the names its pack gives them (glossary counts). */
export type Overview = Record<string, number>;

/** A pattern's filters: each scope filter the pack's loader takes, by its
 *  key (health: grgr_ck, cspd_cat, cspi_id, pdpd_id; a bank: branch_id,
 *  product_code), and a defined pattern's rules beyond them. */
export interface PatternFilters {
  conditions?: Record<string, PatternCondition>;
  [filter: string]: (string | number)[] | Record<string, PatternCondition> | undefined;
}

/** A child analysis's flat body: the filters beside k, and the record type
 *  chosen as the pack's type filter (typeFilter()). The server takes
 *  only the filters the child records' loader lists. */
function childBody(filters: PatternFilters, recordType: string | null | undefined) {
  const type = typeFilter();
  return {
    ...filters,
    ...(type ? { [type]: recordType ? [recordType] : null } : {}),
    conditions: filters.conditions ?? null,
  };
}

/** One of a list of values, or a range. */
export interface PatternCondition {
  in?: string[];
  min?: number;
  max?: number;
}

/** A feature a pattern can be defined by. `scope` ones are the hierarchy. */
/** A feature the clustering can find patterns on, as the Config screen lists it. */
export interface ReportField {
  key: string;
  label: string;
  column: string | null;
  /** One of the fields every pattern is described by; else a denorm column. */
  builtin: boolean;
  chosen: boolean;
}

export interface DiscoveryFeature {
  key: string;
  label: string;
  kind: "in" | "range";
  scope: boolean;
  /** Joined from the tables beside the denorm (provider, network, PCP). */
  joined: boolean;
  chosen: boolean;
  default: boolean;
}

export interface PatternFeature {
  key: string;
  label: string;
  kind: "in" | "range";
  scope?: boolean;
  /** A scope filter the server reads as ids: its values are sent as numbers. */
  numeric?: boolean;
  unit?: string;
  options?: { value: string; label: string | null; count: number }[];
  min?: number | null;
  max?: number | null;
}

export interface ContinuousStats {
  mean: number;
  std: number;
  min: number;
  max: number;
  median: number;
}

export interface CategoricalDist {
  counts: Record<string, number>;
  pct: Record<string, number>;
}

export interface DiscoveredPattern {
  cluster_id: number;
  name: string;
  size: number;
  pct_of_subset: number;
  pct_of_total: number;
  rare: boolean;
  weight_band: string;
  /** A claims pattern's claims, and its share of the scope's lines (its
   *  size counts lines; a claim's lines can sit in several patterns, so
   *  only the lines' shares add up to the whole). */
  n_claims?: number;
  pct_of_lines?: number;
  continuous: Record<string, ContinuousStats>;
  categorical: Record<string, CategoricalDist>;
  family: Record<string, number | null>;
  descriptions: Record<string, string[]>;
  /** Counts per bin of the analysis's `distributions`, feature by feature.
   *  Absent on patterns analyzed before histograms were recorded. */
  histograms?: Record<string, number[]>;
  /** Where the members live: address records with their shares. */
  geography?: Array<Record<string, string | number>>;
}

/** A continuous feature over the whole scope: round bin edges, the scope's
 *  count in each, and whether the last bin is open (values beyond the 99th
 *  percentile are counted into it rather than stretching the axis). */
export interface ScopeDistribution {
  edges: number[];
  counts: number[];
  open_top: boolean;
}

export interface AnalyzePayload {
  total_members: number;
  subset_members: number;
  pct_of_total: number;
  n_patterns: number;
  method: string;
  silhouette: number | null;
  patterns: DiscoveredPattern[];
  scope_label: string;
  distributions?: Record<string, ScopeDistribution>;
}

export interface PatternTarget {
  table: string;
  column: string;
  element: string | null;
  generator: string;
  params: Record<string, unknown>;
  parameter: string;
  relation: "subject" | "descendant" | "ancestor";
  depth: number;
  apply: "all" | "at_least_one";
  confidence: string | null;
  replaces: { generator?: string; params?: Record<string, unknown> };
  note: string | null;
}

export interface PatternRejection {
  parameter: string;
  reason: string;
  candidates: string[];
}

export interface ToPlanPayload {
  pattern: {
    id: string;
    kind: string;
    label: string;
    support: number | null;
    parameters: { label: string; value?: unknown; min?: number; max?: number;
                  values?: string[]; weights?: number[]; unit?: string | null }[];
  };
  // None in a preview while no plan is built: worked out when loaded.
  resolution: {
    pattern: string;
    subject: string | null;
    targets: PatternTarget[];
    rejections: PatternRejection[];
  } | null;
  applied: string[];
  plan?: "none";
}



/* ------------------------------------------------------------------ */
/* Discovery, saved patterns, stories, layouts                         */
/* ------------------------------------------------------------------ */

export interface DiscoveredRow {
  pattern_id: number;
  entity: string;
  cluster_id: number;
  name: string;
  size: number;
  pct_of_population: number;
  silhouette: number;
  combo: PatternFilters;
  /** Where it was found, broadest first ("Acme › Medical"): none is the book. */
  scope?: string[];
  saved?: boolean;
}

export interface DiscoveryPayload {
  total_population: number;
  combinations_processed: number;
  n_discovered: number;
  patterns: DiscoveredRow[];
}

/**
 * Who a file is for. Nothing: the plan's run. A pattern: new members carrying
 * its values, or claims for the members it holds. Saved patterns are named by
 * id; one an analysis just found is named by the analysis that found it.
 */
export interface ScenarioRequest {
  domain: Domain;
  filters: PatternFilters;
  k: number | null;
  cluster_id: number;
  claim_type: string | null;
  dimensions: string[];
  /** A saved pattern, by its id, in place of an analysis's address. */
  pattern_id?: number | null;
}

export type DownloadFormat = "xlsx" | "pdf" | "csv";

/** The download types as a new user knows them. */
export const DOWNLOAD_FORMATS: { value: DownloadFormat; label: string }[] = [
  { value: "xlsx", label: "Excel" },
  { value: "pdf", label: "PDF" },
  { value: "csv", label: "CSV (plain text)" },
];

export interface PatternReportRequest {
  domain: Domain;
  filters: PatternFilters;
  k: number | null;
  cluster_id?: number;
  claim_type?: string | null;
  pattern_id?: number;
  scenario_keys?: string[];
  /** A download: the pattern's name, saved patterns by id ([] = all), the file type. */
  name?: string;
  pattern_ids?: number[];
  format?: DownloadFormat;
}

export interface PatternReportRow {
  field: string;
  field_label: string;
  value: string;
  value_label: string;
  count: number;
  basis: string;
  of: number;
  pct: number;
  generation: string;
}

export interface PatternReport {
  entity: Domain;
  size: number;
  size_basis: string;
  rows: PatternReportRow[];
  scenario_keys: { key: string; label: string }[];
  scenarios: { values: Record<string, string>; label: string; count: number; pct: number }[];
}

export interface ScenarioList {
  dimensions: { key: string; label: string }[];
  rows: number;
  scenarios: { values: Record<string, string>; label: string; rows: number; share: number }[];
  more: number;
}

export type FilePattern =
  | { kind: "saved"; id: number; name: string; entity: string; members: number }
  | {
      // An earlier generation request: more files for the same people.
      kind: "request";
      requestId: number;
      name: string;
      entity: string;
      members: number;
      started: string;
    }
  | {
      // Several scenarios of one pattern, written into one file.
      kind: "scenarios";
      name: string;
      entity: string;
      scenarios: { pattern_id: number; label: string; count: number }[];
      members: number;
    }
  | {
      kind: "live";
      name: string;
      domain: Domain;
      pattern: DiscoveredPattern;
      filters: PatternFilters;
      k: number | null;
      claimType: string | null;
      members: number;
    };

/** The part of an export request that names a pattern. */
export function patternRequest(
  chosen: FilePattern | null,
  newMembers: number,
): Record<string, unknown> {
  if (!chosen) return {};
  if (chosen.kind === "saved") return { pattern_id: chosen.id, members: newMembers };
  if (chosen.kind === "request") return { request_id: chosen.requestId, members: newMembers };
  if (chosen.kind === "scenarios") return { scenarios: chosen.scenarios };
  return {
    members: newMembers,
    pattern: {
      profile: chosen.pattern,
      domain: chosen.domain,
      filters: chosen.filters,
      k: chosen.k,
      cluster_id: chosen.pattern.cluster_id,
      claim_type: chosen.claimType,
    },
  };
}

export interface FilePatternOption {
  pattern_id: number;
  name: string;
  domain: Domain;
  member_count: number;
  saved: boolean;
  origin?: string;
}

export interface SavedPattern {
  pattern_id: number;
  name: string;
  entity: string;
  member_count: number;
  pct_of_population: number;
  silhouette: number;
  /** Where it was found or defined, broadest first. */
  scope?: string[];
  /** Its name at each scope level, in the levels' order ("All" where it spans one). */
  scope_levels?: (string | null)[];
  saved_at: string;
  /** "user_defined" for a pattern somebody defined by its rules. */
  origin?: string;
  /** Found under an earlier build of a generated pack: kept read-only. */
  archived?: boolean;
  plan_version?: string | null;
  /** The analysis it was saved from, where it was saved from one: what the
   *  Find screen's Not saved matches found patterns on. `records` is set when
   *  only chosen records were saved, which is not the whole pattern. */
  saved_from?: {
    domain: Domain;
    filters: PatternFilters;
    k: number | null;
    cluster_id: number;
    claim_type: string | null;
    records?: number | null;
  } | null;
}

export interface SaveResult {
  pattern_id: number;
  saved: boolean;
  name: string;
}

/** The rules a requirement states, and the words each was read from. */
export interface StoryReading {
  story: string;
  entity: Domain;
  rules: Record<string, PatternCondition>;
  found: { feature: string; label: string; rule: PatternCondition; from: string }[];
  count: number | null;
}

/** A story is a line of prose. The analyzer reads and matches them as text. */
export interface StoryParsePayload {
  stories: string[];
  column_used?: string | null;
  filename?: string | null;
  warning?: string;
}

export interface StoryCandidate {
  pattern_id: number;
  name: string;
  match_score: number;
  match_count?: number;
  member_count?: number;
  /** Where it was found, broadest first. */
  scope?: string[];
  /** Its name at each scope level, in the levels' order. */
  scope_levels?: (string | null)[];
}

export interface StoryMatched {
  index: number;
  story: string;
  data_type: string;
  volume: number | null;
  keywords: string[];
  confidence: number;
  matched: boolean;
  candidates: StoryCandidate[];
}

export interface StoryMatchPayload {
  stories: StoryMatched[];
  warning?: string;
}

export interface Layout {
  id: number;
  name: string;
  kind: string;
  delimiter: string | null;
  status: string;
  purpose: string;
  sample_name: string | null;
  created_by: string | null;
  created_at: string;
  approved_by: string | null;
  approved_at: string | null;
  // Who confirmed the approval at the computer (the operating-system user).
  confirmed_by?: string | null;
  n_fields?: number;
}

export interface LayoutField {
  name: string;
  is_default?: boolean;
  index?: number;
  start?: number;
  length?: number;
  max_width?: number;
  rule_label?: string;
  type?: string;
  rule_id?: number | null;
  rule_name?: string | null;
  gen_method?: string;
  /** How the rule was found: by its name, an alias, or picked by a person. */
  matched_by?: "exact" | "alias" | "unmatched" | "chosen" | "blank";
  /** Chosen to be written empty, whatever the field is called. */
  blank?: boolean;
  /** The sample's date format, which the written file keeps. */
  date_format?: string | null;
  samples?: string[];
}

export interface FileSource {
  folder: string | null;
  files: { name: string; size: number; modified: string }[];
}

export interface FileProfile {
  name: string;
  kind: string;
  delimiter?: string | null;
  row_count: number;
  error?: string;
  fields: (LayoutField & { empty_rate?: number; distinct?: number })[];
}

/** Picking a rule for a field remembers its name as an alias where it can. */
export interface AliasOutcome {
  aliased?: string[];
  not_aliased?: { name: string; reason: string }[];
}

/** Saving reports what was written, not an id: the name is the identity. */
export interface SavedLayoutResult extends AliasOutcome {
  name: string;
  status: string;
  purpose: string;
  n_fields: number;
  n_default: number;
}

export interface LayoutDetail extends Layout {
  fields: LayoutField[];
  n_default?: number;
}

export interface LayoutEditBody {
  name?: string;
  delimiter?: string | null;
  fields?: LayoutField[];
}

export interface LayoutEditResult extends AliasOutcome {
  id: number;
  name: string;
  status: string;
  n_fields: number;
  n_default: number;
  reapproval_required: boolean;
}

export interface SaveLayoutBody {
  name: string;
  kind: string;
  delimiter?: string | null;
  fields: LayoutField[];
  sample_name?: string | null;
  purpose?: string;
  /** Refuse rather than replace a layout already called this. */
  as_new?: boolean;
}

export interface LayoutParsePayload {
  kind: string;
  delimiter?: string | null;
  fields: LayoutField[];
  sample_name?: string;
  n_default?: number;
}

export interface GenerationRule {
  id: number;
  field_name: string;
  field_label?: string;
  field_category?: string;
  gen_method?: string;
  data_type?: string;
  length?: number | null;
  prefix?: string | null;
  postfix?: string | null;
  start_value?: number | null;
  domain?: string;
  format_pattern?: string | null;
  lookup_source?: string | null;
  /** The element this rule produces; its name is the same. */
  element?: string | null;
  /** A number's measured range and places, for a `range` rule. */
  min_value?: number | null;
  max_value?: number | null;
  decimals?: number | null;
  /** How often each value of `domain` occurs, comma separated, in its order. */
  weights?: string | null;
  /** How a `range` rule's numbers fall between its bounds, as the plan
   *  measured them; mean and deviation are of the underlying normal. */
  distribution?: string | null;
  mean_value?: number | null;
  stddev_value?: number | null;
  active?: boolean;
  /** Names of the layouts that point at this rule. */
  used_by?: string[];
  /** Other column names that mean this element. */
  aliases?: string[];
}

/**
 * The source rows behind one pattern.
 *
 * Not generated data: these are the rows the pattern was derived from, which
 * is what makes its claim about the book checkable. `phi_columns` names the
 * ones carrying personal data — the server sends their real values, so
 * whatever displays them decides whether to show them.
 */
export interface PatternRowsPayload {
  pattern_id: number | null;
  name: string;
  entity: string;
  columns: string[];
  phi_columns: string[];
  rows: Array<Record<string, unknown>>;
  /** Each row's name, aligned with `rows`: what a choice of records sends. */
  keys: string[];
  total: number;
  offset: number;
  limit: number;
  returned: number;
  has_more: boolean;
  truncated: boolean;
  rebuild_ms: number;
}

/** One SNIP level's outcome. */
export interface SnipLevel {
  level: number;
  name: string;
  status: "pass" | "fail" | "warn" | "partial" | "not_checked";
  rules: number;
  errors: number;
  warnings: number;
  notes: string[];
  findings: { level: number; rule: string; message: string; segment?: number; raw?: string; severity: string }[];
}

export interface SnipReport {
  transaction: string;
  version: string;
  segments: number;
  transactions: number;
  partner: string | null;
  passed: boolean;
  levels: SnipLevel[];
  disclaimer: string;
  file?: string;
  /** Another format's report says what it checked in a sentence (no segments, no partner). */
  summary?: string;
}

export interface SnipRules {
  rules: { origin: string; file: string; id: string; transaction?: string; version?: string; name?: string; rules: number }[];
  partners: string[];
  customerDir: string;
}

/** A database onboarded here: read, and perhaps planned and generated into. */
export interface OnboardedDatabase {
  key: string;
  server: string;
  database: string;
  current: boolean;
  planned: boolean;
  /** Rows generated into it that have not been cleaned up. */
  rowsInIt: boolean;
}

/** A pattern to save: the whole of an analyzed one, or chosen records of it. */
export interface KeepPatternRequest {
  domain: Domain;
  filters: PatternFilters;
  k: number | null;
  cluster_id: number;
  claim_type: string | null;
  record_keys?: string[];
  name?: string;
  /** "user_defined" for a pattern somebody defined by its rules. */
  origin?: string;
}

export interface KeptPattern {
  pattern_id: number;
  /** False when the same pattern was saved before: `name` is the one it has. */
  created?: boolean;
  /** True when it had been deleted from Saved and saving it brought it back. */
  restored?: boolean;
  name: string;
  domain: Domain;
  member_count: number;
  records: number | null;
  /** The saved pattern itself, in the shape an analysis gives one, so it can
   *  be resolved onto the plan for the database route. */
  profile: DiscoveredPattern;
}

/** A target on a server of its own. */
export interface TargetConnection {
  database: string;
  server?: string;
  port?: number;
  /** Windows authentication: no password is stored anywhere. */
  trusted?: boolean;
  user?: string;
  password?: string;
  driver?: string;
  trustCert?: boolean;
}

export interface StoredTarget {
  configured: boolean;
  database?: string;
  server?: string;
  trusted?: boolean;
  user?: string;
  hasPassword?: boolean;
}

/** One EDI transaction set this installation can write. */
/**
 * One place in a sample file, and what generation does with it. How the place
 * is addressed (X12: segment_id, qualifier, position...) is the format's own,
 * carried back unread when the config is saved.
 */
export interface SamplePosition {
  [address: string]: unknown;
  label: string;
  example: string;
  count: number;
  /** What the master config names here, as it names it. */
  config_name: string | null;
  /** The SDM algorithm that makes this position's value; null copies the sample. */
  algorithm: string | null;
  reason: string;
  suggested: string | null;
  /** Kept as the sample has it: the format never makes this place (not the member's). */
  kept?: boolean;
}

/** What a file is written for: the pack's primary records, or their children. */
export type SampleSubject = Domain;

export interface SampleReading {
  /** How the sample is read: x12. */
  format: string;
  /** What it is within that format: 834, 837. */
  kind: string;
  version: string;
  sampleName: string | null;
  basedOn: string | null;
  /** The standard file read against, when one was. */
  defaultId?: string | null;
  counts: Record<string, number>;
  /** The counts as one line for the screen. */
  summary: string;
  subject: SampleSubject;
  /** The checks a written file can be run through ("snip"), or none. */
  validation: string | null;
  /** What a file of it is saved as: .txt, .json, .hl7, .zip. */
  extension?: string;
  rows: SamplePosition[];
  algorithms: string[];
  /** Elements a config can make optional or repeat (FHIR, CDA); null for other formats. */
  structure: SampleElement[] | null;
  /** What the config read already varies. */
  vary: SampleVary[];
}

/** An uploaded sample: X12, FHIR, HL7 v2 or HL7 v3 (cda), or a flat file. */
export type SampleDetected =
  | { format: "flat" }
  | {
      format: string;
      label: string;
      kind: string;
      version: string;
      subject: SampleSubject;
      defaultId: string | null;
    };

/** A sample file handed from one screen to another, already read. */
export interface HandedSample {
  name: string;
  text: string;
  format: string;
}

export interface LayoutApproval {
  secondApprover: string | null;
  required: boolean;
}

export interface SampleDefault {
  id: string;
  /** x12, fhir, hl7v2 or cda. */
  format: string;
  name: string;
  sampleName: string;
}

export interface GenConfigSummary {
  id: number;
  name: string;
  format: string;
  kind: string;
  subject: SampleSubject | null;
  validation: string | null;
  /** The written file's extension, with its dot. */
  extension: string;
  version: string | null;
  based_on: string | null;
  sample_name: string | null;
  created_by: string | null;
  created_at: string;
  positions: number;
}

export interface EdiFormatSummary {
  id: string;
  name: string;
  transaction: string;
  subject: string;
  version: string;
  description: string;
}

export interface DatabaseRow {
  name: string;
  online: boolean;
  isSource: boolean;
  isStore: boolean;
  isTarget: boolean;
  // Production: never read or written from the screens.
  protected: boolean;
}

export interface DatabasesPayload {
  databases: DatabaseRow[];
  source: string;
  store: string;
  target: string;
  server: string;
}


export interface RulesPayload {
  rules: GenerationRule[];
  n_active: number;
  lookups: string[];
  /** method -> what it does, in the server's words. */
  methods: Record<string, string>;
}

/** One generated column, as the plan describes it, before it has a rule. */
export interface SeedColumn {
  column: string;
  table: string;
  element: string | null;
  generator: string;
  method: string;
  data_type: string;
  label: string;
  category: string;
}

export interface SeedPayload {
  // False until a plan is built for the database being worked on.
  has_plan?: boolean;
  columns: SeedColumn[];
  missing: SeedColumn[];
  n_columns: number;
  n_existing: number;
  n_missing: number;
}

export interface NewRule {
  field_name: string;
  field_label?: string | null;
  field_category?: string;
  gen_method: string;
  data_type?: string;
  length?: number | null;
  prefix?: string | null;
  postfix?: string | null;
  start_value?: number | null;
  domain?: string | null;
  format_pattern?: string | null;
  lookup_source?: string | null;
  min_value?: number | null;
  max_value?: number | null;
  decimals?: number | null;
  weights?: string | null;
  distribution?: string | null;
  element?: string | null;
  mean_value?: number | null;
  stddev_value?: number | null;
}


/** A claims pattern describes claim lines, so it counts and names differently. */
export interface ClaimsPattern extends DiscoveredPattern {
  n_claims?: number;
  n_members?: number;
  pct_of_lines?: number;
  approval_rate?: number;
  in_network_rate?: number;
  top_diagnoses?: Record<string, number>;
  top_procedures?: Record<string, number>;
  diagnosis_descriptions?: Record<string, string>;
  procedure_descriptions?: Record<string, string>;
}

export interface ClaimsAnalyzePayload {
  subset_claims: number;
  subset_claim_lines: number;
  book_claims: number;
  n_patterns: number;
  method: string;
  silhouette: number | null;
  scope_label: string;
  patterns: ClaimsPattern[];
  distributions?: Record<string, ScopeDistribution>;
}


export interface StoryPlanned {
  pattern_id: number;
  rows: number;
  pattern: string;
  subject: string | null;
  share: number | null;
  targets: PatternTarget[];
  rejections: PatternRejection[];
}

export interface StoryToPlanPayload {
  allocation: Record<string, number>;
  total_volume: number;
  patterns: StoryPlanned[];
  unreadable: number[];
  applied: string[];
}


export interface CodeCount {
  code: string;
  description: string | null;
  n: number;
}

/** One of a record's highlights: its most frequent codes, most first. */
export interface Highlight {
  key: string;
  label: string;
  codes: CodeCount[];
}

/** Which records a screen is asking about: an entity key of the business
 *  pack (lib/pack.ts says what it is called and the role it plays). Held by
 *  the shell, so one choice follows you across the screens rather than being
 *  made on each. */
export type Domain = string;

/** An element of a sample a config can make optional or repeat. */
export interface SampleElement {
  at: string;
  /** The most times the sample has it in one place. */
  count: number;
  repeats: boolean;
}

/** In what share of documents an element is, and how many times it then occurs. */
export interface SampleVary {
  at: string;
  present: number;
  min: number | null;
  max: number | null;
  // How many times, each in its share: [[1, 50], [2, 30], [3, 20]].
  mix?: [number, number][] | null;
}
