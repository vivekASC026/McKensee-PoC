# FACETS sources for consistent EDI 837 generation

What the analysis store needs from FACETS so that a generated 837P / 837I claim
agrees with the member's enrollment, plan, benefits and provider network on the
date of service -- and the new `sdm` tables that hold it.

- Every table and column named here was checked against the FACETS data
  dictionary (1,445 tables, 22,811 columns). Descriptions, primary keys and
  types below are the dictionary's own. Nothing was invented; where a code value
  is not in the dictionary it is called out as an assumption.
- `sdm.member_denorm` and `sdm.claims_denorm` stay as they are. Twenty-one new
  tables are added in schema `sdm`.
- DDL: `sdg/packs/health/sql/03_denorm_837_tables.sql` (guarded, add-only).
  Extract: `sdg/packs/health/sql/04_denorm_837_extract.sql` (full refresh of the new
  tables only, scoped to what the existing denorm tables reference).
- Both scripts were run end to end on SQL Server against a scratch database
  holding the FACETS tables built from the dictionary's column types and a small
  test data set (professional + hospital claim, group practitioner, PCP,
  facility, COB, benefits); they ran clean twice in a row (re-runnable) and the
  scratch database was dropped.

## Contents

1. How the pieces connect
2. Generating a consistent claim (the lookup order)
3. 837 loop crosswalk
4. Source tables by area (with join path, dating and 837 mapping)
5. New sdm tables (grain, columns, indexes, extract SQL)
6. Performance
7. Gaps: what the 837 needs that the dictionary does not hold
8. Assumptions

## 1. How the pieces connect

```
member_denorm (MEME_CK, SBSB_CK, GRGR_CK, CSPD_CAT, CSPI_ID, PDPD_ID)
   |
   +-- CMC_MEPE_PRCS_ELIG ........ eligibility span; adds CSCS_ID      -> sdm.member_elig
   |      GRGR_CK+CSCS_ID+CSPD_CAT+CSPI_ID
   |         |
   |         +-- CMC_CSPI_CS_PLAN  (dated) -> PDPD_ID, NWST_PFX          -> sdm.plan_denorm
   |         +-- CMC_CSCS_CLASS          -> TPCT_PFX (service conversion)
   |         +-- CMC_PDPD_PRODUCT -> LOBD_ID -> CMC_LOBD_LINE_BUS -> CMC_PYPY_PAYOR (2010BB)
   |
   |   NWST_PFX -> CMC_NWST_NET_SET (dated): NWNW_ID + NWPR_PFX, PDVC_TIER, I/P status -> sdm.plan_network
   |                     |
   |                     +-- CMC_NWPR_RELATION (dated) on NWNW_ID + NWPR_PFX -> PRPR_ID, AGAG_ID -> sdm.provider_network
   |                               |
   |                               +-- CMC_PRPR_PROV -> CMC_PRCP_COMM_PRAC / CMC_PRFA_FACILITY / CMC_MCTN_TAX_NAME -> sdm.provider_denorm
   |                               +-- CMC_PRAD_ADDRESS (dated) via PRPR.PRAD_ID                             -> sdm.provider_address
   |                               +-- CMC_PRER_RELATION / CMC_PRDS_DATE / CMC_PRAF_FAC_AFFIL (dated)          -> sdm.provider_relation
   |
   |   PDPD_ID + PDVC_TIER -> CMC_PDVC_VAR_COMP (dated) -> SEPY_PFX -> CMC_SEPY_SE_PYMT (dated) -> SESE_ID + SESE_RULE
   |                                                     |                -> CMC_SESE_SERVICE, CMC_SETR_TIER, CMC_SEDF_SE_DEFN -> sdm.plan_benefit_denorm
   |                                                     +-- DEDE_PFX -> CMC_DEDE_DEDUCT; LTLT_PFX -> CMC_LTLT_LIMIT + CMC_LTSE_SE_IX -> sdm.plan_accumulator
   |   TPCT_PFX -> CMC_TPCT_CPT_CONV / CMC_RCCT_CONV / CMC_SECT_CONV (code range + setting -> SESE_ID) -> sdm.service_code_map
   |   PSCD_ID  -> CMC_PSCD_POS_DESC (setting)                                                         -> sdm.place_of_service
   |
   +-- CMC_SBSB_SUBSC / CMC_SBAD_ADDR / CMC_GRGR_GROUP -> sdm.subscriber_denorm, sdm.member_address
   +-- CMC_MEPR_PRIM_PROV (dated) -> sdm.member_pcp
   +-- CMC_MECB_COB (dated) + CMC_MCRE_RELAT_ENT -> sdm.member_cob

claims_denorm (CLCL_ID, CDML_SEQ_NO)
   +-- CMC_CLCL_CLAIM + CMC_CLHP_HOSP + CMC_CLMF_MULT_FUNC + CMC_CLSD_SUPP_DATA + CMC_CLED_EDI_DATA + CMC_CLCB_CL_COB -> sdm.claims_header_ext_denorm
   +-- CMC_CDML_CL_LINE + CMC_CDSD_SUPP_DATA + CMC_CDCB_LI_COB -> sdm.claims_detail_denorm
   +-- CMC_CLMD_DIAG -> sdm.claims_diag;  CMC_CLHI_PROC -> sdm.claims_icd_proc
   +-- CMC_CLHC_COND_CODE / CMC_CLHO_OCC_CODE / CMC_CLVC_VAL_CODE -> sdm.claims_ub_code
   +-- CMC_UMSV_SERVICES via CDML_UMAUTH_ID / CDML_UMREF_ID -> sdm.claims_auth
```

Row valid on a service date, everywhere: `EFF_DT <= svc_dt AND ISNULL(TERM_DT, '9999-12-31') >= svc_dt`.

## 2. Generating a consistent claim (the lookup order)

For member `m` and service date `svc_dt`:

1. **Eligibility.** `sdm.member_elig` row for `m.MEME_CK` and the product category, with `svc_dt` in the span and `MEPE_ELIG_IND = 'Y'`. Gives GRGR_CK, CSCS_ID, CSPI_ID, PDPD_ID.
2. **Plan.** `sdm.plan_denorm` on GRGR_CK + CSCS_ID + CSPD_CAT + CSPI_ID with `svc_dt` in the CSPI window. Gives NWST_PFX (networks), TPCT_PFX (code-to-service conversion), LOBD_ID and the payer (2010BB), PDDS_PROD_TYPE (SBR09).
3. **Networks and tier.** `sdm.plan_network` rows for NWST_PFX in force on `svc_dt`: (NWNW_ID, NWPR_PFX, PDVC_TIER, NWST_PR_NW_STS).
4. **Providers.**
   - In network: `sdm.provider_network` rows with the same NWNW_ID + NWPR_PFX, in force on `svc_dt`, accepting the member's age and sex (NWPR_MIN_AGE / NWPR_MAX_AGE / NWPR_SEX), joined to `sdm.provider_denorm` (PRPR_TERM_DT null or after `svc_dt`). For an out-of-network claim pick a provider with no such row; then the non-par PDVC rows apply.
   - **837P:** rendering (2310B / 2420A) = a practitioner (`PRPR_ENTITY = 'P'`). Billing (2010AA) = the group in force in `sdm.provider_relation` (`REL_SOURCE = 'PRER'`, `REL_ENTITY = 'G'`), else the practitioner. Pay-to (2010AB) = the `PRDS` payee in force, at its `IS_REMIT = 'Y'` address. Service facility (2310C) = the rendering provider's `IS_PRIMARY` practice address (or a facility). Referring (2310A) = the member's PCP in force in `sdm.member_pcp` when the product needs referrals.
   - **837I:** billing (2010AA) = a facility (`PRPR_ENTITY = 'F'`) in network; attending / operating (2310A / 2310B) = practitioners with a `PRAF` affiliation to that facility in force.
   - NPI = `PRPR_NPI`; taxonomy = `PRPR_TAXONOMY_CD`; tax ID = `MCTN_ID` with REF01 = EI or SY from `MCTN_TYPE`; addresses from `sdm.provider_address` in force on `svc_dt`.
5. **Covered services.** `sdm.plan_benefit_denorm` for PDPD_ID and the network's PDVC_TIER (and the PDVC condition flags that fit the claim: par / non-par / PCP, auth obtained, referral obtained), with `BEN_EFF_DT <= svc_dt <= BEN_TERM_DT`, `SESE_RULE_TYPE` not Disallow, and the member's age / sex within SESE_MIN_AGE / SESE_MAX_AGE / SESE_VALID_SEX.
6. **Codes and place of service.** For the chosen SESE_ID: `sdm.service_code_map` rows of the plan's TPCT_PFX -- `MAP_KIND = 'PROC'` (837P SV101 / 837I SV202) or `'REV'` (837I SV201) -- whose `MCTR_SETG` equals the `TPCT_MCTR_SETG` of the chosen `sdm.place_of_service` row; draw the procedure / revenue code inside CODE_LOW..CODE_HIGH (valid on `svc_dt` in CMC_IPCD_PROC_CD / CMC_RCRC_RC_DESC).
7. **Cost share.** SETR_COPAY_AMT, SETR_COIN_PCT and the deductible accumulator ACAC_ACC_NO (amounts in `sdm.plan_accumulator`) give CDML_COPAY_AMT / CDML_COINS_AMT / CDML_DED_AMT that match the plan.
8. **Authorization / referral.** When SEDF_UM_REQ_IND (or the PDVC_PC_OBT / PDVC_REF_OBT row chosen) says one is needed, generate REF*G1 / REF*9F and keep the line inside the authorized window, provider, POS and units (shape in `sdm.claims_auth`).
9. **COB.** `sdm.member_cob` row in force: MECB_INSUR_ORDER gives 2000B SBR01 for this payer and 2320 SBR01 for the other; 2330A / 2330B from the row; amounts shaped like `CLCB_*` / `CDCB_*`.

## 3. 837 loop crosswalk

