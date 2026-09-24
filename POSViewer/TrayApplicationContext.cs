using System.Drawing;
using Microsoft.Win32;

namespace POSViewer;

internal sealed class TrayApplicationContext : ApplicationContext
{
    private readonly NotifyIcon _notifyIcon;
    private readonly ContextMenuStrip _contextMenu;
    private readonly ConnectionForm _connectionForm;
    private bool _exitRequested;

    public TrayApplicationContext()
    {
        RegisterStartup();
        _contextMenu = new ContextMenuStrip();
        _contextMenu.Items.Add("Continue", null, (_, _) => ShowApplication());
        _contextMenu.Items.Add("Exit", null, (_, _) => ExitApplication());

        _notifyIcon = new NotifyIcon
        {
            Icon = SystemIcons.Application,
            Text = "Lora POS Returns",
            ContextMenuStrip = _contextMenu,
            Visible = true
        };
        _notifyIcon.DoubleClick += (_, _) => ShowApplication();

        _connectionForm = new ConnectionForm();
        _connectionForm.StartHidden = true;
        _connectionForm.FormClosed += HandleFormClosed;

        // Let the form initialize its saved connection and dashboard before hiding it.
        _connectionForm.Show();
    }

    private static void RegisterStartup()
    {
        using var startupKey = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run");
        startupKey?.SetValue("LoraPOSViewer", $"\"{Application.ExecutablePath}\"");
    }

    private void ShowApplication()
    {
        Form? formToShow = null;
        for (var index = Application.OpenForms.Count - 1; index >= 0; index--)
        {
            var form = Application.OpenForms[index];
            if (!form.IsDisposed)
            {
                formToShow = form;
                break;
            }
        }

        formToShow ??= _connectionForm;
        formToShow.Show();
        formToShow.WindowState = FormWindowState.Normal;
        formToShow.Activate();
        formToShow.BringToFront();
    }

    private void HandleFormClosed(object? sender, FormClosedEventArgs e)
    {
        if (!_exitRequested && Application.OpenForms.Count == 0)
        {
            ExitApplication();
        }
    }

    private void ExitApplication()
    {
        _exitRequested = true;
        _notifyIcon.Visible = false;
        _notifyIcon.Dispose();
        _contextMenu.Dispose();

        foreach (Form form in Application.OpenForms.Cast<Form>().ToList())
        {
            form.Close();
        }

        ExitThread();
    }
}
