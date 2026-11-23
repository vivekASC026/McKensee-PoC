# Ten-minute walkthrough

For a customer visit. Timings are generous; the whole sequence runs in well
under a minute on a laptop, so the time is for talking, not waiting.

There are two versions of this walk. The **web interface** version is the one to
use in front of a customer, because the cascade and the review queue do the
arguing for you. The **command line** version below it shows the same thing to
an engineer who wants to see the artifacts.

Before they arrive:

```
scripts\seed.cmd --medium      rebuild the fixture
scripts\verify.cmd             confirm all 17 planted patterns hold
```

Leave a terminal open in the project directory and a browser ready.

---

# In the browser

```
scripts\ui.cmd
```

## 1. Read the database (1 minute)

Start on **Read the database**. Read the line under the headline out loud:

> Nothing is written to the source.

Press **Read the database**. The four stages report as they finish. Stop on the
first one:

> It found a foreign key nobody declared. `CMC_CLCL_CLAIM.MEME_CK` has no
> constraint on it, but every value exists in the member table. That is the
> Facets pattern, and it was found by checking the data rather than by reading
> the DDL.

## 2. Schema (2 minutes)

Click **Schema**. Forty-two columns, all on one page.

> Color here means one thing: where a rule came from. Blue is forced by the
> schema, violet is governed by the catalog, green was measured from the data.
> Whether the chip is filled or outlined tells you how sure the tool is. Two
> facts, two channels, nothing decorative.

Click `PRPR_NPI`.

> Look at the panel. It measured the column, fitted the format, and then noticed
> that every value passes the NPI check-digit test. That is why it is confident
> this is a National Provider Identifier rather than a phone number. And at the
> bottom, in amber, it admits what it cannot do: a pattern reproduces the shape
> but not the check digit.

## 3. Review (2 minutes)

Click **Review**.

> Five columns it will not decide alone, out of forty-two. Each one is a question
> with an answer already attached.

Take `CDML_SVC_CD`.

> The catalog has no Facets alias for a procedure code, so the name tells it
> nothing. But the format matches `PROCEDURE_CODE` exactly, so it offers that and
> asks. Press **Show the evidence** and you get the reason.

Press **Accept**. The card moves to Settled and the counter ticks.

> That decision just went into the same file the command line reads. Either way
> in, one set of artifacts.

## 4. Plan (2 minutes)

Click **Plan**. This is the screen to linger on.

> One number at the top. Everything below it is the measured number of children
> each parent really has.

Change 5,000 to 20,000 and press enter. Watch the counts recompute.

> Now look at the distribution on the middle edge. The magenta bar is the thirty
> percent of members with no claims at all. A flat ratio of five claims per
> member throws that bar away, and then no test ever exercises a member with
> none, or the member with forty.

Scroll down to the provenance bar.

> Every rule, and which layer decided it. And in the Beat column: the service
> date beat the catalog, because the catalog can say what a service date looks
> like but not that it falls inside the member's coverage. That is a property of
> this schema, not of the element.

## 5. Generate (1 minute)

Click **Generate**, then **Show sample rows**, then **Generate**, then
**Validate**.

> Two hundred thousand rows, written, read back, and checked against every rule
> the plan claimed. Fifty-six checks.

Finish with **Remove the rows**.

> It removes exactly what it wrote, by key. What was already there is untouched.

---

# At the command line

## 1. The catalog (2 minutes)

Open `sdg/packs/health/catalog.yaml`.

> This is the client's catalog, not the project's. Thirty business data
> elements, each with the rule for how its values are produced. Member ID is
> defined once, here, with its format and its PII class. Every application that
> has a member identifier inherits this definition rather than writing its own.

Point at three things:

- **`MEMBER_ID`** — rule `generate`, a format, PII class `identifier`, and a
  list of aliases. The aliases are matched on tokens, so `MEME_ID` in Facets and
  `MBR_ID` somewhere else both find this one entry.
- **`PLAN_CODE`** — rule `reuse`. The element says "plan codes come from master
  data"; it does not say which database that is. That binding is per
  environment.
- **`SSN`** — sits in the catalog and is used by nothing in this application.
  That is the point: the catalog belongs to the organization.

---

## 2. Discover and profile (1 minute)

```
sdg discover
sdg profile
```

> Discover reads the structure. Seven tables, and look at the bottom: it found a
> foreign key nobody declared. `CMC_CLCL_CLAIM.MEME_CK` has no constraint, but
> every value exists in the member table. That is the Facets pattern, and it is
> found by checking the data, not by reading the DDL.

