use super::*;
use crate::db::members::MemberDetails;
use crate::funds;
use crate::loans::service::{catch_up, issue};
use crate::loans::suspensions;

fn member(journal: &Journal, name: &str) -> i64 {
    journal.members().add(&MemberDetails { name: name.into(), phone: "0700".into(), ..Default::default() }).unwrap()
}

fn statement(journal: &Journal, id: i64, from: &str, to: &str) -> Statement {
    prepare(journal, id, from, to, &StatementConfig::default()).unwrap()
}

#[test]
fn a_loan_is_replayed_from_its_start_and_shown_for_the_period() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    issue(&journal, ann, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
    catch_up(&journal, ann, "L-001", Some("2026-06-01")).unwrap();

    let s = statement(&journal, ann, "2026-03-01", "2026-04-30");
    assert_eq!(s.period, "March 01, 2026 to April 30, 2026");
    assert!(s.active);
    let lines: Vec<(&str, &str, f64, f64, Option<f64>)> = s.loans[0]
        .lines
        .iter()
        .map(|l| (l.date.as_str(), l.event_type.as_str(), l.interest, l.credit, l.gross))
        .collect();
    // Issued for 115,000 owed in all; February paid 5,750 before the period,
    // which the brought-forward line carries in.
    assert_eq!(
        lines,
        [
            ("2026-03-01", BROUGHT_FORWARD, 0.0, 0.0, Some(109250.0)),
            ("2026-03-01", "Interest Charged", 750.0, 0.0, None),
            ("2026-03-01", event::REPAYMENT, 0.0, 5750.0, Some(103500.0)),
            ("2026-04-01", "Interest Charged", 750.0, 0.0, None),
            ("2026-04-01", event::REPAYMENT, 0.0, 5750.0, Some(97750.0)),
        ]
    );
    assert_eq!((s.net_outstanding, s.gross_outstanding), (85000.0, 97750.0));
    let carried = &s.loans[0].lines[0];
    assert!(carried.brought_forward);
    assert_eq!(carried.balance, 95000.0);
}

#[test]
fn the_gross_figure_uses_the_loans_own_rate() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    issue(&journal, ann, 100000.0, 10, "2026-01-01", Some(0.10)).unwrap();
    let s = statement(&journal, ann, "2026-01-01", "2026-01-31");
    assert_eq!(s.loans[0].lines[0].gross, Some(110000.0));
}

#[test]
fn savings_run_from_the_first_deposit_and_withdrawals_are_negative() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    for (date, kind, amount) in [("2025-12-01", "Deposit", 2000.0), ("2026-01-05", "Deposit", 2000.0), ("2026-01-20", "Withdrawal", 500.0), ("2026-03-01", "Deposit", 2000.0)] {
        funds::post(&journal, funds::Posting::new(Fund::Savings, ann, date, kind, amount, "")).unwrap();
    }
    let s = statement(&journal, ann, "2026-01-01", "2026-01-31");
    let lines: Vec<(f64, f64)> = s.savings.iter().map(|l| (l.amount, l.balance)).collect();
    // December's deposit is carried in.
    assert_eq!(lines, [(0.0, 2000.0), (2000.0, 4000.0), (-500.0, 3500.0)]);
    assert!(s.savings[0].brought_forward && !s.savings[0].is_withdrawal());
    assert!(s.savings[2].is_withdrawal());
    assert_eq!(s.savings_balance, 3500.0); // as at the period's end, not today
    assert!(s.loans.is_empty());
}

#[test]
fn a_suspension_is_noted_with_the_months_inside_the_period() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    issue(&journal, ann, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
    catch_up(&journal, ann, "L-001", Some("2026-06-01")).unwrap();
    let loan = journal.loans().by_ref(ann, "L-001").unwrap().unwrap();
    suspensions::record(&journal, loan.id, "2026-07-01", Some("2026-10-01"), Some("2026-10-09")).unwrap();

    let s = statement(&journal, ann, "2026-06-01", "2026-08-31");
    let notes: Vec<&str> = s.loans[0].lines.iter().filter_map(|l| l.annotation.as_deref()).collect();
    assert_eq!(notes, ["Loan suspended — no deductions for Jul, Aug 2026"]);
    assert_eq!(s.loans[0].lines.last().unwrap().date, "2026-07-01");
}

