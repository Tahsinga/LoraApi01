using System.Data;
using System.Data.SqlClient;
using System.Drawing;
using System.Globalization;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;

namespace POSViewer;

public sealed class DashboardForm : Form
{
    private readonly ConnectionForm _connectionForm;
    private readonly ConnectionSettings _settings;
    private readonly DataGridView _gridView = new();
    private readonly TextBox _searchTextBox = new();
    private readonly ComboBox _branchComboBox = new();
    private readonly Button _refreshButton = new();
    private readonly Button _returnButton = new();
    private readonly System.Windows.Forms.Timer _refreshTimer = new();
    private readonly RichTextBox _logTextBox = new();
    private readonly DateTimePicker _dateFilterPicker = new();
    private readonly Label _statusLabel = new();

    public DashboardForm(ConnectionSettings settings, ConnectionForm connectionForm)
    {
        _settings = settings;
        _connectionForm = connectionForm;

        Text = "Main PC Dashboard";
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        Size = new Size(1000, 650);
        BackColor = Color.FromArgb(245, 245, 245);

        var topPanel = new Panel
        {
            Dock = DockStyle.Top,
            Height = 160,
            BackColor = Color.FromArgb(236, 236, 236),
            BorderStyle = BorderStyle.FixedSingle
        };

        var titleLabel = new Label
        {
            Text = "Main PC Dashboard",
            Font = new Font("Segoe UI", 22F, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(20, 18),
            ForeColor = Color.FromArgb(30, 30, 30)
        };

        var statusLabel = new Label
        {
            Text = $"Main PC | Connected to: {_settings.Server} / {_settings.Database}",
            Font = new Font("Segoe UI", 10.5F),
            AutoSize = true,
            Location = new Point(22, 62),
            ForeColor = Color.FromArgb(70, 70, 70)
        };

        _statusLabel.Text = "Main PC sync complete.";
        _statusLabel.Font = new Font("Segoe UI", 9.5F, FontStyle.Bold);
        _statusLabel.AutoSize = true;
        _statusLabel.Location = new Point(515, 94);
        _statusLabel.ForeColor = Color.DarkGreen;

        var dateLabel = new Label
        {
            Text = "Filter Date:",
            Font = new Font("Segoe UI", 10.5F),
            AutoSize = true,
            Location = new Point(22, 92)
        };

        _dateFilterPicker.Location = new Point(130, 87);
        _dateFilterPicker.Width = 150;
        _dateFilterPicker.Height = 28;
        _dateFilterPicker.Font = new Font("Segoe UI", 10F);
        _dateFilterPicker.Format = DateTimePickerFormat.Short;
        _dateFilterPicker.Value = DateTime.Now;
        _dateFilterPicker.ValueChanged += async (_, _) => await LoadMovementsAsync();

        var searchLabel = new Label
        {
            Text = "Invoice Number:",
            Font = new Font("Segoe UI", 10.5F),
            AutoSize = true,
            Location = new Point(22, 122)
        };

        _searchTextBox.Location = new Point(178, 117);
        _searchTextBox.Width = 220;
        _searchTextBox.Height = 28;
        _searchTextBox.Font = new Font("Segoe UI", 10F);
        _searchTextBox.BorderStyle = BorderStyle.FixedSingle;
        _searchTextBox.KeyDown += (_, e) =>
        {
            if (e.KeyCode == Keys.Enter)
            {
                _ = LoadMovementsAsync();
            }
        };

        var branchLabel = new Label
        {
            Text = "Branch:",
            Font = new Font("Segoe UI", 10.5F),
            AutoSize = true,
            Location = new Point(470, 122)
        };

        _branchComboBox.DropDownStyle = ComboBoxStyle.DropDownList;
        _branchComboBox.Location = new Point(548, 117);
        _branchComboBox.Width = 175;
        _branchComboBox.Height = 28;
        _branchComboBox.Font = new Font("Segoe UI", 10F);
        _branchComboBox.SelectedIndexChanged += async (_, _) => await LoadMovementsAsync();

        _refreshButton.Text = "Refresh";
        _refreshButton.Location = new Point(760, 116);
        _refreshButton.Width = 110;
        _refreshButton.Height = 30;
        _refreshButton.FlatStyle = FlatStyle.Flat;
        _refreshButton.BackColor = Color.FromArgb(240, 240, 240);
        _refreshButton.ForeColor = Color.FromArgb(30, 30, 30);
        _refreshButton.Font = new Font("Segoe UI", 10F, FontStyle.Bold);
        _refreshButton.Click += async (_, _) => await LoadMovementsAsync();

        _returnButton.Text = "Cancel";
        _returnButton.Location = new Point(880, 116);
        _returnButton.Width = 110;
        _returnButton.Height = 30;
        _returnButton.FlatStyle = FlatStyle.Flat;
        _returnButton.BackColor = Color.FromArgb(22, 88, 50);
        _returnButton.ForeColor = Color.White;
        _returnButton.Font = new Font("Segoe UI", 10F, FontStyle.Bold);
        _returnButton.Click += async (_, _) => await TryCreateReturnAsync();

        topPanel.Controls.Add(titleLabel);
        topPanel.Controls.Add(statusLabel);
        topPanel.Controls.Add(_statusLabel);
        topPanel.Controls.Add(dateLabel);
        topPanel.Controls.Add(_dateFilterPicker);
        topPanel.Controls.Add(searchLabel);
        topPanel.Controls.Add(_searchTextBox);
        topPanel.Controls.Add(branchLabel);
        topPanel.Controls.Add(_branchComboBox);
        topPanel.Controls.Add(_refreshButton);
        topPanel.Controls.Add(_returnButton);

        Controls.Add(topPanel);

        _gridView.Location = new Point(20, 170);
        _gridView.Size = new Size(ClientSize.Width - 40, ClientSize.Height - 270);
        _gridView.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
        _gridView.ReadOnly = true;
        _gridView.AllowUserToAddRows = false;
        _gridView.AllowUserToDeleteRows = false;
        _gridView.AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.None;
        _gridView.AutoGenerateColumns = true;
        _gridView.RowHeadersVisible = false;
        _gridView.SelectionMode = DataGridViewSelectionMode.FullRowSelect;
        _gridView.MultiSelect = false;
        _gridView.AutoSizeRowsMode = DataGridViewAutoSizeRowsMode.None;
        _gridView.ColumnHeadersHeightSizeMode = DataGridViewColumnHeadersHeightSizeMode.EnableResizing;
        _gridView.ColumnHeadersHeight = 30;
        _gridView.RowTemplate.Height = 26;
        _gridView.ScrollBars = ScrollBars.Both;
        _gridView.AllowUserToOrderColumns = true;

        var logLabel = new Label
        {
            Text = "Logs",
            Font = new Font("Segoe UI", 11F, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(20, ClientSize.Height - 92)
        };

        _logTextBox.Location = new Point(20, ClientSize.Height - 220);
        _logTextBox.Size = new Size(ClientSize.Width - 40, 170);
        _logTextBox.Anchor = AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
        _logTextBox.ReadOnly = true;
        _logTextBox.Multiline = true;
        _logTextBox.HideSelection = false;
        _logTextBox.ShortcutsEnabled = true;
        _logTextBox.BackColor = Color.FromArgb(30, 30, 30);
        _logTextBox.ForeColor = Color.White;
        _logTextBox.Font = new Font("Consolas", 9F);
        _logTextBox.ScrollBars = RichTextBoxScrollBars.Vertical;

        _refreshTimer.Interval = 15000;
        _refreshTimer.Tick += async (_, _) =>
        {
            _statusLabel.Text = "Main PC syncing...";
            await ImportBranchProductCatalogAsync();
            await SyncProductCatalogAsync();
            await LoadMovementsAsync();
            _statusLabel.Text = "Main PC sync complete.";
        };
        _refreshTimer.Start();

        Controls.Add(_gridView);
        Controls.Add(logLabel);
        Controls.Add(_logTextBox);

        FormClosing += (_, _) => _refreshTimer.Stop();

        Shown += async (_, _) =>
        {
            await LoadBranchListAsync();
            await ImportBranchProductCatalogAsync();
            await SyncProductCatalogAsync();
            await LoadMovementsAsync();
        };
        Resize += (_, _) =>
        {
            _gridView.Location = new Point(20, 170);
            _gridView.Height = ClientSize.Height - 430;
            _logTextBox.Top = ClientSize.Height - 220;
            _logTextBox.Height = 170;
        };

        AddLog("[INFO] Main PC connected successfully.", Color.LightBlue);
    }

    private async Task ImportBranchProductCatalogAsync()
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(60) };
            var response = await client.GetAsync($"{_settings.GetApiBaseUrl()}/api/products/inbox/");
            if (!response.IsSuccessStatusCode)
            {
                AddLog($"[WARNING] Branch product inbox request failed: {response.StatusCode}", Color.Orange);
                return;
            }

            var payload = await response.Content.ReadFromJsonAsync<ProductInboxResponse>();
            var products = payload?.products ?? new List<ProductCatalogItem>();
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
                        UpdatedAt datetime2 NOT NULL CONSTRAINT DF_BranchProductCatalog_UpdatedAt DEFAULT SYSUTCDATETIME(),
                        CONSTRAINT PK_BranchProductCatalog PRIMARY KEY (Branch, ProductID)
                    );
                END";
            using (var createCommand = new SqlCommand(createTableSql, connection))
            {
                await createCommand.ExecuteNonQueryAsync();
            }
            using (var alterCommand = new SqlCommand(@"
                IF COL_LENGTH('dbo.BranchProductCatalog', 'SellingPrice') IS NULL
                    ALTER TABLE [dbo].[BranchProductCatalog] ADD SellingPrice decimal(18,2) NOT NULL CONSTRAINT DF_BranchProductCatalog_SellingPrice_Existing DEFAULT 0;", connection))
            {
                await alterCommand.ExecuteNonQueryAsync();
            }

            foreach (var product in products)
            {
                const string upsertSql = @"
                    UPDATE [dbo].[BranchProductCatalog]
                    SET ProductDesc = @productName, ProductCode = @productCode, BarCode = @barcode, SellingPrice = @sellingPrice, UpdatedAt = SYSUTCDATETIME()
                    WHERE Branch = @branch AND ProductID = @productId;
                    IF @@ROWCOUNT = 0
                    INSERT INTO [dbo].[BranchProductCatalog](Branch, ProductID, ProductDesc, ProductCode, BarCode, SellingPrice)
                    VALUES (@branch, @productId, @productName, @productCode, @barcode, @sellingPrice);";
                using var command = new SqlCommand(upsertSql, connection);
                command.Parameters.AddWithValue("@branch", product.branch ?? string.Empty);
                command.Parameters.AddWithValue("@productId", product.product_id);
                command.Parameters.AddWithValue("@productName", product.product_name ?? string.Empty);
                command.Parameters.AddWithValue("@productCode", product.product_code ?? string.Empty);
                command.Parameters.AddWithValue("@barcode", product.barcode ?? string.Empty);
                command.Parameters.AddWithValue("@sellingPrice", product.selling_price);
                await command.ExecuteNonQueryAsync();
            }

            AddLog($"[SUCCESS] Main received {products.Count} product(s) from branch inbox and stored them in dbo.BranchProductCatalog.", Color.LightGreen);
        }
        catch (Exception ex)
        {
            AddLog($"[WARNING] Branch product import failed: {ex.Message}", Color.Orange);
        }
    }

