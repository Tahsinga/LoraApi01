using System.Data.SqlClient;
using System.Globalization;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;

namespace POSViewer;

public sealed class MainSyncDashboardForm : Form
{
    private readonly ConnectionForm _connectionForm;
    private readonly ConnectionSettings _settings;
    private readonly ListBox _syncLogListBox = new();
    private readonly Label _statusLabel = new();
    private readonly Button _syncNowButton = new();
    private readonly Button _backButton = new();
    private readonly System.Windows.Forms.Timer _autoSyncTimer = new();
    private readonly SemaphoreSlim _syncGate = new(1, 1);
    private readonly Dictionary<string, string> _transferStates = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, string> _publishedCatalogStates = new(StringComparer.OrdinalIgnoreCase);

    public MainSyncDashboardForm(ConnectionSettings settings, ConnectionForm connectionForm)
    {
        _settings = settings;
        _connectionForm = connectionForm;

        Text = "Main Sync Dashboard";
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        Size = new Size(900, 600);
        BackColor = Color.FromArgb(245, 245, 245);

        var titleLabel = new Label
        {
            Text = "Main Sync Dashboard",
            Font = new Font("Segoe UI", 22F, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(20, 20),
            ForeColor = Color.FromArgb(30, 30, 30)
        };

        var infoLabel = new Label
        {
            Text = $"This PC is connected as: {_settings.DeviceRole} | Server: {_settings.Server} | Database: {_settings.Database}",
            Font = new Font("Segoe UI", 11F),
            AutoSize = true,
            Location = new Point(20, 68),
            ForeColor = Color.FromArgb(60, 60, 60)
        };

        _statusLabel.Text = "Waiting for Main sync...";
        _statusLabel.Location = new Point(20, 110);
        _statusLabel.AutoSize = true;
        _statusLabel.Font = new Font("Segoe UI", 10F, FontStyle.Bold);
        _statusLabel.ForeColor = Color.DarkGreen;

        _syncLogListBox.Location = new Point(20, 145);
        _syncLogListBox.Size = new Size(ClientSize.Width - 40, ClientSize.Height - 240);
        _syncLogListBox.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
        _syncLogListBox.Font = new Font("Consolas", 10F);

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
        Controls.Add(_syncLogListBox);
        Controls.Add(_syncNowButton);
        Controls.Add(_backButton);

        Resize += (_, _) =>
        {
            _syncLogListBox.Size = new Size(ClientSize.Width - 40, ClientSize.Height - 240);
            _syncNowButton.Location = new Point(20, ClientSize.Height - 60);
            _backButton.Location = new Point(160, ClientSize.Height - 60);
        };
        FormClosing += (_, _) => _autoSyncTimer.Stop();

        AddLog("[MAIN] Main PC ready to receive products from branches.");
        _autoSyncTimer.Interval = 5000;
        _autoSyncTimer.Tick += async (_, _) => await SyncNowAsync();
        _autoSyncTimer.Start();
        _ = SyncNowAsync();
    }

    private void AddLog(string message)
    {
        _syncLogListBox.Items.Insert(0, $"[{DateTime.Now:HH:mm:ss}] {message}");
    }

    private async Task SyncNowAsync()
    {
        if (!await _syncGate.WaitAsync(0))
        {
            return;
        }

        try
        {
            _statusLabel.Text = "Main PC receiving branch products...";
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(60) };
            await PollStockTransfersAsync(client);
            await StoreStockMovementsAsync(client);
            var response = await client.GetAsync($"{_settings.GetApiBaseUrl()}/api/products/inbox/");
            if (!response.IsSuccessStatusCode)
            {
                _statusLabel.ForeColor = Color.DarkRed;
                _statusLabel.Text = $"Receive failed: {response.StatusCode}";
                AddLog($"[ERROR] Branch product inbox returned {response.StatusCode}.");
                return;
            }

            var payload = await response.Content.ReadFromJsonAsync<ProductInboxResponse>();
            var products = payload?.products ?? new List<ProductCatalogItem>();
            await StoreBranchProductsAsync(products);
            AddLog($"[RECEIVED] {products.Count} branch snapshot product(s) stored in CloudPOS.dbo.BranchProductCatalog.");

            var published = await PublishMainCatalogAsync(client);
            _statusLabel.ForeColor = published ? Color.DarkGreen : Color.DarkRed;
            _statusLabel.Text = published ? $"Main published {products.Count} branch product(s) to web." : "Main web publish failed.";
            AddLog(published ? "[PUBLISHED] Main database snapshot changes sent to the web API." : "[ERROR] Main database snapshot changes were not published.");
        }
        catch (Exception ex)
        {
            _statusLabel.ForeColor = Color.DarkRed;
            _statusLabel.Text = "Main sync failed.";
            AddLog($"[ERROR] {ex.Message}");
        }
        finally
        {
            _syncGate.Release();
        }
    }

