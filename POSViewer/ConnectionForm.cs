using System.Data.SqlClient;
using System.Net.Http.Json;

namespace POSViewer;

public sealed class ConnectionForm : Form
{
    private readonly TextBox _serverTextBox = new();
    private readonly TextBox _databaseTextBox = new();
    private readonly TextBox _usernameTextBox = new();
    private readonly TextBox _passwordTextBox = new();
    private readonly CheckBox _integratedSecurityCheckBox = new();
    private readonly CheckBox _rememberLoginCheckBox = new();
    private readonly TextBox _apiUrlTextBox = new();
    private readonly ComboBox _deviceRoleComboBox = new();
    private readonly ComboBox _printerComboBox = new();
    private readonly Button _testPrinterButton = new();
    private readonly Button _testSqlButton = new();
    private readonly Button _testApiButton = new();
    private readonly Button _saveAndConnectButton = new();
    private readonly Label _statusLabel = new();
    private readonly Label _usernameLabel = new();
    private readonly Label _passwordLabel = new();

    private bool _autoLoginAttempted;
    private CancellationTokenSource? _autoLoginRetryCts;
    internal bool StartHidden;

    public ConnectionForm()
    {
        InitializeComponent();
        LoadSavedValues();
    }

    public void ResetAndShow()
    {
        _autoLoginRetryCts?.Cancel();
        _serverTextBox.Clear();
        _databaseTextBox.Clear();
        _usernameTextBox.Clear();
        _passwordTextBox.Clear();
        _integratedSecurityCheckBox.Checked = true;
        _rememberLoginCheckBox.Checked = true;
        _apiUrlTextBox.Text = ConnectionSettings.DefaultApiBaseUrl;
        _deviceRoleComboBox.SelectedItem = "Branch PC";

        var saved = ConnectionSettings.Load();
        if (saved.RememberLogin)
        {
            _serverTextBox.Text = saved.Server;
            _databaseTextBox.Text = saved.Database;
            _usernameTextBox.Text = saved.Username;
            _integratedSecurityCheckBox.Checked = saved.IntegratedSecurity;
            _rememberLoginCheckBox.Checked = saved.RememberLogin;
            _passwordTextBox.Text = saved.Password;
            _apiUrlTextBox.Text = string.IsNullOrWhiteSpace(saved.ApiBaseUrl) ? ConnectionSettings.DefaultApiBaseUrl : saved.ApiBaseUrl;
            if (!string.IsNullOrWhiteSpace(saved.DeviceRole))
            {
                _deviceRoleComboBox.SelectedItem = saved.DeviceRole;
            }
            SelectSavedPrinter(saved.PrinterName);
        }

        _statusLabel.Text = string.Empty;
        Show();
        Activate();
    }

