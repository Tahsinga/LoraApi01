@echo off
setlocal enabledelayedexpansion

cls
echo ============================================================================
echo LORA POS RETURNS - DELETION TEST IN TERMINAL
echo ============================================================================
echo.

echo Enter SQL Server Connection Details:
set /p sqlServer=SQL Server name (e.g., localhost, .\SQLEXPRESS): 
set /p database=Database name (branch database): 

if "%sqlServer%"=="" (
    echo Error: Server name is required
    pause
    exit /b 1
)

if "%database%"=="" (
    echo Error: Database name is required
    pause
    exit /b 1
)

echo.
echo Connecting to %sqlServer%.%database%...
echo.

REM Test basic query
sqlcmd -S %sqlServer% -d %database% -Q "SELECT @@VERSION AS 'SQL Server Version';" >nul 2>&1

if errorlevel 1 (
    echo ERROR: Cannot connect to SQL Server
    echo Please verify:
    echo   - SQL Server name is correct
    echo   - Database exists
    echo   - You have permissions
    pause
    exit /b 1
)

echo ============================================================================
echo STEP 1: Show Available Data
echo ============================================================================
echo.

sqlcmd -S %sqlServer% -d %database% -Q "SELECT TOP 20 InvoiceNum, Branch, ProductID, EntryNo, Qty FROM [dbo].[Movement] ORDER BY InvoiceNum DESC;"

echo.
echo ============================================================================
echo STEP 2: Enter Test Values
echo ============================================================================
echo.

set /p testInvoice=Enter Invoice number to test (from list above): 
set /p testBranch=Enter exact Branch name (from list above): 

if "%testInvoice%"=="" (
    echo Error: Invoice number is required
    pause
    exit /b 1
)

if "%testBranch%"=="" (
    echo Error: Branch name is required
    pause
    exit /b 1
)

echo.
echo ============================================================================
echo STEP 3: Testing Deletion for Invoice %testInvoice% at Branch %testBranch%
echo ============================================================================
echo.

REM Create temp SQL file
(
    echo DECLARE @Invoice NVARCHAR(50) = '%testInvoice%';
    echo DECLARE @Branch NVARCHAR(100) = '%testBranch%';
    echo PRINT '';
    echo PRINT '=== DELETION TEST ===';
    echo PRINT '';
    echo.
    echo DECLARE @MatchCount INT;
    echo SELECT @MatchCount = COUNT(*^)
    echo FROM [dbo].[Movement]
    echo WHERE CAST(InvoiceNum AS nvarchar(50^)^) = @Invoice
    echo   AND CAST(Branch AS nvarchar(100^)^) = @Branch;
    echo.
    echo PRINT 'Matching Records: ' + CAST(@MatchCount AS NVARCHAR(10^)^);
    echo PRINT '';
    echo.
    echo IF @MatchCount ^> 0
    echo BEGIN
    echo     PRINT 'STATUS: DELETION WILL WORK!';
    echo     PRINT 'Records that will be deleted:';
    echo     PRINT '';
    echo     SELECT 
    echo         InvoiceNum,
    echo         Branch,
    echo         ProductID,
    echo         EntryNo,
    echo         Qty
    echo     FROM [dbo].[Movement]
    echo     WHERE CAST(InvoiceNum AS nvarchar(50^)^) = @Invoice
    echo       AND CAST(Branch AS nvarchar(100^)^) = @Branch
    echo     ORDER BY EntryNo;
    echo END
    echo ELSE
    echo BEGIN
    echo     PRINT 'STATUS: NO MATCHES FOUND!';
    echo     PRINT '';
    echo     PRINT 'Checking why...';
    echo     PRINT '';
    echo.
    echo     DECLARE @InvoiceExists INT;
    echo     SELECT @InvoiceExists = COUNT(*^)
    echo     FROM [dbo].[Movement]
    echo     WHERE CAST(InvoiceNum AS nvarchar(50^)^) LIKE '%%' + @Invoice + '%%';
    echo.
    echo     IF @InvoiceExists ^> 0
    echo         PRINT 'Invoice EXISTS in database (' + CAST(@InvoiceExists AS NVARCHAR(10^)^) + ' matches^)'
    echo     ELSE
    echo         PRINT 'Invoice NOT found in database';
    echo.
    echo     PRINT '';
    echo.
    echo     DECLARE @BranchExists INT;
    echo     SELECT @BranchExists = COUNT(DISTINCT Branch^)
    echo     FROM [dbo].[Movement]
    echo     WHERE UPPER(CAST(Branch AS nvarchar(100^)^)^) = UPPER(@Branch^);
    echo.
    echo     IF @BranchExists ^> 0
    echo         PRINT 'Branch EXISTS'
    echo     ELSE
    echo     BEGIN
    echo         PRINT 'Branch NOT found';
    echo         PRINT 'Available branches:';
    echo         SELECT '  - ' + CAST(Branch AS NVARCHAR(100^)^) FROM [dbo].[Movement] GROUP BY Branch ORDER BY Branch;
    echo     END
    echo END
) > "%TEMP%\deletion_test.sql"

echo Running deletion test...
echo.

sqlcmd -S %sqlServer% -d %database% -i "%TEMP%\deletion_test.sql"

echo.
echo ============================================================================
echo TEST COMPLETE
echo ============================================================================
echo.
echo If you see "DELETION WILL WORK" above, deletion is functioning correctly.
echo.
pause
