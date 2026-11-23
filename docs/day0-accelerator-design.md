# Source accelerator: day 0 at a new customer

Status: **approved by the product owner, 2026-11-20.** This is the design being
built. It was formed from the product owner's purpose and the code first; section 9
compares it with the earlier `docs/source-accelerator-design.md` (revision 4), whose
refresh lock, drift check and stop-between-tables are kept as phase 3 and 4 items.

Decisions (product owner, 2026-11-20):
- One primary and one child record kind per customer is enough for the first release.
- 837 files stay as they are: hand work, not generated.
- A one-time restart of SDM after the build is acceptable.

## 1. Purpose and who it serves

The on-ground team at a new customer: one senior data engineer, sometimes two, in the first week. Today they read hundreds of tables by hand, decide the hierarchy, design the denorm, write the extract SQL, and author a pack. The health pack's `features.json` alone is 1,700 lines of hand-written configuration, and `FACETS.sql` is 1,584 lines.

The accelerator makes the tool do the reading and write the first draft of everything, and leaves the person the decisions: what the records are, what they are called, which levels matter, what "current" means. Its output is a business pack and a filled pattern store, so Patterns works on day 1 the way it works for health today.

## 2. Day-0 steps today, and what the tool takes over

| Step | Today (by hand) | With the accelerator | Honest label |
|---|---|---|---|
| Connections, logins, `.env` | `customer.env.example`, DBA for rights | Same, plus a source list on screen | by hand |
| Understand the landscape | Read `sys.*`, find keys, profile | `sdg/discovery` + `sdg/profiling`, run light, per source | automated |
| Define the hierarchy | Person decides group → plan → product → member | Tool proposes record kinds and levels with evidence; person picks, orders, names | assisted |
| Identify source tables | Person traces joins | Derived from the chosen levels; realigns on every change | assisted |
| Extraction criteria | Person writes "in force today", 24 months | Tool proposes from date pairs and status codes, shows share kept; person sets | assisted |
| Denorm structure | Person writes DDL | Generated from the approved plan | automated |
| Extract/load SQL | 1,500 lines by hand | Generated in the pack's `-- table:` format; editable | automated |
| Pack files | Hand-authored JSON | Generated; words from the person | automated |
| Refresh | `extract.cmd` on Tidal | Same runner, plus a button | exists |

What the tool cannot do: know what a group *is*, or that `MEPE_ELIG_IND = 'Y'` means eligible. It shows the value spread and the person picks.

## 3. The design

One new package, `sdg/accelerator/`, one contract file (the **source plan**, like the generation plan is for loads), one rail entry **Source setup** with tabs in work order: Sources, Landscape, Hierarchy, Tables, Criteria, Structure, Approval, Build, Load. Long work runs through `sdg/api/jobs.py` and the screen polls. The plan is kept in the store (`sdm.source_plans`: id, source, status, plan_json, approved_by, approved_at, built_hash) because the team shares the store across the VDI and laptops, as patterns are shared today.

**A. Landscape.** Inputs: named sources, each registered in `ConnectionRegistry` (`register_connection`) with `reads_production=True` for this step only. Does: `discovery.run.discover` (metadata, inferred keys, buckets), then `profiling.run.profile` on a shortlist (tables by row count, tables referenced by others), under `SDG_PROFILE_MAX_MINUTES`, with pairwise analyses on the shortlist only. Outputs: the existing `PhysicalModel` and `ProfileReport` JSON under `work/accelerator/<source>/`, personal values already withheld by `profiling/personal.py`. Shown: tables by size, hub tables (most referenced), code tables, date-pair tables, person-like tables (catalog pii classes). Person: mark schemas or tables out of scope (audit, backup, log). Reused: all of `sdg/discovery`, `sdg/profiling`, `sdg/discovery/classify.py`.

