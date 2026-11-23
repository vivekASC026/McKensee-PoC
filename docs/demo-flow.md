# Demo flow: from a pattern to FHIR, HL7 v2 and HL7 v3 files

What the customer sees: SDM finds a pattern in the real book of business, then writes test files that follow that pattern in FHIR, HL7 v2 and HL7 v3, with no database set up first. Every file passes its format's checks. Optional and repeating elements, the "dynamic" part of FHIR and HL7 v3, are shown on the screen.

It takes about 25 minutes with talking. Checked end to end on 2026-10-23 through every route into a pattern; every file passed its checks.

## Before the customer arrives (15 minutes)

1. Start the tool with `scripts\ui.cmd` and open it in the browser. If it was already open, press Ctrl+F5.
2. Warm up the analysis. `ui.cmd` clears its cache when it starts, and the first run of each analysis is slow.
   - **Patterns › Full Book Scan**, Members, **Scan the Book** (seconds).
   - Switch to **Claims** and **Scan the Book** at depth Groups (about 2 minutes).
   - **Requirement Match**: run the claims requirement from step 7 once.
   - Leave the tool running.
3. Open these in File Explorer:
   - `Desktop\EDI\edi-mask\samples` holds the customer-style samples: `hl7`, `fhir`, `cda` and `hl7v3` (HL7 v3 messages downloaded from the Open eHealth IPF project).
   - `Desktop\EDI\edi-mask\samples\sdm-generated` holds files written in the last test run, for both patterns, in case something is slow.

## Part 1: find a pattern (6 minutes)

Rail: **Patterns**. Members or Claims is chosen top left. There are four ways in, and each ends with the same card and the same buttons. Three are shown here; Requirement Match starts Part 3.

**1. Targeted Analysis.** With Members selected, click **Find Patterns**.

> SDM groups the members by age, tenure, gender, plan, product and PCP, and names each group by what sets it apart. Nobody wrote these rules: the data did.

Open the first card. Show the age and tenure charts, then **Counts and %** and **Show the source records** (the real records behind it).

**2. Full Book Scan.** Click **Scan the Book**.

> Every combination of group, product category, plan and product, ranked by its share of the book.

**3. Custom Pattern.** This one goes into the files.
- Pattern name: `Women 30 to 45`.
- **Add a feature** › Gender, tick **F**.
- **Add a feature** › Age, from `30` to `45`.
- Click **Measure it**: 6,087 members meet these rules.
- Click **Save pattern**.

If the same rules were saved before, the card says *Already saved, as "…"*. That is by design: the same rules make the same pattern, and it is listed under its first name.

## Part 2: files for the members pattern (7 minutes)

On the **Women 30 to 45** card, click **Generate a file**. Later the same button is on its row under **Saved Patterns**.

Generation › Files opens with **Written for** set to the pattern. Set **New members** to `100`.

> A member file is new people carrying this pattern's values. Anything the pattern does not name keeps its rule.

**4. FHIR tab.** It opens on the standard **FHIR R4 members (Patient, Coverage)** layout. Every place is listed with its sample value and the algorithm that fills it. Patient gender says **SEX: the standard layout generates it**.

Then the "dynamic" part, under **Optional and repeating elements**:
- Add `Patient telecom[*]`: in `70`% of documents, occurs `1` to `3`.
- Add `Patient address[*]`: in `80`% of documents.

> Some patients have no phone, some have three; one in five has no address. Each file is reproducible: the same config gives the same file.

Then:
1. Type a name in **New config name** and click **Save config as**.
2. Click **Write the file**, then **Run FHIR checks**: "No errors".
3. Click **Show first 100 lines**.

**5. HL7 v2 tab.** It opens on the standard **HL7 v2.5.1 ADT^A01 (admission)** layout.
1. **Save config as**, then **Write the file**.
2. **Run HL7 v2 checks**: "100 messages; no errors".
3. **Show first 100 lines**: PID-8 is F and PID-7 is a birth date 30 to 45 years back.

**6. HL7 v3 tab.** Set **Master layout** to **HL7 v3 message: patient registry add (PRPA_IN201301UV02)**. This is the IHE PIX v3 patient identity feed.
- Under **Optional and repeating elements**, start typing `telecom`, pick `…/patientPerson/telecom` and set `70`% and `1` to `4`.
- **Save config as**, then **Write the file**: a zip of `PRPA_IN201301UV02-00001.xml` and so on, one message per member.
- **Run HL7 v3 checks**: "100 messages; no errors".

> One pattern, three standards, the same people: every patient in all three files is a woman aged 30 to 45.