    public void ShowConnectionScreen()
    {
        LoadSavedValues();
        Show();
        Activate();
    }

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);

        if (_autoLoginAttempted)
        {
            return;
        }

        var saved = ConnectionSettings.Load();
        if (saved.RememberLogin && !string.IsNullOrWhiteSpace(saved.Server) && !string.IsNullOrWhiteSpace(saved.Database))
        {
            _autoLoginAttempted = true;
            _autoLoginRetryCts = new CancellationTokenSource();
            _ = TryConnectUntilConnectedAsync(saved, _autoLoginRetryCts.Token);
        }
    }

    private void InitializeComponent()
    {
        Text = "SQL Connection";
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        MinimizeBox = false;
        Size = new Size(520, 610);

        var titleLabel = new Label
        {
            Text = "Connect to SQL Server",
            Font = new Font("Segoe UI", 16F, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(20, 20)
        };

        var serverLabel = new Label { Text = "Server:", Location = new Point(20, 70), AutoSize = true };
        _serverTextBox.Location = new Point(160, 66);
        _serverTextBox.Size = new Size(300, 28);
        _serverTextBox.Text = "(localdb)\\MSSQLLocalDB";

        var databaseLabel = new Label { Text = "Database:", Location = new Point(20, 110), AutoSize = true };
        _databaseTextBox.Location = new Point(160, 106);
        _databaseTextBox.Size = new Size(300, 28);
        _databaseTextBox.Text = "POS";

        _usernameLabel.Text = "Username:";
        _usernameLabel.Location = new Point(20, 150);
        _usernameLabel.AutoSize = true;
        _usernameTextBox.Location = new Point(160, 146);
        _usernameTextBox.Size = new Size(300, 28);

        _passwordLabel.Text = "Password:";
        _passwordLabel.Location = new Point(20, 190);
        _passwordLabel.AutoSize = true;
        _passwordTextBox.Location = new Point(160, 186);
        _passwordTextBox.Size = new Size(300, 28);
        _passwordTextBox.UseSystemPasswordChar = true;

        _integratedSecurityCheckBox.Text = "Use Windows Authentication";
        _integratedSecurityCheckBox.Location = new Point(160, 225);
        _integratedSecurityCheckBox.Checked = true;
        _integratedSecurityCheckBox.CheckedChanged += (_, _) => UpdateAuthenticationFields();

        _rememberLoginCheckBox.Text = "Remember my connection";
        _rememberLoginCheckBox.Location = new Point(160, 255);
        _rememberLoginCheckBox.Checked = true;

        var apiUrlLabel = new Label
        {
            Text = "API URL:",
            Location = new Point(20, 290),
            AutoSize = true
        };

        _apiUrlTextBox.Location = new Point(160, 286);
        _apiUrlTextBox.Size = new Size(300, 28);
        _apiUrlTextBox.Text = ConnectionSettings.DefaultApiBaseUrl;

        var deviceRoleLabel = new Label
        {
            Text = "PC Role:",
            Location = new Point(20, 330),
            AutoSize = true
        };

        _deviceRoleComboBox.DropDownStyle = ComboBoxStyle.DropDownList;
        _deviceRoleComboBox.Location = new Point(160, 326);
        _deviceRoleComboBox.Size = new Size(300, 28);
        _deviceRoleComboBox.Items.Add("Branch PC");
        _deviceRoleComboBox.SelectedItem = "Branch PC";

        var printerLabel = new Label
        {
            Text = "Receipt Printer:",
            Location = new Point(20, 370),
            AutoSize = true
        };

        _printerComboBox.DropDownStyle = ComboBoxStyle.DropDownList;
        _printerComboBox.Location = new Point(160, 366);
        _printerComboBox.Size = new Size(300, 28);
        LoadPrinters();

        _testSqlButton.Text = "Test SQL";
        _testSqlButton.Size = new Size(125, 35);
        _testSqlButton.Location = new Point(160, 410);
        _testSqlButton.Click += async (_, _) => await TestSqlConnectionAsync();

        _testPrinterButton.Text = "Test Printer";
        _testPrinterButton.Size = new Size(125, 35);
        _testPrinterButton.Location = new Point(25, 410);
        _testPrinterButton.Click += (_, _) => TestPrinter();

        _testApiButton.Text = "Test API";
        _testApiButton.Size = new Size(125, 35);
        _testApiButton.Location = new Point(295, 410);
        _testApiButton.Click += async (_, _) => await TestApiConnectionAsync();

        _saveAndConnectButton.Text = "Save & Connect";
        _saveAndConnectButton.Size = new Size(150, 35);
        _saveAndConnectButton.Location = new Point(160, 455);
        _saveAndConnectButton.BackColor = Color.FromArgb(0, 120, 215);
        _saveAndConnectButton.ForeColor = Color.White;
        _saveAndConnectButton.FlatStyle = FlatStyle.Flat;
        _saveAndConnectButton.Click += async (_, _) =>
        {
            _autoLoginRetryCts?.Cancel();
            await TryConnectAsync(BuildSettingsFromForm(), showSuccessMessage: true, showDashboardAfterSuccess: true);
        };

        _statusLabel.AutoSize = true;
        _statusLabel.Location = new Point(20, 505);
        _statusLabel.ForeColor = Color.ForestGreen;

        Controls.Add(titleLabel);
        Controls.Add(serverLabel);
        Controls.Add(_serverTextBox);
        Controls.Add(databaseLabel);
        Controls.Add(_databaseTextBox);
        Controls.Add(_usernameLabel);
        Controls.Add(_usernameTextBox);
        Controls.Add(_passwordLabel);
        Controls.Add(_passwordTextBox);
        Controls.Add(_integratedSecurityCheckBox);
        Controls.Add(_rememberLoginCheckBox);
        Controls.Add(apiUrlLabel);
        Controls.Add(_apiUrlTextBox);
        Controls.Add(deviceRoleLabel);
        Controls.Add(_deviceRoleComboBox);
        Controls.Add(printerLabel);
        Controls.Add(_printerComboBox);
        Controls.Add(_testPrinterButton);
        Controls.Add(_testSqlButton);
        Controls.Add(_testApiButton);
        Controls.Add(_saveAndConnectButton);
        Controls.Add(_statusLabel);

        UpdateAuthenticationFields();
    }

    private void UpdateAuthenticationFields()
    {
        var usingWindowsAuth = _integratedSecurityCheckBox.Checked;
        _usernameLabel.Enabled = !usingWindowsAuth;
        _passwordLabel.Enabled = !usingWindowsAuth;
        _usernameTextBox.Enabled = !usingWindowsAuth;
        _passwordTextBox.Enabled = !usingWindowsAuth;
    }

    private void LoadSavedValues()
    {
        var saved = ConnectionSettings.Load();
        if (!saved.RememberLogin)
        {
            return;
        }

        _serverTextBox.Text = saved.Server;
        _databaseTextBox.Text = saved.Database;
        _usernameTextBox.Text = saved.Username;
        _integratedSecurityCheckBox.Checked = saved.IntegratedSecurity;
        _rememberLoginCheckBox.Checked = saved.RememberLogin;
        _passwordTextBox.Text = saved.Password;
        _apiUrlTextBox.Text = string.IsNullOrWhiteSpace(saved.ApiBaseUrl) ? ConnectionSettings.DefaultApiBaseUrl : saved.ApiBaseUrl;
        SelectSavedPrinter(saved.PrinterName);
        UpdateAuthenticationFields();
    }

    private void LoadPrinters()
    {
        _printerComboBox.Items.Clear();
        foreach (string printerName in System.Drawing.Printing.PrinterSettings.InstalledPrinters)
        {
            _printerComboBox.Items.Add(printerName);
        }

        if (_printerComboBox.Items.Count == 0)
        {
            _printerComboBox.Items.Add("No printers found");
            _printerComboBox.SelectedIndex = 0;
        }
    }

    private void SelectSavedPrinter(string printerName)
    {
        if (!string.IsNullOrWhiteSpace(printerName))
        {
            var index = _printerComboBox.Items.IndexOf(printerName);
            if (index >= 0)
            {
                _printerComboBox.SelectedIndex = index;
                return;
            }
        }

        if (_printerComboBox.Items.Count > 0 && _printerComboBox.Items[0]?.ToString() != "No printers found")
        {
            _printerComboBox.SelectedIndex = 0;
        }
    }

    private ConnectionSettings BuildSettingsFromForm()
    {
        return new ConnectionSettings
        {
            Server = _serverTextBox.Text.Trim(),
            Database = _databaseTextBox.Text.Trim(),
            Username = _usernameTextBox.Text.Trim(),
            Password = _passwordTextBox.Text.Trim(),
            IntegratedSecurity = _integratedSecurityCheckBox.Checked,
            RememberLogin = _rememberLoginCheckBox.Checked,
            DeviceRole = _deviceRoleComboBox.SelectedItem?.ToString() ?? "Branch PC",
            ApiBaseUrl = string.IsNullOrWhiteSpace(_apiUrlTextBox.Text) ? ConnectionSettings.DefaultApiBaseUrl : _apiUrlTextBox.Text.Trim(),
            PrinterName = _printerComboBox.SelectedItem?.ToString() == "No printers found" ? string.Empty : _printerComboBox.SelectedItem?.ToString() ?? string.Empty
        };
    }

    private static async Task<bool> CheckGatewayAsync(string apiBaseUrl)
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            var normalizedApiUrl = ConnectionSettings.NormalizeApiBaseUrl(apiBaseUrl);
            var response = await client.GetAsync($"{normalizedApiUrl}/api/health/");
            if (!response.IsSuccessStatusCode)
            {
                return false;
            }

            var payload = await response.Content.ReadFromJsonAsync<GatewayHealthResponse>();
            return payload is not null && string.Equals(payload.status, "ok", StringComparison.OrdinalIgnoreCase);
        }
        catch
        {
            return false;
        }
    }

    private async Task TestSqlConnectionAsync()
    {
        var settings = BuildSettingsFromForm();
        if (string.IsNullOrWhiteSpace(settings.Server) || string.IsNullOrWhiteSpace(settings.Database))
        {
            _statusLabel.ForeColor = Color.DarkRed;
            _statusLabel.Text = "SQL test failed: server and database are required.";
            return;
        }

        if (!settings.IntegratedSecurity && string.IsNullOrWhiteSpace(settings.Username))
        {
            _statusLabel.ForeColor = Color.DarkRed;
            _statusLabel.Text = "SQL test failed: username is required for SQL auth.";
            return;
        }

        try
        {
            using var connection = new SqlConnection(settings.BuildConnectionString());
            await connection.OpenAsync();
            _statusLabel.ForeColor = Color.ForestGreen;
            _statusLabel.Text = "SQL test successful: database connection is working.";
        }
        catch (Exception ex)
        {
            _statusLabel.ForeColor = Color.DarkRed;
            _statusLabel.Text = $"SQL test failed: {ex.Message}";
        }
    }

    private void TestPrinter()
    {
        var printerName = _printerComboBox.SelectedItem?.ToString() ?? string.Empty;
        if (printerName == "No printers found")
        {
            printerName = string.Empty;
        }

        var printed = ReceiptPrinter.TryPrint(
            printerName,
            "PRINTER TEST",
            new List<(string Label, string Value)>
            {
                ("Status", "Printer is working"),
                ("Printer", printerName),
                ("Date", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss")),
                ("Machine", Environment.MachineName)
            },
            out var printError);

        _statusLabel.ForeColor = printed ? Color.ForestGreen : Color.DarkRed;
        _statusLabel.Text = printed
            ? "Printer test page sent successfully."
            : $"Printer test failed: {printError}";
    }

    private async Task TestApiConnectionAsync()
    {
        var settings = BuildSettingsFromForm();
        var apiUrl = string.IsNullOrWhiteSpace(_apiUrlTextBox.Text) ? ConnectionSettings.DefaultApiBaseUrl : _apiUrlTextBox.Text.Trim();
        try
        {
            var reachable = await CheckGatewayAsync(apiUrl);
            if (reachable)
            {
                _statusLabel.ForeColor = Color.ForestGreen;
                _statusLabel.Text = $"API test successful: {apiUrl} is responding.";
                return;
            }

            _statusLabel.ForeColor = Color.DarkRed;
            _statusLabel.Text = $"API test failed: {apiUrl} is not reachable or not returning a valid response.";
        }
        catch (Exception ex)
        {
            _statusLabel.ForeColor = Color.DarkRed;
            _statusLabel.Text = $"API test failed: {ex.Message}";
        }
    }

    private async Task TryConnectAsync(ConnectionSettings settings, bool showSuccessMessage, bool showDashboardAfterSuccess)
    {
        if (string.IsNullOrWhiteSpace(settings.Server) || string.IsNullOrWhiteSpace(settings.Database))
        {
            _statusLabel.ForeColor = Color.DarkRed;
            _statusLabel.Text = "Server and database are required.";
            return;
        }

        if (!settings.IntegratedSecurity && string.IsNullOrWhiteSpace(settings.Username))
        {
            _statusLabel.ForeColor = Color.DarkRed;
            _statusLabel.Text = "Username is required for SQL authentication.";
            return;
        }

        try
        {
            using var connection = new SqlConnection(settings.BuildConnectionString());
            await connection.OpenAsync();
            await CompleteConnectionAsync(settings, showSuccessMessage, showDashboardAfterSuccess);
        }
        catch (Exception ex)
        {
            _statusLabel.ForeColor = Color.DarkRed;
            _statusLabel.Text = $"Connection failed: {ex.Message}";
        }
    }

    private async Task TryConnectUntilConnectedAsync(ConnectionSettings settings, CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                using var connection = new SqlConnection(settings.BuildConnectionString());
                await connection.OpenAsync(cancellationToken);
                await CompleteConnectionAsync(settings, showSuccessMessage: false, showDashboardAfterSuccess: true);
                return;
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception ex)
            {
                _statusLabel.ForeColor = Color.DarkOrange;
                _statusLabel.Text = $"SQL Server is unavailable. Retrying in 10 seconds... {ex.Message}";

                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(10), cancellationToken);
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    return;
                }
            }
        }
    }

    private async Task CompleteConnectionAsync(ConnectionSettings settings, bool showSuccessMessage, bool showDashboardAfterSuccess)
    {
        var gatewayReachable = await CheckGatewayAsync(settings.ApiBaseUrl);

        if (gatewayReachable)
        {
            _statusLabel.ForeColor = Color.ForestGreen;
            _statusLabel.Text = showSuccessMessage ? "SQL and API connection successful." : string.Empty;
        }
        else
        {
            _statusLabel.ForeColor = Color.DarkOrange;
            _statusLabel.Text = showSuccessMessage
                ? $"SQL connection successful, but Django API is not reachable at {settings.ApiBaseUrl}."
                : string.Empty;
        }

        if (showDashboardAfterSuccess)
        {
            ConnectionSettings.Save(settings);

            Form dashboard = settings.DeviceRole == "Branch PC"
                ? new BranchSyncDashboardForm(settings, this)
                : new DashboardForm(settings, this);

            dashboard.Show();
            if (StartHidden)
            {
                dashboard.Hide();
            }
            Hide();
        }
        else if (settings.RememberLogin)
        {
            ConnectionSettings.Save(settings);
        }
    }

    private sealed class GatewayHealthResponse
    {
        public string? status { get; set; }
    }
}
