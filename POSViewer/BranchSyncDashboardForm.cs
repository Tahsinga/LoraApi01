using System.Data;
using System.Data.SqlClient;
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
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
            var branchName = await GetBranchNameAsync();

            try
            {
                var heartbeat = new
                {
                    branch = branchName,
                    device_role = _settings.DeviceRole
                };
                var heartbeatJson = JsonSerializer.Serialize(heartbeat);
                using var heartbeatContent = new StringContent(heartbeatJson, Encoding.UTF8, "application/json");
                await client.PostAsync($"{apiBaseUrl}/api/branches/", heartbeatContent);
            }
            catch (HttpRequestException)
            {
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
            
            if (pendingDeletions.Count > 0)
            {
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] RECEIVED {pendingDeletions.Count} cancellation command(s) from API for branch {branchName}.");
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

                _statusLabel.ForeColor = Color.DarkGreen;
                _statusLabel.Text = $"✓ Processed {pendingDeletions.Count} trigger(s) | {result.count} total pending";
                _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] === SYNC COMPLETE ===");
            }
            else
            {
                _statusLabel.ForeColor = Color.DarkGreen;
                _statusLabel.Text = $"No pending deletions | Branch: {branchName}";
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
            var stockLines = new List<(int ProductId, decimal Quantity)>();

            // Log what we're about to delete
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] DEBUG: DeleteAndConfirmAsync called");
            _syncQueueListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] DEBUG: Invoice={invoiceNum} | Product={productId} | Branch={branchName} | Entry={entryNo}");

            if (wholeInvoice)
            {
                const string stockLinesSql = @"
                    SELECT ProductID, Quantity
                    FROM [dbo].[Movement]
                    WHERE (InvoiceNum = @invoiceNum OR CAST(InvoiceNum AS nvarchar(50)) = @invoiceNum)
                      AND UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@branchName);";

                using var stockLinesCommand = new SqlCommand(stockLinesSql, connection);
                stockLinesCommand.Parameters.AddWithValue("@invoiceNum", invoiceNum);
                stockLinesCommand.Parameters.AddWithValue("@branchName", branchName);
                using (var stockReader = await stockLinesCommand.ExecuteReaderAsync())
                {
                    while (await stockReader.ReadAsync())
                    {
                        if (!stockReader.IsDBNull(0) && !stockReader.IsDBNull(1))
                        {
                            stockLines.Add((Convert.ToInt32(stockReader[0]), Math.Abs(Convert.ToDecimal(stockReader[1]))));
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

    private sealed class BranchSyncTriggerResponse
    {
        public string? status { get; set; }
        public string? service { get; set; }
        public string? branch_filter { get; set; }
        public List<DeletionTrigger>? pending_deletions { get; set; }
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
}
