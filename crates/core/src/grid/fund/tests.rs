use super::*;
use crate::db::members::MemberDetails;
use crate::funds;

const MONTHS: [&str; 3] = ["2026-01", "2026-02", "2026-03"];

fn months() -> Vec<String> {
    MONTHS.iter().map(|m| m.to_string()).collect()
}

fn member(journal: &Journal, name: &str) -> i64 {
    journal.members().add(&MemberDetails { name: name.into(), ..Default::default() }).unwrap()
}

fn grid(journal: &Journal, fund: Fund) -> Grid {
    load(journal, fund, &months(), true, false).unwrap()
}

fn row(grid: &Grid, id: i64) -> &MemberRow {
    grid.members.iter().find(|m| m.member_id == id).unwrap()
}

fn save(journal: &Journal, fund: Fund, id: i64, month: &str, text: &str) -> BatchResult {
    let g = grid(journal, fund);
    let change = stage(&g, row(&g, id), month, text).unwrap().expect("a change");
    apply_changes(journal, fund, &[change]).unwrap()
}

fn rows_of(journal: &Journal, fund: Fund, id: i64) -> Vec<(String, f64)> {
    funds::rows(journal, fund, id).unwrap().into_iter().map(|r| (r.transaction_type, r.amount)).collect()
}

fn put(journal: &Journal, fund: Fund, id: i64, date: &str, kind: &str, amount: f64) {
    journal.funds().add(fund, id, date, kind, amount, "", None).unwrap();
    journal.funds().recalculate(fund, id).unwrap();
}

#[test]
fn a_figure_in_an_empty_month_is_one_deposit() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    let g = grid(&journal, Fund::Savings);
    let change = stage(&g, row(&g, ann), "2026-01", "2,000").unwrap().unwrap();
    assert_eq!((change.kind, change.amount), (ChangeKind::Insert, 2000.0));
    apply_changes(&journal, Fund::Savings, &[change]).unwrap();
    assert_eq!(rows_of(&journal, Fund::Savings, ann), [("Deposit".to_string(), 2000.0)]);
}

#[test]
fn a_bare_figure_sets_the_deposit_and_keeps_the_withdrawal() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    put(&journal, Fund::Savings, ann, "2026-01-01", "Deposit", 2000.0);
    put(&journal, Fund::Savings, ann, "2026-01-15", "Withdrawal", 500.0);
    save(&journal, Fund::Savings, ann, "2026-01", "2,500");
    assert_eq!(rows_of(&journal, Fund::Savings, ann), [("Deposit".into(), 2500.0), ("Withdrawal".into(), 500.0)]);
}

#[test]
fn a_withdrawal_joins_the_months_deposit_without_touching_it() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    put(&journal, Fund::Savings, ann, "2026-01-01", "Deposit", 2000.0);
    let deposit_id = funds::rows(&journal, Fund::Savings, ann).unwrap()[0].id;
    save(&journal, Fund::Savings, ann, "2026-01", "+@W=500");
    let rows = funds::rows(&journal, Fund::Savings, ann).unwrap();
    assert_eq!(rows[0].id, deposit_id);
    assert_eq!(funds::balance(&journal, Fund::Savings, ann).unwrap(), 1500.0);
}

#[test]
fn typing_back_what_is_there_changes_nothing_even_with_a_zero_row() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    put(&journal, Fund::Savings, ann, "2026-01-01", "Deposit", 0.0);
    put(&journal, Fund::Savings, ann, "2026-01-15", "Withdrawal", 500.0);
    let g = grid(&journal, Fund::Savings);
    let shown = operations(Fund::Savings).format(&cell_entries(Fund::Savings, row(&g, ann).cell("2026-01")).unwrap());
    assert_eq!(shown, "@D=0, @W=500");
    assert_eq!(stage(&g, row(&g, ann), "2026-01", &shown).unwrap(), None);
}

#[test]
fn a_month_with_an_interest_posting_is_read_only() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    put(&journal, Fund::Savings, ann, "2026-01-01", "Deposit", 2000.0);
    put(&journal, Fund::Savings, ann, "2026-01-20", "Interest", 50.0);
    let g = grid(&journal, Fund::Savings);
    assert!(stage(&g, row(&g, ann), "2026-01", "3000").unwrap_err().contains("no grid operation"));
}

#[test]
fn undo_restores_a_set_exactly_with_original_ids() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    put(&journal, Fund::Savings, ann, "2026-01-01", "Deposit", 2000.0);
    put(&journal, Fund::Savings, ann, "2026-01-01", "Withdrawal", 500.0);
    let snapshot = |j: &Journal| -> Vec<(i64, String, String, f64)> { j.funds().history(Fund::Savings, ann).unwrap() };
    let before = snapshot(&journal);
    let result = save(&journal, Fund::Savings, ann, "2026-01", "");
    assert!(snapshot(&journal).is_empty());
    revert(&journal, Fund::Savings, &result).unwrap();
    assert_eq!(snapshot(&journal), before);
}

#[test]
fn a_withdrawal_bigger_than_the_savings_is_refused() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    put(&journal, Fund::Savings, ann, "2026-01-01", "Deposit", 2000.0);
    let g = grid(&journal, Fund::Savings);
    let change = stage(&g, row(&g, ann), "2026-02", "-2500").unwrap().unwrap();
    let result = apply_changes(&journal, Fund::Savings, &[change]).unwrap();
    assert!(result.refused[0].reason.contains("overdrawn by 500"));
    assert_eq!(funds::balance(&journal, Fund::Savings, ann).unwrap(), 2000.0);
}

