-- 002_seed_default_settings
--
-- Make every configurable value an explicit row rather than a fallback buried
-- in code.
--
-- Until now a journal that had never had a key written fell back to a default
-- supplied at the call site, and those defaults had already diverged:
-- fy_start_month resolved to 'November' in the Settings dialog and 'January'
-- in reports.py, so the UI and the books disagreed on the financial year
-- (finding S1). Phase 2a consolidated the defaults into one table in
-- src/db/settings.py; this writes them into the journal so the effective
-- configuration is visible, auditable and editable in the Settings dialog
-- instead of implied.
--
-- NOTE ON THE fy_start_month VALUE
-- This file originally seeded 'January' — the value reports.py fell back to —
-- so that adopting it changed no existing figure. It was then changed to
-- 'November', the institution's actual financial year and what the production
-- journal already stores explicitly.
--
-- Editing an already-applied migration is normally wrong. It is safe here and
-- only here: this migration has never been applied to anything but the
-- production journal, where fy_start_month was already 'November' so the
-- INSERT OR IGNORE was a no-op, and throwaway test databases. Any future
-- correction to a seeded value must be a new migration.

-- Behaviour-preserving by construction: INSERT OR IGNORE only fills keys that
-- are absent, and every value written equals the default that already applied.
-- The production journal already stores fy_start_month = 'November' and is
-- untouched by this.

INSERT OR IGNORE INTO settings (key, value) VALUES
    ('org_name',                    ''),          -- branding: organisation name
    ('org_logo_path',               ''),          -- branding: falls back to the bundled logo
    ('app_theme',                   'Light'),     -- 'Light' | 'Dark'
    ('fy_start_month',              'November'),  -- financial-year start month name
    ('deduct_same_month',           'false'),     -- first deduction in the month of issue?
    ('christmas_unlock_month',      '12'),        -- month Christmas withdrawals are permitted
    ('default_savings_increment',   '2500'),      -- fallback monthly savings amount
    ('default_christmas_increment', '0'),         -- fallback monthly Christmas amount
    ('default_interest_rate',       '0.15'),      -- flat rate loans are grossed-up by for display
    ('excel_header_bg',             '#E2E8F0'),   -- slate-200, Excel column headers
    ('excel_total_bg',              '#F1F5F9');   -- slate-100, Excel total rows
