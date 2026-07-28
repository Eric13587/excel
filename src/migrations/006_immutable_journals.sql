-- 006: posted journal entries become immutable.
--
-- GLService.reverse_entry has always documented the rule — "Corrections never
-- edit or delete a posted entry; they post the inverse" — and the Journal
-- Register enforces it in the dialog. It has never been enforced anywhere a
-- statement could actually reach the table. This puts it in the database, where
-- it holds for the importer, a repair script, and anything written later.
--
-- WHAT IS IMMUTABLE, AND WHAT IS NOT
--
-- The GL holds two kinds of entry and only one of them is a statement about
-- the books:
--
--   * **authored** — 'manual', 'migration', 'reversal'. Somebody decided these.
--     They are the record, and the record does not get rewritten.
--
--   * **projected** — the six subledger sources. These are derived data: a
--     restatement of `ledger`, `savings`, `christmas_savings` and
--     `benevolent_ledger` in double-entry form. Deleting and re-deriving one is
--     not altering the books, it is recomputing a view, and it is exactly what
--     rebuild_auto_journals and the change queue do. Freezing them would freeze
--     the projection.
--
-- That asymmetry is the whole design. Immutability belongs to the authored
-- record; the projection's integrity comes from being reproducible from its
-- source, which migration 005 made cheap and src.integrity now checks.
--
-- The corollary is that a projected entry must never be *reversed* either: the
-- reversal is authored and survives, while the original is re-derived on the
-- next rebuild, so the pair stops netting to zero and the correction silently
-- doubles. The service now refuses that, and it is why these triggers do not
-- try to prevent it — a reversal is an INSERT, which no trigger here sees.
--
-- WHAT AN UPDATE IS ALLOWED TO TOUCH
--
-- Reversing an entry writes `status` on the original and `status`/`reversal_of`
-- on the new one. Those are the audit trail being recorded, not the entry being
-- changed, so the UPDATE trigger is scoped with UPDATE OF to the columns that
-- carry accounting substance and deliberately leaves the status columns free.

-- ------------------------------------------------------------------ --
-- journal_entries
-- ------------------------------------------------------------------ --
DROP TRIGGER IF EXISTS trg_journal_entry_immutable_upd;
CREATE TRIGGER trg_journal_entry_immutable_upd
BEFORE UPDATE OF entry_date, memo, source, source_ref ON journal_entries
WHEN OLD.source NOT IN ('loan_disbursement','repayment','interest',
                        'savings','christmas','benevolent')
BEGIN
    SELECT RAISE(ABORT, 'posted journal entries are immutable — post a reversal instead');
END;

DROP TRIGGER IF EXISTS trg_journal_entry_immutable_del;
CREATE TRIGGER trg_journal_entry_immutable_del
BEFORE DELETE ON journal_entries
WHEN OLD.source NOT IN ('loan_disbursement','repayment','interest',
                        'savings','christmas','benevolent')
BEGIN
    SELECT RAISE(ABORT, 'posted journal entries cannot be deleted — post a reversal instead');
END;

-- ------------------------------------------------------------------ --
-- journal_lines
--
-- A line carries no source of its own, so the rule is read from its entry. If
-- the parent is already gone the subquery yields NULL, `NULL NOT IN (...)` is
-- NULL rather than true, and the delete is allowed — which is the direction
-- that matters, because it lets an orphaned line be cleaned up while a line
-- belonging to a live authored entry stays frozen.
-- ------------------------------------------------------------------ --
DROP TRIGGER IF EXISTS trg_journal_line_immutable_upd;
CREATE TRIGGER trg_journal_line_immutable_upd
BEFORE UPDATE ON journal_lines
WHEN (SELECT e.source FROM journal_entries e WHERE e.id = OLD.entry_id)
     NOT IN ('loan_disbursement','repayment','interest',
             'savings','christmas','benevolent')
BEGIN
    SELECT RAISE(ABORT, 'posted journal lines are immutable — post a reversal instead');
END;

DROP TRIGGER IF EXISTS trg_journal_line_immutable_del;
CREATE TRIGGER trg_journal_line_immutable_del
BEFORE DELETE ON journal_lines
WHEN (SELECT e.source FROM journal_entries e WHERE e.id = OLD.entry_id)
     NOT IN ('loan_disbursement','repayment','interest',
             'savings','christmas','benevolent')
BEGIN
    SELECT RAISE(ABORT, 'posted journal lines cannot be deleted — post a reversal instead');
END;

-- NOTE for future migrations: these triggers hard-code the projected sources,
-- because a trigger cannot read a Python constant. GLService._AUTO_SOURCES is
-- the same list; a new projected source has to be added in both places, and
-- test_projected_sources_match_the_trigger fails if they drift apart.
