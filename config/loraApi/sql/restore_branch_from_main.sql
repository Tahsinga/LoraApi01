/* ==========================================================================
   LORA - make a branch restore script from the MAIN computer

   Use this only when the Lora website is unreachable. Run it on the Main
   computer, in the CloudPOS database (it holds dbo.BranchProductCatalog,
   the copy of every branch catalog the Main sync keeps).

   1. Set @Branch below to the branch name exactly as the sync uses it.
   2. Easiest - from a Command Prompt on the Main computer:
        sqlcmd -S .\SQLEXPRESS -E -d CloudPOS -i restore_branch_from_main.sql -o restore_BRANCH.sql
      or in SQL Server Management Studio: run it, then copy everything from
      the Messages tab into a new file.
   3. Run the produced restore script on the branch's new Quantum database
      (it explains itself; it restores when you press Execute).
   ========================================================================== */
SET NOCOUNT ON;

DECLARE @Branch nvarchar(255) = N'BRANCH NAME HERE';

IF OBJECT_ID(N'dbo.BranchProductCatalog', N'U') IS NULL
BEGIN
    RAISERROR(N'dbo.BranchProductCatalog not found - run this in the Main computer''s CloudPOS database.', 16, 1);
    RETURN;
END
IF NOT EXISTS (SELECT 1 FROM dbo.BranchProductCatalog WHERE Branch = @Branch)
BEGIN
    RAISERROR(N'No products for that branch in dbo.BranchProductCatalog. Check the branch name.', 16, 1);
    RETURN;
END

DECLARE @Count int, @Units decimal(18,3), @AsOf nvarchar(40), @Now nvarchar(40);
SELECT @Count = COUNT(*), @Units = SUM(AvailableQuantity), @AsOf = CONVERT(nvarchar(40), MAX(UpdatedAt), 120) + N' UTC'
FROM dbo.BranchProductCatalog WHERE Branch = @Branch;
SET @Now = CONVERT(nvarchar(40), GETDATE(), 120);

-- Header (same text as the website version).
DECLARE @Header nvarchar(max) = N'/* ==========================================================================
   LORA - restore branch stock into a Quantum POS database

   Branch:       {branch}
   Source:       {source}
   Generated:    {generated}
   Stock as of:  {as_of}
   Products:     0   (total units 0)

   HOW TO USE
   1. Install Quantum POS on the PC and create the empty database first.
   2. Do NOT start the branch sync (POSViewer) yet.
   3. Open this file in SQL Server Management Studio, connected to the
      branch Quantum database (pick it in the database list at the top).
   4. Press Execute once. It RESTORES straight away and shows the totals.
      (To only preview first, change @Apply = 1 to @Apply = 0 below.)
   5. Start POSViewer. Stock transfers still queued on the website are
      applied by the sync as usual.

   Safe to run again: products are matched by ProductID and each branch
   stock balance is SET to the backed-up quantity (never added on top).
   Sales made after the branch''s last sync are not in this backup.
   ========================================================================== */
SET NOCOUNT ON;
SET XACT_ABORT ON;

DECLARE @Apply bit = 1;                 -- 1 = restore, 0 = dry run (nothing saved)
DECLARE @Branch varchar(50) = N''{branch}'';
DECLARE @CompanyId int = 412;
DECLARE @OldPosCompanyId int = 412;   -- company older POSViewer versions always used

-- Use this Quantum database''s own company: the branch row first, then the company record.
IF OBJECT_ID(N''dbo.Branches'', N''U'') IS NOT NULL AND OBJECT_ID(N''dbo.Company'', N''U'') IS NOT NULL
    SELECT TOP (1) @CompanyId = companies.companyId FROM (
        SELECT 0 AS priority, CAST(Coid AS int) AS companyId FROM dbo.Branches WHERE Branch = @Branch AND Coid > 0
        UNION ALL
        SELECT 1, CAST(CompanyID AS int) FROM dbo.Company WHERE CompanyID > 0
    ) AS companies ORDER BY companies.priority;

IF OBJECT_ID(N''dbo.Products'', N''U'') IS NULL OR OBJECT_ID(N''dbo.ProductStockBalances'', N''U'') IS NULL
BEGIN
    RAISERROR(N''This is not a Quantum POS database (dbo.Products / dbo.ProductStockBalances not found). Select the branch database and run again.'', 16, 1);
    RETURN;
END

IF OBJECT_ID(N''tempdb..#RestoreProducts'') IS NOT NULL DROP TABLE #RestoreProducts;
CREATE TABLE #RestoreProducts (
    ProductID int NOT NULL PRIMARY KEY,
    ProductDesc nvarchar(250) NOT NULL,
    ProductCode nvarchar(50) NOT NULL,
    BarCode nvarchar(100) NOT NULL,
    SellingPrice decimal(18,2) NOT NULL,
    TaxRate decimal(18,2) NOT NULL,
    StockBal decimal(18,3) NOT NULL
);

