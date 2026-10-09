//! Removing a deduction, and putting a missed one back — one definition for
//! the member's ledger and the Ledger Grid alike.
//!
//! docs/grid/deleting-months.md §4 is the specification. Removing a deduction
//! is one of two things, decided by what follows it on the loan:
//!
//! * **un-run** — it is the loan's last entry: the deduction and the month's
//!   interest charge go, the interest returns to unearned, and the month falls
//!   due again — the exact inverse of a deduction;
//! * **missed** — later deductions exist: only the deduction goes, the
//!   interest stays charged, and every later deduction is re-split, interest
//!   first.
//!
//! A buy-off after it, or a top-up between it and the next deduction, refuses
//! it, as does a loan that does not reconcile with its own history.

use rusqlite::params;

use super::projection::{clear_split, project_loan, reconcile_problem};
use crate::Journal;
use crate::dates;
use crate::db::ledger::{NewEntry, Snapshot};
use crate::db::model::event;
use crate::error::{Error, Result};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Removal {
    /// The loan's last entry: the month is undone.
    UnRun,
    /// Later deductions exist: the payment is recorded as not made.
    Missed,
}

/// Loan events that are not deductions, and that a deletion never removes.
const STRUCTURAL: [&str; 3] = [event::LOAN_ISSUED, event::TOP_UP, event::RESTRUCTURE];

/// A loan's non-interest rows: `(date, id, kind)` in order.
pub type Timeline = Vec<(String, i64, String)>;

