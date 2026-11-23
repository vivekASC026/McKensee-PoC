# SNIP rule files

SDM's SNIP-style checks are driven entirely by files. The engine
(`sdg/writers/snip/engine.py`) knows the rule *kinds* below and nothing about
any transaction set. Everything a set requires is written here, and a customer
changes it in their own folder with no change to code.

This is SNIP-style checking, not certification: the files carry what they
carry. The report always says which levels were checked and which were not.

## Where the files live

| | Shipped (replaced on upgrade) | Customer (kept) |
|---|---|---|
| Rules | `sdg/writers/snip/rules/<set>.json` | `work/snip/<set>.json` |
| Code sets | `sdg/writers/snip/codesets/<name>.txt` | `work/snip/codesets/<name>.txt` |
| Trading partners | -- | `work/snip/partners/<name>.json` |

`SDG_SNIP_DIR` in `.env` points the customer folder elsewhere -- a shared drive,
say. `work/` is never packaged and survives an upgrade.

**How they combine.** A customer file whose `transaction` and `version` match a
shipped one extends it:

* a rule with a new `id` is added;
* a rule with the same `id` replaces the shipped one, field by field;
* `{"id": "...", "enabled": false}` switches a shipped rule off.

A customer file for a set nothing ships -- an 837I, a 270 -- is used on its own.
A customer code set replaces the shipped one of the same name.

**Trading partners** (SNIP level 7) are one file each, chosen on the EDI screen.
Their rules are added to the set's, at level 7 unless they say otherwise.

## A rule file

```json
{
  "id": "837P",
  "transaction": "837",
  "version": "005010X222A1",
  "functional_code": "HC",
  "scopes": [
    {"name": "claim", "opens": "CLM"},
    {"name": "line", "opens": "LX"}
  ],
  "rules": [ ... ]
}
```

`scopes` name the repeating parts of the set: a scope opens on a segment (with
an optional `when`) and lasts until another opener of the same or an outer
scope. They are listed outermost first. Rules use them to say "in every claim".

Every rule has an `id`, a `kind`, a SNIP `level` (1-7), and optionally a
`severity` (`error`, the default, or `warning`), a `message` that replaces the
engine's wording, and `"enabled": false`.

### Selecting segments

Most rules take a `segment` and an optional `when`, which picks occurrences by
an element's value:

```json
"segment": "NM1", "when": {"position": 1, "equals": "85"}
"when": {"position": 1, "in": ["IL", "74"]}
"when": {"position": 2, "not_in": ["18"]}
```

Positions are X12 positions, from 1. A composite's part is `"component": 2`.

## Rule kinds

**`required_segment`** -- a segment must appear, in the transaction or in every
occurrence of a scope. `min` (default 1) and `max` bound the count.

```json
{"id": "payer-name", "level": 2, "kind": "required_segment", "scope": "subscriber",
 "segment": "NM1", "when": {"position": 1, "equals": "PR"}, "label": "Payer name (2010BB)"}
```

**`element`** -- one element of each selected segment. Any of:

| Field | Checks |
|---|---|
| `required` | present and non-empty |
| `min`, `max` | length |
| `type` | `AN`, `ID`, `N0`, `N2`, `R` (decimal), `DT` (CCYYMMDD), `TM` (HHMM) |
| `codes` | one of a list written in the rule |
| `code_set` | one of a named code set file; noted as not checked if the file is missing |
| `pattern`, `pattern_label` | matches a regular expression |
| `check: "npi"` | the NPI check digit |

```json
{"id": "clm05-2", "level": 2, "kind": "element", "segment": "CLM", "position": 5,
 "component": 2, "required": true, "codes": ["B"]}
```

**`both_or_neither`** -- the listed elements are all present or all absent.

```json
{"id": "nm1-id-pair", "level": 4, "kind": "both_or_neither", "segment": "NM1", "positions": [8, 9]}
```

**`followed_by`** -- a segment must be followed directly by another.

```json
{"id": "address-then-city", "level": 4, "kind": "followed_by", "segment": "N3", "then": "N4"}
```

**`conditional`** -- when `if` holds on a segment, `then` must: another element
of the same segment, or a segment elsewhere (within `scope`, if given).

```json
{"id": "subscriber-is-self", "level": 4, "kind": "conditional",
 "if": {"segment": "INS", "position": 1, "equals": "Y"},
 "then": {"position": 2, "equals": "18"},
 "text": "INS01 is Y, so INS02 must be 18."}
```

**`date_by_qualifier`** -- a date's format follows its qualifier (D8, RD8).

```json
{"id": "dates", "level": 4, "kind": "date_by_qualifier", "segment": "DTP",
 "format_position": 2, "value_position": 3}
```

**`sum`** -- a total equals its parts, less any deductions, within each
occurrence of `scope` or across the transaction. `tolerance` defaults to 0.005.

```json
{"id": "claim-balances", "level": 3, "kind": "sum", "scope": "claim",
 "total": {"segment": "CLM", "position": 2},
 "parts": {"segment": "SV1", "position": 2}}
```

## Code sets

One code per line; anything after the first word, and anything after `#`, is
ignored, so a list can carry descriptions. Shipped: `place_of_service`,
`states`, `claim_filing`, `relationship_834`.

Not shipped, because they are large and updated every year: `icd10cm`
(ICD-10-CM, published free by CMS) and `hcpcs` (HCPCS Level II, CMS). Put them
in `work/snip/codesets/` as `icd10cm.txt` and `hcpcs.txt` -- codes without the
dot -- and the membership rules switch on. CPT is licensed by the AMA and is
never shipped; a licensee can add it the same way and point a rule at it.

## Where the codes in SDM's own files come from

The validation is separate from generation and changes nothing in it. The
codes SDM writes are another matter, and configured elsewhere: an element
marked `code_set: true` in the catalog takes its values from the pattern
store -- the codes the sources actually use -- so files carry real codes
whichever database is being generated into.

## A trading partner

`work/snip/partners/acme.json`:

```json
{
  "name": "ACME Health",
  "rules": [
    {"id": "acme-submitter", "kind": "element", "segment": "NM1",
     "when": {"position": 1, "equals": "41"}, "position": 9, "codes": ["SDM001"],
     "message": "ACME accepts files only from submitter SDM001."},
    {"id": "acme-max-claims", "kind": "required_segment", "segment": "CLM", "max": 5000}
  ]
}
```
