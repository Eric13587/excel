//! Writing a loan's derived values back into its rows.
//!
//! [`super::derivation`] computes what a loan's postings *should* be; this
//! puts that answer into the `ledger` and `loans` rows everything else reads.
//! A split is derived, so it is recomputed rather than preserved, and
//! [`clear_split`] is what an edit calls so the derivation is not handed a
//! stale one to replay.
//!
//! What is never touched: the amount, date and type of every row — those are
//! facts. Only running balances, the principal/interest split and the loan's
//! cached position are rewritten.

use log::warn;
use rusqlite::params;

use super::derivation::{DerivedLoan, PostingKind, derive};
use super::events::reconstruct_for_loan;
use crate::Journal;
use crate::dates;
use crate::db::model::{LOAN_ACTIVE, LOAN_PAID, event};
use crate::error::Result;
use crate::money::round2;

/// Rows the derivation emits a posting for. Every other row on a loan — an
/// issue, a top-up, a restructure marker — has its running columns carried.
const POSTED: [&str; 3] = [event::INTEREST, event::REPAYMENT, event::BUYOFF];

/// Forget a row's derived split, so the next projection recomputes it.
pub fn clear_split(journal: &Journal, row_id: i64) -> Result<()> {
    journal.ledger().set_portions(row_id, 0.0, 0.0)
}

struct StoredRow {
    id: i64,
    date: String,
    derived: [f64; 5],
}

/// Pair stored rows with derived postings, or say why they do not line up.
fn alignment(journal: &Journal, member_id: i64, reference: &str) -> Result<Alignment> {
    let events = reconstruct_for_loan(journal, member_id, reference)?;
    if events.is_empty() {
        return Ok(Alignment::Nothing);
    }
    // Up to the loan's last entry, not just its last payment: a month whose
    // interest was charged and nothing paid — a missed month — can be the
    // latest thing to have happened to it.
    let last = journal.ledger().last_date(member_id, reference)?.map(|d| dates::date_part(&d).to_string());
    let result = derive(&events, last.as_deref());
    let mut stmt = journal.conn().prepare(
        "SELECT id, date, balance, principal_balance, interest_balance, principal_portion, \
         interest_portion FROM ledger WHERE individual_id=? AND loan_id=? \
         AND event_type IN ('Interest Earned','Repayment','Loan Buyoff') ORDER BY date, id",
    )?;
    let stored: Vec<StoredRow> = stmt
        .query_map(params![member_id, reference], |r| {
            let num = |i: usize| -> rusqlite::Result<f64> { Ok(r.get::<_, Option<f64>>(i)?.unwrap_or(0.0)) };
            Ok(StoredRow { id: r.get(0)?, date: r.get(1)?, derived: [num(2)?, num(3)?, num(4)?, num(5)?, num(6)?] })
        })?
        .collect::<rusqlite::Result<_>>()?;
    let postings = result
        .postings
        .iter()
        .filter(|p| matches!(p.kind, PostingKind::InterestEarned | PostingKind::Repayment))
        .count();
    if stored.len() != postings {
        return Ok(Alignment::Mismatch(format!("{} stored rows vs {postings} derived", stored.len())));
    }
    for (row, posting) in stored.iter().zip(&result.postings) {
        if dates::date_part(&row.date) != posting.date {
            return Ok(Alignment::Mismatch(format!(
                "row {} dated {} where its schedule has {}",
                row.id,
                dates::date_part(&row.date),
                posting.date
            )));
        }
    }
    Ok(Alignment::Aligned(result, stored))
}

enum Alignment {
    Nothing,
    Mismatch(String),
    Aligned(DerivedLoan, Vec<StoredRow>),
}

/// `Ok(None)` when [`project_loan`] can derive this loan; otherwise why not.
/// Asks without writing anything.
pub fn reconcile_problem(journal: &Journal, member_id: i64, reference: &str) -> Result<Option<String>> {
    Ok(match alignment(journal, member_id, reference)? {
        Alignment::Mismatch(why) => Some(why),
        _ => None,
    })
}

/// Re-derive one loan and write the result into its rows. Returns how many
/// ledger rows changed — 0 on a loan that was already right.
pub fn project_loan(journal: &Journal, member_id: i64, reference: &str) -> Result<usize> {
    let (result, stored) = match alignment(journal, member_id, reference)? {
        Alignment::Nothing => return Ok(0),
        Alignment::Mismatch(why) => {
            warn!("Loan {member_id}/{reference}: {why} — not projecting");
            return Ok(0);
        }
        Alignment::Aligned(result, stored) => (result, stored),
    };

    let mut changed = 0;
    for (row, posting) in stored.iter().zip(&result.postings) {
        let gross = round2(posting.principal_balance + posting.interest_balance);
        let wanted = [
            gross,
            posting.principal_balance,
            posting.interest_balance,
            posting.principal_portion,
            posting.interest_portion,
        ];
        if row.derived.map(round2) == wanted {
            continue;
        }
        journal.ledger().set_derived(row.id, wanted[0], wanted[1], wanted[2], wanted[3], wanted[4])?;
        changed += 1;
    }
    changed += carry_position(journal, member_id, reference)?;

    if let Some(loan) = journal.loans().by_ref(member_id, reference)? {
        // unearned_interest is deliberately not written: it is what the loan
        // was written to charge and has not charged yet — a term, not a
        // derivation.
        let status = if result.principal_balance > 0.0 { LOAN_ACTIVE } else { LOAN_PAID };
        let next_due = next_due(journal, member_id, reference, &loan)?;
        journal.loans().set_projection(
            loan.id,
            result.principal_balance,
            result.interest_balance,
            status,
            next_due.as_deref(),
        )?;
    }
    Ok(changed)
}

