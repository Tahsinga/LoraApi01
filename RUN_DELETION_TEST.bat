@echo off
REM ============================================================================
REM DELETION TEST HELPER
REM ============================================================================
REM This script helps you run the SQL diagnostic queries
REM ============================================================================

echo.
echo ============================================================================
echo LORA POS RETURNS - DELETION DIAGNOSTIC HELPER
echo ============================================================================
echo.

echo This script will help you diagnose why invoices are not being deleted.
echo.
echo REQUIRED: You must have sqlcmd.exe installed (comes with SQL Server)
echo.

REM Ask user for connection details
set /p SERVER=Enter SQL Server name (e.g., localhost or .\SQLEXPRESS): 
set /p DATABASE=Enter Branch Database name (e.g., BranchDB): 
set /p INVOICE=Enter an Invoice number to test (from your data): 
set /p BRANCH=Enter the Branch name (exact name from database): 

echo.
echo ============================================================================
echo STEP 1: Checking if data exists...
echo ============================================================================
echo.

sqlcmd -S %SERVER% -d %DATABASE% -Q "SELECT TOP 5 InvoiceNum, Branch, ProductID FROM [dbo].[Movement] ORDER BY InvoiceNum DESC;"

echo.
echo ============================================================================
echo STEP 2: Testing deletion with your values...
echo ============================================================================
echo Invoice: %INVOICE%
echo Branch: %BRANCH%
echo.

sqlcmd -S %SERVER% -d %DATABASE% -Q ^
"DECLARE @Invoice NVARCHAR(50) = '%INVOICE%'; ^
DECLARE @Branch NVARCHAR(100) = '%BRANCH%'; ^
PRINT 'Testing deletion for:'; ^
PRINT 'Invoice: ' + @Invoice; ^
PRINT 'Branch: ' + @Branch; ^
PRINT ''; ^
PRINT 'Records that WILL be deleted:'; ^
SELECT * FROM [dbo].[Movement] ^
WHERE CAST(InvoiceNum AS nvarchar(50)) = @Invoice ^
  AND CAST(Branch AS nvarchar(100)) = @Branch; ^
PRINT ''; ^
DECLARE @Count INT; ^
SELECT @Count = COUNT(*) FROM [dbo].[Movement] ^
WHERE CAST(InvoiceNum AS nvarchar(50)) = @Invoice ^
  AND CAST(Branch AS nvarchar(100)) = @Branch; ^
PRINT 'Total rows that will be deleted: ' + CAST(@Count AS NVARCHAR(10));"

echo.
echo ============================================================================
echo DIAGNOSTIC COMPLETE
echo ============================================================================
echo.
echo Next steps:
echo 1. If rows were found, deletion SHOULD work
echo 2. If NO rows were found, check if invoice/branch name is exactly correct
echo 3. Open QUICK_DELETE_TEST.sql in SSMS for detailed testing
echo.
pause
