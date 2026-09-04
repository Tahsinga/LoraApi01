# DELETION DEBUGGING GUIDE
## Why Invoices Are Not Being Deleted

---

## 📋 STEP 1: Run the SQL Diagnostics

### Use QUICK_DELETE_TEST.sql to find the issue:

1. Open SQL Server Management Studio (SSMS)
2. Connect to your **Branch Database**
3. Open the file: `QUICK_DELETE_TEST.sql`
4. **Replace these values** in the script:
   ```sql
   DECLARE @TestInvoice NVARCHAR(50) = 'PASTE_INVOICE_NUMBER_HERE';
   DECLARE @TestBranch NVARCHAR(100) = 'PASTE_BRANCH_HERE';
   ```

5. Run the script and look at the output

### 🎯 Expected Results:

**If deletion works:**
- You'll see: `✓ DELETION WILL WORK - X rows will be deleted`
- The query will show exactly what rows match

**If deletion fails:**
- You'll see: `✗ Branch does NOT exist` OR other diagnostics
- This tells you exactly why the DELETE isn't matching

---

## 🔍 STEP 2: Test With Comprehensive Diagnostics

### Run TEST_DELETION_QUERIES.sql:

This tests all 3 deletion strategies:

1. **Strategy 1**: EXACT STRING MATCH
   - `WHERE InvoiceNum = @Invoice AND Branch = @Branch`
   
2. **Strategy 2**: CAST STRING MATCH  
   - `WHERE CAST(InvoiceNum AS nvarchar(50)) = @Invoice AND CAST(Branch AS nvarchar(100)) = @Branch`
   
3. **Strategy 3**: CASE-INSENSITIVE MATCH
   - `WHERE UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@Branch)`

The script tests all three to see which one will actually work.

---

## 🧪 STEP 3: Test Actual Deletion (SAFE - Uses Rollback)

The script includes:

```sql
BEGIN TRANSACTION;
  -- Deletion happens here
ROLLBACK TRANSACTION; -- This undoes the deletion so you can test again
```

**To actually delete after confirming it works:**
- Change `ROLLBACK` to `COMMIT`
- Re-run the query

---

## 📊 STEP 4: Enable Debug Logging in Application

The updated application now logs:

```
[14:35:22] DEBUG: DeleteAndConfirmAsync called
[14:35:22] DEBUG: Invoice=123 | Product=5 | Branch=Store-A | Entry=1
[14:35:22] DEBUG: SQL Query:
          DELETE FROM [dbo].[Movement]
          WHERE (InvoiceNum = @invoiceNum OR CAST(InvoiceNum AS nvarchar(50)) = @invoiceNum)
            AND (Branch = @branchName OR UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@branchName))
            AND ProductID = @productId;
[14:35:22] DEBUG: Executing DELETE...
[14:35:22] DEBUG: @@ROWCOUNT = 5
[14:35:22] DEBUG: Remaining records after delete: 0
```

**Look for `@@ROWCOUNT`:**
- If it shows **0** = No rows were deleted (data mismatch)
- If it shows **> 0** = Rows were successfully deleted

---

## ❌ Common Deletion Failures & Solutions

### Issue 1: Branch Name Doesn't Match
**Symptom:** 
```
✗ Branch does NOT exist
```

**Solution:**
- Run: `SELECT DISTINCT Branch FROM [dbo].[Movement];`
- Use exact branch name (check case, spaces, special characters)
- Branch in API call must EXACTLY match database

---

### Issue 2: Invoice Number Format Mismatch
**Symptom:**
```
Remaining records after delete: 5
@@ROWCOUNT = 0
```

**Solution:**
- Check if InvoiceNum is **integer** or **string** in database
- Run: `SELECT SQL_VARIANT_PROPERTY(InvoiceNum, 'BaseType') FROM [dbo].[Movement];`
- If mixed types, application handles it with CAST

---

### Issue 3: ProductID Doesn't Match
**Symptom:**
```
✗ ProductID does NOT exist for that invoice
```

**Solution:**
- Verify ProductID in the data grid matches database
- Run query to see actual ProductIDs:
  ```sql
  SELECT DISTINCT ProductID FROM [dbo].[Movement] WHERE InvoiceNum = '123';
  ```

---

### Issue 4: EntryNo (Line Number) Issues
**Symptom:**
```
Multiple entries found, only deleting one
```

**Solution:**
- EntryNo might be optional
- If invoice has 5 line items, must delete each individually
- Application now handles this with **entry_no** parameter

---

## ✅ VERIFICATION CHECKLIST

Before declaring deletion fixed, verify:

- [ ] SQL SELECT query returns matching rows
- [ ] SQL DELETE query in QUICK_DELETE_TEST.sql actually deletes rows
- [ ] Application shows `@@ROWCOUNT = X` (not 0)
- [ ] Application shows `Remaining records after delete: 0`
- [ ] API received deletion trigger (check sync dashboard)
- [ ] API confirmed deletion (check sync dashboard logs)

---

## 📝 STEP 5: Collect Logs for Troubleshooting

When testing:

1. **From Sync Dashboard**, copy all DEBUG messages:
   ```
   [14:35:22] DEBUG: ...
   [14:35:22] DEBUG: ...
   ```

2. **From SQL Query**, capture the results showing:
   - Data found: YES/NO
   - Match count: X
   - Strategies tried: 1, 2, 3 (which one worked?)

3. **Share with support:**
   - Debug logs from application
   - SQL query results
   - Exact invoice number tested
   - Exact branch name
   - Error messages if any

---

## 🔧 ADVANCED DEBUGGING

### Check if Deletion is Actually Happening But Not Showing

Run this query on the database:

```sql
-- Show what gets deleted each time app runs
SELECT TOP 20 
    CONVERT(NVARCHAR(50), GETDATE(), 120) AS CheckTime,
    COUNT(*) AS CurrentRowCount
FROM [dbo].[Movement]
GROUP BY CONVERT(DATE, GETDATE())
ORDER BY CheckTime DESC;
```

Run this **before** and **after** attempting a deletion in the app.
If row count decreases, deletion IS working!

---

## 📞 NEXT STEPS

1. ✅ Run QUICK_DELETE_TEST.sql with your test invoice
2. ✅ Note the result (works or doesn't work)
3. ✅ If doesn't work, run TEST_DELETION_QUERIES.sql
4. ✅ Check application sync dashboard for debug messages
5. ✅ Compare SQL results with app behavior

Once you identify WHICH strategy works (1, 2, or 3), 
I can make the application use ONLY that strategy.

---

**Need Help?**
- Share the output from QUICK_DELETE_TEST.sql
- Share the DEBUG messages from the sync dashboard
- Tell me: Did SELECT find rows? Did DELETE delete them?
