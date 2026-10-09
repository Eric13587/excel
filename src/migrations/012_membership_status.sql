-- 012: make membership status mean something.
--
-- `individuals` already carried `is_retired`, `retired_date` and
-- `employment_status`. Only reports consulted them, so nothing prevented a
-- monthly catch-up from generating a year of contributions for somebody who
-- left the SACCO in March. The status was decoration; src/services/policy.py
-- now enforces it, and this makes the column trustworthy enough to enforce on.
--
-- TWO COLUMNS, ONE TRUTH
--
-- `employment_status` becomes authoritative and `is_retired` becomes a derived
-- mirror, kept in step by trigger. Both are kept because `src/reports.py` reads
-- `is_retired` in five places to decide who appears on a statement, and
-- rewriting reporting logic is not what this migration is for. The trigger
-- means they cannot disagree, which is the failure mode of any two-column
-- status.
--
-- Values are constrained by trigger rather than CHECK: a CHECK needs a table
-- rebuild, and `individuals` is the parent of every other table in the journal.
-- A BEFORE trigger refuses the same writes at a fraction of the risk.

-- Anything already flagged retired says so in words.
UPDATE individuals
   SET employment_status = 'Retired'
 WHERE is_retired = 1
   AND (employment_status IS NULL OR employment_status = '' OR employment_status = 'Active');

-- Everyone else is active until told otherwise.
UPDATE individuals
   SET employment_status = 'Active'
 WHERE employment_status IS NULL OR TRIM(employment_status) = '';

-- Statuses the application understands. A value outside this set would silently
-- read as "not active" to the policy rules and quietly stop somebody's
-- contributions, so it is refused at the door instead.
DROP TRIGGER IF EXISTS trg_individuals_status_ins;
CREATE TRIGGER trg_individuals_status_ins
BEFORE INSERT ON individuals
WHEN NEW.employment_status IS NOT NULL
 AND NEW.employment_status NOT IN
     ('Active','Retired','Resigned','Terminated','Deceased','Suspended')
BEGIN
    SELECT RAISE(ABORT, 'unknown employment_status');
END;

DROP TRIGGER IF EXISTS trg_individuals_status_upd;
CREATE TRIGGER trg_individuals_status_upd
BEFORE UPDATE OF employment_status ON individuals
WHEN NEW.employment_status IS NOT NULL
 AND NEW.employment_status NOT IN
     ('Active','Retired','Resigned','Terminated','Deceased','Suspended')
BEGIN
    SELECT RAISE(ABORT, 'unknown employment_status');
END;

-- Keep the legacy flag in step with the word, in both directions, so reports
-- reading is_retired and policy reading employment_status always agree.
DROP TRIGGER IF EXISTS trg_individuals_status_mirrors_flag;
CREATE TRIGGER trg_individuals_status_mirrors_flag
AFTER UPDATE OF employment_status ON individuals
WHEN NEW.employment_status IS NOT NULL
BEGIN
    UPDATE individuals
       SET is_retired = CASE WHEN NEW.employment_status = 'Active' THEN 0 ELSE 1 END
     WHERE id = NEW.id
       AND is_retired IS NOT (CASE WHEN NEW.employment_status = 'Active' THEN 0 ELSE 1 END);
END;

DROP TRIGGER IF EXISTS trg_individuals_flag_mirrors_status;
CREATE TRIGGER trg_individuals_flag_mirrors_status
AFTER UPDATE OF is_retired ON individuals
WHEN (NEW.is_retired = 1 AND NEW.employment_status = 'Active')
  OR (NEW.is_retired = 0 AND NEW.employment_status <> 'Active')
BEGIN
    -- Only the generic word is inferable from the flag; a specific reason
    -- (Resigned, Deceased) has to be set through employment_status.
    UPDATE individuals
       SET employment_status = CASE WHEN NEW.is_retired = 1 THEN 'Retired' ELSE 'Active' END
     WHERE id = NEW.id;
END;

-- The policy rules filter by status on every mass operation.
CREATE INDEX IF NOT EXISTS idx_individuals_status ON individuals(employment_status);
