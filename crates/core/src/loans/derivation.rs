//! Deriving a loan's postings from what was asserted about it.
//!
//! A ledger holds two kinds of row. A `Repayment` payroll took is a *fact*; the
//! `Interest Earned` row beside it is a *consequence* of the loan's terms. This
//! module separates them: [`LoanEvent`]s are the asserted list (disbursements,
//! term changes, suspensions, payments received) and [`derive`] is a pure
//! function from that list to every posting the loan implies.
//!
//! The month loop mirrors catch-up deliberately — accrue
//! `min(monthly_interest, unearned)`, then settle interest before principal —
//! so a replay can be compared with stored rows, and any divergence is a
//! finding about the data rather than a modelling choice.

use crate::dates;
use crate::money::round2;

/// What happened to a loan.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventKind {
    Disbursed,
    ToppedUp,
    TermsChanged,
    Suspended,
    PaymentReceived,
    Settled,
}

impl EventKind {
    /// State changes apply before their own month accrues; a payment is a
    /// transaction within that month and lands after its accrual.
    fn changes_state(self) -> bool {
        matches!(
            self,
            EventKind::Disbursed | EventKind::ToppedUp | EventKind::TermsChanged | EventKind::Suspended
        )
    }
}

/// One asserted fact, effective on a date. `seq` orders events sharing a date.
#[derive(Debug, Clone, PartialEq)]
pub struct LoanEvent {
    pub date: String,
    pub kind: EventKind,
    pub seq: i64,
    pub amount: f64,
    pub installment: Option<f64>,
    pub monthly_interest: Option<f64>,
    pub unearned_interest: Option<f64>,
    /// Suspension end; `None` is open-ended.
    pub until: Option<String>,
    /// An asserted split, when the ledger recorded one.
    pub principal: Option<f64>,
    pub interest: Option<f64>,
    /// The ledger row it came from.
    pub source: Option<i64>,
}

