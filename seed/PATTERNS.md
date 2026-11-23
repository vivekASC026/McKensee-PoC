# Planted patterns

The seed data is the fixture for the whole proof of concept. Each pattern below
is put there on purpose and is checked by `seed/verify.py` and again by the
build step named in the last column.

| # | Pattern | Where | Checked in |
|---|---------|-------|------------|
| 1 | `CMC_CLCL_CLAIM.MEME_CK` carries no foreign key, but every value exists in `CMC_MEME_MEMBER.MEME_CK` | claims | Step 4 inference |
| 2 | Identity (`SBSB_CK`, `MEME_CK`), computed (`CLCL_ROW_HASH`), and defaulted (`CLCL_CREATED_TS`) columns | schema | Step 4 classification |
| 3 | Claims per member: 30 percent of members have none; the rest follow a long tail with median 5, 90th percentile 14, maximum 40 | claims | Step 5 ratio |
| 4 | Members enrolled on a dental product carry about half the claims of medical members | claims | Step 5 (measured only) |
| 5 | Dental products never carry a service code beginning with `M` | claim lines | Step 5 (measured only) |
| 6 | `MEME_ZIP` determines `MEME_CITY` and `MEME_STATE`, over about 600 ZIP triples | members | Step 5 functional dependency |
| 7 | `CLCL_SVC_DT` falls inside member coverage; `CLCL_RECD_DT` is 0 to 30 days after service; `CLCL_PAID_DT` is 5 to 60 days after received | claims | Step 5 date ordering, Step 7 date-relative |
| 8 | `MEME_TERM_DT` is null for 85 percent of members; `CLCL_PAID_DT` is null for 20 percent of claims | members, claims | Step 5 nullability |
| 9 | `CLCL_CUR_STS` takes six values with `02` at about 70 percent | claims | Step 7 weighted lookup |
| 10 | `CLCL_TOT_CHG` equals the sum of `CDML_CHG_AMT` for the claim, exactly | claims, lines | Step 7 expression proposal |
| 11 | Every `PRPR_NPI` passes the NPI check-digit test (Luhn over `80840` + first nine digits) | providers | Step 5 check digit, Step 6 mapping evidence |
| 12 | Business key formats: `SBSB_ID` `[A-Z]{3}\d{6}`, `MEME_ID` `[A-Z]{3}\d{8}`, `CLCL_ID` `\d{12}`, `CDML_SVC_CD` `[A-Z]\d{4}`, `PRPR_ID` `[A-Z]{3}\d{9}` | all | Step 5 format fitting, Step 6 mapping |
| 13 | An `AFTER INSERT` trigger on `CMC_CLCL_CLAIM` writes one row to `CMC_CLCL_AUDIT` per claim | claims | Step 9 writer |
| 14 | `REF_PLAN_CODE` holds 40 codes, 3 of them inactive | MDM_REF | Step 8 reference set |
| 15 | `REF_PRODUCT_CODE` carries the same identifiers as `CMC_PDPD_PRODUCT`, in a second database | MDM_REF | Step 8 reference set |
| 16 | Every subscriber sits in a group, a subgroup, and a coverage plan; the plan names the product | enrollment | analyzer hierarchy |
| 17 | Each group holds a different workforce: ages spread about 38 years between the youngest group and the oldest, and the sex balance spreads about 50 points | members | analyzer clustering |
| 18 | A claim carries the product its member is enrolled on, never another | claims, enrollment | analyzer claims |

## Volumes

Measured, not estimated. Volumes below the provider floor are held there.

| Scale | Subscribers | Members | Providers | Products | Claims | Claim lines | Build |
|-------|------------:|--------:|----------:|---------:|-------:|------------:|------:|
| `--small` (1 percent) | 200 | 469 | 50 | 25 | 2,254 | 6,982 | 0.4s |
| `--medium` (25 percent) | 5,000 | 11,451 | 75 | 25 | 52,980 | 163,342 | 4.2s |
| full | 20,000 | 46,044 | 300 | 25 | 213,295 | 657,217 | 17.2s |

All twenty-two planted patterns hold at every scale. At full volume the
claims-per-member distribution measures a 29.6 percent childless share, a median
of 5 among members with claims, a 90th percentile of 14, and a maximum of 40.

Provider count keeps a floor of 50 so the low-cardinality columns still have
enough distinct values to profile at the smallest scale. Product and plan code
counts do not scale; they are reference data.

## A note on the claim volume

The build brief lists both 2,000,000 claims and a claims-per-member
distribution with median 5 and a maximum near 40. Those cannot both hold: with
46,000 members, a maximum of 40 caps the table at 1.84 million rows, and a
median of 5 puts the real total near 215,000. The distribution is the tested
fixture, so the seed honors it and the claim count falls out at about 215,000.

## A note on "median 5"

The brief reads "30 percent of members have zero; **the rest** follow a long
tail with median 5". The median therefore describes members that have at least
one claim. Including the zeros would put the median at 3. The profiler reports
both figures (`median` over all parents and `median_nonzero`), and the tests
check `median_nonzero`.
