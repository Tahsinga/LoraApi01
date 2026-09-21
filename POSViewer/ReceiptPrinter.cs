using System.Drawing.Printing;
using System.Data;

namespace POSViewer;

public static class ReceiptPrinter
{
    public static bool TryPrintMovementReport(
        string printerName,
        string title,
        DataTable table,
        string branch,
        DateTime reportDate,
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

            var rowIndex = 0;
            var summaryPagePending = false;
            document.DefaultPageSettings.PaperSize = new PaperSize("Sales Report", 394, 3200);
            document.DefaultPageSettings.Margins = new Margins(10, 10, 10, 10);
            document.PrintPage += (_, eventArgs) =>
            {
                using var titleFont = new Font("Arial", 13F, FontStyle.Bold);
                using var bodyFont = new Font("Arial", 9F, FontStyle.Regular);
                using var boldFont = new Font("Arial", 9F, FontStyle.Bold);
                var bounds = eventArgs.MarginBounds;
                var y = bounds.Top + 40;

                eventArgs.Graphics!.DrawString(title, titleFont, Brushes.Black, bounds.Left, y);
                y += 24;
                eventArgs.Graphics.DrawString($"Branch: {branch}", bodyFont, Brushes.Black, bounds.Left, y);
                y += 20;
                eventArgs.Graphics.DrawString($"Date: {reportDate:yyyy-MM-dd}", bodyFont, Brushes.Black, bounds.Left, y);
                y += 20;
                eventArgs.Graphics.DrawLine(Pens.Black, bounds.Left, y, bounds.Right, y);
                y += 10;

                var cashierColumn = table.Columns.IndexOf("Cashier");
                var paymentMethodColumn = table.Columns.IndexOf("PaymentMethod");
                var currencyColumn = table.Columns.IndexOf("Currency");
                var totalColumn = table.Columns.IndexOf("Total");
                var taxTotalColumn = table.Columns.IndexOf("TaxTotal");
                var receiptCountColumn = table.Columns.IndexOf("ReceiptCount");
                var receiptCountsByCashier = new Dictionary<string, long>(StringComparer.Ordinal);
                var totalsByPaymentMethod = new Dictionary<string, decimal>(StringComparer.Ordinal);
                var taxesByPaymentMethod = new Dictionary<string, decimal>(StringComparer.Ordinal);
                var totalReceiptCount = 0L;
                decimal totalSales = 0m;
                decimal totalTax = 0m;
                if (receiptCountColumn >= 0)
                {
                    foreach (DataRow tableRow in table.Rows)
                    {
                        var rowCashier = cashierColumn >= 0 ? tableRow[cashierColumn]?.ToString() ?? "Unknown" : "Unknown";
                        var rowReceiptCount = long.TryParse(tableRow[receiptCountColumn]?.ToString(), out var parsedCount)
                            ? parsedCount
                            : 0L;
                        receiptCountsByCashier[rowCashier] = receiptCountsByCashier.GetValueOrDefault(rowCashier) + rowReceiptCount;
                        totalReceiptCount += rowReceiptCount;
                    }
                }
                foreach (DataRow tableRow in table.Rows)
                {
                    var paymentMethod = paymentMethodColumn >= 0 ? tableRow[paymentMethodColumn]?.ToString() ?? "Unknown" : "Unknown";
                    var currency = currencyColumn >= 0 ? tableRow[currencyColumn]?.ToString() ?? "UNKNOWN" : "UNKNOWN";
                    var paymentKey = $"{paymentMethod} ({currency})";
                    if (totalColumn >= 0 && decimal.TryParse(tableRow[totalColumn]?.ToString(), out var rowTotal))
                    {
                        totalSales += rowTotal;
                        totalsByPaymentMethod[paymentKey] = totalsByPaymentMethod.GetValueOrDefault(paymentKey) + rowTotal;
                    }

                    if (taxTotalColumn >= 0 && decimal.TryParse(tableRow[taxTotalColumn]?.ToString(), out var rowTax))
                    {
                        totalTax += rowTax;
                        taxesByPaymentMethod[paymentKey] = taxesByPaymentMethod.GetValueOrDefault(paymentKey) + rowTax;
                    }
                }

                if (summaryPagePending)
                {
                    eventArgs.Graphics.DrawString("REPORT SUMMARY", titleFont, Brushes.Black, bounds.Left, y);
                    y += 30;
                    eventArgs.Graphics.DrawString("ALL USERS TOTALS BY PAYMENT METHOD", boldFont, Brushes.Black, bounds.Left, y);
                    y += 24;
                    foreach (var paymentTotal in totalsByPaymentMethod.OrderBy(entry => entry.Key))
                    {
                        var paymentTax = taxesByPaymentMethod.GetValueOrDefault(paymentTotal.Key);
                        eventArgs.Graphics.DrawString($"{paymentTotal.Key}: {paymentTotal.Value:0.00}", bodyFont, Brushes.Black, bounds.Left, y);
                        y += 20;
                        eventArgs.Graphics.DrawString($"  Tax: {paymentTax:0.00}", bodyFont, Brushes.Black, bounds.Left + 10, y);
                        y += 20;
                    }

                    eventArgs.Graphics.DrawString($"All users total: {totalSales:0.00}", boldFont, Brushes.Black, bounds.Left, y);
                    y += 22;
                    eventArgs.Graphics.DrawString($"Total tax: {totalTax:0.00}", boldFont, Brushes.Black, bounds.Left, y);
                    y += 22;
                    eventArgs.Graphics.DrawString($"Total receipts: {totalReceiptCount}", boldFont, Brushes.Black, bounds.Left, y);
                    y += 30;
                    eventArgs.Graphics.DrawLine(Pens.Black, bounds.Left, y, bounds.Right, y);
                    y += 12;

                    foreach (var cashierTotal in receiptCountsByCashier.OrderBy(entry => entry.Key))
                    {
                        eventArgs.Graphics.DrawString($"{cashierTotal.Key}: {cashierTotal.Value} receipt(s)", bodyFont, Brushes.Black, bounds.Left, y);
                        y += 20;
                    }

                    eventArgs.HasMorePages = false;
                    return;
                }

                string? previousCashier = null;

                while (rowIndex < table.Rows.Count)
                {
                    var row = table.Rows[rowIndex];

                    var cashier = cashierColumn >= 0 ? row[cashierColumn]?.ToString() ?? "Unknown" : "Unknown";
                    if (!string.Equals(previousCashier, cashier, StringComparison.Ordinal))
                    {
                        if (previousCashier != null)
                        {
                            eventArgs.Graphics.DrawLine(Pens.Black, bounds.Left, y, bounds.Right, y);
                            y += 8;
                        }

                        eventArgs.Graphics.DrawString(cashier, boldFont, Brushes.Black, bounds.Left, y);
                        y += 14;
                        var cashierReceiptCount = receiptCountsByCashier.GetValueOrDefault(cashier);
                        eventArgs.Graphics.DrawString($"Receipts: {cashierReceiptCount}", bodyFont, Brushes.Black, bounds.Left + 10, y);
                        y += 18;
                        previousCashier = cashier;
                    }

                    if (paymentMethodColumn >= 0 && currencyColumn >= 0 && totalColumn >= 0)
                    {
                        var paymentMethod = row[paymentMethodColumn]?.ToString() ?? string.Empty;
                        var currency = row[currencyColumn]?.ToString() ?? string.Empty;
                        var total = row[totalColumn] == DBNull.Value ? "0" : row[totalColumn]?.ToString() ?? "0";
                        var summary = $"{paymentMethod} ({currency}): {total}";
                        var summarySize = eventArgs.Graphics.MeasureString(summary, bodyFont, new SizeF(bounds.Width, bounds.Bottom - y));
                        if (y + summarySize.Height > bounds.Bottom - 48)
                        {
                            break;
                        }

                        eventArgs.Graphics.DrawString(summary, bodyFont, Brushes.Black, bounds.Left + 10, y);
                        y += Math.Max(12, (int)Math.Ceiling(summarySize.Height));
                    }

                    rowIndex++;
                    y += 4;
                }

                if (rowIndex < table.Rows.Count)
                {
                    eventArgs.HasMorePages = true;
                    return;
                }

                summaryPagePending = true;
                eventArgs.HasMorePages = true;
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
