"""Rebuild a branch's Quantum POS stock from the cloud copy.

If a branch PC loses its data, a fresh Quantum database can be filled again
from what the branch last synced to Lora:

* from the website (cloud database): /stock/restore/ generates a ready-to-run
  T-SQL script for one branch (``build_restore_script``);
* if the website is unreachable: ``restore_branch_from_main.sql`` runs on the
  Main computer's CloudPOS database (dbo.BranchProductCatalog, the copy of
  every branch catalog the Main sync keeps) and prints the same script.

The script is idempotent: products are matched by ProductID and the branch
stock balance is SET to the backed-up value, never added. It runs as a dry run
until @Apply is changed to 1.
"""
from decimal import Decimal
from pathlib import Path

# Quantum company id the branch POS uses (POSViewer BranchSyncDashboardForm.BranchCompanyId).
POS_COMPANY_ID = 412
VALUES_BATCH_SIZE = 500  # SQL Server allows at most 1000 rows per VALUES list

MAIN_FALLBACK_SQL = Path(__file__).resolve().parent / 'sql' / 'restore_branch_from_main.sql'

# Everything after the backed-up rows. Shared word for word with
# restore_branch_from_main.sql (a test keeps the two in sync).
RESTORE_SQL_BODY = """\
BEGIN TRANSACTION;

DECLARE @ProductsUpdated int = 0, @ProductsAdded int = 0, @StockRowsSet int = 0, @StockRowsAdded int = 0,
    @ProductsMoved int = 0, @StockRowsMoved int = 0, @Locked int;

-- 0) Lock the product and stock tables until the restore finishes, so the branch sync
--    (POSViewer) cannot add the same products halfway through.
SELECT @Locked = COUNT(*) FROM dbo.Products WITH (TABLOCKX, HOLDLOCK);
SELECT @Locked = COUNT(*) FROM dbo.ProductStockBalances WITH (TABLOCKX, HOLDLOCK);

-- 0b) Older POSViewer versions always saved products and stock under company 412. If this
--     Quantum database uses another company, move those rows to it so Quantum shows them.
IF @CompanyId <> @OldPosCompanyId
BEGIN
    UPDATE p SET p.coid = @CompanyId
    FROM dbo.Products AS p
    WHERE p.coid = @OldPosCompanyId
      AND NOT EXISTS (SELECT 1 FROM dbo.Products AS x WHERE x.coid = @CompanyId AND x.ProductCode = p.ProductCode);
    SET @ProductsMoved = @@ROWCOUNT;

    UPDATE s SET s.coid = @CompanyId
    FROM dbo.ProductStockBalances AS s
    WHERE s.coid = @OldPosCompanyId
      AND NOT EXISTS (
          SELECT 1 FROM dbo.ProductStockBalances AS x
          WHERE x.ProductID = s.ProductID AND x.branch = s.branch AND x.coid = @CompanyId);
    SET @StockRowsMoved = @@ROWCOUNT;
END

-- 1) Products that already exist: bring name, barcode, price and tax rate up to date, make them active.
UPDATE p
SET p.ProductDesc = r.ProductDesc, p.BarCode = r.BarCode, p.SellingPrice = r.SellingPrice,
    p.TaxRate = r.TaxRate, p.IsActive = 1
FROM dbo.Products AS p
JOIN #RestoreProducts AS r ON r.ProductID = p.ProductID;
SET @ProductsUpdated = @@ROWCOUNT;

-- 2) Missing products: insert them with their original ProductID (product codes are unique per company).
IF OBJECT_ID(N'tempdb..#NewProducts') IS NOT NULL DROP TABLE #NewProducts;
SELECT m.*,
    CASE WHEN m.CodeCopy > 1
              OR EXISTS (SELECT 1 FROM dbo.Products AS x WHERE x.ProductCode = m.ProductCode AND x.coid = @CompanyId)
         THEN LEFT(m.ProductCode + N'-R' + CAST(m.ProductID AS nvarchar(12)), 50)
         ELSE m.ProductCode END AS FinalCode
INTO #NewProducts
FROM (
    SELECT r.*, ROW_NUMBER() OVER (PARTITION BY r.ProductCode ORDER BY r.ProductID) AS CodeCopy
    FROM #RestoreProducts AS r
    WHERE NOT EXISTS (SELECT 1 FROM dbo.Products AS p WHERE p.ProductID = r.ProductID)
) AS m;

IF EXISTS (SELECT 1 FROM #NewProducts)
BEGIN
    IF COLUMNPROPERTY(OBJECT_ID(N'dbo.Products'), 'ProductID', 'IsIdentity') = 1
        SET IDENTITY_INSERT dbo.Products ON;
    INSERT INTO dbo.Products (
        ProductID, CatID, ProductDesc, Cost, SellingPrice, SellingPriceWholesale, WholesaleQTY,
        UnitsPerPack, ReorderLevel, DoneBy, DoneWhen, ProductCode, BarCode, Uploaded,
        TaxRate, Imported, UOM, BinLocation, IsActive, ProductDesc2, coid, SpecialPrice,
        ProductExpires, DifferentPricesPerBranch, IsIngridientOnly, PharmacyIsPrescription,
        IsUnlimitedStockItem, IsVoucher, isfavourite, isweighed, approval_audit, iseditable,
        RecordSerialNumber)
    SELECT n.ProductID, 1, n.ProductDesc, 0, n.SellingPrice, 0, 0,
        0, 0, N'LoraRestore', CONVERT(varchar(50), GETDATE(), 112), n.FinalCode, n.BarCode, 1,
        n.TaxRate, 0, 'EA', '0', 1, '', @CompanyId, NULL,
        0, 0, 0, 0,
        0, 0, 0, 0, 0, 0,
        0
    FROM #NewProducts AS n
    WHERE NOT EXISTS (SELECT 1 FROM dbo.Products AS p WHERE p.ProductID = n.ProductID);
    SET @ProductsAdded = @@ROWCOUNT;
    IF COLUMNPROPERTY(OBJECT_ID(N'dbo.Products'), 'ProductID', 'IsIdentity') = 1
        SET IDENTITY_INSERT dbo.Products OFF;
END

-- 3) Branch stock: SET each balance to the backed-up quantity (one row per product, company and branch).
UPDATE s
SET s.StockBal = r.StockBal
FROM dbo.ProductStockBalances AS s
JOIN #RestoreProducts AS r ON r.ProductID = s.ProductID
WHERE s.coid = @CompanyId AND s.branch = @Branch;
SET @StockRowsSet = @@ROWCOUNT;

INSERT INTO dbo.ProductStockBalances (ProductID, StockBal, MvtEntryNo, coid, branch, batchnumber, expirydate)
SELECT r.ProductID, r.StockBal,
    ISNULL((SELECT MAX(x.MvtEntryNo) FROM dbo.ProductStockBalances AS x WHERE x.ProductID = r.ProductID), 0) + 1,
    @CompanyId, @Branch, NULL, NULL
FROM #RestoreProducts AS r
WHERE NOT EXISTS (
    SELECT 1 FROM dbo.ProductStockBalances AS s
    WHERE s.ProductID = r.ProductID AND s.coid = @CompanyId AND s.branch = @Branch);
SET @StockRowsAdded = @@ROWCOUNT;

-- 4) Result.
SELECT @Branch AS Branch,
    @CompanyId AS CompanyId,
    CASE WHEN EXISTS (SELECT 1 FROM dbo.Branches WHERE Branch = @Branch) THEN N'yes'
         ELSE N'NO - this Quantum database has no branch with this name' END AS BranchInQuantum,
    (SELECT COUNT(*) FROM #RestoreProducts) AS ProductsInBackup,
    @ProductsMoved AS ProductsMovedToCompany,
    @StockRowsMoved AS StockRowsMovedToCompany,
    @ProductsUpdated AS ProductsUpdated,
    @ProductsAdded AS ProductsAdded,
    @StockRowsSet AS StockRowsSet,
    @StockRowsAdded AS StockRowsAdded,
    (SELECT SUM(StockBal) FROM #RestoreProducts) AS UnitsInBackup,
    (SELECT SUM(s.StockBal) FROM dbo.ProductStockBalances AS s
        WHERE s.coid = @CompanyId AND s.branch = @Branch
          AND s.ProductID IN (SELECT ProductID FROM #RestoreProducts)) AS UnitsInPosNow,
    (SELECT COUNT(*) FROM dbo.Products AS p
        WHERE p.coid = @CompanyId AND p.IsActive = 1
          AND p.ProductID IN (SELECT ProductID FROM #RestoreProducts)) AS ProductsQuantumShows,
    CASE WHEN @Apply = 1 THEN N'RESTORED - start the branch sync (POSViewer) again.'
         ELSE N'DRY RUN - nothing was saved. Set @Apply = 1 and run again to restore.' END AS Result;

IF @Apply = 1 COMMIT TRANSACTION; ELSE ROLLBACK TRANSACTION;

-- (a dry run's ROLLBACK already removed #NewProducts)
IF OBJECT_ID(N'tempdb..#NewProducts') IS NOT NULL DROP TABLE #NewProducts;
IF OBJECT_ID(N'tempdb..#RestoreProducts') IS NOT NULL DROP TABLE #RestoreProducts;
"""


