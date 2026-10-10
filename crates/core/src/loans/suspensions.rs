//! Suspensions: months a loan takes no deduction and accrues no interest.
//!
//! A span is the half-open window `[start, end)`, the end being the resume
//! date if it was resumed, else the intended end, else open-ended. A due date
//! on the end date is *not* suspended — deductions resume on it. Every reader
//! here, the derivation, statements and the grid use that one rule.

use super::service::{has_manual_edits, has_restructure_events, rebuild_schedule};
use crate::Journal;
use crate::dates;
use crate::db::model::{Suspension, event};
use crate::error::{Error, Result};

/// One loan's windows, by primary key.
pub fn loan_windows(journal: &Journal, loan_id: i64) -> Result<Vec<(String, Option<String>)>> {
    Ok(journal
        .loans()
        .suspensions_for_loan(loan_id)?
        .into_iter()
        .filter(|s| !s.start_date.is_empty())
        .map(|s| {
            let end = s.end().map(|e| dates::date_part(e).to_string());
            (dates::date_part(&s.start_date).to_string(), end)
        })
        .collect())
}

/// Does a due date fall inside a window?
pub fn date_in_suspension(date: &str, windows: &[(String, Option<String>)]) -> bool {
    let date = dates::date_part(date);
    windows
        .iter()
        .any(|(start, end)| date >= start.as_str() && end.as_deref().is_none_or(|end| date < end))
}

/// Suspend deductions from `start` (default today) until `until` (`None` is
/// open-ended). Extending a suspension already open moves only its end, so
/// the recorded span still covers the whole skipped period.
pub fn suspend(journal: &Journal, loan_id: i64, until: Option<&str>, start: Option<&str>) -> Result<()> {
    let start = start.map(str::to_string).unwrap_or_else(dates::today);
    journal.transaction(|| {
        let loan = journal.loans().get(loan_id)?.ok_or_else(|| Error::LoanNotFound(loan_id.to_string()))?;
        journal.loans().set_suspended(loan_id, until)?;
        match journal.loans().open_suspension(loan_id)? {
            Some(open) => journal.loans().extend_suspension(open.id, until)?,
            None => {
                journal.loans().add_suspension(
                    loan_id,
                    loan.individual_id,
                    &loan.reference,
                    &start,
                    until,
                    None,
                    "active",
                )?;
            }
        }
        Ok(())
    })
}

/// End a suspension: deductions resume from `resumed` (default today).
pub fn resume(journal: &Journal, loan_id: i64, resumed: Option<&str>) -> Result<()> {
    let resumed = resumed.map(str::to_string).unwrap_or_else(dates::today);
    journal.transaction(|| {
        journal.loans().clear_suspended(loan_id)?;
        journal.loans().close_open_suspension(loan_id, &resumed)
    })
}

/// Whether a recorded span is in force now or already over.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Recorded {
    /// Ended on or before today: stored as a closed record; the loan's live
    /// flag is untouched because the loan is not suspended now.
    Historical,
    /// Running now (or from a date to come): the loan is suspended live.
    Active,
}

/// Record a suspension anywhere in time — past, present or future.
pub fn record(journal: &Journal, loan_id: i64, start: &str, end: Option<&str>, today: Option<&str>) -> Result<Recorded> {
    let today = today.map(str::to_string).unwrap_or_else(dates::today);
    if let Some(end) = end.filter(|e| *e <= today.as_str()) {
        journal.transaction(|| {
            let loan = journal.loans().get(loan_id)?.ok_or_else(|| Error::LoanNotFound(loan_id.to_string()))?;
            journal.loans().add_suspension(
                loan_id,
                loan.individual_id,
                &loan.reference,
                start,
                Some(end),
                Some(end),
                "resumed",
            )?;
            Ok(())
        })?;
        return Ok(Recorded::Historical);
    }
    suspend(journal, loan_id, end, Some(start))?;
    Ok(Recorded::Active)
}

/// Remove a span entirely: it never applied, so catch-up will deduct those
/// months again. Clears the loan's flag if it was the live span.
pub fn delete(journal: &Journal, suspension_id: i64) -> Result<Option<Suspension>> {
    let Some(span) = journal.loans().suspension(suspension_id)? else { return Ok(None) };
    journal.transaction(|| {
        let loan_id = span.loan_id.unwrap_or_default();
        let was_open = journal.loans().open_suspension(loan_id)?.is_some_and(|s| s.id == suspension_id);
        journal.loans().delete_suspension(suspension_id)?;
        if was_open {
            journal.loans().clear_suspended(loan_id)?;
        }
        Ok(())
    })?;
    Ok(Some(span))
}

/// Delete a span, optionally re-deducting the months it skipped (`rebuild`).
/// Without a rebuild the skipped months stay skipped; with one, the schedule
/// is re-derived from issue so they appear. Returns the span and how many
/// deductions the rebuild created.
pub fn remove(
    journal: &Journal,
    member_id: i64,
    reference: &str,
    suspension_id: i64,
    rebuild: bool,
) -> Result<(Option<Suspension>, usize)> {
    if journal.loans().suspension(suspension_id)?.is_none() {
        return Ok((None, 0));
    }
    if rebuild && has_restructure_events(journal, member_id, reference)? {
        return Err(Error::invalid(
            "This loan has top-ups or buy-offs, so its schedule cannot be rebuilt automatically. \
             Remove the suspension without rebuilding, then adjust the affected months by hand.",
        ));
    }
    if rebuild && has_manual_edits(journal, member_id, reference)? {
        return Err(Error::invalid(
            "This loan has hand-edited entries, which a rebuild would replace with the scheduled \
             amounts. Remove the suspension without rebuilding, then adjust the affected months by hand.",
        ));
    }
    journal.transaction(|| {
        let span = delete(journal, suspension_id)?;
        let created = if rebuild { rebuild_schedule(journal, member_id, reference, None)? } else { 0 };
        Ok((span, created))
    })
}

/// Deductions already recorded within a period — a suspension recorded over
/// them would contradict them.
pub fn deductions_in_period(journal: &Journal, member_id: i64, reference: &str, start: &str, end: Option<&str>) -> Result<i64> {
    journal
        .ledger()
        .count_events(member_id, reference, &[event::REPAYMENT, event::BUYOFF], Some(start), end)
}
