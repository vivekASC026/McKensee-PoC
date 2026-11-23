-- FACETS_QA: what an extract reads out of this source.
--
-- The joins live in the source database as sdm.member_denorm and
-- sdm.claims_denorm, which is where they were written. Selecting through them
-- keeps one definition of the denormalized shape rather than two that drift.
-- A source without such views would name its joins here instead.

-- table: member_denorm
SELECT [MEME_CK], [SBSB_CK], [GRGR_CK], [MEME_SFX], [MEME_REL], [MEME_LAST_NAME], [MEME_FIRST_NAME], [MEME_MID_INIT], [MEME_SEX], [MEME_BIRTH_DT], [MEME_SSN], [MEME_MCTR_STS], [MEME_ORIG_EFF_DT], [MEME_MARITAL_STATUS], [MEME_MEDCD_NO], [MEME_HICN], [MEME_MCTR_RACE_NVL], [MEME_MCTR_ETHN_NVL], [SBSB_ID], [SBSB_LAST_NAME], [SBSB_FIRST_NAME], [SBSB_ORIG_EFF_DT], [SBSB_MCTR_STS], [SBSB_EMPLOY_ID], [SGSG_CK], [SBSG_EFF_DT], [SBSG_TERM_DT], [SBSG_MCTR_TRSN], [SGSG_ID], [SGSG_NAME], [CSCS_ID], [SGSG_STATE], [SGSG_STS], [SGSG_ORIG_EFF_DT], [SGSG_TERM_DT], [GRGR_ID], [GRGR_NAME], [GRGR_STATE], [GRGR_COUNTY], [GRGR_STS], [GRGR_ORIG_EFF_DT], [GRGR_TERM_DT], [GRGR_MCTR_TYPE], [PAGR_CK], [CSPI_ID], [CSPD_CAT], [CSPI_EFF_DT], [CSPI_TERM_DT], [PDPD_ID], [CSPI_SEL_IND], [CSPI_HIOS_ID_NVL], [CSPD_CAT_DESC], [CSPD_TYPE], [LOBD_ID], [PDPD_RISK_IND], [PDPD_MCTR_CCAT], [PLDS_DESC], [PDDS_DESC], [EMAIL]
FROM sdm.member_denorm;

-- table: claims_denorm
SELECT [CLCL_ID], [CDML_SEQ_NO], [CLCL_CL_TYPE], [CLCL_CL_SUB_TYPE], [CLCL_CUR_STS], [CLST_MCTR_REAS], [CLCL_NTWK_IND], [CLCL_INPUT_DT], [CLCL_RECD_DT], [CLCL_PAID_DT], [CLCL_LOW_SVC_DT], [CLCL_HIGH_SVC_DT], [CLCL_TOT_CHG], [CLCL_TOT_PAYABLE], [MEME_CK], [SBSB_CK], [CLCL_ME_AGE], [MEME_SEX], [MEME_REL], [MEPE_FI], [GRGR_CK], [GRGR_NAME], [SGSG_CK], [CSPD_CAT], [CSPI_ID], [PLDS_DESC], [PDPD_ID], [PDDS_PROD_TYPE], [IDCD_ID_PRIMARY], [IDCD_DESC_PRIMARY], [IDCD_ID_ADMIT], [CLMD_DIAG_COUNT], [IPCD_ID], [IPCD_DESC], [IPCD_TYPE], [CDML_UNITS], [CDML_UNITS_ALLOW], [PSCD_ID], [RCRC_ID], [LOBD_ID], [PRPR_ID], [PRPR_NAME], [PRPR_NPI], [PRPR_MCTR_SPEC], [NWNW_ID], [CDML_CHG_AMT], [CDML_ALLOW], [CDML_PAID_AMT], [CDML_DED_AMT], [CDML_COPAY_AMT], [CDML_COINS_AMT], [CDML_DISALL_AMT], [CDML_DISC_AMT], [CDML_TOT_PA_LIAB], [CDML_FROM_DT], [CDML_TO_DT], [CDML_CUR_STS], [CLCL_EXT_AUTH_NO], [CDML_PRE_AUTH_IND], [CLCL_PRPR_ID_REF], [CLCL_PRPR_ID_PCP], [CLCL_PCP_IND]
FROM sdm.claims_denorm;