def sql_text(value):
    """A T-SQL N'...' literal."""
    return "N'" + str(value if value is not None else '').replace("'", "''") + "'"


def sql_number(value):
    return format(Decimal(str(value or 0)).normalize(), 'f')


def restore_header(branch, source, generated_at, product_count, total_units, stock_as_of):
    lines = [
        '/* ==========================================================================',
        '   LORA - restore branch stock into a Quantum POS database',
        '',
        f'   Branch:       {branch}',
        f'   Source:       {source}',
        f'   Generated:    {generated_at}',
        f'   Stock as of:  {stock_as_of}',
        f'   Products:     {product_count}   (total units {sql_number(total_units)})',
        '',
        '   HOW TO USE',
        '   1. Install Quantum POS on the PC and create the empty database first.',
        '   2. Do NOT start the branch sync (POSViewer) yet.',
        '   3. Open this file in SQL Server Management Studio, connected to the',
        '      branch Quantum database (pick it in the database list at the top).',
        '   4. Run it once as it is: it is a DRY RUN and only shows what would change.',
        '   5. Change   @Apply = 0   to   @Apply = 1   below and run it again.',
        '   6. Start POSViewer. Stock transfers still queued on the website are',
        '      applied by the sync as usual.',
        '',
        '   Safe to run again: products are matched by ProductID and each branch',
        '   stock balance is SET to the backed-up quantity (never added on top).',
        '   Sales made after the branch\'s last sync are not in this backup.',
        '   ========================================================================== */',
        'SET NOCOUNT ON;',
        'SET XACT_ABORT ON;',
        '',
        'DECLARE @Apply bit = 0;                 -- 0 = dry run (nothing saved), 1 = restore',
        f'DECLARE @Branch varchar(50) = {sql_text(branch)};',
        f'DECLARE @CompanyId int = {POS_COMPANY_ID};',
        f'DECLARE @OldPosCompanyId int = {POS_COMPANY_ID};   -- company older POSViewer versions always used',
        '',
        '-- Use this Quantum database\'s own company: the branch row first, then the company record.',
        "IF OBJECT_ID(N'dbo.Branches', N'U') IS NOT NULL AND OBJECT_ID(N'dbo.Company', N'U') IS NOT NULL",
        '    SELECT TOP (1) @CompanyId = companies.companyId FROM (',
        "        SELECT 0 AS priority, CAST(Coid AS int) AS companyId FROM dbo.Branches WHERE Branch = @Branch AND Coid > 0",
        '        UNION ALL',
        '        SELECT 1, CAST(CompanyID AS int) FROM dbo.Company WHERE CompanyID > 0',
        '    ) AS companies ORDER BY companies.priority;',
        '',
        "IF OBJECT_ID(N'dbo.Products', N'U') IS NULL OR OBJECT_ID(N'dbo.ProductStockBalances', N'U') IS NULL",
        'BEGIN',
        "    RAISERROR(N'This is not a Quantum POS database (dbo.Products / dbo.ProductStockBalances not found). Select the branch database and run again.', 16, 1);",
        '    RETURN;',
        'END',
        '',
        "IF OBJECT_ID(N'tempdb..#RestoreProducts') IS NOT NULL DROP TABLE #RestoreProducts;",
        'CREATE TABLE #RestoreProducts (',
        '    ProductID int NOT NULL PRIMARY KEY,',
        '    ProductDesc nvarchar(250) NOT NULL,',
        '    ProductCode nvarchar(50) NOT NULL,',
        '    BarCode nvarchar(100) NOT NULL,',
        '    SellingPrice decimal(18,2) NOT NULL,',
        '    TaxRate decimal(18,2) NOT NULL,',
        '    StockBal decimal(18,3) NOT NULL',
        ');',
        '',
    ]
    return '\n'.join(lines) + '\n'