    private void AddLog(string message, Color color)
    {
        if (IsDisposed || Disposing || _logTextBox.IsDisposed || _logTextBox.Disposing)
        {
            return;
        }

        var originalSelection = _logTextBox.SelectionStart;
        _logTextBox.SelectionColor = color;
        _logTextBox.AppendText($"[{DateTime.Now:HH:mm:ss}] {message}{Environment.NewLine}");
        _logTextBox.SelectionStart = _logTextBox.TextLength;
        _logTextBox.ScrollToCaret();
        _logTextBox.SelectionStart = originalSelection;
    }

    private async Task LoadBranchListAsync()
    {
        try
        {
            _branchComboBox.Items.Clear();

            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();

            var query = @"
                SELECT CAST(Branch AS nvarchar(100)) AS BranchName
                FROM [dbo].[Branches]
                WHERE Branch IS NOT NULL AND LTRIM(RTRIM(CAST(Branch AS nvarchar(100)))) <> ''
                ORDER BY BranchName;";

            using var command = new SqlCommand(query, connection);
            using var reader = await command.ExecuteReaderAsync();

            while (await reader.ReadAsync())
            {
                var branch = reader["BranchName"]?.ToString();
                if (!string.IsNullOrWhiteSpace(branch))
                {
                    _branchComboBox.Items.Add(branch);
                }
            }

            if (_branchComboBox.Items.Count > 0)
            {
                _branchComboBox.SelectedIndex = 0;
            }
        }
        catch (Exception ex)
        {
            AddLog($"[ERROR] Unable to load branch list: {ex.Message}", Color.IndianRed);
        }
    }