-- The provider side, into the tables sdg/packs/health/sql/03_denorm_837_tables.sql
-- defines, so the demo store has the shape a customer's has. A customer fills
-- them with sdg/packs/health/sql/04_denorm_837_extract.sql.

-- table: provider_denorm
SELECT p.PRPR_ID, p.PRPR_ENTITY,
       CASE WHEN p.PRPR_ENTITY = 'P' THEN '1' ELSE '2' END AS NM1_ENTITY_TYPE,
       p.PRPR_NPI, CAST(p.PRPR_SPECIALTY AS char(4)) AS PRCF_MCTR_SPEC,
       CAST('AC' AS char(2)) AS PRPR_STS
FROM dbo.CMC_PRPR_PROV AS p;

-- table: provider_network
SELECT r.PRPR_ID, r.NWNW_ID, r.NWPR_PFX, r.NWPR_PCP_IND,
       CAST(r.NWPR_EFF_DT AS datetime) AS NWPR_EFF_DT,
       CAST(r.NWPR_TERM_DT AS datetime) AS NWPR_TERM_DT,
       r.AGAG_ID, n.NWNW_NAME, n.NWNW_MCTR_TYPE
FROM dbo.CMC_NWPR_RELATION AS r
JOIN dbo.CMC_NWNW_NETWORK AS n ON n.NWNW_ID = r.NWNW_ID;

-- table: plan_network
SELECT s.NWST_PFX, CAST(s.NWST_EFF_DT AS datetime) AS NWST_EFF_DT, s.NWST_SEQ_NO,
       CAST(s.NWST_TERM_DT AS datetime) AS NWST_TERM_DT, s.NWNW_ID, s.NWPR_PFX,
       s.NWST_PR_NW_STS, s.PDVC_TIER, n.NWNW_NAME, n.NWNW_MCTR_TYPE
FROM dbo.CMC_NWST_NET_SET AS s
JOIN dbo.CMC_NWNW_NETWORK AS n ON n.NWNW_ID = s.NWNW_ID;

-- table: plan_denorm
SELECT DISTINCT CAST(sb.GRGR_CK AS int) AS GRGR_CK, CAST('C001' AS char(4)) AS CSCS_ID,
       p.CSPD_CAT, p.CSPI_ID, CAST(p.CSPI_EFF_DT AS datetime) AS CSPI_EFF_DT,
       CAST(p.CSPI_TERM_DT AS datetime) AS CSPI_TERM_DT, p.PDPD_ID, p.NWST_PFX,
       CAST(pl.PLDS_DESC AS char(70)) AS PLDS_DESC, pr.PDPD_TYPE AS PDDS_PROD_TYPE
FROM dbo.CMC_SBSB_SUBSC AS sb
JOIN dbo.CMC_CSPI_CS_PLAN AS p ON p.CSPI_ID = sb.CSPI_ID
JOIN dbo.CMC_PDPD_PRODUCT AS pr ON pr.PDPD_ID = p.PDPD_ID
LEFT JOIN dbo.CMC_PLDS_PLAN_DESC AS pl ON pl.CSPI_ID = p.CSPI_ID;

-- table: member_pcp
SELECT CAST(m.MEME_CK AS int) AS MEME_CK, m.MEPR_PCP_TYPE,
       CAST(m.MEPR_EFF_DT AS datetime) AS MEPR_EFF_DT,
       CAST(m.MEPR_TERM_DT AS datetime) AS MEPR_TERM_DT,
       CAST(m.GRGR_CK AS int) AS GRGR_CK, m.PRPR_ID, p.PRPR_NPI, p.PRPR_ENTITY
FROM dbo.CMC_MEPR_PRIM_PROV AS m
JOIN dbo.CMC_PRPR_PROV AS p ON p.PRPR_ID = m.PRPR_ID;