def restore_values(rows):
    """INSERT batches for #RestoreProducts. rows: dicts with the backed-up product fields."""
    out = []
    for start in range(0, len(rows), VALUES_BATCH_SIZE):
        batch = rows[start:start + VALUES_BATCH_SIZE]
        out.append('INSERT INTO #RestoreProducts (ProductID, ProductDesc, ProductCode, BarCode, SellingPrice, TaxRate, StockBal) VALUES')
        out.append(',\n'.join(
            '({id}, {name}, {code}, {barcode}, {price}, {tax}, {qty})'.format(
                id=int(row['product_id']),
                name=sql_text(row['product_name'][:250]),
                code=sql_text((row['product_code'] or str(int(row['product_id']) + 1))[:50]),
                barcode=sql_text((row['barcode'] or '')[:100]),
                price=sql_number(row['selling_price']),
                tax=sql_number(row['tax_rate']),
                qty=sql_number(row['quantity']),
            )
            for row in batch
        ) + ';')
        out.append('')
    return '\n'.join(out) + '\n'


def build_restore_script(branch, rows, source, generated_at, stock_as_of):
    total_units = sum((Decimal(str(row['quantity'] or 0)) for row in rows), Decimal('0'))
    return (
        restore_header(branch, source, generated_at, len(rows), total_units, stock_as_of)
        + restore_values(rows)
        + RESTORE_SQL_BODY
    )


