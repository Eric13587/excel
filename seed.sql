-- =============================================================================
-- LoanMaster — reference / default data
-- =============================================================================
-- Everything here is real default data the application already writes on its
-- own; no sample members, loans or transactions (real member data must never
-- be committed — see CLAUDE.md).
--
--   1. chart_of_accounts  — seeded verbatim by create_tables() from
--                           src/config.py DEFAULT_CHART_OF_ACCOUNTS. Codes
--                           follow the 1000/2000/... banding so reports can
--                           group by leading digit. Well-known codes are
--                           referenced by GLService's auto-posting engine, so
--                           they must not be renumbered.
--   2. settings           — the fallback values currently hard-coded as the
--                           second argument of db.get_setting(...) call sites.
--                           Seeding them makes the effective configuration
--                           visible in the journal instead of implicit.
--
-- Both sections are idempotent (INSERT OR IGNORE), so this file is safe to
-- re-run against an existing journal.
--
-- Apply with:  sqlite3 loan_master.db < seed.sql
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Standard SACCO chart of accounts
--    normal_balance = the side that INCREASES the account. Contra accounts
--    (1190 Allowance for Loan Losses) deliberately carry the opposite of their
--    section, which is what makes them net against it on the balance sheet.
-- -----------------------------------------------------------------------------

-- Assets (1000s)
INSERT OR IGNORE INTO chart_of_accounts (code, name, type, normal_balance) VALUES
    ('1000', 'Cash at Bank',                'Asset',     'debit'),
    ('1010', 'Cash on Hand',                'Asset',     'debit'),
    ('1100', 'Loans Receivable',            'Asset',     'debit'),
    ('1190', 'Allowance for Loan Losses',   'Asset',     'credit'),  -- contra-asset
    ('1200', 'Interest Receivable',         'Asset',     'debit');

-- Liabilities (2000s)
INSERT OR IGNORE INTO chart_of_accounts (code, name, type, normal_balance) VALUES
    ('2000', 'Member Deposits / Savings',   'Liability', 'credit'),
    ('2010', 'Christmas Savings',           'Liability', 'credit'),
    ('2100', 'External Borrowings',         'Liability', 'credit'),
    ('2200', 'Accounts Payable',            'Liability', 'credit'),
    ('2300', 'Benevolent Fund',             'Liability', 'credit');

-- Equity (3000s)
INSERT OR IGNORE INTO chart_of_accounts (code, name, type, normal_balance) VALUES
    ('3000', 'Member Share Capital',        'Equity',    'credit'),
    ('3100', 'Retained Earnings',           'Equity',    'credit'),
    ('3900', 'Opening Balance Equity',      'Equity',    'credit');  -- migration suspense

-- Income (4000s)
INSERT OR IGNORE INTO chart_of_accounts (code, name, type, normal_balance) VALUES
    ('4000', 'Loan Interest Income',        'Income',    'credit'),
    ('4100', 'Fees & Fines Income',         'Income',    'credit'),
    ('4200', 'Bank Interest Income',        'Income',    'credit'),
    ('4900', 'Other Income',                'Income',    'credit');

-- Expenses (5000s)
INSERT OR IGNORE INTO chart_of_accounts (code, name, type, normal_balance) VALUES
    ('5000', 'Staff Salaries',              'Expense',   'debit'),
    ('5100', 'Office Rent',                 'Expense',   'debit'),
    ('5200', 'Utility Bills',               'Expense',   'debit'),
    ('5300', 'Stationery / Supplies',       'Expense',   'debit'),
    ('5400', 'Bank Fees / Charges',         'Expense',   'debit'),
    ('5500', 'Marketing / PR',              'Expense',   'debit'),
    ('5600', 'Loan Loss Provision',         'Expense',   'debit'),
    ('5900', 'Other Expenses',              'Expense',   'debit');

-- -----------------------------------------------------------------------------
-- 2. Default settings
--    Applied to every journal by migration 002_seed_default_settings.sql, which
--    is the authority. Kept here so a hand-built journal gets them too; the two
--    lists must stay identical.
-- -----------------------------------------------------------------------------
INSERT OR IGNORE INTO settings (key, value) VALUES
    ('org_name',                    ''),          -- branding: organisation name
    ('org_logo_path',               ''),          -- branding: falls back to bundled logo
    ('app_theme',                   'Light'),     -- 'Light' | 'Dark'
    ('fy_start_month',              'January'),   -- fiscal-year start month name
    ('deduct_same_month',           'false'),     -- first deduction in the issue month?
    ('christmas_unlock_month',      '12'),        -- month withdrawals are permitted
    ('default_savings_increment',   '2500'),      -- fallback monthly savings amount
    ('default_christmas_increment', '0'),         -- fallback monthly Christmas amount
    ('excel_header_bg',             '#E2E8F0'),   -- slate-200, Excel column headers
    ('excel_total_bg',              '#F1F5F9');   -- slate-100, Excel total rows

COMMIT;
