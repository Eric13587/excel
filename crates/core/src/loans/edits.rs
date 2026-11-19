//! Correcting a loan's history from the member's ledger: delete an entry, edit
//! one, change a repayment, edit a top-up, move dates.
//!
//! Every edit ends by re-deriving the loan, so what follows the edited row
//! follows from it — and is refused if it leaves the loan out of order or
//! repaid beyond what it owed ([`super::guards`]). Nothing is moved into a
//! month to come, and a changed interest charge keeps the loan's total
//! interest what it was written for.

use rusqlite::params;

use super::deletions;
use super::guards::{keep_sound, soundness};
use super::projection::{clear_split, project_loan, project_member};
use super::service::{loan_rate, recalculate_default_deduction, require_loan, unearned_from_ledger};
use crate::Journal;
use crate::dates;
use crate::db::loans::LoanState;
use crate::db::model::{LedgerRow, event};
use crate::error::{Error, Result};
use crate::money::{round2, shillings};

fn loan_rows(journal: &Journal, row: &LedgerRow) -> Result<Vec<LedgerRow>> {
    journal.ledger().for_loan(row.individual_id, &row.loan_ref)
}

/// The paired row: a repayment's same-day interest charge before it, or an
/// interest charge's same-day repayment after it.
fn sibling(rows: &[LedgerRow], row: &LedgerRow) -> Option<i64> {
    let at = rows.iter().position(|r| r.id == row.id)?;
    match row.event_type.as_str() {
        event::REPAYMENT if at > 0 => {
            let prev = &rows[at - 1];
            (prev.event_type == event::INTEREST && prev.date == row.date).then_some(prev.id)
        }
        event::INTEREST => {
            let next = rows.get(at + 1)?;
            (next.event_type == event::REPAYMENT && next.date == row.date).then_some(next.id)
        }
        _ => None,
    }
}

/// Delete one entry and revert what it did to the loan.
///
/// * a **repayment** (or the interest charge paired with it) is removed by the
///   rules in [`super::deletions`]: un-run if it is the loan's last entry,
///   recorded as missed if later deductions exist — the same definition the
///   Ledger Grid uses;
/// * a **standalone interest charge** can go only from the end of the loan;
///   its interest returns to unearned;
/// * a **buy-off** takes the interest it realised, and the loan returns to the
///   state it was in before;
/// * a **top-up or restructure** takes every later entry on the loan, which
///   was computed on top of it, and the loan returns to its state before;
/// * the **issue** deletes the loan — a loan without its issue is not one.
pub fn delete_entry(journal: &Journal, member_id: i64, row_id: i64) -> Result<()> {
    journal.transaction(|| {
        let Some(row) = journal.ledger().get(row_id)? else { return Ok(()) };
        let before = soundness(journal, member_id, &row.loan_ref)?;
        if row.loan_ref.is_empty() || row.loan_ref == "-" {
            return journal.ledger().delete(row_id);
        }
        let Some(loan) = journal.loans().by_ref(member_id, &row.loan_ref)? else {
            return journal.ledger().delete(row_id);
        };
        let rows = loan_rows(journal, &row)?;
        match row.event_type.as_str() {
            event::LOAN_ISSUED => journal.loans().delete_with_ledger(member_id, &row.loan_ref)?,
            event::REPAYMENT | event::INTEREST => {
                let payment = if row.event_type == event::REPAYMENT { Some(row.id) } else { sibling(&rows, &row) };
                match payment {
                    Some(payment_id) => {
                        if let Some(why) = deletions::unreconciled(journal, member_id, &row.loan_ref)? {
                            return Err(Error::Invalid(why));
                        }
                        let payment_row = rows.iter().find(|r| r.id == payment_id).unwrap_or(&row);
                        let timeline = deletions::timeline(journal, member_id, &row.loan_ref)?;
                        let kind = deletions::removal_kind(&timeline, payment_id, &payment_row.date, &[])
                            .map_err(Error::Invalid)?;
                        deletions::remove(journal, member_id, loan.id, &row.loan_ref, payment_id, kind)?;
                    }
                    None => {
                        // An interest charge with no deduction beside it.
                        if rows.last().is_some_and(|last| last.id != row.id) {
                            return Err(Error::invalid(
                                "An interest charge in the middle of a loan's history cannot be removed on \
                                 its own: the months after it were worked out from it.",
                            ));
                        }
                        journal.ledger().delete(row.id)?;
                        journal.loans().adjust_unearned(loan.id, row.added)?;
                    }
                }
            }
            event::BUYOFF => {
                for realised in rows.iter().filter(|r| r.event_type == event::INTEREST && r.date == row.date) {
                    journal.ledger().delete(realised.id)?;
                }
                journal.ledger().delete(row.id)?;
                restore_previous(journal, loan.id, &row)?;
                charge_what_remains(journal, member_id, loan.id, &row.loan_ref)?;
            }
            event::TOP_UP | event::RESTRUCTURE => {
                let at = rows.iter().position(|r| r.id == row.id).unwrap_or(0);
                for later in &rows[at..] {
                    journal.ledger().delete(later.id)?;
                }
                restore_previous(journal, loan.id, &row)?;
                charge_what_remains(journal, member_id, loan.id, &row.loan_ref)?;
            }
            _ => journal.ledger().delete(row.id)?,
        }
        project_member(journal, member_id)?;
        if journal.loans().by_ref(member_id, &row.loan_ref)?.is_some() {
            keep_sound(journal, member_id, &row.loan_ref, &before)?;
        }
        recalculate_default_deduction(journal, member_id)
    })
}

