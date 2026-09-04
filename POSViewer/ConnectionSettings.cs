using System.Data.SqlClient;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace POSViewer;

public sealed class ConnectionSettings
{
    public const string DefaultApiBaseUrl = "https://loraapi.onrender.com";

    public string Server { get; set; } = "";
    public string Database { get; set; } = "";
    public string Username { get; set; } = "";
    public string Password { get; set; } = "";
    public bool IntegratedSecurity { get; set; } = true;
    public bool RememberLogin { get; set; } = true;
    public string DeviceRole { get; set; } = "Branch PC";
    public string ApiBaseUrl { get; set; } = DefaultApiBaseUrl;
    public string PrinterName { get; set; } = "";

    public string GetApiBaseUrl()
    {
        return NormalizeApiBaseUrl(ApiBaseUrl);
    }

    public static string NormalizeApiBaseUrl(string apiBaseUrl)
    {
        var value = apiBaseUrl.Trim();
        if (Uri.TryCreate(value, UriKind.Absolute, out var uri))
        {
            return new UriBuilder(uri.Scheme, uri.Host, uri.Port).Uri.ToString().TrimEnd('/');
        }

        return value.TrimEnd('/');
    }

    public static string StoragePath
    {
        get
        {
            var folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "LoraPOSReturns");
            Directory.CreateDirectory(folder);
            return Path.Combine(folder, "connection.json");
        }
    }

    public string BuildConnectionString()
    {
        var builder = new SqlConnectionStringBuilder
        {
            DataSource = Server,
            InitialCatalog = Database,
            IntegratedSecurity = IntegratedSecurity,
            Encrypt = true,
            TrustServerCertificate = true
        };

        if (!IntegratedSecurity)
        {
            builder.UserID = Username;
            builder.Password = Password;
        }

        return builder.ConnectionString;
    }

    public static void Save(ConnectionSettings settings)
    {
        var payload = new ConnectionSettings
        {
            Server = settings.Server,
            Database = settings.Database,
            Username = settings.Username,
            Password = settings.RememberLogin && !string.IsNullOrWhiteSpace(settings.Password)
                ? Encrypt(settings.Password)
                : string.Empty,
            IntegratedSecurity = settings.IntegratedSecurity,
            RememberLogin = settings.RememberLogin,
            DeviceRole = "Branch PC",
            ApiBaseUrl = string.IsNullOrWhiteSpace(settings.ApiBaseUrl)
                ? DefaultApiBaseUrl
                : settings.ApiBaseUrl.TrimEnd('/'),
            PrinterName = settings.PrinterName?.Trim() ?? string.Empty
        };

        var json = JsonSerializer.Serialize(payload, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(StoragePath, json);
    }

    public static ConnectionSettings Load()
    {
        if (!File.Exists(StoragePath))
        {
            return new ConnectionSettings();
        }

        try
        {
            var json = File.ReadAllText(StoragePath);
            var settings = JsonSerializer.Deserialize<ConnectionSettings>(json) ?? new ConnectionSettings();

            if (settings.RememberLogin && !string.IsNullOrWhiteSpace(settings.Password))
            {
                settings.Password = Decrypt(settings.Password);
            }
            else
            {
                settings.Password = string.Empty;
            }

            if (string.IsNullOrWhiteSpace(settings.ApiBaseUrl))
            {
                settings.ApiBaseUrl = DefaultApiBaseUrl;
            }

            settings.DeviceRole = "Branch PC";

            return settings;
        }
        catch
        {
            return new ConnectionSettings();
        }
    }

    public static string Encrypt(string plainText)
    {
        if (string.IsNullOrWhiteSpace(plainText))
        {
            return string.Empty;
        }

        var bytes = Encoding.UTF8.GetBytes(plainText);
        var protectedBytes = ProtectedData.Protect(bytes, null, DataProtectionScope.CurrentUser);
        return Convert.ToBase64String(protectedBytes);
    }

    public static string Decrypt(string protectedText)
    {
        if (string.IsNullOrWhiteSpace(protectedText))
        {
            return string.Empty;
        }

        var bytes = Convert.FromBase64String(protectedText);
        var unprotectedBytes = ProtectedData.Unprotect(bytes, null, DataProtectionScope.CurrentUser);
        return Encoding.UTF8.GetString(unprotectedBytes);
    }
}