## Part 3: a claims pattern from a sentence, and its files (7 minutes)

**7. Requirement Match.** Go back to **Patterns** and open **Requirement Match**.
- Requirement: `claims for type 2 diabetes from female members over 40`.
- Click **Find Patterns**.

> SDM read the sentence as claims, and as three rules: diagnosis Type 2 diabetes, gender F, member age 40 or more. The patterns appear without pressing anything else.

The first pattern is "Professional · Adult · female · Type 2 diabetes without complications": 1,870 claims, 5,322 lines.

Open the first card and click **Generate a file**.

> A claim file is claims for the members this pattern actually holds: 1,281 women, every claim with the type 2 diabetes diagnosis.

**8. FHIR tab.** Set **Master layout** to **FHIR R4 claims (Claim, Encounter)**. **Save config as**, **Write the file** (a few seconds), then **Run FHIR checks**. Claim.diagnosis is E119000 (E11.9, type 2 diabetes, as the source stores it) and Patient.gender is female.

**9. HL7 v2 tab.** Set **Master layout** to **DFT^P03 (charges)**.
1. **Save config as**, **Write the file**, **Run HL7 v2 checks**.
2. **Show first 100 lines**: one message per claim, an FT1 per claim line with its procedure, units and charge, and DG1-3 the diagnosis.
3. **ORU^R01 (lab results)** works the same way: an order per claim line.

**10. HL7 v3 tab.** Use **C-CDA R2.1 CCD**. **Save config as**, **Write the file** (about 20 seconds for 1,281 members), then **Run HL7 v3 checks**. Each document's Problems section holds the claim's diagnosis (ICD-10-CM), and its Procedures section an entry per claim line.

## Part 4: their own samples (3 minutes, if asked)

On any tab, click **Sample file** (the button reads "… (standard) · use my own…") and pick a file from `edi-mask\samples`.

| Tab | Sample | Result |
| --- | --- | --- |
| HL7 v3 | `hl7v3\PIXv3_FEED_REV_Maximal_Request.xml` | Patient registry revise (PRPA_IN201302UV02), read with the add's mapping: 15 places generated. |
| HL7 v3 | `hl7v3\PDQv3.xml` | Refused with a clear reason: a demographics query, not a member record. |
| HL7 v2 | `hl7\adt_a01.hl7` | ADT, 27 places generated. |
| FHIR | `fhir\patient-example.json` | Patient, 17 places generated. |
| HL7 v3 | `cda\ccd-example.xml` | CCD, 44 places generated. |

**Custom Format** reads any of these with the customer's own masking config. A place their config does not name is copied from the sample. **Accept N suggestions** fills in what SDM knows (sex, state, diagnosis).

## Questions to expect

**"FHIR and HL7 v3 are dynamic. Can it do that?"** Yes, on the FHIR and HL7 v3 tabs: any element can be left out of a share of the documents, or repeated from min to max times. The copies get values of their own, and the choice is saved with the config.

**"And HL7 v2?"** The structure repeats as the records say: an FT1 per claim line, an order per line, a message per claim or member. Making a single segment optional or repeated (NK1, OBX, PID-13) is not on the HL7 v2 tab yet.

**"Are the files valid?"** Each tab checks the file as written. FHIR checks resource models, references and value sets. HL7 v2 checks required segments and fields, dates and HL7 tables. HL7 v3 checks required elements, unique ids and gender codes. The checks are not a full schema, Schematron or IHE conformance validation. They read the first 200 records, so a large file is checked at once.

**"The pattern says over 40, but a file has a 39-year-old."** Claims files use the members' real birth dates, and ages are as of 2026-06-30. "Over 40" is the age written on each claim when it was made. In this data that age counts calendar years only, so a member whose birthday had not come yet shows as a year older.

**"Which HL7 v3 messages?"** HL7 v3 has documents (C-CDA) and messages. SDM writes CCD documents and the patient registry add and revise messages (IHE PIX v3 feed). Queries, query responses, acknowledgements and merges are refused with a reason: they are not member records.

**"How big can it go?"** A members file of 100 is instant. Claims for about 1,300 members take seconds (C-CDA about 20 seconds). A 20,000-member claims pattern written as C-CDA takes over 5 minutes, so don't use one live.

## If something goes wrong

- **A tab shows an old screen:** press Ctrl+F5.
- **The first Full Book Scan or Requirement Match is slow:** say "the first run reads the whole book; after that it is cached". This is why the warm-up step is there.
- **Writing a file is slow:** open the matching file in `samples\sdm-generated` instead.