**B. Hierarchy proposal** (`sdg/accelerator/hierarchy.py`, pure functions over model and profile, no database).
- *Record kinds*: tables scored on row count, in-degree of keys (declared plus inferred), business key, person or date columns. Top candidates for primary; a child is a table keyed to the primary with mean children per parent above 1 (`profiling/ratio.py`); a line table is one whose key is the child key plus a sequence. The person picks the pair; structure alone gets the bank wrong (transactions point at accounts).
- *Levels per record kind*: parent tables reachable by key, or by a bridge table with effective/term dates, with 2 to a few thousand distinct values, plus a label column (text, distinct ratio near 1, tokens NAME or DESC). Code columns on the record (2 to 50 values) are levels too. Order broadest first; keep only chains that nest (functional dependency from the profile, or one `COUNT(DISTINCT)` pair query on the sample). A level with too many values is a narrowing level, not a walk level, matching how `scope_walk._levels` stops at `combo_key`.
- *Features*: code columns (`kind: in`), amounts (`range`), a birth-like date → `_age`, an original effective date → `_tenure` (the `derive: elapsed` op the loader already has).
- Several hierarchies: one per record kind, as `loaders.<entity>.scope` is today. The person names each level and record kind; those words become `glossary.json`.

Outputs: the `hierarchy` section of the plan with each level's table, key, label, path, counts, confidence, alternatives.

**C. Source tables.** Derived from B, never typed: record table, line table, each level's table, label tables, bridge tables, with the join path and its evidence (declared or inferred, coverage). Realigns on every change in B from the figures already read. Person: confirm, swap a table, or state a join where inference found none (composite keys, see risks).

**D. Extraction criteria.** Proposed: for each eff/term date pair an "in force on the extract date" filter (the `9999` placeholder counts in the profile show which pairs are these); for a status code column with a check list, the dominant value; for the child, a window on its widest date column (default 24 months). Every criterion shows the share of rows it keeps, one `COUNT` each, sampled on huge tables, with a query timeout. Criteria are stored as the conditions `sdg/plan/filters.py` already parses, not free text. Personal columns: proposed Withheld (NULL) or Surrogate by catalog pii class; identifying columns cannot be made Kept; surrogates use `SDG_EXTRACT_KEY` as now.

**E. Denorm structures.** One `<kind>_denorm` per analyzed record kind: keys, each level's key and label, features, the dates derived features need; child rows at line grain with the primary's key. Types copied from the source columns in the model. Column names stay the source's (so the generated `features.json` matches by construction); clashes get the table prefix. Person: tick extra columns, nothing else.

**F. Approval.** Same shape as layouts (`sdm.layouts`): `pending_review` → `approved`, `approved_by`, `approved_at`, optional second approver from the existing setting. Any edit drops approval. Before approval: both record kinds named, every walk level has a path, every column has a privacy class, every column a configuration key names exists in the structure.

