# Synthetic files from a sample: the logic

How SDM writes a synthetic X12, FHIR, HL7 v2 or CDA file from a sample file
and a config. Code: `sdg/writers/samples/`. Tests: `tests/test_sample_*.py`,
browser tests 5b–5d.

## 1. The idea

**A synthetic file is the sample file, except the places a config names.**

* The **sample** is a real-shaped file of the format (an 837, a FHIR Bundle,
  an ADT message, a CCD). Everything in it that no config entry names is
  copied as it is: envelopes, codes, providers, comments, namespaces,
  clinical content.
* The **config** lists places in the sample (a path) and the algorithm that
  fills each one (`LAST_NAME`, `DOB`, `SSN`, ...). The masking tool's
  config for the same format (edi-mask `configs/`) is read as the *master*: a
  generation config starts from it, and its paths are written the same way.
* The **records** (members, or claim lines, from a pattern or the plan's run)
  say *how many* of everything are written: members, claims, lines,
  messages, documents. A count never comes from the config.

The same way of working serves every format. The user's requirement: *the
framework is the same across EDI, HL7, FHIR and CDA; only the config files
differ.*

## 2. Three layers

| Layer | File(s) | Knows | Never knows |
|---|---|---|---|
| **Engine** | `engine.py` | units, records, rules, derived places | any segment, resource or element |
| **Readers** (syntax) | `segments.py` (X12 + HL7 v2), `x12.py`, `hl7v2.py`, `fhir.py`, `cda.py` | how a file is parsed and written, how a place is addressed | which transaction, resource or document type it is |
| **Profiles** (config) | `profiles/*.json` | what repeats, what follows from what, what a place holds | how to parse anything |

A profile may `extend` another (`x12-837.json` extends `x12.json`), and its
own keys replace the base's. Profiles are re-read when edited, with nothing
to restart. **Adding a kind of file (an 835, an ORU, a discharge summary) is
adding a profile; a reader is added only for a new syntax.**

Each format module provides the same interface (`__init__.py`): `detect`,
`read_sample`, `facts`, `subject`, `read_config`, `write_config`,
`positions`, `used`, `propose`, `stream`, and optionally `check` (FHIR, CDA)
and `package` (CDA). The API (`/api/samples/...`), the table
`sdm.sample_gen_configs` and the screen (Generation > Files > *From a
sample*) read the format off the sample and never look inside it.

## 3. The flow

1. **Read**: the user picks a sample and a master (a masking config, a saved
   generation config, or none). The format is detected from the sample's
   content and its kind from the profile whose `match` fits best.
2. **Propose**: every value-holding place in the sample is listed with an
   example, a count, the master's algorithm name for it, and a proposed SDM
   algorithm (section 4).
3. **Save as**: the user accepts or changes each place and saves a new
   generation config. The master is never changed.
4. **Write**: for a saved config and a set of records, the engine writes the
   file (section 5). It can be previewed, downloaded and checked.

## 4. How an algorithm is proposed for a place

For each place, in order (`engine.decide`):

1. The master names no algorithm for it → **copied from the sample**.
2. The master's name is itself an SDM algorithm → that one.
3. The profile's `places` list says what this place holds (`PID-5.1` is
   `LAST_NAME`; `recordTarget/patientRole/patient/birthTime/@value` is `DOB`)
   → that.
4. Otherwise the masking name is read (`masking-names.json`: `lastName` →
   `LAST_NAME`), **but only inside the member's own blocks** (`member` in the
   profile: X12/HL7 segments, FHIR resource types, CDA paths). A doctor's
   name that the masking config calls `lastName` is not the member's last
   name, so it is left for the user to choose.

Places that the structure owns (identities, sequence numbers, totals) are
never proposed; the profile's `derive` rules set them (section 5.4).
Structural fields (code systems, template ids, the narrative) are listed as
`structural` in the profile and never proposed.

## 5. How a file is written

### 5.1 Units: what repeats

A profile's `units` describe what repeats, nested:

```
837:   member (group MEMBER_ID) > claim (group CLAIM_ID) > line
FHIR:  member (Patient …) > claim (Claim|EOB) > line (item[*] inside the claim)
DFT:   claim (MSH … ) > line (FT1, with its NTE/PR1)
CDA:   member (group MEMBER_ID) > document (group CLAIM_ID + SERVICE_DATE)
         > line (an entry in the procedures section, per claim line)
```

Unit keys:

| Key | Meaning |
|---|---|
| `starts` / `stops` | where an occurrence begins / where the unit ends in the sample |
| `group` | the records a unit takes: consecutive records sharing that element (`MEMBER_ID`), or a list (`["CLAIM_ID", "SERVICE_DATE"]`: each claim's records split by day) |
| `row` | the seed row a rule draws from: `count` (per unit written) or `nested` (parent's row × 1000 + place among siblings) |
| `requires` | an occurrence without this child unit is not modeled on |
| `in` | the unit repeats inside its parent (FHIR `item[*]`; CDA `…/section/entry`, with `where` testing the element holding them: the procedures section's `code/@code` = 47519-4) |
| `while` | segments that stay with the unit (HL7 NTE/PR1 after an FT1) |
| none of `starts` | the unit only groups its children (CDA's member holds documents) |

**How many are written is always the records'.** Each unit written is a
copy of one of the sample's occurrences of it, taken in turn (the sample's
second claim models the second claim written, the first models the third
when the sample has two, and so on).

### 5.2 Values: what fills a place

A place takes, in order:

1. the record's own value for the element (from the pattern or the run);
2. otherwise the element's rule (the algorithm), drawn at the unit's row
   with the file's seed. The same request writes the same file.

Rules of thumb that hold in every format:

* **A record holds one value per element.** The first place filled with an
  element takes the record's value; further places with the same element in
  the same unit (a second given name, a second identifier repetition) take
  the rule's.
* **Levels.** A place can belong to an enclosing unit. CDA's profile marks
  everything under `recordTarget` as the member's (`levels`), so it is filled
  from the member's record and row: all of a member's documents name the same
  person, even where a rule makes the value.
* **Written the way the sample writes it** (`engine.fit` plus the reader):
  dates in the sample's format (`20260301`, `2026-03-01`, an HL7 v3
  timestamp keeps its time of day and zone: `20260301103026-0500`), amounts
  with the sample's decimals, upper case where the sample is upper case, a
  number stays a JSON number, a URI keeps its scheme (`tel:`, `mailto:`).
* **Codes.** Where the format codes a value differently from the store, the
  profile's `codes` map it (`SEX` F → FHIR `female`, CDA `F` with displayName
  `Female`; `RELATIONSHIP_CODE` S → HL7 `SPO`, FHIR `spouse`), or say which
  code system a code is written in (`systems`: a procedure code is HCPCS when
  it starts with a letter, CPT otherwise). A description of the sample's code
  (FHIR `display`; CDA `displayName`, `translation`s and `originalText`; HL7
  DG1-3.2) is dropped or rewritten when the code changes. A FHIR HumanName's or
  Address's `text` is said again from the parts written (profile `texts`:
  `{given} {family}`, `{line}, {city}, {state} {postalCode}`), where the
  sample has one.
* **Addresses** are generated whole: street, city, state and ZIP come from
  one address, never a new ZIP on the sample's street.

### 5.3 Qualified places

One path can hold different things, told apart by a neighbor. These become
separate places, each with its own algorithm:

* X12: `NM1*IL NM103` (an NM1 whose NM101 is IL), `NM108=34` → SSN.
* FHIR: `telecom[*].value` where `system` is `phone` or `email` (`where`).
* CDA: `id/@extension` by its `root` (record number vs SSN); a telecom by
  its scheme (`valuePrefix`); any place inside a section by the section's
  LOINC code (`within`: `section=47519-4`), so a date among the procedures
  and the same path among the results take different algorithms.

### 5.4 Derived places (`derive`)

Set by the engine after a unit is filled; never chosen by anyone:

