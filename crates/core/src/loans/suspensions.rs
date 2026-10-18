//! Suspensions: months a loan takes no deduction and accrues no interest.
//!
//! A span is the half-open window `[start, end)`, the end being the resume
//! date if it was resumed, else the intended end, else open-ended. A due date
//! on the end date is *not* suspended — deductions resume on it. Every reader
//! here, the derivation, statements and the grid use that one rule.
//!
//! A loan's history has to explain itself: a suspension is not recorded over
//! months that hold deductions or interest charges, and one that has skipped
//! months is not removed without deducting them — either would leave months
//! the schedule cannot account for. [`record_and_clear`] records one over
//! such months by removing what it contradicts; [`remove`] with a rebuild
//! deducts the months a removed one skipped.

use std::collections::HashSet;

use super::deletions::resplit_after;
use super::guards;
use super::projection::project_loan;
use super::service::{has_manual_edits, has_restructure_events, rebuild_schedule, recalculate_default_deduction};
use crate::Journal;
use crate::dates;
use crate::db::model::{LedgerRow, Suspension, event};
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
    let start = start.map(str::to_string).unwrap_or_else(|| dates::format(journal.today()));
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
    let resumed = resumed.map(str::to_string).unwrap_or_else(|| dates::format(journal.today()));
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

/// Record a suspension anywhere in time — past, present or future. Refused
/// over months that already hold deductions or interest charges:
/// [`record_and_clear`] records such a suspension by removing them.
pub fn record(journal: &Journal, loan_id: i64, start: &str, end: Option<&str>, today: Option<&str>) -> Result<Recorded> {
    let loan = journal.loans().get(loan_id)?.ok_or_else(|| Error::LoanNotFound(loan_id.to_string()))?;
    let taken = taken_in_period(journal, loan.individual_id, &loan.reference, start, end)?;
    if !taken.is_empty() {
        return Err(Error::NotAllowed(vec![format!(
            "{} has {taken} {}. A suspended month takes no deduction and charges no interest: remove them as \
             the suspension is recorded, or suspend from after them.",
            loan.reference,
            window_words(start, end)
        )]));
    }
    journal.transaction(|| {
        let sound = guards::soundness(journal, loan.individual_id, &loan.reference)?;
        let recorded = record_over(journal, loan_id, start, end, today)?;
        project_loan(journal, loan.individual_id, &loan.reference)?;
        guards::keep_sound(journal, loan.individual_id, &loan.reference, &sound)?;
        recalculate_default_deduction(journal, loan.individual_id)?;
        Ok(recorded)
    })
}

/// Record a suspension over months that already hold entries, removing what
/// it contradicts: the deductions in those months, and the interest charged
/// in them, which goes back to what the loan has still to charge. Every other
/// entry stays; the deductions after it settle interest first again, and the
/// term runs on by the months suspended. Refused over months a buy-off was
/// worked out from. Returns how many deductions went.
pub fn record_and_clear(journal: &Journal, loan_id: i64, start: &str, end: Option<&str>) -> Result<(Recorded, usize)> {
    let loan = journal.loans().get(loan_id)?.ok_or_else(|| Error::LoanNotFound(loan_id.to_string()))?;
    let (member, reference) = (loan.individual_id, loan.reference.as_str());
    journal.transaction(|| {
        let rows = journal.ledger().for_loan(member, reference)?;
        let inside: Vec<&LedgerRow> = in_window(&rows, start, end).collect();
        // A buy-off was worked out from the history before it: months taken
        // out of that history would leave it settling the wrong figure.
        let buyoff = rows.iter().find(|r| r.event_type == event::BUYOFF && dates::date_part(&r.date) >= dates::date_part(start));
        if let (Some(buyoff), false) = (buyoff, inside.is_empty()) {
            return Err(Error::NotAllowed(vec![format!(
                "{reference} was bought off on {}, for a figure worked out from a history that includes those \
                 months. Reverse the buy-off in the member's ledger first (Delete Last restores the loan).",
                dates::date_part(&buyoff.date)
            )]));
        }
        let sound = guards::soundness(journal, member, reference)?;
        if let Some(first) = inside.first() {
            resplit_after(journal, member, reference, &first.date, 0)?;
        }
        let mut removed = 0;
        for row in &inside {
            if row.event_type == event::INTEREST {
                journal.loans().adjust_unearned(loan.id, row.added)?;
            } else {
                removed += 1;
            }
            journal.ledger().delete(row.id)?;
        }
        let recorded = record_over(journal, loan_id, start, end, None)?;
        project_loan(journal, member, reference)?;
        guards::keep_sound(journal, member, reference, &sound)?;
        // A loan its last deductions paid off owes again without them.
        recalculate_default_deduction(journal, member)?;
        Ok((recorded, removed))
    })
}