    private async Task StoreStockMovementsAsync(HttpClient client)
    {
        var response = await client.GetAsync($"{_settings.GetApiBaseUrl()}/api/stock/movements/device-log/");
        if (!response.IsSuccessStatusCode)
        {
            AddLog($"[ERROR] Stock movement sync returned {response.StatusCode}.");
            return;
        }

        var payload = await response.Content.ReadFromJsonAsync<StockMovementLogResponse>();
        var movements = payload?.movements ?? new List<StockMovementLog>();
        if (movements.Count == 0)
        {
            return;
        }

        using var connection = new SqlConnection(_settings.BuildConnectionString());
        await connection.OpenAsync();
        const string createTableSql = @"
            IF OBJECT_ID(N'dbo.BranchStockMovements', N'U') IS NULL
            BEGIN
                CREATE TABLE [dbo].[BranchStockMovements](
                    MovementID bigint NOT NULL,
                    Branch nvarchar(255) NOT NULL,
                    ProductID int NOT NULL,
                    ProductName nvarchar(255) NOT NULL,
                    MovementType nvarchar(20) NOT NULL,
                    Quantity decimal(18,3) NOT NULL,
                    Source nvarchar(255) NULL,
                    CreatedAt datetime2 NOT NULL,
                    CONSTRAINT PK_BranchStockMovements PRIMARY KEY (MovementID)
                );
                CREATE INDEX IX_BranchStockMovements_ProductDate
                    ON [dbo].[BranchStockMovements](Branch, ProductID, CreatedAt);
            END";
        using (var createCommand = new SqlCommand(createTableSql, connection))
        {
            await createCommand.ExecuteNonQueryAsync();
        }

        const string insertSql = @"
            IF NOT EXISTS (SELECT 1 FROM [dbo].[BranchStockMovements] WHERE MovementID = @movementId)
            INSERT INTO [dbo].[BranchStockMovements]
                (MovementID, Branch, ProductID, ProductName, MovementType, Quantity, Source, CreatedAt)
            VALUES
                (@movementId, @branch, @productId, @productName, @movementType, @quantity, @source, @createdAt);";
        var stored = 0;
        foreach (var movement in movements)
        {
            if (movement.id <= 0 || string.IsNullOrWhiteSpace(movement.created_at))
            {
                continue;
            }

            using var command = new SqlCommand(insertSql, connection);
            command.Parameters.AddWithValue("@movementId", movement.id);
            command.Parameters.AddWithValue("@branch", movement.branch ?? string.Empty);
            command.Parameters.AddWithValue("@productId", movement.product_id);
            command.Parameters.AddWithValue("@productName", movement.product_name ?? string.Empty);
            command.Parameters.AddWithValue("@movementType", movement.movement_type ?? string.Empty);
            command.Parameters.AddWithValue("@quantity", movement.quantity);
            command.Parameters.AddWithValue("@source", movement.source ?? string.Empty);
            command.Parameters.AddWithValue("@createdAt", DateTimeOffset.Parse(movement.created_at).UtcDateTime);
            stored += await command.ExecuteNonQueryAsync();
        }

        AddLog($"[MOVEMENTS] Stored {stored} new movement record(s) in CloudPOS.dbo.BranchStockMovements.");
    }

    private async Task PollStockTransfersAsync(HttpClient client)
    {
        var response = await client.GetAsync($"{_settings.GetApiBaseUrl()}/api/stock/transfers/device-log/");
        if (!response.IsSuccessStatusCode)
        {
            AddLog($"[ERROR] Stock transfer status returned {response.StatusCode}.");
            return;
        }

        var payload = await response.Content.ReadFromJsonAsync<StockTransferLogResponse>();
        var transfers = payload?.transfers ?? new List<StockTransferLog>();
            AddLog($"[CONNECTION] Web transfer channel reachable. Tracking {transfers.Count} web transfer(s).");

        foreach (var transfer in transfers)
        {
            var state = transfer.status ?? "unknown";
            if (_transferStates.TryGetValue(transfer.id ?? string.Empty, out var previousState) && previousState == state)
            {
                continue;
            }

            _transferStates[transfer.id ?? string.Empty] = state;
            AddLog($"[WEB TRANSFER SENT] {state.ToUpperInvariant()}: {transfer.id} | {transfer.product_name} | Qty {transfer.quantity} | Branch {transfer.branch}");
        }
    }