| Op | Does | Used for |
|---|---|---|
| `sequence` | numbers an item across the file, re-pointing parent references | X12 HL01/HL02 |
| `index` | an item's place among its siblings | LX01, FT1-1, FHIR `item.sequence` |
| `sum` | a unit's total over its children | CLM02, FHIR `Claim.total` |
| `rename` | a new identity per copy (`to`: `{id}-{n}`, or `{uuid}`); every reference to it follows | FHIR resource ids + fullUrls, HL7 MSH-10, CDA document id and setId, CDA procedure entry ids |
| `set` | a fixed value where the place (or the element it is on, `on`) is; CDA adds the attribute if missing | CDA observation `value/@nullFlavor` = UNK |
| `drop` | removes a place | FHIR narrative, HL7 DG1-3.2, CDA methodCode/targetSiteCode of a procedure entry |

A rule with `unit` applies to that unit's items only (a CDA procedure
entry's id, not the ids at the same path in the document's other sections).

### 5.5 Envelopes and counts

The reader writes the format's envelope: X12 ISA/GS/ST with SE01 counting
segments and GE/IEA closing; HL7 batch FHS/BHS with BTS-1 counting messages;
the FHIR Bundle around its entries; the CDA prolog (XML declaration,
stylesheet) on every document.

## 6. Per format

**X12** (`x12.py`, profiles `x12-834`, `x12-837`). Master: edi-mask
`EDI837Config.json` / `EDI835Config.json`-style segment configs. An 834
repeats the member loop per member; an 837 repeats subscriber > claim >
line, renumbers HL and LX, adds up CLM02. Checked by the SNIP checks.

**FHIR R4** (`fhir.py`, profiles `fhir-members`, `fhir-claims`). Master:
`FHIRR4Config.json` (`resourceType` + `path` with `[*]` + `where`). A
Patient and what follows it per member; a Claim/ExplanationOfBenefit per
claim with `item[*]` per line. Every resource gets a new id, and
references (`Patient/123`, `urn:uuid:` fullUrls) follow. Checked against
the fhir.resources R4B models, plus references and the profile's value sets.

**HL7 v2** (`hl7v2.py`, profiles `hl7v2-adt`, `hl7v2-dft`, `hl7v2-oru`). Master:
`HL7v251Config.json`. Fields are numbered as HL7 numbers them (MSH counts
its field separator; labels `PID-5.1`). ADT: a message per member. DFT^P03:
a message per claim, an FT1 per line. ORU^R01: a message per claim (lab work
is billed as claim lines), an order (ORC, OBR and the results, notes and
specimen after it) per line, OBR-1 counting the orders, OBR-4 the line's
procedure where the sample has a test code, OBR-7/OBX-14 its date, and each
order its own placer/filler numbers (ORC-2/3, OBR-2/3); result values
(OBX-5) are the sample's, since a claim has none. Each message gets its own
MSH-10.
Repetitions (`~`) are filled, the first from the record. Checked (*Run HL7
checks*) for structure (segment ids, the first message's delimiters in every
MSH, the segments the message type requires -- the kind profile's
`message.required` -- and the fields `checks.fields` requires, BTS-1/FTS-1
counts), unique MSH-10 control ids, `checks.dates` written as HL7 dates
(DTM, a real day), and `checks.codes` in their HL7 tables (0001 sex, 0063
relationship, 0103 processing id). Not a conformance test against a message
profile.

**CDA R2** (`cda.py`, profiles `cda`, `cda-document`, `cda-ccd`). Master:
`CDAR2Config.json` (XPath-shaped paths from the document root, `//` for any
depth, `@attr`, `valuePrefix`). Section 7.

## 7. CDA in detail

* **What a document is.** One `ClinicalDocument` about one patient,
  identified by its own `ClinicalDocument/id`. Every document written is the
  sample's document, filled by the same config; its id and setId are renamed
  per copy (`{id}-{n}`), so every document is unique.
* **How many documents.** Since the data is synthetic, the count must
  follow from the claim. It is never configured and never taken from an
  invented field. **One document per day of service on a claim**: the
  document unit groups a member's claim lines by `CLAIM_ID` and
  `SERVICE_DATE`. An office visit (one date) gets one document; a five-day
  stay with lines on five dates gets five. Lines out of date order still
  give one document per date. A claim whose lines carry no date gets one
  document, and member records (no claims) get one per member.
* **Same person in every document.** Places under `recordTarget` (ids, SSN,
  name, address, sex, birth date) are the member's level and are filled from
  the member's record and row.
