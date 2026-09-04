# DELETION TESTING KIT - Complete Guide

## 📦 What You Have

You now have **4 diagnostic tools** to find why invoices aren't being deleted:

### 1. **QUICK_DELETE_TEST.sql** 
   - Fast diagnostic to check if deletion will work
   - Shows if data exists and which strategy works
   - **Start here first**

### 2. **TEST_DELETION_QUERIES.sql**
   - Comprehensive test with all 3 deletion strategies
   - Shows data types and format issues
   - Tests actual deletion with safe ROLLBACK

### 3. **DELETION_DEBUG_GUIDE.md**
   - Complete troubleshooting guide
   - Common issues and solutions
   - How to read debug logs

### 4. **RUN_DELETION_TEST.bat**
   - Batch script to run SQL tests automatically
   - Easier than manual SSMS queries

### 5. **Updated POSViewer Application**
   - Enhanced with detailed DEBUG logging
   - Shows exact SQL queries being executed
   - Shows @@ROWCOUNT (how many rows deleted)
   - Shows remaining rows after deletion

---

## 🚀 Quick Start

### Option A: Use the Batch Script (Easiest)
```
1. Double-click RUN_DELETION_TEST.bat
2. Enter your SQL server name
3. Enter your database name
4. Enter an invoice number to test
5. Enter the branch name
6. Script will show if deletion will work
```

### Option B: Use SQL Management Studio (More Details)
```
1. Open SSMS
2. Connect to your branch database
3. Open QUICK_DELETE_TEST.sql
4. Replace the test values at the top
5. Run the script
6. Look for ✓ or ✗ results
```

### Option C: Full Diagnostic
```
1. Run QUICK_DELETE_TEST.sql first
2. If it shows issues, run TEST_DELETION_QUERIES.sql
3. Check DELETION_DEBUG_GUIDE.md for solutions
```

---

## 🎯 What to Look For

When you run the test, you should see ONE of these results:

### ✅ SUCCESS
```
✓ DELETION WILL WORK - 5 rows will be deleted

Records that will be deleted:
InvoiceNum | Branch   | ProductID | EntryNo | Qty
-----------|----------|-----------|---------|----
123        | Store-A  | 5         | 1       | 2
123        | Store-A  | 6         | 2       | 1
```

**What to do:** Deletion logic is correct. Check application logs.

---

### ❌ FAILURE - Branch Not Found
```
✗ Branch does NOT exist

Available branches:
Branch
-------
BRANCH_A
BRANCH_B
STORE_MAIN
```

**What to do:** Make sure branch name matches EXACTLY (case, spaces, special characters)

---

### ❌ FAILURE - Invoice Not Found
```
✗ Invoice does NOT exist anywhere
```

**What to do:** 
- Check if invoice was already deleted
- Check invoice number format (might be missing leading zeros)
- Verify invoice exists in the database

---

### ❌ FAILURE - Data Type Issues
```
Strategy 1 (EXACT): NO MATCH
Strategy 2 (CAST): NO MATCH  
Strategy 3 (CASE-INSENSITIVE): MATCH FOUND - 5 rows
```

**What to do:** Only Strategy 3 works. Application will use it.

---

## 🔧 Application Debug Mode

Run the updated POSViewer application. When you cancel an invoice:

### Watch the Sync Dashboard for these messages:

```
[14:35:22] DEBUG: DeleteAndConfirmAsync called
[14:35:22] DEBUG: Invoice=123 | Product=5 | Branch=Store-A | Entry=1
[14:35:22] DEBUG: SQL Query:
          DELETE FROM [dbo].[Movement]
          WHERE (InvoiceNum = @invoiceNum OR CAST(InvoiceNum AS nvarchar(50)) = @invoiceNum)
            AND (Branch = @branchName OR UPPER(CAST(Branch AS nvarchar(100))) = UPPER(@branchName))
            AND ProductID = @productId;
[14:35:22] DEBUG: Executing DELETE...
[14:35:22] DEBUG: @@ROWCOUNT = 5           ← THIS IS KEY!
[14:35:22] ✓ DELETED: 5 row(s) | Confirmed to API
```