/// Write the running position onto rows the derivation does not post: a
/// disbursement adds what it disbursed to the position brought forward, a
/// restructure repeats it.
fn carry_position(journal: &Journal, member_id: i64, reference: &str) -> Result<usize> {
    let mut stmt = journal.conn().prepare(
        "SELECT id, event_type, added, balance, principal_balance, interest_balance FROM ledger \
         WHERE individual_id=? AND loan_id=? ORDER BY date, id",
    )?;
    let rows: Vec<(i64, String, f64, [f64; 3])> = stmt
        .query_map(params![member_id, reference], |r| {
            let num = |i: usize| -> rusqlite::Result<f64> { Ok(r.get::<_, Option<f64>>(i)?.unwrap_or(0.0)) };
            Ok((r.get(0)?, r.get(1)?, num(2)?, [num(3)?, num(4)?, num(5)?]))
        })?
        .collect::<rusqlite::Result<_>>()?;
    drop(stmt);

    let (mut principal, mut interest) = (0.0, 0.0);
    let mut changed = 0;
    for (id, kind, added, stored) in rows {
        if POSTED.contains(&kind.as_str()) {
            principal = stored[1];
            interest = stored[2];
            continue;
        }
        principal = round2(principal + added);
        let wanted = [round2(principal + interest), principal, round2(interest)];
        if stored.map(round2) == wanted {
            continue;
        }
        journal.ledger().set_running(id, wanted[0], wanted[1], wanted[2])?;
        changed += 1;
    }
    Ok(changed)
}

/// When the next deduction falls due: a month after the last month the
/// schedule has dealt with — paid, or its interest charged with nothing paid
/// (missed) — or after issue (honouring `deduct_same_month`) when none has.
/// The interest a buy-off realises on its own date is not a month.
fn next_due(journal: &Journal, member_id: i64, reference: &str, loan: &crate::db::model::Loan) -> Result<Option<String>> {
    let ledger = journal.ledger();
    let rows = ledger.for_loan(member_id, reference)?;
    let settled: Vec<&str> = rows.iter().filter(|r| r.event_type == event::BUYOFF).map(|r| dates::date_part(&r.date)).collect();
    let last_repayment = rows
        .iter()
        .filter(|r| {
            r.event_type == event::REPAYMENT
                || (r.event_type == event::INTEREST && !settled.contains(&dates::date_part(&r.date)))
        })
        .map(|r| r.date.clone())
        .max();
    let base = match &last_repayment {
        Some(date) => Some(date.clone()),
        None => ledger
            .min_date(member_id, reference, event::LOAN_ISSUED)?
            .or_else(|| Some(loan.start_date.clone()).filter(|s| !s.is_empty())),
    };
    let Some(base) = base else { return Ok(loan.next_due_date.clone()) };
    let Some(base) = dates::parse(base.split_whitespace().next().unwrap_or("")) else {
        return Ok(loan.next_due_date.clone());
    };
    if last_repayment.is_some() || !journal.settings().deduct_same_month() {
        Ok(Some(dates::format(dates::add_months(base, 1))))
    } else {
        Ok(Some(dates::format(base)))
    }
}

/// Re-derive every loan a member holds. Returns the rows changed in total.
pub fn project_member(journal: &Journal, member_id: i64) -> Result<usize> {
    let mut changed = 0;
    for loan in journal.loans().for_member(member_id)? {
        changed += project_loan(journal, member_id, &loan.reference)?;
    }
    Ok(changed)
}

/// Does replaying the reconstructed events reproduce the stored accruals?
pub fn replay_matches(journal: &Journal, member_id: i64, reference: &str) -> Result<(bool, String)> {
    let events = reconstruct_for_loan(journal, member_id, reference)?;
    if events.is_empty() {
        return Ok((true, "nothing to replay".into()));
    }
    let derived = derive(&events, None);
    let (count, total): (i64, f64) = journal.conn().query_row(
        "SELECT COUNT(*), COALESCE(SUM(added), 0) FROM ledger WHERE individual_id=? AND loan_id=? \
         AND event_type='Interest Earned'",
        params![member_id, reference],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let accruals = derived.postings.iter().filter(|p| p.kind == PostingKind::InterestEarned).count();
    if accruals as i64 != count {
        return Ok((false, format!("{accruals} accrual(s) derived vs {count} stored")));
    }
    if (derived.total_accrued() - total).abs() > 0.51 {
        return Ok((false, format!("interest derived {:.2} vs stored {total:.2}", derived.total_accrued())));
    }
    Ok((true, "matches".into()))
}
