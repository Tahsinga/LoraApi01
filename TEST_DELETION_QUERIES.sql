-- ============================================================================
-- LORA POS RETURNS - DELETION TEST QUERIES
-- ============================================================================
-- Run these queries against your branch database to test deletion logic
-- Replace [branch_database_name] with your actual branch database name
-- ============================================================================

-- ============================================================================
-- STEP 1: EXAMINE THE DATA STRUCTURE
-- ============================================================================

-- Check if Movement table exists and see its structure
SELECT TOP 1 * FROM [dbo].[Movement];

-- Get column data types
SELECT 
    COLUMN_NAME, 
    DATA_TYPE, 
    CHARACTER_MAXIMUM_LENGTH,
    IS_NULLABLE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME = 'Movement'
ORDER BY ORDINAL_POSITION;

-- ============================================================================
-- STEP 2: VIEW SAMPLE DATA TO UNDERSTAND WHAT WE'RE DELETING
-- ============================================================================

-- Show last 20 invoices
SELECT TOP 20 
    InvoiceNum,
    Branch,
    ProductID,
    EntryNo,
    Qty,
    CAST(InvoiceNum AS nvarchar(50)) AS InvoiceNum_AsString,
    CAST(Branch AS nvarchar(100)) AS Branch_AsString,
    CAST(EntryNo AS nvarchar(50)) AS EntryNo_AsString
FROM [dbo].[Movement]
ORDER BY InvoiceNum DESC;

-- Count total records
SELECT COUNT(*) AS TotalMovements FROM [dbo].[Movement];

-- Show invoices by branch
SELECT 
    Branch, 
    COUNT(*) AS RecordCount,
    COUNT(DISTINCT InvoiceNum) AS UniqueInvoices
FROM [dbo].[Movement]
GROUP BY Branch
ORDER BY RecordCount DESC;

-- ============================================================================
-- STEP 3: PICK A TEST INVOICE AND VERIFY DATA
-- ============================================================================

-- GET A SAMPLE INVOICE TO TEST (run this first, then copy the values below)
DECLARE @TestInvoiceNum NVARCHAR(50);
DECLARE @TestBranch NVARCHAR(100);

SELECT TOP 1 
    @TestInvoiceNum = CAST(InvoiceNum AS nvarchar(50)),
    @TestBranch = CAST(Branch AS nvarchar(100))
FROM [dbo].[Movement]
ORDER BY InvoiceNum DESC;

PRINT '=== TEST VALUES ===';
PRINT 'Invoice: ' + ISNULL(@TestInvoiceNum, 'NULL');
PRINT 'Branch: ' + ISNULL(@TestBranch, 'NULL');

-- Show all rows for this invoice
SELECT 
    InvoiceNum,
    ProductID,
    Branch,
    EntryNo,
    Qty,
    'ROW_COUNT' = ROW_NUMBER() OVER (ORDER BY EntryNo)
FROM [dbo].[Movement]
WHERE CAST(InvoiceNum AS nvarchar(50)) = @TestInvoiceNum
  AND CAST(Branch AS nvarchar(100)) = @TestBranch
ORDER BY EntryNo;

-- ============================================================================
-- STEP 4: TEST DELETION STRATEGY 1 (EXACT STRING MATCH)
-- ============================================================================

PRINT '=== STRATEGY 1: EXACT STRING MATCH ===';

DECLARE @Invoice1 NVARCHAR(50) = '123';  -- <-- CHANGE THIS TO YOUR TEST INVOICE
DECLARE @Branch1 NVARCHAR(100) = 'Store-A';  -- <-- CHANGE THIS TO YOUR TEST BRANCH
DECLARE @ProductId1 INT = 5;  -- <-- CHANGE THIS TO YOUR TEST PRODUCT

-- First, show what would be deleted
SELECT 
    'WOULD_DELETE' AS Action,
    InvoiceNum,
    ProductID,
    Branch,
    EntryNo,
    Qty
FROM [dbo].[Movement]
WHERE InvoiceNum = @Invoice1
  AND Branch = @Branch1
  AND ProductID = @ProductId1;

-- Count how many rows match
DECLARE @Count1 INT;
SELECT @Count1 = COUNT(*)
FROM [dbo].[Movement]
WHERE InvoiceNum = @Invoice1
  AND Branch = @Branch1
  AND ProductID = @ProductId1;

PRINT 'Match count: ' + CAST(@Count1 AS NVARCHAR(10));

-- ============================================================================
-- STEP 5: TEST DELETION STRATEGY 2 (CAST STRING MATCH)
-- ============================================================================

PRINT '=== STRATEGY 2: CAST TO STRING MATCH ===';

-- Show what would be deleted with CAST
SELECT 
    'WOULD_DELETE' AS Action,
    InvoiceNum,
    ProductID,
    Branch,
    EntryNo,
    Qty
FROM [dbo].[Movement]
WHERE CAST(InvoiceNum AS nvarchar(50)) = @Invoice1
  AND CAST(Branch AS nvarchar(100)) = @Branch1
  AND ProductID = @ProductId1;

DECLARE @Count2 INT;
SELECT @Count2 = COUNT(*)
FROM [dbo].[Movement]
WHERE CAST(InvoiceNum AS nvarchar(50)) = @Invoice1
  AND CAST(Branch AS nvarchar(100)) = @Branch1
  AND ProductID = @ProductId1;

