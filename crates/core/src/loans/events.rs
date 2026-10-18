//! Recovering a loan's asserted events from the ledger it already has.
//!
//! Disbursements, top-ups, payments and suspensions are recorded directly and
//! come back exactly. Terms are not stored with history — `monthly_interest`
//! is one current value — but the accruals are the evidence: each month posts
//! `min(monthly_interest, unearned)`, so a change in the accrued amount *is* a
//! change in the terms, effective the date it first appears. That is
//! inference, stated as such, and checkable: replaying the reconstruction
//! reproduces the stored postings.

use rusqlite::params;

use super::derivation::{EventKind, LoanEvent};
use crate::Journal;
use crate::db::model::event;
use crate::error::Result;
use crate::money::round2;

const EPSILON: f64 = 0.005;

/// The columns reconstruction reads from a ledger row.
#[derive(Debug, Clone, PartialEq)]
pub struct EventRow {
    pub id: i64,
    pub date: String,
    pub kind: String,
    pub added: f64,
    pub deducted: f64,
    pub principal_portion: f64,
    pub interest_portion: f64,
}

/// Recover the asserted events for one loan, earliest first. `rows` are in
/// `(date, id)` order; `suspensions` are `(start, end)` windows, the end being
/// the resume date for a span that was resumed.
pub fn reconstruct(rows: &[EventRow], suspensions: &[(String, Option<String>)]) -> Vec<LoanEvent> {
    let mut events = Vec::new();
    let mut seq = 0;
    let mut current_rate: Option<f64> = None;

    // What each disbursement and top-up brought: the interest charged from it
    // until the next one. The charges are the evidence of what each was
    // written for, as far as it has been charged — so interest a top-up adds
    // is never charged before the top-up.
    let mut pools: Vec<f64> = Vec::new();
    for row in rows {
        match row.kind.as_str() {
            event::LOAN_ISSUED | event::TOP_UP => pools.push(0.0),
            event::INTEREST => match pools.last_mut() {
                Some(pool) => *pool = round2(*pool + row.added),
                None => pools.push(round2(row.added)),
            },
            _ => {}
        }
    }
    let mut pool = pools.into_iter();
    let first_accrual = rows
        .iter()
        .find(|r| r.kind == event::INTEREST)
        .map(|r| round2(r.added))
        .unwrap_or(0.0);

    for (at, row) in rows.iter().enumerate() {
        seq += 1;
        // A buy-off's charge of what was left to charge, just before it on
        // its own day, is part of the settlement, not a change of terms.
        let realised = row.kind == event::INTEREST
            && rows.get(at + 1).is_some_and(|next| next.kind == event::BUYOFF && next.date == row.date);
        match row.kind.as_str() {
            event::LOAN_ISSUED => {
                events.push(LoanEvent {
                    amount: row.added,
                    monthly_interest: Some(first_accrual),
                    unearned_interest: Some(pool.next().unwrap_or(0.0)),
                    source: Some(row.id),
                    ..LoanEvent::new(row.date.clone(), EventKind::Disbursed, seq)
                });
                current_rate = Some(first_accrual);
            }
            event::TOP_UP => events.push(LoanEvent {
                amount: row.added,
                unearned_interest: Some(pool.next().unwrap_or(0.0)),
                source: Some(row.id),
                ..LoanEvent::new(row.date.clone(), EventKind::ToppedUp, seq)
            }),
            event::INTEREST if realised => {}
            event::INTEREST => {
                let amount = round2(row.added);
                if current_rate.is_none_or(|rate| (amount - rate).abs() > EPSILON) {
                    // The accrual changed, so the terms did; seq-1 places the
                    // change before the month it governs.
                    events.push(LoanEvent {
                        monthly_interest: Some(amount),
                        source: Some(row.id),
                        ..LoanEvent::new(row.date.clone(), EventKind::TermsChanged, seq - 1)
                    });
                    current_rate = Some(amount);
                }
            }
            event::REPAYMENT | event::BUYOFF => {
                // A 0/0 split is the absence of one — what clearing a stale
                // split leaves — and means "work it out", not "allocate nothing".
                let recorded = row.principal_portion != 0.0 || row.interest_portion != 0.0;
                let kind = if row.kind == event::BUYOFF { EventKind::Settled } else { EventKind::PaymentReceived };
                events.push(LoanEvent {
                    amount: row.deducted,
                    principal: recorded.then_some(row.principal_portion),
                    interest: recorded.then_some(row.interest_portion),
                    source: Some(row.id),
                    ..LoanEvent::new(row.date.clone(), kind, seq)
                });
            }
            _ => {}
        }
    }

    for (start, until) in suspensions {
        events.push(LoanEvent { until: until.clone(), ..LoanEvent::new(start.clone(), EventKind::Suspended, 0) });
    }
    events.sort_by(|a, b| (a.date.as_str(), a.seq).cmp(&(b.date.as_str(), b.seq)));
    events
}