/// After an entry that changed a loan's terms is taken away, the loan has
/// left to charge what its remaining history was written for, less what it
/// has charged. The snapshot restored is the loan before that entry; an
/// earlier entry corrected since must not take its correction back with it.
fn charge_what_remains(journal: &Journal, member_id: i64, loan_id: i64, reference: &str) -> Result<()> {
    let Some(loan) = journal.loans().get(loan_id)? else { return Ok(()) };
    let wanted = round2(unearned_from_ledger(journal, member_id, reference, loan_rate(journal, &loan))?);
    journal.loans().adjust_unearned(loan_id, round2(wanted - loan.unearned_interest))
}

/// Return a loan to the state an entry recorded before it was made. An entry
/// from before snapshots were kept leaves the figures to the projection.
fn restore_previous(journal: &Journal, loan_id: i64, row: &LedgerRow) -> Result<()> {
    if let Some(state) = row.previous_state.as_deref().and_then(LoanState::from_json) {
        journal.loans().restore_state(loan_id, &state)?;
    } else if row.event_type == event::TOP_UP {
        let Some(loan) = journal.loans().get(loan_id)? else { return Ok(()) };
        let rate = loan_rate(journal, &loan);
        journal.loans().adjust_unearned(loan_id, -(row.added * rate).min(loan.unearned_interest))?;
    }
    Ok(())
}

/// Edit an entry's facts — date, amounts, notes. The split is cleared and
/// re-derived; `mark_edited` locks the amount against regeneration.
#[allow(clippy::too_many_arguments)]
pub fn edit_entry(
    journal: &Journal,
    member_id: i64,
    row_id: i64,
    date: &str,
    added: f64,
    deducted: f64,
    notes: &str,
    mark_edited: bool,
) -> Result<()> {
    if dates::parse(date).is_none() {
        return Err(Error::invalid(format!("{date} is not a date (use YYYY-MM-DD).")));
    }
    if added < 0.0 || deducted < 0.0 {
        return Err(Error::invalid("Amounts cannot be negative; the entry's type carries the direction."));
    }
    journal.transaction(|| {
        let row = journal.ledger().get(row_id)?.ok_or_else(|| Error::invalid("That entry no longer exists."))?;
        if dates::date_part(date) != dates::date_part(&row.date)
            && let Some(why) = journal.date_ahead(date)
        {
            return Err(Error::NotAllowed(vec![why]));
        }
        let before = soundness(journal, member_id, &row.loan_ref)?;
        if row.event_type == event::INTEREST && (added - row.added).abs() > 0.005 {
            // What this month charges more, the months to come charge less:
            // the loan's total interest stays what it was written for.
            let loan = require_loan(journal, member_id, &row.loan_ref)?;
            if !loan.is_active() {
                return Err(Error::NotAllowed(vec![format!(
                    "{} is {}; its interest charges stay as they were.",
                    row.loan_ref,
                    loan.status.to_lowercase()
                )]));
            }
            let more = round2(added - row.added);
            if more > loan.unearned_interest + 0.005 {
                return Err(Error::NotAllowed(vec![format!(
                    "That charges {} more interest, but {} has only {} left to charge.",
                    shillings(more),
                    row.loan_ref,
                    shillings(loan.unearned_interest)
                )]));
            }
            journal.loans().adjust_unearned(loan.id, -more)?;
        }
        journal.ledger().update_core(row_id, date, added, deducted, notes, None, mark_edited, None)?;
        clear_split(journal, row_id)?;
        if !row.loan_ref.is_empty() {
            project_loan(journal, member_id, &row.loan_ref)?;
        }
        project_member(journal, member_id)?;
        if !row.loan_ref.is_empty() {
            keep_sound(journal, member_id, &row.loan_ref, &before)?;
        }
        recalculate_default_deduction(journal, member_id)
    })
}

