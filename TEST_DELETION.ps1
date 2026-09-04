$ErrorActionPreference = "Stop"

Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host "LORA POS RETURNS - DELETION FUNCTIONALITY TEST" -ForegroundColor Cyan
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host ""

# Ask for connection details
Write-Host "Enter SQL Server Connection Details:" -ForegroundColor Yellow
$sqlServer = Read-Host "SQL Server name (e.g., localhost, .\SQLEXPRESS, or IP address)"
$database = Read-Host "Database name (branch database)"

if ([string]::IsNullOrWhiteSpace($sqlServer) -or [string]::IsNullOrWhiteSpace($database)) {
    Write-Host "Error: Server and database names are required" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Connecting to $sqlServer.$database..." -ForegroundColor Green

# Test connection
try {
    $connection = New-Object System.Data.SqlClient.SqlConnection
    $connection.ConnectionString = "Server=$sqlServer;Database=$database;Integrated Security=true;Encrypt=true;TrustServerCertificate=true;"
    $connection.Open()
    Write-Host "✓ Connection successful!" -ForegroundColor Green
    $connection.Close()
} catch {
    Write-Host "✗ Connection failed: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host "STEP 1: Examining Database Structure" -ForegroundColor Cyan
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host ""

# Get table structure
$query1 = @"
SELECT 
    COUNT(*) AS TotalRows,
    COUNT(DISTINCT InvoiceNum) AS UniqueInvoices,
    COUNT(DISTINCT Branch) AS UniqueBranches
FROM [dbo].[Movement];

PRINT '';
PRINT 'Sample data:';
SELECT TOP 10 
    InvoiceNum,
    Branch,
    ProductID,
    EntryNo,
    Qty
FROM [dbo].[Movement]
ORDER BY InvoiceNum DESC;
"@

try {
    $connection = New-Object System.Data.SqlClient.SqlConnection
    $connection.ConnectionString = "Server=$sqlServer;Database=$database;Integrated Security=true;Encrypt=true;TrustServerCertificate=true;"
    $connection.Open()
    
    $command = $connection.CreateCommand()
    $command.CommandText = $query1
    $command.CommandTimeout = 30
    
    $reader = $command.ExecuteReader()
    
    Write-Host "Database Statistics:" -ForegroundColor Yellow
    if ($reader.Read()) {
        $totalRows = $reader["TotalRows"]
        $uniqueInvoices = $reader["UniqueInvoices"]
        $uniqueBranches = $reader["UniqueBranches"]
        
        Write-Host "  Total Rows: $totalRows"
        Write-Host "  Unique Invoices: $uniqueInvoices"
        Write-Host "  Unique Branches: $uniqueBranches"
    }
    
    $reader.Close()
    
    # Get sample data
    Write-Host ""
    Write-Host "Sample Rows:" -ForegroundColor Yellow
    $reader = $command.ExecuteReader()
    $reader.NextResult()
    
    $count = 0
    while ($reader.Read() -and $count -lt 10) {
        $invoice = $reader["InvoiceNum"]
        $branch = $reader["Branch"]
        $product = $reader["ProductID"]
        $entry = $reader["EntryNo"]
        $qty = $reader["Qty"]
        
        Write-Host "  Invoice: $invoice | Branch: $branch | Product: $product | Entry: $entry | Qty: $qty"
        $count++
    }
    
    $reader.Close()
    $connection.Close()
} catch {
    Write-Host "Error getting database structure: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host "STEP 2: Pick a Test Invoice" -ForegroundColor Cyan
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host ""

$testInvoice = Read-Host "Enter an Invoice number to test (from the list above)"
$testBranch = Read-Host "Enter the exact Branch name (from the list above)"

if ([string]::IsNullOrWhiteSpace($testInvoice) -or [string]::IsNullOrWhiteSpace($testBranch)) {
    Write-Host "Error: Invoice and branch are required" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host "STEP 3: Testing Deletion Logic" -ForegroundColor Cyan
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Testing with: Invoice=$testInvoice | Branch=$testBranch" -ForegroundColor Yellow
Write-Host ""

# Test deletion query
$testQuery = @"
DECLARE @Invoice NVARCHAR(50) = '$testInvoice';
DECLARE @Branch NVARCHAR(100) = '$testBranch';

PRINT '=== DIAGNOSIS ===';
PRINT '';

-- Check if records exist
DECLARE @MatchCount INT;
SELECT @MatchCount = COUNT(*)
FROM [dbo].[Movement]
WHERE CAST(InvoiceNum AS nvarchar(50)) = @Invoice
  AND CAST(Branch AS nvarchar(100)) = @Branch;

PRINT 'Matching Records: ' + CAST(@MatchCount AS NVARCHAR(10));

IF @MatchCount = 0
BEGIN
    PRINT '';
    PRINT '!!! NO MATCHES FOUND - DEBUGGING !!!';
    PRINT '';
    
    -- Check if invoice exists anywhere
    DECLARE @ExistsCount INT;
    SELECT @ExistsCount = COUNT(*)
    FROM [dbo].[Movement]
    WHERE CAST(InvoiceNum AS nvarchar(50)) LIKE '%' + @Invoice + '%';
    
    PRINT 'Invoice exists in DB: ' + CASE WHEN @ExistsCount > 0 THEN 'YES (' + CAST(@ExistsCount AS NVARCHAR(10)) + ' matches)' ELSE 'NO' END;
    
    -- Check if branch exists
    DECLARE @BranchCount INT;
    SELECT @BranchCount = COUNT(DISTINCT Branch)
    FROM [dbo].[Movement]
    WHERE UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@Branch);
    
    PRINT 'Branch exists: ' + CASE WHEN @BranchCount > 0 THEN 'YES' ELSE 'NO' END;
    PRINT '';
    
    IF @BranchCount = 0
    BEGIN
        PRINT 'Available branches:';
        SELECT DISTINCT '  ' + CAST(Branch AS nvarchar(100)) AS BranchName FROM [dbo].[Movement] ORDER BY Branch;
    END
END
ELSE
BEGIN
    PRINT '';
    PRINT '✓ DELETION WILL WORK!';
    PRINT '';
    PRINT 'Records that will be deleted:';
    SELECT 
        InvoiceNum,
        Branch,
        ProductID,
        EntryNo,
        Qty
    FROM [dbo].[Movement]
    WHERE CAST(InvoiceNum AS nvarchar(50)) = @Invoice
      AND CAST(Branch AS nvarchar(100)) = @Branch
    ORDER BY EntryNo;
END
"@

try {
    $connection = New-Object System.Data.SqlClient.SqlConnection
    $connection.ConnectionString = "Server=$sqlServer;Database=$database;Integrated Security=true;Encrypt=true;TrustServerCertificate=true;"
    $connection.Open()
    
    $command = $connection.CreateCommand()
    $command.CommandText = $testQuery
    $command.CommandTimeout = 30
    
    # Execute and display results
    $reader = $command.ExecuteReader()
    
    while ($reader.Read()) {
        if ($reader.FieldCount -eq 1 -and $reader.GetName(0).ToLower().Contains("nvarchar")) {
            Write-Host $reader[0] -ForegroundColor Cyan
        }
    }
    
    $reader.Close()
    
    # Get table results if deletion will work
    $reader = $command.ExecuteReader()
    $reader.NextResult()
    
    $rowCount = 0
    while ($reader.Read()) {
        if ($rowCount -eq 0) {
            Write-Host ""
            Write-Host "Rows to delete:" -ForegroundColor Green
            Write-Host "---" 
        }
        
        $inv = $reader["InvoiceNum"]
        $bra = $reader["Branch"]
        $prod = $reader["ProductID"]
        $ent = $reader["EntryNo"]
        $qty = $reader["Qty"]
        
        Write-Host "  [$($rowCount + 1)] Invoice=$inv | Branch=$bra | Product=$prod | Entry=$ent | Qty=$qty" -ForegroundColor Green
        $rowCount++
    }
    
    $reader.Close()
    $connection.Close()
} catch {
    Write-Host "Error running test: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host "TEST COMPLETE" -ForegroundColor Cyan
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host ""

if ($rowCount -gt 0) {
    Write-Host "✓ SUCCESS: Deletion will work for this invoice!" -ForegroundColor Green
    Write-Host "  $rowCount row(s) will be deleted" -ForegroundColor Green
} else {
    Write-Host "✗ PROBLEM: No records found matching your criteria" -ForegroundColor Red
    Write-Host "  Check if:" -ForegroundColor Yellow
    Write-Host "    - Invoice number is correct (exact match, case-sensitive)" 
    Write-Host "    - Branch name is correct (exact match, case-sensitive)"
    Write-Host "    - Record hasn't already been deleted"
}
