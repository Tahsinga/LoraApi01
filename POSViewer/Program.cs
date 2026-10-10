namespace POSViewer;

static class Program
{
    private const string SingleInstanceMutexName = @"Local\LoraReturnsConnect.POSViewer.SingleInstance";

    [STAThread]
    static void Main(string[] args)
    {
        using var instanceMutex = new Mutex(
            initiallyOwned: true,
            name: SingleInstanceMutexName,
            createdNew: out var isFirstInstance);
        if (!isFirstInstance)
        {
            return;
        }

        ConnectionSettings.ConfigureProfile(args);
        ApplicationConfiguration.Initialize();
        Application.Run(new TrayApplicationContext());
    }
}