//! A loan's lifecycle: issue, deduct, catch up, top up, buy off, delete.
//!
//! Segregated model: `loans.balance` is principal only; interest is charged
//! month by month out of `unearned_interest` into `interest_balance`, and a
//! deduction settles interest before principal. After writing, each operation
//! re-derives the member's loans (see [`super::projection`]) so the ledger's
//! running balances follow from its facts.
//!
//! Each operation is one transaction: it happens whole or not at all.

use super::projection::project_member;
use super::suspensions::{date_in_suspension, loan_windows, resume};
use crate::Journal;
use crate::dates;
use crate::db::ledger::NewEntry;
use crate::db::loans::{LoanState, NewLoan};
use crate::db::model::{LOAN_ACTIVE, LOAN_PAID, Loan, event};
use crate::error::{Error, Result};
use crate::money::round2;
use crate::policy::{self, Operation};

/// A float as LoanMaster 1.x wrote it into notes (`15000.0`, `0.5`), so the
/// notes of either version read alike.
pub(crate) fn py_float(value: f64) -> String {
    if value.is_finite() && value == value.trunc() && value.abs() < 1e16 {
        format!("{value:.1}")
    } else {
        format!("{value}")
    }
}

/// What a loan is written at: its own rate, else the journal's default.
pub fn loan_rate(journal: &Journal, loan: &Loan) -> f64 {
    loan.interest_rate.unwrap_or_else(|| journal.settings().default_interest_rate())
}

pub(crate) fn require_loan(journal: &Journal, member_id: i64, reference: &str) -> Result<Loan> {
    journal
        .loans()
        .by_ref(member_id, reference)?
        .ok_or_else(|| Error::LoanNotFound(reference.to_string()))
}

fn require_active(loan: &Loan) -> Result<()> {
    if loan.is_active() {
        Ok(())
    } else {
        Err(Error::LoanInactive { reference: loan.reference.clone(), status: loan.status.clone() })
    }
}

/// Keep the member's cached deduction — the sum of their active instalments.
pub fn recalculate_default_deduction(journal: &Journal, member_id: i64) -> Result<()> {
    let total: f64 = journal.loans().active_for_member(member_id)?.iter().map(|l| l.installment).sum();
    journal.members().set_default_deduction(member_id, total)
}

fn settle(journal: &Journal, member_id: i64) -> Result<()> {
    project_member(journal, member_id)?;
    recalculate_default_deduction(journal, member_id)
}

/// The figures a new loan is written with.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LoanTerms {
    pub interest_total: f64,
    pub total_repayment: f64,
    pub installment: f64,
    pub monthly_interest: f64,
}