> Profile reads the data. It fits a format to every string column, finds which
> columns determine which, works out the date orderings, and measures how many
> children each parent has.

Show one column:

```
type work\profile.json | findstr /C:"MEME_ID" /C:"fitted_format"
```

Or just point at the profile card on the review page in a moment.

---

## 3. Map and review (2 minutes)

```
sdg map
sdg review
```

> Ninety-four percent of the mappable columns matched an element at high
> confidence, with the evidence for each one recorded.

Open `work/review.html`.

Scroll to a profile card, for example `CMC_PRPR_PROV.PRPR_NPI`:

> Here is what the tool knows about this column. It measured the values, fitted
> the format, and then noticed that every value passes the NPI check-digit test.
> That is why it is confident this is a National Provider Identifier and not a
> phone number.

Now scroll to the review queue at the top:

> Six columns it will not decide on its own. Take `CDML_SVC_CD`. The catalog has
> no Facets alias recorded for a procedure code, so the name tells it nothing.
> But the format matches `PROCEDURE_CODE` exactly, so it offers that as the
> nearest element and asks. That is the difference from a default generator: the
> question comes with an answer attached.

---

## 4. The hierarchy (1 minute)

Scroll to **Hierarchy and volumes** on the same page.

> Claims per member. Not a flat four per member: twenty-nine percent of members
> have no claims at all, the median among those who do is five, the ninetieth
> percentile is fourteen, and the maximum is forty. That shape came from the
> data, and the evidence is written next to it.

> A flat ratio would give every member the same number of claims. No test
> exercises a member with none, and no test exercises the member with forty.

---

## 5. Plan (1 minute)

```
sdg plan --count dbo.CMC_SBSB_SUBSC=5000 ^
         --reuse dbo.CMC_PRPR_PROV --reuse dbo.CMC_PDPD_PRODUCT ^
         --exclude dbo.CMC_CLCL_AUDIT
```

> Five thousand subscribers. Everything else follows: eleven thousand members,
> fifty-three thousand claims, a hundred and sixty thousand lines. Those counts
> come from the measured ratios.

> Providers and products are set to reuse. The tool is not generating providers;
> it is loading the ones already there and pointing claims at them. The plan
> codes come from a different database entirely, filtered to the active ones:
> thirty-seven of forty, leaving out the three inactive codes.

Point at the provenance line:

> Every rule records where it came from. Structural rules the database's shape
> forced, catalog rules the organization owns, learned rules from the data.

---

## 6. Dry run (1 minute)

```
sdg dryrun
```

> Nothing is written. These are the rows it would produce. Note the claim total
> equals the sum of its lines, because the profiler found that relationship and
> the plan kept it as an expression rather than as a number drawn from a
> distribution.

---

## 7. Generate and validate (1 minute)

```
sdg generate
sdg validate
```

> Two hundred and thirty thousand rows, loaded, then read back and checked
> against every rule the plan claimed: keys resolve, unique columns are unique,
> code columns stay inside their lists, null rates land within five points, and
> the claim totals still equal their line sums.

Show the manifest:

```
type work\run-manifest.json | more
```

> The manifest records the seed, the plan version, the reference sets, and the
> key of every row written, so the run can be reproduced or removed exactly.

---

## 8. Change the catalog and regenerate (1 minute)

This is the closing move. Edit `sdg/packs/health/catalog.yaml`, find `MEMBER_ID`, and
widen the format:

```yaml
  - name: MEMBER_ID
    params: {regex: "[A-Z]{4}\\d{9}", unique: true}
    format: "[A-Z]{4}\\d{9}"
```

Then:

```
sdg plan --count dbo.CMC_SBSB_SUBSC=5000 ^
         --reuse dbo.CMC_PRPR_PROV --reuse dbo.CMC_PDPD_PRODUCT ^
         --exclude dbo.CMC_CLCL_AUDIT
sdg dryrun
```

> One change, in one place, and every member identifier in every table changes
> with it. Nothing about the schema was touched, no project was edited, and if
> ten applications mapped to this element, all ten would follow.

---

## Tidying up

```
sdg cleanup --yes
```

> It removes exactly the rows it wrote, by key. The rows that were already there
> are untouched.

Mention the one honest caveat if the audit table comes up:

> The claim table has an insert trigger, and it fired during the load. Those
> audit rows stay: the database wrote them, not the tool, and deleting rows it
> did not write is not something a test data tool should do quietly. The run
> reports that it happened.

---

## If you only have three minutes

```
scripts\demo.cmd
```

Runs the whole sequence and prints the success measures at the end.