**G. Build.** One click from an approved plan: write the pack folder under `SDG_PACKS_DIR/<id>/` (`pack.json`, `features.json`, `glossary.json`, `story.json`, `column_names.json`, `catalog.yaml`, `sql/store_tables.sql`, `extract/<SOURCE>.sql`), validate it with `packs.check` and a full load in a fresh process, then create the tables add-only the way `seed/bootstrap.ensure_store` does, or hand the script to the DBA when the login cannot create (today's customer path). A later change makes `ALTER TABLE ADD` only; drops are listed for the DBA. The screen then says: set `SDG_PACK=<id>`, restart. Scripts tab: view and download the generated SQL; edits go in the pack file, as `FACETS.sql` is edited today.

**H. Load and refresh.** **Load now** runs `sdg.extract.run.extract(source)` as a job, per-table progress, result from `sdm.extract_log`. **Refresh** is the same button, on demand. The extract stays the one path that reads a protected source. After a load: row count against the record anchor, key uniqueness at the grain, every chosen feature has values.

## 4. How the domain is not assumed

- A new customer always gets a **new pack**, under `SDG_PACKS_DIR`, outside the repo. Health and banking are never edited. The banking pack is the template: it is the pack with no health code paths, and the generated files must look like its files. Health-only modules (`x12`, `provider_features`, the 21 support tables) are not generated; the new pack lists `pattern_analysis` and `pattern_generation` with CSV only.
- Two domains at one customer (claims and members) are two record kinds in one pack, each with its own hierarchy. That is the primary/child shape the pack schema allows.
- Words come from the person. Accelerator code says "record kind", "level", "line"; `tests/test_neutrality.py` keeps counting.
- A neutral base catalog (`sdg/accelerator/base_catalog.yaml`: names, DOB, national ID, contacts, address, amounts, dates, codes) drives pii classes before any pack exists; the generated `catalog.yaml` adds the customer's columns as aliases.
- Settings that assume a person (sex, marital status, household, subscriber codes) are written empty and the readers must accept empty. Health `features.json` keys the generator cannot fill (provider joins, `scope_columns`) are left empty as banking leaves them; scope lives in `scope_json` only.

## 5. Data and production-source safety

- Every source read goes through one wrapper, `accelerator.source_read()`, that refuses any statement not starting with `SELECT` or `WITH`, and a test pins it. Sources never reach a writer, cleanup or DDL path.
- At connect, `HAS_PERMS_BY_NAME` reports a login that can write; the run continues only on a confirm.
- Reads are the existing light ones: `sys.*` catalog, `TABLESAMPLE` with a fixed seed, `SAMPLE_ROWS` 100,000, `MEASURE_SAMPLE_ROWS` 1,000,000, the whole-read cap, inclusion checks of 5,000 values with `LOOP JOIN`, query timeouts, the profile budget.
- No personal value in the plan, pack or logs: level labels are organization names, never a pii column; the profile already withholds personal values; `logsafe` filters driver errors; the work folder is limited to the signed-in account (the existing icacls path).
- Identifiers enter generated SQL only from the discovered model, through `packs.plain_name`; criteria values through the filters grammar.
- Store writes: `refuse_protected` before DDL; per-table transaction on load; `extract_log` stamp invalidates caches.

## 6. How we prove it works

- **Recovery, measured.** Run the proposal against the seeded `FACETS_QA` and the sample bank with no hint. Score: of the hand-defined walk levels (health members `GRGR_CK, CSPD_CAT, CSPI_ID, PDPD_ID`; claims the same plus `CLCL_CL_TYPE` narrowing; bank `BRANCH_ID`, `PRODUCT_CODE`) how many are in the top six candidates, and how many in the right order. Kept in a fixture like `neutrality-counts.json`; a run may only improve it.
- **Renamed source.** A copy of the demo source with every table and column renamed must give the same record kinds (after the person's confirm) and levels. This measures structure detection, since the bank has eight tables.
- **Pack round-trip.** The pack generated from the sample bank loads, every module that reads a pack imports in a fresh process, and pattern analysis finds the same number of patterns at the same sizes as the hand pack (`test_bank_e2e` style).
- **Extract parity.** The generated `BANK.sql` loads the same row counts as the hand one.
- **Safety.** Every statement sent to a source in a run is a SELECT; a Withheld column loads NULL under any script; an unclassified column loads NULL; build refuses a protected database; build refuses an unapproved plan; an edit drops approval.
- **Browser.** Source setup under the banking pack says no health word; each step is one click.

## 7. Phases, smallest first

1. **Propose (CLI, then screen).** `sdg accelerate landscape <source>` and `propose`; the Hierarchy and Tables tabs with pick, order, name, realign. The team gets a documented hierarchy and table list in a day. Recovery and renamed-source tests.
2. **Plan to files.** Criteria, Structure, Approval, and Build writing the pack folder and the DDL and extract SQL. Usable at once through today's runbook (`setup_tables.sql`, `extract.cmd`). Pack round-trip and extract parity tests.
3. **Build and load on screen.** Create or alter the store from the screen, Load now, Refresh, Scripts tab, the post-load checks.
4. **Change after approval.** Re-approve, rebuild with ADDs, archive the old patterns (kept, read-only, marked with their plan version), refuse analysis while the running pack differs from the approved plan.

## 8. Risks and open questions

- **One primary and one child per pack** (`schema.json`). Sibling top-level domains (policies and payments) do not fit. Is one pair enough for the first release?
- **Composite keys.** Inference finds single-column keys; FACETS reaches plan and product through a four-column key on the eligibility table. I propose a cheap extension (test a child's columns against a parent's declared PK columns by name) plus a stated join on the Tables tab. Where neither works, the person writes that join.
- **Restart on build.** The pack is read once at start (`lru_cache`, module constants in `pattern_scope.py`, `discovery_features.py`). Acceptable, or should the constants be made re-readable?
- **Day-1 depth.** A generated pack gives banking-level analysis. Provider joins and 837 files stay hand work. Is that the bar?
- **Who approves?** Second approver optional, as for layouts?
- **Server-side extract** puts `SDG_EXTRACT_KEY` and source reads on the UI process. Today only the command line reads a source. Phase 3 opens that; phase 2 does not.
- **Label values on screen** (group names) are customer business names; they are already shown on Patterns. Confirm that is fine for the proposal screens too.

## 9. Where this differs from `docs/source-accelerator-design.md`, and why

The document (revision 4) and this design agree on the spine: propose record kinds at low confidence, one hierarchy per record kind, walk and narrowing levels, figures counted once so realignment is instant, privacy classes per column enforced by the runner, layout-style approval, a new pack from a neutral template, scope in `scope_json`, a restart, archived patterns, the renamed-source test. Differences:

1. **No overlay path for an existing pack** (its §5.3, §5.7, §5.8, and the 545-column role registry). The product owner said FACETS is one customer's. A second health customer on QNXT would need a 1,700-line health `features.json` re-pointed through an overlay plus a registry nobody has written; and health's file writers would still want 21 support tables. A new pack is simpler, and the person can copy health's `code_names` by hand. This removes a whole class of code and tests.
2. **No typed expression grammar** (§5.9). The first release has no hand expressions: a derived column is a `derive` op in `features.json`; anything else is a hand edit to the pack's extract file, as it is today. The grammar can come when a customer needs it.
3. **No staging-and-promote load** (§5.10). At day 0 the denorm is empty, and the extract already replaces each table in one transaction and keeps old rows on failure. The backup, disk-space estimate and restore are a later refinement.
4. **Phases ordered for use at a customer sooner.** The document's phase C is the screen and D/E the build and load. This one puts "plan to files" second, so the team can run the generated pack through today's `setup_tables.sql` and `extract.cmd` before any server-side DDL or server-side extract exists.
5. **The plan lives in the store**, not in `work/accelerator/<run>/`. A second approver on the teammate's laptop cannot see a file under the user's VDI account; patterns and layouts are already shared through the store.
6. **Lighter refresh controls.** The application lock, stop-between-tables and drift check (§5.10) are good and cheap, but belong in phase 3, not the first deliverable. The in-process job registry already stops two runs from one UI.
7. **Knowledge files** (§5.4 item 7) are dropped. The FACETS knowledge would only serve the one customer that already has a hand pack; the recovery test on `FACETS_QA` measures the proposer without it.

What the document has that this design had missed and now adopts: `HAS_PERMS_BY_NAME` at connect, the fresh-process startup test of a generated pack, "every column any configuration key names must exist" as an approval gate, and making person-specific settings optional in the code.

### Critical files for implementation
- `sdg/packs/__init__.py` (pack loading, `check`, `held`, `SDG_PACKS_DIR`; the generated pack must pass it)
- `sdg/packs/banking/features.json` (the template every generated `features.json` must match in shape)
- `sdg/discovery/inference.py` (key inference the hierarchy proposal rests on; composite-key extension)
- `sdg/extract/run.py` (the `-- table:` runner, surrogates, `extract_log`; Load and Refresh reuse it)
- `sdg/analyzer/services/layout_service.py` (the approval pattern to copy for `sdm.source_plans`)
