using System.Data;
using System.Data.SqlClient;
using System.Globalization;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;

namespace POSViewer;

public sealed class BranchSyncDashboardForm : Form
{
    private readonly ConnectionForm _connectionForm;
    private readonly ConnectionSettings _settings;
    private readonly ListBox _syncQueueListBox = new();
    private readonly Label _statusLabel = new();
    private readonly Button _syncNowButton = new();
    private readonly Button _backButton = new();
    private readonly System.Windows.Forms.Timer _autoPollTimer = new();
    private readonly SemaphoreSlim _syncGate = new(1, 1);
    private string? _branchName;
    private bool _productCatalogSynced;
    private DateTime _lastProductCatalogSyncUtc = DateTime.MinValue;

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

        _autoPollTimer.Interval = 5000;
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
            using var client = new HttpClient { Timeout = TimeSpan.FromMinutes(3) };
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

            if (!_productCatalogSynced || DateTime.UtcNow - _lastProductCatalogSyncUtc >= TimeSpan.FromSeconds(5))
            {
                _productCatalogSynced = await SyncProductCatalogAsync(client, branchName);
                if (_productCatalogSynced)
                {
                    _lastProductCatalogSyncUtc = DateTime.UtcNow;
                }
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
            var pendingInvoiceReprints = result.pending_invoice_reprints ?? new List<InvoiceReprintRequest>();
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] [CONNECTION] Transfer poll succeeded for {branchName}: {pendingTransfers.Count} command(s).");
            
            if (pendingDeletions.Count > 0 || pendingReports.Count > 0 || pendingTransfers.Count > 0 || pendingPriceUpdates.Count > 0 || pendingProductCreations.Count > 0 || pendingInvoiceReprints.Count > 0)
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
                _statusLabel.Text = $"✓ Processed {pendingDeletions.Count} deletion(s), {pendingReports.Count} report(s), {Math.Min(pendingTransfers.Count, 1)} transfer(s) | More transfers remain queued until this is confirmed";
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

    private async Task<string> GetBranchNameAsync()
    {
        if (!string.IsNullOrWhiteSpace(_branchName))
        {
            return _branchName;
        }

        if (!string.IsNullOrWhiteSpace(_settings.BranchName))
        {
            _branchName = _settings.BranchName.Trim();
            return _branchName;
        }

        try
        {
            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();

            const string query = @"
                SELECT TOP (1) CAST(Branch AS nvarchar(50))
                FROM [dbo].[Branches]
                WHERE Branch IS NOT NULL
                  AND LTRIM(RTRIM(CAST(Branch AS nvarchar(50)))) <> ''
                ORDER BY CASE WHEN ISNULL(Visible, 1) = 1 THEN 0 ELSE 1 END, Branch;";

            using var command = new SqlCommand(query, connection);
            var value = await command.ExecuteScalarAsync();
            _branchName = value?.ToString()?.Trim();
        }
        catch (Exception ex)
        {
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] Could not read Branches.Branch: {ex.Message}");
        }