def build_main_fallback_sql():
    """T-SQL for the Main computer's CloudPOS database that PRINTs a restore script.

    Generated from the same pieces as build_restore_script so both stay identical.
    """
    def printed(text):
        return [f'PRINT {sql_text(line)};' if line else "PRINT '';" for line in text.split('\n')[:-1]]

    header_template = restore_header('{branch}', '{source}', '{generated}', 0, 0, '{as_of}')
    body_prints = '\n'.join(printed(RESTORE_SQL_BODY))
    lines = [
        '/* ==========================================================================',
        '   LORA - make a branch restore script from the MAIN computer',
        '',
        '   Use this only when the Lora website is unreachable. Run it on the Main',
        '   computer, in the CloudPOS database (it holds dbo.BranchProductCatalog,',
        '   the copy of every branch catalog the Main sync keeps).',
        '',
        '   1. Set @Branch below to the branch name exactly as the sync uses it.',
        '   2. Easiest - from a Command Prompt on the Main computer:',
        '        sqlcmd -S .\\SQLEXPRESS -E -d CloudPOS -i restore_branch_from_main.sql -o restore_BRANCH.sql',
        '      or in SQL Server Management Studio: run it, then copy everything from',
        '      the Messages tab into a new file.',
        '   3. Run the produced restore script on the branch\'s new Quantum database',
        '      (it explains itself; it starts as a dry run).',
        '   ========================================================================== */',
        'SET NOCOUNT ON;',
        '',
        "DECLARE @Branch nvarchar(255) = N'BRANCH NAME HERE';",
        '',
        "IF OBJECT_ID(N'dbo.BranchProductCatalog', N'U') IS NULL",
        'BEGIN',
        "    RAISERROR(N'dbo.BranchProductCatalog not found - run this in the Main computer''s CloudPOS database.', 16, 1);",
        '    RETURN;',
        'END',
        'IF NOT EXISTS (SELECT 1 FROM dbo.BranchProductCatalog WHERE Branch = @Branch)',
        'BEGIN',
        "    RAISERROR(N'No products for that branch in dbo.BranchProductCatalog. Check the branch name.', 16, 1);",
        '    RETURN;',
        'END',
        '',
        'DECLARE @Count int, @Units decimal(18,3), @AsOf nvarchar(40), @Now nvarchar(40);',
        'SELECT @Count = COUNT(*), @Units = SUM(AvailableQuantity), @AsOf = CONVERT(nvarchar(40), MAX(UpdatedAt), 120) + N\' UTC\'',
        'FROM dbo.BranchProductCatalog WHERE Branch = @Branch;',
        'SET @Now = CONVERT(nvarchar(40), GETDATE(), 120);',
        '',
        '-- Header (same text as the website version).',
        'DECLARE @Header nvarchar(max) = ' + sql_text(header_template) + ';',
        "SET @Header = REPLACE(@Header, N'DECLARE @Branch varchar(50) = N''{branch}'';', N'DECLARE @Branch varchar(50) = N''' + REPLACE(@Branch, N'''', N'''''') + N''';');",
        "SET @Header = REPLACE(@Header, N'{branch}', @Branch);",
        "SET @Header = REPLACE(@Header, N'{source}', N'Main computer CloudPOS.dbo.BranchProductCatalog');",
        "SET @Header = REPLACE(@Header, N'{generated}', @Now);",
        "SET @Header = REPLACE(@Header, N'{as_of}', @AsOf);",
        "SET @Header = REPLACE(@Header, N'Products:     0   (total units 0)', N'Products:     ' + CAST(@Count AS nvarchar(12)) + N'   (total units ' + CAST(CAST(@Units AS decimal(18,0)) AS nvarchar(30)) + N')');",
        '',
        'DECLARE @Line nvarchar(max), @Rest nvarchar(max) = @Header, @Pos int;',
        'WHILE LEN(@Rest) > 0',
        'BEGIN',
        '    SET @Pos = CHARINDEX(NCHAR(10), @Rest);',
        '    IF @Pos = 0 SET @Pos = LEN(@Rest) + 1;',
        "    SET @Line = REPLACE(LEFT(@Rest, @Pos - 1), NCHAR(13), N'');  -- the .sql file may have Windows line endings",
        '    PRINT @Line;',
        '    SET @Rest = SUBSTRING(@Rest, @Pos + 1, 2147483647);',
        'END',
        '',
        '-- Backed-up products, 500 per INSERT.',
        'DECLARE @Rn int, @Id int, @Name nvarchar(250), @Code nvarchar(50), @Bar nvarchar(100), @Price decimal(18,2), @Tax decimal(18,2), @Qty decimal(18,3), @Total int;',
        'SET @Total = @Count;',
        'DECLARE products CURSOR LOCAL FAST_FORWARD FOR',
        '    SELECT ROW_NUMBER() OVER (ORDER BY ProductID), ProductID, LEFT(ProductDesc, 250),',
        '        LEFT(COALESCE(NULLIF(LTRIM(RTRIM(ProductCode)), N\'\'), CAST(ProductID + 1 AS nvarchar(12))), 50),',
        "        LEFT(COALESCE(BarCode, N''), 100), SellingPrice, TaxRate, AvailableQuantity",
        '    FROM dbo.BranchProductCatalog WHERE Branch = @Branch;',
        'OPEN products;',
        'FETCH NEXT FROM products INTO @Rn, @Id, @Name, @Code, @Bar, @Price, @Tax, @Qty;',
        'WHILE @@FETCH_STATUS = 0',
        'BEGIN',
        f'    IF (@Rn - 1) % {VALUES_BATCH_SIZE} = 0',
        "        PRINT N'INSERT INTO #RestoreProducts (ProductID, ProductDesc, ProductCode, BarCode, SellingPrice, TaxRate, StockBal) VALUES';",
        "    PRINT N'(' + CAST(@Id AS nvarchar(12))",
        "        + N', N''' + REPLACE(@Name, N'''', N'''''') + N''''",
        "        + N', N''' + REPLACE(@Code, N'''', N'''''') + N''''",
        "        + N', N''' + REPLACE(@Bar, N'''', N'''''') + N''''",
        "        + N', ' + CAST(@Price AS nvarchar(30)) + N', ' + CAST(@Tax AS nvarchar(30)) + N', ' + CAST(@Qty AS nvarchar(30)) + N')'",
        f"        + CASE WHEN @Rn % {VALUES_BATCH_SIZE} = 0 OR @Rn = @Total THEN N';' ELSE N',' END;",
        f"    IF @Rn % {VALUES_BATCH_SIZE} = 0 OR @Rn = @Total PRINT '';",
        '    FETCH NEXT FROM products INTO @Rn, @Id, @Name, @Code, @Bar, @Price, @Tax, @Qty;',
        'END',
        'CLOSE products;',
        'DEALLOCATE products;',
        '',
        '-- Restore steps (same text as the website version).',
        body_prints,
        '',
    ]
    return '\n'.join(lines)
