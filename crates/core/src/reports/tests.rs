use super::*;
use crate::db::members::MemberDetails;
use crate::funds;
use crate::loans::service::{catch_up, issue};

fn member(journal: &Journal, name: &str) -> i64 {
    journal.members().add(&MemberDetails { name: name.into(), pf_no: format!("PF-{name}"), ..Default::default() }).unwrap()
}

fn go() -> impl FnMut(usize, usize, &str) -> bool {
    |_, _, _| true
}

fn date(text: &str) -> NaiveDate {
    dates::parse(text).unwrap()
}

fn cells(table: &Table, row: usize) -> Vec<String> {
    table.rows[row]
        .iter()
        .map(|v| match v {
            Value::Text(t) => t.clone(),
            Value::Number(n) => n.to_string(),
            Value::Blank => String::new(),
        })
        .collect()
}

#[test]
fn quarters_follow_the_financial_year() {
    let journal = Journal::open_in_memory().unwrap(); // the year starts in November
    let starts = quarter_starts(&journal, date("2026-10-09")).unwrap();
    assert!(starts.iter().all(|d| [11, 2, 5, 8].contains(&d.month()) && d.day() == 1));
    assert_eq!(default_quarter(&journal, date("2026-10-09")).unwrap(), date("2026-08-01"));
    assert_eq!(fy_start(&journal, date("2026-02-14")), date("2025-11-01"));
    assert_eq!(fy_end(&journal, date("2026-02-14")), date("2026-10-31"));
    assert!(check_quarter_start(&journal, "2026-02-01").is_ok());
    assert!(check_quarter_start(&journal, "2026-02-02").unwrap_err().to_string().contains("1st day"));
    let wrong = check_quarter_start(&journal, "2026-03-01").unwrap_err().to_string();
    assert!(wrong.contains("must start in: February, May, August, November"), "{wrong}");
}

#[test]
fn the_interest_report_shows_each_months_interest_and_the_balance_at_quarter_end() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    issue(&journal, ann, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
    catch_up(&journal, ann, "L-001", Some("2026-06-01")).unwrap();

    let table = loan_interest(&journal, "2026-02-01", None, &mut go()).unwrap();
    assert_eq!(table.columns, ["Name", "Loan Amount", "B/F at Feb 2026", "Feb-26", "Mar-26", "Apr-26", "Sub Total", "Grand Total"]);
    // February to April charged 750 each; April's deduction left 85,000.
    assert_eq!(cells(&table, 0), ["Ann", "85000", "0", "750", "750", "750", "2250", "2250"]);
    assert_eq!(table.total.as_ref().unwrap()[0], Value::text("TOTAL"));
    assert_eq!(table.subtitle, "Period: Feb 2026 - Apr 2026");

    // The next quarter brings February to April forward.
    let next = loan_interest(&journal, "2026-05-01", None, &mut go()).unwrap();
    assert_eq!(cells(&next, 0)[1..4], ["75000", "2250", "750"]);
}

#[test]
fn chosen_columns_come_out_in_the_chosen_order() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    issue(&journal, ann, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
    catch_up(&journal, ann, "L-001", Some("2026-04-01")).unwrap();
    let chosen = ["Grand Total".to_string(), "Name".to_string()];
    let table = loan_interest(&journal, "2026-02-01", Some(&chosen), &mut go()).unwrap();
    assert_eq!(table.columns, chosen);
    assert_eq!(cells(&table, 0), ["2250", "Ann"]);
}

#[test]
fn a_report_can_be_stopped() {
    let journal = Journal::open_in_memory().unwrap();
    member(&journal, "Ann");
    let mut stop = |_: usize, _: usize, _: &str| false;
    assert!(matches!(loan_interest(&journal, "2026-02-01", None, &mut stop), Err(Error::Cancelled)));
}

#[test]
fn the_shares_report_carries_the_balance_forward_and_counts_cash_out() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    for (date, kind, amount) in
        [("2025-12-01", "Deposit", 2000.0), ("2026-01-01", "Deposit", 2000.0), ("2026-02-01", "Deposit", 2000.0), ("2026-03-15", "Withdrawal", 500.0)]
    {
        funds::post(&journal, funds::Posting::new(Fund::Savings, ann, date, kind, amount, "")).unwrap();
    }
    let table = savings_quarter(&journal, "2026-02-01", None, &mut go()).unwrap();
    assert_eq!(table.columns[1], "B/F 31ST JAN 2026");
    assert_eq!(cells(&table, 0), ["Ann", "4000", "2000", "0", "0", "2000", "500", "5500"]);
}

#[test]
fn a_fund_report_lists_each_month_of_the_period() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    member(&journal, "Ben"); // never saved at Christmas
    funds::post(&journal, funds::Posting::new(Fund::Christmas, ann, "2026-01-10", "Deposit", 1000.0, "")).unwrap();
    funds::post(&journal, funds::Posting::new(Fund::Christmas, ann, "2026-03-10", "Deposit", 1500.0, "")).unwrap();
    let table = fund_period(&journal, Fund::Christmas, "2026-01-01", "2026-03-31", None, &mut go()).unwrap();
    assert_eq!(table.columns, ["PF No", "Name", "Employment Status", "Jan-26", "Feb-26", "Mar-26", "Total"]);
    assert_eq!(table.rows.len(), 1);
    assert_eq!(cells(&table, 0), ["PF-Ann", "Ann", "Active", "1000", "0", "1500", "2500"]);
    assert_eq!(table.title, "Christmas Report");
    assert!(fund_period(&journal, Fund::Christmas, "2026-03-01", "2026-01-01", None, &mut go()).is_err());
}

#[test]
fn the_members_list_is_by_name_with_the_chosen_columns() {
    let journal = Journal::open_in_memory().unwrap();
    member(&journal, "Zed");
    let ann = member(&journal, "Ann");
    funds::post(&journal, funds::Posting::new(Fund::Savings, ann, "2026-01-10", "Deposit", 1234.5, "")).unwrap();
    let table = members_list(&journal, &["name", "savings", "is_retired"], &mut go()).unwrap();
    assert_eq!(table.columns, ["Name", "Savings Balance", "Retired"]);
    assert_eq!(cells(&table, 0), ["Ann", "1235", "No"]);
    assert_eq!(cells(&table, 1), ["Zed", "0", "No"]);
    assert!(table.total.is_none());
}

#[test]
fn the_arrears_listing_totals_what_is_overdue() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    issue(&journal, ann, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
    let rows = crate::arrears::arrears(&journal, Some("2026-04-15")).unwrap();
    let table = arrears_report(&rows, "2026-04-15").unwrap();
    assert_eq!(table.columns.len(), ARREARS_COLUMNS.len());
    let total = table.total.unwrap();
    assert_eq!(total[0], Value::text("TOTAL — 1 member(s), 1 loan(s)"));
    assert_eq!(total[8], Value::Number(rows[0].overdue_amount));
    assert!(arrears_report(&[], "2026-04-15").is_err());
}