fn record_over(journal: &Journal, loan_id: i64, start: &str, end: Option<&str>, today: Option<&str>) -> Result<Recorded> {
    let today = today.map(str::to_string).unwrap_or_else(|| dates::format(journal.today()));
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
/// Without a rebuild only a span that has skipped nothing yet goes — one that
/// has would leave months nothing explains; with one, the schedule is
/// re-derived from issue so they are deducted. Returns the span and how many
/// deductions the rebuild created.
pub fn remove(
    journal: &Journal,
    member_id: i64,
    reference: &str,
    suspension_id: i64,
    rebuild: bool,
) -> Result<(Option<Suspension>, usize)> {
    let Some(span) = journal.loans().suspension(suspension_id)? else { return Ok((None, 0)) };
    if !rebuild && let Some(why) = skipped(journal, member_id, reference, &span)? {
        return Err(Error::NotAllowed(vec![why]));
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

/// Why removing a span on its own would leave months unexplained, or
/// `None`: it has skipped months the loan has since moved past.
pub fn skipped(journal: &Journal, member_id: i64, reference: &str, span: &Suspension) -> Result<Option<String>> {
    let Some(loan) = journal.loans().by_ref(member_id, reference)? else { return Ok(None) };
    let start = dates::date_part(&span.start_date);
    if !loan.is_active() {
        return Ok(Some(format!(
            "{reference} is {}, and the suspension from {start} is part of how it was repaid; it stays.",
            loan.status.to_lowercase()
        )));
    }
    if dates::month_key(start) >= dates::month_key(loan.due()) {
        return Ok(None);
    }
    Ok(Some(format!(
        "The suspension from {start} has already skipped months of {reference}, and nothing else accounts for \
         them. Remove it with Deduct Them Now (the schedule is rebuilt), or keep it."
    )))
}

/// What a period of a loan already holds that a suspension over it would
/// contradict: deductions (a buy-off among them), and months charged
/// interest with nothing paid — missed months. A suspended month has neither.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Taken {
    pub deductions: usize,
    pub missed: usize,
}

impl Taken {
    pub fn is_empty(&self) -> bool {
        self.deductions == 0 && self.missed == 0
    }
}

impl std::fmt::Display for Taken {
    /// "3 deductions", "1 missed month", "2 deductions and 1 missed month".
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let count = |n: usize, one: &str, many: &str| match n {
            0 => None,
            1 => Some(format!("1 {one}")),
            n => Some(format!("{n} {many}")),
        };
        let parts: Vec<String> =
            [count(self.deductions, "deduction", "deductions"), count(self.missed, "missed month", "missed months")]
                .into_iter()
                .flatten()
                .collect();
        f.write_str(&parts.join(" and "))
    }
}

/// A loan's deductions and interest charges inside the window `[start, end)`.
fn in_window<'a>(rows: &'a [LedgerRow], start: &str, end: Option<&str>) -> impl Iterator<Item = &'a LedgerRow> {
    let window = [(dates::date_part(start).to_string(), end.map(|e| dates::date_part(e).to_string()))];
    rows.iter().filter(move |r| {
        [event::REPAYMENT, event::BUYOFF, event::INTEREST].contains(&r.event_type.as_str()) && date_in_suspension(&r.date, &window)
    })
}

/// "between 2026-05-01 and 2026-08-01", or "from 2026-05-01 on".
pub fn window_words(start: &str, end: Option<&str>) -> String {
    match end {
        Some(end) => format!("between {} and {}", dates::date_part(start), dates::date_part(end)),
        None => format!("from {} on", dates::date_part(start)),
    }
}

