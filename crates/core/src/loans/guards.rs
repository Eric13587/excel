//! What every change to a loan must leave true: its history still reads in
//! order — every charge and repayment where its schedule puts it, and every
//! month before its last payment accounted for — and the loan is never repaid
//! more than it owed, in principal or in interest, nor paid money that
//! settles nothing.
//!
//! A loan already broken before a change is not held against it: only a
//! change that makes it worse is refused, so old figures can still be
//! corrected.

use std::collections::HashSet;

use super::projection::reconcile_problem;
use super::service::loan_rate;
use super::suspensions::{date_in_suspension, loan_windows};
use crate::Journal;
use crate::dates;
use crate::error::{Error, Result};
use crate::money::shillings;

/// How sound a loan's history is, to tell whether a change made it worse.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Soundness {
    readable: bool,
    low_principal: f64,
    low_interest: f64,
    /// The most any one payment took beyond what it settled.
    excess: f64,
    /// Months nothing accounts for (see [`unexplained_months`]).
    gaps: usize,
    /// Entries dated before the loan's disbursement.
    early: usize,
}

/// Entries dated before the loan's disbursement — or every entry, when it has
/// none.
fn before_disbursement(rows: &[crate::db::model::LedgerRow]) -> usize {
    match rows.iter().find(|r| r.event_type == event::LOAN_ISSUED) {
        Some(issue) => rows.iter().filter(|r| dates::date_part(&r.date) < dates::date_part(&issue.date)).count(),
        None => rows.len(),
    }
}

use crate::db::model::event;

/// What a payment took beyond the principal and interest it settled.
fn excess(row: &crate::db::model::LedgerRow) -> f64 {
    if row.event_type == event::REPAYMENT || row.event_type == event::BUYOFF {
        (row.deducted - row.principal_portion - row.interest_portion).max(0.0)
    } else {
        0.0
    }
}

pub fn soundness(journal: &Journal, member_id: i64, reference: &str) -> Result<Soundness> {
    let (mut low_principal, mut low_interest, mut most) = (0.0f64, 0.0f64, 0.0f64);
    let rows = journal.ledger().for_loan(member_id, reference)?;
    let early = before_disbursement(&rows);
    for row in rows {
        low_principal = low_principal.min(row.principal_balance);
        low_interest = low_interest.min(row.interest_balance);
        most = most.max(excess(&row));
    }
    Ok(Soundness {
        readable: reconcile_problem(journal, member_id, reference)?.is_none(),
        low_principal,
        low_interest,
        excess: most,
        gaps: unexplained_months(journal, member_id, reference)?.len(),
        early,
    })
}

/// Due months before a loan's last payment that nothing accounts for: no
/// interest charged in them, not suspended, and interest still left to
/// charge on the loan's terms. A month passed over is either paid, missed
/// (charged, nothing paid) or suspended — never just absent.
pub fn unexplained_months(journal: &Journal, member_id: i64, reference: &str) -> Result<Vec<String>> {
    let Some(loan) = journal.loans().by_ref(member_id, reference)? else { return Ok(Vec::new()) };
    let rows = journal.ledger().for_loan(member_id, reference)?;
    let Some(issued) = rows.iter().find(|r| r.event_type == event::LOAN_ISSUED).and_then(|r| dates::parse(&r.date)) else {
        return Ok(Vec::new());
    };
    let Some(last_payment) = rows
        .iter()
        .filter(|r| r.event_type == event::REPAYMENT || r.event_type == event::BUYOFF)
        .map(|r| dates::date_part(&r.date).to_string())
        .max()
    else {
        return Ok(Vec::new());
    };
    let rate = loan_rate(journal, &loan);
    let windows = loan_windows(journal, loan.id)?;
    let charged: HashSet<String> = rows.iter().filter(|r| r.event_type == event::INTEREST).map(|r| dates::month_key(&r.date)).collect();
    let mut due = if journal.settings().deduct_same_month() { issued } else { dates::add_months(issued, 1) };
    let mut gaps = Vec::new();
    while dates::format(due) <= last_payment {
        let day = dates::format(due);
        let month = dates::month_key(&day);
        let written: f64 = rows
            .iter()
            .filter(|r| (r.event_type == event::LOAN_ISSUED || r.event_type == event::TOP_UP) && dates::date_part(&r.date) <= day.as_str())
            .map(|r| r.added * rate)
            .sum();
        let charged_before: f64 =
            rows.iter().filter(|r| r.event_type == event::INTEREST && dates::date_part(&r.date) < day.as_str()).map(|r| r.added).sum();
        if !charged.contains(&month) && !date_in_suspension(&day, &windows) && charged_before + 0.5 < written {
            gaps.push(month);
        }
        due = dates::add_months(due, 1);
    }
    Ok(gaps)
}

/// Refuse the change just made — inside the caller's transaction, which the
/// refusal rolls back — when it left the loan worse than `before`.
pub fn keep_sound(journal: &Journal, member_id: i64, reference: &str, before: &Soundness) -> Result<()> {
    let rows = journal.ledger().for_loan(member_id, reference)?;
    if !rows.iter().any(|r| r.event_type == event::LOAN_ISSUED) {
        return Err(Error::NotAllowed(vec![format!(
            "That would leave {reference} without its disbursement. To remove the loan, delete the loan."
        )]));
    }
    if before_disbursement(&rows) > before.early {
        return Err(Error::NotAllowed(vec![format!(
            "That would date an entry of {reference} before the loan was disbursed."
        )]));
    }
    let gaps = unexplained_months(journal, member_id, reference)?;
    if gaps.len() > before.gaps {
        let shown: Vec<String> = gaps.iter().take(4).map(|m| dates::month_label(m)).collect();
        return Err(Error::NotAllowed(vec![format!(
            "That would leave {} of {reference} with no deduction, no interest charge and no suspension ({}{}), \
             which nothing accounts for. Move a whole schedule from its disbursement, or record a suspension for \
             those months.",
            if gaps.len() == 1 { "a month".to_string() } else { format!("{} months", gaps.len()) },
            shown.join(", "),
            if gaps.len() > 4 { ", …" } else { "" }
        )]));
    }
    if before.readable
        && let Some(why) = reconcile_problem(journal, member_id, reference)?
    {
        return Err(Error::NotAllowed(vec![format!(
            "That would put {reference}'s entries out of step with its schedule ({why}). Move a whole schedule \
             from its disbursement, record a suspension for months without deductions, or correct the entries \
             in date order."
        )]));
    }
    for row in journal.ledger().for_loan(member_id, reference)? {
        let over = excess(&row);
        if over > before.excess + 0.005 {
            return Err(Error::NotAllowed(vec![format!(
                "That is {} more than {reference} owed on {}; it owed {} then. To settle the loan, buy it off.",
                shillings(over),
                dates::date_part(&row.date),
                shillings(row.deducted - over)
            )]));
        }
        if row.principal_balance < before.low_principal.min(0.0) - 0.005 {
            return Err(Error::NotAllowed(vec![format!(
                "That would have {reference} repaid {} more than it was lent, by {}.",
                shillings(-row.principal_balance),
                dates::date_part(&row.date)
            )]));
        }
        if row.interest_balance < before.low_interest.min(0.0) - 0.005 {
            return Err(Error::NotAllowed(vec![format!(
                "That would have {reference} pay {} more interest than it had been charged, by {}.",
                shillings(-row.interest_balance),
                dates::date_part(&row.date)
            )]));
        }
    }
    Ok(())
}
