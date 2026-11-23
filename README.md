# SDM — synthetic data generation tool

> Picking this up again? Read [STATUS.md](STATUS.md) first. It carries the
> current state; this file explains how the thing works.

A proof of concept for rule-based synthetic test data. It reads a client's data
model, profiles the data itself, matches columns to business data elements from
an organization catalog, and proposes a generation rule for every column with
the evidence behind it. What it cannot decide goes to a review queue.

SDM is one framework for any business. A **business pack** -- configuration
only, no code -- says what a business's records are called, what its patterns
are found on and which file formats it is offered. US health insurance (FACETS
members and claims) is the pack that ships; see [Business packs](#business-packs).
The examples below are the health pack's.

Built against the design document and build brief in `docs/`. SQL Server only,
Python 3.12 or later.

There are two ways in: a command line, and a web interface that drives the same
engine through the same files.

> **Setting up at a customer** (their SDM platform database, FACETS on its own
> server): follow `scripts/customer/README.md`, not the steps below.

## What it does

```
connect -> discover -> profile -> map -> review -> plan -> dryrun -> generate -> validate
```

The **plan** is the contract. Every module reads or writes it, it validates
against a JSON Schema, and it records for every column the resolved rule, its
parameters, the evidence, and which layer supplied it.

Three ideas do most of the work:

- **Rules live on business elements, not on columns.** An element (the health
  pack's Member ID, say) is defined once in the catalog with its format and PII
  class. Columns map to it and inherit
  the rule. Aliases match on tokens, so one entry covers `MEME_ID`, `MBR_ID`,
  and `MEMBER_NBR`.
- **The tool learns from the data, not just the schema.** It fits formats, finds
  undeclared foreign keys by value inclusion, finds functional dependencies and
  date orderings, and measures how many children each parent really has.
- **Nothing is guessed silently.** Every mapping and every proposed rule carries
  its evidence and the share of rows that supported it. High-confidence items
  are accepted automatically and can still be changed; the rest go to a queue.

## Business packs

One pack per installation: `SDG_PACK` in `.env` names it (`health` when it is
not set). Two are built in: `health` (US health insurance, FACETS) and
`banking` (a retail bank: customers, accounts, transactions; its sample bank
is built by `python -m seed.bank SDMT_BANK_SRC`). A pack is a folder,
`sdg/packs/<id>/`, or one under `SDG_PACKS_DIR`:

```
pack.json          its records and the role each plays, reference tables, the
                   plan's constants, the formats and modules it offers, its SQL
features.json      what its patterns are found on, counted and reported by
glossary.json      every word the screens use for its records
story.json         the words a story or a search is read in
column_names.json  business names for its columns
catalog.yaml       its element catalog
sql/               its tables in the SDM platform database, and their indexes
extract/           the SELECT behind each of those tables, per source
```

Code asks a record's **role** -- `primary` (health: members), `child` (claims)
or `parent_of_primary` (subscribers) -- and never its name, so the API's
neutral routes are by the pack's keys (`/api/entities/{key}/...`,
`/api/scope/{level}`); `/api/claims/*` and `/api/hierarchy/*` stay as aliases.
The screens read the pack once at start (`/api/pack`) and say its words.

A format only one business uses is a **module** the pack offers by name:
`x12`, `snip`, `adjudication`, `fhir`, `hl7v2`, `hl7v3`, `cda`, pattern
analysis (`pattern_analysis`: both packs; a pattern's scope is kept in
`sdm.patterns.scope_json`, in the pack's words) and writing files or plans
from a pattern (`pattern_generation`: both packs). A bank's patterns
are found, kept, reported and read from a story once its store is filled by
`extract/BANK.sql`. Where the active pack does not name one, its menus and tabs do not appear and its routes answer "... is not
offered by the active business pack". CSV and fixed width are every pack's.
Tests hold the health pack to what the code held before it moved
(`tests/test_pack_health.py`, `tests/test_pack_constants.py`), and
`tests/test_neutrality.py` keeps health words outside the pack from growing.

## Getting started

On a new machine, start with `DEPLOY.md`: it covers installing SQL Server,
the ODBC driver and Python first. With those in place:

```
scripts\setup.cmd          create the virtual environment and install
                           (then edit .env: server, and sign-in)
scripts\seed.cmd           create the databases and build the fixture
scripts\first-run.cmd      fill the store, run the pipeline once, seed the rules
scripts\ui.cmd             serve the interface at http://127.0.0.1:8777
scripts\verify.cmd         confirm all 17 planted patterns hold
scripts\test.cmd           run the test suite
```

`docker-compose.yml` starts SQL Server 2022 in a container if you would rather
not install it locally. Everything else is the same.

## The web interface

```
scripts\ui.cmd
```

Opens on http://127.0.0.1:8777. Every script here is a thin wrapper around
`python -m sdg.cli.main <command>`; the installed `sdg` alias does the same
thing wherever your machine's policy allows an installed console script to run. Node 18 or later is needed the first time, to
build it; after that the built files are served by the same Python process that
runs the engine.

The left menu is the way through, in the order the work happens. For files
from patterns -- the only way a customer machine works, where database loads
are off -- only two matter, and the screens open on the first:

- **Patterns** -- find a pattern in the pattern store (Targeted Analysis, Full
  Book Scan, Custom Pattern, Requirement Match) and save it.
- **Generation** -- write files for a saved pattern: flat files, and the
  formats the pack offers (health: X12, FHIR, HL7 v2, C-CDA, or a format
  modeled on a sample). Every file is kept as a
  generation request (the Requests tab) and can be written again, the same.

For a database worked on and loaded, the steps before them:

- **Read the database** — one action, and each stage reports what it found as it
  goes, including any foreign key it discovered that nobody declared.
- **Schema** — every column in one dense table, filterable, with a panel beside
  it showing how a column was measured, which element it matched, and the
  evidence for both. Every column here is editable, not only the ones the tool
  was unsure about.
- **Review** — the queue, as a decision per card: the question, the tool's
  answer, the alternatives, and the evidence. Approving writes to the same
  `review-decisions.json` the command line reads.
- **Plan** — what each table's mode is (write rows, reuse its keys, or leave it
  alone), the cascade, then every resolved rule. Type one root count and the
  row counts below it recompute through the measured ratios. Each edge carries
  its own distribution, with the childless share drawn in its own color because
  that is the number a flat ratio throws away. Each edge is editable too: keep
  the measured shape, or state a fixed number or a range instead.
- **Generate** — preview, write, check, remove, with the validation results in
  full.

Light and dark, keyboard focus, and no color used decoratively: hue says where
a rule came from, and fill says how confident the tool is.

Anything started in the browser can be finished at a prompt and the other way
round. Both read and write the same artifacts in `work/`: the rules you change,
what each table's mode is, and any children-per-parent shape you state all live
in `review-decisions.json`, so `sdg plan` reproduces what the Plan screen
showed without repeating any of it as flags. A flag typed at the prompt still
wins over the stored decision, because it is what that run asked for.

## The fixture

The health pack's demo fixture: `seed/build.py` creates a Facets-shaped
membership and claims slice with patterns planted on purpose, listed in [seed/PATTERNS.md](seed/PATTERNS.md).
Those patterns are the test fixtures for the whole proof of concept: an
undeclared foreign key, a long-tailed claims-per-member distribution, a
functional dependency from ZIP to city and state, date orderings, a computed
total, and NPIs with valid check digits.

Three scales: `--small` (1 percent, for fast test runs), `--medium` (25 percent,
the development default), and full.

## Layout

```
sdg/
  catalog/     element catalog: the model, the loader, dictionaries
  discovery/   connections, metadata from sys.* views, classifier, key inference
  profiling/   column measures, format fitting, check digits, dependencies, ratios
  mapping/     evidence matchers, confidence scoring, the mapping store
  plan/        schema, model, proposer, hierarchy, resolver, validator, reference sets,
               and what a person may change about a rule
  engine/      seed engine, generator library, expression language, execution
  writers/     CSV, SQL Server, post-run validation, manifest, cleanup
  review/      review queue and the static HTML page
  api/         the HTTP API behind the web interface
  cli/         the sdg command
  packs/       business packs: one per business, configuration only (health is built in)
ui/            React and TypeScript interface, built with Vite
seed/          fixture builder and its pattern verifier
examples/      the catalog's dictionaries, a hand-written sample plan
tests/         one file per build step, named for what it proves
docs/          the demo script
```

## Results on the fixture

Measured on the full-volume fixture: 20,000 subscribers, 46,044 members,
213,295 claims, 657,217 claim lines.

Generated at that scale, the population keeps the source's shape rather than
just its totals: claims per member covers every count from 0 to 40 with no gaps,
29.6 percent of members have none against the source's 29.6 percent, and the
median among those who have any is 5 in both.

| Measure | Target | Result |
|---|---|---|
| Auto-map rate at high confidence | 60 percent or more | 94 percent |
| Review queue | under 20 columns | 5 columns |
| Claims per member, share with none | within 2 points of 30 percent | 30 percent |
| Claims per member, median among those with claims | 5 | 5 |
| Post-run validation | passes | 56 of 56 checks |
| Profiling the claims table at full volume | inside a 60-second budget | 6.9 seconds, nothing skipped |
| Full demo, generating 943,203 rows | under 10 minutes | 83 seconds |

The demo generates, validates, and then removes what it wrote, so the fixture is
the same afterwards. `sdg demo --keep` leaves the rows in place.

The test suite is 417 tests across the ten build steps, the API, and editing,
and it is idempotent against the fixture: every test that writes to the database
removes its own rows.

## Things worth knowing

**Reproducibility.** Every value is a function of the named seed, the scenario,
the field, and the row index, hashed with BLAKE2b. The same plan and seed
produce byte-identical output on any machine, and the field order in the plan
does not affect the data.

**EDI from a sample.** Generation > Files > *EDI from a sample* writes an 834
or 837 that is a trading partner's sample file everywhere except the positions
a generation config names. A config uses the masking tool's segment
addressing under `genSegments` (`segment_id`, `qualifier`, `loop_segment_id`,
`loop_qualifier`, `position` counting the segment id as 1). Read a sample
against a master -- a masking config, another saved config, or none -- and
each position of the sample is proposed an SDM algorithm (from the master's
name, or from the position's place: `NM1*IL` position 4 is a last name). Accept
or change each, then *Save config as*: the master is never changed, and the
saved config keeps its sample (`sdm.sample_gen_configs`, with its `format`
and `kind`). An 834 repeats the
sample's member loops, one per new member; an 837 repeats its subscriber, claim
and line loops for the pattern's members, renumbering HL and LX, adding up
CLM02 and counting SE. The way of working is not X12's; it is three layers in
`sdg/writers/samples/` (the whole logic, every format: `docs/files-from-a-sample.md`):

* **one engine** (`engine.py`): units, records, rules, derived positions --
  no segment, resource or element of any format;
* **a reader per syntax** (`x12.py`, `hl7v2.py` -- both on `segments.py`,
  the config language, proposals and filling every segment syntax shares --
  `fhir.py` and `cda.py`):
  parse and write the file, address a place the way the masking tool's config
  for that syntax does (X12 `genSegments`; FHIR `genResources` with
  `path` + `where`, so edi-mask's `FHIRR4Config.json` is a master as it
  is; CDA `genPaths` with XPath-shaped paths, `//` and `valuePrefix`, so is
  `CDAR2Config.json`), cut the sample into units. No transaction or resource rule in it;
* **a profile per kind of file** (`profiles/x12-834.json`, `x12-837.json`,
  extending `x12.json`): what repeats (`units`, with `group`, `starts`,
  `stops`, `requires`), what the structure carries (`derive`: `sequence` for
  HL01/HL02, `index` for LX01, `sum` for CLM02), what a place holds
  (`places`), and how the sample is summed up. FHIR's (`fhir.json`,
  `fhir-members.json`, `fhir-claims.json`): a Patient and what follows it per
  member, a Claim/ExplanationOfBenefit per claim, its `item[*]` per line;
  `rename` gives every resource written a new id with every reference and
  fullUrl following, `index` numbers items, `sum` makes Claim.total, `drop`
  removes the sample's narrative; `codes` writes SEX as male/female and
  relationship as self/spouse/child. `masking-names.json` reads the masking
  tool's algorithm names for every format.

HL7 v2's (`hl7v2.json`, `hl7v2-adt.json`, `hl7v2-dft.json`,
`hl7v2-oru.json`): an ADT message per member; an ORU^R01 per claim with an
order (ORC, OBR, results, specimen) per line, each order its own numbers and
the line's date; a DFT^P03 message per claim with an FT1 per line (`while` keeps a
line's NTE/PR1 with it; the DG1/IN1 after the last FT1 are the claim's,
written once after its lines); `rename` gives each message its own MSH-10,
`index` numbers FT1-1, `drop` clears DG1-3.2 (the sample's diagnosis text);
`member.segments` (PID, IN1) keeps a doctor's name in PV1 from being read as
the member's. edi-mask's `HL7v251Config.json` is a master as it is; a field's
repetitions (`~`) are all generated, the first from the record. *Run HL7
checks*: segment ids and delimiters, the segments a message type requires
(`message.required`), required fields, BTS/FTS counts, unique MSH-10, HL7
dates, and the HL7 tables in `checks.codes` (0001, 0063, 0103).

CDA's (`cda.json`, `cda-document.json`, `cda-ccd.json`): a member gets a
document per day of service on each claim (encounter) -- the document unit
groups by `["CLAIM_ID", "SERVICE_DATE"]`, so an office visit gets one and a
five-day stay five, as the claim's lines say; a claim without dates one,
member records one per member; no count is ever configured; inside each, the
procedures section has an entry per claim line of the day (its code as CPT
or HCPCS, its date, a new id) and its table a row per entry said from it
(`99213 (CPT)`, `05 Mar 2026 10:30am`) -- every one the sample's document under
the same config, its id and set id renamed, everything else (stylesheet,
comments, namespaces, clinical content) the sample's. The `member` unit only
groups documents; the profile's `levels` says which places are the member's
(everything under recordTarget), filled from the member's record and row so
all of a member's documents name the same person even where a rule makes the
value; the document's dates (effectiveTime, author/authenticator times,
serviceEvent, encompassingEncounter) are its claim's service date. Places are
paths from the root (`recordTarget/patientRole/patient/name/family`); an id's
`root` tells the record number from the SSN (`qualified`), a telecom's
scheme (`tel:`, `mailto:`) stays on it, a timestamp keeps the sample's
precision, time and zone, and a code's displayName follows it.
`member.under`/`except` keeps the guardian's, the authors' and the
performers' names from being read as the member's. The download is a zip of
documents (`CCD-00001.xml`, ...); the preview and the checks (*Run CDA
checks*: well-formed, required CDA header, unique document ids, narrative
references, the profile's value sets -- not a schema or Schematron
validation) read them unpacked. XML is read with lxml.