/// Flat interest: `principal × rate` over the term, instalments rounded up.
pub fn terms_for(principal: f64, duration_months: i64, rate: f64) -> LoanTerms {
    let interest_total = principal * rate;
    let total_repayment = principal + interest_total;
    let months = duration_months.max(1) as f64;
    LoanTerms {
        interest_total,
        total_repayment,
        installment: (total_repayment / months).ceil(),
        monthly_interest: (interest_total / months).ceil(),
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Issued {
    pub reference: String,
    pub installment: f64,
}

/// Issue a new loan. The first deduction falls a month after issue, or in the
/// month of issue when the journal says so.
pub fn issue(
    journal: &Journal,
    member_id: i64,
    principal: f64,
    duration_months: i64,
    date: &str,
    rate: Option<f64>,
) -> Result<Issued> {
    if principal <= 0.0 {
        return Err(Error::invalid("A loan must be for more than zero."));
    }
    if duration_months < 1 {
        return Err(Error::invalid("A loan must run for at least one month."));
    }
    let issued = dates::parse(date).ok_or_else(|| Error::invalid(format!("{date} is not a date.")))?;
    let rate = rate.unwrap_or_else(|| journal.settings().default_interest_rate());
    let terms = terms_for(principal, duration_months, rate);
    let next_due = if journal.settings().deduct_same_month() {
        dates::format(issued)
    } else {
        dates::format(dates::add_months(issued, 1))
    };
    let date = dates::format(issued);

    journal.transaction(|| {
        let reference = journal.loans().next_reference(member_id)?;
        journal.loans().add(&NewLoan {
            member_id,
            reference: &reference,
            principal,
            total_amount: terms.total_repayment,
            installment: terms.installment,
            monthly_interest: terms.monthly_interest,
            start_date: &date,
            next_due_date: &next_due,
            unearned_interest: terms.interest_total,
            duration_months,
            interest_rate: rate,
        })?;
        // A new loan has no history: it opens at its own principal.
        journal.ledger().add(&NewEntry {
            member_id,
            date: date.clone(),
            event_type: event::LOAN_ISSUED.into(),
            loan_ref: reference.clone(),
            added: principal,
            balance: principal,
            notes: format!(
                "Principal: {}, Total Interest: {}, Duration: {duration_months}m",
                py_float(principal),
                py_float(terms.interest_total)
            ),
            installment_amount: terms.installment,
            principal_balance: principal,
            ..NewEntry::default()
        })?;
        settle(journal, member_id)?;
        Ok(Issued { reference, installment: terms.installment })
    })
}

/// Take one month's deduction: accrue the month's interest, then settle
/// interest before principal with the instalment.
pub fn deduct(journal: &Journal, member_id: i64, reference: &str) -> Result<()> {
    journal.transaction(|| {
        let mut loan = require_loan(journal, member_id, reference)?;
        require_active(&loan)?;
        if loan.is_suspended {
            return Err(Error::LoanSuspended { reference: reference.into(), until: loan.suspend_until.clone() });
        }
        // Step over recorded suspension months, so the deduction lands on the
        // first month not suspended — the term extends.
        let windows = loan_windows(journal, loan.id)?;
        if !windows.is_empty() && date_in_suspension(loan.due(), &windows) {
            let mut due = dates::parse(loan.due()).ok_or_else(|| Error::invalid("The loan has no due date."))?;
            for _ in 0..600 {
                if !date_in_suspension(&dates::format(due), &windows) {
                    break;
                }
                due = dates::add_months(due, 1);
            }
            loan.next_due_date = Some(dates::format(due));
            journal.loans().set_next_due(loan.id, loan.due())?;
        }

        let previous = LoanState::of(&loan).to_json();
        let date = loan.due().to_string();
        if dates::parse(&date).is_none() {
            return Err(Error::invalid(format!("{reference} has no valid due date.")));
        }

        let accrual = loan.monthly_interest.min(loan.unearned_interest);
        let new_unearned = loan.unearned_interest - accrual;
        let mut interest_balance = loan.interest_balance;
        let position = journal.ledger().loan_position(member_id, reference)?;
        let (mut running, mut running_interest) = (position.balance, position.interest_balance);
        if accrual > 0.0 {
            running += accrual;
            running_interest += accrual;
            journal.ledger().add(&NewEntry {
                member_id,
                date: date.clone(),
                event_type: event::INTEREST.into(),
                loan_ref: reference.into(),
                added: accrual,
                balance: running,
                notes: "Monthly Interest Accrual".into(),
                interest_amount: accrual,
                principal_balance: position.principal_balance,
                interest_balance: running_interest,
                previous_state: Some(previous.clone()),
                ..NewEntry::default()
            })?;
            interest_balance += accrual;
        }

        let amount = loan.installment;
        let interest_pay = if interest_balance > 0.0 { amount.min(interest_balance) } else { 0.0 };
        let remaining = amount - interest_pay;
        let principal_pay = if remaining > 0.0 { remaining.min(loan.balance) } else { 0.0 };
        let payment = interest_pay + principal_pay;
        let new_principal = loan.balance - principal_pay;
        journal.ledger().add(&NewEntry {
            member_id,
            date: date.clone(),
            event_type: event::REPAYMENT.into(),
            loan_ref: reference.into(),
            deducted: payment,
            balance: running - payment,
            notes: "Monthly Deduction".into(),
            principal_balance: position.principal_balance - principal_pay,
            interest_balance: running_interest - interest_pay,
            principal_portion: principal_pay,
            interest_portion: interest_pay,
            previous_state: Some(previous),
            ..NewEntry::default()
        })?;

        let status = if new_principal > 0.0 { LOAN_ACTIVE } else { LOAN_PAID };
        let next_due = dates::add_months_str(&date, 1).unwrap_or_default();
        journal.loans().set_status(
            loan.id,
            new_principal,
            &next_due,
            status,
            Some(interest_balance - interest_pay),
            Some(new_unearned),
        )?;
        settle(journal, member_id)
    })
}

/// Deduct every month due up to `target` (default today), stepping over
/// suspended months. Returns the number of deductions taken.
pub fn catch_up(journal: &Journal, member_id: i64, reference: &str, target: Option<&str>) -> Result<usize> {
    policy::enforce(journal, &Operation::new("loan.catch_up", member_id).for_loan(reference))?;
    let limit = target.map(str::to_string).unwrap_or_else(dates::today);
    journal.transaction(|| {
        let mut loan = require_loan(journal, member_id, reference)?;
        require_active(&loan)?;

        if loan.is_suspended {
            let until = loan.suspend_until.clone().filter(|s| !s.is_empty());
            let windows = loan_windows(journal, loan.id)?;
            // The resume date is the first due date *not* suspended — the
            // half-open window every reader of a suspension uses.
            if let Some(until) = until.as_deref().filter(|u| loan.due() >= *u) {
                resume(journal, loan.id, Some(until))?;
            } else if !windows.is_empty() && !date_in_suspension(loan.due(), &windows) {
                // Suspended, but the due month sits before the suspension
                // starts: those months are owed. The loop below skips the
                // suspended ones itself.
            } else {
                let Some(mut due) = dates::parse(loan.due()) else { return Ok(0) };
                let resume_date = until.clone().unwrap_or_else(|| limit.clone());
                let mut skipped = 0;
                while dates::format(due) < resume_date && dates::format(due) <= limit {
                    due = dates::add_months(due, 1);
                    skipped += 1;
                }
                if skipped > 0 {
                    journal.loans().set_next_due(loan.id, &dates::format(due))?;
                }
                match until.as_deref() {
                    Some(until) if dates::format(due).as_str() >= until => {
                        resume(journal, loan.id, Some(until))?;
                        loan = require_loan(journal, member_id, reference)?;
                        if !loan.is_active() {
                            return Ok(0);
                        }
                    }
                    _ => return Ok(0),
                }
            }
        }

        let position = journal.ledger().loan_position(member_id, reference)?;
        let (mut running, mut running_principal, mut running_interest) =
            (position.balance, position.principal_balance, position.interest_balance);
        let mut sim = loan.clone();
        if sim.due().trim().is_empty() {
            log::warn!("Loan {reference} has no due date; skipping catch-up.");
            return Ok(0);
        }
        let windows = loan_windows(journal, loan.id)?;
        let mut entries = Vec::new();
        let mut count = 0;
        let mut skipped = 0;

        while sim.due() <= limit.as_str() {
            let due = sim.due().to_string();
            if date_in_suspension(&due, &windows) {
                sim.next_due_date = dates::add_months_str(&due, 1);
                skipped += 1;
                continue;
            }
            // The state before this month, as a single deduction records it —
            // what deleting the month's entries returns the loan to.
            let before = LoanState::of(&sim).to_json();
            let accrual = sim.monthly_interest.min(sim.unearned_interest);
            sim.unearned_interest -= accrual;
            sim.interest_balance += accrual;
            if accrual > 0.0 {
                running += accrual;
                running_interest += accrual;
                entries.push(NewEntry {
                    member_id,
                    date: due.clone(),
                    event_type: event::INTEREST.into(),
                    loan_ref: reference.into(),
                    added: accrual,
                    balance: running,
                    notes: "Monthly Interest Accrual".into(),
                    interest_amount: accrual,
                    principal_balance: running_principal,
                    interest_balance: running_interest,
                    previous_state: Some(before.clone()),
                    ..NewEntry::default()
                });
            }
            let amount = sim.installment;
            let interest_pay = if sim.interest_balance > 0.0 { amount.min(sim.interest_balance) } else { 0.0 };
            let remaining = amount - interest_pay;
            let principal_pay = if sim.balance > 0.0 { remaining.min(sim.balance) } else { 0.0 };
            let payment = interest_pay + principal_pay;
            sim.balance -= principal_pay;
            sim.interest_balance -= interest_pay;
            running -= payment;
            running_principal -= principal_pay;
            running_interest -= interest_pay;
            entries.push(NewEntry {
                member_id,
                date: due.clone(),
                event_type: event::REPAYMENT.into(),
                loan_ref: reference.into(),
                deducted: payment,
                balance: running,
                notes: "Monthly Deduction".into(),
                installment_amount: amount,
                principal_balance: running_principal,
                interest_balance: running_interest,
                principal_portion: principal_pay,
                interest_portion: interest_pay,
                previous_state: Some(before),
                ..NewEntry::default()
            });
            sim.next_due_date = dates::add_months_str(&due, 1);
            count += 1;
            if sim.balance <= 0.0 && sim.interest_balance <= 0.0 {
                sim.status = LOAN_PAID.into();
                break;
            }
        }

        if !entries.is_empty() {
            journal.ledger().add_many(&entries)?;
            journal.loans().set_catch_up_state(
                loan.id,
                sim.balance,
                sim.interest_balance,
                sim.unearned_interest,
                sim.due(),
                &sim.status,
            )?;
            settle(journal, member_id)?;
        } else if skipped > 0 {
            // The window fell entirely inside a suspension: no deductions,
            // but the term still advances.
            journal.loans().set_next_due(loan.id, sim.due())?;
        }
        Ok(count)
    })
}

/// Deduct month by month from `from` to `to` (both `YYYY-MM-DD`), stepping
/// over suspended months — the ledger's "Auto…" deduction.
pub fn deduct_range(journal: &Journal, member_id: i64, reference: &str, from: &str, to: &str) -> Result<usize> {
    journal.transaction(|| {
        let Some(loan) = journal.loans().by_ref(member_id, reference)? else { return Ok(0) };
        if !loan.is_active() {
            return Ok(0);
        }
        let (Some(mut current), Some(end)) = (dates::parse(from), dates::parse(to)) else {
            return Err(Error::invalid("Choose a valid range of dates."));
        };
        let position = journal.ledger().loan_position(member_id, reference)?;
        let (mut running, mut running_principal, mut running_interest) =
            (position.balance, position.principal_balance, position.interest_balance);
        let (mut principal, mut interest, mut unearned) =
            (loan.balance, loan.interest_balance, loan.unearned_interest);
        let mut next_due = loan.due().to_string();
        let windows = loan_windows(journal, loan.id)?;
        let mut made = 0;

        while current <= end && principal > 0.0 {
            let date = dates::format(current);
            if date_in_suspension(&date, &windows) {
                current = dates::add_months(current, 1);
                next_due = dates::format(current);
                continue;
            }
            let accrual = loan.monthly_interest.min(unearned);
            if accrual > 0.0 {
                unearned -= accrual;
                interest += accrual;
                running += accrual;
                running_interest += accrual;
                journal.ledger().add(&NewEntry {
                    member_id,
                    date: date.clone(),
                    event_type: event::INTEREST.into(),
                    loan_ref: reference.into(),
                    added: accrual,
                    balance: running,
                    notes: "Monthly Interest Accrual (Auto)".into(),
                    interest_amount: accrual,
                    principal_balance: running_principal,
                    interest_balance: running_interest,
                    ..NewEntry::default()
                })?;
            }
            let amount = loan.installment.min(principal + interest);
            let interest_pay = if interest > 0.0 { amount.min(interest) } else { 0.0 };
            let remaining = amount - interest_pay;
            let principal_pay = if remaining > 0.0 { remaining.min(principal) } else { 0.0 };
            let payment = interest_pay + principal_pay;
            interest -= interest_pay;
            principal -= principal_pay;
            running -= payment;
            running_principal -= principal_pay;
            running_interest -= interest_pay;
            journal.ledger().add(&NewEntry {
                member_id,
                date: date.clone(),
                event_type: event::REPAYMENT.into(),
                loan_ref: reference.into(),
                deducted: payment,
                balance: running,
                notes: "Monthly Deduction (Auto)".into(),
                principal_balance: running_principal,
                interest_balance: running_interest,
                principal_portion: principal_pay,
                interest_portion: interest_pay,
                ..NewEntry::default()
            })?;
            made += 1;
            current = dates::add_months(current, 1);
            next_due = dates::format(current);
            if principal <= 0.0 {
                break;
            }
        }
        let status = if principal > 0.0 { LOAN_ACTIVE } else { LOAN_PAID };
        journal.loans().set_status(loan.id, principal, &next_due, status, Some(interest), Some(unearned))?;
        settle(journal, member_id)?;
        Ok(made)
    })
}

/// Interest a loan was written for, less what it has charged — recomputed
/// from the ledger at the loan's own rate.
fn unearned_from_ledger(journal: &Journal, member_id: i64, reference: &str, rate: f64) -> Result<f64> {
    let mut expected = 0.0;
    let mut accrued = 0.0;
    for row in journal.ledger().for_loan(member_id, reference)? {
        match row.event_type.as_str() {
            event::LOAN_ISSUED | event::TOP_UP => expected += row.added * rate,
            event::INTEREST => accrued += row.added,
            _ => {}
        }
    }
    Ok((expected - accrued).max(0.0))
}

/// Lend more on a loan and re-term it over `new_duration` months.
pub fn top_up(
    journal: &Journal,
    member_id: i64,
    reference: &str,
    amount: f64,
    new_duration: i64,
    date: Option<&str>,
) -> Result<()> {
    if amount <= 0.0 || new_duration < 1 {
        return Err(Error::invalid("A top-up needs an amount and a duration of at least one month."));
    }
    journal.transaction(|| {
        project_member(journal, member_id)?;
        let loan = require_loan(journal, member_id, reference)?;
        let previous = LoanState::of(&loan).to_json();
        let date = date.map(str::to_string).unwrap_or_else(dates::today);
        let rate = loan_rate(journal, &loan);

        let top_up_interest = amount * rate;
        let new_unearned = unearned_from_ledger(journal, member_id, reference, rate)? + top_up_interest;
        let new_principal = loan.balance + amount;
        let total_future = new_principal + new_unearned + loan.interest_balance;
        let new_installment = (total_future / new_duration as f64).ceil();
        let new_monthly = (new_unearned / new_duration as f64).ceil();
        let position = journal.ledger().loan_position(member_id, reference)?;

        journal.loans().set_details(
            loan.id,
            new_principal + new_unearned,
            new_principal,
            new_installment,
            new_monthly,
            &date,
            Some(new_unearned),
            None,
            None,
        )?;
        journal.loans().set_terms(loan.id, new_duration, None)?;
        journal.ledger().add(&NewEntry {
            member_id,
            date: date.clone(),
            event_type: event::TOP_UP.into(),
            loan_ref: reference.into(),
            added: amount,
            balance: position.balance + amount,
            notes: format!(
                "Top-Up: {}, Add'l Interest: {}, Duration: {new_duration}m",
                py_float(amount),
                py_float(top_up_interest)
            ),
            installment_amount: new_installment,
            interest_amount: new_monthly,
            principal_balance: position.principal_balance + amount,
            interest_balance: position.interest_balance,
            previous_state: Some(previous),
            ..NewEntry::default()
        })?;
        recalculate_default_deduction(journal, member_id)
    })
}

/// Settle a loan in full: realise its unearned interest, then pay principal
/// and all interest in one entry. Returns the amount settled.
pub fn buyoff(journal: &Journal, member_id: i64, reference: &str, date: Option<&str>) -> Result<f64> {
    journal.transaction(|| {
        let loan = require_loan(journal, member_id, reference)?;
        require_active(&loan)?;
        let previous = LoanState::of(&loan).to_json();
        let position = journal.ledger().loan_position(member_id, reference)?;
        let (mut balance, principal, mut interest) =
            (position.balance, position.principal_balance, position.interest_balance);
        if principal <= 0.0 && interest <= 0.0 {
            return Ok(0.0);
        }
        let date = date
            .map(str::to_string)
            .or_else(|| loan.next_due_date.clone().filter(|d| !d.is_empty()))
            .unwrap_or_else(dates::today);

        let future = loan.unearned_interest;
        if future > 0.0 {
            balance += future;
            interest += future;
            journal.ledger().add(&NewEntry {
                member_id,
                date: date.clone(),
                event_type: event::INTEREST.into(),
                loan_ref: reference.into(),
                added: future,
                balance,
                notes: "Buyoff: Realizing unearned interest".into(),
                interest_amount: future,
                principal_balance: principal,
                interest_balance: interest,
                previous_state: Some(previous.clone()),
                ..NewEntry::default()
            })?;
        }
        let amount = principal + interest;
        journal.ledger().add(&NewEntry {
            member_id,
            date: date.clone(),
            event_type: event::BUYOFF.into(),
            loan_ref: reference.into(),
            deducted: amount,
            balance: balance - amount,
            notes: "Full Loan Settlement (Buyoff)".into(),
            principal_portion: principal,
            interest_portion: interest,
            previous_state: Some(previous),
            ..NewEntry::default()
        })?;
        journal.loans().set_status(loan.id, 0.0, "", LOAN_PAID, Some(0.0), Some(0.0))?;
        settle(journal, member_id)?;
        Ok(amount)
    })
}

/// Delete a loan and every entry on it.
pub fn delete_loan(journal: &Journal, member_id: i64, reference: &str) -> Result<()> {
    journal.transaction(|| {
        journal.loans().delete_with_ledger(member_id, reference)?;
        settle(journal, member_id)
    })
}

/// Is this the loan's most recent repayment?
pub fn is_latest_repayment(journal: &Journal, member_id: i64, reference: &str, row_id: i64) -> Result<bool> {
    let latest: Option<i64> = journal
        .conn()
        .query_row(
            "SELECT id FROM ledger WHERE individual_id=? AND loan_id=? AND event_type='Repayment' \
             ORDER BY date DESC, id DESC LIMIT 1",
            rusqlite::params![member_id, reference],
            |r| r.get(0),
        )
        .ok();
    Ok(latest.is_none_or(|id| id == row_id))
}

/// True if the loan has top-ups or buy-offs — a plain rebuild is unsafe.
pub fn has_restructure_events(journal: &Journal, member_id: i64, reference: &str) -> Result<bool> {
    Ok(journal.ledger().count_events(member_id, reference, &[event::TOP_UP, event::BUYOFF], None, None)? > 0)
}

/// True if somebody hand-corrected a row on this loan.
pub fn has_manual_edits(journal: &Journal, member_id: i64, reference: &str) -> Result<bool> {
    let n: i64 = journal.conn().query_row(
        "SELECT COUNT(*) FROM ledger WHERE individual_id=? AND loan_id=? AND COALESCE(is_edited, 0) <> 0",
        rusqlite::params![member_id, reference],
        |r| r.get(0),
    )?;
    Ok(n > 0)
}

/// Re-derive a loan's schedule from issue, applying recorded suspensions:
/// wipe its deductions and interest, reset it to issued, and catch up again
/// to the latest deduction (or `target`). Refused for loans with top-ups,
/// buy-offs or hand edits, which a rebuild would silently overwrite.
pub fn rebuild_schedule(journal: &Journal, member_id: i64, reference: &str, target: Option<&str>) -> Result<usize> {
    journal.transaction(|| {
        let Some(loan) = journal.loans().by_ref(member_id, reference)? else { return Ok(0) };
        if has_restructure_events(journal, member_id, reference)? {
            return Err(Error::invalid("Cannot auto-rebuild a loan with top-ups or buy-offs; adjust it manually."));
        }
        if has_manual_edits(journal, member_id, reference)? {
            return Err(Error::invalid(
                "This loan has hand-edited entries, which a rebuild would replace with the scheduled \
                 amounts. Adjust the affected months manually instead.",
            ));
        }
        let last = journal.ledger().max_date(member_id, reference, event::REPAYMENT)?;
        let limit = target.map(str::to_string).or(last).unwrap_or_else(dates::today);
        journal.ledger().delete_events(member_id, reference, &[event::REPAYMENT, event::INTEREST])?;

        let unearned = round2(loan.principal * loan_rate(journal, &loan));
        let issue = journal
            .ledger()
            .min_date(member_id, reference, event::LOAN_ISSUED)?
            .unwrap_or_else(|| loan.start_date.clone());
        let issued = dates::parse(&issue).ok_or_else(|| Error::invalid("The loan has no issue date."))?;
        let next_due = if journal.settings().deduct_same_month() { issued } else { dates::add_months(issued, 1) };
        journal.loans().reset_to_issued(loan.id, loan.principal, unearned, &dates::format(next_due))?;
        project_member(journal, member_id)?;
        catch_up(journal, member_id, reference, Some(&limit))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::members::MemberDetails;

    pub(crate) fn journal_with_member() -> (Journal, i64) {
        let journal = Journal::open_in_memory().unwrap();
        let id = journal
            .members()
            .add(&MemberDetails { name: "Ann".into(), ..MemberDetails::default() })
            .unwrap();
        (journal, id)
    }

    #[test]
    fn notes_read_like_the_first_version() {
        assert_eq!(py_float(100000.0), "100000.0");
        assert_eq!(py_float(15000.5), "15000.5");
    }

    #[test]
    fn issuing_writes_the_terms_and_opens_at_its_own_principal() {
        let (journal, ann) = journal_with_member();
        issue(&journal, ann, 50000.0, 10, "2026-01-01", Some(0.15)).unwrap();
        let second = issue(&journal, ann, 100000.0, 20, "2025-12-01", Some(0.15)).unwrap();
        assert_eq!(second.reference, "L-002");
        assert_eq!(second.installment, 5750.0);
        let issued = journal.ledger().for_loan(ann, "L-002").unwrap();
        assert_eq!((issued[0].balance, issued[0].principal_balance), (100000.0, 100000.0));
        let loan = journal.loans().by_ref(ann, "L-002").unwrap().unwrap();
        assert_eq!((loan.monthly_interest, loan.unearned_interest), (750.0, 15000.0));
        assert_eq!(loan.due(), "2026-01-01");
        assert_eq!(journal.members().get(ann).unwrap().unwrap().default_deduction, 5750.0 + 5750.0);
    }

    #[test]
    fn catch_up_matches_the_worked_example() {
        let (journal, ann) = journal_with_member();
        issue(&journal, ann, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
        assert_eq!(catch_up(&journal, ann, "L-001", Some("2026-06-01")).unwrap(), 5);
        let loan = journal.loans().by_ref(ann, "L-001").unwrap().unwrap();
        assert_eq!((loan.balance, loan.unearned_interest, loan.due()), (75000.0, 11250.0, "2026-07-01"));
    }

    #[test]
    fn a_single_deduction_settles_interest_first() {
        let (journal, ann) = journal_with_member();
        issue(&journal, ann, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
        deduct(&journal, ann, "L-001").unwrap();
        let rows = journal.ledger().for_loan(ann, "L-001").unwrap();
        let repayment = rows.iter().find(|r| r.event_type == event::REPAYMENT).unwrap();
        assert_eq!((repayment.interest_portion, repayment.principal_portion), (750.0, 5000.0));
        assert_eq!(journal.loans().by_ref(ann, "L-001").unwrap().unwrap().due(), "2026-03-01");
    }

    #[test]
    fn a_buyoff_settles_principal_and_all_interest() {
        let (journal, ann) = journal_with_member();
        issue(&journal, ann, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
        catch_up(&journal, ann, "L-001", Some("2026-03-01")).unwrap();
        let paid = buyoff(&journal, ann, "L-001", None).unwrap();
        assert_eq!(paid, 90000.0 + 13500.0);
        assert_eq!(journal.loans().by_ref(ann, "L-001").unwrap().unwrap().status, LOAN_PAID);
    }

    #[test]
    fn a_top_up_reterms_the_loan_at_its_own_rate() {
        let (journal, ann) = journal_with_member();
        issue(&journal, ann, 100000.0, 20, "2026-01-01", Some(0.0)).unwrap();
        catch_up(&journal, ann, "L-001", Some("2026-03-01")).unwrap();
        top_up(&journal, ann, "L-001", 20000.0, 10, Some("2026-04-01")).unwrap();
        let loan = journal.loans().by_ref(ann, "L-001").unwrap().unwrap();
        // An interest-free loan stays interest-free when topped up.
        assert_eq!((loan.balance, loan.unearned_interest, loan.installment), (110000.0, 0.0, 11000.0));
    }

    #[test]
    fn a_catch_up_for_a_member_who_left_is_refused() {
        let (journal, ann) = journal_with_member();
        issue(&journal, ann, 1000.0, 2, "2026-01-01", Some(0.15)).unwrap();
        journal.members().set_status(ann, "Retired", Some("2026-01-15")).unwrap();
        assert!(matches!(catch_up(&journal, ann, "L-001", Some("2026-03-01")), Err(Error::NotAllowed(_))));
    }
}