#[test]
fn a_christmas_withdrawal_outside_the_unlock_month_refuses_the_whole_cell() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    put(&journal, Fund::Christmas, ann, "2026-01-01", "Deposit", 1000.0);
    let g = grid(&journal, Fund::Christmas);
    let change = stage(&g, row(&g, ann), "2026-01", "1,000, @D=500, @W=200").unwrap().unwrap();
    let result = apply_changes(&journal, Fund::Christmas, &[change]).unwrap();
    assert!(result.refused[0].reason.contains("only allowed in December"));
    assert_eq!(rows_of(&journal, Fund::Christmas, ann), [("Deposit".into(), 1000.0)]);
}

#[test]
fn an_edit_made_elsewhere_refuses_the_staged_cell() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    put(&journal, Fund::Savings, ann, "2026-01-01", "Deposit", 2000.0);
    let g = grid(&journal, Fund::Savings);
    let change = stage(&g, row(&g, ann), "2026-01", "2,000, @W=500").unwrap().unwrap();
    put(&journal, Fund::Savings, ann, "2026-01-20", "Deposit", 1.0);
    let result = apply_changes(&journal, Fund::Savings, &[change]).unwrap();
    assert!(result.refused[0].reason.contains("changed elsewhere"));
}

#[test]
fn fill_gives_a_withdrawal_month_its_missing_deposit() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    put(&journal, Fund::Savings, ann, "2025-12-01", "Deposit", 2000.0);
    put(&journal, Fund::Savings, ann, "2026-01-20", "Withdrawal", 500.0);
    let g = grid(&journal, Fund::Savings);
    let plan = plan_fill(&journal, &g, &[row(&g, ann)], &months(), Some("2026-03-20")).unwrap();
    assert_eq!(plan.changes.iter().map(|c| c.month.as_str()).collect::<Vec<_>>(), MONTHS);
    assert_eq!((plan.changes[0].kind, plan.changes[0].amount), (ChangeKind::Set, 1500.0));
    apply_changes(&journal, Fund::Savings, &plan.changes).unwrap();
    let january: Vec<(String, f64)> = funds::rows(&journal, Fund::Savings, ann)
        .unwrap()
        .into_iter()
        .filter(|r| r.date.starts_with("2026-01"))
        .map(|r| (r.transaction_type, r.amount))
        .collect();
    assert_eq!(january, [("Deposit".into(), 2000.0), ("Withdrawal".into(), 500.0)]);
}

#[test]
fn fill_does_not_reach_before_the_account_or_into_the_future() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    put(&journal, Fund::Savings, ann, "2026-02-01", "Deposit", 750.0);
    let g = grid(&journal, Fund::Savings);
    let plan = plan_fill(&journal, &g, &[row(&g, ann)], &months(), Some("2026-02-10")).unwrap();
    assert!(plan.changes.is_empty());
    assert_eq!((plan.before_start, plan.in_future), (1, 1));
}

#[test]
fn benevolent_payouts_may_exceed_contributions_and_the_schedule_follows() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    funds::enrol(&journal, ann, 300.0, "2026-01-01").unwrap();
    funds::catch_up_benevolent(&journal, ann, Some("2026-03-31")).unwrap();
    assert_eq!(journal.funds().enrolment(ann).unwrap().unwrap().next_due_date.as_deref(), Some("2026-04-01"));
    let g = grid(&journal, Fund::Benevolent);
    let all: Vec<&MemberRow> = vec![row(&g, ann)];
    let plan = plan_delete_months(&journal, &g, &all, &["2026-03".into()]).unwrap();
    apply_changes(&journal, Fund::Benevolent, &plan.fund_changes).unwrap();
    assert_eq!(journal.funds().enrolment(ann).unwrap().unwrap().next_due_date.as_deref(), Some("2026-03-01"));
    funds::payout(&journal, ann, 10000.0, "2026-02-15", "").unwrap();
    let g = grid(&journal, Fund::Benevolent);
    let plan = plan_delete_months(&journal, &g, &[row(&g, ann)], &["2026-01".into()]).unwrap();
    assert!(plan.refused.is_empty() && !plan.fund_changes.is_empty());
}

#[test]
fn deleting_under_a_withdrawal_it_would_overdraw_is_refused_per_member() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann");
    let ben = member(&journal, "Ben");
    for id in [ann, ben] {
        for m in ["01", "02", "03"] {
            put(&journal, Fund::Savings, id, &format!("2026-{m}-01"), "Deposit", 2000.0);
        }
    }
    put(&journal, Fund::Savings, ann, "2026-03-15", "Withdrawal", 5000.0);
    let g = grid(&journal, Fund::Savings);
    let rows: Vec<&MemberRow> = g.members.iter().collect();
    let plan = plan_delete_months(&journal, &g, &rows, &["2026-01".into(), "2026-02".into()]).unwrap();
    assert_eq!(plan.refused.len(), 1);
    assert!(plan.refused[0].1.contains("overdrawn by 3,000"), "{}", plan.refused[0].1);
    assert!(plan.fund_changes.iter().all(|c| c.member_id == ben));
    assert_eq!(plan.tally_of("deposits"), (2, 4000.0));
}