';
SET @Header = REPLACE(@Header, N'DECLARE @Branch varchar(50) = N''{branch}'';', N'DECLARE @Branch varchar(50) = N''' + REPLACE(@Branch, N'''', N'''''') + N''';');
SET @Header = REPLACE(@Header, N'{branch}', @Branch);
SET @Header = REPLACE(@Header, N'{source}', N'Main computer CloudPOS.dbo.BranchProductCatalog');
SET @Header = REPLACE(@Header, N'{generated}', @Now);
SET @Header = REPLACE(@Header, N'{as_of}', @AsOf);
SET @Header = REPLACE(@Header, N'Products:     0   (total units 0)', N'Products:     ' + CAST(@Count AS nvarchar(12)) + N'   (total units ' + CAST(CAST(@Units AS decimal(18,0)) AS nvarchar(30)) + N')');

DECLARE @Line nvarchar(max), @Rest nvarchar(max) = @Header, @Pos int;
WHILE LEN(@Rest) > 0
BEGIN
    SET @Pos = CHARINDEX(NCHAR(10), @Rest);
    IF @Pos = 0 SET @Pos = LEN(@Rest) + 1;
    SET @Line = REPLACE(LEFT(@Rest, @Pos - 1), NCHAR(13), N'');  -- the .sql file may have Windows line endings
    PRINT @Line;
    SET @Rest = SUBSTRING(@Rest, @Pos + 1, 2147483647);
END

-- Backed-up products, 500 per INSERT.
DECLARE @Rn int, @Id int, @Name nvarchar(250), @Code nvarchar(50), @Bar nvarchar(100), @Price decimal(18,2), @Tax decimal(18,2), @Qty decimal(18,3), @Total int;
SET @Total = @Count;
DECLARE products CURSOR LOCAL FAST_FORWARD FOR
    SELECT ROW_NUMBER() OVER (ORDER BY ProductID), ProductID, LEFT(ProductDesc, 250),
        LEFT(COALESCE(NULLIF(LTRIM(RTRIM(ProductCode)), N''), CAST(ProductID + 1 AS nvarchar(12))), 50),
        LEFT(COALESCE(BarCode, N''), 100), SellingPrice, TaxRate, AvailableQuantity
    FROM dbo.BranchProductCatalog WHERE Branch = @Branch;
OPEN products;
FETCH NEXT FROM products INTO @Rn, @Id, @Name, @Code, @Bar, @Price, @Tax, @Qty;
WHILE @@FETCH_STATUS = 0
BEGIN
    IF (@Rn - 1) % 500 = 0
        PRINT N'INSERT INTO #RestoreProducts (ProductID, ProductDesc, ProductCode, BarCode, SellingPrice, TaxRate, StockBal) VALUES';
    PRINT N'(' + CAST(@Id AS nvarchar(12))
        + N', N''' + REPLACE(@Name, N'''', N'''''') + N''''
        + N', N''' + REPLACE(@Code, N'''', N'''''') + N''''
        + N', N''' + REPLACE(@Bar, N'''', N'''''') + N''''
        + N', ' + CAST(@Price AS nvarchar(30)) + N', ' + CAST(@Tax AS nvarchar(30)) + N', ' + CAST(@Qty AS nvarchar(30)) + N')'
        + CASE WHEN @Rn % 500 = 0 OR @Rn = @Total THEN N';' ELSE N',' END;
    IF @Rn % 500 = 0 OR @Rn = @Total PRINT '';
    FETCH NEXT FROM products INTO @Rn, @Id, @Name, @Code, @Bar, @Price, @Tax, @Qty;
END
CLOSE products;
DEALLOCATE products;

-- Restore steps (same text as the website version).
PRINT N'BEGIN TRANSACTION;';
PRINT '';
PRINT N'DECLARE @ProductsUpdated int = 0, @ProductsAdded int = 0, @StockRowsSet int = 0, @StockRowsAdded int = 0,';
PRINT N'    @ProductsMoved int = 0, @StockRowsMoved int = 0, @Locked int;';
PRINT '';
PRINT N'-- 0) Lock the product and stock tables until the restore finishes, so the branch sync';
PRINT N'--    (POSViewer) cannot add the same products halfway through.';
PRINT N'SELECT @Locked = COUNT(*) FROM dbo.Products WITH (TABLOCKX, HOLDLOCK);';
PRINT N'SELECT @Locked = COUNT(*) FROM dbo.ProductStockBalances WITH (TABLOCKX, HOLDLOCK);';
PRINT '';
PRINT N'-- 0b) Older POSViewer versions always saved products and stock under company 412. If this';
PRINT N'--     Quantum database uses another company, move those rows to it so Quantum shows them.';
PRINT N'IF @CompanyId <> @OldPosCompanyId';
PRINT N'BEGIN';
PRINT N'    UPDATE p SET p.coid = @CompanyId';
PRINT N'    FROM dbo.Products AS p';
PRINT N'    WHERE p.coid = @OldPosCompanyId';
PRINT N'      AND NOT EXISTS (SELECT 1 FROM dbo.Products AS x WHERE x.coid = @CompanyId AND x.ProductCode = p.ProductCode);';
PRINT N'    SET @ProductsMoved = @@ROWCOUNT;';
PRINT '';
PRINT N'    UPDATE s SET s.coid = @CompanyId';
PRINT N'    FROM dbo.ProductStockBalances AS s';
PRINT N'    WHERE s.coid = @OldPosCompanyId';
PRINT N'      AND NOT EXISTS (';
PRINT N'          SELECT 1 FROM dbo.ProductStockBalances AS x';
PRINT N'          WHERE x.ProductID = s.ProductID AND x.branch = s.branch AND x.coid = @CompanyId);';
PRINT N'    SET @StockRowsMoved = @@ROWCOUNT;';
PRINT N'END';
PRINT '';
PRINT N'-- 1) Products that already exist: bring name, barcode, price and tax rate up to date, make them active.';
PRINT N'UPDATE p';
PRINT N'SET p.ProductDesc = r.ProductDesc, p.BarCode = r.BarCode, p.SellingPrice = r.SellingPrice,';
PRINT N'    p.TaxRate = r.TaxRate, p.IsActive = 1';
PRINT N'FROM dbo.Products AS p';
PRINT N'JOIN #RestoreProducts AS r ON r.ProductID = p.ProductID;';
PRINT N'SET @ProductsUpdated = @@ROWCOUNT;';
PRINT '';
PRINT N'-- 2) Missing products: insert them with their original ProductID (product codes are unique per company).';
PRINT N'IF OBJECT_ID(N''tempdb..#NewProducts'') IS NOT NULL DROP TABLE #NewProducts;';
PRINT N'SELECT m.*,';
PRINT N'    CASE WHEN m.CodeCopy > 1';
PRINT N'              OR EXISTS (SELECT 1 FROM dbo.Products AS x WHERE x.ProductCode = m.ProductCode AND x.coid = @CompanyId)';
PRINT N'         THEN LEFT(m.ProductCode + N''-R'' + CAST(m.ProductID AS nvarchar(12)), 50)';
PRINT N'         ELSE m.ProductCode END AS FinalCode';
PRINT N'INTO #NewProducts';
PRINT N'FROM (';
PRINT N'    SELECT r.*, ROW_NUMBER() OVER (PARTITION BY r.ProductCode ORDER BY r.ProductID) AS CodeCopy';
PRINT N'    FROM #RestoreProducts AS r';
PRINT N'    WHERE NOT EXISTS (SELECT 1 FROM dbo.Products AS p WHERE p.ProductID = r.ProductID)';
PRINT N') AS m;';
PRINT '';
PRINT N'IF EXISTS (SELECT 1 FROM #NewProducts)';
PRINT N'BEGIN';
PRINT N'    IF COLUMNPROPERTY(OBJECT_ID(N''dbo.Products''), ''ProductID'', ''IsIdentity'') = 1';
PRINT N'        SET IDENTITY_INSERT dbo.Products ON;';
PRINT N'    INSERT INTO dbo.Products (';
PRINT N'        ProductID, CatID, ProductDesc, Cost, SellingPrice, SellingPriceWholesale, WholesaleQTY,';
PRINT N'        UnitsPerPack, ReorderLevel, DoneBy, DoneWhen, ProductCode, BarCode, Uploaded,';
PRINT N'        TaxRate, Imported, UOM, BinLocation, IsActive, ProductDesc2, coid, SpecialPrice,';
PRINT N'        ProductExpires, DifferentPricesPerBranch, IsIngridientOnly, PharmacyIsPrescription,';
PRINT N'        IsUnlimitedStockItem, IsVoucher, isfavourite, isweighed, approval_audit, iseditable,';
PRINT N'        RecordSerialNumber)';
PRINT N'    SELECT n.ProductID, 1, n.ProductDesc, 0, n.SellingPrice, 0, 0,';
PRINT N'        0, 0, N''LoraRestore'', CONVERT(varchar(50), GETDATE(), 112), n.FinalCode, n.BarCode, 1,';
PRINT N'        n.TaxRate, 0, ''EA'', ''0'', 1, '''', @CompanyId, NULL,';
PRINT N'        0, 0, 0, 0,';
PRINT N'        0, 0, 0, 0, 0, 0,';
PRINT N'        0';
PRINT N'    FROM #NewProducts AS n';
PRINT N'    WHERE NOT EXISTS (SELECT 1 FROM dbo.Products AS p WHERE p.ProductID = n.ProductID);';
PRINT N'    SET @ProductsAdded = @@ROWCOUNT;';
PRINT N'    IF COLUMNPROPERTY(OBJECT_ID(N''dbo.Products''), ''ProductID'', ''IsIdentity'') = 1';
PRINT N'        SET IDENTITY_INSERT dbo.Products OFF;';
PRINT N'END';
PRINT '';
PRINT N'-- 3) Branch stock: SET each balance to the backed-up quantity (one row per product, company and branch).';
PRINT N'UPDATE s';
PRINT N'SET s.StockBal = r.StockBal';
PRINT N'FROM dbo.ProductStockBalances AS s';
PRINT N'JOIN #RestoreProducts AS r ON r.ProductID = s.ProductID';
PRINT N'WHERE s.coid = @CompanyId AND s.branch = @Branch;';
PRINT N'SET @StockRowsSet = @@ROWCOUNT;';
PRINT '';
PRINT N'INSERT INTO dbo.ProductStockBalances (ProductID, StockBal, MvtEntryNo, coid, branch, batchnumber, expirydate)';
PRINT N'SELECT r.ProductID, r.StockBal,';
PRINT N'    ISNULL((SELECT MAX(x.MvtEntryNo) FROM dbo.ProductStockBalances AS x WHERE x.ProductID = r.ProductID), 0) + 1,';
PRINT N'    @CompanyId, @Branch, NULL, NULL';
PRINT N'FROM #RestoreProducts AS r';
PRINT N'WHERE NOT EXISTS (';
PRINT N'    SELECT 1 FROM dbo.ProductStockBalances AS s';
PRINT N'    WHERE s.ProductID = r.ProductID AND s.coid = @CompanyId AND s.branch = @Branch);';
PRINT N'SET @StockRowsAdded = @@ROWCOUNT;';
PRINT '';
PRINT N'-- 4) Result.';
PRINT N'SELECT @Branch AS Branch,';
PRINT N'    @CompanyId AS CompanyId,';
PRINT N'    CASE WHEN EXISTS (SELECT 1 FROM dbo.Branches WHERE Branch = @Branch) THEN N''yes''';
PRINT N'         ELSE N''NO - this Quantum database has no branch with this name'' END AS BranchInQuantum,';
PRINT N'    (SELECT COUNT(*) FROM #RestoreProducts) AS ProductsInBackup,';
PRINT N'    @ProductsMoved AS ProductsMovedToCompany,';
PRINT N'    @StockRowsMoved AS StockRowsMovedToCompany,';
PRINT N'    @ProductsUpdated AS ProductsUpdated,';
PRINT N'    @ProductsAdded AS ProductsAdded,';
PRINT N'    @StockRowsSet AS StockRowsSet,';
PRINT N'    @StockRowsAdded AS StockRowsAdded,';
PRINT N'    (SELECT SUM(StockBal) FROM #RestoreProducts) AS UnitsInBackup,';
PRINT N'    (SELECT SUM(s.StockBal) FROM dbo.ProductStockBalances AS s';
PRINT N'        WHERE s.coid = @CompanyId AND s.branch = @Branch';
PRINT N'          AND s.ProductID IN (SELECT ProductID FROM #RestoreProducts)) AS UnitsInPosNow,';
PRINT N'    (SELECT COUNT(*) FROM dbo.Products AS p';
PRINT N'        WHERE p.coid = @CompanyId AND p.IsActive = 1';
PRINT N'          AND p.ProductID IN (SELECT ProductID FROM #RestoreProducts)) AS ProductsQuantumShows,';
PRINT N'    CASE WHEN @Apply = 1 THEN N''RESTORED - start the branch sync (POSViewer) again.''';
PRINT N'         ELSE N''DRY RUN - nothing was saved. Set @Apply = 1 and run again to restore.'' END AS Result;';
PRINT '';
PRINT N'IF @Apply = 1 COMMIT TRANSACTION; ELSE ROLLBACK TRANSACTION;';
PRINT '';
PRINT N'-- (a dry run''s ROLLBACK already removed #NewProducts)';
PRINT N'IF OBJECT_ID(N''tempdb..#NewProducts'') IS NOT NULL DROP TABLE #NewProducts;';
PRINT N'IF OBJECT_ID(N''tempdb..#RestoreProducts'') IS NOT NULL DROP TABLE #RestoreProducts;';