/// The rows reconstruction reads, for one loan, in `(date, id)` order.
pub fn event_rows(journal: &Journal, member_id: i64, reference: &str) -> Result<Vec<EventRow>> {
    let mut stmt = journal.conn().prepare(
        "SELECT id, date, event_type, added, deducted, principal_portion, interest_portion \
         FROM ledger WHERE individual_id=? AND loan_id=? ORDER BY date, id",
    )?;
    let rows = stmt.query_map(params![member_id, reference], |r| {
        Ok(EventRow {
            id: r.get(0)?,
            date: r.get(1)?,
            kind: r.get(2)?,
            added: r.get::<_, Option<f64>>(3)?.unwrap_or(0.0),
            deducted: r.get::<_, Option<f64>>(4)?.unwrap_or(0.0),
            principal_portion: r.get::<_, Option<f64>>(5)?.unwrap_or(0.0),
            interest_portion: r.get::<_, Option<f64>>(6)?.unwrap_or(0.0),
        })
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

/// A loan's suspension windows: `[start, resumed-or-intended end)`.
pub fn suspension_windows(journal: &Journal, member_id: i64, reference: &str) -> Result<Vec<(String, Option<String>)>> {
    let mut stmt = journal.conn().prepare(
        "SELECT start_date, COALESCE(resumed_date, suspend_until) FROM loan_suspensions \
         WHERE individual_id=? AND loan_ref=? ORDER BY start_date",
    )?;
    let rows = stmt.query_map(params![member_id, reference], |r| Ok((r.get(0)?, r.get(1)?)))?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

/// Reconstruct one loan's events from the journal.
pub fn reconstruct_for_loan(journal: &Journal, member_id: i64, reference: &str) -> Result<Vec<LoanEvent>> {
    let rows = event_rows(journal, member_id, reference)?;
    let windows = suspension_windows(journal, member_id, reference)?;
    Ok(reconstruct(&rows, &windows))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(id: i64, date: &str, kind: &str, added: f64, deducted: f64) -> EventRow {
        EventRow {
            id,
            date: date.into(),
            kind: kind.into(),
            added,
            deducted,
            principal_portion: 0.0,
            interest_portion: 0.0,
        }
    }

    #[test]
    fn a_change_in_the_accrual_is_a_change_in_the_terms() {
        let rows = vec![
            row(1, "2026-01-01", event::LOAN_ISSUED, 1000.0, 0.0),
            row(2, "2026-02-01", event::INTEREST, 10.0, 0.0),
            row(3, "2026-02-01", event::REPAYMENT, 0.0, 100.0),
            row(4, "2026-03-01", event::INTEREST, 12.0, 0.0),
        ];
        let events = reconstruct(&rows, &[]);
        let changes: Vec<_> = events
            .iter()
            .filter(|e| e.kind == EventKind::TermsChanged)
            .map(|e| (e.date.as_str(), e.monthly_interest))
            .collect();
        assert_eq!(changes, [("2026-03-01", Some(12.0))]);
        assert_eq!(events[0].unearned_interest, Some(22.0));
    }

    #[test]
    fn an_empty_split_is_left_for_the_derivation_to_work_out() {
        let rows = vec![row(1, "2026-01-01", event::LOAN_ISSUED, 1000.0, 0.0), row(2, "2026-02-01", event::REPAYMENT, 0.0, 50.0)];
        let events = reconstruct(&rows, &[]);
        let payment = events.iter().find(|e| e.kind == EventKind::PaymentReceived).unwrap();
        assert_eq!((payment.principal, payment.interest), (None, None));
    }
}