    private async Task SyncProductCatalogAsync()
    {
        _statusLabel.Text = "Syncing main product catalog to web...";
        try
        {
            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();
                        const string query = @"
                                SELECT ProductID, ProductDesc, ProductCode, BarCode, SellingPrice
                                FROM [dbo].[BranchProductCatalog]
                                WHERE ProductID IS NOT NULL
                                    AND ProductDesc IS NOT NULL
                                    AND LTRIM(RTRIM(ProductDesc)) <> '';";
            using var command = new SqlCommand(query, connection);
            using var reader = await command.ExecuteReaderAsync();
            var products = new List<object>();
            while (await reader.ReadAsync())
            {
                products.Add(new
                {
                    product_id = Convert.ToInt32(reader["ProductID"]),
                    product_name = reader["ProductDesc"]?.ToString()?.Trim() ?? string.Empty,
                    product_code = reader["ProductCode"]?.ToString()?.Trim() ?? string.Empty,
                    barcode = reader["BarCode"]?.ToString()?.Trim() ?? string.Empty,
                    selling_price = Convert.ToDecimal(reader["SellingPrice"], CultureInfo.InvariantCulture),
                });
            }

            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(60) };
            var payload = JsonSerializer.Serialize(new { products });
            using var content = new StringContent(payload, Encoding.UTF8, "application/json");
            var response = await client.PostAsync($"{_settings.GetApiBaseUrl()}/api/products/publish/", content);
            AddLog(response.IsSuccessStatusCode
                ? $"[SUCCESS] Synced {products.Count} products to the web catalog."
                : $"[WARNING] Product catalog sync failed: {response.StatusCode}", response.IsSuccessStatusCode ? Color.LightGreen : Color.Orange);
            _statusLabel.Text = response.IsSuccessStatusCode
                ? $"Main PC catalog synced: {products.Count} products."
                : "Main PC catalog sync failed.";
        }
        catch (Exception ex)
        {
            AddLog($"[WARNING] Product catalog sync unavailable: {ex.Message}", Color.Orange);
            _statusLabel.Text = "Main PC catalog sync unavailable.";
        }
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
    }

    private async Task TryCreateReturnAsync()
    {
        if (_gridView.CurrentRow == null)
        {
            AddLog("[WARNING] Search by invoice number first, then select the invoice row before creating a return credit.", Color.Orange);
            return;
        }

        var row = _gridView.CurrentRow;
        var invoiceNum = GetCellValue(row, "InvoiceNum", "InvoiceNo", "Invoice");
        if (string.IsNullOrWhiteSpace(invoiceNum))
        {
            AddLog("[WARNING] Search by invoice number first and select the invoice row before creating a return credit.", Color.Orange);
            return;
        }

        var productIdText = GetCellValue(row, "ProductID", "ItemCode", "ProductCode", "Code", "SKU");
        var branch = GetCellValue(row, "Branch", "branch", "BranchCode", "Location");
        var qtyValue = GetCellNumericValue(row, "Qty", "Quantity", "QTY", "ItemQty", "QtySold");

        if (string.IsNullOrWhiteSpace(productIdText) || string.IsNullOrWhiteSpace(branch) || qtyValue == 0)
        {
            AddLog("[WARNING] The selected invoice row is missing the product, branch, or quantity needed for a return credit.", Color.Orange);
            return;
        }

        var productId = int.TryParse(productIdText, out var parsedProductId) ? parsedProductId : 0;
        var coid = GetCellNumericValue(row, "coid", "CoId");
        var returnQty = Math.Abs(qtyValue);
        var entryNo = GetCellValue(row, "EntryNo", "Entry");
        var returnReference = string.IsNullOrWhiteSpace(invoiceNum) ? $"RET-{DateTime.Now:yyyyMMddHHmmss}" : $"{invoiceNum}-RETURN";

        try
        {
            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();

            var stockSql = @"
                UPDATE [dbo].[ProductStockBalances]
                SET StockBal = StockBal + @returnQty
                WHERE branch = @branch
                  AND ProductID = @productId";

            if (coid > 0)
            {
                stockSql += " AND coid = @coid";
            }

            using (var stockCommand = new SqlCommand(stockSql, connection))
            {
                stockCommand.Parameters.AddWithValue("@returnQty", returnQty);
                stockCommand.Parameters.AddWithValue("@branch", branch);
                stockCommand.Parameters.AddWithValue("@productId", productId);
                if (coid > 0)
                {
                    stockCommand.Parameters.AddWithValue("@coid", coid);
                }

                var affected = await stockCommand.ExecuteNonQueryAsync();
                if (affected == 0)
                {
                    AddLog($"[WARNING] No stock balance row was updated for ProductID {productId} in branch {branch}.", Color.Orange);
                    return;
                }
            }

            // Delete the movement record - use multiple strategies to ensure it's deleted
            int deletedRows = 0;
            
            // Strategy 1: Try with all 4 fields if EntryNo is available
            if (!string.IsNullOrWhiteSpace(entryNo) && int.TryParse(entryNo, out var entryValue) && entryValue > 0)
            {
                var deleteSql = @"
                    DELETE FROM [dbo].[Movement]
                    WHERE (CAST(InvoiceNum AS nvarchar(50)) = @invoiceNum OR InvoiceNum = @invoiceNumInt)
                      AND ProductID = @productId
                      AND Branch = @branch
                      AND (CAST(EntryNo AS nvarchar(50)) = @entryNo OR EntryNo = @entryNoInt);";

                using (var deleteCommand = new SqlCommand(deleteSql, connection))
                {
                    deleteCommand.Parameters.AddWithValue("@invoiceNum", invoiceNum);
                    deleteCommand.Parameters.AddWithValue("@invoiceNumInt", int.TryParse(invoiceNum, out var iv) ? iv : 0);
                    deleteCommand.Parameters.AddWithValue("@productId", productId);
                    deleteCommand.Parameters.AddWithValue("@branch", branch);
                    deleteCommand.Parameters.AddWithValue("@entryNo", entryNo);
                    deleteCommand.Parameters.AddWithValue("@entryNoInt", entryValue);

                    deletedRows = await deleteCommand.ExecuteNonQueryAsync();
                    AddLog($"[DEBUG] Strategy 1 (4-field match): deleted {deletedRows} row(s)", Color.Gray);
                }
            }

            // Strategy 2: If nothing deleted, try 3-field match (without EntryNo)
            if (deletedRows == 0)
            {
                var deleteSql = @"
                    DELETE FROM [dbo].[Movement]
                    WHERE (CAST(InvoiceNum AS nvarchar(50)) = @invoiceNum OR InvoiceNum = @invoiceNumInt)
                      AND ProductID = @productId
                      AND Branch = @branch;";

                using (var deleteCommand = new SqlCommand(deleteSql, connection))
                {
                    deleteCommand.Parameters.AddWithValue("@invoiceNum", invoiceNum);
                    deleteCommand.Parameters.AddWithValue("@invoiceNumInt", int.TryParse(invoiceNum, out var iv) ? iv : 0);
                    deleteCommand.Parameters.AddWithValue("@productId", productId);
                    deleteCommand.Parameters.AddWithValue("@branch", branch);

                    deletedRows = await deleteCommand.ExecuteNonQueryAsync();
                    AddLog($"[DEBUG] Strategy 2 (3-field match): deleted {deletedRows} row(s)", Color.Gray);
                }
            }

            // Strategy 3: If still nothing, try case-insensitive branch match
            if (deletedRows == 0)
            {
                var deleteSql = @"
                    DELETE FROM [dbo].[Movement]
                    WHERE (CAST(InvoiceNum AS nvarchar(50)) = @invoiceNum OR InvoiceNum = @invoiceNumInt)
                      AND ProductID = @productId
                      AND UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@branch);";

                using (var deleteCommand = new SqlCommand(deleteSql, connection))
                {
                    deleteCommand.Parameters.AddWithValue("@invoiceNum", invoiceNum);
                    deleteCommand.Parameters.AddWithValue("@invoiceNumInt", int.TryParse(invoiceNum, out var iv) ? iv : 0);
                    deleteCommand.Parameters.AddWithValue("@productId", productId);
                    deleteCommand.Parameters.AddWithValue("@branch", branch);

                    deletedRows = await deleteCommand.ExecuteNonQueryAsync();
                    AddLog($"[DEBUG] Strategy 3 (case-insensitive): deleted {deletedRows} row(s)", Color.Gray);
                }
            }

            if (deletedRows == 0)
            {
                AddLog($"[WARNING] Stock was restored for ProductID {productId}, but no Movement transaction matched the criteria [Invoice={invoiceNum}, Product={productId}, Branch={branch}, Entry={entryNo}]. It may already be deleted or the data format doesn't match.", Color.Orange);
                await LoadMovementsAsync();
                return;
            }

            await NotifyGatewayInvoiceDeletedAsync(invoiceNum, branch, productId, entryNo);

            var printed = ReceiptPrinter.TryPrint(
                _settings.PrinterName,
                "CANCELLATION CONFIRMATION",
                new List<(string Label, string Value)>
                {
                    ("Status", "CANCELLED"),
                    ("Invoice", invoiceNum),
                    ("Branch", branch),
                    ("Product", productId.ToString()),
                    ("Quantity", returnQty.ToString("0.##")),
                    ("Entry No", entryNo),
                    ("Rows deleted", deletedRows.ToString()),
                    ("Date", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss")),
                    ("Machine", Environment.MachineName)
                },
                out var printError);

            if (!printed)
            {
                AddLog($"[WARNING] Cancellation completed, but receipt was not printed: {printError}", Color.Orange);
            }

            AddLog($"[SUCCESS] Return credit processed for ProductID {productId} in branch {branch}. Stock balance restored by {returnQty} and the original Movement row was deleted.", Color.LightGreen);
            await LoadMovementsAsync();
        }
        catch (Exception ex)
        {
            AddLog($"[ERROR] Unable to create return credit: {ex.Message}", Color.IndianRed);
        }
    }

    private async Task NotifyGatewayInvoiceDeletedAsync(string invoiceNum, string branch, int productId, string entryNo = "")
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
            // Send COMPLETE deletion details to trigger system
            var payload = new
            {
                branch = branch,
                invoice = invoiceNum,
                product_id = productId,
                entry_no = entryNo,
                action = "delete",
                deleted = true
            };

            var payloadJson = JsonSerializer.Serialize(payload);
            using var payloadContent = new StringContent(payloadJson, Encoding.UTF8, "application/json");
            var response = await client.PostAsync($"{_settings.GetApiBaseUrl()}/api/branch-sync/", payloadContent);
            if (!response.IsSuccessStatusCode)
            {
                AddLog($"[WARNING] Local delete was applied, but the gateway rejected the trigger notification: {response.StatusCode}.", Color.Orange);
            }
            else
            {
                AddLog($"[INFO] ✓ Deletion trigger sent to API: Invoice {invoiceNum} | Product {productId} | Entry {entryNo}", Color.LightBlue);
            }
        }
        catch (Exception ex)
        {
            AddLog($"[WARNING] Local delete succeeded, but the API trigger notification failed: {ex.Message}", Color.Orange);
        }
    }

    private static async Task<HashSet<string>> GetTableColumnNamesAsync(SqlConnection connection, string tableName)
    {
        var columns = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        var schemaSql = @"
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = @tableName;";

        using var schemaCommand = new SqlCommand(schemaSql, connection);
        schemaCommand.Parameters.AddWithValue("@tableName", tableName);

        using var reader = await schemaCommand.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            var name = reader[0]?.ToString();
            if (!string.IsNullOrWhiteSpace(name))
            {
                columns.Add(name.Trim());
            }
        }

        return columns;
    }

    private static async Task<HashSet<string>> GetIdentityColumnNamesAsync(SqlConnection connection, string tableName)
    {
        var identityColumns = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        var identitySql = @"
            SELECT c.name
            FROM sys.columns c
            INNER JOIN sys.tables t ON c.object_id = t.object_id
            WHERE t.name = @tableName AND c.is_identity = 1;";

        using var identityCommand = new SqlCommand(identitySql, connection);
        identityCommand.Parameters.AddWithValue("@tableName", tableName);

        using var reader = await identityCommand.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            var name = reader[0]?.ToString();
            if (!string.IsNullOrWhiteSpace(name))
            {
                identityColumns.Add(name.Trim());
            }
        }

        return identityColumns;
    }

    private static List<string> GetPreferredMovementColumns(HashSet<string> columns)
    {
        var selected = new List<string>();

        AddPreferredColumn(columns, selected, "EntryNo", "Entry", "EntryNo2");
        AddPreferredColumn(columns, selected, "InvoiceNum", "InvoiceNo", "InvoiceID");
        AddPreferredColumn(columns, selected, "ProductID", "ProductId", "ItemID");
        AddPreferredColumn(columns, selected, "Quantity", "Qty", "QTY", "ItemQty", "StockQty");
        AddPreferredColumn(columns, selected, "ProductCode", "ItemCode", "Code", "SKU");
        AddPreferredColumn(columns, selected, "coid");
        AddPreferredColumn(columns, selected, "branch");
        AddPreferredColumn(columns, selected, "DoneWhen", "TranDate", "Date", "MovementDate", "TransactionDate");
        AddPreferredColumn(columns, selected, "Ref", "Notes", "Narration", "Description");
        AddPreferredColumn(columns, selected, "TranDescription");
        AddPreferredColumn(columns, selected, "Comments");

        return selected;
    }

    private static void AddPreferredColumn(HashSet<string> columns, List<string> selected, params string[] candidateNames)
    {
        foreach (var candidate in candidateNames)
        {
            if (columns.Contains(candidate))
            {
                selected.Add(candidate);
                return;
            }
        }
    }

    private static string BuildMovementInsertSql(List<string> selectedColumns)
    {
        var parameterNames = selectedColumns.Select(c => $"@{c}").ToList();
        return $"INSERT INTO [dbo].[Movement] ({string.Join(", ", selectedColumns.Select(c => $"[{c}]"))}) VALUES ({string.Join(", ", parameterNames)});";
    }

    private static void AddMovementInsertParameters(SqlCommand command, List<string> selectedColumns, string returnReference, string branch, int productId, decimal returnQty, string entryNo, decimal coid, string productCode)
    {
        foreach (var columnName in selectedColumns)
        {
            if (string.Equals(columnName, "EntryNo", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(columnName, "Entry", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(columnName, "EntryNo2", StringComparison.OrdinalIgnoreCase))
            {
                command.Parameters.AddWithValue("@" + columnName, int.TryParse(entryNo, out var entryNoValue) ? entryNoValue : 0);
            }
            else if (string.Equals(columnName, "InvoiceNum", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "InvoiceNo", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "InvoiceID", StringComparison.OrdinalIgnoreCase))
            {
                command.Parameters.AddWithValue("@" + columnName, int.TryParse(returnReference, out var invoiceValue) ? invoiceValue : 0);
            }
            else if (string.Equals(columnName, "ProductID", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "ProductId", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "ItemID", StringComparison.OrdinalIgnoreCase))
            {
                command.Parameters.AddWithValue("@" + columnName, productId);
            }
            else if (string.Equals(columnName, "Quantity", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "Qty", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "QTY", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "ItemQty", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "StockQty", StringComparison.OrdinalIgnoreCase))
            {
                command.Parameters.AddWithValue("@" + columnName, -returnQty);
            }
            else if (string.Equals(columnName, "ProductCode", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "ItemCode", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "Code", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "SKU", StringComparison.OrdinalIgnoreCase))
            {
                command.Parameters.AddWithValue("@" + columnName, string.IsNullOrWhiteSpace(productCode) ? string.Empty : productCode);
            }
            else if (string.Equals(columnName, "coid", StringComparison.OrdinalIgnoreCase))
            {
                command.Parameters.AddWithValue("@coid", coid > 0 ? (int)coid : 0);
            }
            else if (string.Equals(columnName, "branch", StringComparison.OrdinalIgnoreCase))
            {
                command.Parameters.AddWithValue("@branch", branch);
            }
            else if (string.Equals(columnName, "DoneWhen", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "TranDate", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "Date", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "MovementDate", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "TransactionDate", StringComparison.OrdinalIgnoreCase))
            {
                var unixSeconds = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
                command.Parameters.AddWithValue("@" + columnName, (int)unixSeconds);
            }
            else if (string.Equals(columnName, "Ref", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "Notes", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "Narration", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(columnName, "Description", StringComparison.OrdinalIgnoreCase))
            {
                command.Parameters.AddWithValue("@" + columnName, $"RETURN-{returnReference}");
            }
            else if (string.Equals(columnName, "TranDescription", StringComparison.OrdinalIgnoreCase))
            {
                command.Parameters.AddWithValue("@TranDescription", "Return / Credit");
            }
            else if (string.Equals(columnName, "Comments", StringComparison.OrdinalIgnoreCase))
            {
                command.Parameters.AddWithValue("@Comments", "Return / Credit");
            }
        }
    }

    private static string GetCellValue(DataGridViewRow row, params string[] names)
    {
        foreach (var name in names)
        {
            var cell = row.Cells.Cast<DataGridViewCell>()
                .FirstOrDefault(c => string.Equals(c.OwningColumn?.Name, name, StringComparison.OrdinalIgnoreCase));

            if (cell != null && cell.Value != null)
            {
                return cell.Value.ToString();
            }
        }

        return string.Empty;
    }

    private static decimal GetCellNumericValue(DataGridViewRow row, params string[] names)
    {
        foreach (var name in names)
        {
            var cell = row.Cells.Cast<DataGridViewCell>()
                .FirstOrDefault(c => string.Equals(c.OwningColumn?.Name, name, StringComparison.OrdinalIgnoreCase));

            if (cell != null && cell.Value != null && decimal.TryParse(cell.Value.ToString(), out var result))
            {
                return result;
            }
        }

        return 0m;
    }

    private async Task LoadMovementsAsync()
    {
        _statusLabel.Text = "Loading main movement data...";
        try
        {
            using var connection = new SqlConnection(_settings.BuildConnectionString());
            await connection.OpenAsync();

            var filter = _searchTextBox.Text.Trim();
            var branchFilter = _branchComboBox.SelectedItem?.ToString() ?? "";
            var selectedDate = _dateFilterPicker.Value.ToString("yyyyMMdd");
            
            var query = @"
                SELECT TOP 500 *
                FROM [dbo].[Movement]
                WHERE (@invoiceFilter = '' OR CAST(InvoiceNum AS nvarchar(50)) LIKE '%' + @invoiceFilter + '%')
                  AND (@branchFilter = '' OR CAST(Branch AS nvarchar(100)) = @branchFilter)
                  AND (CAST(TranDate AS nvarchar(8)) = @selectedDate)
                ORDER BY Branch ASC, TranDate DESC, EntryNo DESC;";

            using var command = new SqlCommand(query, connection);
            command.Parameters.AddWithValue("@invoiceFilter", filter);
            command.Parameters.AddWithValue("@branchFilter", branchFilter);
            command.Parameters.AddWithValue("@selectedDate", selectedDate);

            using var adapter = new SqlDataAdapter(command);
            var table = new DataTable();
            adapter.Fill(table);

            if (table.Rows.Count == 0)
            {
                _gridView.DataSource = null;
                _gridView.Rows.Clear();
                AddLog("[WARNING] No movement records found for this filter.", Color.Orange);
                return;
            }

            _gridView.DataSource = table;
            if (_gridView.Columns.Contains("QRCodeIMG")) _gridView.Columns["QRCodeIMG"].Visible = false;
            if (_gridView.Columns.Contains("DigSig")) _gridView.Columns["DigSig"].Visible = false;

            AddLog($"[SUCCESS] Loaded {table.Rows.Count} movement records.", Color.LightGreen);
            _statusLabel.Text = $"Main PC ready. Loaded {table.Rows.Count} movement records.";
        }
        catch (Exception ex)
        {
            _gridView.DataSource = null;
            AddLog($"[ERROR] Unable to load Movements data: {ex.Message}", Color.IndianRed);
            _statusLabel.Text = "Main PC movement load failed.";
        }
        finally
        {
            if (!_refreshTimer.Enabled)
            {
                _refreshTimer.Start();
            }
        }
    }
}