| 837 loop / segment | Element(s) | Where it comes from (new tables unless noted) |
|---|---|---|
| 1000A / 1000B submitter, receiver | NM1, PER | Not in FACETS (CLED_TRAD_PARTNER only) -- generator configuration |
| 2000A billing provider specialty | PRV*BI*PXC | provider_denorm.PRPR_TAXONOMY_CD (billing provider) |
| 2010AA billing provider | NM101 85, NM102, NM103-105, NM109 (XX) | provider_denorm: NM1_ENTITY_TYPE, PRPR_NAME / PRFA_NAME or PRCP_LAST/FIRST/MID, PRPR_NPI |
| 2010AA | N3, N4 | provider_address (IS_PRIMARY = 'Y', PRAD_PRACTICE_IND = 'Y') |
| 2010AA | REF*EI / REF*SY | provider_denorm.MCTN_ID, MCTN_TYPE |
| 2010AB pay-to address | NM1*87, N3, N4 | provider_relation (PRDS payee) -> provider_address (IS_REMIT = 'Y'); claim: claims_header_ext_denorm.CLCL_PAYEE_PR_ID |
| 2000B subscriber | SBR01 | member_cob.MECB_INSUR_ORDER (P when no other coverage) |
| 2000B | SBR02, SBR03, SBR04 | member_denorm.MEME_REL ('M' -> 18); subscriber_denorm.GRGR_ID; claims_denorm.GRGR_NAME |
| 2000B | SBR09 claim filing indicator | Derived from plan_denorm.PDDS_PROD_TYPE (gap: no column) |
| 2010BA subscriber | NM1*IL NM103-105, NM109 | subscriber_denorm SBSB_LAST_NAME / FIRST / MID_INIT, SBSB_ID (+ MEME_SFX) |
| 2010BA | N3, N4, DMG | subscriber_denorm SBAD_*; member_denorm MEME_BIRTH_DT, MEME_SEX (subscriber's member row) |
| 2010BB payer | NM1*PR NM103, NM109; N3, N4; REF*EI | plan_denorm PYPY_PAYOR_NAME, PYPY_NATL_PLAN_ID / PYPY_NAIC_CODE, PYPY_ADDR*, PYPY_TAX_ID |
| 2000C / 2010CA patient | PAT01, NM1*QC, N3, N4, DMG | member_denorm (MEME_REL, names, birth, sex); member_address (ADDR_ROLE = 'HOME') |
| 2300 claim | CLM01 | claims_header_ext_denorm.CLCL_PA_ACCT_NO |
| 2300 | CLM02 | claims_denorm.CLCL_TOT_CHG |
| 2300 | CLM05-1 | 837P: claims_denorm.PSCD_ID; 837I: CLHP_FAC_TYPE + CLHP_BILL_CLASS |
| 2300 | CLM05-3 | 837P: CLM_FREQUENCY (derived); 837I: CLHP_FREQUENCY |
| 2300 | CLM07, CLM08, CLM09 | CLCL_MED_ASSGN_IND, CLCL_PAY_PR_IND, CLCL_REL_INFO_IND |
| 2300 | CLM11 related causes | CLCL_ACD_IND, CLCL_ACD_STATE |
| 2300 | DTP*431 / 439 / 360 / 361 / 435 / 096 (837P) | CLCL_CURR_ILL_DT, CLCL_ACD_DT, CLCL_UNABL_FROM_DT / TO_DT, CLCL_RELHP_FROM_DT / TO_DT |
| 2300 | DTP*434 / 435 / 096 (837I) | CLHP_STAMENT_FR_DT / TO_DT, CLHP_ADM_DT + CLHP_ADM_HOUR_CD, CLHP_DC_HOUR_CD |
| 2300 | CL1 (837I) | CLHP_ADM_TYP, CLHP_ADM_SOURCE, CLHP_DC_STAT |
| 2300 | REF*F8 | CLCL_ID_ADJ_FROM (or CLCL_MECD_RESUB_NO for Medicaid) |
| 2300 | REF*G1 / REF*9F | claims_denorm.CLCL_EXT_AUTH_NO; CLMF_EXT_PREATH_NO; CLMF_EXT_REFRAL_NO |
| 2300 | REF*EA (837I) | CLHP_MED_REC_NO |
| 2300 | HI diagnoses (ABK, ABF; 837I ABJ) | claims_diag (all CLMD_TYPE positions), CLMD_POA_IND for 837I |
| 2300 | HI*BBR / BBQ (837I) | claims_icd_proc |
| 2300 | HI*BG / BH / BI / BE (837I) | claims_ub_code (COND, OCC, VAL) |
| 2300 | HI*DR (837I) | CLHP_INPUT_AGRG_ID / AGRG_ID |
| 2310A referring (837P) / 2310F (837I) | NM1*DN | claims_denorm.CLCL_PRPR_ID_REF -> provider_denorm; CLMF_PRPR_RF_NPI |
| 2310A attending (837I) | NM1*71, PRV*AT | CLHP_PRPR_ID_ADM, CLHP_PRPR_AD_NPI -> provider_denorm |
| 2310B operating (837I) / 2310C other (837I) | NM1*72 / NM1*ZZ | CLHP_PRPR_ID_OPER / CLHP_PRPR_ID_OTH1 (+ NPIs) |
| 2310B rendering (837P) / 2310D (837I) | NM1*82, PRV*PE | claims_denorm.PRPR_ID (practitioner) or CLSD_PRPR_ID_REND_NVL -> provider_denorm |
| 2310C service facility (837P) / 2310E (837I) | NM1*77, N3, N4 | CLMF_PRPR_ID_FAC or the servicing provider's PRAD_TYPE -> provider_address |
| 2320 other subscriber | SBR, AMT*D, CAS | member_cob; CLCB_COB_AMT, CLCB_COB_DED_AMT / COPAY / COINS |
| 2330A / 2330B | NM1*IL, NM1*PR, N3, N4 | member_cob MECB_PRIM_*, MCRE_NAME, MCRE_ADDR* (payer ID is a gap) |
| 2400 line | LX01 | claims_detail_denorm.CDML_EXT_LINE_NO |
| 2400 | SV101 / SV202 procedure + modifiers | PROC_CODE, IPCD_MOD1, CDML_IPCD_MOD2..4 |
| 2400 | SV102 / SV203, SV104 / SV205 | claims_denorm CDML_CHG_AMT, CDML_UNITS |
| 2400 | SV105 POS (837P) | claims_denorm.PSCD_ID |
| 2400 | SV107 diagnosis pointers | CDML_CLMD_TYPE2..5 (against claims_diag.CLMD_TYPE) |
| 2400 | SV201 revenue code, SV206 rate (837I) | claims_denorm.RCRC_ID; CDML_HCPCS_AMT |
| 2400 | DTP*472 | claims_denorm CDML_FROM_DT / CDML_TO_DT |
| 2400 | REF*G1 / REF*9F (line) | CDML_UMAUTH_ID / CDML_UMREF_ID |
| 2410 drug | LIN*N4, CTP04, CTP05 | CDSD_NDC_CODE, CDSD_NDC_UNITS, CDSD_NDC_MCTR_TYPE |
| 2420A line rendering | NM1*82 | CDSD_PRPR_ID_REND, CDSD_PRPR_RE_NPI -> provider_denorm |
| 2420G ambulance pick-up | N4 | CDSD_MED_AMB_ZIP |
| 2430 line adjudication | SVD, CAS | CDCB_COB_AMT, CDCB_COB_DED_AMT / COPAY / COINS |

## 4. Source tables by area

Each entry: functional area, the dictionary's description, primary key, how it
joins to the existing denorm grain, its effective dating, and the columns to
extract with the 837 element each feeds. The tables documented here but not
loaded by the extract are listed at the end of section 8.

### Provider

#### CMC_PRPR_PROV

- **Functional area:** prov (Base Provider Table for Practitioner, Facility, Group and IPA)
- **Description (dictionary):** This table stores much of the higher level characteristic data for a provider and serves as the base provider table during the reporting process. Some of the data stored in this table includes the provider's IDs by type, payment claim indicator, specialty, name, pre-authorization indicator, payment hold date, and the common practitioner ID (if applicable).
- **Primary key:** PRPR_ID
- **Join to the denorm grain:** PRPR_ID = claims_denorm.PRPR_ID (servicing), CLCL_PRPR_ID_REF (referring), CLCL_PRPR_ID_PCP (PCP); = CMC_MEPR_PRIM_PROV.PRPR_ID; = CMC_NWPR_RELATION.PRPR_ID; = CMC_CDSD_SUPP_DATA.CDSD_PRPR_ID_REND (line rendering); = CMC_CLCL_CLAIM.CLCL_PAYEE_PR_ID (pay-to).
- **Effective dating:** Not effective-dated. PRPR_TERM_DT is the provider termination; a provider is usable on a service date when PRPR_TERM_DT IS NULL or PRPR_TERM_DT >= service date.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PRPR_ID | char(12) | Provider Identifier | Key. Optional REF*G2 (payer-assigned provider ID) in 2010AA/2310x/2420x |
| PRPR_ENTITY | char(1) | Provider Entity | Practitioner vs facility: P = practitioner, G = provider group, I = IPA, F = facility (values from the dictionary's edit criteria). Drives NM102 (1 = person for P; 2 = non-person for G/I/F) |
| PRPR_NAME | char(55) | Provider Name | NM103 organization name for G/I/F (2010AA, 2310C/2310E service facility, 2010AB) |
| PRCP_ID | char(12) | Common Practitioner ID | Join to CMC_PRCP_COMM_PRAC for a practitioner's last/first name |
| PRPR_NPI | char(10) | National Practitioner Identifier | NM108 = XX, NM109 NPI (2010AA billing, 2310A referring, 2310B rendering, 2310C service facility, 2420A line rendering; 837I 2310A attending / 2310B operating) |
| PRPR_TAXONOMY_CD | char(12) | Taxonomy Code | PRV03 taxonomy (2000A PRV*BI*PXC billing, 2310B PRV*PE*PXC rendering, 837I 2310A PRV*AT attending). Column is char(12); taxonomy codes are 10 characters |
| PRPR_MCTR_TYPE | char(4) | Provider Type | Provider type (hospital, lab, MD...). Decoded in CMC_MCTR_CD_TRANS: MCTR_ENTITY = FAC (facility) or PRAC, MCTR_TYPE = TYPE |
| PRCF_MCTR_SPEC | char(4) | Provider Specialty | Primary specialty; keeps generated procedures consistent with the provider (claims_denorm.PRPR_MCTR_SPEC) |
| PRCF_MCTR_SPEC2 | char(4) | Secondary Specialty | Secondary specialty |
| PRPR_MCTR_PRTY | char(4) | Provider Practice Type | Practice type (sole proprietor, corporation...) |
| TPCT_MCTR_TCAT | char(1) | Provider Procedure/Revenue Code Conversion Table Category | Provider's service conversion category; selects the matching CMC_TPCT_CPT_CONV / CMC_RCCT_CONV rows |
| PRAD_ID | char(12) | Provider Address ID | Address owner: the provider's own PRPR_ID, or the group/IPA whose address it shares; join to CMC_PRAD_ADDRESS.PRAD_ID |
| PRAD_TYPE_PRIM | char(3) | Provider Primary Address Type | Primary practice address type: 2010AA N3/N4 and 2310C/2310E service facility N3/N4 |
| PRAD_TYPE_CHECK | char(3) | Provider Remittance Address Type | Remittance address type: 2010AB pay-to N3/N4 (Facets defaults it to PRAD_TYPE_PRIM when blank) |
| MCTN_ID | char(9) | Provider Tax Identification Number for search and reporting purposes | Tax ID: 2010AA REF02 (REF01 = EI or SY from CMC_MCTN_TAX_NAME.MCTN_TYPE) |
| PRPR_STS | char(2) | Provider Status | Provider status (Proxy / Mock PCP are the only functional values) |
| PRPR_TERM_DT | datetime | Provider Termination Date | Provider termination date (service-date filter) |
| PRPR_PREAUTH_IND | char(1) | Provider Pre-authorization Indicator | Provider pre-authorization requirement |
| PRCR_ID | char(12) | Credentialing ID | Credentialing ID; joins a practitioner to CMC_PRAF_FAC_AFFIL (facility affiliations) |

#### CMC_PRCP_COMM_PRAC

- **Functional area:** prov (Common Provider Indicative Information)
- **Description (dictionary):** This table is used as a one-time indicative entry point for a provider (excluding facilities) who might otherwise be identified multiple times within the system in order to comply with their various pricing agreements. The providers name, title, DOB, gender, Social Security Number, National Practitioner Number, and the last date this practitioner was used in channelling is stored.
- **Primary key:** PRCP_ID
- **Join to the denorm grain:** PRCP_ID = CMC_PRPR_PROV.PRCP_ID (practitioners only; PRPR_ENTITY = P).
- **Effective dating:** Not effective-dated; PRCP_TERM_DT ends the common practitioner.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PRCP_ID | char(12) | Common Practitioner Identifier | Key |
| PRCP_LAST_NAME | char(35) | Common Practitioner Last Name | NM103 last name for a person (2010AA when billing as an individual, 2310A, 2310B, 2420A; 837I 2310A/2310B) |
| PRCP_FIRST_NAME | char(15) | Common Practitioner First Name | NM104 first name |
| PRCP_MID_INIT | char(1) | Common Practitioner Middle Initial | NM105 middle initial |
| PRCP_TITLE | char(10) | Common Practitioner Title | Credential (MD, RN...). Not an 837 element |
| PRCP_NPI | char(10) | National Provider Identifier | Practitioner NPI (PRPR_NPI is copied from it for practitioners) |
| PRCP_TERM_DT | datetime | Common Practitioner Termination Date | Termination date |

#### CMC_PRFA_FACILITY

- **Functional area:** prov (Facility Indicative Information Data)
- **Description (dictionary):** This table stores indicative data for facilities including name, address, AHA and AMA numbers, payment hold date, accreditation type, Medicare information, room rate, and last channel date.
- **Primary key:** PRPR_ID
- **Join to the denorm grain:** PRPR_ID = CMC_PRPR_PROV.PRPR_ID where PRPR_ENTITY = F.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PRPR_ID | char(12) | Provider Identifier | Key |
| PRFA_NAME | char(50) | Facility Name | Facility name: NM103 for 837I 2010AA billing facility and 2310E / 837P 2310C service facility |

#### CMC_PRAD_ADDRESS

- **Functional area:** prov (Provider Address Information by Address Type)
- **Description (dictionary):** This tables stores address information for each provider by address type: primary, mailing, etc. Multiple addresses can be stored.
- **Primary key:** PRAD_ID, PRAD_TYPE, PRAD_EFF_DT
- **Join to the denorm grain:** PRAD_ID = CMC_PRPR_PROV.PRAD_ID and PRAD_TYPE = PRAD_TYPE_PRIM (practice), PRAD_TYPE_CHECK (remit), or CMC_CLCL_CLAIM.PRAD_TYPE (the address the claim was serviced at).
- **Effective dating:** PRAD_EFF_DT / PRAD_TERM_DT. Row valid on the service date: PRAD_EFF_DT <= svc_dt AND (PRAD_TERM_DT IS NULL OR PRAD_TERM_DT >= svc_dt).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PRAD_ID | char(12) | Provider Address ID | Key part |
| PRAD_TYPE | char(3) | Provider Address Type | Key part; user-defined address type |
| PRAD_EFF_DT | datetime | Provider Address (Type) Effective Date | Key part; effective date |
| PRAD_TERM_DT | datetime | Provider Address (Type) Termination Date | Termination date |
| PRAD_ADDR1 | char(40) | Provider Address (Type) Line 1 | N301 (2010AA, 2010AB, 2310C/2310E, 2420C) |
| PRAD_ADDR2 | char(40) | Provider Address (Type) Line 2 | N302 |
| PRAD_CITY | char(19) | Provider Address (Type) City | N401 |
| PRAD_STATE | char(2) | Provider Address (Type) State | N402 |
| PRAD_ZIP | char(11) | Provider Address (Type) Zip Code | N403 (2010AA needs ZIP+4) |
| PRAD_CTRY_CD | char(4) | Provider Address (Type) Country | N404 |
| PRAD_COUNTY | char(20) | Provider Address (Type) County | County (geography consistency with the member) |
| PRAD_PHONE | char(20) | Provider Address (Type) Phone Number | PER04 billing provider contact (TE) |
| PRAD_PRACTICE_IND | char(1) | Provider Address (Type) Practice Indicator | Y = a service location; only practice addresses may be a service facility / 2010AA physical address |
| PRAD_TYPE_MAIL | char(3) | Provider Address (Type) Corresponding Mailing Address | Mailing address type paired with this practice location |

#### CMC_PRNP_NPI

- **Functional area:** prov (NPI History)
- **Description (dictionary):** This table will support historical changes made to a provider's NPI with date sensitivity.
- **Primary key:** PRPR_ID, PRCP_ID, PRPR_NPI
- **Join to the denorm grain:** PRPR_ID = CMC_PRPR_PROV.PRPR_ID (and PRCP_ID).
- **Effective dating:** Term date only (PRNP_TERM_DT); the NPI without a term date, or with PRNP_TERM_DT >= svc_dt, is the one in force. Optional: use when NPIs changed during the claim period.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PRPR_ID | char(12) | Provider ID | Key part |
| PRCP_ID | char(12) | Common Practitioner ID | Key part |
| PRPR_NPI | char(10) | NPI | Key part; historical NPI |
| PRNP_TERM_DT | datetime | Term Date | NPI end date |

#### CMC_MCTN_TAX_NAME

- **Functional area:** mcar (Tax Name)
- **Description (dictionary):** This table will maintain Tax Name data, including a Tax Address, for Commission Entities and Providers
- **Primary key:** MCTN_ID, MCTN_TYPE
- **Join to the denorm grain:** MCTN_ID = CMC_PRPR_PROV.MCTN_ID (also CMC_MCTI_TAX_INFO.MCTN_ID + MCTN_TYPE).
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| MCTN_ID | char(9) | Tax ID | Tax identification number: 2010AA REF02 |
| MCTN_TYPE | char(1) | Type | EIN or SSN: 2010AA REF01 = EI (EIN) or SY (SSN) |
| MCTN_NAME | char(55) | Corporation Tax Name | Corporate tax name (W-9 name); not sent on the 837 |

#### CMC_MCTI_TAX_INFO

- **Functional area:** mcar (Tax Information for Providers/Commission Entities)
- **Description (dictionary):** This table contains Tax Information for Providers and Commission Entities.
- **Primary key:** MCTI_ENTY_ID, MCTI_TYPE, MCTI_EFF_DT
- **Join to the denorm grain:** MCTI_ENTY_ID = CMC_PRPR_PROV.PRPR_ID (assumption: the dictionary gives no definition for MCTI_ENTY_ID / MCTI_TYPE).
- **Effective dating:** MCTI_EFF_DT / MCTI_TERM_DT. Optional: the tax ID in force on a date when it has changed.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| MCTI_ENTY_ID | char(12) | Entity ID | Entity (provider) ID |
| MCTI_TYPE | char(1) | Type | Entity type |
| MCTI_EFF_DT | datetime | Effective Date | Effective date |
| MCTI_TERM_DT | datetime | Termination Date | Termination date |
| MCTN_ID | char(9) | Tax ID | Tax ID in force |
| MCTN_TYPE | char(1) | Tax ID Type | EIN / SSN |

#### CMC_NWPR_RELATION

- **Functional area:** prov (Participating Provider Link to Network ID)
- **Description (dictionary):** This table stores information on those providers who participate in a particular provider network. It further forms a link between provider and network IDs and includes effective and termination dates, agreement ID, PCP indicator, and patient acceptance requirements for that relationship. This link is also utilized for directory and channeling purposes.
- **Primary key:** PRPR_ID, NWNW_ID, NWPR_PFX, NWPR_PCP_IND, NWPR_EFF_DT
- **Join to the denorm grain:** PRPR_ID = provider; NWNW_ID + NWPR_PFX = CMC_NWST_NET_SET.NWNW_ID + NWPR_PFX for the member's plan (CMC_CSPI_CS_PLAN.NWST_PFX). That is the provider-to-plan network link.
- **Effective dating:** NWPR_EFF_DT / NWPR_TERM_DT. In network on the service date: NWPR_EFF_DT <= svc_dt AND (NWPR_TERM_DT IS NULL OR NWPR_TERM_DT >= svc_dt).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PRPR_ID | char(12) | Provider Identifier | Key part |
| NWNW_ID | char(12) | Network Provider Relationship Identifier | Key part; network |
| NWPR_PFX | char(4) | Network Provider Relationship Prefix | Key part; product-component prefix linking provider + agreement to products |
| NWPR_PCP_IND | char(1) | Network Provider Relationship Primary Care Provider Indicator | Key part; provider is a PCP in this network (eligible for CMC_MEPR_PRIM_PROV / claims_denorm.CLCL_PRPR_ID_PCP) |
| NWPR_EFF_DT | datetime | Network Provider Relationship Effective Date | Key part; participation start |
| NWPR_TERM_DT | datetime | Network Provider Relationship Termination Date | Participation end |
| AGAG_ID | char(12) | Network Provider Relationship Agreement Identifier | Provider agreement (contract) used to adjudicate; join CMC_AGAG_AGREEMENT. claims_denorm has no AGAG_ID, CMC_CLCL_CLAIM does |
| NWPR_ACC_PAT_IND | char(1) | Network Provider Relationship Accepting Patients Indicator | Accepting patients |
| NWPR_MIN_AGE | smallint | Network Provider Relationship Minimum Patient Age | Minimum patient age accepted |
| NWPR_MAX_AGE | smallint | Network Provider Relationship Maximum Patient Age | Maximum patient age accepted |
| NWPR_SEX | char(1) | Network Provider Relationship Gender | Patient gender accepted |

#### CMC_NWNW_NETWORK

- **Functional area:** prov (Provider Network Indicative Data)
- **Description (dictionary):** This table stores all the indicative data related to a provider network, and is where the Network ID originates.
- **Primary key:** NWNW_ID
- **Join to the denorm grain:** NWNW_ID = CMC_NWPR_RELATION.NWNW_ID = CMC_NWST_NET_SET.NWNW_ID = claims_denorm.NWNW_ID.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| NWNW_ID | char(12) | Network Identifier | Key |
| NWNW_NAME | char(50) | Network Name | Network name |
| NWNW_MCTR_TYPE | char(4) | Network Type | Network type (HMO, PPO...) |

#### CMC_AGAG_AGREEMENT

- **Functional area:** prov (Provider Agreement Table)
- **Description (dictionary):** This table stores the base information for a provider agreement, and is where the Agreement ID is created. This table links with other tables in the provider data model area that determine reimbursement for services.
- **Primary key:** AGAG_ID, AGAG_EFF_DT
- **Join to the denorm grain:** AGAG_ID = CMC_NWPR_RELATION.AGAG_ID (or CMC_CLCL_CLAIM.AGAG_ID).
- **Effective dating:** AGAG_EFF_DT / AGAG_TERM_DT (AGAG_EFF_DT is part of the key). Pick the row with AGAG_EFF_DT <= svc_dt AND (AGAG_TERM_DT IS NULL OR AGAG_TERM_DT >= svc_dt).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| AGAG_ID | char(12) | Provider Agreement Identifier | Key part |
| AGAG_EFF_DT | datetime | Provider Agreement Effective Date | Key part |
| AGAG_TERM_DT | datetime | Provider Agreement Termination Date | Termination date |
| AGAG_MCTR_TYPE | char(4) | Provider Agreement Type | Agreement type (fee schedule, DRG...) |
| AGAG_DESC | varchar(70) | Agreement Description | Agreement description |

#### CMC_PRER_RELATION

- **Functional area:** prov (Provider Entity Relationship Information Data)
- **Description (dictionary):** This table stores relationship information between provider entities, groups, IPAs or Sponsors. The Provider Group and IPA PRER relationships are read during the processing applications, except capitation (claims, UM, Member/PCP selection, channeling, and directory printing). There is no functionality for the Sponsor PRER relationship at this time.
- **Primary key:** PRPR_ID, PRER_PRPR_ENTITY, PRER_EFF_DT
- **Join to the denorm grain:** PRPR_ID = the practitioner/facility/group; PRER_PRPR_ID = the related group (G), IPA (I) or sponsor (S).
- **Effective dating:** PRER_EFF_DT / PRER_TERM_DT. Relationship in force on svc_dt.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PRPR_ID | char(12) | Provider Identifier | Key part |
| PRER_PRPR_ENTITY | char(1) | Provider Entity Relationship | Key part; G = group, I = IPA, S = sponsoring physician |
| PRER_EFF_DT | datetime | Provider Entity Relationship Effective Date | Key part |
| PRER_TERM_DT | datetime | Provider Entity Relationship Termination Date | End date |
| PRER_PRPR_ID | char(12) | Provider Entity Relationship Identifier | Related provider. For a practitioner, the group (G) is the 837P 2010AA billing provider while the practitioner is the 2310B rendering provider |

#### CMC_PRDS_DATE

- **Functional area:** prov (Provider Date Sensitivity Table)
- **Description (dictionary):** This date sensitive table will allow users to designate a payee for claims or capitation with date sensitivity. This table will be used in the future for additional date sensitive fields.
- **Primary key:** PRPR_ID, PRDS_EFF_DT
- **Join to the denorm grain:** PRPR_ID = provider. The payee's PRPR_ID is the provider itself (P) or the PRER_PRPR_ID of the CMC_PRER_RELATION row whose PRER_PRPR_ENTITY = PRDS_PAY_CL_IND (per the dictionary note).
- **Effective dating:** PRDS_EFF_DT / PRDS_TERM_DT. Payee in force on svc_dt.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PRPR_ID | char(12) | Provider Identifier | Key part |
| PRDS_EFF_DT | datetime | Provider Payee Effective Date | Key part |
| PRDS_TERM_DT | datetime | Provider Payee Termination Date | End date |
| PRDS_PAY_CL_IND | char(1) | Provider Payee - Claims | Claims payee entity (P/G/I): 2010AB pay-to provider; matches CMC_CLCL_CLAIM.CLCL_PAYEE_PR_ID |

#### CMC_PRAF_FAC_AFFIL

- **Functional area:** prov (Provider Relationship With Facility Information)
- **Description (dictionary):** This table stores information on a particular provider's relationship with a facility (i.e. hospital). Please note, this relationship can be created from the facility OR the common practitioner/provider group, IPA applications. From the facility application, the Affiliations section displays all providers affiliated to the facility. From the common practitioner, provider group, or IPA applications, the Affiliations section displays all facilities that the provider has admitting privileges.
- **Primary key:** PRPR_ID, PRCR_ID
- **Join to the denorm grain:** PRPR_ID = the facility; PRCR_ID = CMC_PRPR_PROV.PRCR_ID of the practitioner.
- **Effective dating:** PRAF_FROM_DT / PRAF_TO_DT. Affiliation in force on svc_dt.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PRPR_ID | char(12) | Provider ID (Facility) | Key part; facility |
| PRCR_ID | char(12) | Credentialing ID | Key part; practitioner credentialing ID |
| PRAF_FROM_DT | datetime | Provider Affiliation Effective Date | Affiliation start |
| PRAF_TO_DT | datetime | Provider Affiliation Termination Date | Affiliation end |
| PRAF_STS | char(2) | Provider Affiliation Status | Privilege level |
| PRAF_PRIM_IND | char(1) | Provider Affiliation-Primary Affiliation | Primary affiliation |
| PRAF_MCTR_SPEC | char(4) | Provider Affiliation Specialty | Specialty practiced at the facility. Use: 837I attending/operating (2310A/2310B) drawn from practitioners affiliated with the billing facility |

#### CMC_MEPR_PRIM_PROV

- **Functional area:** mesb (Member's History of PCP Relationships)
- **Description (dictionary):** This table stores the history of each member's selected primary care provider (PCP) relationships, including the effective and term dates of the member's relationship with the provider, the PCP type (Medical Primary (MP), Medical Gynecologist (MG), Medical Vision (MV), etc. and provider ID.
- **Primary key:** MEME_CK, MEPR_PCP_TYPE, MEPR_EFF_DT
- **Join to the denorm grain:** MEME_CK = member_denorm.MEME_CK; PRPR_ID = PCP (CMC_PRPR_PROV).
- **Effective dating:** MEPR_EFF_DT / MEPR_TERM_DT. PCP in force on svc_dt, per MEPR_PCP_TYPE (MP medical primary, MG gynecologist, MV vision ...).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| MEME_CK | int | Member Contrived Key | Key part |
| MEPR_PCP_TYPE | char(2) | Primary Care Provider Type | Key part; PCP type |
| MEPR_EFF_DT | datetime | Primary Care Provider Effective Date | Key part |
| MEPR_TERM_DT | datetime | Primary Care Provider Termination Date | End date |
| PRPR_ID | char(12) | Provider Identifier | PCP: claims_denorm.CLCL_PRPR_ID_PCP; typical 837P 2310A referring provider (NM1*DN) on HMO referral claims |
| GRGR_CK | int | Group Contrived Key | Group |

#### CMC_MCTR_CD_TRANS

- **Functional area:** mcar (User Defined Code Trans lations)
- **Description (dictionary):** This table stores the code translation descriptions, as defined, for the user defined codes throughout the system.
- **Primary key:** MCTR_ENTITY, MCTR_TYPE, MCTR_VALUE
- **Join to the denorm grain:** MCTR_ENTITY + MCTR_TYPE + MCTR_VALUE = any user-defined code (specialty, provider type, network type...).
- **Effective dating:** Not effective-dated. Lookup only; not extracted.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| MCTR_ENTITY | char(4) | User Defined Codes Entity | Key part |
| MCTR_TYPE | char(4) | User Defined Code Type | Key part |
| MCTR_VALUE | char(4) | User Defined Code | Key part |
| MCTR_DESC | varchar(255) | User Defined Code Description | Description |


### Member

#### CMC_SBSB_SUBSC

- **Functional area:** mesb (Subscriber Indicative Data)
- **Description (dictionary):** This table stores indicative information on the subscriber such as name, original effective date, employee ID, hire date, and family coverage. For a subscriber's address information, you can link Subscriber_CKEY and the desired address indicator (Home_Address or Mailing Address) to Subscriber_CKEY and Address_Type in Table View CMCV_SUB_ADDRESS.
- **Primary key:** SBSB_CK
- **Join to the denorm grain:** SBSB_CK = member_denorm.SBSB_CK.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| SBSB_CK | int | Subscriber Contrived Key | Key |
| GRGR_CK | int | Group Contrived Key | Group |
| SBSB_ID | char(9) | Subscriber Identifier | 2010BA NM109 member ID (SBSB_ID + MEME_SFX per the tool's convention) |
| SBSB_LAST_NAME | char(35) | Subscriber Last Name | 2010BA NM103 |
| SBSB_FIRST_NAME | char(15) | Subscriber First Name | 2010BA NM104 |
| SBSB_MID_INIT | char(1) | Subscriber Middle Initial | 2010BA NM105 |
| SBAD_TYPE_HOME | char(1) | Subscriber Home Address | Subscriber home address type (CMC_SBAD_ADDR.SBAD_TYPE) |
| SBAD_TYPE_MAIL | char(1) | Subscriber Mailing Address | Subscriber mailing address type |

#### CMC_MEME_MEMBER

- **Functional area:** mesb (Member Specific Indicative Data)
- **Description (dictionary):** This table stores indicative information on the member including name, relationship, suffix, and original effective date. For a member's address information, you can link Subscriber _CKEY and the desired address indicator (Home_Address or Mailing_Address) to Subscriber _CKEY and Address_Type in Table View CMCV_SUB_ADDRESS.
- **Primary key:** MEME_CK
- **Join to the denorm grain:** MEME_CK = member_denorm.MEME_CK; SBSB_CK = CMC_SBAD_ADDR.SBSB_CK.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| MEME_CK | int | Member Contrived Key | Key |
| SBSB_CK | int | Subscriber Contrived Key | Subscriber |
| MEME_REL | char(1) | Relationship | M = subscriber (system-assigned). M: patient is the subscriber, 2000B SBR02 = 18 and no 2000C loop; otherwise 2000C PAT01 from the relationship |
| SBAD_TYPE_HOME | char(1) | Subscriber Home Address | Member home address type; blank means the subscriber's home address (dictionary edit criteria) |
| SBAD_TYPE_MAIL | char(1) | Subscriber Mailing Address | Member mailing address type; blank means the subscriber's home address |
| MEME_HICN | char(12) | Medicare Number | Medicare number (Medicare secondary / crossover scenarios) |
| MEME_MEDCD_NO | char(20) | Medicaid Number | Medicaid number |

#### CMC_SBAD_ADDR

- **Functional area:** mesb (Subscriber/Member Address/ Phone/Fax)
- **Description (dictionary):** This table stores address and phone/fax information for each subscriber and member. Multiple addresses, such as home and mailing, can be identified by the Address type.
- **Primary key:** SBSB_CK, SBAD_TYPE
- **Join to the denorm grain:** SBSB_CK = member_denorm.SBSB_CK and SBAD_TYPE = CMC_MEME_MEMBER.SBAD_TYPE_HOME (or CMC_SBSB_SUBSC.SBAD_TYPE_HOME when the member's is blank).
- **Effective dating:** Not effective-dated: the current address only (history is not in this table).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| SBSB_CK | int | Subscriber Contrived Key | Key part |
| SBAD_TYPE | char(1) | Subscriber Address Type | Key part; address type |
| SBAD_ADDR1 | char(40) | Subscriber Address Line 1 | N301 (2010BA subscriber, 2010CA patient) |
| SBAD_ADDR2 | char(40) | Subscriber Address Line 2 | N302 |
| SBAD_CITY | char(19) | City | N401 |
| SBAD_STATE | char(2) | State | N402 |
| SBAD_ZIP | char(11) | Zip Code | N403 |
| SBAD_CTRY_CD | char(4) | Country | N404 |
| SBAD_COUNTY | char(20) | County | County |
| SBAD_PHONE | char(20) | Subscriber Phone Number | Phone (not an 837P element; used for realism) |

#### CMC_MEPE_PRCS_ELIG

- **Functional area:** mesb (Comprehensive Member Eligibility Status Table)
- **Description (dictionary):** The MEPE table is the major source for all processed eligibility information for all members in Facets. It contains contiguous historical details on each member's eligibility events: Select, Changes, Terminations, etc. MEPE determines eligibility for claims processing, premium billing, capitation, on-line eligibility inquiry and ID card production. In addition, the MEPE table is used extensively in the Facets reporting application for most membership reports.
- **Primary key:** MEME_CK, CSPD_CAT, MEPE_EFF_DT
- **Join to the denorm grain:** MEME_CK = member_denorm.MEME_CK; GRGR_CK + CSCS_ID + CSPD_CAT + CSPI_ID = CMC_CSPI_CS_PLAN; PDPD_ID = CMC_PDPD_PRODUCT.
- **Effective dating:** MEPE_EFF_DT / MEPE_TERM_DT per CSPD_CAT. Eligible on svc_dt: MEPE_EFF_DT <= svc_dt AND (MEPE_TERM_DT IS NULL OR MEPE_TERM_DT >= svc_dt) AND MEPE_ELIG_IND = 'Y' (assumed value).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| MEME_CK | int | Member Contrived Key | Key part |
| CSPD_CAT | char(1) | Class Product Category | Key part; product category (medical, dental...) |
| MEPE_EFF_DT | datetime | Effective Date | Key part |
| MEPE_TERM_DT | datetime | Termination Date | End date |
| MEPE_ELIG_IND | char(1) | Eligibility Indicator | Eligible / not eligible for the span |
| GRGR_CK | int | Group Contrived Key | Group |
| SGSG_CK | int | Subgroup Contrived Key | Subgroup |
| CSCS_ID | char(4) | Class Identifier | Class: the missing key from member_denorm to CMC_CSPI_CS_PLAN and CMC_CSCS_CLASS |
| CSPI_ID | char(8) | Class Plan Identifier | Plan |
| PDPD_ID | char(8) | Product Identifier | Product |
| MEPE_FI | char(1) | Family Indicator | Family indicator |
| MEPE_PLAN_ENTRY_DT | datetime | Plan Entry Date | Plan entry date |

#### CMC_GRGR_GROUP

- **Functional area:** mesb (Group indicative data)
- **Description (dictionary):** This table maintains indicative data for a group.
- **Primary key:** GRGR_CK
- **Join to the denorm grain:** GRGR_CK = member_denorm.GRGR_CK.
- **Effective dating:** Not effective-dated (GRGR_TERM_DT ends the group).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| GRGR_CK | int | Group Contrived Key | Key |
| GRGR_ID | char(8) | Group Identifier | 2000B SBR03 group or policy number |
| GRGR_NAME | char(50) | Group Name | 2000B SBR04 group name (also in claims_denorm) |

#### CMC_MECB_COB

- **Functional area:** mesb (Member COB Information Data)
- **Description (dictionary):** This table stores coordination of benefits information, including the type of additional coverage, carrier ID, policy, and effective and termination dates.
- **Primary key:** MEME_CK, MECB_INSUR_TYPE, MECB_INSUR_ORDER, MECB_MCTR_STYP, MECB_EFF_DT
- **Join to the denorm grain:** MEME_CK = member_denorm.MEME_CK; MCRE_ID = CMC_MCRE_RELAT_ENT.MCRE_ID (the other carrier).
- **Effective dating:** MECB_EFF_DT / MECB_TERM_DT. Other coverage in force on svc_dt; the order of determination says whether this plan is primary or secondary.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| MEME_CK | int | Member Contrived Key | Key part |
| MECB_INSUR_TYPE | char(1) | Insurance Type | Key part; type of other coverage |
| MECB_INSUR_ORDER | char(1) | Insurance Order | Key part; order of determination: 2000B SBR01 (this payer) and 2320 SBR01 (other payer) P/S/T |
| MECB_MCTR_STYP | char(4) | Supplemental Drug Type | Key part; supplemental drug type |
| MECB_EFF_DT | datetime | Coordination of Benefits Effective Date | Key part |
| MECB_TERM_DT | datetime | Termination Date | End date |
| MCRE_ID | char(9) | Coordination of Benefits Carrier Identifier | Other carrier: 2330B NM109 (as a proprietary payer ID, NM108 = PI; see gaps) |
| MECB_POLICY_ID | varchar(25) | Policy Identifier | 2320 SBR03 / 2330A NM109 other subscriber's policy ID |
| MECB_PRIM_LAST_NM | varchar(35) | Last Name of Primary COB holder | 2330A NM103 other insured last name |
| MECB_PRIM_FIRST_NM | varchar(15) | First Name of Primary COB holder | 2330A NM104 |
| MECB_PRIM_ID | varchar(40) | ID of Primary COB holder | 2330A NM109 other insured ID |
| MECB_MCTR_MSP | char(4) | Medicare Secondary Payer Type | 2320 SBR05 Medicare secondary payer type |

#### CMC_MCRE_RELAT_ENT

- **Functional area:** mesb (Related Entity Indicative Data)
- **Description (dictionary):** This table stores name, address and phone/fax information associated with each related entity.
- **Primary key:** MCRE_ID
- **Join to the denorm grain:** MCRE_ID = CMC_MECB_COB.MCRE_ID.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| MCRE_ID | char(9) | Related Entity Identifier | Key |
| MCRE_NAME | char(50) | Related Entity Name | 2330B NM103 other payer name |
| MCRE_TYPE | char(2) | Related Entity Type | Related entity type |
| MCRE_ADDR1 | char(40) | Related Entity Address Line 1 | 2330B N301 |
| MCRE_ADDR2 | char(40) | Related Entity Address Line 2 | 2330B N302 |
| MCRE_CITY | char(19) | City | 2330B N401 |
| MCRE_STATE | char(2) | State | 2330B N402 |
| MCRE_ZIP | char(11) | Zip Code | 2330B N403 |


### Plan and benefit

#### CMC_CSPI_CS_PLAN

- **Functional area:** plan (Plan/Product Linking Data Table)
- **Description (dictionary):** This table stores information that ties the plan to a product. The product category, group administrative rules, billing rate guarantee information, product ID, and effective and termination dates for the plan are stored here.
- **Primary key:** GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID, CSPI_EFF_DT
- **Join to the denorm grain:** GRGR_CK + CSCS_ID + CSPD_CAT + CSPI_ID = CMC_MEPE_PRCS_ELIG (member_denorm has GRGR_CK, CSPD_CAT, CSPI_ID; CSCS_ID comes from MEPE).
- **Effective dating:** CSPI_EFF_DT / CSPI_TERM_DT. Plan row in force on svc_dt.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| GRGR_CK | int | Class/Plan Group Contrived Key | Key part |
| CSCS_ID | char(4) | Class ID | Key part |
| CSPD_CAT | char(1) | Class/Plan Product Category | Key part |
| CSPI_ID | char(8) | Plan ID | Key part |
| CSPI_EFF_DT | datetime | Class/Plan Effective Date | Key part |
| CSPI_TERM_DT | datetime | Class/Plan Termination Date | End date |
| PDPD_ID | char(8) | Product ID | Product |
| NWST_PFX | char(4) | Network Set Prefix | Network set: which networks (and tiers) the plan uses; join CMC_NWST_NET_SET |
| CSPI_PDPD_BEG_MMDD | smallint | Plan Year Begin Date | Plan year begin (MMDD) for deductible / limit periods |

#### CMC_PLDS_PLAN_DESC

- **Functional area:** plan (Plan Description Table)
- **Description (dictionary):** This table stores the ID code and description assigned to a benefit plan .
- **Primary key:** CSPI_ID
- **Join to the denorm grain:** CSPI_ID = plan.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CSPI_ID | char(8) | Plan ID | Key |
| PLDS_DESC | char(70) | Plan Description | Plan description (also in claims_denorm) |

#### CMC_CSCS_CLASS

- **Functional area:** plan (Class Information Table)
- **Description (dictionary):** This table stores the description of the class and pointers to the type of service conversion table.
- **Primary key:** GRGR_CK, CSCS_ID
- **Join to the denorm grain:** GRGR_CK + CSCS_ID = CMC_MEPE_PRCS_ELIG.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| GRGR_CK | int | Group Contrived Key | Key part |
| CSCS_ID | char(4) | Class ID | Key part |
| TPCT_PFX | char(4) | Class Service/Revenue Conversion Prefix | Service conversion prefix: maps procedures / revenue codes to Service IDs (CMC_TPCT_CPT_CONV, CMC_RCCT_CONV, CMC_SECT_CONV) |

#### CMC_PDPD_PRODUCT

- **Functional area:** plan (Product Indicative Data Table)
- **Description (dictionary):** This table links together a product prefix with a line of business ID. It stores information on the alternate line of business, and the member and family accumulation method for this product.
- **Primary key:** PDPD_ID, PDPD_EFF_DT
- **Join to the denorm grain:** PDPD_ID = member_denorm.PDPD_ID / claims_denorm.PDPD_ID.
- **Effective dating:** PDPD_EFF_DT / PDPD_TERM_DT. Product row in force on svc_dt.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PDPD_ID | char(8) | Product ID | Key part |
| PDPD_EFF_DT | datetime | Product Effective Date | Key part |
| PDPD_TERM_DT | datetime | Termination Date | End date |
| LOBD_ID | char(4) | Product Line of Business | Line of business: payer via CMC_LOBD_LINE_BUS.PYPY_ID |
| LOBD_ALT_RISK_ID | char(4) | Product Alternate Line of Business | Alternate line of business |

#### CMC_PDDS_PROD_DESC

- **Functional area:** plan (Product Descriptve Data Table)
- **Description (dictionary):** This table stores functionality information about a product including type of product (medical or dental claims processing or pre-pricing, clinical editing, capitation) and utilization management indicators. Information regarding claim interest calculations is also stored here, as well as HEDIS related data.
- **Primary key:** PDPD_ID
- **Join to the denorm grain:** PDPD_ID = product.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PDPD_ID | char(8) | Product ID | Key |
| PDDS_DESC | varchar(70) | Product Description | Product description |
| PDDS_PROD_TYPE | char(1) | Product Type | Commercial / Medicare risk / Medicaid: derive 2000B SBR09 claim filing indicator (CI, MB/16, MC...) |
| PDDS_MCTR_BCAT | char(4) | Product Business Category | Business category |
| PDDS_UM_IND | char(1) | Utilization Management Indicator | Product uses referrals / pre-authorizations |
| PDDS_OON_IND | char(1) | Provider Out of Network Submission Acceptance Indicator | Out-of-network submissions accepted |

#### CMC_LOBD_LINE_BUS

- **Functional area:** acct (Line of Business Information Table)
- **Description (dictionary):** This table stores information pertaining to the user's defined line of business.
- **Primary key:** LOBD_ID
- **Join to the denorm grain:** LOBD_ID = CMC_PDPD_PRODUCT.LOBD_ID (= claims_denorm.LOBD_ID).
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| LOBD_ID | char(4) | Line of Business ID | Key |
| LOBD_NAME | char(50) | Line of Business Name | Line of business name |
| LOBD_MCTR_TYPE | char(4) | Line of Business Type | Line of business type |
| PYPY_ID | char(8) | Payor ID | Payor |

#### CMC_PYPY_PAYOR

- **Functional area:** acct (Payor Indicative History Table)
- **Description (dictionary):** This table contains indicative information relating to a payor and includes effective and termination dates as well as bank account and address information.
- **Primary key:** PYPY_ID, PYPY_EFF_DT
- **Join to the denorm grain:** PYPY_ID = CMC_LOBD_LINE_BUS.PYPY_ID.
- **Effective dating:** PYPY_EFF_DT / PYPY_TERM_DT. Payor row in force on svc_dt.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PYPY_ID | char(8) | Payor ID | Key part |
| PYPY_EFF_DT | datetime | Effective Date | Key part |
| PYPY_TERM_DT | datetime | Termination Date | End date |
| PYPY_PAYOR_NAME | char(50) | Payor Name | 2010BB NM103 payer name |
| PYPY_NATL_PLAN_ID | varchar(80) | National Plan ID | 2010BB NM109 (NM108 = XV national plan ID) |
| PYPY_NAIC_CODE | varchar(50) | National Association of Insurance Commissioners Code | 2010BB NM109 (NM108 = PI) or REF*NF |
| PYPY_TAX_ID | char(9) | Payor Tax ID | 2010BB REF*EI |
| PYPY_ADDR1 | char(40) | Address Line One | 2010BB N301 |
| PYPY_ADDR2 | char(40) | Address Line Two | 2010BB N302 |
| PYPY_CITY | char(19) | City | 2010BB N401 |
| PYPY_STATE | char(2) | State Code | 2010BB N402 |
| PYPY_ZIP | char(11) | Zip Code | 2010BB N403 |

#### CMC_NWST_NET_SET

- **Functional area:** plan (Network Set Table)
- **Description (dictionary):** This table ties a group of networks to a benefit component prefix and points to various other prefixes which direct claim adjudication.
- **Primary key:** NWST_PFX, NWST_EFF_DT, NWST_SEQ_NO
- **Join to the denorm grain:** NWST_PFX = CMC_CSPI_CS_PLAN.NWST_PFX; NWNW_ID + NWPR_PFX = CMC_NWPR_RELATION.
- **Effective dating:** NWST_EFF_DT / NWST_TERM_DT.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| NWST_PFX | char(4) | Network Set Benefit Component Prefix | Key part |
| NWST_EFF_DT | datetime | Network Set Effective Date | Key part |
| NWST_SEQ_NO | smallint | Network Set Sequence Number | Key part |
| NWST_TERM_DT | datetime | Network Set Termination Date | End date |
| NWNW_ID | char(12) | Network ID | Network in the plan's set |
| NWPR_PFX | char(4) | Network Provider Relationship Prefix | Provider-relationship prefix to match on CMC_NWPR_RELATION |
| NWST_PR_NW_STS | char(1) | Network Status | I = HMO / in-network, P = PPO / participating (drives claims_denorm.CLCL_NTWK_IND) |
| PDVC_TIER | smallint | Network Tier | Network tier: picks the CMC_PDVC_VAR_COMP row (cost-share tier) |

#### CMC_PDBC_PROD_COMP

- **Functional area:** plan (Product Benefit Component Definition Table)
- **Description (dictionary):** This table ties a product component prefix together with the product ID. The elements stored in this table include the component type and prefix for the designated product.
- **Primary key:** PDPD_ID, PDBC_TYPE, PDBC_EFF_DT
- **Join to the denorm grain:** PDPD_ID = product; PDBC_PFX = component tables (CMC_SEDF_SE_DEFN, CMC_BSBS_SUM / CMC_BSDL_DETAILS, CMC_IPCR_PROC_CRIT, CMC_IPMC_DEFN).
- **Effective dating:** PDBC_EFF_DT / PDBC_TERM_DT per PDBC_TYPE.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PDPD_ID | char(8) | Product ID | Key part |
| PDBC_TYPE | char(4) | Product Component Type | Key part; component type (decoded by CMC_PDPT_DESC) |
| PDBC_EFF_DT | datetime | Product Component Effective Date | Key part |
| PDBC_TERM_DT | datetime | Product Component Termination Date | End date |
| PDBC_PFX | char(4) | Product Component Prefix ID | Component prefix |

#### CMC_PDVC_VAR_COMP

- **Functional area:** plan (Product Variable Component Table)
- **Description (dictionary):** This table allows the user to point to the primary or alternate line of business ID and vary the component prefixes for payment, deductible and limits.
- **Primary key:** PDPD_ID, PDVC_TIER, PDVC_TYPE, PDVC_EFF_DT, PDVC_SEQ_NO
- **Join to the denorm grain:** PDPD_ID = product; PDVC_TIER = CMC_NWST_NET_SET.PDVC_TIER of the provider's network.
- **Effective dating:** PDVC_EFF_DT / PDVC_TERM_DT.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PDPD_ID | char(8) | Product ID | Key part |
| PDVC_TIER | smallint | Product Variable Component Tier | Key part; provider tier |
| PDVC_TYPE | char(1) | Product Variable Component Type | Key part; variable component type |
| PDVC_EFF_DT | datetime | Product Variable Component Effective Date | Key part |
| PDVC_SEQ_NO | smallint | Product Variable Component Sequence Number | Key part |
| PDVC_TERM_DT | datetime | Product Variable Components Termination Date | End date |
| PDVC_PR_PCP | char(1) | Primary Care Provider | Row applies when the servicing provider is the member's PCP |
| PDVC_PR_IN | char(1) | In-Network Provider | ... in-network provider |
| PDVC_PR_PAR | char(1) | Participating Provider | ... participating provider |
| PDVC_PR_NONPAR | char(1) | Non-Participating Provider | ... non-participating provider |
| PDVC_PC_NR | char(1) | Preauthorization Not Required | ... pre-authorization not required |
| PDVC_PC_OBT | char(1) | Preauthorization Obtained | ... pre-authorization obtained (2300/2400 REF*G1 present) |
| PDVC_PC_VIOL | char(1) | Preauthorization Violation | ... pre-authorization violated |
| PDVC_REF_NR | char(1) | Referral Not Required | ... referral not required |
| PDVC_REF_OBT | char(1) | Referral Obtained | ... referral obtained (REF*9F, 2310A referring present) |
| PDVC_REF_VIOL | char(1) | Referral Violation | ... referral violated |
| PDVC_LOBD_PTR | char(1) | Line of Business Indicator | Primary or alternate line of business |
| SEPY_PFX | char(4) | Service Payment Prefix | Service payment prefix: CMC_SEPY_SE_PYMT |
| DEDE_PFX | char(4) | Deductible Prefix | Deductible prefix: CMC_DEDE_DEDUCT |
| LTLT_PFX | char(4) | Limit Prefix | Limit prefix: CMC_LTLT_LIMIT |

#### CMC_SEPY_SE_PYMT

- **Functional area:** plan (Service Payment Table)
- **Description (dictionary):** This table stores information defining and categorizing a particular service payment prefix, and ties types of service to service rules. Service Payment prefixes are tied to products as variable components.
- **Primary key:** SEPY_PFX, SEPY_EFF_DT, SESE_ID
- **Join to the denorm grain:** SEPY_PFX = CMC_PDVC_VAR_COMP.SEPY_PFX (= CMC_CDML_CL_LINE.SEPY_PFX).
- **Effective dating:** SEPY_EFF_DT / SEPY_TERM_DT.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| SEPY_PFX | char(4) | Service Payment Prefix | Key part |
| SEPY_EFF_DT | datetime | Service Payment Effective Date | Key part |
| SESE_ID | char(4) | Service Code ID | Key part; covered Service ID (type of service) |
| SEPY_TERM_DT | datetime | Service Payment Termination Date | End date |
| SESE_RULE | char(3) | Service Rule ID | Service rule (cost-share rule) for the Service ID |

#### CMC_SESE_SERVICE

- **Functional area:** plan (Service Rule Definition Table)
- **Description (dictionary):** This table stores rules for types of service that define whether the service payment rule is amount oriented, counter oriented, or a total disallow. Also stored is acceptable covered member, age and sex criteria.
- **Primary key:** SESE_ID, SESE_RULE
- **Join to the denorm grain:** SESE_ID + SESE_RULE = CMC_SEPY_SE_PYMT.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| SESE_ID | char(4) | Type of Service | Key part |
| SESE_RULE | char(3) | Type of Service Rule | Key part |
| SESE_DESC | char(70) | Service Rule Description | Rule description |
| SESE_RULE_TYPE | char(1) | Service Processing Rule Type | Amount, Counter or Disallow (a Disallow service is not covered) |
| SESE_CALC_IND | char(1) | Calculation Indicator | Order of allowed / deductible / copay / coinsurance calculation |
| SESE_VALID_SEX | char(1) | Valid Gender | Valid gender for the service (match MEME_SEX) |
| SESE_MIN_AGE | smallint | Service Minimum Age | Minimum age |
| SESE_MAX_AGE | smallint | Service Maximum Age | Maximum age |
| SESE_COV_TYPE | char(1) | Service Covered Members | Covered members (subscriber / spouse / dependents) |

#### CMC_SEDS_SE_DESC

- **Functional area:** plan (Service Description Table)
- **Description (dictionary):** This table stores descriptive information for the Service ID and links a service ID to a service type (Surgery, Assistant Surgery, or Anesthesia).
- **Primary key:** SESE_ID
- **Join to the denorm grain:** SESE_ID = Service ID.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| SESE_ID | char(4) | Service ID | Key |
| SEDS_DESC | char(70) | Service Description | Service description |
| SEDS_TYPE | char(1) | Service Type | Surgery / assistant surgery / anesthesia |

#### CMC_SETR_TIER

- **Functional area:** plan (Service Tier Information Table)
- **Description (dictionary):** This table, which is linked to the SESE table, stores information that specifies tiering of copays, deductibles and coinsurance for the defined service rule.
- **Primary key:** SESE_ID, SESE_RULE, SETR_TIER_NO
- **Join to the denorm grain:** SESE_ID + SESE_RULE = CMC_SESE_SERVICE.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| SESE_ID | char(4) | Type of Service | Key part |
| SESE_RULE | char(3) | Service Rule | Key part |
| SETR_TIER_NO | smallint | Service Tier Number | Key part; calculation tier 0-9 |
| SETR_ALLOW_AMT | money | Maximum Allowed | Maximum allowed amount |
| SETR_ALLOW_CTR | smallint | Maximum Counter Allowed | Maximum allowed counter (visits/units) |
| SETR_COPAY_AMT | money | Copayment Amount | Copay: claims_denorm.CDML_COPAY_AMT; 2320/2430 CAS PR-3 on COB claims |
| SETR_COIN_PCT | money | Coinsurance Percent | Coinsurance %: CDML_COINS_AMT; CAS PR-2 |
| ACAC_ACC_NO | smallint | Deductible Accumulator Number | Deductible accumulator (CMC_DEDE_DEDUCT.ACAC_ACC_NO): CDML_DED_AMT; CAS PR-1 |

#### CMC_SEDF_SE_DEFN

- **Functional area:** plan (Service Definition Table)
- **Description (dictionary):** This table links together a product component with a pricing type. The data stored here defines pricing parameters which include definitions for capitated services and risk and withhold indicators, as well as indicators for UM. This table further points to a specified pricing table.
- **Primary key:** PDBC_PFX, SEDF_EFF_DT, SESE_ID
- **Join to the denorm grain:** PDBC_PFX = CMC_PDBC_PROD_COMP.PDBC_PFX for the service-definition component (= CMC_CLCL_CLAIM.PDBC_PFX_SEDF); SESE_ID = Service ID.
- **Effective dating:** SEDF_EFF_DT / SEDF_TERM_DT.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PDBC_PFX | char(4) | Service Definition Prefix | Key part |
| SEDF_EFF_DT | datetime | Service Definition Effective Date | Key part |
| SESE_ID | char(4) | Service Code ID | Key part |
| SEDF_TERM_DT | datetime | Service Definition Termination Date | End date |
| SEDF_UM_REQ_IND | char(1) | Utilization Management Required Indicator | Service needs a referral and/or pre-authorization: then 2300 REF*G1 / REF*9F must be generated |
| SEDF_UM_BP_REF_IND | char(1) | Bypass Referral Requirement | Referral requirement bypassed |
| SEDF_CAP_IND | char(1) | Capitated Service Indicator | Capitated service |

#### CMC_DEDE_DEDUCT

- **Functional area:** plan (Deductible Rules Table)
- **Description (dictionary):** This table stores information on the rules and parameters for applying deductibles within a plan's benefit structure, including the family carryover and stop loss amounts. Deductible Rule benefit component prefixes are linked to the product structure via the PDVC table.
- **Primary key:** DEDE_PFX, ACAC_ACC_NO
- **Join to the denorm grain:** DEDE_PFX = CMC_PDVC_VAR_COMP.DEDE_PFX; ACAC_ACC_NO = CMC_SETR_TIER.ACAC_ACC_NO.
- **Effective dating:** Not effective-dated (the PDVC row is).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| DEDE_PFX | char(4) | Deductible Rules Benefit Component Prefix | Key part |
| ACAC_ACC_NO | smallint | Deductible Rules Accumulator Number | Key part |
| DEDE_DESC | char(70) | Deductible Rules Description | Description |
| DEDE_RULE | smallint | Deductible Rule | Deductible type |
| DEDE_PERIOD_IND | char(1) | Deductible Rules Period Indicator | Accumulation period (with CSPI_PDPD_BEG_MMDD) |
| DEDE_MEME_AMT | money | Deductible Rules Member Amount/Percentage | Member deductible |
| DEDE_FAM_AMT | money | Deductible Rules Family Amount/Percentage | Family deductible |

#### CMC_LTLT_LIMIT

- **Functional area:** plan (Limit Rules Table)
- **Description (dictionary):** This table stores information on a benefit limit accumulator including designations for the stop loss amount and carryover/reinstatement amount. This table works closely with the related diagnosis table for the purpose of establishing related diagnosis categories which, when submitted to claims, dictates benefit limits. The Limit Rules benefit component prefix is linked to a product via the PDVC table.
- **Primary key:** LTLT_PFX, ACAC_ACC_NO
- **Join to the denorm grain:** LTLT_PFX = CMC_PDVC_VAR_COMP.LTLT_PFX.
- **Effective dating:** Not effective-dated (the PDVC row is).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| LTLT_PFX | char(4) | Limit Benefit Component Prefix | Key part |
| ACAC_ACC_NO | smallint | Limit Accumulator Number | Key part |
| LTLT_DESC | varchar(70) | Limit Description | Description |
| LTLT_CAT | char(1) | Limit Category | Limit or stop-loss (out-of-pocket maximum) |
| LTLT_LEVEL | char(1) | Limit Level | Member / family level |
| LTLT_PERIOD_IND | char(1) | Limit Accumulation Period | Accumulation period |
| LTLT_RULE | char(1) | Limit Rule | Processing method |
| LTLT_IX_IND | char(1) | Limit Include/Exclude Indicator | Services include / exclude |
| LTLT_IX_TYPE | char(1) | Limit Include/Exclude Type | Include/exclude by service or diagnosis |
| LTLT_AMT1 | money | Limit/Stoploss Amount/Salary Pct. | Limit amount (or counter) |

#### CMC_LTSE_SE_IX

- **Functional area:** plan (Limit and Accumulator Link to Services Table)
- **Description (dictionary):** This table stores information that identifies the medical services to be included or excluded during limit processing and accumulation. It works closely with the benefit limit accumulator table where the specifics of a limit are defined.
- **Primary key:** LTLT_PFX, ACAC_ACC_NO, SESE_ID
- **Join to the denorm grain:** LTLT_PFX + ACAC_ACC_NO = CMC_LTLT_LIMIT; SESE_ID = Service ID.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| LTLT_PFX | char(4) | Limit Benefit Component Prefix | Key part |
| ACAC_ACC_NO | smallint | Limit Accumulator Number | Key part |
| SESE_ID | char(4) | Service ID | Key part; service counted toward the limit |
| LTSE_WT_CTR | int | Weighted Counter | Weighted counter |

#### CMC_TPCT_CPT_CONV

- **Functional area:** plan (Procedure Code Conversion Table)
- **Description (dictionary):** This table stores information used to convert procedure codes to types of service for adjudicating claims and processing utilization management episodes.
- **Primary key:** TPCT_PFX, TPCT_MCTR_TCAT, TPCT_MCTR_SETG, TPCT_IPCD_LOW, TPCT_MODIFIER
- **Join to the denorm grain:** TPCT_PFX = CMC_CSCS_CLASS.TPCT_PFX; procedure IPCD_ID BETWEEN TPCT_IPCD_LOW AND TPCT_IPCD_HIGH; TPCT_MCTR_SETG = CMC_PSCD_POS_DESC.TPCT_MCTR_SETG of the place of service.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| TPCT_PFX | char(4) | Service Conversion Table(s) Prefix | Key part |
| TPCT_MCTR_TCAT | char(1) | Service Conversion Category | Key part; service conversion category |
| TPCT_MCTR_SETG | char(1) | Procedure Code Conversion Setting | Key part; setting (I = inpatient, O = outpatient, or user-defined such as M = maternity) |
| TPCT_IPCD_LOW | char(5) | Procedure Code (Low) | Key part; procedure range low |
| TPCT_MODIFIER | char(2) | Procedure Modifier | Key part; modifier |
| TPCT_IPCD_HIGH | char(5) | Procedure Code (High) | Procedure range high |
| SESE_ID | char(4) | Service Code Identifier | Service ID the procedure converts to |
| CSPD_CAT | char(1) | Class Product Category | Product category |
| RPTC_RPT_CAT | char(4) | Service Reporting Category | Reporting category |

#### CMC_RCCT_CONV

- **Functional area:** plan (Revenue Code Conversion Table)
- **Description (dictionary):** This table is used to obtain type of service codes for hospital claims. Based on revenue code, type of bill and setting (In or Out), a service is assigned. A category which represents groups of similar revenue codes is available for reporting.
- **Primary key:** TPCT_PFX, TPCT_MCTR_TCAT, RCCT_MCTR_SETG, RCCT_RCRC_ID_LOW
- **Join to the denorm grain:** TPCT_PFX = CMC_CSCS_CLASS.TPCT_PFX; revenue code BETWEEN RCCT_RCRC_ID_LOW AND RCCT_RCRC_ID_HIGH.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| TPCT_PFX | char(4) | Service Conversion Table(s) Prefix | Key part |
| TPCT_MCTR_TCAT | char(1) | Service Conversion Category | Key part |
| RCCT_MCTR_SETG | char(1) | Revenue Code Conversion Setting | Key part; inpatient / outpatient setting (from the type of bill) |
| RCCT_RCRC_ID_LOW | char(4) | Revenue Code Low | Key part; revenue code low |
| RCCT_RCRC_ID_HIGH | char(4) | Revenue Code High | Revenue code high |
| SESE_ID | char(4) | Service Code Identifier | Service ID for the revenue code (837I SV201) |
| CSPD_CAT | char(1) | Class Product Category | Product category |
| RPTC_RPT_CAT | char(4) | Service Reporting Category | Reporting category |

#### CMC_SECT_CONV

- **Functional area:** plan (Service Code Conversion Table)
- **Description (dictionary):** This table contains service codes used for claim processing which are used when the claim does not contain a revenue or procedure code.
- **Primary key:** TPCT_PFX, SESE_ID
- **Join to the denorm grain:** TPCT_PFX = CMC_CSCS_CLASS.TPCT_PFX.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| TPCT_PFX | char(4) | Service Code Conversion Table Prefix | Key part |
| SESE_ID | char(4) | Service ID | Key part; default Service ID when a claim has no procedure or revenue code |
| CSPD_CAT | char(1) | Class Product Category | Product category |
| RPTC_RPT_CAT | char(4) | Service Reporting Category | Reporting category |

#### CMC_PSCD_POS_DESC

- **Functional area:** plan (Place of Service Description Table)
- **Description (dictionary):** This table defines a place of service ID, stores the description of the ID, assigns an inpatient or outpatient indicator, and gives the user the ability to store a synonymous, user defined indicator for definition of a 'category' of services.
- **Primary key:** PSCD_ID
- **Join to the denorm grain:** PSCD_ID = claims_denorm.PSCD_ID (= CMC_CDML_CL_LINE.PSCD_ID).
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PSCD_ID | char(2) | Place of Service Code | Key; 837P CLM05-1 / SV105 place of service |
| PSCD_DESC | char(70) | Place of Service Description | Description |
| PSCD_POS_IND | char(1) | Normative Place of Service Indicator | Normative inpatient / outpatient / dental setting |
| TPCT_MCTR_TCAT | char(1) | Service Conversion Category | Service conversion category for the POS |
| TPCT_MCTR_SETG | char(1) | Place of Service Setting | Setting: joins to CMC_TPCT_CPT_CONV.TPCT_MCTR_SETG, so the POS decides which procedure-to-service rows apply |

#### CMC_BSBS_SUM

- **Functional area:** plan (Benefit Summary Table)
- **Description (dictionary):** This table stores the component prefix ID, type and description of a benefit summary.
- **Primary key:** PDBC_PFX, BSBS_TYPE
- **Join to the denorm grain:** PDBC_PFX = CMC_PDBC_PROD_COMP.PDBC_PFX (benefit summary component).
- **Effective dating:** Not effective-dated. Optional (descriptive).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PDBC_PFX | char(4) | Benefit Summary Benefit Component Prefix | Key part |
| BSBS_TYPE | char(4) | Benefit Summary Type | Key part; benefit type |
| BSBS_DESC | varchar(255) | Benefit Summary Description | Benefit description |

#### CMC_BSDL_DETAILS

- **Functional area:** plan (Benefit Summary Details Table)
- **Description (dictionary):** This table holds user defined copay, deductible, coinsurance, and limit amounts for each user defined benefit type.
- **Primary key:** PDBC_PFX, BSDL_TYPE, BSDL_NTWK_IND, BSDL_EFF_DT
- **Join to the denorm grain:** PDBC_PFX = CMC_BSBS_SUM.PDBC_PFX; BSDL_TYPE = BSBS_TYPE.
- **Effective dating:** BSDL_EFF_DT / BSDL_TERM_DT. Optional: the user-maintained summary of copay / deductible / coinsurance per benefit and network, useful to sanity-check the SEPY/SETR results.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PDBC_PFX | char(4) | Benefit Summary Benefit Component Prefix | Key part |
| BSDL_TYPE | char(4) | Benefit Summary Details Type | Key part |
| BSDL_NTWK_IND | char(1) | Benefit Summary Details Network Indicator | Key part; network indicator |
| BSDL_EFF_DT | datetime | Benefit Summary Details Effective Date | Key part |
| BSDL_TERM_DT | datetime | Benefit Summary Details Termination Date | End date |
| BSDL_COPAY_AMT | money | Benefit Summary Details Copayment Amount | Copay |
| BSDL_DEDE_AMT | money | Benefit Summary Details Deductible Amount | Deductible |
| BSDL_COIN_PCT | money | Benefit Summary Details Coinsurance Percent | Coinsurance % |
| BSDL_LTLT_AMT | money | Benefit Summary Details Limit/Stoploss Amount | Limit amount |
| BSDL_COV_IND | char(1) | Benefit Summary Details Covered Indicator | Covered indicator |
| BSDL_TIER | smallint | Benefit Summary Details Tier | Tier |

#### CMC_IPCR_PROC_CRIT

- **Functional area:** plan (Procedure Edit Criteria Table)
- **Description (dictionary):** This table stores criteria information for procedure codes including age ranges, sex, pre-authorization, second surgical opinion, and place of service parameters. Information stored in this table is used during utilization management processing.
- **Primary key:** PDBC_PFX, IPCD_ID, IPCR_EFF_DT
- **Join to the denorm grain:** PDBC_PFX = CMC_PDBC_PROD_COMP.PDBC_PFX (procedure criteria component); IPCD_ID = procedure.
- **Effective dating:** IPCR_EFF_DT / IPCR_TERM_DT. Optional.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PDBC_PFX | char(4) | Procedure Edit Criteria Benefit Component Prefix | Key part |
| IPCD_ID | char(7) | Procedure Code | Key part |
| IPCR_EFF_DT | datetime | Procedure Edit Criteria Effective Date | Key part |
| IPCR_TERM_DT | datetime | Procedure Edit Criteria Termination Date | End date |
| IPCR_POS | char(1) | Procedure Edit Criteria Place of Service Ind | Valid place of service for the procedure |
| IPCR_SEX | char(1) | Procedure Edit Criteria Gender Edit | Gender edit |
| IPCR_NORM_MIN_AGE | smallint | Procedure Edit Criteria Normal Minimum Age | Normal minimum age |
| IPCR_NORM_MAX_AGE | smallint | Procedure Edit Criteria Normal Maximum Age | Normal maximum age |
| IPCR_PRE_AUTH_IND | char(1) | Procedure Edit Criteria Pre-Authorization Indicator | Procedure normally needs pre-authorization |

#### CMC_IPMC_DEFN

- **Functional area:** plan (Procedure UM Definition)
- **Description (dictionary):** Identifies referral and/or pre-authorization requirements for a procedure code when the benefit component is associated to the member's product. May identify a dollar threshold for the pre-authorization requirement or a specific time period threshold to trigger pre-authorization activity. Suggested use: require pre-authorization for Durable Medical Equipment if needed for more than 90 calendar days when rental price might exceed purchase price.
- **Primary key:** PDBC_PFX, IPCD_LOW, IPMC_EFF_DT
- **Join to the denorm grain:** PDBC_PFX = CMC_PDBC_PROD_COMP.PDBC_PFX; procedure BETWEEN IPCD_LOW AND IPCD_HIGH.
- **Effective dating:** IPMC_EFF_DT / IPMC_TERM_DT. Optional.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| PDBC_PFX | char(4) | Procedure UM Definition Prefix | Key part |
| IPCD_LOW | char(7) | Low Procedure Code | Key part |
| IPMC_EFF_DT | datetime | Procedure UM Def Effective Date | Key part |
| IPMC_TERM_DT | datetime | Procedure UM Def Termination Date | End date |
| IPCD_HIGH | char(7) | Procedure UM Definition High Procedure Code | Procedure high |
| IPMC_UM_IND | char(1) | Referral and/or Pre-auth Indicators | Referral and/or pre-authorization required |
| IPMC_UM_AUTH_AMT | money | Pre-auth Minimum Charge Threshold | Charge threshold for pre-authorization |

#### CMC_IPCD_PROC_CD

- **Functional area:** plan (Procedure Code Description Table)
- **Description (dictionary):** This tables stores information on all valid procedure codes (including those with modifiers) and their corresponding descriptions.
- **Primary key:** IPCD_ID
- **Join to the denorm grain:** IPCD_ID = claims_denorm.IPCD_ID.
- **Effective dating:** IPCD_EFF_DT / IPCD_TERM_DT: a code is valid on svc_dt.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| IPCD_ID | char(7) | Procedure Code | Key; char(7) = 5-character code plus modifier ('including those with modifiers'): SV101-2 / SV202-2 |
| IPCD_EFF_DT | datetime | Procedure Codes Effective Date | Code effective |
| IPCD_TERM_DT | datetime | Procedure Codes Termination Date | Code termination |
| IPCD_DESC | varchar(175) | Procedure Description | Description |
| IPCD_TYPE | char(1) | Procedure Type | Procedure type |

#### CMC_IDCD_DIAG_CD

- **Functional area:** plan (Diagnosis Codes Description Table)
- **Description (dictionary):** This table stores information on all valid diagnosis codes along with the Major Diagnostic Categories (stored in the MDCD table) that each code is assigned.
- **Primary key:** IDCD_ID, IDCD_TYPE
- **Join to the denorm grain:** IDCD_ID = claims_denorm.IDCD_ID_PRIMARY / CMC_CLMD_DIAG.IDCD_ID.
- **Effective dating:** IDCD_EFF_DT / IDCD_TERM_DT: a code is valid on svc_dt.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| IDCD_ID | varchar(10) | Diagnosis Code Identifier | Key part; HI01-2 / HI0n-2 |
| IDCD_TYPE | char(1) | Diagnosis Code Type | Key part; code set (ICD-9 / ICD-10) |
| IDCD_EFF_DT | datetime | Diagnosis Codes Effective Date | Effective |
| IDCD_TERM_DT | datetime | Diagnosis Codes Termination Date | Termination |
| IDCD_DESC | varchar(228) | Diagnosis Code Description | Description |
| MDCD_ID_M | char(2) | Major Diagnostic Category for Males | Major diagnostic category for males (blank = not valid for males) |
| MDCD_ID_F | char(2) | Major Diagnostic Category for Females | Major diagnostic category for females |

#### CMC_RCRC_RC_DESC

- **Functional area:** plan (Revenue Code ID Description Table)
- **Description (dictionary):** This table stores descriptive information for revenue codes.
- **Primary key:** RCRC_ID
- **Join to the denorm grain:** RCRC_ID = claims_denorm.RCRC_ID.
- **Effective dating:** RCRC_EFF_DT / RCRC_TERM_DT.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| RCRC_ID | char(4) | Revenue Code ID | Key; 837I SV201 revenue code |
| RCRC_EFF_DT | datetime | Revenue Codes Effective Date | Effective |
| RCRC_TERM_DT | datetime | Revenue Codes Termination Date | Termination |
| RCRC_DESC | char(70) | Revenue Code Description | Description |

#### CMC_HFCD_FREQ_DESC

- **Functional area:** plan (Hospital Bill Frequency Description Table)
- **Description (dictionary):** This table stores the codes and their corresponding descriptions to identify frequency of a hospital bill.
- **Primary key:** HFCD_ID
- **Join to the denorm grain:** HFCD_ID = CMC_CLHP_HOSP.CLHP_FREQUENCY.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| HFCD_ID | char(1) | Hospital Frequency Code | Key; bill frequency (third digit of type of bill, CLM05-3) |
| HFCD_DESC | char(70) | Hospital Frequency Code Description | Description |


### Claims

#### CMC_CLCL_CLAIM

- **Functional area:** clcl (Claim Level Claim Information Data)
- **Description (dictionary):** This table stores the base claim level information for both Medical and Hospital claims, and is where the Claim ID originates. This table stores indicative information including the member, provider, PCP, and plan in effect, as well as input and received dates and the current status of the claim. The Claim ID allows linkage to other tables including the CMCV_CL_LINE Table View containing detailed service information.
- **Primary key:** CLCL_ID
- **Join to the denorm grain:** CLCL_ID = claims_denorm.CLCL_ID.
- **Effective dating:** Not effective-dated (claim dates are the service dates).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim Identifier | Key |
| PRPR_ID | char(12) | Servicing Provider | Servicing provider: 837P 2310B rendering (practitioner) or 2010AA billing (group/facility) |
| CLCL_PAYEE_PR_ID | char(12) | Payee Provider Identifier | Payee: 2010AB pay-to provider |
| PRAD_TYPE | char(3) | Address Type | Servicing address type: 2310C service facility address (CMC_PRAD_ADDRESS) |
| NWNW_ID | char(12) | Provider Network Identifier | Network the claim adjudicated in |
| AGAG_ID | char(12) | Provider Agreement Identifier | Agreement used |
| NWPR_PFX | char(4) | Network Provider Relationship Prefix | Network-provider prefix used |
| CSCS_ID | char(4) | Class Identifier | Class (completes the CSPI key) |
| CLCL_PA_ACCT_NO | varchar(35) | Patient Account Number | CLM01 patient control number |
| CLCL_PAY_PR_IND | char(1) | Claim Assignment Indicator | CLM08 benefits assignment (assigned to provider) |
| CLCL_REL_INFO_IND | char(1) | Claim Release of Information Indicator | CLM09 release of information |
| CLCL_MED_ASSGN_IND | char(1) | Medicare Assigned Indicator | CLM07 Medicare assignment (no Facets function) |
| CLCL_ACD_IND | char(1) | Claim Accident Indicator | CLM11-1 related causes (accident / employment / other) |
| CLCL_ACD_STATE | char(2) | Auto Accident State | CLM11-4 auto accident state |
| CLCL_ACD_DT | datetime | Claim Accident Date | DTP*439 accident date |
| CLCL_CURR_ILL_DT | datetime | Current Illness Date | 837P DTP*431 onset of current illness |
| CLCL_SIMI_ILL_DT | datetime | Similar Illness Date | CMS-1500 box 15 other date (no single 5010 element) |
| CLCL_UNABL_FROM_DT | datetime | Beginning date unable to work | 837P DTP*360 disability from |
| CLCL_UNABL_TO_DT | datetime | Ending date unable to work | 837P DTP*361 disability to |
| CLCL_RELHP_FROM_DT | datetime | Beginning date of hospitalization | 837P DTP*435 related hospitalization admission |
| CLCL_RELHP_TO_DT | datetime | Ending date of hospitalization | 837P DTP*096 related hospitalization discharge |
| CLCL_OUT_LAB_IND | char(1) | Outside Laboratory Indicator | Outside lab (837P 2400 PS1 purchased service when Y) |
| CLCL_ID_ADJ_FROM | char(12) | Adjusted Claim Identifier | Adjusted (original) claim: CLM05-3 = 7 and REF*F8 payer claim control number |
| CLCL_MECD_RESUB_NO | char(15) | Medicaid Resubmission Number | Medicaid resubmission number: REF*F8 on Medicaid replacements |
| CLCL_OTHER_BN_IND | char(1) | Other Benefits Indicator | COB exists (informational; MECB is authoritative) |
| CLCL_EXT_REF_IND | char(1) | Claim External Referral Indicator | External referral indicator |

#### CMC_CDML_CL_LINE

- **Functional area:** clcl (Claim Line Detail Data)
- **Description (dictionary):** This table stores the detail information for medical claim line items. It serves as the primary table when reporting information on claim line data, including date of service, type and place of service, service rendered and procedure code/modifier as well as the outcome of claims processing such as price used, disallowed and benefit amounts. It is linked to the indicative claim information via the Claim_ID.
- **Primary key:** CLCL_ID, CDML_SEQ_NO
- **Join to the denorm grain:** CLCL_ID + CDML_SEQ_NO = claims_denorm.
- **Effective dating:** Not effective-dated (CDML_FROM_DT / CDML_TO_DT are the line service dates).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim Identifier | Key part |
| CDML_SEQ_NO | smallint | Line-Item Sequence Number | Key part |
| CDML_EXT_LINE_NO | smallint | Line Item 837 Sequence Number | Line item 837 sequence number: LX01 |
| IPCD_ID | char(7) | Procedure Code | SV101-2 / SV202-2 procedure; positions 6-7 hold the first modifier (SV101-3) - assumption, see below |
| CDML_IPCD_MOD2 | char(2) | Second Procedure Modifier | SV101-4 second modifier |
| CDML_IPCD_MOD3 | char(2) | Third Procedure Modifier | SV101-5 third modifier |
| CDML_IPCD_MOD4 | char(2) | Fourth Procedure Modifier | SV101-6 fourth modifier |
| IDCD_ID | varchar(10) | Diagnosis Code | Line diagnosis: SV107-1 pointer target |
| CDML_CLMD_TYPE2 | char(2) | First Additional Diagnosis Pointer | SV107-2 diagnosis pointer (a CLMD_TYPE) |
| CDML_CLMD_TYPE3 | char(2) | Second Additional Diagnosis Pointer | SV107-3 |
| CDML_CLMD_TYPE4 | char(2) | Third Additional Diagnosis Pointer | SV107-4 |
| CDML_CLMD_TYPE5 | char(2) | Fourth Additional Diagnosis Pointer | Additional pointer (837P allows 4) |
| CDML_CLMD_TYPE6 | char(2) | Fifth Additional Diagnosis Pointer | Additional pointer |
| CDML_CLMD_TYPE7 | char(2) | Sixth Additional Diagnosis Pointer | Additional pointer |
| CDML_CLMD_TYPE8 | char(2) | Seventh Additional Diagnosis Pointer | Additional pointer |
| SESE_ID | char(4) | Service Identifier | Service ID the line adjudicated under: the link to plan_benefit_denorm |
| SESE_RULE | char(3) | Service Rule | Service rule |
| SEPY_PFX | char(4) | Product Component Service Payment Prefix | Service payment prefix used |
| DEDE_PFX | char(4) | Product Component Deductible Prefix | Deductible prefix used |
| LTLT_PFX | char(4) | Limit Prefix | Limit prefix used |
| PDVC_LOBD_PTR | char(1) | Line of Business Indicator | Primary / alternate line of business |
| CDML_ROOM_TYPE | char(2) | Room Type | Room type (837I accommodation revenue lines) |
| CDML_HCPCS_AMT | money | HCPCS/Rate | Rate: 837I SV206 unit rate |
| CDML_REF_IND | char(1) | Referral Indicator | Referral indicator |
| CDML_PC_IND | char(1) | Pre-authorization Indicator | Pre-authorization indicator |
| CDML_UMREF_ID | char(9) | Referral Identifier | Referral (UMUM_REF_ID): 2400 REF*9F |
| CDML_REFSV_SEQ_NO | smallint | Line-item Referral Service Sequence Number | Referral service sequence (UMSV_SEQ_NO) |
| CDML_UMAUTH_ID | char(9) | Preauthorization Identifier | Pre-authorization (UMUM_REF_ID): 2400 REF*G1 |
| CDML_AUTHSV_SEQ_NO | smallint | Line-item Preauthorization Service Sequence Number | Pre-authorization service sequence (UMSV_SEQ_NO) |
| CDML_CL_NTWK_IND | char(1) | Related Parameter Network Indicator | Line network indicator |
| CDML_DISALL_EXCD | char(3) | Disallow Explanation Code | Disallow explanation (2430 CAS reason source) |
| PRCF_MCTR_SPEC | char(4) | Line Item Provider Specialty | Line provider specialty |

#### CMC_CLMD_DIAG

- **Functional area:** clcl (Claim Specific Diagnosis Data)
- **Description (dictionary):** This table stores information on the specific diagnosis code(s) submitted for each claim with an indicator for primary, secondary, etc. Multiple diagnoses can exist per claim and are differentiated at the line-item level..
- **Primary key:** CLCL_ID, CLMD_TYPE
- **Join to the denorm grain:** CLCL_ID = claims_denorm.CLCL_ID.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim Identifier | Key part |
| CLMD_TYPE | char(2) | Claim Diagnosis Type | Key part; diagnosis position (primary, admitting, secondary...). claims_denorm only has the primary and admitting |
| IDCD_ID | varchar(10) | Diagnosis Code | HI segments: 837P HI*ABK (principal) / ABF (other); 837I HI*ABK principal, ABJ admitting, ABF other |
| CLMD_POA_IND | char(1) | Present on Admission | 837I HI0n-9 present-on-admission indicator |
| IDCD_ID_SUB | varchar(10) | Submitted Diagnosis Code | Submitted diagnosis code |

#### CMC_CLHP_HOSP

- **Functional area:** clcl (UB04 Specific Hospital Data)
- **Description (dictionary):** This table stores UB04 specific information on hospital based claims. Included are data elements such as the type of facility, billing class, admitting provider, admission type and date, and discharge status and date. All other detail information such as dates of service, type of service and charge/benefit information can be found in the CMCV_CL_LINE Table View.
- **Primary key:** CLCL_ID
- **Join to the denorm grain:** CLCL_ID = claims_denorm.CLCL_ID (hospital claims; CLCL_CL_SUB_TYPE = hospital).
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim Identifier | Key |
| CLHP_FAC_TYPE | char(2) | Facility Type | Type of bill, facility type + classification: CLM05-1 (837I) |
| CLHP_BILL_CLASS | char(1) | Bill Classification | Type of bill classification (setting) |
| CLHP_FREQUENCY | char(1) | Bill Frequency | Type of bill frequency: CLM05-3 |
| CLHP_ADM_TYP | char(2) | Type of Admission/Priority of Admission | CL101 admission type |
| CLHP_ADM_SOURCE | char(1) | Admission Source/Point of Origin for Admission or Visit | CL102 admission source |
| CLHP_DC_STAT | char(2) | Discharge Status | CL103 patient status |
| CLHP_ADM_DT | datetime | Admission Date | DTP*435 admission date |
| CLHP_ADM_HOUR_CD | char(2) | Admission Hour | DTP*435 admission hour |
| CLHP_DC_DT | datetime | Discharge Date | Discharge date |
| CLHP_DC_HOUR_CD | char(2) | Discharge Hour | DTP*096 discharge hour |
| CLHP_STAMENT_FR_DT | datetime | Beginning Date of Billing Statement | DTP*434 statement from |
| CLHP_STAMENT_TO_DT | datetime | Ending Date of Billing Statement | DTP*434 statement to |
| CLHP_MED_REC_NO | char(17) | Medical Record Number | REF*EA medical record number |
| AGRG_ID | char(4) | System generated DRG code | System DRG |
| CLHP_INPUT_AGRG_ID | char(4) | Submitted DRG Code | HI*DR submitted DRG |
| CLHP_PRPR_ID_ADM | char(12) | Admitting Physician's Identifier | Admitting / attending physician: 837I 2310A NM1*71 |
| CLHP_PRPR_AD_NPI | varchar(10) | Admitting Provider NPI | 2310A NM109 |
| CLHP_PRPR_ID_OPER | char(12) | Operating Provider Identifier | Operating physician: 837I 2310B NM1*72 |
| CLHP_PRPR_OP_NPI | varchar(10) | Operating Provider NPI | 2310B NM109 |
| CLHP_PRPR_ID_OTH1 | char(12) | Other Physician's Identifier (1st occurrence) | Other operating physician: 837I 2310C NM1*ZZ |
| CLHP_PRPR_O1_NPI | varchar(10) | Other Provider #1 NPI | 2310C NM109 |
| CLHP_PRPR_ID_OTH2 | char(12) | Other Physician's Identifier (2nd occurrence) | Second other physician |
| CLHP_PRPR_O2_NPI | varchar(10) | Other Provider #2 NPI | NPI |
| CLHP_ICD_QUAL_IND | char(1) | ICD Qualifier | ICD version qualifier (ABK vs BK) |
| CLHP_BIRTH_WGT | smallint | Birth Weight | Value code 54 birth weight |

#### CMC_CLHI_PROC

- **Functional area:** clcl (Type and Actual Procedure Code Data)
- **Description (dictionary):** This table stores the type of procedure code (CPT4, ICD-9) and the actual procedure code used on a hospital claim.
- **Primary key:** CLCL_ID, CLHI_TYPE
- **Join to the denorm grain:** CLCL_ID = claims_denorm.CLCL_ID (hospital claims).
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim Identifier | Key part |
| CLHI_TYPE | char(2) | Procedure Code Type | Key part; principal, secondary... position |
| IPCD_ID | char(7) | Procedure Code | 837I HI*BBR principal procedure / HI*BBQ other procedures (ICD-PCS) |
| CLHI_IP_DT | datetime | Procedure Date | Procedure date (HI0n-4, D8) |

#### CMC_CLHC_COND_CODE

- **Functional area:** clcl (Hospital Specific Condition Code Data)
- **Description (dictionary):** This table stores information which identifies the hospital specific condition code for the hospital claim submission.
- **Primary key:** CLCL_ID, CLHC_SEQ_NO
- **Join to the denorm grain:** CLCL_ID = claims_denorm.CLCL_ID.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim Identifier | Key part |
| CLHC_SEQ_NO | smallint | Specific Line-Item Sequence Number | Key part |
| CLHC_COND_CD | char(2) | Specific Condition Code | 837I HI*BG condition code |

#### CMC_CLHO_OCC_CODE

- **Functional area:** clcl (UB04 Occurrence Code Data)
- **Description (dictionary):** This table stores the UB04 occurrence information including the 2 byte code indicating the specific medical, accident or insurance related occurrence and the from and to date. A sequence number allows multiple entries to exist.
- **Primary key:** CLCL_ID, CLHO_SEQ_NO
- **Join to the denorm grain:** CLCL_ID = claims_denorm.CLCL_ID.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim Identifier | Key part |
| CLHO_SEQ_NO | smallint | UB92 Line-Item Sequence Number | Key part |
| CLHO_OCC_CODE | char(2) | UB92 Occurrence Code | 837I HI*BH occurrence code (HI*BI span when a to-date is present) |
| CLHO_OCC_FROM_DT | datetime | UB92 Occurrence Code From (Starting) Date | Occurrence (span) from |
| CLHO_OCC_TO_DT | datetime | UB92 Occurance Code To (Ending) Date | Occurrence span to |

#### CMC_CLVC_VAL_CODE

- **Functional area:** clcl (Hospital Processing Value Codes)
- **Description (dictionary):** This table is used to store the entry of optional Value Codes associated with Hosptial Claims Processing. These values, when captured, are being stored for informational purposes only.
- **Primary key:** CLCL_ID, CLVC_NUMBER, CLVC_LETTER
- **Join to the denorm grain:** CLCL_ID = claims_denorm.CLCL_ID.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim Identifier | Key part |
| CLVC_NUMBER | char(2) | Hospital Processing Value Code Number | Key part |
| CLVC_LETTER | char(1) | Hospital Processing Value Code Letter | Key part |
| CLVC_CODE | char(2) | Hospital Processing Value Code | 837I HI*BE value code |
| CLVC_AMT | money | Hosptial Processing Value Amount | HI*BE amount |
| CLVC_VALUE | smallint | Hospital Processing Value Units | Value units |

#### CMC_CDSD_SUPP_DATA

- **Functional area:** clcl (Claim Line Item Supplemental Data)
- **Description (dictionary):** This table stores line level data supplemental to the main claim table.
- **Primary key:** CLCL_ID, CDML_SEQ_NO
- **Join to the denorm grain:** CLCL_ID + CDML_SEQ_NO = claims_denorm.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim ID | Key part |
| CDML_SEQ_NO | smallint | Line Item Sequence Number | Key part |
| CDSD_PRPR_ID_REND | varchar(12) | Rendering Provider ID | Line rendering provider: 837P 2420A NM1*82 |
| CDSD_PRPR_RE_NPI | varchar(10) | Rendering Provider NPI | 2420A NM109 |
| CDSD_PRPR_RE_TAX | varchar(9) | Rendering Provider Tax ID | Rendering tax ID |
| CDSD_NDC_CODE | varchar(11) | National Drug Code | 2410 LIN03 NDC (LIN02 = N4) |
| CDSD_NDC_UNITS | varchar(15) | National Drug Code Units | 2410 CTP04 quantity |
| CDSD_NDC_MCTR_TYPE | char(4) | National Drug Code Units Type | 2410 CTP05-1 unit (F2, GR, ME, ML, UN) |
| CDSD_SERV_LOC_ZIP | varchar(11) | Derived Service Location Zip Code | Derived service location ZIP (2310C/2420C N403 consistency) |
| CDSD_MED_AMB_ZIP | varchar(11) | Ambulance Pickup Zip | 2420G ambulance pick-up N403 |

#### CMC_CLSD_SUPP_DATA

- **Functional area:** clcl (Claim Supplemental Data)
- **Description (dictionary):** This table stores claim level data supplemental to the main claim table.
- **Primary key:** CLCL_ID
- **Join to the denorm grain:** CLCL_ID = claims_denorm.CLCL_ID.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim ID | Key |
| CLSD_PRPR_ID_REND_NVL | varchar(12) | Rendering Provider ID | Claim rendering provider: 837P 2310B / 837I 2310D |
| CLSD_PRPR_RE_NPI_NVL | varchar(10) | Rendering Provider NPI | Rendering NPI |
| CLSD_PRPR_ID_IPA | varchar(12) | IPA ID | Member's IPA |

#### CMC_CLMF_MULT_FUNC

- **Functional area:** clcl (Multifunction Table)
- **Description (dictionary):** Table that stores claim data used for multiple functions.
- **Primary key:** CLCL_ID
- **Join to the denorm grain:** CLCL_ID = claims_denorm.CLCL_ID.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim Identifier | Key |
| CLMF_PRPR_ID_FAC | char(12) | Related Facility Identifier | Service facility: 837P 2310C / 837I 2310E NM1*77 |
| CLMF_PRPR_FA_NPI | varchar(10) | Related Facility NPI | Service facility NPI |
| CLMF_PRPR_RF_NPI | varchar(10) | Referring Provider NPI | Referring NPI: 837P 2310A / 837I 2310F NM109 |
| CLMF_PRPR_SE_NPI | varchar(10) | Servicing Provider NPI | Servicing NPI |
| CLMF_INPUT_TXNM_CD | char(12) | Provider Taxonomy Code | Submitted taxonomy: PRV03 |
| CLMF_EXT_REFRAL_NO | varchar(18) | External Referral Number | REF*9F referral number |
| CLMF_EXT_PREATH_NO | varchar(18) | External Preauthorization Number | REF*G1 prior authorization number |
| CLMF_ICD_IND_PROC | char(1) | ICD Version Indicator-processed | ICD version processed |

#### CMC_CLED_EDI_DATA

- **Functional area:** clcl (EDI Trading Partner Data)
- **Description (dictionary):** This table stores the external reference number and the identifier for the EDI Trading Partner for electronically submitted claims.
- **Primary key:** CLCL_ID
- **Join to the denorm grain:** CLCL_ID = claims_denorm.CLCL_ID (electronic claims).
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim Identifier | Key |
| CLED_HIPAA_VERSION | varchar(12) | nan | Version (005010X222A1 / X223A2) |
| CLED_PAYER_CLM_NUM | varchar(50) | nan | Payer claim number |
| CLED_EXT_REF | varchar(50) | EDI External Claim Reference Number | External claim reference (clearinghouse REF*D9) |
| CLED_TRAD_PARTNER | char(15) | EDI Claim Trading Partner | Trading partner: 1000A submitter |

#### CMC_CLCB_CL_COB

- **Functional area:** clcl (Claim Level COB Information)
- **Description (dictionary):** Stores claim level COB information.
- **Primary key:** CLCL_ID
- **Join to the denorm grain:** CLCL_ID = claims_denorm.CLCL_ID.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim Identifier | Key |
| CLCB_COB_TYPE | char(1) | COB Type | COB type (matches CMC_MECB_COB.MECB_INSUR_TYPE) |
| CLCB_COB_AMT | money | Primary Carrier Payment Amount | 2320 AMT*D other payer paid |
| CLCB_COB_ALLOW | money | COB Other Carrier Allowable Amount | Other carrier allowed |
| CLCB_COB_DED_AMT | money | Other Carrier Deductible Amount | 2320 CAS PR-1 |
| CLCB_COB_COPAY_AMT | money | Other Carrier Copay | 2320 CAS PR-3 |
| CLCB_COB_COINS_AMT | money | Other Carrier Coinsurance Amount | 2320 CAS PR-2 |
| CLCB_COB_REAS_CD | char(4) | Other Carrier Reason Code | Other carrier reason code |

#### CMC_CDCB_LI_COB

- **Functional area:** clcl (COB Data Line Item)
- **Description (dictionary):** This table stores information on the Coordination of Benefits (COB) at the claim line item level, including the amount, savings, and type of coordination that exists with each line item on the claim.
- **Primary key:** CLCL_ID, CDML_SEQ_NO
- **Join to the denorm grain:** CLCL_ID + CDML_SEQ_NO = claims_denorm.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim Identifier | Key part |
| CDML_SEQ_NO | smallint | Medical Line-Item Sequence Number | Key part |
| CDCB_COB_TYPE | char(1) | COB/Medicare Type | COB type |
| CDCB_COB_AMT | money | COB/Medicare Paid Amount | 2430 SVD02 other payer line paid |
| CDCB_COB_ALLOW | money | COB/Medicare (Higher/Lower) Allowable Amount | Other payer allowed |
| CDCB_COB_DED_AMT | money | Other Carrier Deductible Amount | 2430 CAS PR-1 |
| CDCB_COB_COPAY_AMT | money | Other Carrier Copay | 2430 CAS PR-3 |
| CDCB_COB_COINS_AMT | money | Other Carrier Coinsurance Amount | 2430 CAS PR-2 |
| CDCB_COB_REAS_CD | char(4) | Other Carrier Reason Code | Other carrier reason code |

#### CMC_CLPE_PROV_DATA

- **Functional area:** clcl (Claim Level Input Provider Data)
- **Description (dictionary):** This table stores originally input provider data.
- **Primary key:** CLCL_ID, CLPE_PRPR_TYPE
- **Join to the denorm grain:** CLCL_ID = claims_denorm.CLCL_ID.
- **Effective dating:** Not effective-dated. Optional: provider data as submitted (not extracted; CLPE_PRPR_TYPE values are not defined in the dictionary).

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| CLCL_ID | char(12) | Claim ID | Key part |
| CLPE_PRPR_TYPE | char(2) | Provider Type | Key part; which provider role |
| CLPE_NPI | varchar(10) | Input NPI | Submitted NPI |
| CLPE_TAX_ID | varchar(9) | Input Tax ID | Submitted tax ID |
| CLPE_ORG_NAME | varchar(50) | Organization Name | Submitted organization name |
| CLPE_TAXONOMY_CD_NVL | char(12) | Taxonomy Code | Submitted taxonomy |

#### CMC_UMUM_UTIL_MGT

- **Functional area:** util (UM Review Indicative Information)
- **Description (dictionary):** This table stores the base information for utilization management processing and is where the Reference_ID is created. It stores member data, initial diagnosis, accident data, Co-ordination of Benefits (COB) indicator, user-defined quality and risk data, and links to a Case Management episode or another Reference_ID. There is only one base record per Reference ID.
- **Primary key:** UMUM_REF_ID
- **Join to the denorm grain:** UMUM_REF_ID = CMC_CDML_CL_LINE.CDML_UMAUTH_ID / CDML_UMREF_ID; MEME_CK = member.
- **Effective dating:** Not effective-dated.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| UMUM_REF_ID | char(9) | Reference ID | Key; the authorization / referral number (REF*G1 / REF*9F) |
| MEME_CK | int | Member Contrived Key | Member |
| IDCD_ID | varchar(10) | (Primary) Diagnosis | Primary diagnosis |
| CSPI_ID | char(8) | Plan ID | Plan |
| PDPD_ID | char(8) | Product ID | Product |

#### CMC_UMSV_SERVICES

- **Functional area:** util (Pre-Authorization/Referral Services)
- **Description (dictionary):** This table stores data related to the service or procedure requested for pre-authorization or referral. Data includes: requesting, service and facility providers, service from and to dates, place of service, requested and authorized units and next review date.
- **Primary key:** UMUM_REF_ID, UMSV_SEQ_NO
- **Join to the denorm grain:** UMUM_REF_ID + UMSV_SEQ_NO = CDML_UMAUTH_ID + CDML_AUTHSV_SEQ_NO (or CDML_UMREF_ID + CDML_REFSV_SEQ_NO).
- **Effective dating:** UMSV_FROM_DT / UMSV_TO_DT: the authorized service window that the claim line's dates must fall within.

| Column | Type | Dictionary name | 837 loop / segment / use |
|---|---|---|---|
| UMUM_REF_ID | char(9) | Reference ID | Key part |
| UMSV_SEQ_NO | smallint | Sequence Number | Key part |
| UMSV_AUTH_IND | char(1) | Pre-authorization Indicator | Pre-authorization |
| UMSV_REF_IND | char(1) | Referral Indicator | Referral |
| UMSV_FROM_DT | datetime | From or Low Service Date | Authorized from |
| UMSV_TO_DT | datetime | To or High Service Date | Authorized to |
| UMSV_PRPR_ID_REQ | char(12) | Requesting or Referring Provider ID | Requesting / referring provider: 2310A referring |
| UMSV_PRPR_ID_SVC | char(12) | Service Provider ID | Service provider: must equal the rendering / billing provider |
| UMSV_PRPR_ID_FAC | char(12) | Facility ID | Facility: service facility |
| UMSV_PSCD_ID_AUTH | char(2) | Authorized Place of Service Code | Authorized place of service |
| IPCD_ID | char(7) | Procedure | Authorized procedure |
| SESE_ID | char(4) | Service ID (Type of Service or TOS) | Authorized Service ID |
| UMSV_UNITS_AUTH | smallint | Authorized Units | Authorized units |
| UMVT_STS | char(2) | Current and Last Service Status Code | Service status |
| UMSV_AUTH_DT | datetime | Authorization (Approval) Date | Approval date |

## 5. New sdm tables

Column types are FACETS's (from the dictionary) unless the column is derived. All columns are NULL except the key columns.

### Provider

#### sdm.provider_denorm

- **Grain:** One row per provider (PRPR_ID): the non-dated facts plus names, NPI, taxonomy and tax ID. Addresses, networks and relationships are dated and live in their own tables.
- **Primary key (clustered):** PRPR_ID
- **Index ix_provider_denorm_npi:** (PRPR_NPI) INCLUDE (PRPR_ID, PRPR_ENTITY)
- **Index ix_provider_denorm_spec:** (PRPR_ENTITY, PRCF_MCTR_SPEC) INCLUDE (PRPR_ID)

| Column | Type | Source | 837 / use |
|---|---|---|---|
| PRPR_ID | char(12) | CMC_PRPR_PROV.PRPR_ID | Key. Optional REF*G2 (payer-assigned provider ID) in 2010AA/2310x/2420x |
| PRPR_ENTITY | char(1) | CMC_PRPR_PROV.PRPR_ENTITY | Practitioner vs facility: P = practitioner, G = provider group, I = IPA, F = facility (values from the dictionary's edit criteria). Drives NM102 (1 = person for P; 2 = non-person for G/I/F) |
| NM1_ENTITY_TYPE | char(1) | derived | '1' (person) when PRPR_ENTITY = 'P', else '2': 837 NM102 |
| PRPR_NAME | char(55) | CMC_PRPR_PROV.PRPR_NAME | NM103 organization name for G/I/F (2010AA, 2310C/2310E service facility, 2010AB) |
| PRCP_ID | char(12) | CMC_PRPR_PROV.PRCP_ID | Join to CMC_PRCP_COMM_PRAC for a practitioner's last/first name |
| PRCP_LAST_NAME | char(35) | CMC_PRCP_COMM_PRAC.PRCP_LAST_NAME | NM103 last name for a person (2010AA when billing as an individual, 2310A, 2310B, 2420A; 837I 2310A/2310B) |
| PRCP_FIRST_NAME | char(15) | CMC_PRCP_COMM_PRAC.PRCP_FIRST_NAME | NM104 first name |
| PRCP_MID_INIT | char(1) | CMC_PRCP_COMM_PRAC.PRCP_MID_INIT | NM105 middle initial |
| PRCP_TITLE | char(10) | CMC_PRCP_COMM_PRAC.PRCP_TITLE | Credential (MD, RN...). Not an 837 element |
| PRFA_NAME | char(50) | CMC_PRFA_FACILITY.PRFA_NAME | Facility name: NM103 for 837I 2010AA billing facility and 2310E / 837P 2310C service facility |
| PRPR_NPI | char(10) | CMC_PRPR_PROV.PRPR_NPI | NM108 = XX, NM109 NPI (2010AA billing, 2310A referring, 2310B rendering, 2310C service facility, 2420A line rendering; 837I 2310A attending / 2310B operating) |
| PRPR_TAXONOMY_CD | char(12) | CMC_PRPR_PROV.PRPR_TAXONOMY_CD | PRV03 taxonomy (2000A PRV*BI*PXC billing, 2310B PRV*PE*PXC rendering, 837I 2310A PRV*AT attending). Column is char(12); taxonomy codes are 10 characters |
| PRPR_MCTR_TYPE | char(4) | CMC_PRPR_PROV.PRPR_MCTR_TYPE | Provider type (hospital, lab, MD...). Decoded in CMC_MCTR_CD_TRANS: MCTR_ENTITY = FAC (facility) or PRAC, MCTR_TYPE = TYPE |
| PRCF_MCTR_SPEC | char(4) | CMC_PRPR_PROV.PRCF_MCTR_SPEC | Primary specialty; keeps generated procedures consistent with the provider (claims_denorm.PRPR_MCTR_SPEC) |
| PRCF_MCTR_SPEC2 | char(4) | CMC_PRPR_PROV.PRCF_MCTR_SPEC2 | Secondary specialty |
| PRPR_MCTR_PRTY | char(4) | CMC_PRPR_PROV.PRPR_MCTR_PRTY | Practice type (sole proprietor, corporation...) |
| TPCT_MCTR_TCAT | char(1) | CMC_PRPR_PROV.TPCT_MCTR_TCAT | Provider's service conversion category; selects the matching CMC_TPCT_CPT_CONV / CMC_RCCT_CONV rows |
| PRAD_ID | char(12) | CMC_PRPR_PROV.PRAD_ID | Address owner: the provider's own PRPR_ID, or the group/IPA whose address it shares; join to CMC_PRAD_ADDRESS.PRAD_ID |
| PRAD_TYPE_PRIM | char(3) | CMC_PRPR_PROV.PRAD_TYPE_PRIM | Primary practice address type: 2010AA N3/N4 and 2310C/2310E service facility N3/N4 |
| PRAD_TYPE_CHECK | char(3) | CMC_PRPR_PROV.PRAD_TYPE_CHECK | Remittance address type: 2010AB pay-to N3/N4 (Facets defaults it to PRAD_TYPE_PRIM when blank) |
| MCTN_ID | char(9) | CMC_PRPR_PROV.MCTN_ID | Tax ID: 2010AA REF02 (REF01 = EI or SY from CMC_MCTN_TAX_NAME.MCTN_TYPE) |
| MCTN_TYPE | char(1) | CMC_MCTN_TAX_NAME.MCTN_TYPE | EIN or SSN: 2010AA REF01 = EI (EIN) or SY (SSN) |
| MCTN_NAME | char(55) | CMC_MCTN_TAX_NAME.MCTN_NAME | Corporate tax name (W-9 name); not sent on the 837 |
| PRPR_STS | char(2) | CMC_PRPR_PROV.PRPR_STS | Provider status (Proxy / Mock PCP are the only functional values) |
| PRPR_TERM_DT | datetime | CMC_PRPR_PROV.PRPR_TERM_DT | Provider termination date (service-date filter) |
| PRPR_PREAUTH_IND | char(1) | CMC_PRPR_PROV.PRPR_PREAUTH_IND | Provider pre-authorization requirement |
| PRCR_ID | char(12) | CMC_PRPR_PROV.PRCR_ID | Credentialing ID; joins a practitioner to CMC_PRAF_FAC_AFFIL (facility affiliations) |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.provider_denorm
-- One row per provider. Person names come from the common practitioner
-- (PRPR_ENTITY = 'P'); facility name from PRFA. Tax ID type (EI / SY) from
-- MCTN; where one MCTN_ID has rows of both types, the one the provider's
-- CMC_MCTI_TAX_INFO row names wins.
TRUNCATE TABLE sdm.provider_denorm;
INSERT INTO sdm.provider_denorm WITH (TABLOCK)
    (PRPR_ID, PRPR_ENTITY, NM1_ENTITY_TYPE, PRPR_NAME, PRCP_ID, PRCP_LAST_NAME, PRCP_FIRST_NAME, PRCP_MID_INIT,
     PRCP_TITLE, PRFA_NAME, PRPR_NPI, PRPR_TAXONOMY_CD, PRPR_MCTR_TYPE, PRCF_MCTR_SPEC, PRCF_MCTR_SPEC2,
     PRPR_MCTR_PRTY, TPCT_MCTR_TCAT, PRAD_ID, PRAD_TYPE_PRIM, PRAD_TYPE_CHECK, MCTN_ID, MCTN_TYPE, MCTN_NAME,
     PRPR_STS, PRPR_TERM_DT, PRPR_PREAUTH_IND, PRCR_ID)
SELECT p.PRPR_ID, p.PRPR_ENTITY, CASE WHEN p.PRPR_ENTITY = 'P' THEN '1' ELSE '2' END, p.PRPR_NAME,
       p.PRCP_ID, cp.PRCP_LAST_NAME, cp.PRCP_FIRST_NAME, cp.PRCP_MID_INIT,
       cp.PRCP_TITLE, fa.PRFA_NAME, p.PRPR_NPI, p.PRPR_TAXONOMY_CD, p.PRPR_MCTR_TYPE, p.PRCF_MCTR_SPEC, p.PRCF_MCTR_SPEC2,
       p.PRPR_MCTR_PRTY, p.TPCT_MCTR_TCAT, p.PRAD_ID, p.PRAD_TYPE_PRIM, p.PRAD_TYPE_CHECK, p.MCTN_ID, tn.MCTN_TYPE, tn.MCTN_NAME,
       p.PRPR_STS, p.PRPR_TERM_DT, p.PRPR_PREAUTH_IND, p.PRCR_ID
FROM dbo.CMC_PRPR_PROV p
JOIN #scope_prov k ON k.PRPR_ID = p.PRPR_ID
LEFT JOIN dbo.CMC_PRCP_COMM_PRAC cp ON cp.PRCP_ID = p.PRCP_ID AND p.PRPR_ENTITY = 'P'
LEFT JOIN dbo.CMC_PRFA_FACILITY fa ON fa.PRPR_ID = p.PRPR_ID
OUTER APPLY (SELECT TOP 1 t.MCTN_TYPE, t.MCTN_NAME
             FROM dbo.CMC_MCTN_TAX_NAME t
             WHERE t.MCTN_ID = p.MCTN_ID
             ORDER BY CASE WHEN EXISTS (SELECT 1 FROM dbo.CMC_MCTI_TAX_INFO ti
                                        WHERE ti.MCTI_ENTY_ID = p.PRPR_ID
                                          AND ti.MCTN_ID = t.MCTN_ID AND ti.MCTN_TYPE = t.MCTN_TYPE)
                           THEN 0 ELSE 1 END,
                      t.MCTN_TYPE) tn;
```

#### sdm.provider_address

- **Grain:** One row per provider x address type x effective date (PRPR_ID, PRAD_TYPE, PRAD_EFF_DT). Shared addresses (PRAD_ID of a group) are copied onto each provider that uses them, so a lookup never needs PRAD_ID.
- **Primary key (clustered):** PRPR_ID, PRAD_TYPE, PRAD_EFF_DT
- **Index ix_provider_address_role:** (PRPR_ID, IS_PRIMARY, IS_REMIT, PRAD_EFF_DT) INCLUDE (PRAD_TERM_DT, PRAD_ZIP, PRAD_STATE)

| Column | Type | Source | 837 / use |
|---|---|---|---|
| PRPR_ID | char(12) | CMC_PRPR_PROV.PRPR_ID | Key. Optional REF*G2 (payer-assigned provider ID) in 2010AA/2310x/2420x |
| PRAD_ID | char(12) | CMC_PRAD_ADDRESS.PRAD_ID | Key part |
| PRAD_TYPE | char(3) | CMC_PRAD_ADDRESS.PRAD_TYPE | Key part; user-defined address type |
| PRAD_EFF_DT | datetime | CMC_PRAD_ADDRESS.PRAD_EFF_DT | Key part; effective date |
| PRAD_TERM_DT | datetime | CMC_PRAD_ADDRESS.PRAD_TERM_DT | Termination date |
| IS_PRIMARY | char(1) | derived | 'Y' when PRAD_TYPE = PRPR.PRAD_TYPE_PRIM: 2010AA / 2310C address |
| IS_REMIT | char(1) | derived | 'Y' when PRAD_TYPE = COALESCE(PRPR.PRAD_TYPE_CHECK, PRAD_TYPE_PRIM): 2010AB pay-to address |
| PRAD_PRACTICE_IND | char(1) | CMC_PRAD_ADDRESS.PRAD_PRACTICE_IND | Y = a service location; only practice addresses may be a service facility / 2010AA physical address |
| PRAD_TYPE_MAIL | char(3) | CMC_PRAD_ADDRESS.PRAD_TYPE_MAIL | Mailing address type paired with this practice location |
| PRAD_ADDR1 | char(40) | CMC_PRAD_ADDRESS.PRAD_ADDR1 | N301 (2010AA, 2010AB, 2310C/2310E, 2420C) |
| PRAD_ADDR2 | char(40) | CMC_PRAD_ADDRESS.PRAD_ADDR2 | N302 |
| PRAD_CITY | char(19) | CMC_PRAD_ADDRESS.PRAD_CITY | N401 |
| PRAD_STATE | char(2) | CMC_PRAD_ADDRESS.PRAD_STATE | N402 |
| PRAD_ZIP | char(11) | CMC_PRAD_ADDRESS.PRAD_ZIP | N403 (2010AA needs ZIP+4) |
| PRAD_COUNTY | char(20) | CMC_PRAD_ADDRESS.PRAD_COUNTY | County (geography consistency with the member) |
| PRAD_CTRY_CD | char(4) | CMC_PRAD_ADDRESS.PRAD_CTRY_CD | N404 |
| PRAD_PHONE | char(20) | CMC_PRAD_ADDRESS.PRAD_PHONE | PER04 billing provider contact (TE) |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.provider_address
-- Every dated address of each provider's address owner (PRAD_ID), flagged as
-- primary practice (2010AA, 2310C) and remittance (2010AB; Facets defaults the
-- remit type to the primary). Address on svc_dt: PRAD_EFF_DT <= svc_dt AND
-- ISNULL(PRAD_TERM_DT, '99991231') >= svc_dt.
TRUNCATE TABLE sdm.provider_address;
INSERT INTO sdm.provider_address WITH (TABLOCK)
    (PRPR_ID, PRAD_ID, PRAD_TYPE, PRAD_EFF_DT, PRAD_TERM_DT, IS_PRIMARY, IS_REMIT, PRAD_PRACTICE_IND, PRAD_TYPE_MAIL,
     PRAD_ADDR1, PRAD_ADDR2, PRAD_CITY, PRAD_STATE, PRAD_ZIP, PRAD_COUNTY, PRAD_CTRY_CD, PRAD_PHONE)
SELECT p.PRPR_ID, a.PRAD_ID, a.PRAD_TYPE, a.PRAD_EFF_DT, a.PRAD_TERM_DT,
       CASE WHEN a.PRAD_TYPE = p.PRAD_TYPE_PRIM THEN 'Y' ELSE 'N' END,
       CASE WHEN a.PRAD_TYPE = COALESCE(NULLIF(RTRIM(p.PRAD_TYPE_CHECK), ''), p.PRAD_TYPE_PRIM) THEN 'Y' ELSE 'N' END,
       a.PRAD_PRACTICE_IND, a.PRAD_TYPE_MAIL,
       a.PRAD_ADDR1, a.PRAD_ADDR2, a.PRAD_CITY, a.PRAD_STATE, a.PRAD_ZIP, a.PRAD_COUNTY, a.PRAD_CTRY_CD, a.PRAD_PHONE
FROM dbo.CMC_PRPR_PROV p
JOIN #scope_prov k ON k.PRPR_ID = p.PRPR_ID
JOIN dbo.CMC_PRAD_ADDRESS a ON a.PRAD_ID = p.PRAD_ID;
```

#### sdm.provider_network

- **Grain:** One row per provider network participation (PRPR_ID, NWNW_ID, NWPR_PFX, NWPR_PCP_IND, NWPR_EFF_DT), with the network and the agreement (the latest agreement row overlapping the participation dates).
- **Primary key (clustered):** PRPR_ID, NWNW_ID, NWPR_PFX, NWPR_PCP_IND, NWPR_EFF_DT
- **Index ix_provider_network_net:** (NWNW_ID, NWPR_PFX, NWPR_EFF_DT) INCLUDE (PRPR_ID, NWPR_TERM_DT, NWPR_PCP_IND, AGAG_ID)

| Column | Type | Source | 837 / use |
|---|---|---|---|
| PRPR_ID | char(12) | CMC_NWPR_RELATION.PRPR_ID | Key part |
| NWNW_ID | char(12) | CMC_NWPR_RELATION.NWNW_ID | Key part; network |
| NWPR_PFX | char(4) | CMC_NWPR_RELATION.NWPR_PFX | Key part; product-component prefix linking provider + agreement to products |
| NWPR_PCP_IND | char(1) | CMC_NWPR_RELATION.NWPR_PCP_IND | Key part; provider is a PCP in this network (eligible for CMC_MEPR_PRIM_PROV / claims_denorm.CLCL_PRPR_ID_PCP) |
| NWPR_EFF_DT | datetime | CMC_NWPR_RELATION.NWPR_EFF_DT | Key part; participation start |
| NWPR_TERM_DT | datetime | CMC_NWPR_RELATION.NWPR_TERM_DT | Participation end |
| AGAG_ID | char(12) | CMC_NWPR_RELATION.AGAG_ID | Provider agreement (contract) used to adjudicate; join CMC_AGAG_AGREEMENT. claims_denorm has no AGAG_ID, CMC_CLCL_CLAIM does |
| NWPR_ACC_PAT_IND | char(1) | CMC_NWPR_RELATION.NWPR_ACC_PAT_IND | Accepting patients |
| NWPR_MIN_AGE | smallint | CMC_NWPR_RELATION.NWPR_MIN_AGE | Minimum patient age accepted |
| NWPR_MAX_AGE | smallint | CMC_NWPR_RELATION.NWPR_MAX_AGE | Maximum patient age accepted |
| NWPR_SEX | char(1) | CMC_NWPR_RELATION.NWPR_SEX | Patient gender accepted |
| NWNW_NAME | char(50) | CMC_NWNW_NETWORK.NWNW_NAME | Network name |
| NWNW_MCTR_TYPE | char(4) | CMC_NWNW_NETWORK.NWNW_MCTR_TYPE | Network type (HMO, PPO...) |
| AGAG_EFF_DT | datetime | CMC_AGAG_AGREEMENT.AGAG_EFF_DT | Key part |
| AGAG_TERM_DT | datetime | CMC_AGAG_AGREEMENT.AGAG_TERM_DT | Termination date |
| AGAG_MCTR_TYPE | char(4) | CMC_AGAG_AGREEMENT.AGAG_MCTR_TYPE | Agreement type (fee schedule, DRG...) |
| AGAG_DESC | varchar(70) | CMC_AGAG_AGREEMENT.AGAG_DESC | Agreement description |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.provider_network
-- Network participation with the network and the agreement (contract). The
-- agreement row is the latest one overlapping the participation dates.
-- In network for a member on svc_dt: a row whose NWNW_ID + NWPR_PFX is in the
-- member plan's plan_network rows, with NWPR_EFF_DT <= svc_dt AND
-- ISNULL(NWPR_TERM_DT, '99991231') >= svc_dt.
TRUNCATE TABLE sdm.provider_network;
INSERT INTO sdm.provider_network WITH (TABLOCK)
    (PRPR_ID, NWNW_ID, NWPR_PFX, NWPR_PCP_IND, NWPR_EFF_DT, NWPR_TERM_DT, AGAG_ID,
     NWPR_ACC_PAT_IND, NWPR_MIN_AGE, NWPR_MAX_AGE, NWPR_SEX, NWNW_NAME, NWNW_MCTR_TYPE,
     AGAG_EFF_DT, AGAG_TERM_DT, AGAG_MCTR_TYPE, AGAG_DESC)
SELECT nw.PRPR_ID, nw.NWNW_ID, nw.NWPR_PFX, nw.NWPR_PCP_IND, nw.NWPR_EFF_DT, nw.NWPR_TERM_DT, nw.AGAG_ID,
       nw.NWPR_ACC_PAT_IND, nw.NWPR_MIN_AGE, nw.NWPR_MAX_AGE, nw.NWPR_SEX, n.NWNW_NAME, n.NWNW_MCTR_TYPE,
       ag.AGAG_EFF_DT, ag.AGAG_TERM_DT, ag.AGAG_MCTR_TYPE, ag.AGAG_DESC
FROM dbo.CMC_NWPR_RELATION nw
JOIN #scope_prov k ON k.PRPR_ID = nw.PRPR_ID
LEFT JOIN dbo.CMC_NWNW_NETWORK n ON n.NWNW_ID = nw.NWNW_ID
OUTER APPLY (SELECT TOP 1 g.AGAG_EFF_DT, g.AGAG_TERM_DT, g.AGAG_MCTR_TYPE, g.AGAG_DESC
             FROM dbo.CMC_AGAG_AGREEMENT g
             WHERE g.AGAG_ID = nw.AGAG_ID
               AND g.AGAG_EFF_DT <= ISNULL(nw.NWPR_TERM_DT, '99991231')
               AND ISNULL(g.AGAG_TERM_DT, '99991231') >= nw.NWPR_EFF_DT
             ORDER BY g.AGAG_EFF_DT DESC) ag;
```

#### sdm.provider_relation

- **Grain:** One row per dated provider relationship: PRER (practitioner/facility to group, IPA or sponsor), PRDS (claims payee, resolved to a provider ID) and PRAF (practitioner to affiliated facility). REL_SOURCE says which.
- **Clustered index (not unique):** PRPR_ID, REL_SOURCE, REL_EFF_DT
- **Index ix_provider_relation_rel:** (REL_PRPR_ID, REL_SOURCE) INCLUDE (PRPR_ID, REL_EFF_DT, REL_TERM_DT)

| Column | Type | Source | 837 / use |
|---|---|---|---|
| PRPR_ID | char(12) | CMC_PRER_RELATION.PRPR_ID | Key part |
| REL_SOURCE | char(4) | derived | 'PRER', 'PRDS' or 'PRAF' |
| REL_ENTITY | char(1) | derived | PRER: PRER_PRPR_ENTITY (G/I/S); PRDS: PRDS_PAY_CL_IND (P/G/I); PRAF: 'F' |
| REL_EFF_DT | datetime | derived | PRER_EFF_DT / PRDS_EFF_DT / PRAF_FROM_DT |
| REL_TERM_DT | datetime | derived | PRER_TERM_DT / PRDS_TERM_DT / PRAF_TO_DT |
| REL_PRPR_ID | char(12) | derived | PRER_PRPR_ID / payee PRPR_ID / facility PRPR_ID (PRAF.PRPR_ID) |
| REL_PRPR_ENTITY | char(1) | CMC_PRPR_PROV.PRPR_ENTITY | Practitioner vs facility: P = practitioner, G = provider group, I = IPA, F = facility (values from the dictionary's edit criteria). Drives NM102 (1 = person for P; 2 = non-person for G/I/F) |
| REL_PRPR_NAME | char(55) | CMC_PRPR_PROV.PRPR_NAME | NM103 organization name for G/I/F (2010AA, 2310C/2310E service facility, 2010AB) |
| REL_PRPR_NPI | char(10) | CMC_PRPR_PROV.PRPR_NPI | NM108 = XX, NM109 NPI (2010AA billing, 2310A referring, 2310B rendering, 2310C service facility, 2420A line rendering; 837I 2310A attending / 2310B operating) |
| REL_PRPR_TAXONOMY_CD | char(12) | CMC_PRPR_PROV.PRPR_TAXONOMY_CD | PRV03 taxonomy (2000A PRV*BI*PXC billing, 2310B PRV*PE*PXC rendering, 837I 2310A PRV*AT attending). Column is char(12); taxonomy codes are 10 characters |
| REL_MCTN_ID | char(9) | CMC_PRPR_PROV.MCTN_ID | Tax ID: 2010AA REF02 (REF01 = EI or SY from CMC_MCTN_TAX_NAME.MCTN_TYPE) |
| PRAF_PRIM_IND | char(1) | CMC_PRAF_FAC_AFFIL.PRAF_PRIM_IND | Primary affiliation |
| PRAF_STS | char(2) | CMC_PRAF_FAC_AFFIL.PRAF_STS | Privilege level |


### Member


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.provider_relation
-- PRER: practitioner/facility -> group (G), IPA (I) or sponsor (S). For a
--   practitioner billing through a group, the group is 837P 2010AA and the
--   practitioner 2310B.
-- PRDS: claims payee (2010AB). The payee is the provider itself (P) or the
--   PRER_PRPR_ID of the relationship whose entity equals PRDS_PAY_CL_IND
--   (dictionary note), in force when the payee row starts.
-- PRAF: practitioner -> affiliated facility (through PRCR_ID), for 837I
--   attending / operating physicians at the billing facility.
TRUNCATE TABLE sdm.provider_relation;
INSERT INTO sdm.provider_relation WITH (TABLOCK)
    (PRPR_ID, REL_SOURCE, REL_ENTITY, REL_EFF_DT, REL_TERM_DT, REL_PRPR_ID,
     REL_PRPR_ENTITY, REL_PRPR_NAME, REL_PRPR_NPI, REL_PRPR_TAXONOMY_CD, REL_MCTN_ID)
SELECT r.PRPR_ID, 'PRER', r.PRER_PRPR_ENTITY, r.PRER_EFF_DT, r.PRER_TERM_DT, r.PRER_PRPR_ID,
       rp.PRPR_ENTITY, rp.PRPR_NAME, rp.PRPR_NPI, rp.PRPR_TAXONOMY_CD, rp.MCTN_ID
FROM dbo.CMC_PRER_RELATION r
JOIN #scope_prov k ON k.PRPR_ID = r.PRPR_ID
LEFT JOIN dbo.CMC_PRPR_PROV rp ON rp.PRPR_ID = r.PRER_PRPR_ID;

INSERT INTO sdm.provider_relation WITH (TABLOCK)
    (PRPR_ID, REL_SOURCE, REL_ENTITY, REL_EFF_DT, REL_TERM_DT, REL_PRPR_ID,
     REL_PRPR_ENTITY, REL_PRPR_NAME, REL_PRPR_NPI, REL_PRPR_TAXONOMY_CD, REL_MCTN_ID)
SELECT ds.PRPR_ID, 'PRDS', ds.PRDS_PAY_CL_IND, ds.PRDS_EFF_DT, ds.PRDS_TERM_DT, py.PAYEE_ID,
       rp.PRPR_ENTITY, rp.PRPR_NAME, rp.PRPR_NPI, rp.PRPR_TAXONOMY_CD, rp.MCTN_ID
FROM dbo.CMC_PRDS_DATE ds
JOIN #scope_prov k ON k.PRPR_ID = ds.PRPR_ID
OUTER APPLY (SELECT CASE WHEN ds.PRDS_PAY_CL_IND = 'P' THEN ds.PRPR_ID
                         ELSE (SELECT TOP 1 r.PRER_PRPR_ID
                               FROM dbo.CMC_PRER_RELATION r
                               WHERE r.PRPR_ID = ds.PRPR_ID
                                 AND r.PRER_PRPR_ENTITY = ds.PRDS_PAY_CL_IND
                                 AND r.PRER_EFF_DT <= ISNULL(ds.PRDS_TERM_DT, '99991231')
                                 AND ISNULL(r.PRER_TERM_DT, '99991231') >= ds.PRDS_EFF_DT
                               ORDER BY r.PRER_EFF_DT DESC) END AS PAYEE_ID) py
LEFT JOIN dbo.CMC_PRPR_PROV rp ON rp.PRPR_ID = py.PAYEE_ID;

INSERT INTO sdm.provider_relation WITH (TABLOCK)
    (PRPR_ID, REL_SOURCE, REL_ENTITY, REL_EFF_DT, REL_TERM_DT, REL_PRPR_ID,
     REL_PRPR_ENTITY, REL_PRPR_NAME, REL_PRPR_NPI, REL_PRPR_TAXONOMY_CD, REL_MCTN_ID, PRAF_PRIM_IND, PRAF_STS)
SELECT p.PRPR_ID, 'PRAF', 'F', af.PRAF_FROM_DT, af.PRAF_TO_DT, af.PRPR_ID,
       fp.PRPR_ENTITY, fp.PRPR_NAME, fp.PRPR_NPI, fp.PRPR_TAXONOMY_CD, fp.MCTN_ID, af.PRAF_PRIM_IND, af.PRAF_STS
FROM dbo.CMC_PRPR_PROV p
JOIN #scope_prov k ON k.PRPR_ID = p.PRPR_ID
JOIN dbo.CMC_PRAF_FAC_AFFIL af ON af.PRCR_ID = p.PRCR_ID
LEFT JOIN dbo.CMC_PRPR_PROV fp ON fp.PRPR_ID = af.PRPR_ID
WHERE p.PRPR_ENTITY <> 'F'
  AND NULLIF(RTRIM(p.PRCR_ID), '') IS NOT NULL;

-- Related providers (groups, IPAs, payees, facilities) join the scope so
-- their own name, NPI, tax ID and addresses are extracted too.
INSERT INTO #scope_prov (PRPR_ID)
SELECT DISTINCT r.REL_PRPR_ID
FROM sdm.provider_relation r
WHERE NULLIF(RTRIM(r.REL_PRPR_ID), '') IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM #scope_prov k WHERE k.PRPR_ID = r.REL_PRPR_ID);
```

#### sdm.member_elig

- **Grain:** One row per member eligibility span (MEME_CK, CSPD_CAT, MEPE_EFF_DT). The bridge from member_denorm to plans: it carries CSCS_ID, which member_denorm lacks and which CMC_CSPI_CS_PLAN and CMC_CSCS_CLASS need.
- **Primary key (clustered):** MEME_CK, CSPD_CAT, MEPE_EFF_DT
- **Index ix_member_elig_plan:** (GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID) INCLUDE (MEME_CK, MEPE_EFF_DT, MEPE_TERM_DT, PDPD_ID)

| Column | Type | Source | 837 / use |
|---|---|---|---|
| MEME_CK | int | CMC_MEPE_PRCS_ELIG.MEME_CK | Key part |
| CSPD_CAT | char(1) | CMC_MEPE_PRCS_ELIG.CSPD_CAT | Key part; product category (medical, dental...) |
| MEPE_EFF_DT | datetime | CMC_MEPE_PRCS_ELIG.MEPE_EFF_DT | Key part |
| MEPE_TERM_DT | datetime | CMC_MEPE_PRCS_ELIG.MEPE_TERM_DT | End date |
| MEPE_ELIG_IND | char(1) | CMC_MEPE_PRCS_ELIG.MEPE_ELIG_IND | Eligible / not eligible for the span |
| GRGR_CK | int | CMC_MEPE_PRCS_ELIG.GRGR_CK | Group |
| SGSG_CK | int | CMC_MEPE_PRCS_ELIG.SGSG_CK | Subgroup |
| CSCS_ID | char(4) | CMC_MEPE_PRCS_ELIG.CSCS_ID | Class: the missing key from member_denorm to CMC_CSPI_CS_PLAN and CMC_CSCS_CLASS |
| CSPI_ID | char(8) | CMC_MEPE_PRCS_ELIG.CSPI_ID | Plan |
| PDPD_ID | char(8) | CMC_MEPE_PRCS_ELIG.PDPD_ID | Product |
| MEPE_FI | char(1) | CMC_MEPE_PRCS_ELIG.MEPE_FI | Family indicator |
| MEPE_PLAN_ENTRY_DT | datetime | CMC_MEPE_PRCS_ELIG.MEPE_PLAN_ENTRY_DT | Plan entry date |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.member_elig
-- Every eligibility span of the members in scope. Carries CSCS_ID, the key
-- member_denorm lacks for CMC_CSPI_CS_PLAN / CMC_CSCS_CLASS.
-- Pick the span for a service date: MEPE_EFF_DT <= svc_dt AND
-- ISNULL(MEPE_TERM_DT, '99991231') >= svc_dt AND MEPE_ELIG_IND = 'Y'.
TRUNCATE TABLE sdm.member_elig;
INSERT INTO sdm.member_elig WITH (TABLOCK)
    (MEME_CK, CSPD_CAT, MEPE_EFF_DT, MEPE_TERM_DT, MEPE_ELIG_IND, GRGR_CK, SGSG_CK,
     CSCS_ID, CSPI_ID, PDPD_ID, MEPE_FI, MEPE_PLAN_ENTRY_DT)
SELECT e.MEME_CK, e.CSPD_CAT, e.MEPE_EFF_DT, e.MEPE_TERM_DT, e.MEPE_ELIG_IND, e.GRGR_CK, e.SGSG_CK,
       e.CSCS_ID, e.CSPI_ID, e.PDPD_ID, e.MEPE_FI, e.MEPE_PLAN_ENTRY_DT
FROM dbo.CMC_MEPE_PRCS_ELIG e
JOIN #scope_member s ON s.MEME_CK = e.MEME_CK;
```

#### sdm.subscriber_denorm

- **Grain:** One row per subscriber (SBSB_CK): 2000B / 2010BA, the subscriber's name, group number and home address.
- **Primary key (clustered):** SBSB_CK

| Column | Type | Source | 837 / use |
|---|---|---|---|
| SBSB_CK | int | CMC_SBSB_SUBSC.SBSB_CK | Key |
| GRGR_CK | int | CMC_SBSB_SUBSC.GRGR_CK | Group |
| GRGR_ID | char(8) | CMC_GRGR_GROUP.GRGR_ID | 2000B SBR03 group or policy number |
| SBSB_ID | char(9) | CMC_SBSB_SUBSC.SBSB_ID | 2010BA NM109 member ID (SBSB_ID + MEME_SFX per the tool's convention) |
| SBSB_LAST_NAME | char(35) | CMC_SBSB_SUBSC.SBSB_LAST_NAME | 2010BA NM103 |
| SBSB_FIRST_NAME | char(15) | CMC_SBSB_SUBSC.SBSB_FIRST_NAME | 2010BA NM104 |
| SBSB_MID_INIT | char(1) | CMC_SBSB_SUBSC.SBSB_MID_INIT | 2010BA NM105 |
| SBAD_TYPE_HOME | char(1) | CMC_SBSB_SUBSC.SBAD_TYPE_HOME | Subscriber home address type (CMC_SBAD_ADDR.SBAD_TYPE) |
| SBAD_ADDR1 | char(40) | CMC_SBAD_ADDR.SBAD_ADDR1 | N301 (2010BA subscriber, 2010CA patient) |
| SBAD_ADDR2 | char(40) | CMC_SBAD_ADDR.SBAD_ADDR2 | N302 |
| SBAD_CITY | char(19) | CMC_SBAD_ADDR.SBAD_CITY | N401 |
| SBAD_STATE | char(2) | CMC_SBAD_ADDR.SBAD_STATE | N402 |
| SBAD_ZIP | char(11) | CMC_SBAD_ADDR.SBAD_ZIP | N403 |
| SBAD_COUNTY | char(20) | CMC_SBAD_ADDR.SBAD_COUNTY | County |
| SBAD_CTRY_CD | char(4) | CMC_SBAD_ADDR.SBAD_CTRY_CD | N404 |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.subscriber_denorm
-- 2000B / 2010BA: subscriber name, group number (SBR03) and home address.
TRUNCATE TABLE sdm.subscriber_denorm;
INSERT INTO sdm.subscriber_denorm WITH (TABLOCK)
    (SBSB_CK, GRGR_CK, GRGR_ID, SBSB_ID, SBSB_LAST_NAME, SBSB_FIRST_NAME, SBSB_MID_INIT,
     SBAD_TYPE_HOME, SBAD_ADDR1, SBAD_ADDR2, SBAD_CITY, SBAD_STATE, SBAD_ZIP, SBAD_COUNTY, SBAD_CTRY_CD)
SELECT sb.SBSB_CK, sb.GRGR_CK, g.GRGR_ID, sb.SBSB_ID, sb.SBSB_LAST_NAME, sb.SBSB_FIRST_NAME, sb.SBSB_MID_INIT,
       sb.SBAD_TYPE_HOME, a.SBAD_ADDR1, a.SBAD_ADDR2, a.SBAD_CITY, a.SBAD_STATE, a.SBAD_ZIP, a.SBAD_COUNTY, a.SBAD_CTRY_CD
FROM dbo.CMC_SBSB_SUBSC sb
JOIN (SELECT DISTINCT m.SBSB_CK
      FROM dbo.CMC_MEME_MEMBER m
      JOIN #scope_member s ON s.MEME_CK = m.MEME_CK) ss ON ss.SBSB_CK = sb.SBSB_CK
LEFT JOIN dbo.CMC_GRGR_GROUP g ON g.GRGR_CK = sb.GRGR_CK
LEFT JOIN dbo.CMC_SBAD_ADDR a ON a.SBSB_CK = sb.SBSB_CK AND a.SBAD_TYPE = sb.SBAD_TYPE_HOME;
```

#### sdm.member_address

- **Grain:** One row per member x address role (MEME_CK, ADDR_ROLE = HOME or MAIL), resolved the way Facets does: the member's own address type, else the subscriber's home. 2010CA patient address (and 2010BA when the member is the subscriber).
- **Primary key (clustered):** MEME_CK, ADDR_ROLE
- **Index ix_member_address_zip:** (SBAD_ZIP) INCLUDE (MEME_CK, SBAD_STATE)

| Column | Type | Source | 837 / use |
|---|---|---|---|
| MEME_CK | int | CMC_MEME_MEMBER.MEME_CK | Key |
| ADDR_ROLE | char(4) | derived | 'HOME' or 'MAIL' |
| SBSB_CK | int | CMC_MEME_MEMBER.SBSB_CK | Subscriber |
| SBAD_TYPE | char(1) | CMC_SBAD_ADDR.SBAD_TYPE | Key part; address type |
| FROM_SUBSCRIBER | char(1) | derived | 'Y' when the member has no own address type and the subscriber's home is used |
| SBAD_ADDR1 | char(40) | CMC_SBAD_ADDR.SBAD_ADDR1 | N301 (2010BA subscriber, 2010CA patient) |
| SBAD_ADDR2 | char(40) | CMC_SBAD_ADDR.SBAD_ADDR2 | N302 |
| SBAD_CITY | char(19) | CMC_SBAD_ADDR.SBAD_CITY | N401 |
| SBAD_STATE | char(2) | CMC_SBAD_ADDR.SBAD_STATE | N402 |
| SBAD_ZIP | char(11) | CMC_SBAD_ADDR.SBAD_ZIP | N403 |
| SBAD_COUNTY | char(20) | CMC_SBAD_ADDR.SBAD_COUNTY | County |
| SBAD_CTRY_CD | char(4) | CMC_SBAD_ADDR.SBAD_CTRY_CD | N404 |
| SBAD_PHONE | char(20) | CMC_SBAD_ADDR.SBAD_PHONE | Phone (not an 837P element; used for realism) |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.member_address
-- 2010CA patient address (2010BA when the member is the subscriber). Facets
-- rule from the dictionary: a member with no home or mailing address type of
-- its own uses the subscriber's home address. CMC_SBAD_ADDR has no dates, so
-- this is the current address.
TRUNCATE TABLE sdm.member_address;
INSERT INTO sdm.member_address WITH (TABLOCK)
    (MEME_CK, ADDR_ROLE, SBSB_CK, SBAD_TYPE, FROM_SUBSCRIBER,
     SBAD_ADDR1, SBAD_ADDR2, SBAD_CITY, SBAD_STATE, SBAD_ZIP, SBAD_COUNTY, SBAD_CTRY_CD, SBAD_PHONE)
SELECT m.MEME_CK, r.ADDR_ROLE, m.SBSB_CK, a.SBAD_TYPE, r.FROM_SUBSCRIBER,
       a.SBAD_ADDR1, a.SBAD_ADDR2, a.SBAD_CITY, a.SBAD_STATE, a.SBAD_ZIP, a.SBAD_COUNTY, a.SBAD_CTRY_CD, a.SBAD_PHONE
FROM dbo.CMC_MEME_MEMBER m
JOIN #scope_member s ON s.MEME_CK = m.MEME_CK
JOIN dbo.CMC_SBSB_SUBSC sb ON sb.SBSB_CK = m.SBSB_CK
CROSS APPLY (VALUES
    ('HOME', COALESCE(NULLIF(RTRIM(m.SBAD_TYPE_HOME), ''), sb.SBAD_TYPE_HOME),
             CASE WHEN NULLIF(RTRIM(m.SBAD_TYPE_HOME), '') IS NULL THEN 'Y' ELSE 'N' END),
    ('MAIL', COALESCE(NULLIF(RTRIM(m.SBAD_TYPE_MAIL), ''), sb.SBAD_TYPE_HOME),
             CASE WHEN NULLIF(RTRIM(m.SBAD_TYPE_MAIL), '') IS NULL THEN 'Y' ELSE 'N' END)
) r (ADDR_ROLE, ADDR_TYPE, FROM_SUBSCRIBER)
JOIN dbo.CMC_SBAD_ADDR a ON a.SBSB_CK = m.SBSB_CK AND a.SBAD_TYPE = r.ADDR_TYPE;
```

#### sdm.member_pcp

- **Grain:** One row per member PCP assignment (MEME_CK, MEPR_PCP_TYPE, MEPR_EFF_DT), with the PCP's NPI and entity.
- **Primary key (clustered):** MEME_CK, MEPR_PCP_TYPE, MEPR_EFF_DT
- **Index ix_member_pcp_prov:** (PRPR_ID) INCLUDE (MEME_CK, MEPR_EFF_DT, MEPR_TERM_DT)

| Column | Type | Source | 837 / use |
|---|---|---|---|
| MEME_CK | int | CMC_MEPR_PRIM_PROV.MEME_CK | Key part |
| MEPR_PCP_TYPE | char(2) | CMC_MEPR_PRIM_PROV.MEPR_PCP_TYPE | Key part; PCP type |
| MEPR_EFF_DT | datetime | CMC_MEPR_PRIM_PROV.MEPR_EFF_DT | Key part |
| MEPR_TERM_DT | datetime | CMC_MEPR_PRIM_PROV.MEPR_TERM_DT | End date |
| GRGR_CK | int | CMC_MEPR_PRIM_PROV.GRGR_CK | Group |
| PRPR_ID | char(12) | CMC_MEPR_PRIM_PROV.PRPR_ID | PCP: claims_denorm.CLCL_PRPR_ID_PCP; typical 837P 2310A referring provider (NM1*DN) on HMO referral claims |
| PRPR_NPI | char(10) | CMC_PRPR_PROV.PRPR_NPI | NM108 = XX, NM109 NPI (2010AA billing, 2310A referring, 2310B rendering, 2310C service facility, 2420A line rendering; 837I 2310A attending / 2310B operating) |
| PRPR_ENTITY | char(1) | CMC_PRPR_PROV.PRPR_ENTITY | Practitioner vs facility: P = practitioner, G = provider group, I = IPA, F = facility (values from the dictionary's edit criteria). Drives NM102 (1 = person for P; 2 = non-person for G/I/F) |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.member_pcp
-- PCP history. PCP on svc_dt: MEPR_EFF_DT <= svc_dt AND
-- ISNULL(MEPR_TERM_DT, '99991231') >= svc_dt (per MEPR_PCP_TYPE).
TRUNCATE TABLE sdm.member_pcp;
INSERT INTO sdm.member_pcp WITH (TABLOCK)
    (MEME_CK, MEPR_PCP_TYPE, MEPR_EFF_DT, MEPR_TERM_DT, GRGR_CK, PRPR_ID, PRPR_NPI, PRPR_ENTITY)
SELECT pc.MEME_CK, pc.MEPR_PCP_TYPE, pc.MEPR_EFF_DT, pc.MEPR_TERM_DT, pc.GRGR_CK, pc.PRPR_ID, p.PRPR_NPI, p.PRPR_ENTITY
FROM dbo.CMC_MEPR_PRIM_PROV pc
JOIN #scope_member s ON s.MEME_CK = pc.MEME_CK
LEFT JOIN dbo.CMC_PRPR_PROV p ON p.PRPR_ID = pc.PRPR_ID;
```

#### sdm.member_cob

- **Grain:** One row per member other-coverage span (MEME_CK, MECB_INSUR_TYPE, MECB_INSUR_ORDER, MECB_MCTR_STYP, MECB_EFF_DT), with the other carrier's name and address: 2000B SBR01, 2320, 2330A, 2330B.
- **Clustered index (not unique):** MEME_CK, MECB_EFF_DT

| Column | Type | Source | 837 / use |
|---|---|---|---|
| MEME_CK | int | CMC_MECB_COB.MEME_CK | Key part |
| MECB_INSUR_TYPE | char(1) | CMC_MECB_COB.MECB_INSUR_TYPE | Key part; type of other coverage |
| MECB_INSUR_ORDER | char(1) | CMC_MECB_COB.MECB_INSUR_ORDER | Key part; order of determination: 2000B SBR01 (this payer) and 2320 SBR01 (other payer) P/S/T |
| MECB_MCTR_STYP | char(4) | CMC_MECB_COB.MECB_MCTR_STYP | Key part; supplemental drug type |
| MECB_EFF_DT | datetime | CMC_MECB_COB.MECB_EFF_DT | Key part |
| MECB_TERM_DT | datetime | CMC_MECB_COB.MECB_TERM_DT | End date |
| MECB_POLICY_ID | varchar(25) | CMC_MECB_COB.MECB_POLICY_ID | 2320 SBR03 / 2330A NM109 other subscriber's policy ID |
| MECB_PRIM_LAST_NM | varchar(35) | CMC_MECB_COB.MECB_PRIM_LAST_NM | 2330A NM103 other insured last name |
| MECB_PRIM_FIRST_NM | varchar(15) | CMC_MECB_COB.MECB_PRIM_FIRST_NM | 2330A NM104 |
| MECB_PRIM_ID | varchar(40) | CMC_MECB_COB.MECB_PRIM_ID | 2330A NM109 other insured ID |
| MECB_MCTR_MSP | char(4) | CMC_MECB_COB.MECB_MCTR_MSP | 2320 SBR05 Medicare secondary payer type |
| MCRE_ID | char(9) | CMC_MECB_COB.MCRE_ID | Other carrier: 2330B NM109 (as a proprietary payer ID, NM108 = PI; see gaps) |
| MCRE_NAME | char(50) | CMC_MCRE_RELAT_ENT.MCRE_NAME | 2330B NM103 other payer name |
| MCRE_ADDR1 | char(40) | CMC_MCRE_RELAT_ENT.MCRE_ADDR1 | 2330B N301 |
| MCRE_ADDR2 | char(40) | CMC_MCRE_RELAT_ENT.MCRE_ADDR2 | 2330B N302 |
| MCRE_CITY | char(19) | CMC_MCRE_RELAT_ENT.MCRE_CITY | 2330B N401 |
| MCRE_STATE | char(2) | CMC_MCRE_RELAT_ENT.MCRE_STATE | 2330B N402 |
| MCRE_ZIP | char(11) | CMC_MCRE_RELAT_ENT.MCRE_ZIP | 2330B N403 |


### Plan and benefit


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.member_cob
-- Other coverage (2000B SBR01, 2320, 2330A, 2330B) with the other carrier.
TRUNCATE TABLE sdm.member_cob;
INSERT INTO sdm.member_cob WITH (TABLOCK)
    (MEME_CK, MECB_INSUR_TYPE, MECB_INSUR_ORDER, MECB_MCTR_STYP, MECB_EFF_DT, MECB_TERM_DT,
     MECB_POLICY_ID, MECB_PRIM_LAST_NM, MECB_PRIM_FIRST_NM, MECB_PRIM_ID, MECB_MCTR_MSP,
     MCRE_ID, MCRE_NAME, MCRE_ADDR1, MCRE_ADDR2, MCRE_CITY, MCRE_STATE, MCRE_ZIP)
SELECT cb.MEME_CK, cb.MECB_INSUR_TYPE, cb.MECB_INSUR_ORDER, cb.MECB_MCTR_STYP, cb.MECB_EFF_DT, cb.MECB_TERM_DT,
       cb.MECB_POLICY_ID, cb.MECB_PRIM_LAST_NM, cb.MECB_PRIM_FIRST_NM, cb.MECB_PRIM_ID, cb.MECB_MCTR_MSP,
       cb.MCRE_ID, re.MCRE_NAME, re.MCRE_ADDR1, re.MCRE_ADDR2, re.MCRE_CITY, re.MCRE_STATE, re.MCRE_ZIP
FROM dbo.CMC_MECB_COB cb
JOIN #scope_member s ON s.MEME_CK = cb.MEME_CK
LEFT JOIN dbo.CMC_MCRE_RELAT_ENT re ON re.MCRE_ID = cb.MCRE_ID;
```

#### sdm.plan_denorm

- **Grain:** One row per class/plan row (GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID, CSPI_EFF_DT): product, network set, service conversion prefix, line of business and the payer (2010BB) in force at the plan's start.
- **Primary key (clustered):** GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID, CSPI_EFF_DT
- **Index ix_plan_denorm_pdpd:** (PDPD_ID) INCLUDE (CSPI_ID, NWST_PFX, TPCT_PFX)

| Column | Type | Source | 837 / use |
|---|---|---|---|
| GRGR_CK | int | CMC_CSPI_CS_PLAN.GRGR_CK | Key part |
| CSCS_ID | char(4) | CMC_CSPI_CS_PLAN.CSCS_ID | Key part |
| CSPD_CAT | char(1) | CMC_CSPI_CS_PLAN.CSPD_CAT | Key part |
| CSPI_ID | char(8) | CMC_CSPI_CS_PLAN.CSPI_ID | Key part |
| CSPI_EFF_DT | datetime | CMC_CSPI_CS_PLAN.CSPI_EFF_DT | Key part |
| CSPI_TERM_DT | datetime | CMC_CSPI_CS_PLAN.CSPI_TERM_DT | End date |
| PDPD_ID | char(8) | CMC_CSPI_CS_PLAN.PDPD_ID | Product |
| NWST_PFX | char(4) | CMC_CSPI_CS_PLAN.NWST_PFX | Network set: which networks (and tiers) the plan uses; join CMC_NWST_NET_SET |
| CSPI_PDPD_BEG_MMDD | smallint | CMC_CSPI_CS_PLAN.CSPI_PDPD_BEG_MMDD | Plan year begin (MMDD) for deductible / limit periods |
| PLDS_DESC | char(70) | CMC_PLDS_PLAN_DESC.PLDS_DESC | Plan description (also in claims_denorm) |
| TPCT_PFX | char(4) | CMC_CSCS_CLASS.TPCT_PFX | Service conversion prefix: maps procedures / revenue codes to Service IDs (CMC_TPCT_CPT_CONV, CMC_RCCT_CONV, CMC_SECT_CONV) |
| LOBD_ID | char(4) | CMC_PDPD_PRODUCT.LOBD_ID | Line of business: payer via CMC_LOBD_LINE_BUS.PYPY_ID |
| PDDS_DESC | varchar(70) | CMC_PDDS_PROD_DESC.PDDS_DESC | Product description |
| PDDS_PROD_TYPE | char(1) | CMC_PDDS_PROD_DESC.PDDS_PROD_TYPE | Commercial / Medicare risk / Medicaid: derive 2000B SBR09 claim filing indicator (CI, MB/16, MC...) |
| PDDS_MCTR_BCAT | char(4) | CMC_PDDS_PROD_DESC.PDDS_MCTR_BCAT | Business category |
| PDDS_UM_IND | char(1) | CMC_PDDS_PROD_DESC.PDDS_UM_IND | Product uses referrals / pre-authorizations |
| PDDS_OON_IND | char(1) | CMC_PDDS_PROD_DESC.PDDS_OON_IND | Out-of-network submissions accepted |
| PYPY_ID | char(8) | CMC_LOBD_LINE_BUS.PYPY_ID | Payor |
| PYPY_PAYOR_NAME | char(50) | CMC_PYPY_PAYOR.PYPY_PAYOR_NAME | 2010BB NM103 payer name |
| PYPY_NATL_PLAN_ID | varchar(80) | CMC_PYPY_PAYOR.PYPY_NATL_PLAN_ID | 2010BB NM109 (NM108 = XV national plan ID) |
| PYPY_NAIC_CODE | varchar(50) | CMC_PYPY_PAYOR.PYPY_NAIC_CODE | 2010BB NM109 (NM108 = PI) or REF*NF |
| PYPY_TAX_ID | char(9) | CMC_PYPY_PAYOR.PYPY_TAX_ID | 2010BB REF*EI |
| PYPY_ADDR1 | char(40) | CMC_PYPY_PAYOR.PYPY_ADDR1 | 2010BB N301 |
| PYPY_ADDR2 | char(40) | CMC_PYPY_PAYOR.PYPY_ADDR2 | 2010BB N302 |
| PYPY_CITY | char(19) | CMC_PYPY_PAYOR.PYPY_CITY | 2010BB N401 |
| PYPY_STATE | char(2) | CMC_PYPY_PAYOR.PYPY_STATE | 2010BB N402 |
| PYPY_ZIP | char(11) | CMC_PYPY_PAYOR.PYPY_ZIP | 2010BB N403 |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.plan_denorm
-- Class/plan rows with the product, network set (NWST_PFX), service conversion
-- prefix (TPCT_PFX), line of business and payer (2010BB). The product and payer
-- rows are the latest ones overlapping the plan row's dates.
TRUNCATE TABLE sdm.plan_denorm;
INSERT INTO sdm.plan_denorm WITH (TABLOCK)
    (GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID, CSPI_EFF_DT, CSPI_TERM_DT, PDPD_ID, NWST_PFX, CSPI_PDPD_BEG_MMDD,
     PLDS_DESC, TPCT_PFX, LOBD_ID, PDDS_DESC, PDDS_PROD_TYPE, PDDS_MCTR_BCAT, PDDS_UM_IND, PDDS_OON_IND,
     PYPY_ID, PYPY_PAYOR_NAME, PYPY_NATL_PLAN_ID, PYPY_NAIC_CODE, PYPY_TAX_ID,
     PYPY_ADDR1, PYPY_ADDR2, PYPY_CITY, PYPY_STATE, PYPY_ZIP)
SELECT p.GRGR_CK, p.CSCS_ID, p.CSPD_CAT, p.CSPI_ID, p.CSPI_EFF_DT, p.CSPI_TERM_DT, p.PDPD_ID, p.NWST_PFX, p.CSPI_PDPD_BEG_MMDD,
       pl.PLDS_DESC, cs.TPCT_PFX, pd.LOBD_ID, ds.PDDS_DESC, ds.PDDS_PROD_TYPE, ds.PDDS_MCTR_BCAT, ds.PDDS_UM_IND, ds.PDDS_OON_IND,
       lb.PYPY_ID, py.PYPY_PAYOR_NAME, py.PYPY_NATL_PLAN_ID, py.PYPY_NAIC_CODE, py.PYPY_TAX_ID,
       py.PYPY_ADDR1, py.PYPY_ADDR2, py.PYPY_CITY, py.PYPY_STATE, py.PYPY_ZIP
FROM dbo.CMC_CSPI_CS_PLAN p
JOIN #scope_plan k
  ON k.GRGR_CK = p.GRGR_CK AND k.CSCS_ID = p.CSCS_ID AND k.CSPD_CAT = p.CSPD_CAT AND k.CSPI_ID = p.CSPI_ID
LEFT JOIN dbo.CMC_PLDS_PLAN_DESC pl ON pl.CSPI_ID = p.CSPI_ID
LEFT JOIN dbo.CMC_CSCS_CLASS cs ON cs.GRGR_CK = p.GRGR_CK AND cs.CSCS_ID = p.CSCS_ID
OUTER APPLY (SELECT TOP 1 x.LOBD_ID
             FROM dbo.CMC_PDPD_PRODUCT x
             WHERE x.PDPD_ID = p.PDPD_ID
               AND x.PDPD_EFF_DT <= ISNULL(p.CSPI_TERM_DT, '99991231')
               AND ISNULL(x.PDPD_TERM_DT, '99991231') >= p.CSPI_EFF_DT
             ORDER BY x.PDPD_EFF_DT DESC) pd
LEFT JOIN dbo.CMC_PDDS_PROD_DESC ds ON ds.PDPD_ID = p.PDPD_ID
LEFT JOIN dbo.CMC_LOBD_LINE_BUS lb ON lb.LOBD_ID = pd.LOBD_ID
OUTER APPLY (SELECT TOP 1 y.PYPY_PAYOR_NAME, y.PYPY_NATL_PLAN_ID, y.PYPY_NAIC_CODE, y.PYPY_TAX_ID,
                    y.PYPY_ADDR1, y.PYPY_ADDR2, y.PYPY_CITY, y.PYPY_STATE, y.PYPY_ZIP
             FROM dbo.CMC_PYPY_PAYOR y
             WHERE y.PYPY_ID = lb.PYPY_ID
               AND y.PYPY_EFF_DT <= ISNULL(p.CSPI_TERM_DT, '99991231')
               AND ISNULL(y.PYPY_TERM_DT, '99991231') >= p.CSPI_EFF_DT
             ORDER BY y.PYPY_EFF_DT DESC) py;
```

#### sdm.plan_network

- **Grain:** One row per network-set row (NWST_PFX, NWST_EFF_DT, NWST_SEQ_NO): which networks a plan uses, in-network vs participating, and the tier.
- **Primary key (clustered):** NWST_PFX, NWST_EFF_DT, NWST_SEQ_NO
- **Index ix_plan_network_net:** (NWNW_ID, NWPR_PFX) INCLUDE (NWST_PFX, NWST_EFF_DT, NWST_TERM_DT, PDVC_TIER)

| Column | Type | Source | 837 / use |
|---|---|---|---|
| NWST_PFX | char(4) | CMC_NWST_NET_SET.NWST_PFX | Key part |
| NWST_EFF_DT | datetime | CMC_NWST_NET_SET.NWST_EFF_DT | Key part |
| NWST_SEQ_NO | smallint | CMC_NWST_NET_SET.NWST_SEQ_NO | Key part |
| NWST_TERM_DT | datetime | CMC_NWST_NET_SET.NWST_TERM_DT | End date |
| NWNW_ID | char(12) | CMC_NWST_NET_SET.NWNW_ID | Network in the plan's set |
| NWPR_PFX | char(4) | CMC_NWST_NET_SET.NWPR_PFX | Provider-relationship prefix to match on CMC_NWPR_RELATION |
| NWST_PR_NW_STS | char(1) | CMC_NWST_NET_SET.NWST_PR_NW_STS | I = HMO / in-network, P = PPO / participating (drives claims_denorm.CLCL_NTWK_IND) |
| PDVC_TIER | smallint | CMC_NWST_NET_SET.PDVC_TIER | Network tier: picks the CMC_PDVC_VAR_COMP row (cost-share tier) |
| NWNW_NAME | char(50) | CMC_NWNW_NETWORK.NWNW_NAME | Network name |
| NWNW_MCTR_TYPE | char(4) | CMC_NWNW_NETWORK.NWNW_MCTR_TYPE | Network type (HMO, PPO...) |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.plan_network
-- The networks of each plan's network set, with in-network/participating
-- status and the tier that selects the cost-share row in plan_benefit_denorm.
TRUNCATE TABLE sdm.plan_network;
INSERT INTO sdm.plan_network WITH (TABLOCK)
    (NWST_PFX, NWST_EFF_DT, NWST_SEQ_NO, NWST_TERM_DT, NWNW_ID, NWPR_PFX, NWST_PR_NW_STS, PDVC_TIER,
     NWNW_NAME, NWNW_MCTR_TYPE)
SELECT ns.NWST_PFX, ns.NWST_EFF_DT, ns.NWST_SEQ_NO, ns.NWST_TERM_DT, ns.NWNW_ID, ns.NWPR_PFX, ns.NWST_PR_NW_STS, ns.PDVC_TIER,
       n.NWNW_NAME, n.NWNW_MCTR_TYPE
FROM dbo.CMC_NWST_NET_SET ns
JOIN (SELECT DISTINCT NWST_PFX FROM sdm.plan_denorm) k ON k.NWST_PFX = ns.NWST_PFX
LEFT JOIN dbo.CMC_NWNW_NETWORK n ON n.NWNW_ID = ns.NWNW_ID;
```

#### sdm.plan_benefit_denorm

- **Grain:** One row per product variable component x covered Service ID x cost-share tier: (PDPD_ID, PDVC_TIER, PDVC_TYPE, PDVC_SEQ_NO, PDVC_EFF_DT, SESE_ID, SEPY_EFF_DT, SETR_TIER_NO). BEN_EFF_DT / BEN_TERM_DT is where the PDVC and SEPY windows overlap; a service is covered for a member on svc_dt when a row exists for the member's PDPD_ID and network tier with BEN_EFF_DT <= svc_dt <= BEN_TERM_DT and SESE_RULE_TYPE is not Disallow.
- **Clustered index (not unique):** PDPD_ID, SESE_ID, BEN_EFF_DT
- **Index ix_plan_benefit_tier:** (PDPD_ID, PDVC_TIER, BEN_EFF_DT) INCLUDE (BEN_TERM_DT, SESE_ID, SESE_RULE, SESE_RULE_TYPE)

| Column | Type | Source | 837 / use |
|---|---|---|---|
| PDPD_ID | char(8) | CMC_PDVC_VAR_COMP.PDPD_ID | Key part |
| PDVC_TIER | smallint | CMC_PDVC_VAR_COMP.PDVC_TIER | Key part; provider tier |
| PDVC_TYPE | char(1) | CMC_PDVC_VAR_COMP.PDVC_TYPE | Key part; variable component type |
| PDVC_SEQ_NO | smallint | CMC_PDVC_VAR_COMP.PDVC_SEQ_NO | Key part |
| PDVC_EFF_DT | datetime | CMC_PDVC_VAR_COMP.PDVC_EFF_DT | Key part |
| PDVC_TERM_DT | datetime | CMC_PDVC_VAR_COMP.PDVC_TERM_DT | End date |
| PDVC_PR_PCP | char(1) | CMC_PDVC_VAR_COMP.PDVC_PR_PCP | Row applies when the servicing provider is the member's PCP |
| PDVC_PR_IN | char(1) | CMC_PDVC_VAR_COMP.PDVC_PR_IN | ... in-network provider |
| PDVC_PR_PAR | char(1) | CMC_PDVC_VAR_COMP.PDVC_PR_PAR | ... participating provider |
| PDVC_PR_NONPAR | char(1) | CMC_PDVC_VAR_COMP.PDVC_PR_NONPAR | ... non-participating provider |
| PDVC_PC_NR | char(1) | CMC_PDVC_VAR_COMP.PDVC_PC_NR | ... pre-authorization not required |
| PDVC_PC_OBT | char(1) | CMC_PDVC_VAR_COMP.PDVC_PC_OBT | ... pre-authorization obtained (2300/2400 REF*G1 present) |
| PDVC_PC_VIOL | char(1) | CMC_PDVC_VAR_COMP.PDVC_PC_VIOL | ... pre-authorization violated |
| PDVC_REF_NR | char(1) | CMC_PDVC_VAR_COMP.PDVC_REF_NR | ... referral not required |
| PDVC_REF_OBT | char(1) | CMC_PDVC_VAR_COMP.PDVC_REF_OBT | ... referral obtained (REF*9F, 2310A referring present) |
| PDVC_REF_VIOL | char(1) | CMC_PDVC_VAR_COMP.PDVC_REF_VIOL | ... referral violated |
| PDVC_LOBD_PTR | char(1) | CMC_PDVC_VAR_COMP.PDVC_LOBD_PTR | Primary or alternate line of business |
| SEPY_PFX | char(4) | CMC_PDVC_VAR_COMP.SEPY_PFX | Service payment prefix: CMC_SEPY_SE_PYMT |
| DEDE_PFX | char(4) | CMC_PDVC_VAR_COMP.DEDE_PFX | Deductible prefix: CMC_DEDE_DEDUCT |
| LTLT_PFX | char(4) | CMC_PDVC_VAR_COMP.LTLT_PFX | Limit prefix: CMC_LTLT_LIMIT |
| SEPY_EFF_DT | datetime | CMC_SEPY_SE_PYMT.SEPY_EFF_DT | Key part |
| SEPY_TERM_DT | datetime | CMC_SEPY_SE_PYMT.SEPY_TERM_DT | End date |
| BEN_EFF_DT | datetime | derived | Later of PDVC_EFF_DT and SEPY_EFF_DT |
| BEN_TERM_DT | datetime | derived | Earlier of PDVC_TERM_DT and SEPY_TERM_DT (NULL read as 9999-12-31; 9999-12-31 = open) |
| SESE_ID | char(4) | CMC_SEPY_SE_PYMT.SESE_ID | Key part; covered Service ID (type of service) |
| SESE_RULE | char(3) | CMC_SEPY_SE_PYMT.SESE_RULE | Service rule (cost-share rule) for the Service ID |
| SEDS_DESC | char(70) | CMC_SEDS_SE_DESC.SEDS_DESC | Service description |
| SEDS_TYPE | char(1) | CMC_SEDS_SE_DESC.SEDS_TYPE | Surgery / assistant surgery / anesthesia |
| SESE_RULE_TYPE | char(1) | CMC_SESE_SERVICE.SESE_RULE_TYPE | Amount, Counter or Disallow (a Disallow service is not covered) |
| SESE_CALC_IND | char(1) | CMC_SESE_SERVICE.SESE_CALC_IND | Order of allowed / deductible / copay / coinsurance calculation |
| SESE_VALID_SEX | char(1) | CMC_SESE_SERVICE.SESE_VALID_SEX | Valid gender for the service (match MEME_SEX) |
| SESE_MIN_AGE | smallint | CMC_SESE_SERVICE.SESE_MIN_AGE | Minimum age |
| SESE_MAX_AGE | smallint | CMC_SESE_SERVICE.SESE_MAX_AGE | Maximum age |
| SESE_COV_TYPE | char(1) | CMC_SESE_SERVICE.SESE_COV_TYPE | Covered members (subscriber / spouse / dependents) |
| SETR_TIER_NO | smallint | CMC_SETR_TIER.SETR_TIER_NO | Key part; calculation tier 0-9 |
| SETR_ALLOW_AMT | money | CMC_SETR_TIER.SETR_ALLOW_AMT | Maximum allowed amount |
| SETR_ALLOW_CTR | smallint | CMC_SETR_TIER.SETR_ALLOW_CTR | Maximum allowed counter (visits/units) |
| SETR_COPAY_AMT | money | CMC_SETR_TIER.SETR_COPAY_AMT | Copay: claims_denorm.CDML_COPAY_AMT; 2320/2430 CAS PR-3 on COB claims |
| SETR_COIN_PCT | money | CMC_SETR_TIER.SETR_COIN_PCT | Coinsurance %: CDML_COINS_AMT; CAS PR-2 |
| ACAC_ACC_NO | smallint | CMC_SETR_TIER.ACAC_ACC_NO | Deductible accumulator (CMC_DEDE_DEDUCT.ACAC_ACC_NO): CDML_DED_AMT; CAS PR-1 |
| SEDF_UM_REQ_IND | char(1) | CMC_SEDF_SE_DEFN.SEDF_UM_REQ_IND | Service needs a referral and/or pre-authorization: then 2300 REF*G1 / REF*9F must be generated |
| SEDF_UM_BP_REF_IND | char(1) | CMC_SEDF_SE_DEFN.SEDF_UM_BP_REF_IND | Referral requirement bypassed |
| SEDF_CAP_IND | char(1) | CMC_SEDF_SE_DEFN.SEDF_CAP_IND | Capitated service |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.plan_benefit_denorm
-- What a product covers and at what cost share, per provider tier:
--   PDVC (product x tier x conditions) -> SEPY (covered Service IDs + rule)
--   -> SESE (rule type, age/sex) -> SETR (copay, coinsurance, deductible
--   accumulator) and SEDF (referral / pre-authorization requirement).
-- BEN_EFF_DT / BEN_TERM_DT = overlap of the PDVC and SEPY windows
-- ('9999-12-31' when both are open). SEDF comes from the product's
-- service-definition component; PDBC_TYPE = 'SEDF' is assumed -- check
-- CMC_PDPT_DESC on the customer's database.
TRUNCATE TABLE sdm.plan_benefit_denorm;
INSERT INTO sdm.plan_benefit_denorm WITH (TABLOCK)
    (PDPD_ID, PDVC_TIER, PDVC_TYPE, PDVC_SEQ_NO, PDVC_EFF_DT, PDVC_TERM_DT,
     PDVC_PR_PCP, PDVC_PR_IN, PDVC_PR_PAR, PDVC_PR_NONPAR, PDVC_PC_NR, PDVC_PC_OBT, PDVC_PC_VIOL,
     PDVC_REF_NR, PDVC_REF_OBT, PDVC_REF_VIOL, PDVC_LOBD_PTR, SEPY_PFX, DEDE_PFX, LTLT_PFX,
     SEPY_EFF_DT, SEPY_TERM_DT, BEN_EFF_DT, BEN_TERM_DT, SESE_ID, SESE_RULE, SEDS_DESC, SEDS_TYPE,
     SESE_RULE_TYPE, SESE_CALC_IND, SESE_VALID_SEX, SESE_MIN_AGE, SESE_MAX_AGE, SESE_COV_TYPE,
     SETR_TIER_NO, SETR_ALLOW_AMT, SETR_ALLOW_CTR, SETR_COPAY_AMT, SETR_COIN_PCT, ACAC_ACC_NO,
     SEDF_UM_REQ_IND, SEDF_UM_BP_REF_IND, SEDF_CAP_IND)
SELECT v.PDPD_ID, v.PDVC_TIER, v.PDVC_TYPE, v.PDVC_SEQ_NO, v.PDVC_EFF_DT, v.PDVC_TERM_DT,
       v.PDVC_PR_PCP, v.PDVC_PR_IN, v.PDVC_PR_PAR, v.PDVC_PR_NONPAR, v.PDVC_PC_NR, v.PDVC_PC_OBT, v.PDVC_PC_VIOL,
       v.PDVC_REF_NR, v.PDVC_REF_OBT, v.PDVC_REF_VIOL, v.PDVC_LOBD_PTR, v.SEPY_PFX, v.DEDE_PFX, v.LTLT_PFX,
       sp.SEPY_EFF_DT, sp.SEPY_TERM_DT, b.BEN_EFF_DT, b.BEN_TERM_DT, sp.SESE_ID, sp.SESE_RULE, sd.SEDS_DESC, sd.SEDS_TYPE,
       se.SESE_RULE_TYPE, se.SESE_CALC_IND, se.SESE_VALID_SEX, se.SESE_MIN_AGE, se.SESE_MAX_AGE, se.SESE_COV_TYPE,
       tr.SETR_TIER_NO, tr.SETR_ALLOW_AMT, tr.SETR_ALLOW_CTR, tr.SETR_COPAY_AMT, tr.SETR_COIN_PCT, tr.ACAC_ACC_NO,
       df.SEDF_UM_REQ_IND, df.SEDF_UM_BP_REF_IND, df.SEDF_CAP_IND
FROM dbo.CMC_PDVC_VAR_COMP v
JOIN #scope_pdpd k ON k.PDPD_ID = v.PDPD_ID
JOIN dbo.CMC_SEPY_SE_PYMT sp
  ON sp.SEPY_PFX = v.SEPY_PFX
 AND sp.SEPY_EFF_DT <= ISNULL(v.PDVC_TERM_DT, '99991231')
 AND ISNULL(sp.SEPY_TERM_DT, '99991231') >= v.PDVC_EFF_DT
CROSS APPLY (SELECT CASE WHEN sp.SEPY_EFF_DT > v.PDVC_EFF_DT THEN sp.SEPY_EFF_DT ELSE v.PDVC_EFF_DT END AS BEN_EFF_DT,
                    CASE WHEN ISNULL(sp.SEPY_TERM_DT, '99991231') < ISNULL(v.PDVC_TERM_DT, '99991231')
                         THEN ISNULL(sp.SEPY_TERM_DT, '99991231') ELSE ISNULL(v.PDVC_TERM_DT, '99991231') END AS BEN_TERM_DT) b
LEFT JOIN dbo.CMC_SESE_SERVICE se ON se.SESE_ID = sp.SESE_ID AND se.SESE_RULE = sp.SESE_RULE
LEFT JOIN dbo.CMC_SEDS_SE_DESC sd ON sd.SESE_ID = sp.SESE_ID
LEFT JOIN dbo.CMC_SETR_TIER tr ON tr.SESE_ID = sp.SESE_ID AND tr.SESE_RULE = sp.SESE_RULE
OUTER APPLY (SELECT TOP 1 f.SEDF_UM_REQ_IND, f.SEDF_UM_BP_REF_IND, f.SEDF_CAP_IND
             FROM dbo.CMC_PDBC_PROD_COMP pc
             JOIN dbo.CMC_SEDF_SE_DEFN f ON f.PDBC_PFX = pc.PDBC_PFX AND f.SESE_ID = sp.SESE_ID
             WHERE pc.PDPD_ID = v.PDPD_ID
               AND pc.PDBC_TYPE = 'SEDF'
               AND pc.PDBC_EFF_DT <= b.BEN_EFF_DT AND ISNULL(pc.PDBC_TERM_DT, '99991231') >= b.BEN_EFF_DT
               AND f.SEDF_EFF_DT <= b.BEN_EFF_DT AND ISNULL(f.SEDF_TERM_DT, '99991231') >= b.BEN_EFF_DT
             ORDER BY f.SEDF_EFF_DT DESC) df;
```

#### sdm.plan_accumulator

- **Grain:** One row per product variable component x accumulator (x limited Service ID): deductibles (ACCUM_KIND = 'DED', from DEDE) and limits / stop-loss (ACCUM_KIND = 'LMT', from LTLT + LTSE).
- **Clustered index (not unique):** PDPD_ID, ACCUM_KIND, ACAC_ACC_NO

| Column | Type | Source | 837 / use |
|---|---|---|---|
| PDPD_ID | char(8) | CMC_PDVC_VAR_COMP.PDPD_ID | Key part |
| PDVC_TIER | smallint | CMC_PDVC_VAR_COMP.PDVC_TIER | Key part; provider tier |
| PDVC_TYPE | char(1) | CMC_PDVC_VAR_COMP.PDVC_TYPE | Key part; variable component type |
| PDVC_SEQ_NO | smallint | CMC_PDVC_VAR_COMP.PDVC_SEQ_NO | Key part |
| PDVC_EFF_DT | datetime | CMC_PDVC_VAR_COMP.PDVC_EFF_DT | Key part |
| PDVC_TERM_DT | datetime | CMC_PDVC_VAR_COMP.PDVC_TERM_DT | End date |
| ACCUM_KIND | char(3) | derived | 'DED' (CMC_DEDE_DEDUCT) or 'LMT' (CMC_LTLT_LIMIT) |
| ACCUM_PFX | char(4) | derived | DEDE_PFX or LTLT_PFX |
| ACAC_ACC_NO | smallint | CMC_DEDE_DEDUCT.ACAC_ACC_NO | Key part |
| ACCUM_DESC | varchar(70) | derived | DEDE_DESC or LTLT_DESC |
| DEDE_RULE | smallint | CMC_DEDE_DEDUCT.DEDE_RULE | Deductible type |
| DEDE_PERIOD_IND | char(1) | CMC_DEDE_DEDUCT.DEDE_PERIOD_IND | Accumulation period (with CSPI_PDPD_BEG_MMDD) |
| DEDE_MEME_AMT | money | CMC_DEDE_DEDUCT.DEDE_MEME_AMT | Member deductible |
| DEDE_FAM_AMT | money | CMC_DEDE_DEDUCT.DEDE_FAM_AMT | Family deductible |
| LTLT_CAT | char(1) | CMC_LTLT_LIMIT.LTLT_CAT | Limit or stop-loss (out-of-pocket maximum) |
| LTLT_LEVEL | char(1) | CMC_LTLT_LIMIT.LTLT_LEVEL | Member / family level |
| LTLT_PERIOD_IND | char(1) | CMC_LTLT_LIMIT.LTLT_PERIOD_IND | Accumulation period |
| LTLT_RULE | char(1) | CMC_LTLT_LIMIT.LTLT_RULE | Processing method |
| LTLT_IX_IND | char(1) | CMC_LTLT_LIMIT.LTLT_IX_IND | Services include / exclude |
| LTLT_IX_TYPE | char(1) | CMC_LTLT_LIMIT.LTLT_IX_TYPE | Include/exclude by service or diagnosis |
| LTLT_AMT1 | money | CMC_LTLT_LIMIT.LTLT_AMT1 | Limit amount (or counter) |
| SESE_ID | char(4) | CMC_LTSE_SE_IX.SESE_ID | Key part; service counted toward the limit |
| LTSE_WT_CTR | int | CMC_LTSE_SE_IX.LTSE_WT_CTR | Weighted counter |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.plan_accumulator
-- Deductibles (DEDE) and limits / stop-loss (LTLT, with the Service IDs they
-- count from LTSE) reached from each product variable component.
TRUNCATE TABLE sdm.plan_accumulator;
INSERT INTO sdm.plan_accumulator WITH (TABLOCK)
    (PDPD_ID, PDVC_TIER, PDVC_TYPE, PDVC_SEQ_NO, PDVC_EFF_DT, PDVC_TERM_DT, ACCUM_KIND, ACCUM_PFX,
     ACAC_ACC_NO, ACCUM_DESC, DEDE_RULE, DEDE_PERIOD_IND, DEDE_MEME_AMT, DEDE_FAM_AMT)
SELECT v.PDPD_ID, v.PDVC_TIER, v.PDVC_TYPE, v.PDVC_SEQ_NO, v.PDVC_EFF_DT, v.PDVC_TERM_DT, 'DED', d.DEDE_PFX,
       d.ACAC_ACC_NO, d.DEDE_DESC, d.DEDE_RULE, d.DEDE_PERIOD_IND, d.DEDE_MEME_AMT, d.DEDE_FAM_AMT
FROM dbo.CMC_PDVC_VAR_COMP v
JOIN #scope_pdpd k ON k.PDPD_ID = v.PDPD_ID
JOIN dbo.CMC_DEDE_DEDUCT d ON d.DEDE_PFX = v.DEDE_PFX;

INSERT INTO sdm.plan_accumulator WITH (TABLOCK)
    (PDPD_ID, PDVC_TIER, PDVC_TYPE, PDVC_SEQ_NO, PDVC_EFF_DT, PDVC_TERM_DT, ACCUM_KIND, ACCUM_PFX,
     ACAC_ACC_NO, ACCUM_DESC, LTLT_CAT, LTLT_LEVEL, LTLT_PERIOD_IND, LTLT_RULE, LTLT_IX_IND, LTLT_IX_TYPE,
     LTLT_AMT1, SESE_ID, LTSE_WT_CTR)
SELECT v.PDPD_ID, v.PDVC_TIER, v.PDVC_TYPE, v.PDVC_SEQ_NO, v.PDVC_EFF_DT, v.PDVC_TERM_DT, 'LMT', l.LTLT_PFX,
       l.ACAC_ACC_NO, l.LTLT_DESC, l.LTLT_CAT, l.LTLT_LEVEL, l.LTLT_PERIOD_IND, l.LTLT_RULE, l.LTLT_IX_IND, l.LTLT_IX_TYPE,
       l.LTLT_AMT1, x.SESE_ID, x.LTSE_WT_CTR
FROM dbo.CMC_PDVC_VAR_COMP v
JOIN #scope_pdpd k ON k.PDPD_ID = v.PDPD_ID
JOIN dbo.CMC_LTLT_LIMIT l ON l.LTLT_PFX = v.LTLT_PFX
LEFT JOIN dbo.CMC_LTSE_SE_IX x ON x.LTLT_PFX = l.LTLT_PFX AND x.ACAC_ACC_NO = l.ACAC_ACC_NO;
```

#### sdm.service_code_map

- **Grain:** One row per conversion range: procedure ranges (MAP_KIND = 'PROC', CMC_TPCT_CPT_CONV), revenue-code ranges ('REV', CMC_RCCT_CONV) and default services ('SVC', CMC_SECT_CONV), for the TPCT_PFX values of the plans in scope. Procedure / revenue code + setting -> Service ID.
- **Clustered index (not unique):** TPCT_PFX, MAP_KIND, CODE_LOW
- **Index ix_service_code_map_sese:** (TPCT_PFX, SESE_ID) INCLUDE (MAP_KIND, MCTR_SETG, CODE_LOW, CODE_HIGH, TPCT_MODIFIER)

| Column | Type | Source | 837 / use |
|---|---|---|---|
| TPCT_PFX | char(4) | CMC_TPCT_CPT_CONV.TPCT_PFX | Key part |
| MAP_KIND | char(4) | derived | 'PROC', 'REV' or 'SVC' |
| TPCT_MCTR_TCAT | char(1) | CMC_TPCT_CPT_CONV.TPCT_MCTR_TCAT | Key part; service conversion category |
| MCTR_SETG | char(1) | derived | TPCT_MCTR_SETG (PROC) or RCCT_MCTR_SETG (REV); NULL for SVC |
| CODE_LOW | char(7) | derived | TPCT_IPCD_LOW or RCCT_RCRC_ID_LOW; NULL for SVC |
| CODE_HIGH | char(7) | derived | TPCT_IPCD_HIGH or RCCT_RCRC_ID_HIGH; NULL for SVC |
| TPCT_MODIFIER | char(2) | CMC_TPCT_CPT_CONV.TPCT_MODIFIER | Key part; modifier |
| SESE_ID | char(4) | CMC_TPCT_CPT_CONV.SESE_ID | Service ID the procedure converts to |
| CSPD_CAT | char(1) | CMC_TPCT_CPT_CONV.CSPD_CAT | Product category |
| RPTC_RPT_CAT | char(4) | CMC_TPCT_CPT_CONV.RPTC_RPT_CAT | Reporting category |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.service_code_map
-- Procedure / revenue code + setting -> Service ID, for the plans' service
-- conversion prefixes (CMC_CSCS_CLASS.TPCT_PFX). With place_of_service this
-- tells which procedures, at which POS, fall under a covered Service ID.
TRUNCATE TABLE sdm.service_code_map;
INSERT INTO sdm.service_code_map WITH (TABLOCK)
    (TPCT_PFX, MAP_KIND, TPCT_MCTR_TCAT, MCTR_SETG, CODE_LOW, CODE_HIGH, TPCT_MODIFIER, SESE_ID, CSPD_CAT, RPTC_RPT_CAT)
SELECT tp.TPCT_PFX, 'PROC', tp.TPCT_MCTR_TCAT, tp.TPCT_MCTR_SETG, tp.TPCT_IPCD_LOW, tp.TPCT_IPCD_HIGH, tp.TPCT_MODIFIER,
       tp.SESE_ID, tp.CSPD_CAT, tp.RPTC_RPT_CAT
FROM dbo.CMC_TPCT_CPT_CONV tp
JOIN (SELECT DISTINCT TPCT_PFX FROM sdm.plan_denorm WHERE TPCT_PFX IS NOT NULL) k ON k.TPCT_PFX = tp.TPCT_PFX;

INSERT INTO sdm.service_code_map WITH (TABLOCK)
    (TPCT_PFX, MAP_KIND, TPCT_MCTR_TCAT, MCTR_SETG, CODE_LOW, CODE_HIGH, SESE_ID, CSPD_CAT, RPTC_RPT_CAT)
SELECT rc.TPCT_PFX, 'REV', rc.TPCT_MCTR_TCAT, rc.RCCT_MCTR_SETG, rc.RCCT_RCRC_ID_LOW, rc.RCCT_RCRC_ID_HIGH,
       rc.SESE_ID, rc.CSPD_CAT, rc.RPTC_RPT_CAT
FROM dbo.CMC_RCCT_CONV rc
JOIN (SELECT DISTINCT TPCT_PFX FROM sdm.plan_denorm WHERE TPCT_PFX IS NOT NULL) k ON k.TPCT_PFX = rc.TPCT_PFX;

INSERT INTO sdm.service_code_map WITH (TABLOCK)
    (TPCT_PFX, MAP_KIND, SESE_ID, CSPD_CAT, RPTC_RPT_CAT)
SELECT sc.TPCT_PFX, 'SVC', sc.SESE_ID, sc.CSPD_CAT, sc.RPTC_RPT_CAT
FROM dbo.CMC_SECT_CONV sc
JOIN (SELECT DISTINCT TPCT_PFX FROM sdm.plan_denorm WHERE TPCT_PFX IS NOT NULL) k ON k.TPCT_PFX = sc.TPCT_PFX;
```

#### sdm.place_of_service

- **Grain:** One row per place of service (PSCD_ID): its setting, which selects the procedure-to-service rows in service_code_map.
- **Primary key (clustered):** PSCD_ID

| Column | Type | Source | 837 / use |
|---|---|---|---|
| PSCD_ID | char(2) | CMC_PSCD_POS_DESC.PSCD_ID | Key; 837P CLM05-1 / SV105 place of service |
| PSCD_DESC | char(70) | CMC_PSCD_POS_DESC.PSCD_DESC | Description |
| PSCD_POS_IND | char(1) | CMC_PSCD_POS_DESC.PSCD_POS_IND | Normative inpatient / outpatient / dental setting |
| TPCT_MCTR_TCAT | char(1) | CMC_PSCD_POS_DESC.TPCT_MCTR_TCAT | Service conversion category for the POS |
| TPCT_MCTR_SETG | char(1) | CMC_PSCD_POS_DESC.TPCT_MCTR_SETG | Setting: joins to CMC_TPCT_CPT_CONV.TPCT_MCTR_SETG, so the POS decides which procedure-to-service rows apply |


### Claims


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.place_of_service
-- All places of service with their setting (small reference table).
TRUNCATE TABLE sdm.place_of_service;
INSERT INTO sdm.place_of_service WITH (TABLOCK)
    (PSCD_ID, PSCD_DESC, PSCD_POS_IND, TPCT_MCTR_TCAT, TPCT_MCTR_SETG)
SELECT ps.PSCD_ID, ps.PSCD_DESC, ps.PSCD_POS_IND, ps.TPCT_MCTR_TCAT, ps.TPCT_MCTR_SETG
FROM dbo.CMC_PSCD_POS_DESC ps;
```

#### sdm.claims_header_ext_denorm

- **Grain:** One row per claim (CLCL_ID): the 2300-level and claim-provider fields claims_denorm does not carry (it repeats header fields on each line; this table does not).
- **Primary key (clustered):** CLCL_ID

| Column | Type | Source | 837 / use |
|---|---|---|---|
| CLCL_ID | char(12) | CMC_CLCL_CLAIM.CLCL_ID | Key |
| MEME_CK | int | CMC_CLCL_CLAIM.MEME_CK | Key part |
| CSCS_ID | char(4) | CMC_CLCL_CLAIM.CSCS_ID | Class (completes the CSPI key) |
| PRPR_ID | char(12) | CMC_CLCL_CLAIM.PRPR_ID | Servicing provider: 837P 2310B rendering (practitioner) or 2010AA billing (group/facility) |
| PRAD_TYPE | char(3) | CMC_CLCL_CLAIM.PRAD_TYPE | Servicing address type: 2310C service facility address (CMC_PRAD_ADDRESS) |
| CLCL_PAYEE_PR_ID | char(12) | CMC_CLCL_CLAIM.CLCL_PAYEE_PR_ID | Payee: 2010AB pay-to provider |
| NWPR_PFX | char(4) | CMC_CLCL_CLAIM.NWPR_PFX | Network-provider prefix used |
| AGAG_ID | char(12) | CMC_CLCL_CLAIM.AGAG_ID | Agreement used |
| CLCL_PA_ACCT_NO | varchar(35) | CMC_CLCL_CLAIM.CLCL_PA_ACCT_NO | CLM01 patient control number |
| CLCL_PAY_PR_IND | char(1) | CMC_CLCL_CLAIM.CLCL_PAY_PR_IND | CLM08 benefits assignment (assigned to provider) |
| CLCL_REL_INFO_IND | char(1) | CMC_CLCL_CLAIM.CLCL_REL_INFO_IND | CLM09 release of information |
| CLCL_MED_ASSGN_IND | char(1) | CMC_CLCL_CLAIM.CLCL_MED_ASSGN_IND | CLM07 Medicare assignment (no Facets function) |
| CLCL_ACD_IND | char(1) | CMC_CLCL_CLAIM.CLCL_ACD_IND | CLM11-1 related causes (accident / employment / other) |
| CLCL_ACD_STATE | char(2) | CMC_CLCL_CLAIM.CLCL_ACD_STATE | CLM11-4 auto accident state |
| CLCL_ACD_DT | datetime | CMC_CLCL_CLAIM.CLCL_ACD_DT | DTP*439 accident date |
| CLCL_CURR_ILL_DT | datetime | CMC_CLCL_CLAIM.CLCL_CURR_ILL_DT | 837P DTP*431 onset of current illness |
| CLCL_SIMI_ILL_DT | datetime | CMC_CLCL_CLAIM.CLCL_SIMI_ILL_DT | CMS-1500 box 15 other date (no single 5010 element) |
| CLCL_UNABL_FROM_DT | datetime | CMC_CLCL_CLAIM.CLCL_UNABL_FROM_DT | 837P DTP*360 disability from |
| CLCL_UNABL_TO_DT | datetime | CMC_CLCL_CLAIM.CLCL_UNABL_TO_DT | 837P DTP*361 disability to |
| CLCL_RELHP_FROM_DT | datetime | CMC_CLCL_CLAIM.CLCL_RELHP_FROM_DT | 837P DTP*435 related hospitalization admission |
| CLCL_RELHP_TO_DT | datetime | CMC_CLCL_CLAIM.CLCL_RELHP_TO_DT | 837P DTP*096 related hospitalization discharge |
| CLCL_OUT_LAB_IND | char(1) | CMC_CLCL_CLAIM.CLCL_OUT_LAB_IND | Outside lab (837P 2400 PS1 purchased service when Y) |
| CLCL_ID_ADJ_FROM | char(12) | CMC_CLCL_CLAIM.CLCL_ID_ADJ_FROM | Adjusted (original) claim: CLM05-3 = 7 and REF*F8 payer claim control number |
| CLCL_MECD_RESUB_NO | char(15) | CMC_CLCL_CLAIM.CLCL_MECD_RESUB_NO | Medicaid resubmission number: REF*F8 on Medicaid replacements |
| CLCL_OTHER_BN_IND | char(1) | CMC_CLCL_CLAIM.CLCL_OTHER_BN_IND | COB exists (informational; MECB is authoritative) |
| CLCL_EXT_REF_IND | char(1) | CMC_CLCL_CLAIM.CLCL_EXT_REF_IND | External referral indicator |
| CLM_FREQUENCY | char(1) | derived | 837P CLM05-3 derived: '7' when CLCL_ID_ADJ_FROM is set, else '1' (assumption); 837I uses CLHP_FREQUENCY |
| CLHP_FAC_TYPE | char(2) | CMC_CLHP_HOSP.CLHP_FAC_TYPE | Type of bill, facility type + classification: CLM05-1 (837I) |
| CLHP_BILL_CLASS | char(1) | CMC_CLHP_HOSP.CLHP_BILL_CLASS | Type of bill classification (setting) |
| CLHP_FREQUENCY | char(1) | CMC_CLHP_HOSP.CLHP_FREQUENCY | Type of bill frequency: CLM05-3 |
| CLHP_ADM_TYP | char(2) | CMC_CLHP_HOSP.CLHP_ADM_TYP | CL101 admission type |
| CLHP_ADM_SOURCE | char(1) | CMC_CLHP_HOSP.CLHP_ADM_SOURCE | CL102 admission source |
| CLHP_DC_STAT | char(2) | CMC_CLHP_HOSP.CLHP_DC_STAT | CL103 patient status |
| CLHP_ADM_DT | datetime | CMC_CLHP_HOSP.CLHP_ADM_DT | DTP*435 admission date |
| CLHP_ADM_HOUR_CD | char(2) | CMC_CLHP_HOSP.CLHP_ADM_HOUR_CD | DTP*435 admission hour |
| CLHP_DC_DT | datetime | CMC_CLHP_HOSP.CLHP_DC_DT | Discharge date |
| CLHP_DC_HOUR_CD | char(2) | CMC_CLHP_HOSP.CLHP_DC_HOUR_CD | DTP*096 discharge hour |
| CLHP_STAMENT_FR_DT | datetime | CMC_CLHP_HOSP.CLHP_STAMENT_FR_DT | DTP*434 statement from |
| CLHP_STAMENT_TO_DT | datetime | CMC_CLHP_HOSP.CLHP_STAMENT_TO_DT | DTP*434 statement to |
| CLHP_MED_REC_NO | char(17) | CMC_CLHP_HOSP.CLHP_MED_REC_NO | REF*EA medical record number |
| AGRG_ID | char(4) | CMC_CLHP_HOSP.AGRG_ID | System DRG |
| CLHP_INPUT_AGRG_ID | char(4) | CMC_CLHP_HOSP.CLHP_INPUT_AGRG_ID | HI*DR submitted DRG |
| CLHP_PRPR_ID_ADM | char(12) | CMC_CLHP_HOSP.CLHP_PRPR_ID_ADM | Admitting / attending physician: 837I 2310A NM1*71 |
| CLHP_PRPR_AD_NPI | varchar(10) | CMC_CLHP_HOSP.CLHP_PRPR_AD_NPI | 2310A NM109 |
| CLHP_PRPR_ID_OPER | char(12) | CMC_CLHP_HOSP.CLHP_PRPR_ID_OPER | Operating physician: 837I 2310B NM1*72 |
| CLHP_PRPR_OP_NPI | varchar(10) | CMC_CLHP_HOSP.CLHP_PRPR_OP_NPI | 2310B NM109 |
| CLHP_PRPR_ID_OTH1 | char(12) | CMC_CLHP_HOSP.CLHP_PRPR_ID_OTH1 | Other operating physician: 837I 2310C NM1*ZZ |
| CLHP_PRPR_O1_NPI | varchar(10) | CMC_CLHP_HOSP.CLHP_PRPR_O1_NPI | 2310C NM109 |
| CLHP_ICD_QUAL_IND | char(1) | CMC_CLHP_HOSP.CLHP_ICD_QUAL_IND | ICD version qualifier (ABK vs BK) |
| CLHP_BIRTH_WGT | smallint | CMC_CLHP_HOSP.CLHP_BIRTH_WGT | Value code 54 birth weight |
| CLMF_PRPR_ID_FAC | char(12) | CMC_CLMF_MULT_FUNC.CLMF_PRPR_ID_FAC | Service facility: 837P 2310C / 837I 2310E NM1*77 |
| CLMF_PRPR_FA_NPI | varchar(10) | CMC_CLMF_MULT_FUNC.CLMF_PRPR_FA_NPI | Service facility NPI |
| CLMF_PRPR_RF_NPI | varchar(10) | CMC_CLMF_MULT_FUNC.CLMF_PRPR_RF_NPI | Referring NPI: 837P 2310A / 837I 2310F NM109 |
| CLMF_INPUT_TXNM_CD | char(12) | CMC_CLMF_MULT_FUNC.CLMF_INPUT_TXNM_CD | Submitted taxonomy: PRV03 |
| CLMF_EXT_REFRAL_NO | varchar(18) | CMC_CLMF_MULT_FUNC.CLMF_EXT_REFRAL_NO | REF*9F referral number |
| CLMF_EXT_PREATH_NO | varchar(18) | CMC_CLMF_MULT_FUNC.CLMF_EXT_PREATH_NO | REF*G1 prior authorization number |
| CLMF_ICD_IND_PROC | char(1) | CMC_CLMF_MULT_FUNC.CLMF_ICD_IND_PROC | ICD version processed |
| CLSD_PRPR_ID_REND_NVL | varchar(12) | CMC_CLSD_SUPP_DATA.CLSD_PRPR_ID_REND_NVL | Claim rendering provider: 837P 2310B / 837I 2310D |
| CLSD_PRPR_RE_NPI_NVL | varchar(10) | CMC_CLSD_SUPP_DATA.CLSD_PRPR_RE_NPI_NVL | Rendering NPI |
| CLED_HIPAA_VERSION | varchar(12) | CMC_CLED_EDI_DATA.CLED_HIPAA_VERSION | Version (005010X222A1 / X223A2) |
| CLED_EXT_REF | varchar(50) | CMC_CLED_EDI_DATA.CLED_EXT_REF | External claim reference (clearinghouse REF*D9) |
| CLED_TRAD_PARTNER | char(15) | CMC_CLED_EDI_DATA.CLED_TRAD_PARTNER | Trading partner: 1000A submitter |
| CLCB_COB_TYPE | char(1) | CMC_CLCB_CL_COB.CLCB_COB_TYPE | COB type (matches CMC_MECB_COB.MECB_INSUR_TYPE) |
| CLCB_COB_AMT | money | CMC_CLCB_CL_COB.CLCB_COB_AMT | 2320 AMT*D other payer paid |
| CLCB_COB_ALLOW | money | CMC_CLCB_CL_COB.CLCB_COB_ALLOW | Other carrier allowed |
| CLCB_COB_DED_AMT | money | CMC_CLCB_CL_COB.CLCB_COB_DED_AMT | 2320 CAS PR-1 |
| CLCB_COB_COPAY_AMT | money | CMC_CLCB_CL_COB.CLCB_COB_COPAY_AMT | 2320 CAS PR-3 |
| CLCB_COB_COINS_AMT | money | CMC_CLCB_CL_COB.CLCB_COB_COINS_AMT | 2320 CAS PR-2 |
| CLCB_COB_REAS_CD | char(4) | CMC_CLCB_CL_COB.CLCB_COB_REAS_CD | Other carrier reason code |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.claims_header_ext_denorm
-- One row per claim: 2300 fields, institutional (UB) header, claim-level
-- providers (attending, operating, service facility, rendering, payee), EDI
-- and claim-level COB. CLM_FREQUENCY for 837P is derived: '7' when the claim
-- adjusts another, else '1' (assumption; voids are not identifiable).
TRUNCATE TABLE sdm.claims_header_ext_denorm;
INSERT INTO sdm.claims_header_ext_denorm WITH (TABLOCK)
    (CLCL_ID, MEME_CK, CSCS_ID, PRPR_ID, PRAD_TYPE, CLCL_PAYEE_PR_ID, NWPR_PFX, AGAG_ID,
     CLCL_PA_ACCT_NO, CLCL_PAY_PR_IND, CLCL_REL_INFO_IND, CLCL_MED_ASSGN_IND,
     CLCL_ACD_IND, CLCL_ACD_STATE, CLCL_ACD_DT, CLCL_CURR_ILL_DT, CLCL_SIMI_ILL_DT,
     CLCL_UNABL_FROM_DT, CLCL_UNABL_TO_DT, CLCL_RELHP_FROM_DT, CLCL_RELHP_TO_DT,
     CLCL_OUT_LAB_IND, CLCL_ID_ADJ_FROM, CLCL_MECD_RESUB_NO, CLCL_OTHER_BN_IND, CLCL_EXT_REF_IND, CLM_FREQUENCY,
     CLHP_FAC_TYPE, CLHP_BILL_CLASS, CLHP_FREQUENCY, CLHP_ADM_TYP, CLHP_ADM_SOURCE, CLHP_DC_STAT,
     CLHP_ADM_DT, CLHP_ADM_HOUR_CD, CLHP_DC_DT, CLHP_DC_HOUR_CD, CLHP_STAMENT_FR_DT, CLHP_STAMENT_TO_DT,
     CLHP_MED_REC_NO, AGRG_ID, CLHP_INPUT_AGRG_ID,
     CLHP_PRPR_ID_ADM, CLHP_PRPR_AD_NPI, CLHP_PRPR_ID_OPER, CLHP_PRPR_OP_NPI, CLHP_PRPR_ID_OTH1, CLHP_PRPR_O1_NPI,
     CLHP_ICD_QUAL_IND, CLHP_BIRTH_WGT,
     CLMF_PRPR_ID_FAC, CLMF_PRPR_FA_NPI, CLMF_PRPR_RF_NPI, CLMF_INPUT_TXNM_CD, CLMF_EXT_REFRAL_NO, CLMF_EXT_PREATH_NO,
     CLMF_ICD_IND_PROC, CLSD_PRPR_ID_REND_NVL, CLSD_PRPR_RE_NPI_NVL,
     CLED_HIPAA_VERSION, CLED_EXT_REF, CLED_TRAD_PARTNER,
     CLCB_COB_TYPE, CLCB_COB_AMT, CLCB_COB_ALLOW, CLCB_COB_DED_AMT, CLCB_COB_COPAY_AMT, CLCB_COB_COINS_AMT, CLCB_COB_REAS_CD)
SELECT cl.CLCL_ID, cl.MEME_CK, cl.CSCS_ID, cl.PRPR_ID, cl.PRAD_TYPE, cl.CLCL_PAYEE_PR_ID, cl.NWPR_PFX, cl.AGAG_ID,
       cl.CLCL_PA_ACCT_NO, cl.CLCL_PAY_PR_IND, cl.CLCL_REL_INFO_IND, cl.CLCL_MED_ASSGN_IND,
       cl.CLCL_ACD_IND, cl.CLCL_ACD_STATE, cl.CLCL_ACD_DT, cl.CLCL_CURR_ILL_DT, cl.CLCL_SIMI_ILL_DT,
       cl.CLCL_UNABL_FROM_DT, cl.CLCL_UNABL_TO_DT, cl.CLCL_RELHP_FROM_DT, cl.CLCL_RELHP_TO_DT,
       cl.CLCL_OUT_LAB_IND, cl.CLCL_ID_ADJ_FROM, cl.CLCL_MECD_RESUB_NO, cl.CLCL_OTHER_BN_IND, cl.CLCL_EXT_REF_IND,
       CASE WHEN NULLIF(RTRIM(cl.CLCL_ID_ADJ_FROM), '') IS NOT NULL THEN '7' ELSE '1' END,
       hp.CLHP_FAC_TYPE, hp.CLHP_BILL_CLASS, hp.CLHP_FREQUENCY, hp.CLHP_ADM_TYP, hp.CLHP_ADM_SOURCE, hp.CLHP_DC_STAT,
       hp.CLHP_ADM_DT, hp.CLHP_ADM_HOUR_CD, hp.CLHP_DC_DT, hp.CLHP_DC_HOUR_CD, hp.CLHP_STAMENT_FR_DT, hp.CLHP_STAMENT_TO_DT,
       hp.CLHP_MED_REC_NO, hp.AGRG_ID, hp.CLHP_INPUT_AGRG_ID,
       hp.CLHP_PRPR_ID_ADM, hp.CLHP_PRPR_AD_NPI, hp.CLHP_PRPR_ID_OPER, hp.CLHP_PRPR_OP_NPI, hp.CLHP_PRPR_ID_OTH1, hp.CLHP_PRPR_O1_NPI,
       hp.CLHP_ICD_QUAL_IND, hp.CLHP_BIRTH_WGT,
       mf.CLMF_PRPR_ID_FAC, mf.CLMF_PRPR_FA_NPI, mf.CLMF_PRPR_RF_NPI, mf.CLMF_INPUT_TXNM_CD, mf.CLMF_EXT_REFRAL_NO, mf.CLMF_EXT_PREATH_NO,
       mf.CLMF_ICD_IND_PROC, sd.CLSD_PRPR_ID_REND_NVL, sd.CLSD_PRPR_RE_NPI_NVL,
       ed.CLED_HIPAA_VERSION, ed.CLED_EXT_REF, ed.CLED_TRAD_PARTNER,
       cb.CLCB_COB_TYPE, cb.CLCB_COB_AMT, cb.CLCB_COB_ALLOW, cb.CLCB_COB_DED_AMT, cb.CLCB_COB_COPAY_AMT, cb.CLCB_COB_COINS_AMT, cb.CLCB_COB_REAS_CD
FROM dbo.CMC_CLCL_CLAIM cl
JOIN #scope_claim k ON k.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLHP_HOSP hp ON hp.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLMF_MULT_FUNC mf ON mf.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLSD_SUPP_DATA sd ON sd.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLED_EDI_DATA ed ON ed.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLCB_CL_COB cb ON cb.CLCL_ID = cl.CLCL_ID;
```

#### sdm.claims_detail_denorm

- **Grain:** One row per claim line (CLCL_ID, CDML_SEQ_NO): the 2400-level fields claims_denorm does not carry (modifiers, line diagnosis pointers, NDC, line rendering provider, service ID, UM links, line COB).
- **Primary key (clustered):** CLCL_ID, CDML_SEQ_NO
- **Index ix_claims_detail_sese:** (SESE_ID) INCLUDE (CLCL_ID, CDML_SEQ_NO, SEPY_PFX)

| Column | Type | Source | 837 / use |
|---|---|---|---|
| CLCL_ID | char(12) | CMC_CDML_CL_LINE.CLCL_ID | Key part |
| CDML_SEQ_NO | smallint | CMC_CDML_CL_LINE.CDML_SEQ_NO | Key part |
| CDML_EXT_LINE_NO | smallint | CMC_CDML_CL_LINE.CDML_EXT_LINE_NO | Line item 837 sequence number: LX01 |
| PROC_CODE | char(5) | derived | LEFT(IPCD_ID, 5): SV101-2 / SV202-2 |
| IPCD_MOD1 | char(2) | derived | SUBSTRING(IPCD_ID, 6, 2): SV101-3 first modifier (assumption: IPCD_ID holds code + modifier) |
| CDML_IPCD_MOD2 | char(2) | CMC_CDML_CL_LINE.CDML_IPCD_MOD2 | SV101-4 second modifier |
| CDML_IPCD_MOD3 | char(2) | CMC_CDML_CL_LINE.CDML_IPCD_MOD3 | SV101-5 third modifier |
| CDML_IPCD_MOD4 | char(2) | CMC_CDML_CL_LINE.CDML_IPCD_MOD4 | SV101-6 fourth modifier |
| IDCD_ID | varchar(10) | CMC_CDML_CL_LINE.IDCD_ID | Line diagnosis: SV107-1 pointer target |
| IDCD_ID_REL | varchar(10) | CMC_CDML_CL_LINE.IDCD_ID_REL |  |
| CDML_CLMD_TYPE2 | char(2) | CMC_CDML_CL_LINE.CDML_CLMD_TYPE2 | SV107-2 diagnosis pointer (a CLMD_TYPE) |
| CDML_CLMD_TYPE3 | char(2) | CMC_CDML_CL_LINE.CDML_CLMD_TYPE3 | SV107-3 |
| CDML_CLMD_TYPE4 | char(2) | CMC_CDML_CL_LINE.CDML_CLMD_TYPE4 | SV107-4 |
| CDML_CLMD_TYPE5 | char(2) | CMC_CDML_CL_LINE.CDML_CLMD_TYPE5 | Additional pointer (837P allows 4) |
| SESE_ID | char(4) | CMC_CDML_CL_LINE.SESE_ID | Service ID the line adjudicated under: the link to plan_benefit_denorm |
| SESE_RULE | char(3) | CMC_CDML_CL_LINE.SESE_RULE | Service rule |
| SEPY_PFX | char(4) | CMC_CDML_CL_LINE.SEPY_PFX | Service payment prefix used |
| DEDE_PFX | char(4) | CMC_CDML_CL_LINE.DEDE_PFX | Deductible prefix used |
| LTLT_PFX | char(4) | CMC_CDML_CL_LINE.LTLT_PFX | Limit prefix used |
| PDVC_LOBD_PTR | char(1) | CMC_CDML_CL_LINE.PDVC_LOBD_PTR | Primary / alternate line of business |
| CDML_ROOM_TYPE | char(2) | CMC_CDML_CL_LINE.CDML_ROOM_TYPE | Room type (837I accommodation revenue lines) |
| CDML_HCPCS_AMT | money | CMC_CDML_CL_LINE.CDML_HCPCS_AMT | Rate: 837I SV206 unit rate |
| CDML_REF_IND | char(1) | CMC_CDML_CL_LINE.CDML_REF_IND | Referral indicator |
| CDML_PC_IND | char(1) | CMC_CDML_CL_LINE.CDML_PC_IND | Pre-authorization indicator |
| CDML_UMREF_ID | char(9) | CMC_CDML_CL_LINE.CDML_UMREF_ID | Referral (UMUM_REF_ID): 2400 REF*9F |
| CDML_REFSV_SEQ_NO | smallint | CMC_CDML_CL_LINE.CDML_REFSV_SEQ_NO | Referral service sequence (UMSV_SEQ_NO) |
| CDML_UMAUTH_ID | char(9) | CMC_CDML_CL_LINE.CDML_UMAUTH_ID | Pre-authorization (UMUM_REF_ID): 2400 REF*G1 |
| CDML_AUTHSV_SEQ_NO | smallint | CMC_CDML_CL_LINE.CDML_AUTHSV_SEQ_NO | Pre-authorization service sequence (UMSV_SEQ_NO) |
| CDML_CL_NTWK_IND | char(1) | CMC_CDML_CL_LINE.CDML_CL_NTWK_IND | Line network indicator |
| CDML_DISALL_EXCD | char(3) | CMC_CDML_CL_LINE.CDML_DISALL_EXCD | Disallow explanation (2430 CAS reason source) |
| CDSD_PRPR_ID_REND | varchar(12) | CMC_CDSD_SUPP_DATA.CDSD_PRPR_ID_REND | Line rendering provider: 837P 2420A NM1*82 |
| CDSD_PRPR_RE_NPI | varchar(10) | CMC_CDSD_SUPP_DATA.CDSD_PRPR_RE_NPI | 2420A NM109 |
| CDSD_NDC_CODE | varchar(11) | CMC_CDSD_SUPP_DATA.CDSD_NDC_CODE | 2410 LIN03 NDC (LIN02 = N4) |
| CDSD_NDC_UNITS | varchar(15) | CMC_CDSD_SUPP_DATA.CDSD_NDC_UNITS | 2410 CTP04 quantity |
| CDSD_NDC_MCTR_TYPE | char(4) | CMC_CDSD_SUPP_DATA.CDSD_NDC_MCTR_TYPE | 2410 CTP05-1 unit (F2, GR, ME, ML, UN) |
| CDSD_SERV_LOC_ZIP | varchar(11) | CMC_CDSD_SUPP_DATA.CDSD_SERV_LOC_ZIP | Derived service location ZIP (2310C/2420C N403 consistency) |
| CDSD_MED_AMB_ZIP | varchar(11) | CMC_CDSD_SUPP_DATA.CDSD_MED_AMB_ZIP | 2420G ambulance pick-up N403 |
| CDCB_COB_TYPE | char(1) | CMC_CDCB_LI_COB.CDCB_COB_TYPE | COB type |
| CDCB_COB_AMT | money | CMC_CDCB_LI_COB.CDCB_COB_AMT | 2430 SVD02 other payer line paid |
| CDCB_COB_ALLOW | money | CMC_CDCB_LI_COB.CDCB_COB_ALLOW | Other payer allowed |
| CDCB_COB_DED_AMT | money | CMC_CDCB_LI_COB.CDCB_COB_DED_AMT | 2430 CAS PR-1 |
| CDCB_COB_COPAY_AMT | money | CMC_CDCB_LI_COB.CDCB_COB_COPAY_AMT | 2430 CAS PR-3 |
| CDCB_COB_COINS_AMT | money | CMC_CDCB_LI_COB.CDCB_COB_COINS_AMT | 2430 CAS PR-2 |
| CDCB_COB_REAS_CD | char(4) | CMC_CDCB_LI_COB.CDCB_COB_REAS_CD | Other carrier reason code |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.claims_detail_denorm
-- One row per claim line: 2400 fields claims_denorm lacks. PROC_CODE and
-- IPCD_MOD1 split CDML_CL_LINE.IPCD_ID (char 7) into code and first modifier
-- (assumption: IPCD holds "procedure codes including those with modifiers").
TRUNCATE TABLE sdm.claims_detail_denorm;
INSERT INTO sdm.claims_detail_denorm WITH (TABLOCK)
    (CLCL_ID, CDML_SEQ_NO, CDML_EXT_LINE_NO, PROC_CODE, IPCD_MOD1, CDML_IPCD_MOD2, CDML_IPCD_MOD3, CDML_IPCD_MOD4,
     IDCD_ID, IDCD_ID_REL, CDML_CLMD_TYPE2, CDML_CLMD_TYPE3, CDML_CLMD_TYPE4, CDML_CLMD_TYPE5,
     SESE_ID, SESE_RULE, SEPY_PFX, DEDE_PFX, LTLT_PFX, PDVC_LOBD_PTR, CDML_ROOM_TYPE, CDML_HCPCS_AMT,
     CDML_REF_IND, CDML_PC_IND, CDML_UMREF_ID, CDML_REFSV_SEQ_NO, CDML_UMAUTH_ID, CDML_AUTHSV_SEQ_NO,
     CDML_CL_NTWK_IND, CDML_DISALL_EXCD,
     CDSD_PRPR_ID_REND, CDSD_PRPR_RE_NPI, CDSD_NDC_CODE, CDSD_NDC_UNITS, CDSD_NDC_MCTR_TYPE,
     CDSD_SERV_LOC_ZIP, CDSD_MED_AMB_ZIP,
     CDCB_COB_TYPE, CDCB_COB_AMT, CDCB_COB_ALLOW, CDCB_COB_DED_AMT, CDCB_COB_COPAY_AMT, CDCB_COB_COINS_AMT, CDCB_COB_REAS_CD)
SELECT d.CLCL_ID, d.CDML_SEQ_NO, d.CDML_EXT_LINE_NO,
       CAST(LEFT(d.IPCD_ID, 5) AS char(5)), NULLIF(SUBSTRING(d.IPCD_ID, 6, 2), ''),
       d.CDML_IPCD_MOD2, d.CDML_IPCD_MOD3, d.CDML_IPCD_MOD4,
       d.IDCD_ID, d.IDCD_ID_REL, d.CDML_CLMD_TYPE2, d.CDML_CLMD_TYPE3, d.CDML_CLMD_TYPE4, d.CDML_CLMD_TYPE5,
       d.SESE_ID, d.SESE_RULE, d.SEPY_PFX, d.DEDE_PFX, d.LTLT_PFX, d.PDVC_LOBD_PTR, d.CDML_ROOM_TYPE, d.CDML_HCPCS_AMT,
       d.CDML_REF_IND, d.CDML_PC_IND, d.CDML_UMREF_ID, d.CDML_REFSV_SEQ_NO, d.CDML_UMAUTH_ID, d.CDML_AUTHSV_SEQ_NO,
       d.CDML_CL_NTWK_IND, d.CDML_DISALL_EXCD,
       sd.CDSD_PRPR_ID_REND, sd.CDSD_PRPR_RE_NPI, sd.CDSD_NDC_CODE, sd.CDSD_NDC_UNITS, sd.CDSD_NDC_MCTR_TYPE,
       sd.CDSD_SERV_LOC_ZIP, sd.CDSD_MED_AMB_ZIP,
       cb.CDCB_COB_TYPE, cb.CDCB_COB_AMT, cb.CDCB_COB_ALLOW, cb.CDCB_COB_DED_AMT, cb.CDCB_COB_COPAY_AMT, cb.CDCB_COB_COINS_AMT, cb.CDCB_COB_REAS_CD
FROM dbo.CMC_CDML_CL_LINE d
JOIN #scope_claim k ON k.CLCL_ID = d.CLCL_ID
LEFT JOIN dbo.CMC_CDSD_SUPP_DATA sd ON sd.CLCL_ID = d.CLCL_ID AND sd.CDML_SEQ_NO = d.CDML_SEQ_NO
LEFT JOIN dbo.CMC_CDCB_LI_COB cb ON cb.CLCL_ID = d.CLCL_ID AND cb.CDML_SEQ_NO = d.CDML_SEQ_NO;
```

#### sdm.claims_diag

- **Grain:** One row per claim diagnosis (CLCL_ID, CLMD_TYPE): every diagnosis, not only the primary and admitting ones in claims_denorm.
- **Primary key (clustered):** CLCL_ID, CLMD_TYPE

| Column | Type | Source | 837 / use |
|---|---|---|---|
| CLCL_ID | char(12) | CMC_CLMD_DIAG.CLCL_ID | Key part |
| CLMD_TYPE | char(2) | CMC_CLMD_DIAG.CLMD_TYPE | Key part; diagnosis position (primary, admitting, secondary...). claims_denorm only has the primary and admitting |
| IDCD_ID | varchar(10) | CMC_CLMD_DIAG.IDCD_ID | HI segments: 837P HI*ABK (principal) / ABF (other); 837I HI*ABK principal, ABJ admitting, ABF other |
| CLMD_POA_IND | char(1) | CMC_CLMD_DIAG.CLMD_POA_IND | 837I HI0n-9 present-on-admission indicator |
| IDCD_ID_SUB | varchar(10) | CMC_CLMD_DIAG.IDCD_ID_SUB | Submitted diagnosis code |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.claims_diag
-- Every diagnosis of every claim in scope (HI segments).
TRUNCATE TABLE sdm.claims_diag;
INSERT INTO sdm.claims_diag WITH (TABLOCK)
    (CLCL_ID, CLMD_TYPE, IDCD_ID, CLMD_POA_IND, IDCD_ID_SUB)
SELECT dg.CLCL_ID, dg.CLMD_TYPE, dg.IDCD_ID, dg.CLMD_POA_IND, dg.IDCD_ID_SUB
FROM dbo.CMC_CLMD_DIAG dg
JOIN #scope_claim k ON k.CLCL_ID = dg.CLCL_ID;
```

#### sdm.claims_icd_proc

- **Grain:** One row per institutional claim procedure (CLCL_ID, CLHI_TYPE): 837I HI*BBR / BBQ.
- **Primary key (clustered):** CLCL_ID, CLHI_TYPE

| Column | Type | Source | 837 / use |
|---|---|---|---|
| CLCL_ID | char(12) | CMC_CLHI_PROC.CLCL_ID | Key part |
| CLHI_TYPE | char(2) | CMC_CLHI_PROC.CLHI_TYPE | Key part; principal, secondary... position |
| IPCD_ID | char(7) | CMC_CLHI_PROC.IPCD_ID | 837I HI*BBR principal procedure / HI*BBQ other procedures (ICD-PCS) |
| CLHI_IP_DT | datetime | CMC_CLHI_PROC.CLHI_IP_DT | Procedure date (HI0n-4, D8) |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.claims_icd_proc
-- Institutional claim-level procedures (837I HI*BBR / BBQ).
TRUNCATE TABLE sdm.claims_icd_proc;
INSERT INTO sdm.claims_icd_proc WITH (TABLOCK)
    (CLCL_ID, CLHI_TYPE, IPCD_ID, CLHI_IP_DT)
SELECT hi.CLCL_ID, hi.CLHI_TYPE, hi.IPCD_ID, hi.CLHI_IP_DT
FROM dbo.CMC_CLHI_PROC hi
JOIN #scope_claim k ON k.CLCL_ID = hi.CLCL_ID;
```

#### sdm.claims_ub_code

- **Grain:** One row per UB condition, occurrence or value code (CLCL_ID, CODE_KIND, CODE_SEQ): 837I HI*BG / BH / BI / BE.
- **Primary key (clustered):** CLCL_ID, CODE_KIND, CODE_SEQ

| Column | Type | Source | 837 / use |
|---|---|---|---|
| CLCL_ID | char(12) | CMC_CLHC_COND_CODE.CLCL_ID | Key part |
| CODE_KIND | char(4) | derived | 'COND' (CLHC), 'OCC' (CLHO), 'VAL' (CLVC) |
| CODE_SEQ | smallint | derived | CLHC_SEQ_NO, CLHO_SEQ_NO, or ROW_NUMBER over CLVC_NUMBER, CLVC_LETTER |
| UB_CODE | char(2) | derived | CLHC_COND_CD / CLHO_OCC_CODE / CLVC_CODE |
| FROM_DT | datetime | derived | CLHO_OCC_FROM_DT |
| TO_DT | datetime | derived | CLHO_OCC_TO_DT (a span when present) |
| VALUE_AMT | money | derived | CLVC_AMT |
| VALUE_UNITS | smallint | derived | CLVC_VALUE |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.claims_ub_code
-- UB condition (HI*BG), occurrence (HI*BH / BI) and value (HI*BE) codes.
TRUNCATE TABLE sdm.claims_ub_code;
INSERT INTO sdm.claims_ub_code WITH (TABLOCK)
    (CLCL_ID, CODE_KIND, CODE_SEQ, UB_CODE)
SELECT hc.CLCL_ID, 'COND', hc.CLHC_SEQ_NO, hc.CLHC_COND_CD
FROM dbo.CMC_CLHC_COND_CODE hc
JOIN #scope_claim k ON k.CLCL_ID = hc.CLCL_ID;

INSERT INTO sdm.claims_ub_code WITH (TABLOCK)
    (CLCL_ID, CODE_KIND, CODE_SEQ, UB_CODE, FROM_DT, TO_DT)
SELECT ho.CLCL_ID, 'OCC', ho.CLHO_SEQ_NO, ho.CLHO_OCC_CODE, ho.CLHO_OCC_FROM_DT, ho.CLHO_OCC_TO_DT
FROM dbo.CMC_CLHO_OCC_CODE ho
JOIN #scope_claim k ON k.CLCL_ID = ho.CLCL_ID;

INSERT INTO sdm.claims_ub_code WITH (TABLOCK)
    (CLCL_ID, CODE_KIND, CODE_SEQ, UB_CODE, VALUE_AMT, VALUE_UNITS)
SELECT vc.CLCL_ID, 'VAL',
       ROW_NUMBER() OVER (PARTITION BY vc.CLCL_ID ORDER BY vc.CLVC_NUMBER, vc.CLVC_LETTER),
       vc.CLVC_CODE, vc.CLVC_AMT, vc.CLVC_VALUE
FROM dbo.CMC_CLVC_VAL_CODE vc
JOIN #scope_claim k ON k.CLCL_ID = vc.CLCL_ID;
```

#### sdm.claims_auth

- **Grain:** One row per claim line x UM link (CLCL_ID, CDML_SEQ_NO, AUTH_KIND = 'AUTH' or 'REF'): the authorized or referred service the line points to, so generated lines fall inside the authorized dates, provider, POS and units.
- **Primary key (clustered):** CLCL_ID, CDML_SEQ_NO, AUTH_KIND

| Column | Type | Source | 837 / use |
|---|---|---|---|
| CLCL_ID | char(12) | CMC_CDML_CL_LINE.CLCL_ID | Key part |
| CDML_SEQ_NO | smallint | CMC_CDML_CL_LINE.CDML_SEQ_NO | Key part |
| AUTH_KIND | char(4) | derived | 'AUTH' (CDML_UMAUTH_ID) or 'REF' (CDML_UMREF_ID) |
| UMUM_REF_ID | char(9) | CMC_UMSV_SERVICES.UMUM_REF_ID | Key part |
| UMSV_SEQ_NO | smallint | CMC_UMSV_SERVICES.UMSV_SEQ_NO | Key part |
| UMSV_FROM_DT | datetime | CMC_UMSV_SERVICES.UMSV_FROM_DT | Authorized from |
| UMSV_TO_DT | datetime | CMC_UMSV_SERVICES.UMSV_TO_DT | Authorized to |
| UMSV_PRPR_ID_REQ | char(12) | CMC_UMSV_SERVICES.UMSV_PRPR_ID_REQ | Requesting / referring provider: 2310A referring |
| UMSV_PRPR_ID_SVC | char(12) | CMC_UMSV_SERVICES.UMSV_PRPR_ID_SVC | Service provider: must equal the rendering / billing provider |
| UMSV_PRPR_ID_FAC | char(12) | CMC_UMSV_SERVICES.UMSV_PRPR_ID_FAC | Facility: service facility |
| UMSV_PSCD_ID_AUTH | char(2) | CMC_UMSV_SERVICES.UMSV_PSCD_ID_AUTH | Authorized place of service |
| IPCD_ID | char(7) | CMC_UMSV_SERVICES.IPCD_ID | Authorized procedure |
| SESE_ID | char(4) | CMC_UMSV_SERVICES.SESE_ID | Authorized Service ID |
| UMSV_UNITS_AUTH | smallint | CMC_UMSV_SERVICES.UMSV_UNITS_AUTH | Authorized units |
| UMVT_STS | char(2) | CMC_UMSV_SERVICES.UMVT_STS | Service status |
| UMSV_AUTH_DT | datetime | CMC_UMSV_SERVICES.UMSV_AUTH_DT | Approval date |


Extract (from `sdg/packs/health/sql/04_denorm_837_extract.sql`):

```sql
-- sdm.claims_auth
-- The authorized / referred service each line points to (REF*G1 / REF*9F),
-- so generated lines stay within the authorized dates, provider, POS, units.
TRUNCATE TABLE sdm.claims_auth;
INSERT INTO sdm.claims_auth WITH (TABLOCK)
    (CLCL_ID, CDML_SEQ_NO, AUTH_KIND, UMUM_REF_ID, UMSV_SEQ_NO, UMSV_FROM_DT, UMSV_TO_DT,
     UMSV_PRPR_ID_REQ, UMSV_PRPR_ID_SVC, UMSV_PRPR_ID_FAC, UMSV_PSCD_ID_AUTH, IPCD_ID, SESE_ID,
     UMSV_UNITS_AUTH, UMVT_STS, UMSV_AUTH_DT)
SELECT d.CLCL_ID, d.CDML_SEQ_NO, 'AUTH', u.UMUM_REF_ID, u.UMSV_SEQ_NO, u.UMSV_FROM_DT, u.UMSV_TO_DT,
       u.UMSV_PRPR_ID_REQ, u.UMSV_PRPR_ID_SVC, u.UMSV_PRPR_ID_FAC, u.UMSV_PSCD_ID_AUTH, u.IPCD_ID, u.SESE_ID,
       u.UMSV_UNITS_AUTH, u.UMVT_STS, u.UMSV_AUTH_DT
FROM dbo.CMC_CDML_CL_LINE d
JOIN #scope_claim k ON k.CLCL_ID = d.CLCL_ID
JOIN dbo.CMC_UMSV_SERVICES u ON u.UMUM_REF_ID = d.CDML_UMAUTH_ID AND u.UMSV_SEQ_NO = d.CDML_AUTHSV_SEQ_NO;

INSERT INTO sdm.claims_auth WITH (TABLOCK)
    (CLCL_ID, CDML_SEQ_NO, AUTH_KIND, UMUM_REF_ID, UMSV_SEQ_NO, UMSV_FROM_DT, UMSV_TO_DT,
     UMSV_PRPR_ID_REQ, UMSV_PRPR_ID_SVC, UMSV_PRPR_ID_FAC, UMSV_PSCD_ID_AUTH, IPCD_ID, SESE_ID,
     UMSV_UNITS_AUTH, UMVT_STS, UMSV_AUTH_DT)
SELECT d.CLCL_ID, d.CDML_SEQ_NO, 'REF', u.UMUM_REF_ID, u.UMSV_SEQ_NO, u.UMSV_FROM_DT, u.UMSV_TO_DT,
       u.UMSV_PRPR_ID_REQ, u.UMSV_PRPR_ID_SVC, u.UMSV_PRPR_ID_FAC, u.UMSV_PSCD_ID_AUTH, u.IPCD_ID, u.SESE_ID,
       u.UMSV_UNITS_AUTH, u.UMVT_STS, u.UMSV_AUTH_DT
FROM dbo.CMC_CDML_CL_LINE d
JOIN #scope_claim k ON k.CLCL_ID = d.CLCL_ID
JOIN dbo.CMC_UMSV_SERVICES u ON u.UMUM_REF_ID = d.CDML_UMREF_ID AND u.UMSV_SEQ_NO = d.CDML_REFSV_SEQ_NO;
```

## 6. Performance

- **Narrow, keyed tables.** Each new table holds only the columns the 837 and the consistency rules need, clustered on the join key the generator uses (PRPR_ID, MEME_CK, PDPD_ID + SESE_ID, CLCL_ID + CDML_SEQ_NO). Header facts live once per claim in `claims_header_ext_denorm`, not on every line.
- **Dated rows stay dated.** Addresses, networks, relationships, PCPs, COB, eligibility and benefits keep their EFF/TERM columns instead of being exploded per day or per claim; the generator filters with one range predicate, which the clustered keys and the listed indexes serve (`ix_provider_network_net`, `ix_plan_network_net`, `ix_plan_benefit_tier`, `ix_member_elig_plan`, `ix_member_pcp_prov`).
- **Scoped extract.** Only members, claims, plans and products that `member_denorm` / `claims_denorm` reference are loaded, plus the providers they touch and the plans' network rosters. The roster is the largest set; drop that UNION branch in the `#scope_prov` block if only already-seen providers are wanted.
- **Load pattern.** TRUNCATE + `INSERT ... WITH (TABLOCK)` for minimal logging under SIMPLE / BULK_LOGGED recovery; temp scope tables have primary keys so the joins to FACETS are key lookups. `TOP 1 ... ORDER BY` lookups (product, payer, agreement, payee, tax type) touch FACETS primary keys.
- **Optional.** Page compression on `plan_benefit_denorm`, `provider_network` and `claims_detail_denorm` if they grow large (`ALTER TABLE ... REBUILD WITH (DATA_COMPRESSION = PAGE)`).

## 7. Gaps: what the 837 needs that the dictionary does not hold

- **Billing provider is not stored on a claim.** FACETS keeps the servicing provider (CLCL.PRPR_ID) and the payee (CLCL_PAYEE_PR_ID). The 2010AA billing provider must be derived: the practitioner's group (PRER, entity G) for professional claims, the facility for institutional ones.
- **Envelope and submitter / receiver (ISA, GS, 1000A, 1000B).** Not in FACETS beyond CLED_TRAD_PARTNER; generator configuration.
- **Payer electronic ID.** 2010BB has PYPY_NATL_PLAN_ID and PYPY_NAIC_CODE but no separate clearinghouse payer ID; the other payer in 2330B has only the internal MCRE_ID (no NAIC / payer ID).
- **SBR09 claim filing indicator.** No column; derive from PDDS_PROD_TYPE (commercial, Medicare risk, Medicaid) and LOBD_MCTR_TYPE.
- **837P claim frequency (CLM05-3).** No column for professional claims; '7' is inferred from CLCL_ID_ADJ_FROM. Voids ('8') cannot be identified from the dictionary. 837I has CLHP_FREQUENCY.
- **Address history.** CMC_SBAD_ADDR has no effective dates, so member / subscriber addresses are the current ones only. Provider NPI history (CMC_PRNP_NPI) has a term date but no effective date.
- **Code values.** The dictionary defines columns but not their value lists for CLMD_TYPE (diagnosis position), CLHI_TYPE, PDBC_TYPE, PDVC_TYPE, MECB_INSUR_TYPE / ORDER, SESE_RULE_TYPE, MEPE_ELIG_IND, MCTN_TYPE, CLPE_PRPR_TYPE, PRAD_TYPE. Read them from the data (`SELECT DISTINCT`), CMC_PDPT_DESC (component types) and CMC_MCTR_CD_TRANS (user-defined codes) before relying on specific values. In particular the 837I HI qualifiers ABJ (admitting), APR (reason for visit) and ABN (external cause) depend on CLMD_TYPE values.
- **Segments with no FACETS source.** Supervising provider (837P 2310D), ordering provider (2420E), purchased service (PS1 amounts), ambulance (CR1 / CR2 beyond the pick-up ZIP), spinal manipulation, DME certificates (CR3 / CRC), home health, PWK detail (only CLCL_RAD_ENC_IND / CLCL_REC_ENC_IND attachment flags exist), NTE, K3, patient weight / pregnancy (PAT05-09).
- **Unit of measure for 837I SV204 / 837P SV103.** No column; UN by default, DA for room and board revenue codes.

## 8. Assumptions

- **Where FACETS lives.** The extract reads `dbo.CMC_*` in the current database. If FACETS is a separate database on the same server, replace `dbo.CMC_` with `<FacetsDb>.dbo.CMC_` (or create synonyms).
- **Dates.** Term dates are inclusive; a NULL term date and 9999-12-31 both mean open. "Latest overlapping row" is used where one row must be picked for a span (product and payer for a plan row, agreement for a network participation, group for a PRDS payee).
- **Eligibility value.** `MEPE_ELIG_IND = 'Y'` means eligible (the dictionary says the indicator marks eligible / non-eligible but gives no values).
- **Provider entity.** PRPR_ENTITY values P (practitioner), G (group), I (IPA), F (facility) come from the dictionary's edit criteria; NM102 = 1 for P, else 2.
- **Subscriber.** MEME_REL = 'M' is the subscriber (dictionary edit criteria).
- **Service-definition component.** `PDBC_TYPE = 'SEDF'` identifies the CMC_SEDF_SE_DEFN component of a product (inferred from CMC_CLCL_CLAIM.PDBC_PFX_SEDF); confirm in CMC_PDPT_DESC.
- **First modifier.** CMC_CDML_CL_LINE.IPCD_ID is char(7) and CMC_IPCD_PROC_CD holds "procedure codes (including those with modifiers)"; positions 1-5 are taken as the code and 6-7 as the first modifier. Modifiers 2-4 have their own columns.
- **Tax ID type.** MCTN_TYPE distinguishes EIN from SSN (dictionary text); the stored code for each must be read from the data to map to REF01 EI / SY. MCTI_ENTY_ID is taken to be the PRPR_ID for providers (no dictionary definition).
- **Payee.** Per the dictionary note on PRDS_PAY_CL_IND, the payee's PRPR_ID is the provider itself (P) or the PRER_PRPR_ID of the relationship of that entity type.
- **Member address.** A member with no own home / mailing address type uses the subscriber's home address (dictionary edit criteria on SBAD_TYPE).
- **Scope.** The extract follows the existing denorm tables: their members, claims, plans and products, the providers they reference, the providers' related groups / payees / facilities (one level), and the full rosters of the plans' networks.
- **Not extracted, documented only:** CMC_PRNP_NPI, CMC_MCTI_TAX_INFO (used only to choose the tax type), CMC_MCTR_CD_TRANS, CMC_BSBS_SUM / CMC_BSDL_DETAILS, CMC_IPCR_PROC_CRIT, CMC_IPMC_DEFN, CMC_IPCD_PROC_CD, CMC_IDCD_DIAG_CD, CMC_RCRC_RC_DESC, CMC_HFCD_FREQ_DESC, CMC_CLPE_PROV_DATA, CMC_UMUM_UTIL_MGT. Each is a straightforward add if needed.