/// Change what a repayment took, and re-term the loan around it ("sticky"
/// deduction): the new figure becomes the instalment, the term is worked out
/// from what is left, and the month's and later interest charges move to the
/// new monthly rate.
pub fn change_repayment(journal: &Journal, member_id: i64, row_id: i64, amount: f64, notes: &str) -> Result<()> {
    if amount <= 0.0 {
        return Err(Error::invalid(
            "Setting a repayment to zero is not supported. If no deduction happened that month, move \
             the entry's date to the month it actually happened.",
        ));
    }
    journal.transaction(|| {
        let row = journal.ledger().get(row_id)?.ok_or_else(|| Error::invalid("That entry no longer exists."))?;
        let before = soundness(journal, member_id, &row.loan_ref)?;
        change_repayment_inner(journal, member_id, row_id, amount, notes, false)?;
        keep_sound(journal, member_id, &row.loan_ref, &before)
    })
}

fn split_against_previous(journal: &Journal, row: &LedgerRow, amount: f64) -> Result<(f64, f64)> {
    let rows = loan_rows(journal, row)?;
    let at = rows.iter().position(|r| r.id == row.id).unwrap_or(0);
    let interest_before = if at > 0 { rows[at - 1].interest_balance } else { 0.0 };
    let interest = if interest_before > 0.0 { amount.min(interest_before) } else { 0.0 };
    Ok((amount - interest, interest))
}