    private async Task StoreBranchProductsAsync(List<ProductCatalogItem> products)
    {
        using var connection = new SqlConnection(_settings.BuildConnectionString());
        await connection.OpenAsync();
        const string createTableSql = @"
            IF OBJECT_ID(N'dbo.BranchProductCatalog', N'U') IS NULL
            BEGIN
                CREATE TABLE [dbo].[BranchProductCatalog](
                    Branch nvarchar(255) NOT NULL,
                    ProductID int NOT NULL,
                    ProductDesc nvarchar(250) NOT NULL,
                    ProductCode nvarchar(50) NULL,
                    BarCode nvarchar(100) NULL,
                    SellingPrice decimal(18,2) NOT NULL CONSTRAINT DF_BranchProductCatalog_SellingPrice DEFAULT 0,
                        AvailableQuantity decimal(18,3) NOT NULL CONSTRAINT DF_BranchProductCatalog_AvailableQuantity DEFAULT 0,
                    UpdatedAt datetime2 NOT NULL CONSTRAINT DF_BranchProductCatalog_UpdatedAt DEFAULT SYSUTCDATETIME(),
                    CONSTRAINT PK_BranchProductCatalog PRIMARY KEY (Branch, ProductID)
                );
            END";
        using (var createCommand = new SqlCommand(createTableSql, connection))
        {
            await createCommand.ExecuteNonQueryAsync();
        }
        using (var alterCommand = new SqlCommand(@"
            IF COL_LENGTH('dbo.BranchProductCatalog', 'AvailableQuantity') IS NULL
                ALTER TABLE [dbo].[BranchProductCatalog] ADD AvailableQuantity decimal(18,3) NOT NULL CONSTRAINT DF_BranchProductCatalog_AvailableQuantity_Existing DEFAULT 0;
            IF COL_LENGTH('dbo.BranchProductCatalog', 'SellingPrice') IS NULL
                ALTER TABLE [dbo].[BranchProductCatalog] ADD SellingPrice decimal(18,2) NOT NULL CONSTRAINT DF_BranchProductCatalog_SellingPrice_Existing DEFAULT 0;", connection))
        {
            await alterCommand.ExecuteNonQueryAsync();
        }

        foreach (var product in products)
        {
            const string upsertSql = @"
                UPDATE [dbo].[BranchProductCatalog]
                SET ProductDesc = @productName, ProductCode = @productCode, BarCode = @barcode, SellingPrice = @sellingPrice, AvailableQuantity = @availableQuantity, UpdatedAt = SYSUTCDATETIME()
                WHERE Branch = @branch AND ProductID = @productId;
                IF @@ROWCOUNT = 0
                INSERT INTO [dbo].[BranchProductCatalog](Branch, ProductID, ProductDesc, ProductCode, BarCode, SellingPrice, AvailableQuantity)
                VALUES (@branch, @productId, @productName, @productCode, @barcode, @sellingPrice, @availableQuantity);";
            using var command = new SqlCommand(upsertSql, connection);
            command.Parameters.AddWithValue("@branch", product.branch ?? string.Empty);
            command.Parameters.AddWithValue("@productId", product.product_id);
            command.Parameters.AddWithValue("@productName", product.product_name ?? string.Empty);
            command.Parameters.AddWithValue("@productCode", product.product_code ?? string.Empty);
            command.Parameters.AddWithValue("@barcode", product.barcode ?? string.Empty);
            command.Parameters.AddWithValue("@sellingPrice", product.selling_price);
            command.Parameters.AddWithValue("@availableQuantity", product.available_quantity);
            await command.ExecuteNonQueryAsync();
        }
    }

    private async Task<bool> PublishMainCatalogAsync(HttpClient client)
    {
        using var connection = new SqlConnection(_settings.BuildConnectionString());
        await connection.OpenAsync();
        var query = @"
                        SELECT Branch, ProductID, ProductDesc, ProductCode, BarCode, SellingPrice, AvailableQuantity
                        FROM [dbo].[BranchProductCatalog]
                        WHERE ProductID IS NOT NULL
                            AND ProductDesc IS NOT NULL
                            AND LTRIM(RTRIM(ProductDesc)) <> '';";
                using var command = new SqlCommand(query, connection);
        using var reader = await command.ExecuteReaderAsync();
        var products = new List<object>();
        var productStates = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        while (await reader.ReadAsync())
        {
            var branch = reader["Branch"]?.ToString()?.Trim() ?? string.Empty;
            var productId = Convert.ToInt32(reader["ProductID"]);
            var productName = reader["ProductDesc"]?.ToString()?.Trim() ?? string.Empty;
            var productCode = reader["ProductCode"]?.ToString()?.Trim() ?? string.Empty;
            var barcode = reader["BarCode"]?.ToString()?.Trim() ?? string.Empty;
            var sellingPrice = Convert.ToDecimal(reader["SellingPrice"], CultureInfo.InvariantCulture);
            var availableQuantity = Convert.ToDecimal(reader["AvailableQuantity"], CultureInfo.InvariantCulture);
            var stateKey = $"{branch}\u001e{productId}";
            var state = $"{productName}\u001f{productCode}\u001f{barcode}\u001f{sellingPrice}\u001f{availableQuantity}";
            productStates[stateKey] = state;
            if (_publishedCatalogStates.TryGetValue(stateKey, out var previousState) && previousState == state)
            {
                continue;
            }

            products.Add(new
            {
                product_id = productId,
                branch,
                product_name = productName,
                product_code = productCode,
                barcode
                , selling_price = sellingPrice
                , available_quantity = availableQuantity
            });
        }

        if (products.Count == 0)
        {
            _publishedCatalogStates.Clear();
            foreach (var item in productStates)
            {
                _publishedCatalogStates[item.Key] = item.Value;
            }
            return true;
        }

        var json = JsonSerializer.Serialize(new { products });
        using var content = new StringContent(json, Encoding.UTF8, "application/json");
        var response = await client.PostAsync($"{_settings.GetApiBaseUrl()}/api/products/publish/", content);
        var responseBody = await response.Content.ReadAsStringAsync();
        if (!response.IsSuccessStatusCode)
        {
            AddLog($"[ERROR] Main catalog publish returned {(int)response.StatusCode} {response.ReasonPhrase}: {responseBody}");
            return false;
        }

        _publishedCatalogStates.Clear();
        foreach (var item in productStates)
        {
            _publishedCatalogStates[item.Key] = item.Value;
        }
        AddLog($"[PUBLISHED] Sent {products.Count} changed catalog product(s); API response: {responseBody}");
        return true;
    }

    private static async Task<string> ResolveMainSellingPriceExpressionAsync(SqlConnection connection)
    {
        const string columnsSql = @"
            SELECT TABLE_NAME, COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'dbo'
              AND TABLE_NAME IN ('Products', 'Movement')
              AND COLUMN_NAME IN ('SellingPrice', 'SalePrice', 'RetailPrice', 'UnitPrice', 'Price');";
        using var columnsCommand = new SqlCommand(columnsSql, connection);
        using var reader = await columnsCommand.ExecuteReaderAsync();
        var productColumns = new List<string>();
        var movementColumns = new List<string>();
        while (await reader.ReadAsync())
        {
            var table = reader["TABLE_NAME"]?.ToString();
            var column = reader["COLUMN_NAME"]?.ToString();
            if (string.Equals(table, "Products", StringComparison.OrdinalIgnoreCase) && column is not null)
            {
                productColumns.Add(column);
            }
            else if (string.Equals(table, "Movement", StringComparison.OrdinalIgnoreCase) && column is not null)
            {
                movementColumns.Add(column);
            }
        }

        var expressions = productColumns
            .Select(column => $"NULLIF(CONVERT(decimal(18,2), p.[{column}]), 0)")
            .ToList();
        expressions.AddRange(movementColumns.Select(column =>
            $"NULLIF((SELECT TOP 1 CONVERT(decimal(18,2), m.[{column}]) FROM [dbo].[Movement] m WHERE m.ProductID = p.ProductID AND m.[{column}] IS NOT NULL AND m.[{column}] <> 0 ORDER BY m.TranDate DESC, m.EntryNo DESC), 0)"));

        return expressions.Count == 0 ? "CONVERT(decimal(18,2), 0)" : $"COALESCE({string.Join(", ", expressions)}, 0)";
    }

    private sealed class ProductInboxResponse
    {
        public List<ProductCatalogItem>? products { get; set; }
    }

    private sealed class ProductCatalogItem
    {
        public string? branch { get; set; }
        public int product_id { get; set; }
        public string? product_name { get; set; }
        public string? product_code { get; set; }
        public string? barcode { get; set; }
        public decimal selling_price { get; set; }
        public decimal available_quantity { get; set; }
    }

    private sealed class StockTransferLogResponse
    {
        public List<StockTransferLog>? transfers { get; set; }
    }

    private sealed class StockMovementLogResponse
    {
        public List<StockMovementLog>? movements { get; set; }
    }

    private sealed class StockMovementLog
    {
        public long id { get; set; }
        public string? branch { get; set; }
        public int product_id { get; set; }
        public string? product_name { get; set; }
        public string? movement_type { get; set; }
        public decimal quantity { get; set; }
        public string? source { get; set; }
        public string? created_at { get; set; }
    }

    private sealed class StockTransferLog
    {
        public string? id { get; set; }
        public string? branch { get; set; }
        public string? product_name { get; set; }
        public string? quantity { get; set; }
        public string? status { get; set; }
    }
}