        return string.IsNullOrWhiteSpace(_branchName) ? _settings.Database : _branchName;
    }

    private async Task<bool> SyncProductCatalogAsync(HttpClient client, string branchName)
    {
        try
        {
            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();
            var sellingPriceExpression = await ResolveSellingPriceExpressionAsync(connection);
            var query = $@"
                WITH SoldByProduct AS (
                    SELECT m.ProductID,
                           SUM(CASE WHEN ISNULL(m.IsStockIn, 0) = 0 THEN COALESCE(m.Quantity, 0) ELSE 0 END) AS SoldQuantity
                    FROM [dbo].[Movement] m
                    WHERE UPPER(LTRIM(RTRIM(CAST(m.Branch AS nvarchar(100))))) = UPPER(LTRIM(RTRIM(@branch)))
                    GROUP BY m.ProductID
                ), BalanceByProduct AS (
                    SELECT b.ProductID, SUM(COALESCE(b.StockBal, 0)) AS AvailableQuantity
                    FROM [dbo].[ProductStockBalances] b
                    WHERE UPPER(LTRIM(RTRIM(CAST(b.branch AS nvarchar(100))))) = UPPER(LTRIM(RTRIM(@branch)))
                    GROUP BY b.ProductID
                )
                SELECT p.ProductID, p.ProductDesc, p.ProductCode, p.BarCode,
                       {sellingPriceExpression} AS SellingPrice,
                       COALESCE(s.SoldQuantity, 0) AS SoldQuantity,
                       COALESCE(b.AvailableQuantity, 0) AS AvailableQuantity
                FROM [dbo].[Products] p
                LEFT JOIN SoldByProduct s ON s.ProductID = p.ProductID
                LEFT JOIN BalanceByProduct b ON b.ProductID = p.ProductID
                WHERE p.ProductID IS NOT NULL
                  AND p.ProductDesc IS NOT NULL
                  AND LTRIM(RTRIM(p.ProductDesc)) <> ''
                  AND ISNULL(p.IsActive, 1) = 1;";
            using var command = new SqlCommand(query, connection);
            command.Parameters.AddWithValue("@branch", branchName);
            using var reader = await command.ExecuteReaderAsync();
            var products = new List<object>();
            var availableCount = 0;
            while (await reader.ReadAsync())
            {
                var availableQuantity = Convert.ToDecimal(reader["AvailableQuantity"], CultureInfo.InvariantCulture);
                products.Add(new
                {
                    product_id = Convert.ToInt32(reader["ProductID"]),
                    product_name = reader["ProductDesc"]?.ToString()?.Trim() ?? string.Empty,
                    product_code = reader["ProductCode"]?.ToString()?.Trim() ?? string.Empty,
                    barcode = reader["BarCode"]?.ToString()?.Trim() ?? string.Empty,
                    selling_price = Convert.ToDecimal(reader["SellingPrice"], CultureInfo.InvariantCulture),
                    sold_quantity = Convert.ToDecimal(reader["SoldQuantity"], CultureInfo.InvariantCulture),
                    available_quantity = availableQuantity,
                });
                if (availableQuantity != 0)
                {
                    availableCount++;
                }
            }

            var payload = JsonSerializer.Serialize(new { branch = branchName, entered_by = Environment.UserName, products });
            using var content = new StringContent(payload, Encoding.UTF8, "application/json");
            var response = await client.PostAsync($"{_settings.GetApiBaseUrl()}/api/products/sync/", content);
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] [BALANCE] Read {availableCount} non-zero available stock balance(s) for {branchName}.");
            _syncQueueListBox.Items.Insert(0, response.IsSuccessStatusCode
                ? $"[{DateTime.Now:HH:mm:ss}] ✓ Synced {products.Count} products for {branchName}."
                : $"[{DateTime.Now:HH:mm:ss}] WARNING: Product sync failed: {response.StatusCode}");
            return response.IsSuccessStatusCode;
        }
        catch (Exception ex)
        {
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] WARNING: Product sync unavailable: {ex.Message}");
            return false;
        }
    }

    private static async Task<string> ResolveSellingPriceExpressionAsync(SqlConnection connection)
    {
        const string metadataSql = @"
            SELECT TABLE_NAME, COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'dbo'
                            AND TABLE_NAME IN ('Products', 'Movement', 'ProductStockBalances')
              AND COLUMN_NAME IN ('SellingPrice', 'SalePrice', 'RetailPrice', 'UnitPrice', 'Price');";
        var columns = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        using (var metadataCommand = new SqlCommand(metadataSql, connection))
        using (var reader = await metadataCommand.ExecuteReaderAsync())
        {
            while (await reader.ReadAsync())
            {
                columns.Add($"{reader["TABLE_NAME"]}.{reader["COLUMN_NAME"]}");
            }
        }

        var priceExpressions = new List<string>();
        foreach (var column in new[] { "SellingPrice", "SalePrice", "RetailPrice", "UnitPrice", "Price" })
        {
            if (columns.Contains($"Products.{column}"))
            {
                return $"COALESCE(NULLIF(CONVERT(decimal(18,2), p.[{column}]), 0), 0)";
            }
        }

        foreach (var column in new[] { "SellingPrice", "SalePrice", "RetailPrice", "UnitPrice", "Price" })
        {
            if (columns.Contains($"ProductStockBalances.{column}"))
            {
                priceExpressions.Add($"NULLIF((SELECT TOP 1 CONVERT(decimal(18,2), b.[{column}]) FROM [dbo].[ProductStockBalances] b WHERE b.ProductID = p.ProductID AND UPPER(LTRIM(RTRIM(CAST(b.branch AS nvarchar(100))))) = UPPER(LTRIM(RTRIM(@branch))) ORDER BY b.MvtEntryNo DESC), 0)");
                break;
            }
        }

        foreach (var column in new[] { "SellingPrice", "SalePrice", "RetailPrice", "UnitPrice", "Price" })
        {
            if (columns.Contains($"Movement.{column}"))
            {
                priceExpressions.Add($"NULLIF((SELECT TOP 1 CONVERT(decimal(18,2), m.[{column}]) FROM [dbo].[Movement] m WHERE m.ProductID = p.ProductID AND m.[{column}] IS NOT NULL AND m.[{column}] <> 0 ORDER BY m.TranDate DESC, m.EntryNo DESC), 0)");
                break;
            }
        }

        return priceExpressions.Count == 0
            ? "CONVERT(decimal(18,2), 0)"
            : $"COALESCE({string.Join(", ", priceExpressions)}, 0)";
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
                success = deletedCount > 0
            };
            var confirmJson = JsonSerializer.Serialize(confirmPayload);
            using var confirmContent = new StringContent(confirmJson, Encoding.UTF8, "application/json");
            var confirmResponse = await client.PostAsync(
                $"{_settings.GetApiBaseUrl()}/api/confirm-deletion/",
                confirmContent
            );

            if (confirmResponse.IsSuccessStatusCode)
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
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ⚠ API confirmation failed: {confirmResponse.StatusCode}");
            }
        }
        catch (Exception ex)
        {
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] ✗ Deletion error for invoice {deletion.invoice}: {ex.Message}");
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] Exception details: {ex.StackTrace}");
        }

        return deletedCount;
    }

    private async Task ApplyStockTransferAsync(StockTransfer transfer, HttpClient client, string branchName)
    {
        var success = false;
        var error = string.Empty;
        try
        {
            var isStockTake = decimal.TryParse(transfer.target_quantity, NumberStyles.Number, CultureInfo.InvariantCulture, out var targetQuantity);
            if (!decimal.TryParse(transfer.quantity, NumberStyles.Number, CultureInfo.InvariantCulture, out var quantity) || quantity < 0 || (isStockTake && targetQuantity < 0))
            {
                throw new InvalidOperationException($"Invalid transfer quantity: {transfer.quantity}");
            }

            var quantityParameter = isStockTake ? targetQuantity : quantity;
            _syncQueueListBox.Items.Insert(0, isStockTake
                ? $"[{DateTime.Now:HH:mm:ss}] [STOCK TAKE] Replacing branch stock for product {transfer.product_id} with exact quantity {targetQuantity}."
                : $"[{DateTime.Now:HH:mm:ss}] [TRANSFER] Adding quantity {quantity} for product {transfer.product_id}.");

            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();
            const string branchCompanySql = @"
                SELECT TOP (1) Coid
                FROM [dbo].[Branches]
                WHERE UPPER(LTRIM(RTRIM(Branch))) = UPPER(LTRIM(RTRIM(@branch)));";
            using var branchCompanyCommand = new SqlCommand(branchCompanySql, connection);
            branchCompanyCommand.Parameters.AddWithValue("@branch", branchName);
            var branchCompanyValue = await branchCompanyCommand.ExecuteScalarAsync();
            if (branchCompanyValue is null || branchCompanyValue == DBNull.Value)
            {
                throw new InvalidOperationException($"Branch {branchName} was not found in dbo.Branches.");
            }
            var branchCoid = Convert.ToInt32(branchCompanyValue, CultureInfo.InvariantCulture);
                        const string updateSql = @"
                                IF @isStockTake = 1
                                BEGIN
                                    UPDATE [dbo].[ProductStockBalances]
                                    SET StockBal = 0, coid = @coid
                                    WHERE UPPER(LTRIM(RTRIM(CAST(branch AS nvarchar(100))))) = UPPER(LTRIM(RTRIM(@branch)))
                                        AND ProductID = @productId;

                                    UPDATE TOP (1) [dbo].[ProductStockBalances]
                                    SET StockBal = @quantity, coid = @coid
                                    WHERE UPPER(LTRIM(RTRIM(CAST(branch AS nvarchar(100))))) = UPPER(LTRIM(RTRIM(@branch)))
                                        AND ProductID = @productId;
                                END
                                ELSE
                                BEGIN
                                    UPDATE [dbo].[ProductStockBalances]
                                    SET StockBal = StockBal + @quantity, coid = @coid
                                    WHERE UPPER(LTRIM(RTRIM(CAST(branch AS nvarchar(100))))) = UPPER(LTRIM(RTRIM(@branch)))
                                        AND ProductID = @productId;
                                END";

            using var command = new SqlCommand(updateSql, connection);
            command.Parameters.AddWithValue("@quantity", quantityParameter);
            command.Parameters.AddWithValue("@isStockTake", isStockTake ? 1 : 0);
            command.Parameters.AddWithValue("@branch", branchName);
            command.Parameters.AddWithValue("@productId", transfer.product_id);
            command.Parameters.AddWithValue("@coid", branchCoid);
            var affected = await command.ExecuteNonQueryAsync();
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] [STOCK DB] Updated {affected} balance row(s) for product {transfer.product_id}.");
            if (affected == 0)
            {
                const string createBranchRowSql = @"
                    INSERT INTO [dbo].[ProductStockBalances]
                        (ProductID, StockBal, MvtEntryNo, coid, branch, batchnumber, expirydate)
                                        SELECT TOP 1
                        @productId,
                        @quantity,
                        ISNULL(MAX(MvtEntryNo), 0) + 1,
                        @coid,
                        @branch,
                        MAX(batchnumber),
                        MAX(expirydate)
                    FROM [dbo].[ProductStockBalances]
                                        WHERE ProductID = @productId;";
                using var createBranchRowCommand = new SqlCommand(createBranchRowSql, connection);
                createBranchRowCommand.Parameters.AddWithValue("@quantity", quantity);
                createBranchRowCommand.Parameters.AddWithValue("@branch", branchName);
                createBranchRowCommand.Parameters.AddWithValue("@productId", transfer.product_id);
                createBranchRowCommand.Parameters.AddWithValue("@coid", branchCoid);
                affected = await createBranchRowCommand.ExecuteNonQueryAsync();
            }
            if (affected == 0)
            {
                throw new InvalidOperationException($"No ProductStockBalances row exists for product {transfer.product_id} at {branchName}.");
            }

            success = true;
            _syncQueueListBox.Items.Insert(0, isStockTake
                ? $"[{DateTime.Now:HH:mm:ss}] ✓ STOCK TAKE APPLIED: {transfer.product_name} (Product {transfer.product_id}), exact quantity {targetQuantity}"
                : $"[{DateTime.Now:HH:mm:ss}] ✓ STOCK RECEIVED: {transfer.product_name} (Product {transfer.product_id}), quantity {quantity}");
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

            const string productIdExistsSql = "SELECT COUNT(1) FROM [dbo].[Products] WHERE ProductID = @productId;";
            using (var productIdExistsCommand = new SqlCommand(productIdExistsSql, connection, transaction))
            {
                productIdExistsCommand.Parameters.AddWithValue("@productId", actualProductId);
                if (Convert.ToInt32(await productIdExistsCommand.ExecuteScalarAsync(), CultureInfo.InvariantCulture) > 0)
                {
                    throw new InvalidOperationException($"Product ID {actualProductId} already exists in dbo.Products.");
                }
            }

            const string branchCompanySql = @"
                SELECT TOP (1) Coid
                FROM [dbo].[Branches]
                WHERE UPPER(LTRIM(RTRIM(Branch))) = UPPER(LTRIM(RTRIM(@branch)));";
            using var branchCompanyCommand = new SqlCommand(branchCompanySql, connection, transaction);
            branchCompanyCommand.Parameters.AddWithValue("@branch", branchName);
            var companyIdValue = await branchCompanyCommand.ExecuteScalarAsync();
            if (companyIdValue is null || companyIdValue == DBNull.Value)
            {
                throw new InvalidOperationException($"Branch {branchName} was not found in dbo.Branches.");
            }

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
                    0, 0, 'EA', '0', 1, '', @coid, NULL,
                    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
                );";
            using (var identityOnCommand = new SqlCommand("SET IDENTITY_INSERT [dbo].[Products] ON;", connection, transaction))
            {
                await identityOnCommand.ExecuteNonQueryAsync();
            }

            using (var productCommand = new SqlCommand(insertProductSql, connection, transaction))
            {
                productCommand.Parameters.AddWithValue("@productId", productCreation.product_id);
                productCommand.Parameters.AddWithValue("@productName", productCreation.product_name.Trim());
                productCommand.Parameters.AddWithValue("@productCode", (actualProductId + 1).ToString(CultureInfo.InvariantCulture));
                productCommand.Parameters.AddWithValue("@barcode", productCreation.barcode?.Trim() ?? string.Empty);
                productCommand.Parameters.AddWithValue("@sellingPrice", sellingPrice);
                productCommand.Parameters.AddWithValue("@doneBy", Environment.UserName);
                productCommand.Parameters.AddWithValue("@coid", Convert.ToInt32(companyIdValue, CultureInfo.InvariantCulture));
                actualProductId = Convert.ToInt32(await productCommand.ExecuteScalarAsync(), CultureInfo.InvariantCulture);
            }

            using (var identityOffCommand = new SqlCommand("SET IDENTITY_INSERT [dbo].[Products] OFF;", connection, transaction))
            {
                await identityOffCommand.ExecuteNonQueryAsync();
            }

            const string insertBalanceSql = @"
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
            balanceCommand.Parameters.AddWithValue("@coid", Convert.ToInt32(companyIdValue, CultureInfo.InvariantCulture));
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
                                    COALESCE(NULLIF(LTRIM(RTRIM(m.DoneBy)), ''), 'Unknown') AS Cashier,
                                    COALESCE(pm.PaymentMethodDesc, CONCAT('Method ', COALESCE(CAST(m.ReceiptDisplayPaymentMethod AS nvarchar(20)), '0'))) AS PaymentMethod,
                                    COALESCE(NULLIF(pm.Currency, ''), 'UNKNOWN') AS Currency,
                                    CAST((
                                        (
                                            (COALESCE(m.Quantity, 0) * COALESCE(m.SellingPrice, 0))
                                            - COALESCE(m.DiscountAmt, 0)
                                            - COALESCE(m.InvDiscount, 0)
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
                                Cashier,
                                PaymentMethod,
                                Currency,
                                Rate,
                                CAST(SUM(SaleTotal) AS decimal(28, 2)) AS Total,
                                CAST(SUM(TaxTotal) AS decimal(28, 2)) AS TaxTotal,
                                COUNT_BIG(*) AS ReceiptCount
                                FROM Sales
                                        GROUP BY Cashier, PaymentMethod, Currency, Rate
                                        ORDER BY Cashier, PaymentMethod, Currency, Rate;";

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
    }
}
