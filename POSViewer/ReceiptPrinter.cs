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

            var cashierColumn = table.Columns.IndexOf("Cashier");
            var paymentMethodColumn = table.Columns.IndexOf("PaymentMethod");
            var currencyColumn = table.Columns.IndexOf("Currency");
            var rateColumn = table.Columns.IndexOf("Rate");
            var totalColumn = table.Columns.IndexOf("Total");
            var taxTotalColumn = table.Columns.IndexOf("TaxTotal");
            var invoiceColumn = table.Columns.IndexOf("InvoiceNumber");
            var cashierPaymentMethods = new Dictionary<string, HashSet<string>>(StringComparer.Ordinal);
            var reportPaymentMethods = new HashSet<string>(StringComparer.Ordinal);
            foreach (DataRow tableRow in table.Rows)
            {
                var rowCashier = cashierColumn >= 0 ? tableRow[cashierColumn]?.ToString() ?? "Unknown" : "Unknown";
                var paymentMethod = paymentMethodColumn >= 0 ? tableRow[paymentMethodColumn]?.ToString() ?? "Unknown" : "Unknown";
                var currency = currencyColumn >= 0 ? tableRow[currencyColumn]?.ToString() ?? "UNKNOWN" : "UNKNOWN";
                var paymentKey = $"{paymentMethod} ({currency})";
                reportPaymentMethods.Add(paymentKey);
                if (!cashierPaymentMethods.TryGetValue(rowCashier, out var cashierMethods))
                {
                    cashierMethods = new HashSet<string>(StringComparer.Ordinal);
                    cashierPaymentMethods[rowCashier] = cashierMethods;
                }

                cashierMethods.Add(paymentKey);
            }

            var cashierPaymentLineCount = cashierPaymentMethods.Values.Sum(methods => methods.Count);
            var pageHeight = Math.Clamp(
                400 + cashierPaymentMethods.Count * 58 + cashierPaymentLineCount * 16 + reportPaymentMethods.Count * 32,
                700,
                3200);
            document.DefaultPageSettings.PaperSize = new PaperSize("Sales Report", 394, pageHeight);
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

                var receiptCountsByCashier = new Dictionary<string, long>(StringComparer.Ordinal);
                var invoicesByCashier = new Dictionary<string, HashSet<string>>(StringComparer.Ordinal);
                var totalsByPaymentMethod = new Dictionary<string, decimal>(StringComparer.Ordinal);
                var taxesByPaymentMethod = new Dictionary<string, decimal>(StringComparer.Ordinal);
                var totalsByCashier = new Dictionary<string, Dictionary<string, decimal>>(StringComparer.Ordinal);
                var allInvoices = new HashSet<string>(StringComparer.Ordinal);
                var totalReceiptCount = 0L;
                decimal totalSales = 0m;
                decimal totalUsdSales = 0m;
                decimal totalTax = 0m;
                if (invoiceColumn >= 0)
                {
                    foreach (DataRow tableRow in table.Rows)
                    {
                        var rowCashier = cashierColumn >= 0 ? tableRow[cashierColumn]?.ToString() ?? "Unknown" : "Unknown";
                        var invoiceNumber = tableRow[invoiceColumn]?.ToString() ?? string.Empty;
                        if (string.IsNullOrWhiteSpace(invoiceNumber))
                        {
                            continue;
                        }

                        if (!invoicesByCashier.TryGetValue(rowCashier, out var cashierInvoices))
                        {
                            cashierInvoices = new HashSet<string>(StringComparer.Ordinal);
                            invoicesByCashier[rowCashier] = cashierInvoices;
                        }

                        cashierInvoices.Add(invoiceNumber);
                        allInvoices.Add(invoiceNumber);
                    }

                    totalReceiptCount = allInvoices.Count;
                    foreach (var cashierInvoices in invoicesByCashier)
                    {
                        receiptCountsByCashier[cashierInvoices.Key] = cashierInvoices.Value.Count;
                    }
                }
                foreach (DataRow tableRow in table.Rows)
                {
                    var rowCashier = cashierColumn >= 0 ? tableRow[cashierColumn]?.ToString() ?? "Unknown" : "Unknown";
                    var paymentMethod = paymentMethodColumn >= 0 ? tableRow[paymentMethodColumn]?.ToString() ?? "Unknown" : "Unknown";
                    var currency = currencyColumn >= 0 ? tableRow[currencyColumn]?.ToString() ?? "UNKNOWN" : "UNKNOWN";
                    var paymentKey = $"{paymentMethod} ({currency})";
                    if (totalColumn >= 0 && decimal.TryParse(tableRow[totalColumn]?.ToString(), out var rowTotal))
                    {
                        var rate = rateColumn >= 0 && decimal.TryParse(tableRow[rateColumn]?.ToString(), out var parsedRate) && parsedRate > 0m
                            ? parsedRate
                            : 1m;
                        totalUsdSales += string.Equals(currency.Trim(), "USD", StringComparison.OrdinalIgnoreCase)
                            ? rowTotal
                            : rowTotal / rate;
                        totalSales += rowTotal;
                        totalsByPaymentMethod[paymentKey] = totalsByPaymentMethod.GetValueOrDefault(paymentKey) + rowTotal;
                        if (!totalsByCashier.TryGetValue(rowCashier, out var cashierTotals))
                        {
                            cashierTotals = new Dictionary<string, decimal>(StringComparer.Ordinal);
                            totalsByCashier[rowCashier] = cashierTotals;
                        }

                        cashierTotals[paymentKey] = cashierTotals.GetValueOrDefault(paymentKey) + rowTotal;
                    }

                    if (taxTotalColumn >= 0 && decimal.TryParse(tableRow[taxTotalColumn]?.ToString(), out var rowTax))
                    {
                        totalTax += rowTax;
                        taxesByPaymentMethod[paymentKey] = taxesByPaymentMethod.GetValueOrDefault(paymentKey) + rowTax;
                    }
                }
                var totalSalesFromPaymentMethods = totalsByPaymentMethod.Values.Sum();

                eventArgs.Graphics.DrawLine(Pens.Black, bounds.Left, y, bounds.Right, y);
                y += 10;
                eventArgs.Graphics.DrawString("CASHIER PAYMENT TOTALS", boldFont, Brushes.Black, bounds.Left, y);
                y += 20;
                foreach (var cashierTotal in receiptCountsByCashier.OrderBy(entry => entry.Key))
                {
                    eventArgs.Graphics.DrawString($"{cashierTotal.Key} PAYMENT TOTALS", boldFont, Brushes.Black, bounds.Left, y);
                    y += 18;
                    if (totalsByCashier.TryGetValue(cashierTotal.Key, out var cashierPayments))
                    {
                        foreach (var cashierPayment in cashierPayments.OrderBy(entry => entry.Key))
                        {
                            eventArgs.Graphics.DrawString($"{cashierPayment.Key}: {cashierPayment.Value:0.00}", bodyFont, Brushes.Black, bounds.Left + 10, y);
                            y += 16;
                        }
                        var cashierSalesTotal = cashierPayments.Values.Sum();
                        eventArgs.Graphics.DrawString($"Total sales: {cashierSalesTotal:0.00}", boldFont, Brushes.Black, bounds.Left + 10, y);
                        y += 22;
                    }
                }

                eventArgs.Graphics.DrawLine(Pens.Black, bounds.Left, y, bounds.Right, y);
                y += 10;
                eventArgs.Graphics.DrawString("REPORT SUMMARY", boldFont, Brushes.Black, bounds.Left, y);
                y += 20;
                eventArgs.Graphics.DrawString($"Total receipts: {totalReceiptCount}", bodyFont, Brushes.Black, bounds.Left, y);
                y += 18;
                eventArgs.Graphics.DrawString($"Total sales (all users): {totalSalesFromPaymentMethods:0.00}", boldFont, Brushes.Black, bounds.Left, y);
                y += 22;
                eventArgs.Graphics.DrawString($"Total USD sales (all users): {totalUsdSales:0.00}", boldFont, Brushes.Black, bounds.Left, y);
                y += 22;
                eventArgs.Graphics.DrawString($"Total tax: {totalTax:0.00}", bodyFont, Brushes.Black, bounds.Left, y);
                y += 22;
                eventArgs.Graphics.DrawLine(Pens.Black, bounds.Left, y, bounds.Right, y);
                y += 10;
                eventArgs.Graphics.DrawString("TOTAL SALES BY USER", boldFont, Brushes.Black, bounds.Left, y);
                y += 20;
                foreach (var cashierTotal in totalsByCashier.OrderBy(entry => entry.Key))
                {
                    var cashierSales = cashierTotal.Value.Values.Sum();
                    eventArgs.Graphics.DrawString($"{cashierTotal.Key}: {cashierSales:0.00}", bodyFont, Brushes.Black, bounds.Left, y);
                    y += 18;
                }
                eventArgs.Graphics.DrawLine(Pens.Black, bounds.Left, y, bounds.Right, y);
                y += 10;
                eventArgs.Graphics.DrawString("PAYMENT METHOD SUMMARY", boldFont, Brushes.Black, bounds.Left, y);
                y += 20;
                foreach (var paymentTotal in totalsByPaymentMethod.OrderBy(entry => entry.Key))
                {
                    eventArgs.Graphics.DrawString($"{paymentTotal.Key}: {paymentTotal.Value:0.00}", bodyFont, Brushes.Black, bounds.Left, y);
                    y += 16;
                    if (taxesByPaymentMethod.TryGetValue(paymentTotal.Key, out var paymentTax))
                    {
                        eventArgs.Graphics.DrawString($"Tax: {paymentTax:0.00}", bodyFont, Brushes.Black, bounds.Left + 10, y);
                        y += 16;
                    }
                }
                y += 6;
                eventArgs.Graphics.DrawLine(Pens.Black, bounds.Left, y, bounds.Right, y);
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
                    if (string.Equals(detail.Label, "Products", StringComparison.OrdinalIgnoreCase))
                    {
                        eventArgs.Graphics.DrawString(label, boldFont, Brushes.Black, bounds.Left, y);
                        y += 18;
                        var productLines = (detail.Value ?? string.Empty).Split(Environment.NewLine, StringSplitOptions.None);
                        foreach (var productLine in productLines)
                        {
                            var productBounds = new RectangleF(bounds.Left + 10, y, bounds.Width - 10, 60);
                            var productSize = eventArgs.Graphics.MeasureString(productLine, bodyFont, productBounds.Size);
                            eventArgs.Graphics.DrawString(productLine, bodyFont, Brushes.Black, productBounds);
                            y += Math.Max(16, (int)Math.Ceiling(productSize.Height + 2));
                        }
                        continue;
                    }

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