* **The document's own places** come from that day's record: the document
  `effectiveTime`, author/authenticator times and `serviceEvent` /
  `encompassingEncounter` times all take that day's `SERVICE_DATE` at the
  sample's time of day and zone.
* **Procedures come from the claim.** The procedures section (LOINC 47519-4)
  has one entry per claim line of the document's day (the `line` unit, `in`
  the section's entries). Each entry is one of the sample's procedures-section
  entries, taken in turn (procedure, observation, act, procedure, …), with:
  the line's `PROCEDURE_CODE`, written as CPT (`2.16.840.1.113883.6.12`) or,
  starting with a letter, HCPCS (`2.16.840.1.113883.6.285`); the sample's
  displayName, translations (SNOMED, ICD-10-PCS, ICD-9) and original text
  removed, since they describe the sample's procedure; the line's date at the
  sample's time of day and zone; a new UUID for its id. What describes the
  sample's procedure is not carried over: its `methodCode` and
  `targetSiteCode` are dropped, and an observation's result `value` (which
  C-CDA requires) becomes `nullFlavor="UNK"`. The performer, status and
  template are the sample's. Every other section (allergies,
  results, vital signs, …) is the sample's, its dates included.
* **The procedures table follows the entries.** A section's narrative (the
  human-readable table) has a row per entry written: the row the sample's
  entry points at (`text/reference`), taken out with it and written again
  for each entry. The row's ids are numbered for the entry (`Procedure3`,
  `ProcedureDesc3`) and the entry's reference points at its own row. The
  line unit's `narrative.cells` say what each cell shows, from the entry
  (`{*/code/@code} ({*/code/@codeSystemName})` → `99213 (CPT)`;
  `{*/effectiveTime}` → `05 Mar 2026 10:30am`, an interval as low - high);
  `null` keeps the sample's cell (Status). Claims carry no procedure
  description, so the code is what the row names.
* **Whose names are generated.** Only the patient's blocks are proposed as
  the member's (`member.under` = `recordTarget/patientRole`, except the
  provider organization, guardian and birthplace). Authors, performers, the
  custodian and the guardian keep the sample's values unless the user
  chooses otherwise.
* **Kept as the sample has it.** The stylesheet, comments, namespaces and
  prefixes (`sdtc:`), template ids, and the narrative and entries of every
  section but procedures.
* **Output.** The download is a zip with one file per document
  (`CCD-00001.xml`, …), the way CDA documents travel. The preview and the
  checks read the documents unpacked, one after another.
* **Checks** (*Run CDA checks*): each document is well-formed and has the
  header CDA R2 requires; document ids are unique; narrative references
  resolve; coded values are in the profile's value sets. This is not a
  schema (CDA.xsd) or Schematron (C-CDA) validation.

## 8. Adding to it

* **A new kind in an existing syntax** (an 835, an ORU message, a discharge
  summary): a profile file with `kind`, `match`, `subject`, `units`, `derive`
  and `places`. No code change.
* **A new syntax**: a reader with the interface in section 2. Behavior
  (repetition, numbering, totals) stays in the engine and profiles; a new
  derive op goes in the engine, never in a reader. The one-engine test
  (`test_x12_fhir_hl7_and_cda_run_on_one_engine_and_differ_by_profile`)
  guards that.

## 9. Known gaps

* CDA: the claim's diagnoses are not written (the sample's problems section
  is empty, "no information"; a sample with a problem entry would take them
  the same way as procedures); the procedures table names a code, not what
  it is (claims carry no description); sections other than procedures keep
  the sample's dates; the
  patient's phone and email have no SDM algorithm.
* HL7 v2: no ORM/SIU profiles; edi-mask's DFT sample has empty FT1
  procedure and amount, and its ORU sample an empty OBR-4 test code, so only
  dates are generated there; ORU result values are the sample's; an order
  without an ORC (ORC is optional in ORU^R01) is not a unit of its own.
* FHIR: ExplanationOfBenefit totals are not summed (the sample has no
  ExplanationOfBenefit); an address text keeps any part no rule generates
  (the sample's `district`).
* Not yet run on the client's own FHIR, HL7 or CDA samples.
