using System.Drawing.Printing;

namespace POSViewer;

public static class ReceiptPrinter
{
    public static bool TryPrint(
        string printerName,
        string title,
        IReadOnlyList<(string Label, string Value)> details,
        out string error)
    {
        error = string.Empty;

        if (string.IsNullOrWhiteSpace(printerName))
        {
            error = "No receipt printer is selected.";
            return false;
        }

        try
        {
            using var document = new PrintDocument();
            document.PrinterSettings.PrinterName = printerName;
            if (!document.PrinterSettings.IsValid)
            {
                error = $"The selected printer is unavailable: {printerName}";
                return false;
            }

            document.DefaultPageSettings.PaperSize = new PaperSize("Receipt", 315, 1200);
            document.DefaultPageSettings.Margins = new Margins(10, 10, 10, 10);
            document.PrintPage += (_, eventArgs) =>
            {
                using var titleFont = new Font("Arial", 11F, FontStyle.Bold);
                using var bodyFont = new Font("Arial", 8.5F, FontStyle.Regular);
                using var boldFont = new Font("Arial", 8.5F, FontStyle.Bold);
                var bounds = eventArgs.MarginBounds;
                var y = bounds.Top;

                eventArgs.Graphics!.DrawString(title, titleFont, Brushes.Black, bounds.Left, y);
                y += 26;
                eventArgs.Graphics.DrawLine(Pens.Black, bounds.Left, y, bounds.Right, y);
                y += 10;

                foreach (var detail in details)
                {
                    var label = $"{detail.Label}:";
                    eventArgs.Graphics.DrawString(label, boldFont, Brushes.Black, bounds.Left, y);
                    var labelWidth = eventArgs.Graphics.MeasureString(label, boldFont).Width + 4;
                    var valueBounds = new RectangleF(bounds.Left + labelWidth, y, bounds.Width - labelWidth, 60);
                    var valueSize = eventArgs.Graphics.MeasureString(detail.Value ?? string.Empty, bodyFont, valueBounds.Size);
                    eventArgs.Graphics.DrawString(detail.Value ?? string.Empty, bodyFont, Brushes.Black, valueBounds);
                    y += Math.Max(18, (int)Math.Ceiling(valueSize.Height + 4));
                }

                y += 8;
                eventArgs.Graphics.DrawLine(Pens.Black, bounds.Left, y, bounds.Right, y);
                y += 10;
                eventArgs.Graphics.DrawString("Thank you", bodyFont, Brushes.Black, bounds.Left, y);
                eventArgs.HasMorePages = false;
            };

            document.Print();
            return true;
        }
        catch (Exception ex)
        {
            error = ex.Message;
            return false;
        }
    }
}