#[test]
fn an_empty_period_is_empty_and_a_bad_one_is_refused() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    assert!(statement(&journal, ann, "2026-01-01", "2026-01-31").is_empty());
    assert!(prepare(&journal, ann, "2026-02-01", "2026-01-01", &StatementConfig::default()).is_err());
    assert!(matches!(prepare(&journal, 999, "2026-01-01", "2026-01-31", &StatementConfig::default()), Err(Error::MemberNotFound(999))));
}

#[test]
fn notes_lose_the_engines_markers_and_names_become_file_names() {
    assert_eq!(crate::notes::without_markers("Monthly Deduction (Auto)"), "Monthly Deduction");
    assert_eq!(crate::notes::without_markers("Monthly Interest Accrual  (Catch-up) (Auto)"), "Monthly Interest Accrual");
    assert_eq!(safe_file_name("ANN W. O'NEIL / Jr."), "ANN W ONEIL  Jr");
    assert_eq!(safe_file_name("///"), "Statement");
    assert_eq!(safe_file_name(""), "Unknown");
    let today = dates::parse("2026-10-09").unwrap();
    assert_eq!(pdf_file_name("Ann", today), "Ann_statement_20261009.pdf");
    assert_eq!(excel_file_name("Ann", "2026-01-01", "2026-03-31"), "Statement_Ann_2026-01-01_to_2026-03-31.xlsx");
    let (first, last) = whole_months(dates::parse("2026-02-14").unwrap(), dates::parse("2026-02-03").unwrap());
    assert_eq!((dates::format(first), dates::format(last)), ("2026-02-01".to_string(), "2026-02-28".to_string()));
}

fn references(s: &Statement) -> Vec<&str> {
    s.loans.iter().map(|l| l.reference.as_str()).collect()
}

/// L-001 paid off in March; L-002 issued in April and still owing.
fn two_loans(journal: &Journal) -> i64 {
    let ann = member(journal, "Ann");
    issue(journal, ann, 12000.0, 2, "2026-01-01", Some(0.0)).unwrap();
    catch_up(journal, ann, "L-001", Some("2026-03-01")).unwrap();
    issue(journal, ann, 100000.0, 20, "2026-04-01", Some(0.15)).unwrap();
    catch_up(journal, ann, "L-002", Some("2026-06-01")).unwrap();
    ann
}

#[test]
fn only_active_loans_leaves_out_a_loan_paid_off_by_the_period_end() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = two_loans(&journal);
    assert_eq!(references(&statement(&journal, ann, "2026-01-01", "2026-06-30")), ["L-001", "L-002"]);
    let active = StatementConfig { loans: LoanScope::Active, ..StatementConfig::default() };
    let s = prepare(&journal, ann, "2026-01-01", "2026-06-30", &active).unwrap();
    assert_eq!(references(&s), ["L-002"]);
    // The totals are the same either way: a paid loan owes nothing.
    assert_eq!(s.net_outstanding, statement(&journal, ann, "2026-01-01", "2026-06-30").net_outstanding);
}

#[test]
fn a_loan_finished_before_the_period_is_not_shown_at_all() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = two_loans(&journal);
    let s = statement(&journal, ann, "2026-05-01", "2026-06-30");
    assert_eq!(references(&s), ["L-002"]);
    assert!(s.loans[0].lines[0].brought_forward);
}

#[test]
fn a_loan_owing_with_no_entries_in_the_period_is_carried_in() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    issue(&journal, ann, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
    catch_up(&journal, ann, "L-001", Some("2026-03-01")).unwrap();
    let loan = journal.loans().by_ref(ann, "L-001").unwrap().unwrap();
    suspensions::record(&journal, loan.id, "2026-04-01", None, None).unwrap();
    let active = StatementConfig { loans: LoanScope::Active, ..StatementConfig::default() };
    let s = prepare(&journal, ann, "2026-05-01", "2026-06-30", &active).unwrap();
    let lines = &s.loans[0].lines;
    assert!(lines[0].brought_forward && lines[0].balance > 0.0);
    assert!(lines[1].annotation.as_deref().is_some_and(|n| n.starts_with("Loan suspended")));
    assert!(!s.is_empty());
}

#[test]
fn a_statement_from_the_first_entry_carries_nothing_in() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = two_loans(&journal);
    funds::post(&journal, funds::Posting::new(Fund::Savings, ann, "2026-01-05", "Deposit", 2000.0, "")).unwrap();
    let s = statement(&journal, ann, "2026-01-01", "2026-06-30");
    assert!(s.loans.iter().flat_map(|l| &l.lines).all(|l| !l.brought_forward));
    assert!(s.savings.iter().all(|l| !l.brought_forward));
}