A FHIR sample is a Bundle (written back as that Bundle, its entries repeated)
or one resource (written as a collection Bundle). FHIR files are checked
(*Run FHIR checks*, `/api/samples/check`): structure against the
fhir.resources R4B models, references resolving and ids unique, and the
value sets the profile lists (`checks.codes`). So a new X12 transaction
(835, 270), FHIR kind, HL7 message type or CDA document type is a profile. Profiles are read again when edited. The API
(`/api/samples/...`), the table and the screen (Generation > Files > *From a
sample*) read the format off the sample without knowing it. Tests
`tests/test_sample_x12.py`, `tests/test_sample_fhir.py` (samples built from
edi-mask's FHIR examples in `tests/fixtures/fhir`), `tests/test_sample_hl7v2.py`
(edi-mask's HL7 samples, `tests/fixtures/hl7`; `.hl7` files are kept byte for
byte by `.gitattributes`, their segments end with a bare CR),
`tests/test_sample_cda.py` (edi-mask's CCD and CDA config, `tests/fixtures/cda`)
and browser tests 5b, 5c, 5d.

**Identity keys.** When a child in the same run references an identity column,
the run generates the key itself and loads with `IDENTITY_INSERT`, starting
after the highest value already in the table. Reading keys back after each
insert would work too, but it would make the output depend on what the database
happened to assign, and a run has to be reproducible from its seed.

**The catalog does not win everything.** It governs what a value *is*: its
format, its allowed set, where it is drawn from. It cannot express what a value
*depends on*, because a dependency belongs to the schema rather than to the
element. So a learned rule such as "the service date falls inside the member's
coverage" beats the catalog's generic default, and the plan records the
override.

**A rule is checked against the column it writes to.** A pattern wider than its
column does not fail when it is saved, when the plan is built, or in a dry run
of six rows. It fails as a truncation error from the driver after tens of
thousands of rows are already committed. So the editor measures the longest
value a rule can produce and refuses one that cannot fit.

**A load that stops partway still leaves a manifest.** Each table is committed
on its own, so a failure midway leaves the tables before it in the database.
The writer carries the report of what it committed out with the error, the
manifest is written for those tables, and `sdg cleanup` can still remove them.

**Cleanup is exact but not total.** It removes the rows a run wrote, by key,
using the manifest. What a trigger wrote into some other table stays: those rows
were written by the database, not by the run, and the run reports that it
happened rather than deleting them quietly.

**Nothing writes to the source.** Discovery and profiling are read-only. Only
the writer touches the target, and only tables the plan marks `generate`.

**Everything the tool decided can be changed.** A high-confidence rule is still
a proposal, so the editor is the same on every column: the element, the
generator, and that generator's parameters. The form is built from the
generators' own descriptions in `sdg/plan/editing.py`, which is also what
validates an edit before it is stored — a pattern that will not compile or
weights that do not line up with their values are refused with a sentence, next
to the field that caused it, rather than failing later in a run.

Overriding does not discard anything. The tool's proposal is kept beside the
override, the panel says which of the two is in force, and one button puts the
proposal back. Rules the schema forced — an identity key, a composite counter —
are editable too, but they are marked, because overriding one usually produces
data the post-run checks reject.

**An unaccepted match is shown as one.** Where the tool found a nearest element
but did not have the evidence to accept it, the schema table shows it grayed
with the reason on hover, and the editor leaves the element field empty rather
than offering the guess as a filled-in answer.

**Children per parent can be stated instead of measured.** The measured shape is
a histogram with a long tail, which is the honest answer and not something
anyone should retype, so the Plan screen does not offer to edit it bin by bin.
It offers to replace it: a fixed number, or a range, each with a childless
share. The row counts below recompute through the new mean, the edge stops
drawing a histogram it no longer describes, and the plan records the shape as
`provenance: user` with a sentence saying what was set.

The childless share is offered because a flat ratio without one is what makes
generated data look unlike the real thing: 5 claims per member with 30 percent
having none is a population, and 3.5 claims for everybody is not. It is drawn
before the count and lowers the mean, so effective counts stay right. A
histogram counts its own childless parents in the zero bin, so the share is
descriptive there and is not subtracted twice.

## Deviations from the build brief

Three, all deliberate, all visible in the code:

1. **SQL Server runs locally** rather than in Docker, because that is what was
   available. `docker-compose.yml` is still there for the container path.
2. **Python 3.13** rather than 3.12, for the same reason. Nothing depends on
   the difference.
3. **Claim volumes follow the planted distribution rather than the stated two
   million.** The brief asks for both 2,000,000 claims across 46,000 members and
   a claims-per-member distribution with a median of 5 and a maximum near 40.
   Those cannot both hold: a maximum of 40 caps the table at 1.84 million, and a
   median of 5 puts the real total near 215,000. The distribution is the tested
   fixture, so the seed honors it. This is written up in
   [seed/PATTERNS.md](seed/PATTERNS.md).

One reading was clarified rather than changed: the brief's "median 5" describes
members that have at least one claim, since the same sentence says 30 percent
have none. The profiler reports both figures and the tests name which one they
mean.
