-- ============================================================================
-- QUICK DELETION DIAGNOSTIC
-- ============================================================================
-- Use this to quickly diagnose why deletion isn't working
-- ============================================================================

-- STEP 1: What invoices do we have?
PRINT '=== ALL INVOICES IN DATABASE ===';
SELECT TOP 50
    InvoiceNum,
    Branch,
    ProductID,
    EntryNo,
    Qty
FROM [dbo].[Movement]
ORDER BY InvoiceNum DESC;

-- STEP 2: Pick one and test deletion
-- Copy an InvoiceNum from the results above and paste it below
DECLARE @TestInvoice NVARCHAR(50) = 'PASTE_INVOICE_NUMBER_HERE';
DECLARE @TestBranch NVARCHAR(100) = 'PASTE_BRANCH_HERE';
DECLARE @TestProduct INT = 0; -- Set to ProductID or leave 0 for all

PRINT '';
PRINT '=== TESTING DELETION FOR: Invoice=' + @TestInvoice + ', Branch=' + @TestBranch + ' ===';
PRINT '';

-- Show records that WILL be deleted
PRINT 'Records that will be deleted:';
SELECT 
    InvoiceNum,
    Branch,
    ProductID,
    EntryNo,
    Qty
FROM [dbo].[Movement]
WHERE CAST(InvoiceNum AS nvarchar(50)) = @TestInvoice
  AND CAST(Branch AS nvarchar(100)) = @TestBranch
  AND (@TestProduct = 0 OR ProductID = @TestProduct);

-- Count them
DECLARE @RowCount INT;
SELECT @RowCount = COUNT(*)
FROM [dbo].[Movement]
WHERE CAST(InvoiceNum AS nvarchar(50)) = @TestInvoice
  AND CAST(Branch AS nvarchar(100)) = @TestBranch
  AND (@TestProduct = 0 OR ProductID = @TestProduct);

PRINT '';
PRINT 'MATCH COUNT: ' + CAST(@RowCount AS NVARCHAR(10)) + ' rows will be deleted';
PRINT '';

-- If no matches, debug why
IF @RowCount = 0
BEGIN
    PRINT '!!! NO MATCHES FOUND - DEBUGGING !!!';
    PRINT '';
    
    -- Check if invoice exists at all
    DECLARE @ExistsAnywhere INT;
    SELECT @ExistsAnywhere = COUNT(*)
    FROM [dbo].[Movement]
    WHERE CAST(InvoiceNum AS nvarchar(50)) LIKE '%' + @TestInvoice + '%';
    
    IF @ExistsAnywhere > 0
        PRINT '✓ Invoice EXISTS somewhere in database (' + CAST(@ExistsAnywhere AS NVARCHAR(10)) + ' matches)';
    ELSE
        PRINT '✗ Invoice does NOT exist anywhere';
    
    PRINT '';
    
    -- Check if branch exists
    DECLARE @BranchExists INT;
    SELECT @BranchExists = COUNT(DISTINCT Branch)
    FROM [dbo].[Movement]
    WHERE UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@TestBranch);
    
    IF @BranchExists > 0
        PRINT '✓ Branch EXISTS ';
    ELSE
    BEGIN
        PRINT '✗ Branch does NOT exist';
        PRINT 'Available branches:';
        SELECT DISTINCT Branch FROM [dbo].[Movement] ORDER BY Branch;
    END
    
    PRINT '';
    
    -- Try different matching strategies
    PRINT 'Trying different match strategies:';
    
    SELECT 
        'Strategy' = CASE 
            WHEN InvoiceNum = @TestInvoice AND Branch = @TestBranch THEN '1: EXACT'
            WHEN CAST(InvoiceNum AS nvarchar(50)) = @TestInvoice AND CAST(Branch AS nvarchar(100)) = @TestBranch THEN '2: CAST'
            WHEN CAST(InvoiceNum AS nvarchar(50)) = @TestInvoice AND UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@TestBranch) THEN '3: CAST+UPPER'
            ELSE 'NO MATCH'
        END,
        InvoiceNum,
        Branch,
        ProductID,
        EntryNo,
        Qty
    FROM [dbo].[Movement]
    WHERE 
        (InvoiceNum = @TestInvoice OR CAST(InvoiceNum AS nvarchar(50)) = @TestInvoice)
        AND (Branch = @TestBranch OR CAST(Branch AS nvarchar(100)) = @TestBranch OR UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@TestBranch))
END
ELSE
BEGIN
    PRINT '✓ DELETION WILL WORK - ' + CAST(@RowCount AS NVARCHAR(10)) + ' rows will be deleted';
    PRINT '';
    PRINT 'Execute this to DELETE (change ROLLBACK to COMMIT):';
    PRINT '';
    PRINT 'BEGIN TRANSACTION;';
    PRINT '';
    PRINT 'DELETE FROM [dbo].[Movement]';
    PRINT 'WHERE CAST(InvoiceNum AS nvarchar(50)) = ''' + @TestInvoice + '''';
    PRINT '  AND CAST(Branch AS nvarchar(100)) = ''' + @TestBranch + '''';
    IF @TestProduct > 0
        PRINT '  AND ProductID = ' + CAST(@TestProduct AS NVARCHAR(10));
    PRINT ';';
    PRINT '';
    PRINT 'PRINT ''Deleted: '' + CAST(@@ROWCOUNT AS NVARCHAR(10)) + '' rows'';';
    PRINT '';
    PRINT 'ROLLBACK TRANSACTION; -- Change to COMMIT to actually delete';
END