/// What a period already holds that a suspension over it would contradict.
pub fn taken_in_period(journal: &Journal, member_id: i64, reference: &str, start: &str, end: Option<&str>) -> Result<Taken> {
    let rows = journal.ledger().for_loan(member_id, reference)?;
    let (mut paid, mut charged, mut deductions) = (HashSet::new(), HashSet::new(), 0);
    for row in in_window(&rows, start, end) {
        let month = dates::month_key(&row.date);
        if row.event_type == event::INTEREST {
            charged.insert(month);
        } else {
            deductions += 1;
            paid.insert(month);
        }
    }
    Ok(Taken { deductions, missed: charged.difference(&paid).count() })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::members::MemberDetails;
    use crate::loans::projection::reconcile_problem;
    use crate::loans::{edits, service};

    /// 120,000 over 12 months at 15%, deducted February to September 2026,
    /// with a 30,000 top-up in April if asked for.
    fn deducted_loan(top_up: bool) -> (Journal, i64, i64) {
        let journal = Journal::open_in_memory().unwrap();
        let ann = journal.members().add(&MemberDetails { name: "Ann".into(), ..MemberDetails::default() }).unwrap();
        journal.set_today(dates::parse("2026-10-18").unwrap());
        service::issue(&journal, ann, 120_000.0, 12, "2026-01-28", Some(0.15)).unwrap();
        service::catch_up(&journal, ann, "L-001", Some("2026-03-30")).unwrap();
        if top_up {
            service::top_up(&journal, ann, "L-001", 30_000.0, 12, Some("2026-04-28")).unwrap();
        }
        service::catch_up(&journal, ann, "L-001", Some("2026-09-30")).unwrap();
        let loan = journal.loans().by_ref(ann, "L-001").unwrap().unwrap().id;
        (journal, ann, loan)
    }

    fn refusal(result: Result<impl std::fmt::Debug>) -> String {
        match result {
            Err(Error::NotAllowed(why)) => why.join(" "),
            other => panic!("expected a refusal, got {other:?}"),
        }
    }

    #[test]
    fn a_suspension_is_not_recorded_over_deductions_or_missed_months() {
        let (journal, ann, loan) = deducted_loan(false);
        let why = refusal(record(&journal, loan, "2026-06-01", Some("2026-08-01"), None));
        assert!(why.contains("L-001 has 2 deductions between 2026-06-01 and 2026-08-01"), "{why}");
        // A missed month is charged with nothing paid: a suspension contradicts it too.
        let may = journal.ledger().for_loan(ann, "L-001").unwrap();
        let may = may.iter().find(|r| r.event_type == event::REPAYMENT && r.date.starts_with("2026-05")).unwrap();
        edits::delete_entry(&journal, ann, may.id).unwrap();
        let why = refusal(record(&journal, loan, "2026-05-01", Some("2026-06-01"), None));
        assert!(why.contains("1 missed month"), "{why}");
        assert!(journal.loans().suspensions_for_loan(loan).unwrap().is_empty());
        // Deductions resume on the end date: one there is no conflict.
        let taken = taken_in_period(&journal, ann, "L-001", "2026-09-29", Some("2026-10-28")).unwrap();
        assert!(taken.is_empty());
        assert_eq!(taken_in_period(&journal, ann, "L-001", "2026-08-01", Some("2026-09-28")).unwrap().deductions, 1);
    }

    #[test]
    fn recording_over_deductions_removes_them_and_keeps_the_loan_whole() {
        // A topped-up loan cannot be rebuilt; clearing works on it all the same.
        let (journal, ann, loan) = deducted_loan(true);
        let before = journal.loans().get(loan).unwrap().unwrap();
        let (recorded, removed) = record_and_clear(&journal, loan, "2026-06-01", Some("2026-08-01")).unwrap();
        assert_eq!((recorded, removed), (Recorded::Historical, 2));
        let rows = journal.ledger().for_loan(ann, "L-001").unwrap();
        assert!(!rows.iter().any(|r| r.date.starts_with("2026-06") || r.date.starts_with("2026-07")));
        assert_eq!(reconcile_problem(&journal, ann, "L-001").unwrap(), None);
        assert!(guards::unexplained_months(&journal, ann, "L-001").unwrap().is_empty());
        let after = journal.loans().get(loan).unwrap().unwrap();
        // Two months' interest goes back to what the loan has still to charge,
        // and the principal those deductions repaid is owed again.
        assert_eq!(after.unearned_interest, before.unearned_interest + 2.0 * 1625.0);
        assert_eq!(after.balance, before.balance + 2.0 * (12_459.0 - 1625.0));
    }

    #[test]
    fn a_suspension_does_not_run_over_a_buy_off() {
        let (journal, ann, loan) = deducted_loan(false);
        service::buyoff(&journal, ann, "L-001", Some("2026-10-18")).unwrap();
        let why = refusal(record_and_clear(&journal, loan, "2026-09-01", None));
        assert!(why.contains("bought off on 2026-10-18"), "{why}");
        assert_eq!(journal.ledger().for_loan(ann, "L-001").unwrap().iter().filter(|r| r.event_type == event::REPAYMENT).count(), 8);
    }

    #[test]
    fn a_suspension_that_skipped_months_goes_only_with_them_deducted() {
        let (journal, ann, loan) = deducted_loan(false);
        record_and_clear(&journal, loan, "2026-06-01", Some("2026-08-01")).unwrap();
        let span = journal.loans().suspensions_for_loan(loan).unwrap().remove(0);
        assert!(skipped(&journal, ann, "L-001", &span).unwrap().is_some());
        let why = refusal(remove(&journal, ann, "L-001", span.id, false));
        assert!(why.contains("already skipped months of L-001"), "{why}");
        // One still to come has skipped nothing, and simply goes.
        record(&journal, loan, "2026-11-01", Some("2027-01-01"), None).unwrap();
        let ahead = journal.loans().suspensions_for_loan(loan).unwrap().into_iter().find(|s| s.id != span.id).unwrap();
        assert_eq!(skipped(&journal, ann, "L-001", &ahead).unwrap(), None);
        remove(&journal, ann, "L-001", ahead.id, false).unwrap();
        assert_eq!(journal.loans().suspensions_for_loan(loan).unwrap().len(), 1);
    }
}