impl LoanEvent {
    pub fn new(date: impl Into<String>, kind: EventKind, seq: i64) -> LoanEvent {
        LoanEvent {
            date: date.into(),
            kind,
            seq,
            amount: 0.0,
            installment: None,
            monthly_interest: None,
            unearned_interest: None,
            until: None,
            principal: None,
            interest: None,
            source: None,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Terms {
    pub installment: f64,
    pub monthly_interest: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PostingKind {
    InterestEarned,
    Repayment,
}

/// One row the loan implies.
#[derive(Debug, Clone, PartialEq)]
pub struct Posting {
    pub date: String,
    pub kind: PostingKind,
    pub added: f64,
    pub deducted: f64,
    pub principal_portion: f64,
    pub interest_portion: f64,
    pub principal_balance: f64,
    pub interest_balance: f64,
    pub source: Option<i64>,
}

/// Everything replaying a loan's events produces.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct DerivedLoan {
    pub postings: Vec<Posting>,
    pub principal_balance: f64,
    pub interest_balance: f64,
    pub unearned_interest: f64,
    pub next_due_date: Option<String>,
    pub terms_timeline: Vec<(String, Terms)>,
    pub suspension_windows: Vec<(String, Option<String>)>,
    pub settled: bool,
    pub notes: Vec<String>,
}

impl DerivedLoan {
    pub fn total_repaid(&self) -> f64 {
        round2(self.postings.iter().map(|p| p.deducted).sum())
    }

    pub fn total_accrued(&self) -> f64 {
        round2(
            self.postings
                .iter()
                .filter(|p| p.kind == PostingKind::InterestEarned)
                .map(|p| p.added)
                .sum(),
        )
    }
}

/// The terms in force on `date`.
pub fn terms_on(timeline: &[(String, Terms)], date: &str) -> Terms {
    let mut current = Terms::default();
    for (effective, terms) in timeline {
        if effective.as_str() <= date {
            current = *terms;
        } else {
            break;
        }
    }
    current
}

/// Is `date` inside a half-open window `[start, end)`?
pub fn in_suspension(date: &str, windows: &[(String, Option<String>)]) -> bool {
    windows.iter().any(|(start, end)| {
        start.as_str() <= date && end.as_deref().is_none_or(|end| date < end)
    })
}

fn day_before(date: &str) -> String {
    dates::parse(date)
        .map(|d| dates::format(dates::add_days(d, -1)))
        .unwrap_or_else(|| date.to_string())
}

fn next_month(date: &str) -> String {
    dates::add_months_str(date, 1).unwrap_or_else(|| date.to_string())
}

/// Replay asserted events into every posting they imply. Pure: the same
/// events always give the same postings.
///
/// Payments are inputs. The derivation decides only how each splits and what
/// it leaves outstanding. `up_to` bounds scheduled accrual; with none, the
/// replay runs to the last asserted event.
pub fn derive(events: &[LoanEvent], up_to: Option<&str>) -> DerivedLoan {
    let mut ordered: Vec<&LoanEvent> = events.iter().collect();
    ordered.sort_by(|a, b| (a.date.as_str(), a.seq).cmp(&(b.date.as_str(), b.seq)));
    let mut out = DerivedLoan::default();
    let Some(last) = ordered.last() else {
        out.notes.push("no events".into());
        return out;
    };

    let mut terms = Terms::default();
    let mut principal = 0.0;
    let mut interest_bal = 0.0;
    let mut unearned = 0.0;
    let mut next_due: Option<String> = None;
    let limit = up_to.map(str::to_string).unwrap_or_else(|| last.date.clone());

    // Advance through every due month up to `horizon`, accruing interest. A
    // suspended month advances the due date without accruing: that extends
    // the term rather than forgiving the interest.
    let accrue_until = |horizon: &str,
                        next_due: &mut Option<String>,
                        unearned: &mut f64,
                        interest_bal: &mut f64,
                        principal: f64,
                        terms: Terms,
                        out: &mut DerivedLoan| {
        let Some(mut due) = next_due.clone() else { return };
        while due.as_str() <= horizon {
            if in_suspension(&due, &out.suspension_windows) {
                due = next_month(&due);
                continue;
            }
            let accrual = round2(terms.monthly_interest.min(*unearned));
            if accrual > 0.0 {
                *unearned = round2(*unearned - accrual);
                *interest_bal = round2(*interest_bal + accrual);
                out.postings.push(Posting {
                    date: due.clone(),
                    kind: PostingKind::InterestEarned,
                    added: accrual,
                    deducted: 0.0,
                    principal_portion: 0.0,
                    interest_portion: 0.0,
                    principal_balance: round2(principal),
                    interest_balance: *interest_bal,
                    source: None,
                });
            }
            due = next_month(&due);
        }
        *next_due = Some(due);
    };

    for event in ordered {
        if event.date > limit {
            break;
        }
        let horizon = if event.kind.changes_state() { day_before(&event.date) } else { event.date.clone() };
        let bound = if horizon < limit { horizon } else { limit.clone() };
        accrue_until(&bound, &mut next_due, &mut unearned, &mut interest_bal, principal, terms, &mut out);

        match event.kind {
            EventKind::Disbursed => {
                principal = round2(principal + event.amount);
                unearned = round2(unearned + event.unearned_interest.unwrap_or(0.0));
                terms = Terms {
                    installment: event.installment.filter(|v| *v != 0.0).unwrap_or(terms.installment),
                    monthly_interest: event.monthly_interest.unwrap_or(terms.monthly_interest),
                };
                out.terms_timeline.push((event.date.clone(), terms));
                if next_due.is_none() {
                    next_due = Some(next_month(&event.date));
                }
            }
            EventKind::ToppedUp => {
                principal = round2(principal + event.amount);
                unearned = round2(unearned + event.unearned_interest.unwrap_or(0.0));
                if event.installment.is_some() || event.monthly_interest.is_some() {
                    terms = Terms {
                        installment: event.installment.unwrap_or(terms.installment),
                        monthly_interest: event.monthly_interest.unwrap_or(terms.monthly_interest),
                    };
                    out.terms_timeline.push((event.date.clone(), terms));
                }
            }
            EventKind::TermsChanged => {
                terms = Terms {
                    installment: event.installment.unwrap_or(terms.installment),
                    monthly_interest: event.monthly_interest.unwrap_or(terms.monthly_interest),
                };
                out.terms_timeline.push((event.date.clone(), terms));
            }
            EventKind::Suspended => {
                out.suspension_windows.push((event.date.clone(), event.until.clone()));
            }
            EventKind::PaymentReceived | EventKind::Settled => {
                let paid = round2(event.amount);
                // An asserted split wins; otherwise interest is settled first.
                let (interest_pay, principal_pay) = if event.interest.is_some() || event.principal.is_some() {
                    (round2(event.interest.unwrap_or(0.0)), round2(event.principal.unwrap_or(0.0)))
                } else {
                    let interest_pay = round2(paid.min(interest_bal.max(0.0)));
                    let principal_pay = round2((paid - interest_pay).min(principal.max(0.0)));
                    (interest_pay, principal_pay)
                };
                principal = round2(principal - principal_pay);
                interest_bal = round2(interest_bal - interest_pay);
                out.postings.push(Posting {
                    date: event.date.clone(),
                    kind: PostingKind::Repayment,
                    added: 0.0,
                    deducted: paid,
                    principal_portion: principal_pay,
                    interest_portion: interest_pay,
                    principal_balance: round2(principal),
                    interest_balance: interest_bal,
                    source: event.source,
                });
                if event.kind == EventKind::Settled {
                    out.settled = true;
                }
            }
        }
    }

    // Everything after the last event, up to the horizon.
    accrue_until(&limit, &mut next_due, &mut unearned, &mut interest_bal, principal, terms, &mut out);

    out.principal_balance = round2(principal);
    out.interest_balance = round2(interest_bal);
    out.unearned_interest = round2(unearned);
    out.next_due_date = next_due;
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn disbursed(date: &str, amount: f64, monthly: f64, unearned: f64) -> LoanEvent {
        LoanEvent {
            amount,
            monthly_interest: Some(monthly),
            unearned_interest: Some(unearned),
            ..LoanEvent::new(date, EventKind::Disbursed, 1)
        }
    }

    fn paid(date: &str, amount: f64, seq: i64) -> LoanEvent {
        LoanEvent { amount, ..LoanEvent::new(date, EventKind::PaymentReceived, seq) }
    }

    #[test]
    fn a_plain_loan_accrues_then_settles_interest_first() {
        let events = vec![
            disbursed("2026-01-01", 100000.0, 750.0, 15000.0),
            paid("2026-02-01", 5750.0, 2),
            paid("2026-03-01", 5750.0, 3),
        ];
        let out = derive(&events, None);
        let kinds: Vec<_> = out.postings.iter().map(|p| (p.date.as_str(), p.kind)).collect();
        assert_eq!(kinds, [
            ("2026-02-01", PostingKind::InterestEarned), ("2026-02-01", PostingKind::Repayment),
            ("2026-03-01", PostingKind::InterestEarned), ("2026-03-01", PostingKind::Repayment),
        ]);
        assert_eq!(out.principal_balance, 90000.0);
        assert_eq!(out.unearned_interest, 13500.0);
        assert_eq!(out.postings[1].interest_portion, 750.0);
    }

    #[test]
    fn a_missed_payment_is_settled_by_the_next_one_interest_first() {
        let events = vec![
            disbursed("2026-01-01", 100000.0, 750.0, 15000.0),
            paid("2026-02-01", 5750.0, 2),
            paid("2026-04-01", 5750.0, 3),
        ];
        let out = derive(&events, None);
        let april = out.postings.iter().find(|p| p.date == "2026-04-01" && p.kind == PostingKind::Repayment).unwrap();
        assert_eq!((april.interest_portion, april.principal_portion), (1500.0, 4250.0));
    }

    #[test]
    fn a_suspended_month_accrues_nothing() {
        let mut suspension = LoanEvent::new("2026-02-01", EventKind::Suspended, 0);
        suspension.until = Some("2026-03-01".into());
        let events = vec![disbursed("2026-01-01", 1000.0, 10.0, 100.0), suspension, paid("2026-03-01", 100.0, 2)];
        let out = derive(&events, None);
        assert!(out.postings.iter().all(|p| p.date != "2026-02-01"));
        assert_eq!(out.total_accrued(), 10.0);
    }

    #[test]
    fn windows_are_half_open() {
        let windows = vec![("2026-04-28".to_string(), Some("2026-06-28".to_string()))];
        assert!(in_suspension("2026-04-28", &windows));
        assert!(in_suspension("2026-05-28", &windows));
        assert!(!in_suspension("2026-06-28", &windows));
        assert!(in_suspension("2030-01-01", &[("2026-01-01".into(), None)]));
    }
}