fn change_repayment_inner(
    journal: &Journal,
    member_id: i64,
    row_id: i64,
    amount: f64,
    notes: &str,
    resplit_only: bool,
) -> Result<()> {
    let row = journal.ledger().get(row_id)?.ok_or_else(|| Error::invalid("That entry no longer exists."))?;
    let (principal, interest) = split_against_previous(journal, &row, amount)?;
    journal.ledger().update_core(row_id, &row.date, 0.0, amount, notes, Some((principal, interest)), true, None)?;
    project_loan(journal, member_id, &row.loan_ref)?;
    project_member(journal, member_id)?;
    recalculate_default_deduction(journal, member_id)?;
    if resplit_only {
        return Ok(());
    }

    let loan = require_loan(journal, member_id, &row.loan_ref)?;
    if !loan.is_active() {
        return Ok(());
    }
    let total_future = loan.balance + loan.interest_balance + loan.unearned_interest;
    let (future_sum, future_count): (f64, i64) = journal.conn().query_row(
        "SELECT COALESCE(SUM(added), 0), COUNT(*) FROM ledger WHERE individual_id=? AND loan_id=? \
         AND event_type='Interest Earned' AND date >= ?",
        params![member_id, row.loan_ref, row.date],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    // What is left to charge, counting this month's and later charges as not
    // yet charged — they are about to be re-priced.
    let pot = loan.unearned_interest + future_sum;
    let months = ((total_future + amount) / amount).ceil().max(1.0);
    let mut monthly = if pot > 0.0 { (pot / months).ceil() } else { 0.0 };
    // The charges already recorded from this month on take the new rate, but
    // together never more than the loan has left to charge: re-pricing moves
    // interest between months, it does not add any.
    if future_count > 0 && monthly * future_count as f64 > pot {
        monthly = (pot / future_count as f64).floor();
    }
    let remaining = round2((pot - monthly * future_count as f64).max(0.0));
    journal.loans().set_details(
        loan.id,
        loan.total_amount,
        loan.balance,
        amount,
        monthly,
        loan.due(),
        Some(remaining),
        None,
        None,
    )?;
    journal.conn().execute(
        "UPDATE ledger SET added=?, is_edited=1 WHERE individual_id=? AND loan_id=? \
         AND event_type='Interest Earned' AND date >= ?",
        params![monthly, member_id, row.loan_ref, row.date],
    )?;
    journal.conn().execute("UPDATE ledger SET interest_amount=? WHERE id=?", params![monthly, row_id])?;
    project_member(journal, member_id)?;
    // The month's charge moved, so the split is worked out once more.
    change_repayment_inner(journal, member_id, row_id, amount, notes, true)
}

/// Correct a top-up's amount, date or term. The loan is re-termed as if the
/// top-up had been made with the corrected figures, and the interest it adds
/// moves by the change in amount — at the loan's own rate.
pub fn edit_top_up(
    journal: &Journal,
    member_id: i64,
    row_id: i64,
    date: &str,
    amount: f64,
    duration_months: i64,
    notes: &str,
) -> Result<()> {
    if amount <= 0.0 || duration_months < 1 {
        return Err(Error::invalid("A top-up needs an amount and a duration of at least one month."));
    }
    if dates::parse(date).is_none() {
        return Err(Error::invalid(format!("{date} is not a date (use YYYY-MM-DD).")));
    }
    journal.transaction(|| {
        let row = journal.ledger().get(row_id)?.ok_or_else(|| Error::invalid("That entry no longer exists."))?;
        if row.event_type != event::TOP_UP {
            return Err(Error::invalid("Only a top-up can be edited this way."));
        }
        if dates::date_part(date) != dates::date_part(&row.date)
            && let Some(why) = journal.date_ahead(date)
        {
            return Err(Error::NotAllowed(vec![why]));
        }
        let sound = soundness(journal, member_id, &row.loan_ref)?;
        let loan = require_loan(journal, member_id, &row.loan_ref)?;
        let rate = loan_rate(journal, &loan);
        let changed = (row.added - amount).abs() > 0.001;
        journal.ledger().update_core(row_id, date, amount, 0.0, notes, None, changed, None)?;
        journal.loans().adjust_unearned(loan.id, (amount - row.added) * rate)?;

        let rows = journal.ledger().for_loan(member_id, &row.loan_ref)?;
        let at = rows.iter().position(|r| r.id == row_id).unwrap_or(0);
        let before = at.checked_sub(1).map(|i| &rows[i]);
        let (principal_before, interest_before) =
            before.map(|r| (r.principal_balance, r.interest_balance)).unwrap_or((0.0, 0.0));
        let charged_since: f64 =
            rows[at..].iter().filter(|r| r.event_type == event::INTEREST).map(|r| r.added).sum();
        let unearned_now = journal.loans().get(loan.id)?.map(|l| l.unearned_interest).unwrap_or(0.0);
        let unearned_at_top_up = unearned_now + charged_since;
        let months = duration_months as f64;
        let installment = ((principal_before + amount + unearned_at_top_up + interest_before) / months).ceil();
        let monthly = (unearned_at_top_up / months).ceil();
        journal.loans().set_installment(loan.id, installment, monthly)?;
        journal.loans().set_terms(loan.id, duration_months, None)?;
        project_member(journal, member_id)?;
        keep_sound(journal, member_id, &row.loan_ref, &sound)?;
        recalculate_default_deduction(journal, member_id)
    })
}

/// The difference between two dates as `relativedelta(new, old)` computes
/// it: whole months, then days.
pub fn relative_delta(new: chrono::NaiveDate, old: chrono::NaiveDate) -> (i32, i64) {
    use chrono::Datelike;
    let mut months = (new.year() - old.year()) * 12 + new.month() as i32 - old.month() as i32;
    let mut candidate = dates::add_months(old, months);
    if new >= old {
        while candidate > new {
            months -= 1;
            candidate = dates::add_months(old, months);
        }
    } else {
        while candidate < new {
            months += 1;
            candidate = dates::add_months(old, months);
        }
    }
    (months, (new - candidate).num_days())
}

/// Move an entry to `new_date`; with `cascade`, every later entry on the loan
/// moves by the same relative shift (one month moves each one month on).
pub fn move_date(journal: &Journal, member_id: i64, row_id: i64, new_date: &str, cascade: bool) -> Result<usize> {
    let new = dates::parse(new_date).ok_or_else(|| Error::invalid(format!("{new_date} is not a date.")))?;
    journal.transaction(|| {
        let row = journal.ledger().get(row_id)?.ok_or_else(|| Error::invalid("That entry no longer exists."))?;
        let old = dates::parse(&row.date).ok_or_else(|| Error::invalid("The entry's date is unreadable."))?;
        let (months, days) = relative_delta(new, old);
        let before = soundness(journal, member_id, &row.loan_ref)?;
        let rows = journal.ledger().for_loan(member_id, &row.loan_ref)?;
        let start = rows.iter().position(|r| r.id == row_id).unwrap_or(0);
        let targets: Vec<&LedgerRow> = if cascade { rows[start..].iter().collect() } else { vec![&rows[start]] };
        for target in &targets {
            if let Some(old) = dates::parse(&target.date) {
                let moved = dates::format(dates::add_days(dates::add_months(old, months), days));
                if let Some(why) = journal.date_ahead(&moved) {
                    return Err(Error::NotAllowed(vec![why]));
                }
                journal.conn().execute("UPDATE ledger SET date=? WHERE id=?", params![moved, target.id])?;
            }
        }
        project_member(journal, member_id)?;
        keep_sound(journal, member_id, &row.loan_ref, &before)?;
        recalculate_default_deduction(journal, member_id)?;
        Ok(targets.len())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::members::MemberDetails;
    use crate::loans::service::{catch_up, issue};

    fn loan_with_history() -> (Journal, i64) {
        let journal = Journal::open_in_memory().unwrap();
        let id = journal.members().add(&MemberDetails { name: "Ann".into(), ..Default::default() }).unwrap();
        issue(&journal, id, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
        catch_up(&journal, id, "L-001", Some("2026-03-01")).unwrap();
        (journal, id)
    }

    #[test]
    fn deleting_the_last_repayment_takes_its_interest_and_restores_the_loan() {
        let (journal, ann) = loan_with_history();
        let last = journal.ledger().for_loan(ann, "L-001").unwrap().last().unwrap().id;
        delete_entry(&journal, ann, last).unwrap();
        let loan = journal.loans().by_ref(ann, "L-001").unwrap().unwrap();
        assert_eq!(journal.ledger().for_loan(ann, "L-001").unwrap().len(), 3);
        assert_eq!((loan.balance, loan.due()), (95000.0, "2026-03-01"));
        assert_eq!(loan.unearned_interest, 14250.0);
    }

    #[test]
    fn deleting_the_issue_deletes_the_loan() {
        let (journal, ann) = loan_with_history();
        let issue_row = journal.ledger().for_loan(ann, "L-001").unwrap()[0].id;
        delete_entry(&journal, ann, issue_row).unwrap();
        assert!(journal.loans().by_ref(ann, "L-001").unwrap().is_none());
    }

    #[test]
    fn relative_shifts_follow_relativedelta() {
        let d = |s| dates::parse(s).unwrap();
        assert_eq!(relative_delta(d("2026-02-28"), d("2026-01-28")), (1, 0));
        assert_eq!(relative_delta(d("2026-03-05"), d("2026-01-28")), (1, 5));
        assert_eq!(relative_delta(d("2026-01-01"), d("2026-03-01")), (-2, 0));
    }

    #[test]
    fn a_date_moved_with_cascade_moves_the_rest_by_a_month() {
        let (journal, ann) = loan_with_history();
        let rows = journal.ledger().for_loan(ann, "L-001").unwrap();
        let issued = rows.iter().find(|r| r.event_type == event::LOAN_ISSUED).unwrap().id;
        move_date(&journal, ann, issued, "2026-02-01", true).unwrap();
        let moved: Vec<String> = journal.ledger().for_loan(ann, "L-001").unwrap().iter().map(|r| r.date.clone()).collect();
        assert_eq!(moved, ["2026-02-01", "2026-03-01", "2026-03-01", "2026-04-01", "2026-04-01"]);
        assert_eq!(journal.loans().by_ref(ann, "L-001").unwrap().unwrap().due(), "2026-05-01");
    }

    #[test]
    fn moving_deductions_away_from_their_schedule_is_refused() {
        // February's charge moved to March leaves February with none: the
        // schedule could no longer account for it.
        let (journal, ann) = loan_with_history();
        let rows = journal.ledger().for_loan(ann, "L-001").unwrap();
        let feb_interest = rows.iter().find(|r| r.event_type == event::INTEREST).unwrap().id;
        let refused = move_date(&journal, ann, feb_interest, "2026-03-01", true).unwrap_err().to_string();
        assert!(refused.contains("nothing accounts for"), "{refused}");
        assert_eq!(journal.ledger().for_loan(ann, "L-001").unwrap(), rows, "nothing moved");
        // Nor into a month to come.
        let last = rows.last().unwrap().id;
        assert!(move_date(&journal, ann, last, "2099-01-01", false).is_err());
    }
}
