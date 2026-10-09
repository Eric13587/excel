//! Who is behind on a loan, and by how much.
//!
//! Exactly what the dashboard's arrears tile counts — an active loan whose
//! due date has passed — so the list can never disagree with the number above
//! it. Suspended loans are carried *flagged*, not dropped: a deliberate hold is
//! not a default, but it is not something to hide from whoever chases arrears.
//!
//! Days and missed instalments are arithmetic on the due date. The amount
//! overdue is not stored anywhere — the ledger records what was received, not
//! what was expected — so it is `missed × instalment`, capped at what is owed.

use std::collections::HashSet;

use chrono::{Datelike, NaiveDate};

use crate::Journal;
use crate::dates;
use crate::db::loans::OverdueLoan;
use crate::error::Result;

/// SACCO classification by days in arrears (SASRA): label, first day, last day.
pub const BANDS: [(&str, i64, Option<i64>); 5] = [
    ("Performing", 0, Some(30)),
    ("Watch", 31, Some(180)),
    ("Substandard", 181, Some(360)),
    ("Doubtful", 361, Some(540)),
    ("Loss", 541, None),
];

pub fn classify(days: i64) -> &'static str {
    BANDS
        .iter()
        .find(|(_, low, high)| days >= *low && high.is_none_or(|h| days <= h))
        .map(|(label, _, _)| *label)
        .unwrap_or("Loss")
}

/// Instalments unpaid at `as_of`, counting the overdue one itself.
fn months_missed(due: NaiveDate, as_of: NaiveDate) -> i64 {
    let mut months = (as_of.year() - due.year()) as i64 * 12 + as_of.month() as i64 - due.month() as i64;
    if as_of.day() < due.day() && dates::add_months(due, months as i32) > as_of {
        months -= 1;
    }
    (months + 1).max(1)
}

#[derive(Debug, Clone, PartialEq)]
pub struct ArrearsRow {
    pub loan_id: i64,
    pub member_id: i64,
    pub name: String,
    pub pf_no: String,
    pub phone: String,
    pub reference: String,
    pub next_due_date: String,
    pub days: i64,
    pub months_missed: i64,
    pub installment: f64,
    pub overdue_amount: f64,
    pub balance: f64,
    pub band: &'static str,
    pub suspended: bool,
    pub suspend_until: String,
    pub retired: bool,
    pub employment_status: String,
}

/// Overdue loans as arrears rows, worst first.
pub fn build_rows(loans: &[OverdueLoan], as_of: &str) -> Vec<ArrearsRow> {
    let Some(today) = dates::parse(as_of) else { return Vec::new() };
    let mut rows: Vec<ArrearsRow> = loans
        .iter()
        .filter_map(|loan| {
            let Some(due) = dates::parse(&loan.next_due_date) else {
                log::warn!("Skipping loan {}: unreadable due date {:?}", loan.reference, loan.next_due_date);
                return None;
            };
            let days = (today - due).num_days();
            if days <= 0 {
                return None;
            }
            let missed = months_missed(due, today);
            let overdue = if loan.installment != 0.0 { (missed as f64 * loan.installment).min(loan.balance) } else { 0.0 };
            Some(ArrearsRow {
                loan_id: loan.loan_id,
                member_id: loan.individual_id,
                name: loan.name.clone(),
                pf_no: loan.pf_no.clone(),
                phone: loan.phone.clone(),
                reference: loan.reference.clone(),
                next_due_date: dates::date_part(&loan.next_due_date).to_string(),
                days,
                months_missed: missed,
                installment: loan.installment.ceil(),
                overdue_amount: overdue.ceil(),
                balance: loan.balance.ceil(),
                band: classify(days),
                suspended: loan.is_suspended,
                suspend_until: loan.suspend_until.clone().unwrap_or_default(),
                retired: loan.is_retired,
                employment_status: if loan.employment_status.is_empty() {
                    "Active".into()
                } else {
                    loan.employment_status.clone()
                },
            })
        })
        .collect();
    rows.sort_by(|a, b| b.days.cmp(&a.days).then_with(|| a.name.cmp(&b.name)));
    rows
}

/// Headline totals. `members` counts people, not loans.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Summary {
    pub loans: usize,
    pub members: usize,
    pub overdue_amount: f64,
    pub balance: f64,
    pub suspended: usize,
    pub worst_days: i64,
}

pub fn summarize(rows: &[ArrearsRow]) -> Summary {
    Summary {
        loans: rows.len(),
        members: rows.iter().map(|r| r.member_id).collect::<HashSet<_>>().len(),
        overdue_amount: rows.iter().map(|r| r.overdue_amount).sum(),
        balance: rows.iter().map(|r| r.balance).sum(),
        suspended: rows.iter().filter(|r| r.suspended).count(),
        worst_days: rows.iter().map(|r| r.days).max().unwrap_or(0),
    }
}

/// Arrears straight from a journal, as at `as_of` (default today).
pub fn arrears(journal: &Journal, as_of: Option<&str>) -> Result<Vec<ArrearsRow>> {
    let as_of = as_of.map(str::to_string).unwrap_or_else(dates::today);
    Ok(build_rows(&journal.loans().overdue(&as_of)?, &as_of))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bands_follow_sasra() {
        assert_eq!(classify(1), "Performing");
        assert_eq!(classify(31), "Watch");
        assert_eq!(classify(361), "Doubtful");
        assert_eq!(classify(9999), "Loss");
    }

    #[test]
    fn a_loan_due_on_the_first_and_unread_on_the_fifteenth_has_missed_one() {
        let d = |s| dates::parse(s).unwrap();
        assert_eq!(months_missed(d("2026-03-01"), d("2026-03-15")), 1);
        assert_eq!(months_missed(d("2026-03-01"), d("2026-05-01")), 3);
        assert_eq!(months_missed(d("2026-03-28"), d("2026-04-27")), 1);
        assert_eq!(months_missed(d("2026-03-28"), d("2026-04-28")), 2);
    }

    #[test]
    fn the_overdue_amount_never_exceeds_what_is_owed() {
        let loan = OverdueLoan {
            loan_id: 1,
            individual_id: 1,
            reference: "L-001".into(),
            balance: 1000.0,
            installment: 750.0,
            interest_balance: 0.0,
            next_due_date: "2026-01-01".into(),
            start_date: "2025-12-01".into(),
            is_suspended: false,
            suspend_until: None,
            name: "Ann".into(),
            phone: "".into(),
            pf_no: "".into(),
            employment_status: "".into(),
            is_retired: false,
        };
        let rows = build_rows(&[loan], "2026-03-15");
        assert_eq!((rows[0].months_missed, rows[0].overdue_amount), (3, 1000.0));
        assert_eq!(summarize(&rows).members, 1);
    }
}
