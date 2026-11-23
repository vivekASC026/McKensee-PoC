-- BANK: what an extract reads out of a bank's core tables (the sample bank's
-- shape, sdg/packs/banking/sample/bank_source.sql). A bank whose tables are
-- named otherwise names its joins here instead.
--
-- Every customer and every transaction is read: a branch, product, account
-- or customer missing for one leaves its columns empty rather than the row
-- out, so the store's counts are the bank's.

-- table: customer_denorm
SELECT c.CUSTOMER_ID, c.FIRST_NAME, c.LAST_NAME, c.BIRTH_DATE, c.SEGMENT, c.CUST_SINCE,
       c.ADDR_CITY, c.ADDR_STATE, c.ADDR_ZIP, c.EMAIL, c.BRANCH_ID, b.BRANCH_NAME
FROM dbo.CUSTOMER AS c
LEFT JOIN dbo.BRANCH AS b ON b.BRANCH_ID = c.BRANCH_ID;

-- table: account_denorm
-- CUST_AGE: whole years on the day of the transaction (one less where the
-- birthday has not come yet that year; DATEDIFF counts year boundaries).
SELECT t.ACCOUNT_ID, t.TXN_SEQ, a.CUSTOMER_ID, a.PRODUCT_CODE, p.PRODUCT_NAME, p.PRODUCT_TYPE,
       a.OPEN_DATE, a.ACCT_STATUS, t.TXN_DATE, t.TXN_TYPE, t.CHANNEL, t.MCC_CODE, t.AMOUNT,
       c.BRANCH_ID, b.BRANCH_NAME, c.SEGMENT,
       DATEDIFF(year, c.BIRTH_DATE, t.TXN_DATE)
         - CASE WHEN DATEADD(year, DATEDIFF(year, c.BIRTH_DATE, t.TXN_DATE), c.BIRTH_DATE) > t.TXN_DATE
                THEN 1 ELSE 0 END AS CUST_AGE
FROM dbo.TXN AS t
LEFT JOIN dbo.ACCOUNT AS a ON a.ACCOUNT_ID = t.ACCOUNT_ID
LEFT JOIN dbo.PRODUCT AS p ON p.PRODUCT_CODE = a.PRODUCT_CODE
LEFT JOIN dbo.CUSTOMER AS c ON c.CUSTOMER_ID = a.CUSTOMER_ID
LEFT JOIN dbo.BRANCH AS b ON b.BRANCH_ID = c.BRANCH_ID;