PRINT 'Match count: ' + CAST(@Count2 AS NVARCHAR(10));

-- ============================================================================
-- STEP 6: TEST DELETION STRATEGY 3 (CASE-INSENSITIVE BRANCH)
-- ============================================================================

PRINT '=== STRATEGY 3: CASE-INSENSITIVE BRANCH MATCH ===';

SELECT 
    'WOULD_DELETE' AS Action,
    InvoiceNum,
    ProductID,
    Branch,
    EntryNo,
    Qty
FROM [dbo].[Movement]
WHERE CAST(InvoiceNum AS nvarchar(50)) = @Invoice1
  AND UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@Branch1)
  AND ProductID = @ProductId1;

DECLARE @Count3 INT;
SELECT @Count3 = COUNT(*)
FROM [dbo].[Movement]
WHERE CAST(InvoiceNum AS nvarchar(50)) = @Invoice1
  AND UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@Branch1)
  AND ProductID = @ProductId1;

PRINT 'Match count: ' + CAST(@Count3 AS NVARCHAR(10));

-- ============================================================================
-- STEP 7: ACTUAL DELETION TEST (USES TRANSACTION - ROLLBACK AT END)
-- ============================================================================

PRINT '=== TESTING ACTUAL DELETION (WITH ROLLBACK) ===';

BEGIN TRANSACTION;

-- Show before count
DECLARE @BeforeCount INT;
SELECT @BeforeCount = COUNT(*) FROM [dbo].[Movement];
PRINT 'Before deletion: ' + CAST(@BeforeCount AS NVARCHAR(10)) + ' rows';

-- Execute deletion
DELETE FROM [dbo].[Movement]
WHERE CAST(InvoiceNum AS nvarchar(50)) = @Invoice1
  AND CAST(Branch AS nvarchar(100)) = @Branch1
  AND ProductID = @ProductId1;

DECLARE @DeletedRows INT = @@ROWCOUNT;
PRINT 'Deleted rows: ' + CAST(@DeletedRows AS NVARCHAR(10));

-- Show after count
DECLARE @AfterCount INT;
SELECT @AfterCount = COUNT(*) FROM [dbo].[Movement];
PRINT 'After deletion: ' + CAST(@AfterCount AS NVARCHAR(10)) + ' rows';

-- ROLLBACK - undo the deletion so you can test again
ROLLBACK TRANSACTION;

PRINT 'TRANSACTION ROLLED BACK - Data restored';

-- Verify data is restored
DECLARE @RestoreCount INT;
SELECT @RestoreCount = COUNT(*) FROM [dbo].[Movement];
PRINT 'After rollback: ' + CAST(@RestoreCount AS NVARCHAR(10)) + ' rows';

-- ============================================================================
-- STEP 8: CHECK IF DELETION ISSUE IS DATA TYPE MISMATCH
-- ============================================================================

PRINT '=== CHECKING DATA TYPE ISSUES ===';

-- Show InvoiceNum data types
SELECT 
    'InvoiceNum Details' AS Aspect,
    SQL_VARIANT_PROPERTY(InvoiceNum, 'BaseType') AS DataType,
    COUNT(*) AS Count
FROM [dbo].[Movement]
GROUP BY SQL_VARIANT_PROPERTY(InvoiceNum, 'BaseType');

-- Show sample InvoiceNum values with their actual types
SELECT TOP 10
    InvoiceNum,
    SQL_VARIANT_PROPERTY(InvoiceNum, 'BaseType') AS ActualType,
    LEN(CAST(InvoiceNum AS nvarchar(50))) AS StringLength
FROM [dbo].[Movement]
ORDER BY InvoiceNum DESC;

-- Check for whitespace or hidden characters
SELECT TOP 10
    '[' + CAST(InvoiceNum AS nvarchar(50)) + ']' AS InvoiceWithBrackets,
    LEN(CAST(InvoiceNum AS nvarchar(50))) AS Length,
    ASCII(SUBSTRING(CAST(InvoiceNum AS nvarchar(50)), 1, 1)) AS FirstCharASCII
FROM [dbo].[Movement]
ORDER BY InvoiceNum DESC;

-- ============================================================================
-- STEP 9: IF DATA EXISTS, SHOW WHERE THE RECORDS ARE
-- ============================================================================

PRINT '=== VERIFY DATA AFTER TEST ===';

SELECT 
    'REMAINING' AS Status,
    Branch,
    COUNT(*) AS RecordCount,
    'Samples: ' + STRING_AGG(CAST(InvoiceNum AS nvarchar(50)), ', ') AS SampleInvoices
FROM [dbo].[Movement]
GROUP BY Branch
ORDER BY RecordCount DESC;

-- ============================================================================
-- INSTRUCTIONS:
-- ============================================================================
-- 1. Replace @Invoice1, @Branch1, @ProductId1 with actual values from your test data
-- 2. Run each STEP individually to see results
-- 3. STEP 7 uses ROLLBACK so it won't actually delete data
-- 4. Once you confirm the query matches and deletes correctly, 
--    change ROLLBACK to COMMIT to actually delete
-- 5. Share the results of STEP 2 and STEP 3 to diagnose deletion issues
-- ============================================================================
