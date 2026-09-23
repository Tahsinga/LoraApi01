namespace POSViewer;

static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        ConnectionSettings.ConfigureProfile(args);
        ApplicationConfiguration.Initialize();
        Application.Run(new TrayApplicationContext());
    }
}