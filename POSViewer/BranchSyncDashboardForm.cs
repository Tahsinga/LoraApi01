using System.Data;
using System.Data.SqlClient;
using System.Globalization;
using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;

namespace POSViewer;

public sealed class BranchSyncDashboardForm : Form
{
    private const int BranchCompanyId = 412;
    private readonly ConnectionForm _connectionForm;
    private readonly ConnectionSettings _settings;
    private readonly ListBox _syncQueueListBox = new();
    private readonly Label _statusLabel = new();
    private readonly Button _syncNowButton = new();
    private readonly Button _backButton = new();
    private readonly System.Windows.Forms.Timer _autoPollTimer = new();
    private readonly SemaphoreSlim _syncGate = new(1, 1);
    private DateTimeOffset? _sharedProductCatalogSince;

    public BranchSyncDashboardForm(ConnectionSettings settings, ConnectionForm connectionForm)
    {
        _settings = settings;
        _connectionForm = connectionForm;

        Text = "Branch Sync Dashboard";
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        Size = new Size(900, 600);
        BackColor = Color.FromArgb(245, 245, 245);

        var titleLabel = new Label
        {
            Text = "Branch Sync Dashboard",
            Font = new Font("Segoe UI", 22F, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(20, 20),
            ForeColor = Color.FromArgb(30, 30, 30)
        };

        var infoLabel = new Label
        {
            Text = $"This branch is connected as: {_settings.DeviceRole} | Server: {_settings.Server} | Database: {_settings.Database}",
            Font = new Font("Segoe UI", 11F),
            AutoSize = true,
            Location = new Point(20, 68),
            ForeColor = Color.FromArgb(60, 60, 60)
        };

        _statusLabel.Text = "Waiting for sync...";
        _statusLabel.Location = new Point(20, 110);
        _statusLabel.AutoSize = true;
        _statusLabel.Font = new Font("Segoe UI", 10F, FontStyle.Bold);
        _statusLabel.ForeColor = Color.DarkGreen;

        _syncQueueListBox.Location = new Point(20, 145);
        _syncQueueListBox.Size = new Size(ClientSize.Width - 40, ClientSize.Height - 240);
        _syncQueueListBox.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
        _syncQueueListBox.Font = new Font("Consolas", 10F);

        _syncNowButton.Text = "Sync Now";
        _syncNowButton.Location = new Point(20, ClientSize.Height - 60);
        _syncNowButton.Size = new Size(120, 32);
        _syncNowButton.Click += async (_, _) => await SyncNowAsync();

        _backButton.Text = "Back";
        _backButton.Location = new Point(160, ClientSize.Height - 60);
        _backButton.Size = new Size(120, 32);
        _backButton.Click += (_, _) =>
        {
            _connectionForm.ShowConnectionScreen();
            Hide();
        };

        Controls.Add(titleLabel);
        Controls.Add(infoLabel);
        Controls.Add(_statusLabel);
        Controls.Add(_syncQueueListBox);
        Controls.Add(_syncNowButton);
        Controls.Add(_backButton);

        FormClosing += (_, _) => _autoPollTimer.Stop();

        Resize += (_, _) =>
        {
            _syncQueueListBox.Size = new Size(ClientSize.Width - 40, ClientSize.Height - 240);
            _syncNowButton.Location = new Point(20, ClientSize.Height - 60);
            _backButton.Location = new Point(160, ClientSize.Height - 60);
        };

        LoadSyncQueue();

        _autoPollTimer.Interval = 30000;
        _autoPollTimer.Tick += async (_, _) => await SyncNowAsync();
        _autoPollTimer.Start();
        _ = SyncNowAsync();
    }

    private void LoadSyncQueue()
    {
        _syncQueueListBox.Items.Clear();
        _syncQueueListBox.Items.Add("[SYNC] No pending branch transactions.");
        _statusLabel.Text = "Branch dashboard ready.";
    }

    private async Task SyncNowAsync()
    {
        if (!await _syncGate.WaitAsync(0))
        {
            return;
        }

        var apiBaseUrl = _settings.GetApiBaseUrl();
        try
        {
            using var client = new HttpClient(new HttpClientHandler
            {
                AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate | DecompressionMethods.Brotli,
            }) { Timeout = TimeSpan.FromMinutes(3) };
            var branchName = await GetBranchNameAsync();

            try
            {
                if (string.IsNullOrWhiteSpace(_settings.BranchName))
                {
                    throw new InvalidOperationException("Save a branch name before starting branch sync.");
                }
                var heartbeat = new
                {
                    branch = branchName,
                    device_role = _settings.DeviceRole
                };
                var heartbeatJson = JsonSerializer.Serialize(heartbeat);
                using var heartbeatContent = new StringContent(heartbeatJson, Encoding.UTF8, "application/json");
                var heartbeatResponse = await client.PostAsync($"{apiBaseUrl}/api/branches/", heartbeatContent);
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] [CONNECTION] Web API reachable for branch {branchName}: {heartbeatResponse.StatusCode}.");
            }
            catch (HttpRequestException ex)
            {
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✗ [CONNECTION] Web API heartbeat failed: {ex.Message}");
            }

            if (!_sharedProductCatalogSince.HasValue || DateTimeOffset.UtcNow - _sharedProductCatalogSince.Value >= TimeSpan.FromMinutes(1))
            {
                await ApplySharedProductCatalogAsync(client, branchName);
            }
            
            // Poll for pending deletions specific to this branch
            var branchFilter = $"?branch={Uri.EscapeDataString(branchName)}";
            var response = await client.GetAsync($"{apiBaseUrl}/api/branch-sync/{branchFilter}");

            if (!response.IsSuccessStatusCode)
            {
                _statusLabel.ForeColor = Color.DarkRed;
                _statusLabel.Text = $"Sync failed: {response.StatusCode} at {apiBaseUrl}";
                return;
            }

            var mediaType = response.Content.Headers.ContentType?.MediaType;
            if (!string.Equals(mediaType, "application/json", StringComparison.OrdinalIgnoreCase))
            {
                _statusLabel.ForeColor = Color.DarkRed;
                _statusLabel.Text = $"Sync failed: API returned {mediaType ?? "unknown content"} at {apiBaseUrl}";
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✗ API URL is not returning JSON. Check that one Django server is running on port 8000.");
                return;
            }

            var result = await response.Content.ReadFromJsonAsync<BranchSyncTriggerResponse>();
            if (result is null)
            {
                _statusLabel.ForeColor = Color.DarkRed;
                _statusLabel.Text = "Sync failed: no data returned from gateway.";
                return;
            }

            var pendingDeletions = result.pending_deletions ?? new List<DeletionTrigger>();
            var pendingReports = result.pending_reports ?? new List<SalesReportRequest>();
            var pendingTransfers = result.pending_transfers ?? new List<StockTransfer>();
            var pendingPriceUpdates = result.pending_price_updates ?? new List<BranchPriceUpdate>();
            var pendingProductCreations = result.pending_product_creations ?? new List<BranchProductCreation>();
            var pendingProductDeletions = result.pending_product_deletions ?? new List<BranchProductDeletion>();
            var pendingInvoiceReprints = result.pending_invoice_reprints ?? new List<InvoiceReprintRequest>();
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] [CONNECTION] Transfer poll succeeded for {branchName}: {pendingTransfers.Count} command(s).");
            
            if (pendingDeletions.Count > 0 || pendingReports.Count > 0 || pendingTransfers.Count > 0 || pendingPriceUpdates.Count > 0 || pendingProductCreations.Count > 0 || pendingProductDeletions.Count > 0 || pendingInvoiceReprints.Count > 0)
            {
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] RECEIVED {pendingDeletions.Count} cancellation command(s) from API for branch {branchName}.");
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] RECEIVED {pendingTransfers.Count} stock transfer(s) for branch {branchName}.");
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] === PROCESSING {pendingDeletions.Count} DELETION TRIGGER(S) ===");
                
                foreach (var deletion in pendingDeletions)
                {
                    _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] RECEIVED COMMAND: Source={deletion.source}, Invoice={deletion.invoice}, Branch={deletion.branch}, Product={deletion.product_id}, Entry={deletion.entry_no}, ID={deletion.id}");
                    _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] → Processing cancellation from {deletion.source}: Invoice={deletion.invoice}");
                    
                    var deletedCount = await DeleteAndConfirmAsync(deletion, client);
                    
                    if (deletedCount > 0)
                    {
                        _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✓ DELETED: {deletedCount} row(s) | Confirmed to API");
                    }
                    else
                    {
                        _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ⚠ NO ROWS: Invoice {deletion.invoice} - may already be deleted");
                    }
                }

                foreach (var report in pendingReports)
                {
                    if (!string.Equals(report.branch?.Trim(), branchName.Trim(), StringComparison.OrdinalIgnoreCase))
                    {
                        _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✗ SAFETY CHECK: Ignored report for branch {report.branch}; this app is {branchName}.");
                        continue;
                    }

                    await ProcessSalesReportAsync(report, client, branchName);
                }

                foreach (var reprint in pendingInvoiceReprints)
                {
                    if (!string.Equals(reprint.branch?.Trim(), branchName.Trim(), StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }

                    await ProcessInvoiceReprintAsync(reprint, client, branchName);
                }

                foreach (var priceUpdate in pendingPriceUpdates)
                {
                    if (!string.Equals(priceUpdate.branch?.Trim(), branchName.Trim(), StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }

                    await ApplyBranchPriceUpdateAsync(priceUpdate, client, branchName);
                }

                foreach (var productCreation in pendingProductCreations)
                {
                    if (!string.Equals(productCreation.branch?.Trim(), branchName.Trim(), StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }

                    await ApplyBranchProductCreationAsync(productCreation, client, branchName);
                }

                foreach (var productDeletion in pendingProductDeletions)
                {
                    if (!string.Equals(productDeletion.branch?.Trim(), branchName.Trim(), StringComparison.OrdinalIgnoreCase))
                    {
                        _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✗ SAFETY CHECK: Ignored product deletion for branch {productDeletion.branch}; this app is {branchName}.");
                        continue;
                    }

                    await ApplyBranchProductDeletionAsync(productDeletion, client, branchName);
                }

                foreach (var transfer in pendingTransfers.Take(1))
                {
                    if (!string.Equals(transfer.branch?.Trim(), branchName.Trim(), StringComparison.OrdinalIgnoreCase))
                    {
                        _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✗ SAFETY CHECK: Ignored stock transfer for branch {transfer.branch}; this app is {branchName}.");
                        continue;
                    }

                    await ApplyStockTransferAsync(transfer, client, branchName);
                }

                _statusLabel.ForeColor = Color.DarkGreen;
                _statusLabel.Text = $"✓ Sync complete | Branch: {branchName}";
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] === SYNC COMPLETE ===");
            }
            else
            {
                _statusLabel.ForeColor = Color.DarkGreen;
                _statusLabel.Text = $"No pending transactions | Branch: {branchName}";
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] [POLL] No pending transactions for {branchName}. Transfers={pendingTransfers.Count}, reports={pendingReports.Count}, cancellations={pendingDeletions.Count}.");
            }
        }
        catch (Exception ex)
        {
            _statusLabel.ForeColor = Color.DarkRed;
            _statusLabel.Text = $"Sync error at {apiBaseUrl}: {ex.Message}";
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✗ ERROR at {apiBaseUrl}: {ex.Message}");
        }
        finally
        {
            _syncGate.Release();
        }
    }

    private Task<string> GetBranchNameAsync()
    {
        var configuredBranch = _settings.BranchName?.Trim();
        if (string.IsNullOrWhiteSpace(configuredBranch))
        {
            throw new InvalidOperationException("Assign this installation to a branch in Connection Settings before starting branch sync.");
        }

        return Task.FromResult(configuredBranch);
    }

    private async Task<bool> ApplySharedProductCatalogAsync(HttpClient client, string branchName)
    {
        try
        {
            var syncStartedAt = DateTimeOffset.UtcNow;
            var catalogUrl = $"{_settings.GetApiBaseUrl()}/api/products/shared/";
            if (_sharedProductCatalogSince.HasValue)
            {
                catalogUrl += $"?since={Uri.EscapeDataString(_sharedProductCatalogSince.Value.ToString("O", CultureInfo.InvariantCulture))}";
            }

            using var response = await client.GetAsync(catalogUrl);
            if (!response.IsSuccessStatusCode)
            {
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] WARNING: Shared product catalog request failed: {response.StatusCode}");
                return false;
            }

            var payload = await response.Content.ReadFromJsonAsync<SharedProductCatalogResponse>();
            var products = payload?.products ?? new List<SharedProduct>();
            if (products.Count == 0)
            {
                _sharedProductCatalogSince = syncStartedAt;
                return true;
            }

            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();
            var productIds = new HashSet<int>();
            using (var idsCommand = new SqlCommand("SELECT ProductID FROM [dbo].[Products] WHERE ProductID IS NOT NULL;", connection))
            using (var reader = await idsCommand.ExecuteReaderAsync())
            {
                while (await reader.ReadAsync())
                {
                    productIds.Add(Convert.ToInt32(reader[0], CultureInfo.InvariantCulture));
                }
            }

            var added = 0;
            var updated = 0;
            foreach (var product in products)
            {
                if (product.product_id <= 0 || string.IsNullOrWhiteSpace(product.product_name))
                {
                    continue;
                }

                if (!productIds.Add(product.product_id))
                {
                    const string updateProductSql = @"
                        UPDATE [dbo].[Products]
                        SET ProductDesc = @productName, BarCode = @barcode, SellingPrice = @sellingPrice, TaxRate = @taxRate
                        WHERE ProductID = @productId;";
                    using (var productCommand = new SqlCommand(updateProductSql, connection))
                    {
                        productCommand.Parameters.AddWithValue("@productId", product.product_id);
                        productCommand.Parameters.AddWithValue("@productName", product.product_name.Trim());
                        productCommand.Parameters.AddWithValue("@barcode", product.barcode ?? string.Empty);
                        productCommand.Parameters.AddWithValue("@sellingPrice", product.selling_price);
                        productCommand.Parameters.AddWithValue("@taxRate", product.tax_rate);
                        updated += await productCommand.ExecuteNonQueryAsync();
                    }

                    const string ensureZeroBalanceSql = @"
                        IF NOT EXISTS (
                            SELECT 1 FROM [dbo].[ProductStockBalances]
                            WHERE ProductID = @productId AND branch = @branch
                        )
                        INSERT INTO [dbo].[ProductStockBalances]
                            (ProductID, StockBal, MvtEntryNo, coid, branch, batchnumber, expirydate)
                        VALUES (
                            @productId, 0,
                            ISNULL((SELECT MAX(MvtEntryNo) FROM [dbo].[ProductStockBalances] WHERE ProductID = @productId), 0) + 1,
                            @coid, @branch, NULL, NULL
                        );";
                    using (var balanceCommand = new SqlCommand(ensureZeroBalanceSql, connection))
                    {
                        balanceCommand.Parameters.AddWithValue("@productId", product.product_id);
                        balanceCommand.Parameters.AddWithValue("@branch", branchName);
                        balanceCommand.Parameters.AddWithValue("@coid", BranchCompanyId);
                        await balanceCommand.ExecuteNonQueryAsync();
                    }

                    continue;
                }

                using var transaction = connection.BeginTransaction();
                try
                {
                    await InsertPosProductAsync(
                        connection,
                        transaction,
                        product.product_id,
                        product.product_name.Trim(),
                        checked(product.product_id + 1).ToString(CultureInfo.InvariantCulture),
                        product.barcode ?? string.Empty,
                        product.selling_price,
                        product.tax_rate);

                    const string insertBalanceSql = @"
                        IF NOT EXISTS (
                            SELECT 1 FROM [dbo].[ProductStockBalances]
                            WHERE ProductID = @productId AND branch = @branch
                        )
                        INSERT INTO [dbo].[ProductStockBalances]
                            (ProductID, StockBal, MvtEntryNo, coid, branch, batchnumber, expirydate)
                        VALUES (
                            @productId, 0,
                            ISNULL((SELECT MAX(MvtEntryNo) FROM [dbo].[ProductStockBalances] WHERE ProductID = @productId), 0) + 1,
                            @coid, @branch, NULL, NULL
                        );";
                    using (var balanceCommand = new SqlCommand(insertBalanceSql, connection, transaction))
                    {
                        balanceCommand.Parameters.AddWithValue("@productId", product.product_id);
                        balanceCommand.Parameters.AddWithValue("@branch", branchName);
                        balanceCommand.Parameters.AddWithValue("@coid", BranchCompanyId);
                        await balanceCommand.ExecuteNonQueryAsync();
                    }

                    transaction.Commit();
                    added++;
                }
                catch
                {
                    try { transaction.Rollback(); } catch { }
                    throw;
                }
            }

            if (added > 0 || updated > 0)
            {
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✓ Synced {updated} shared product price/metadata row(s) and added {added} product(s) to {branchName} with zero opening stock.");
            }

            _sharedProductCatalogSince = syncStartedAt;
            return true;
        }
        catch (Exception ex)
        {
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] WARNING: Shared product sync failed: {ex.Message}");
            return false;
        }
    }

    private sealed class SharedProductCatalogResponse
    {
        public List<SharedProduct>? products { get; set; }
    }

    private sealed class SharedProduct
    {
        public int product_id { get; set; }
        public string? product_name { get; set; }
        public string? product_code { get; set; }
        public string? barcode { get; set; }
        public decimal selling_price { get; set; }
        public decimal tax_rate { get; set; }
    }

    private static async Task<int> InsertPosProductAsync(
        SqlConnection connection,
        SqlTransaction transaction,
        int productId,
        string productName,
        string productCode,
        string barcode,
        decimal sellingPrice,
        decimal taxRate)
    {
        const string insertProductSql = @"
            INSERT INTO [dbo].[Products](
                ProductID, CatID, ProductDesc, Cost, SellingPrice, SellingPriceWholesale, WholesaleQTY,
                UnitsPerPack, ReorderLevel, DoneBy, DoneWhen, ProductCode, BarCode, Uploaded,
                TaxRate, Imported, UOM, BinLocation, IsActive, ProductDesc2, coid, SpecialPrice,
                ProductExpires, DifferentPricesPerBranch, IsIngridientOnly, PharmacyIsPrescription,
                IsUnlimitedStockItem, IsVoucher, isfavourite, isweighed, approval_audit, iseditable,
                RecordSerialNumber
            )
            OUTPUT INSERTED.ProductID
            VALUES (
                @productId, 1, @productName, 0, @sellingPrice, 0, 0,
                0, 0, @doneBy, CONVERT(varchar(50), GETDATE(), 112), @productCode, @barcode, 1,
                @taxRate, 0, 'EA', '0', 1, '', @coid, NULL,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
            );";

        using (var identityOnCommand = new SqlCommand("SET IDENTITY_INSERT [dbo].[Products] ON;", connection, transaction))
        {
            await identityOnCommand.ExecuteNonQueryAsync();
        }

        try
        {
            var uniqueProductCode = await ResolveUniqueProductCodeAsync(connection, transaction, productCode, BranchCompanyId);
            using var productCommand = new SqlCommand(insertProductSql, connection, transaction);
            productCommand.Parameters.AddWithValue("@productId", productId);
            productCommand.Parameters.AddWithValue("@productName", productName);
            productCommand.Parameters.AddWithValue("@productCode", uniqueProductCode);
            productCommand.Parameters.AddWithValue("@barcode", barcode);
            productCommand.Parameters.AddWithValue("@sellingPrice", sellingPrice);
            productCommand.Parameters.AddWithValue("@taxRate", taxRate);
            productCommand.Parameters.AddWithValue("@doneBy", Environment.UserName);
            productCommand.Parameters.AddWithValue("@coid", BranchCompanyId);
            return Convert.ToInt32(await productCommand.ExecuteScalarAsync(), CultureInfo.InvariantCulture);
        }
        finally
        {
            using var identityOffCommand = new SqlCommand("SET IDENTITY_INSERT [dbo].[Products] OFF;", connection, transaction);
            await identityOffCommand.ExecuteNonQueryAsync();
        }
    }

    private static async Task<string> ResolveUniqueProductCodeAsync(
        SqlConnection connection,
        SqlTransaction transaction,
        string preferredCode,
        int companyId)
    {
        const string productCodeExistsSql = @"
            SELECT TOP 1 1
            FROM [dbo].[Products] WITH (UPDLOCK, HOLDLOCK)
            WHERE ProductCode = @productCode AND coid = @coid;";

        for (var suffix = 0; ; suffix++)
        {
            var candidate = suffix == 0 ? preferredCode : $"{preferredCode}-{suffix}";
            using var command = new SqlCommand(productCodeExistsSql, connection, transaction);
            command.Parameters.AddWithValue("@productCode", candidate);
            command.Parameters.AddWithValue("@coid", companyId);
            if (await command.ExecuteScalarAsync() is null)
            {
                return candidate;
            }
        }
    }

    private async Task<int> DeleteAndConfirmAsync(DeletionTrigger deletion, HttpClient client)
    {
        int deletedCount = 0;
        
        try
        {
            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();

            // Execute deletion using trigger data
            string invoiceNum = deletion.invoice ?? "";
            int productId = deletion.product_id ?? 0;
            string branchName = deletion.branch ?? await GetBranchNameAsync();
            string entryNo = deletion.entry_no ?? "";
            var wholeInvoice = string.Equals(deletion.action, "cancel_invoice", StringComparison.OrdinalIgnoreCase);
            var stockLines = new List<(int ProductId, string ProductName, decimal Quantity, decimal UnitPrice, decimal LineTotal)>();

            // Log what we're about to delete
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] DEBUG: DeleteAndConfirmAsync called");
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] DEBUG: Invoice={invoiceNum} | Product={productId} | Branch={branchName} | Entry={entryNo}");

            if (wholeInvoice)
            {
                const string stockLinesSql = @"
                    SELECT
                        m.ProductID,
                        COALESCE(NULLIF(LTRIM(RTRIM(p.ProductDesc)), ''), CONCAT('Product ', m.ProductID)) AS ProductName,
                        ABS(COALESCE(m.Quantity, 0)) AS Quantity,
                        COALESCE(m.SellingPrice, 0) AS UnitPrice,
                        ABS(
                            (COALESCE(m.Quantity, 0) * COALESCE(m.SellingPrice, 0))
                            - COALESCE(m.DiscountAmt, 0)
                            - COALESCE(m.InvDiscount, 0)
                            + COALESCE(m.TaxAmt, 0)
                        ) AS LineTotal
                    FROM [dbo].[Movement] AS m
                    LEFT JOIN [dbo].[Products] AS p ON p.ProductID = m.ProductID
                    WHERE (m.InvoiceNum = @invoiceNum OR CAST(m.InvoiceNum AS nvarchar(50)) = @invoiceNum)
                      AND UPPER(CAST(m.Branch AS nvarchar(100))) = UPPER(@branchName);";

                using var stockLinesCommand = new SqlCommand(stockLinesSql, connection);
                stockLinesCommand.Parameters.AddWithValue("@invoiceNum", invoiceNum);
                stockLinesCommand.Parameters.AddWithValue("@branchName", branchName);
                using (var stockReader = await stockLinesCommand.ExecuteReaderAsync())
                {
                    while (await stockReader.ReadAsync())
                    {
                        if (!stockReader.IsDBNull(0) && !stockReader.IsDBNull(1))
                        {
                            stockLines.Add((
                                Convert.ToInt32(stockReader[0]),
                                stockReader[1]?.ToString()?.Trim() ?? string.Empty,
                                Convert.ToDecimal(stockReader[2]),
                                Convert.ToDecimal(stockReader[3]),
                                Convert.ToDecimal(stockReader[4])));
                        }
                    }
                }
            }

            if (wholeInvoice)
            {
                return await CancelWholeInvoiceAndConfirmAsync(
                    connection,
                    client,
                    deletion,
                    invoiceNum,
                    branchName,
                    stockLines);
            }

            // Build WHERE clause from trigger data
            var deleteSql = @"
                DELETE FROM [dbo].[Movement]
                WHERE (InvoiceNum = @invoiceNum OR CAST(InvoiceNum AS nvarchar(50)) = @invoiceNum)
                  AND (Branch = @branchName OR UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@branchName))";

            if (productId > 0)
            {
                deleteSql += " AND ProductID = @productId";
            }

            if (!string.IsNullOrWhiteSpace(entryNo))
            {
                deleteSql += " AND (EntryNo = @entryNo OR CAST(EntryNo AS nvarchar(50)) = @entryNo)";
            }

            deleteSql += ";";

            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] DEBUG: SQL Query:\n{deleteSql}");

            using (var command = new SqlCommand(deleteSql, connection))
            {
                command.Parameters.AddWithValue("@invoiceNum", invoiceNum);
                command.Parameters.AddWithValue("@branchName", branchName);
                if (productId > 0)
                {
                    command.Parameters.AddWithValue("@productId", productId);
                }
                if (!string.IsNullOrWhiteSpace(entryNo))
                {
                    command.Parameters.AddWithValue("@entryNo", entryNo);
                }

                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] DEBUG: Executing DELETE...");
                deletedCount = await command.ExecuteNonQueryAsync();
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] DEBUG: @@ROWCOUNT = {deletedCount}");
            }

            if (deletedCount > 0 && deletion.quantity is > 0 && productId > 0)
            {
                var stockSql = @"
                    UPDATE [dbo].[ProductStockBalances]
                    SET StockBal = StockBal + @quantity
                    WHERE branch = @branchName
                      AND ProductID = @productId";

                if (deletion.coid is > 0)
                {
                    stockSql += " AND coid = @coid";
                }

                using var stockCommand = new SqlCommand(stockSql, connection);
                stockCommand.Parameters.AddWithValue("@quantity", deletion.quantity.Value);
                stockCommand.Parameters.AddWithValue("@branchName", branchName);
                stockCommand.Parameters.AddWithValue("@productId", productId);
                if (deletion.coid is > 0)
                {
                    stockCommand.Parameters.AddWithValue("@coid", deletion.coid.Value);
                }

                var restoredRows = await stockCommand.ExecuteNonQueryAsync();
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] STOCK RESTORED: {deletion.quantity.Value} unit(s) across {restoredRows} balance row(s)");
            }

            if (deletedCount > 0 && wholeInvoice)
            {
                foreach (var stockLine in stockLines)
                {
                    const string stockSql = @"
                        UPDATE [dbo].[ProductStockBalances]
                        SET StockBal = StockBal + @quantity
                        WHERE branch = @branchName
                          AND ProductID = @productId;";

                    using var stockCommand = new SqlCommand(stockSql, connection);
                    stockCommand.Parameters.AddWithValue("@quantity", stockLine.Quantity);
                    stockCommand.Parameters.AddWithValue("@branchName", branchName);
                    stockCommand.Parameters.AddWithValue("@productId", stockLine.ProductId);
                    await stockCommand.ExecuteNonQueryAsync();
                }

                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] STOCK RESTORED: {stockLines.Count} invoice line(s)");
            }

            // First, check if data exists before deletion (for debugging)
            var checkSql = @"
                SELECT COUNT(*) AS MatchCount FROM [dbo].[Movement]
                WHERE (InvoiceNum = @invoiceNum OR CAST(InvoiceNum AS nvarchar(50)) = @invoiceNum)
                  AND (Branch = @branchName OR UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@branchName))";

            if (productId > 0)
            {
                checkSql += " AND ProductID = @productId";
            }

            if (!string.IsNullOrWhiteSpace(entryNo))
            {
                checkSql += " AND (EntryNo = @entryNo OR CAST(EntryNo AS nvarchar(50)) = @entryNo)";
            }

            checkSql += ";";

            using (var checkCmd = new SqlCommand(checkSql, connection))
            {
                checkCmd.Parameters.AddWithValue("@invoiceNum", invoiceNum);
                checkCmd.Parameters.AddWithValue("@branchName", branchName);
                if (productId > 0)
                {
                    checkCmd.Parameters.AddWithValue("@productId", productId);
                }
                if (!string.IsNullOrWhiteSpace(entryNo))
                {
                    checkCmd.Parameters.AddWithValue("@entryNo", entryNo);
                }

                var checkResult = await checkCmd.ExecuteScalarAsync();
                int remainingCount = checkResult != null ? Convert.ToInt32(checkResult) : -1;
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] DEBUG: Remaining records after delete: {remainingCount}");
            }

            // CONFIRMATION: Send back to API that deletion succeeded
            var confirmPayload = new
            {
                deletion_id = deletion.id,
                deleted_rows = deletedCount,
                branch = await GetBranchNameAsync(),
                deleted_by = Environment.UserName,
                receipt_products = stockLines.Select(line => new
                {
                    product_id = line.ProductId,
                    product_name = line.ProductName,
                    quantity = line.Quantity,
                    unit_price = line.UnitPrice,
                    total = line.LineTotal,
                }).ToList(),
                receipt_total = stockLines.Sum(line => line.LineTotal),
                success = deletedCount > 0
            };
            var confirmJson = JsonSerializer.Serialize(confirmPayload);
            using var confirmContent = new StringContent(confirmJson, Encoding.UTF8, "application/json");
            var confirmResponse = await client.PostAsync(
                $"{_settings.GetApiBaseUrl()}/api/confirm-deletion/",
                confirmContent
            );

            if (confirmResponse.IsSuccessStatusCode && deletedCount > 0)
            {
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] → API confirmed: deletion_id={deletion.id}");

                var printed = ReceiptPrinter.TryPrint(
                    _settings.PrinterName,
                    "CANCELLATION CONFIRMATION",
                    new List<(string Label, string Value)>
                    {
                        ("Status", "CANCELLED"),
                        ("Invoice", invoiceNum),
                        ("Branch", branchName),
                        ("Product", productId > 0 ? productId.ToString() : "All products"),
                        ("Entry No", entryNo),
                        ("Products", stockLines.Count == 0
                            ? (productId > 0 ? $"Product {productId}" : "No product lines found")
                            : string.Join(Environment.NewLine, stockLines.Select(line =>
                                $"{line.ProductName} x{line.Quantity:0.##} @ {line.UnitPrice:0.00} = {line.LineTotal:0.00}"))),
                        ("Total returned", stockLines.Sum(line => line.LineTotal).ToString("0.00", CultureInfo.InvariantCulture)),
                        ("Rows deleted", deletedCount.ToString()),
                        ("Date", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss")),
                        ("Machine", Environment.MachineName)
                    },
                    out var printError);

                if (!printed)
                {
                    _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] WARNING: Cancellation completed, but receipt was not printed: {printError}");
                }
            }
            else
            {
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ⚠ Cancellation not printed because no invoice rows were deleted (API: {confirmResponse.StatusCode}).");
            }
        }
        catch (Exception ex)
        {
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✗ Deletion error for invoice {deletion.invoice}: {ex.Message}");
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] Exception details: {ex.StackTrace}");
        }

        return deletedCount;
    }

    private async Task<int> CancelWholeInvoiceAndConfirmAsync(
        SqlConnection connection,
        HttpClient client,
        DeletionTrigger deletion,
        string invoiceNum,
        string branchName,
        List<(int ProductId, string ProductName, decimal Quantity, decimal UnitPrice, decimal LineTotal)> stockLines)
    {
        if (!int.TryParse(invoiceNum, NumberStyles.Integer, CultureInfo.InvariantCulture, out var invoiceNumber))
        {
            throw new InvalidOperationException($"Invoice number '{invoiceNum}' is not a valid Quantum invoice number.");
        }

        var companyId = deletion.coid ?? BranchCompanyId;
        var cancelledDetails = await LoadCancelledInvoiceDetailsAsync(connection, invoiceNumber, companyId, branchName);
        if (cancelledDetails.Rows.Count == 0)
        {
            if (stockLines.Count == 0)
            {
                throw new InvalidOperationException($"Invoice {invoiceNum} has no matching Movement rows and is not already cancelled.");
            }

            using var cancelCommand = new SqlCommand("dbo.CancelSale", connection)
            {
                CommandType = CommandType.StoredProcedure
            };
            cancelCommand.Parameters.AddWithValue("@invoicenum", invoiceNumber);
            cancelCommand.Parameters.AddWithValue("@Cancelledby", Environment.UserName);
            cancelCommand.Parameters.AddWithValue("@datecancelled", int.Parse(DateTime.Today.ToString("yyyyMMdd", CultureInfo.InvariantCulture), CultureInfo.InvariantCulture));
            cancelCommand.Parameters.AddWithValue("@coid", companyId);
            cancelCommand.Parameters.AddWithValue("@branch", branchName);
            cancelCommand.Parameters.AddWithValue("@machinename", Environment.MachineName);
            cancelCommand.Parameters.AddWithValue("@iswarehouse", 0);
            cancelCommand.Parameters.AddWithValue("@approval", 0);
            cancelCommand.Parameters.AddWithValue("@comment", deletion.message ?? "Cancelled from web");
            await cancelCommand.ExecuteNonQueryAsync();

            try
            {
                cancelledDetails = await LoadCancelledInvoiceDetailsAsync(connection, invoiceNumber, companyId, branchName);
            }
            catch (Exception ex)
            {
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] WARNING: Quantum cancelled invoice {invoiceNum}, but credit-note details could not be loaded: {ex.Message}");
            }
        }
        else
        {
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] Invoice {invoiceNum} is already cancelled in Quantum; skipping duplicate cancellation.");
        }

        var creditNote = BuildCancellationReceiptDetails(cancelledDetails, stockLines, invoiceNum, branchName);
        var printed = ReceiptPrinter.TryPrint(_settings.PrinterName, "CREDIT NOTE", creditNote, out var printError);
        if (!printed)
        {
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] WARNING: Cancellation completed, but credit note was not printed: {printError}");
        }

        var completedCount = Math.Max(1, Math.Max(stockLines.Count, cancelledDetails.Rows.Count));
        var confirmPayload = new
        {
            deletion_id = deletion.id,
            deleted_rows = completedCount,
            branch = branchName,
            deleted_by = Environment.UserName,
            receipt_products = stockLines.Select(line => new
            {
                product_id = line.ProductId,
                product_name = line.ProductName,
                quantity = line.Quantity,
                unit_price = line.UnitPrice,
                total = line.LineTotal,
            }).ToList(),
            receipt_total = stockLines.Sum(line => line.LineTotal),
            success = true
        };

        try
        {
            using var confirmContent = new StringContent(JsonSerializer.Serialize(confirmPayload), Encoding.UTF8, "application/json");
            using var confirmResponse = await client.PostAsync(
                $"{_settings.GetApiBaseUrl()}/api/confirm-deletion/",
                confirmContent);

            if (confirmResponse.IsSuccessStatusCode)
            {
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] → Quantum cancellation confirmed to API: deletion_id={deletion.id}");
            }
            else
            {
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] WARNING: Invoice {invoiceNum} was cancelled locally, but API confirmation returned {confirmResponse.StatusCode}.");
            }
        }
        catch (Exception ex)
        {
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] WARNING: Invoice {invoiceNum} was cancelled locally, but API confirmation failed: {ex.Message}");
        }

        return completedCount;
    }

    private static async Task<DataTable> LoadCancelledInvoiceDetailsAsync(
        SqlConnection connection,
        int invoiceNumber,
        int companyId,
        string branchName)
    {
        using var command = new SqlCommand("SalesCancelledForPeriodByInvoiceDetails", connection)
        {
            CommandType = CommandType.StoredProcedure
        };
        command.Parameters.AddWithValue("@invoicenum", invoiceNumber);
        command.Parameters.AddWithValue("@coid", companyId);
        command.Parameters.AddWithValue("@branch", branchName);

        await using var reader = await command.ExecuteReaderAsync();
        var details = new DataTable();
        details.Load(reader);
        return details;
    }

    private static List<(string Label, string Value)> BuildCancellationReceiptDetails(
        DataTable cancelledDetails,
        List<(int ProductId, string ProductName, decimal Quantity, decimal UnitPrice, decimal LineTotal)> stockLines,
        string invoiceNum,
        string branchName)
    {
        var details = cancelledDetails.Rows.Count > 0 ? cancelledDetails.Rows[0] : null;
        var itemLines = cancelledDetails.Rows.Count > 0
            ? cancelledDetails.Rows.Cast<DataRow>().Select(row =>
            {
                var code = GetReceiptText(row, "HSCode");
                var description = GetReceiptText(row, "Description");
                var productName = string.Join(" - ", new[] { code, description }.Where(value => !string.IsNullOrWhiteSpace(value)));
                var quantity = GetReceiptDecimal(row, "Quantity");
                var unitPrice = GetReceiptDecimal(row, "Unit_Selling_Price");
                var tax = GetReceiptDecimal(row, "Tax");
                var discount = GetReceiptDecimal(row, "DiscountAmt");
                var total = quantity * unitPrice + tax;
                var productComment = GetReceiptText(row, "Comments");
                var line = $"{productName}\n{quantity:0.##} x {unitPrice:0.00} | Tax {tax:0.00} | Total {total:0.00}";
                if (discount > 0m)
                {
                    line += $"\nDiscount per unit: {discount:0.00}";
                }

                if (!string.IsNullOrWhiteSpace(productComment))
                {
                    line += $"\n{productComment}";
                }

                return line;
            }).ToList()
            : stockLines.Select(line => $"{line.ProductName}\n{line.Quantity:0.##} x {line.UnitPrice:0.00} | Total {line.LineTotal:0.00}").ToList();

        var subtotal = cancelledDetails.Rows.Count > 0
            ? cancelledDetails.Rows.Cast<DataRow>().Sum(row => GetReceiptDecimal(row, "Sub_Total"))
            : stockLines.Sum(line => line.Quantity * line.UnitPrice);
        var taxTotal = cancelledDetails.Rows.Cast<DataRow>().Sum(row => GetReceiptDecimal(row, "Tax"));
        var invoiceDiscount = details is null ? 0m : GetReceiptDecimal(details, "invdiscount");
        var saleDate = details is null ? string.Empty : GetReceiptText(details, "Sale_Date");
        if (DateTime.TryParseExact(saleDate, "yyyyMMdd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var parsedSaleDate)
            || DateTime.TryParse(saleDate, CultureInfo.InvariantCulture, DateTimeStyles.None, out parsedSaleDate))
        {
            saleDate = parsedSaleDate.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        }

        return new List<(string Label, string Value)>
        {
            ("Credit Note Number", details is null ? "Not provided by Quantum" : GetReceiptText(details, "CreditNoteNum")),
            ("Comments", details is null ? "Cancelled from web" : GetReceiptText(details, "CreditNoteComment")),
            ("Invoice Number", invoiceNum),
            ("Invoice Date", saleDate),
            ("Invoice Time", details is null ? string.Empty : GetReceiptText(details, "trantime")),
            ("Customer", details is null ? string.Empty : GetReceiptText(details, "ref")),
            ("Currency", details is null ? string.Empty : GetReceiptText(details, "Currency")),
            ("Products", string.Join(Environment.NewLine, itemLines)),
            ("Subtotal", subtotal.ToString("0.00", CultureInfo.InvariantCulture)),
            ("Tax", taxTotal.ToString("0.00", CultureInfo.InvariantCulture)),
            ("Invoice Discount", invoiceDiscount.ToString("0.00", CultureInfo.InvariantCulture)),
            ("Total", (subtotal + taxTotal).ToString("0.00", CultureInfo.InvariantCulture)),
            ("Cancelled By", Environment.UserName),
            ("Branch", branchName),
            ("Date", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture))
        };
    }

    private static string GetReceiptText(DataRow row, string columnName)
    {
        return row.Table.Columns.Contains(columnName) && !row.IsNull(columnName)
            ? Convert.ToString(row[columnName], CultureInfo.InvariantCulture) ?? string.Empty
            : string.Empty;
    }

    private static decimal GetReceiptDecimal(DataRow row, string columnName)
    {
        if (!row.Table.Columns.Contains(columnName) || row.IsNull(columnName))
        {
            return 0m;
        }

        return decimal.TryParse(
            Convert.ToString(row[columnName], CultureInfo.InvariantCulture),
            NumberStyles.Number,
            CultureInfo.InvariantCulture,
            out var value)
            ? value
            : 0m;
    }

    private async Task ApplyStockTransferAsync(StockTransfer transfer, HttpClient client, string branchName)
    {
        var success = false;
        var error = string.Empty;
        try
        {
            var configuredBranch = _settings.BranchName?.Trim();
            if (string.IsNullOrWhiteSpace(configuredBranch)
                || !string.Equals(branchName.Trim(), configuredBranch, StringComparison.OrdinalIgnoreCase)
                || !string.Equals(transfer.branch?.Trim(), configuredBranch, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException($"Transfer recipient {transfer.branch ?? "(missing)"} does not match this installation's configured branch.");
            }

            branchName = configuredBranch;
            var isStockTake = decimal.TryParse(transfer.target_quantity, NumberStyles.Number, CultureInfo.InvariantCulture, out var targetQuantity);
            if (!decimal.TryParse(transfer.quantity, NumberStyles.Number, CultureInfo.InvariantCulture, out var quantity) || quantity < 0 || (!isStockTake && quantity == 0) || (isStockTake && targetQuantity < 0))
            {
                throw new InvalidOperationException($"Invalid transfer quantity: {transfer.quantity}");
            }

            _syncQueueListBox.Items.Insert(0, isStockTake
                ? $"[{DateTime.Now:HH:mm:ss}] [STOCK TAKE] Replacing branch stock for product {transfer.product_id} with exact quantity {targetQuantity}."
                : $"[{DateTime.Now:HH:mm:ss}] [TRANSFER] Adding quantity {quantity} for product {transfer.product_id}.");

            var finalBalance = await RecordStockMovementAsync(transfer, branchName, isStockTake, quantity, targetQuantity);

            success = true;
            _syncQueueListBox.Items.Insert(0, isStockTake
                ? $"[{DateTime.Now:HH:mm:ss}] ✓ STOCK TAKE APPLIED: {transfer.product_name} (Product {transfer.product_id}), exact quantity {finalBalance} recorded in product movement."
                : $"[{DateTime.Now:HH:mm:ss}] ✓ STOCK RECEIVED: {transfer.product_name} (Product {transfer.product_id}), quantity {quantity}; movement balance {finalBalance}.");
        }
        catch (Exception ex)
        {
            error = ex.Message;
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✗ STOCK TRANSFER FAILED: {error}");
        }

        var completion = new
        {
            transfer_id = transfer.id,
            branch = branchName,
            success,
            error,
        };
        using var content = new StringContent(JsonSerializer.Serialize(completion), Encoding.UTF8, "application/json");
        var completionResponse = await client.PostAsync($"{_settings.GetApiBaseUrl()}/api/stock/transfers/complete/", content);
        _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] [TRANSFER] API acknowledgement for {transfer.id}: {(completionResponse.IsSuccessStatusCode ? "accepted" : completionResponse.StatusCode)}");
    }

    private async Task<decimal> RecordStockMovementAsync(
        StockTransfer transfer,
        string branchName,
        bool isStockTake,
        decimal quantity,
        decimal targetQuantity)
    {
        if (string.IsNullOrWhiteSpace(transfer.id))
        {
            throw new InvalidOperationException("The stock command has no ID and cannot be safely processed more than once.");
        }

        using var connection = new SqlConnection(_settings.BuildConnectionString());
        await connection.OpenAsync();
        using var transaction = connection.BeginTransaction();
        try
        {
            var commandMarker = $"PV:{transfer.id}";
            const string existingMovementSql = @"
                SELECT TOP (1) EntryNo
                FROM [dbo].[Movement] WITH (UPDLOCK, HOLDLOCK)
                WHERE OtherDetail = @commandMarker
                  AND ProductID = @productId
                  AND coid = @coid
                  AND UPPER(LTRIM(RTRIM(CAST(Branch AS nvarchar(100))))) = UPPER(LTRIM(RTRIM(@branch)))
                ORDER BY EntryNo DESC;";
            using (var existingMovementCommand = new SqlCommand(existingMovementSql, connection, transaction))
            {
                existingMovementCommand.Parameters.AddWithValue("@commandMarker", commandMarker);
                existingMovementCommand.Parameters.AddWithValue("@productId", transfer.product_id);
                existingMovementCommand.Parameters.AddWithValue("@coid", BranchCompanyId);
                existingMovementCommand.Parameters.AddWithValue("@branch", branchName);
                var existingMovement = await existingMovementCommand.ExecuteScalarAsync();
                if (existingMovement is not null and not DBNull)
                {
                    var existingBalance = await GetProductStockBalanceAsync(connection, transaction, transfer.product_id, branchName);
                    await transaction.CommitAsync();
                    _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] Existing movement found for command {transfer.id}; skipped duplicate stock update.");
                    return existingBalance;
                }
            }

            const string productSql = @"
                SELECT TOP (1) COALESCE(Cost, 0) AS Cost
                FROM [dbo].[Products]
                WHERE ProductID = @productId AND coid = @coid;";
            decimal productCost;
            using (var productCommand = new SqlCommand(productSql, connection, transaction))
            {
                productCommand.Parameters.AddWithValue("@productId", transfer.product_id);
                productCommand.Parameters.AddWithValue("@coid", BranchCompanyId);
                var cost = await productCommand.ExecuteScalarAsync();
                if (cost is null or DBNull)
                {
                    throw new InvalidOperationException($"Product {transfer.product_id} was not found for company {BranchCompanyId}.");
                }

                productCost = Convert.ToDecimal(cost, CultureInfo.InvariantCulture);
            }

            var currentBalance = await GetProductStockBalanceAsync(connection, transaction, transfer.product_id, branchName);
            var expectedBalance = isStockTake ? targetQuantity : currentBalance + quantity;
            var invoiceNumber = await GetStockMovementInvoiceNumberAsync(connection, transaction, transfer.id, branchName, isStockTake);
            var today = int.Parse(DateTime.Today.ToString("yyyyMMdd", CultureInfo.InvariantCulture), CultureInfo.InvariantCulture);
            var time = int.Parse(DateTime.Now.ToString("HHmm", CultureInfo.InvariantCulture), CultureInfo.InvariantCulture);
            var reference = isStockTake
                ? targetQuantity > currentBalance ? "STOCK INCREASED" : targetQuantity < currentBalance ? "STOCK REDUCED" : "STOCK MAINTAINED"
                : "NEW STOCK IN";

            if (!isStockTake)
            {
                using var movementCommand = new SqlCommand("InsertNewStock", connection, transaction)
                {
                    CommandType = CommandType.StoredProcedure
                };
                movementCommand.Parameters.AddWithValue("@trandate", today);
                movementCommand.Parameters.AddWithValue("@invoicenum", invoiceNumber);
                movementCommand.Parameters.AddWithValue("@productid", transfer.product_id);
                movementCommand.Parameters.AddWithValue("@isstockin", 1);
                movementCommand.Parameters.AddWithValue("@ref", reference);
                movementCommand.Parameters.AddWithValue("@quantity", quantity);
                movementCommand.Parameters.AddWithValue("@cost", productCost);
                movementCommand.Parameters.AddWithValue("@doneby", Environment.UserName);
                movementCommand.Parameters.AddWithValue("@donewhen", today);
                movementCommand.Parameters.AddWithValue("@trancode", 1);
                movementCommand.Parameters.AddWithValue("@coid", BranchCompanyId);
                movementCommand.Parameters.AddWithValue("@branch", branchName);
                movementCommand.Parameters.AddWithValue("@suppliername", string.Empty);
                await movementCommand.ExecuteNonQueryAsync();
            }
            else if (targetQuantity < currentBalance)
            {
                using var movementCommand = new SqlCommand("InsertNewSaleStockTake", connection, transaction)
                {
                    CommandType = CommandType.StoredProcedure
                };
                movementCommand.Parameters.AddWithValue("@trandate", today);
                movementCommand.Parameters.AddWithValue("@invoicenum", invoiceNumber);
                movementCommand.Parameters.AddWithValue("@productid", transfer.product_id);
                movementCommand.Parameters.AddWithValue("@isstockin", 0);
                movementCommand.Parameters.AddWithValue("@ref", reference);
                movementCommand.Parameters.AddWithValue("@quantity", currentBalance - targetQuantity);
                movementCommand.Parameters.AddWithValue("@unitcost", productCost);
                movementCommand.Parameters.AddWithValue("@salesprice", 0m);
                movementCommand.Parameters.AddWithValue("@paymentmethod", 0);
                movementCommand.Parameters.AddWithValue("@doneby", Environment.UserName);
                movementCommand.Parameters.AddWithValue("@donewhen", today);
                movementCommand.Parameters.AddWithValue("@trancode", 2);
                movementCommand.Parameters.AddWithValue("@TranTime", time);
                movementCommand.Parameters.AddWithValue("@coid", BranchCompanyId);
                movementCommand.Parameters.AddWithValue("@branch", branchName);
                movementCommand.Parameters.AddWithValue("@details", commandMarker);
                movementCommand.Parameters.AddWithValue("@stockbal", expectedBalance);
                movementCommand.Parameters.AddWithValue("@machinename", Environment.MachineName);
                await movementCommand.ExecuteNonQueryAsync();
            }
            else
            {
                using var movementCommand = new SqlCommand("InsertNewStockStockTake", connection, transaction)
                {
                    CommandType = CommandType.StoredProcedure
                };
                movementCommand.Parameters.AddWithValue("@trandate", today);
                movementCommand.Parameters.AddWithValue("@invoicenum", invoiceNumber);
                movementCommand.Parameters.AddWithValue("@productid", transfer.product_id);
                movementCommand.Parameters.AddWithValue("@isstockin", 1);
                movementCommand.Parameters.AddWithValue("@ref", reference);
                movementCommand.Parameters.AddWithValue("@quantity", targetQuantity - currentBalance);
                movementCommand.Parameters.AddWithValue("@cost", productCost);
                movementCommand.Parameters.AddWithValue("@doneby", Environment.UserName);
                movementCommand.Parameters.AddWithValue("@donewhen", today);
                movementCommand.Parameters.AddWithValue("@trancode", 2);
                movementCommand.Parameters.AddWithValue("@coid", BranchCompanyId);
                movementCommand.Parameters.AddWithValue("@branch", branchName);
                movementCommand.Parameters.AddWithValue("@details", commandMarker);
                movementCommand.Parameters.AddWithValue("@stockbal", expectedBalance);
                movementCommand.Parameters.AddWithValue("@machinename", Environment.MachineName);
                movementCommand.Parameters.AddWithValue("@trantime", DateTime.Now.ToString("HH:mm", CultureInfo.InvariantCulture));
                await movementCommand.ExecuteNonQueryAsync();
            }

            using (var refreshBalanceCommand = new SqlCommand("SetQuickStockBalForOne", connection, transaction)
            {
                CommandType = CommandType.StoredProcedure
            })
            {
                refreshBalanceCommand.Parameters.AddWithValue("@coid", BranchCompanyId);
                refreshBalanceCommand.Parameters.AddWithValue("@branch", branchName);
                refreshBalanceCommand.Parameters.AddWithValue("@iswarehouse", 0);
                refreshBalanceCommand.Parameters.AddWithValue("@productid", transfer.product_id);
                await refreshBalanceCommand.ExecuteNonQueryAsync();
            }

            var actualBalance = await GetProductStockBalanceAsync(connection, transaction, transfer.product_id, branchName);
            if (actualBalance != expectedBalance)
            {
                throw new InvalidOperationException($"Movement procedure produced stock balance {actualBalance}, but expected {expectedBalance}; the command was rolled back.");
            }

            const string updateMovementSql = @"
                ;WITH NewMovement AS (
                    SELECT TOP (1) *
                    FROM [dbo].[Movement]
                    WHERE InvoiceNum = @invoiceNum
                      AND ProductID = @productId
                      AND coid = @coid
                      AND UPPER(LTRIM(RTRIM(CAST(Branch AS nvarchar(100))))) = UPPER(LTRIM(RTRIM(@branch)))
                    ORDER BY EntryNo DESC
                )
                UPDATE NewMovement
                SET OtherDetail = @commandMarker;";
            using (var updateMovementCommand = new SqlCommand(updateMovementSql, connection, transaction))
            {
                updateMovementCommand.Parameters.AddWithValue("@invoiceNum", invoiceNumber);
                updateMovementCommand.Parameters.AddWithValue("@productId", transfer.product_id);
                updateMovementCommand.Parameters.AddWithValue("@coid", BranchCompanyId);
                updateMovementCommand.Parameters.AddWithValue("@branch", branchName);
                updateMovementCommand.Parameters.AddWithValue("@commandMarker", commandMarker);
                if (await updateMovementCommand.ExecuteNonQueryAsync() == 0)
                {
                    throw new InvalidOperationException($"The movement row for command {transfer.id} could not be found after insertion.");
                }
            }

            await transaction.CommitAsync();
            return actualBalance;
        }
        catch
        {
            try
            {
                await transaction.RollbackAsync();
            }
            catch
            {
            }

            throw;
        }
    }

    private static async Task<decimal> GetProductStockBalanceAsync(
        SqlConnection connection,
        SqlTransaction transaction,
        int productId,
        string branchName)
    {
        using (var movementCommand = new SqlCommand("ProductMovement", connection, transaction)
        {
            CommandType = CommandType.StoredProcedure
        })
        {
            movementCommand.Parameters.AddWithValue("@startdate", 0);
            movementCommand.Parameters.AddWithValue("@enddate", 99991231);
            movementCommand.Parameters.AddWithValue("@productid", productId);
            movementCommand.Parameters.AddWithValue("@coid", BranchCompanyId);
            movementCommand.Parameters.AddWithValue("@isbatch", 0);
            movementCommand.Parameters.AddWithValue("@branch", branchName);
            movementCommand.Parameters.AddWithValue("@nobal", 0);

            using var reader = await movementCommand.ExecuteReaderAsync();
            if (reader.HasRows)
            {
                var entryNumberOrdinal = reader.GetOrdinal("Entry_No");
                var stockBalanceOrdinal = reader.GetOrdinal("Stock_Balance");
                var latestEntryNumber = int.MinValue;
                decimal latestBalance = 0m;
                while (await reader.ReadAsync())
                {
                    var entryNumber = Convert.ToInt32(reader.GetValue(entryNumberOrdinal), CultureInfo.InvariantCulture);
                    if (entryNumber < latestEntryNumber)
                    {
                        continue;
                    }

                    latestEntryNumber = entryNumber;
                    latestBalance = reader.IsDBNull(stockBalanceOrdinal)
                        ? 0m
                        : Convert.ToDecimal(reader.GetValue(stockBalanceOrdinal), CultureInfo.InvariantCulture);
                }

                return latestBalance;
            }
        }

        return 0m;
    }

    private static async Task<int> GetStockMovementInvoiceNumberAsync(
        SqlConnection connection,
        SqlTransaction transaction,
        string transferId,
        string branchName,
        bool isStockTake)
    {
        var procedureName = isStockTake ? "GetNextStockTakeNumber" : "GetNewInvoiceNumber";
        using var command = new SqlCommand(procedureName, connection, transaction)
        {
            CommandType = CommandType.StoredProcedure
        };

        if (isStockTake)
        {
            command.Parameters.AddWithValue("@coid", BranchCompanyId);
            command.Parameters.AddWithValue("@branch", branchName);
            command.Parameters.AddWithValue("@details", $"PV:{transferId}");
            command.Parameters.AddWithValue("@new_id", 0);
        }
        else
        {
            command.Parameters.AddWithValue("@desc", $"PV:{transferId}");
            command.Parameters.AddWithValue("@newinvoicenum", 0);
        }

        var invoiceNumber = await command.ExecuteScalarAsync();
        if (invoiceNumber is null or DBNull)
        {
            throw new InvalidOperationException($"Quantum did not allocate an invoice number for stock command {transferId}.");
        }

        return Convert.ToInt32(invoiceNumber, CultureInfo.InvariantCulture);
    }

    private async Task ApplyBranchPriceUpdateAsync(BranchPriceUpdate priceUpdate, HttpClient client, string branchName)
    {
        var success = false;
        var error = string.Empty;
        try
        {
            if (!decimal.TryParse(priceUpdate.selling_price, NumberStyles.Number, CultureInfo.InvariantCulture, out var sellingPrice) || sellingPrice < 0)
            {
                throw new InvalidOperationException($"Invalid selling price: {priceUpdate.selling_price}");
            }

            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();
            const string updateSql = @"
                UPDATE [dbo].[Products]
                SET SellingPrice = @sellingPrice
                WHERE ProductID = @productId;";
            using var command = new SqlCommand(updateSql, connection);
            command.Parameters.AddWithValue("@sellingPrice", sellingPrice);
            command.Parameters.AddWithValue("@productId", priceUpdate.product_id);
            var affected = await command.ExecuteNonQueryAsync();
            if (affected == 0)
            {
                throw new InvalidOperationException($"Product {priceUpdate.product_id} was not found in dbo.Products.");
            }

            success = true;
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] PRICE UPDATED: {priceUpdate.product_name} to {sellingPrice.ToString("0.00", CultureInfo.InvariantCulture)}");
        }
        catch (Exception ex)
        {
            error = ex.Message;
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] PRICE UPDATE FAILED: {error}");
        }

        var completion = new
        {
            branch = branchName,
            product_id = priceUpdate.product_id,
            success,
            error,
        };
        using var content = new StringContent(JsonSerializer.Serialize(completion), Encoding.UTF8, "application/json");
        var completionResponse = await client.PostAsync($"{_settings.GetApiBaseUrl()}/api/stock/prices/complete/", content);
        _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] [PRICE] API acknowledgement for product {priceUpdate.product_id}: {(completionResponse.IsSuccessStatusCode ? "accepted" : completionResponse.StatusCode)}");
    }

    private async Task ApplyBranchProductCreationAsync(BranchProductCreation productCreation, HttpClient client, string branchName)
    {
        var success = false;
        var error = string.Empty;
        var actualProductId = productCreation.product_id;
        try
        {
            if (string.IsNullOrWhiteSpace(productCreation.product_name))
            {
                throw new InvalidOperationException("A product name is required.");
            }

            if (!decimal.TryParse(productCreation.selling_price, NumberStyles.Number, CultureInfo.InvariantCulture, out var sellingPrice) || sellingPrice < 0)
            {
                throw new InvalidOperationException($"Invalid selling price: {productCreation.selling_price}");
            }

            if (!decimal.TryParse(productCreation.tax_rate, NumberStyles.Number, CultureInfo.InvariantCulture, out var taxRate) || taxRate < 0 || taxRate > 100)
            {
                throw new InvalidOperationException($"Invalid tax rate: {productCreation.tax_rate}");
            }

            if (!decimal.TryParse(productCreation.initial_quantity, NumberStyles.Number, CultureInfo.InvariantCulture, out var initialQuantity) || initialQuantity < 0)
            {
                throw new InvalidOperationException($"Invalid opening quantity: {productCreation.initial_quantity}");
            }

            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();
            using var transaction = connection.BeginTransaction();
            try
            {
            if (actualProductId < 12100000)
            {
                const string nextProductIdSql = @"
                    SELECT CASE
                        WHEN ISNULL(MAX(ProductID), 0) < 12100000 THEN 12100000
                        ELSE MAX(ProductID) + 1
                    END
                    FROM [dbo].[Products] WITH (UPDLOCK, HOLDLOCK);";
                using var nextProductIdCommand = new SqlCommand(nextProductIdSql, connection, transaction);
                actualProductId = Convert.ToInt32(await nextProductIdCommand.ExecuteScalarAsync(), CultureInfo.InvariantCulture);
            }

            const string existingProductSql = "SELECT ProductDesc, BarCode FROM [dbo].[Products] WITH (UPDLOCK, HOLDLOCK) WHERE ProductID = @productId;";
            string? existingProductName = null;
            string? existingBarcode = null;
            using (var existingProductCommand = new SqlCommand(existingProductSql, connection, transaction))
            {
                existingProductCommand.Parameters.AddWithValue("@productId", actualProductId);
                using var existingProductReader = await existingProductCommand.ExecuteReaderAsync();
                if (await existingProductReader.ReadAsync())
                {
                    existingProductName = Convert.ToString(existingProductReader["ProductDesc"], CultureInfo.InvariantCulture)?.Trim();
                    existingBarcode = Convert.ToString(existingProductReader["BarCode"], CultureInfo.InvariantCulture)?.Trim();
                }
            }

            var requestedProductName = productCreation.product_name.Trim();
            var requestedBarcode = productCreation.barcode?.Trim() ?? string.Empty;
            if (existingProductName is not null)
            {
                if (!string.Equals(existingProductName, requestedProductName, StringComparison.OrdinalIgnoreCase)
                    || (!string.IsNullOrEmpty(requestedBarcode) && !string.Equals(existingBarcode, requestedBarcode, StringComparison.Ordinal)))
                {
                    throw new InvalidOperationException($"Product ID {actualProductId} already belongs to '{existingProductName}' in dbo.Products; it does not match '{requestedProductName}'.");
                }
            }
            else
            {
                actualProductId = await InsertPosProductAsync(
                    connection,
                    transaction,
                    productCreation.product_id,
                    requestedProductName,
                    (actualProductId + 1).ToString(CultureInfo.InvariantCulture),
                    requestedBarcode,
                    sellingPrice,
                    taxRate);
            }

            const string insertBalanceSql = @"
                IF NOT EXISTS (
                    SELECT 1 FROM [dbo].[ProductStockBalances]
                    WHERE ProductID = @productId AND branch = @branch
                )
                INSERT INTO [dbo].[ProductStockBalances]
                    (ProductID, StockBal, MvtEntryNo, coid, branch, batchnumber, expirydate)
                VALUES (
                    @productId,
                    @initialQuantity,
                    ISNULL((SELECT MAX(MvtEntryNo) FROM [dbo].[ProductStockBalances] WHERE ProductID = @productId), 0) + 1,
                    @coid,
                    @branch,
                    NULL,
                    NULL);";
            using var balanceCommand = new SqlCommand(insertBalanceSql, connection, transaction);
            balanceCommand.Parameters.AddWithValue("@productId", actualProductId);
            balanceCommand.Parameters.AddWithValue("@initialQuantity", initialQuantity);
            balanceCommand.Parameters.AddWithValue("@coid", BranchCompanyId);
            balanceCommand.Parameters.AddWithValue("@branch", branchName);
            await balanceCommand.ExecuteNonQueryAsync();
            transaction.Commit();
            }
            catch
            {
                try { transaction.Rollback(); } catch { }
                throw;
            }
            success = true;
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✓ PRODUCT CREATED: {productCreation.product_name} (Product {actualProductId})");
        }
        catch (Exception ex)
        {
            error = ex.Message;
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] PRODUCT CREATION FAILED: {error}");
        }

        var completion = new
        {
            branch = branchName,
            product_id = productCreation.product_id,
            actual_product_id = actualProductId,
            success,
            error,
        };
        using var content = new StringContent(JsonSerializer.Serialize(completion), Encoding.UTF8, "application/json");
        var completionResponse = await client.PostAsync($"{_settings.GetApiBaseUrl()}/api/products/create/complete/", content);
        _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] [PRODUCT] API acknowledgement: {(completionResponse.IsSuccessStatusCode ? "accepted" : completionResponse.StatusCode)}");
    }

    private async Task ApplyBranchProductDeletionAsync(BranchProductDeletion productDeletion, HttpClient client, string branchName)
    {
        var success = false;
        var error = string.Empty;

        try
        {
            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();
            const string deactivateSql = "UPDATE [dbo].[Products] SET IsActive = 0 WHERE ProductID = @productId;";
            using var command = new SqlCommand(deactivateSql, connection);
            command.Parameters.AddWithValue("@productId", productDeletion.product_id);
            var affectedRows = await command.ExecuteNonQueryAsync();
            if (affectedRows == 0)
            {
                throw new InvalidOperationException($"Product {productDeletion.product_id} was not found in dbo.Products.");
            }

            success = true;
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✓ PRODUCT DEACTIVATED: {productDeletion.product_name} (Product {productDeletion.product_id}) at {branchName}; sales history retained.");
        }
        catch (Exception ex)
        {
            error = ex.Message;
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✗ PRODUCT DELETION FAILED: {productDeletion.product_name} | {error}");
        }

        var completion = new
        {
            request_id = productDeletion.request_id,
            branch = branchName,
            success,
            error,
        };
        using var content = new StringContent(JsonSerializer.Serialize(completion), Encoding.UTF8, "application/json");
        var response = await client.PostAsync($"{_settings.GetApiBaseUrl()}/api/products/delete/complete/", content);
        _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] [PRODUCT] Deletion acknowledgement: {(response.IsSuccessStatusCode ? "accepted" : response.StatusCode)}");
    }

    private async Task<int> DeleteLocalInvoiceAsync(string invoiceNum, string branchName, int productId = 0)
    {
        if (string.IsNullOrWhiteSpace(invoiceNum))
        {
            return 0;
        }

        try
        {
            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();

            int totalDeletedRows = 0;

            // Strategy 1: Try exact match with string comparison (for nvarchar/string InvoiceNum)
            var deleteSql1 = @"
                DELETE FROM [dbo].[Movement]
                WHERE InvoiceNum = @invoiceNum
                  AND Branch = @branchName";
            
            if (productId > 0)
            {
                deleteSql1 += " AND ProductID = @productId";
            }
            deleteSql1 += ";";

            using (var command = new SqlCommand(deleteSql1, connection))
            {
                command.Parameters.AddWithValue("@invoiceNum", invoiceNum);
                command.Parameters.AddWithValue("@branchName", branchName);
                if (productId > 0)
                {
                    command.Parameters.AddWithValue("@productId", productId);
                }
                
                var result = await command.ExecuteNonQueryAsync();
                totalDeletedRows += result;
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] DEBUG: Strategy 1 (string match) deleted {result} row(s)");
            }

            // Strategy 2: If nothing deleted and InvoiceNum is numeric, try as INT
            if (totalDeletedRows == 0 && int.TryParse(invoiceNum, out var invoiceInt))
            {
                var deleteSql2 = @"
                    DELETE FROM [dbo].[Movement]
                    WHERE CAST(InvoiceNum AS nvarchar(50)) = @invoiceNum
                      AND CAST(Branch AS nvarchar(100)) = @branchName";
                
                if (productId > 0)
                {
                    deleteSql2 += " AND ProductID = @productId";
                }
                deleteSql2 += ";";

                using (var command = new SqlCommand(deleteSql2, connection))
                {
                    command.Parameters.AddWithValue("@invoiceNum", invoiceNum);
                    command.Parameters.AddWithValue("@branchName", branchName);
                    if (productId > 0)
                    {
                        command.Parameters.AddWithValue("@productId", productId);
                    }
                    
                    var result = await command.ExecuteNonQueryAsync();
                    totalDeletedRows += result;
                    _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] DEBUG: Strategy 2 (CAST match) deleted {result} row(s)");
                }
            }

            // Strategy 3: If still nothing, try case-insensitive branch match
            if (totalDeletedRows == 0)
            {
                var deleteSql3 = @"
                    DELETE FROM [dbo].[Movement]
                    WHERE (InvoiceNum = @invoiceNum OR CAST(InvoiceNum AS nvarchar(50)) = @invoiceNum)
                      AND UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@branchName)";
                
                if (productId > 0)
                {
                    deleteSql3 += " AND ProductID = @productId";
                }
                deleteSql3 += ";";

                using (var command = new SqlCommand(deleteSql3, connection))
                {
                    command.Parameters.AddWithValue("@invoiceNum", invoiceNum);
                    command.Parameters.AddWithValue("@branchName", branchName);
                    if (productId > 0)
                    {
                        command.Parameters.AddWithValue("@productId", productId);
                    }
                    
                    var result = await command.ExecuteNonQueryAsync();
                    totalDeletedRows += result;
                    _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] DEBUG: Strategy 3 (case-insensitive) deleted {result} row(s)");
                }
            }

            if (totalDeletedRows == 0)
            {
                // Log available data for debugging
                var checkSql = @"
                    SELECT TOP 5 InvoiceNum, Branch, ProductID, EntryNo 
                    FROM [dbo].[Movement] 
                    WHERE UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@branchName)
                    ORDER BY InvoiceNum DESC;";
                
                using (var checkCmd = new SqlCommand(checkSql, connection))
                {
                    checkCmd.Parameters.AddWithValue("@branchName", branchName);
                    using (var reader = await checkCmd.ExecuteReaderAsync())
                    {
                        if (reader.HasRows)
                        {
                            var samples = "Last invoices in DB: ";
                            while (await reader.ReadAsync())
                            {
                                samples += $"[{reader[0]}],";
                            }
                            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] DEBUG: {samples}");
                        }
                    }
                }
            }

            return totalDeletedRows;
        }
        catch (Exception ex)
        {
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✗ ERROR: Failed to delete invoice {invoiceNum}: {ex.Message}");
            return 0;
        }
    }

    private async Task ProcessInvoiceReprintAsync(InvoiceReprintRequest reprint, HttpClient client, string branchName)
    {
        var success = false;
        var error = string.Empty;
        try
        {
            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();
            const string invoiceLinesSql = @"
                SELECT
                    m.ProductID,
                    COALESCE(NULLIF(LTRIM(RTRIM(p.ProductDesc)), ''), CONCAT('Product ', m.ProductID)) AS ProductName,
                    ABS(COALESCE(m.Quantity, 0)) AS Quantity,
                    COALESCE(m.SellingPrice, 0) AS UnitPrice,
                    ABS((COALESCE(m.Quantity, 0) * COALESCE(m.SellingPrice, 0)) - COALESCE(m.DiscountAmt, 0) - COALESCE(m.InvDiscount, 0) + COALESCE(m.TaxAmt, 0)) AS LineTotal
                FROM [dbo].[Movement] AS m
                LEFT JOIN [dbo].[Products] AS p ON p.ProductID = m.ProductID
                WHERE (m.InvoiceNum = @invoiceNum OR CAST(m.InvoiceNum AS nvarchar(50)) = @invoiceNum)
                  AND UPPER(CAST(m.Branch AS nvarchar(100))) = UPPER(@branch)
                ORDER BY m.ProductID;";

            var products = new List<string>();
            decimal total = 0m;
            using var command = new SqlCommand(invoiceLinesSql, connection);
            command.Parameters.AddWithValue("@invoiceNum", reprint.invoice ?? string.Empty);
            command.Parameters.AddWithValue("@branch", branchName);
            using var reader = await command.ExecuteReaderAsync();
            while (await reader.ReadAsync())
            {
                var productName = reader[1]?.ToString()?.Trim() ?? string.Empty;
                var quantity = Convert.ToDecimal(reader[2], CultureInfo.InvariantCulture);
                var unitPrice = Convert.ToDecimal(reader[3], CultureInfo.InvariantCulture);
                var lineTotal = Convert.ToDecimal(reader[4], CultureInfo.InvariantCulture);
                products.Add($"{productName} x{quantity:0.##} @ {unitPrice:0.00} = {lineTotal:0.00}");
                total += lineTotal;
            }

            if (products.Count == 0)
            {
                throw new InvalidOperationException($"No products found for invoice {reprint.invoice}.");
            }

            success = ReceiptPrinter.TryPrint(
                _settings.PrinterName,
                "INVOICE REPRINT",
                new List<(string Label, string Value)>
                {
                    ("Status", "REPRINTED"),
                    ("Invoice", reprint.invoice ?? string.Empty),
                    ("Branch", branchName),
                    ("Products", string.Join(Environment.NewLine, products)),
                    ("Total", total.ToString("0.00", CultureInfo.InvariantCulture)),
                    ("Date", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss")),
                    ("Machine", Environment.MachineName)
                },
                out error);
        }
        catch (Exception ex)
        {
            error = ex.Message;
        }

        var completion = new
        {
            request_id = reprint.request_id,
            branch = branchName,
            success,
            error,
        };
        using var content = new StringContent(JsonSerializer.Serialize(completion), Encoding.UTF8, "application/json");
        await client.PostAsync($"{_settings.GetApiBaseUrl()}/api/invoice-reprint/complete/", content);
        _syncQueueListBox.Items.Insert(0, success
            ? $"[{DateTime.Now:HH:mm:ss}] ✓ INVOICE REPRINTED: {reprint.invoice}"
            : $"[{DateTime.Now:HH:mm:ss}] ✗ INVOICE REPRINT FAILED: {reprint.invoice} | {error}");
    }

    private async Task ProcessSalesReportAsync(SalesReportRequest report, HttpClient client, string branchName)
    {
        var success = false;
        var rowCount = 0;
        var error = string.Empty;

        try
        {
            if (!DateTime.TryParseExact(report.report_date, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var reportDate))
            {
                throw new InvalidOperationException($"Invalid report date: {report.report_date}");
            }

            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();
                        const string query = @"
                            WITH Sales AS (
                                SELECT
                                    CAST(m.InvoiceNum AS nvarchar(100)) AS InvoiceNumber,
                                    COALESCE(NULLIF(LTRIM(RTRIM(m.DoneBy)), ''), 'Unknown') AS Cashier,
                                    COALESCE(pm.PaymentMethodDesc, CONCAT('Method ', COALESCE(CAST(m.ReceiptDisplayPaymentMethod AS nvarchar(20)), '0'))) AS PaymentMethod,
                                    COALESCE(NULLIF(pm.Currency, ''), 'UNKNOWN') AS Currency,
                                    CAST((
                                        (
                                            (COALESCE(m.Quantity, 0) * COALESCE(m.SellingPrice, 0))
                                            - COALESCE(m.DiscountAmt, 0)
                                            - CASE WHEN ROW_NUMBER() OVER (PARTITION BY m.InvoiceNum ORDER BY m.ProductID, m.EntryNo) = 1
                                                   THEN COALESCE(m.InvDiscount, 0) ELSE 0 END
                                            + COALESCE(m.TaxAmt, 0)
                                        )
                                    ) AS decimal(28, 6)) AS SaleTotal
                                    ,CAST(COALESCE(m.TaxAmt, 0) AS decimal(28, 6)) AS TaxTotal
                                    ,CAST(CASE WHEN COALESCE(m.ReceiptDisplayRate, 0) = 0 THEN 1 ELSE m.ReceiptDisplayRate END AS decimal(28, 6)) AS Rate
                                FROM [dbo].[Movement] AS m
                                OUTER APPLY (
                                    SELECT TOP 1 PaymentMethodDesc, Currency
                                    FROM [dbo].[PaymentMethods]
                                    WHERE PaymentMethodID = m.ReceiptDisplayPaymentMethod
                                      AND (coid = m.coid OR coid IS NULL)
                                    ORDER BY CASE WHEN coid = m.coid THEN 0 ELSE 1 END
                                ) AS pm
                                WHERE m.TranDate = @reportDate
                                  AND UPPER(CAST(m.Branch AS nvarchar(100))) = UPPER(@branch)
                                  AND ISNULL(m.IsStockIn, 0) = 0
                            )
                                SELECT
                                InvoiceNumber,
                                Cashier,
                                PaymentMethod,
                                Currency,
                                Rate,
                                CAST(SUM(SaleTotal) AS decimal(28, 2)) AS Total,
                                CAST(SUM(TaxTotal) AS decimal(28, 2)) AS TaxTotal
                                FROM Sales
                                    GROUP BY InvoiceNumber, Cashier, PaymentMethod, Currency, Rate
                                    ORDER BY Cashier, InvoiceNumber, PaymentMethod, Currency, Rate;";

            using var command = new SqlCommand(query, connection);
            command.Parameters.Add("@reportDate", SqlDbType.Int).Value = int.Parse(reportDate.ToString("yyyyMMdd"));
            command.Parameters.AddWithValue("@branch", branchName);
            using var adapter = new SqlDataAdapter(command);
            var table = new DataTable();
            adapter.Fill(table);
            rowCount = table.Rows.Count;

            success = ReceiptPrinter.TryPrintMovementReport(
                _settings.PrinterName,
                "END-OF-DAY SALES REPORT",
                table,
                branchName,
                reportDate,
                out error);

            _syncQueueListBox.Items.Insert(0, success
                ? $"[{DateTime.Now:HH:mm:ss}] ✓ SALES REPORT PRINTED: {rowCount} row(s) for {branchName} on {reportDate:yyyy-MM-dd}"
                : $"[{DateTime.Now:HH:mm:ss}] ✗ SALES REPORT NOT PRINTED: {error}");
        }
        catch (Exception ex)
        {
            error = ex.Message;
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✗ SALES REPORT FAILED: {error}");
        }

        var completion = new
        {
            report_id = report.id,
            success,
            row_count = rowCount,
            error,
            branch = branchName,
            printed_by = Environment.MachineName
        };
        using var content = new StringContent(JsonSerializer.Serialize(completion), Encoding.UTF8, "application/json");
        await client.PostAsync($"{_settings.GetApiBaseUrl()}/api/sales-report/complete/", content);
    }

    private sealed class BranchSyncTriggerResponse
    {
        public string? status { get; set; }
        public string? service { get; set; }
        public string? branch_filter { get; set; }
        public List<DeletionTrigger>? pending_deletions { get; set; }
        public List<SalesReportRequest>? pending_reports { get; set; }
        public List<StockTransfer>? pending_transfers { get; set; }
        public List<BranchPriceUpdate>? pending_price_updates { get; set; }
        public List<BranchProductCreation>? pending_product_creations { get; set; }
        public List<BranchProductDeletion>? pending_product_deletions { get; set; }
        public List<InvoiceReprintRequest>? pending_invoice_reprints { get; set; }
        public int count { get; set; }
        public string? message { get; set; }
    }

    private sealed class DeletionTrigger
    {
        public string? id { get; set; }
        public string? branch { get; set; }
        public string? invoice { get; set; }
        public int? product_id { get; set; }
        public string? entry_no { get; set; }
        public decimal? quantity { get; set; }
        public int? coid { get; set; }
        public string? action { get; set; }
        public string? status { get; set; }
        public string? source { get; set; }
        public string? message { get; set; }
    }

    private sealed class SalesReportRequest
    {
        public string? id { get; set; }
        public string? branch { get; set; }
        public string? report_date { get; set; }
        public string? status { get; set; }
    }

    private sealed class StockTransfer
    {
        public string? id { get; set; }
        public string? branch { get; set; }
        public int product_id { get; set; }
        public string? product_name { get; set; }
        public string? quantity { get; set; }
        public string? target_quantity { get; set; }
        public string? status { get; set; }
    }

    private sealed class BranchPriceUpdate
    {
        public string? branch { get; set; }
        public int product_id { get; set; }
        public string? product_name { get; set; }
        public string? selling_price { get; set; }
    }

    private sealed class InvoiceReprintRequest
    {
        public string? request_id { get; set; }
        public string? branch { get; set; }
        public string? invoice { get; set; }
    }

    private sealed class BranchProductCreation
    {
        public string? branch { get; set; }
        public int product_id { get; set; }
        public string? product_name { get; set; }
        public string? product_code { get; set; }
        public string? barcode { get; set; }
        public string? initial_quantity { get; set; }
        public string? selling_price { get; set; }
        public string? tax_rate { get; set; }
    }

    private sealed class BranchProductDeletion
    {
        public int request_id { get; set; }
        public string? branch { get; set; }
        public int product_id { get; set; }
        public string? product_name { get; set; }
    }
}