pub fn timeline(journal: &Journal, member_id: i64, reference: &str) -> Result<Timeline> {
    let mut stmt = journal.conn().prepare(
        "SELECT date, id, event_type FROM ledger WHERE individual_id=? AND loan_id=? \
         AND event_type != 'Interest Earned' ORDER BY date, id",
    )?;
    let rows = stmt.query_map(params![member_id, reference], |r| {
        Ok((dates::date_part(&r.get::<_, String>(0)?).to_string(), r.get(1)?, r.get(2)?))
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

fn after<'a>(timeline: &'a Timeline, date: &str, row_id: i64, gone: &[i64]) -> Vec<&'a (String, i64, String)> {
    let key = (dates::date_part(date), row_id);
    timeline
        .iter()
        .filter(|(d, i, _)| (d.as_str(), *i) > key && !gone.contains(i))
        .collect()
}

/// The date of a buy-off after a row, if any.
pub fn buyoff_after(timeline: &Timeline, date: &str, row_id: i64) -> Option<String> {
    after(timeline, date, row_id, &[]).into_iter().find(|(_, _, k)| k == event::BUYOFF).map(|(d, _, _)| d.clone())
}

pub fn behind_buyoff(when: &str) -> String {
    format!(
        "The loan was bought off on {when}, for a figure worked out from a history that includes \
         this month. Reverse the buy-off in the member's ledger first (Delete Last restores the loan)."
    )
}

/// What removing one deduction does, or why it may not be removed. `gone`
/// holds deductions removed before it in the same batch — newer ones.
pub fn removal_kind(timeline: &Timeline, row_id: i64, date: &str, gone: &[i64]) -> std::result::Result<Removal, String> {
    let later = after(timeline, date, row_id, gone);
    if let Some((when, _, _)) = later.iter().find(|(_, _, k)| k == event::BUYOFF) {
        return Err(behind_buyoff(when));
    }
    if later.iter().any(|(_, _, k)| k == event::REPAYMENT) {
        return Ok(Removal::Missed);
    }
    if let Some((when, _, kind)) = later.iter().find(|(_, _, k)| STRUCTURAL.contains(&k.as_str())) {
        let what = kind.replace("Loan ", "").to_lowercase();
        return Err(format!(
            "A {what} on {when} follows this, the loan's last deduction, and was worked out from the \
             balance it left. Remove the {what} in the member's ledger first, or keep this month."
        ));
    }
    Ok(Removal::UnRun)
}

/// Judge one loan's deductions for removal together, newest first, all or
/// nothing. `payments` are `(row_id, date, kind)`; the kinds come back in the
/// same order, or the reason none may go.
pub fn judge(timeline: &Timeline, payments: &[(i64, String, String)]) -> std::result::Result<Vec<Removal>, String> {
    let mut ordered: Vec<&(i64, String, String)> = payments.iter().collect();
    ordered.sort_by(|a, b| (dates::date_part(&b.1), b.0).cmp(&(dates::date_part(&a.1), a.0)));
    let mut gone = Vec::new();
    let mut kinds = std::collections::HashMap::new();
    for (row_id, date, kind) in ordered {
        if kind == event::BUYOFF {
            return Err("A buy-off settles the loan; reverse it in the member's ledger rather than \
                        deleting it here."
                .into());
        }
        kinds.insert(*row_id, removal_kind(timeline, *row_id, date, &gone)?);
        gone.push(*row_id);
    }
    Ok(payments.iter().map(|(id, _, _)| kinds[id]).collect())
}

/// Why this loan's history cannot be re-derived, as a refusal, or `None`.
pub fn unreconciled(journal: &Journal, member_id: i64, reference: &str) -> Result<Option<String>> {
    Ok(reconcile_problem(journal, member_id, reference)?.map(|why| {
        format!(
            "{reference} does not reconcile with its own ledger ({why}), so what this change does to it \
             cannot be worked out. Correct the loan's history in the member's ledger first."
        )
    }))
}

/// Splits on every deduction after a row, as they were — for an exact undo.
pub type Splits = Vec<(i64, f64, f64)>;

/// Clear the split on every deduction after `(date, row_id)`: a split is
/// derived from everything before it, so a change to one deduction changes
/// every one after it. Returns the splits as they were.
pub fn resplit_after(journal: &Journal, member_id: i64, reference: &str, date: &str, row_id: i64) -> Result<Splits> {
    let date = dates::date_part(date).to_string();
    let mut stmt = journal.conn().prepare(
        "SELECT id, principal_portion, interest_portion FROM ledger WHERE individual_id=? AND loan_id=? \
         AND event_type='Repayment' AND (date > ?3 OR (date = ?3 AND id > ?4)) ORDER BY date, id",
    )?;
    let later: Splits = stmt
        .query_map(params![member_id, reference, date, row_id], |r| {
            Ok((r.get(0)?, r.get::<_, Option<f64>>(1)?.unwrap_or(0.0), r.get::<_, Option<f64>>(2)?.unwrap_or(0.0)))
        })?
        .collect::<rusqlite::Result<_>>()?;
    drop(stmt);
    for (id, _, _) in &later {
        clear_split(journal, *id)?;
    }
    Ok(later)
}

pub fn restore_splits(journal: &Journal, splits: &Splits) -> Result<()> {
    for (id, principal, interest) in splits {
        journal.ledger().set_portions(*id, *principal, *interest)?;
    }
    Ok(())
}

/// What a removal did, so it can be put back exactly.
#[derive(Debug, Clone)]
pub struct RemovalRecord {
    pub kind: Removal,
    /// Deleted rows, every column — interest charge first, as they sort.
    pub rows: Vec<Snapshot>,
    /// Interest returned to unearned (un-run).
    pub unearned: f64,
    /// Later splits as they were (missed).
    pub later: Splits,
}

/// Remove one deduction as `kind` says, and re-derive the loan.
pub fn remove(journal: &Journal, member_id: i64, loan_id: i64, reference: &str, row_id: i64, kind: Removal) -> Result<RemovalRecord> {
    let payment = journal
        .ledger()
        .snapshot(row_id)?
        .ok_or_else(|| Error::invalid("That deduction no longer exists."))?;
    let date = payment.get_text("date");
    let mut record = RemovalRecord { kind, rows: vec![payment], unearned: 0.0, later: Vec::new() };
    match kind {
        Removal::UnRun => {
            // The month is undone: its interest charge goes too, and the
            // interest returns to what the loan has still to charge.
            let charge: Option<i64> = journal
                .conn()
                .query_row(
                    "SELECT id FROM ledger WHERE individual_id=? AND loan_id=? AND event_type='Interest Earned' \
                     AND date=? AND id < ? ORDER BY id DESC LIMIT 1",
                    params![member_id, reference, date, row_id],
                    |r| r.get(0),
                )
                .ok();
            if let Some(charge_id) = charge
                && let Some(snapshot) = journal.ledger().snapshot(charge_id)?
            {
                record.unearned = snapshot.get_f64("added");
                record.rows.insert(0, snapshot);
                journal.ledger().delete(charge_id)?;
                journal.loans().adjust_unearned(loan_id, record.unearned)?;
            }
        }
        Removal::Missed => {
            // The month stays charged; what follows settles it first.
            record.later = resplit_after(journal, member_id, reference, &date, row_id)?;
        }
    }
    journal.ledger().delete(row_id)?;
    project_loan(journal, member_id, reference)?;
    Ok(record)
}

/// Put a removal back exactly: original rows and ids, unearned interest, and
/// the later splits as they were.
pub fn unremove(journal: &Journal, loan_id: i64, record: &RemovalRecord) -> Result<()> {
    for row in &record.rows {
        journal.ledger().reinstate(row)?;
    }
    if record.unearned != 0.0 {
        journal.loans().adjust_unearned(loan_id, -record.unearned)?;
    }
    restore_splits(journal, &record.later)
}

/// Record the deduction for a missed month, on the date its interest was
/// charged; later deductions are re-split. Returns the new row and the later
/// splits as they were.
pub fn record_missed(journal: &Journal, member_id: i64, reference: &str, month: &str, amount: f64) -> Result<(i64, Splits)> {
    let charge: Option<(i64, String)> = journal
        .conn()
        .query_row(
            "SELECT id, date FROM ledger WHERE individual_id=? AND loan_id=? AND event_type='Interest Earned' \
             AND date LIKE ? ORDER BY id DESC LIMIT 1",
            params![member_id, reference, format!("{month}%")],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .ok();
    let (charge_id, date) = charge.ok_or_else(|| Error::invalid("No interest is charged in that month any more — reload."))?;
    let paid: i64 = journal.conn().query_row(
        "SELECT COUNT(*) FROM ledger WHERE individual_id=? AND loan_id=? \
         AND event_type IN ('Repayment', 'Loan Buyoff') AND date LIKE ?",
        params![member_id, reference, format!("{month}%")],
        |r| r.get(0),
    )?;
    if paid > 0 {
        return Err(Error::invalid("That month has a deduction now — reload."));
    }
    let later = resplit_after(journal, member_id, reference, &date, charge_id)?;
    let row_id = journal.ledger().add(&NewEntry {
        member_id,
        date: date.clone(),
        event_type: event::REPAYMENT.into(),
        loan_ref: reference.into(),
        deducted: amount,
        notes: "Monthly Deduction (grid entry, recorded late)".into(),
        ..NewEntry::default()
    })?;
    project_loan(journal, member_id, reference)?;
    Ok((row_id, later))
}