### Understanding @@ROWCOUNT:
- **0** = No rows matched WHERE clause (data type mismatch or field mismatch)
- **> 0** = Rows were successfully deleted

---

## 📊 Comparison: SQL Test vs Application

| Component | SQL Test | App Debug Log |
|-----------|----------|---------------|
| Shows query | YES | YES (DEBUG log) |
| Shows parameters | YES | YES (DEBUG log) |
| Shows matches | YES | YES (Before/After count) |
| Shows deleted rows | YES | YES (@@ROWCOUNT) |
| Actually deletes | NO (ROLLBACK) | YES |

---

## ⚙️ Troubleshooting Workflow

```
1. Does SQL test find rows?
   ├─ YES → Go to step 2
   └─ NO → Check branch name and invoice number

2. Does SQL DELETE work in test?
   ├─ YES → Go to step 3
   └─ NO → Check data types (see DELETION_DEBUG_GUIDE.md)

3. Does application show @@ROWCOUNT > 0?
   ├─ YES → Deletion is working! ✓
   └─ NO → Check application logs for errors

4. Does API confirm deletion?
   ├─ YES → Everything works! ✓
   └─ NO → Check API connection
```

---

## 📋 Required Information to Debug

When asking for help, provide:

1. **SQL Test Results**
   - Output from QUICK_DELETE_TEST.sql
   - Did it show ✓ or ✗?

2. **Test Data**
   - Invoice number you tested
   - Branch name you tested
   - Did it match database?

3. **Application Logs**
   - Debug messages from Sync Dashboard
   - Value of @@ROWCOUNT
   - Any ERROR messages

4. **Database Info**
   - SQL Server version
   - How many rows are in Movement table?
   - What data type is InvoiceNum column?

---

## 🎓 Understanding Deletion Failures

### Most Common Reason: Data Format Mismatch

**Example:**
- Application sends: `InvoiceNum = "123"` (string)
- Database has: `InvoiceNum = 123` (integer)
- Direct comparison fails: `"123" = 123` is FALSE

**Solution:**
- Use CAST: `CAST(InvoiceNum AS nvarchar(50)) = "123"` → TRUE
- Application already does this

### Second Most Common: Branch Name Case Sensitivity

**Example:**
- Application sends: `Branch = "store-a"` (lowercase)
- Database has: `Branch = "Store-A"` (mixed case)
- Comparison fails: `"store-a" = "Store-A"` is FALSE

**Solution:**
- Use UPPER: `UPPER(Branch) = UPPER("store-a")` → TRUE
- Application already does this

### Third Most Common: Whitespace/Hidden Characters

**Example:**
- Application sends: `Branch = "Store-A "` (has trailing space)
- Database has: `Branch = "Store-A"` (no space)
- Comparison fails

**Solution:**
- SQL Test script checks for this
- Use TRIM() if needed

---

## 🚨 If Nothing Works

1. Run **TEST_DELETION_QUERIES.sql** with ALL strategies
2. Check **DELETION_DEBUG_GUIDE.md** for your symptom
3. Collect all DEBUG logs from application
4. Check if you have **UPDATE permissions** on Movement table
5. Check if table has **triggers** that prevent deletion

---

## ✅ Success Indicators

You'll know deletion is fixed when:

- [ ] SQL test shows `✓ DELETION WILL WORK`
- [ ] Application shows `@@ROWCOUNT = X` (not 0)
- [ ] Sync Dashboard shows `✓ DELETED: X row(s) | Confirmed to API`
- [ ] Database shows row count decreased
- [ ] Same invoice number doesn't reappear

---

## 📞 Support

If you get stuck:

1. Share output from **QUICK_DELETE_TEST.sql**
2. Share DEBUG logs from **Sync Dashboard**
3. Tell me exact invoice number and branch name you tested
4. Tell me if deletion worked in SQL but not in application (or vice versa)

This will help pinpoint the exact issue immediately.
