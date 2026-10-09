//! The loans × months grid. A loan's rows are the consequence of a schedule,
//! so most of these hold a grid figure to what the engine itself produces.
//! The deletion tests reproduce docs/grid/deleting-months.md §6 to the
//! shilling: 100,000 at 15% over 20 months — 5,750 a month, 750 of it
//! interest — issued in January and deducted from February.

use super::*;
use crate::db::ledger::NewEntry;
use crate::db::members::MemberDetails;
use crate::db::model::Loan;
use crate::loans::projection::replay_matches;
use crate::loans::service::{buyoff, catch_up, deduct, issue, top_up};

/// Past every month the tests look at.
const LATER: &str = "2026-09-15";

fn months() -> Vec<String> {
    dates::month_range("2026-01", "2026-08")
}

fn member(journal: &Journal, name: &str) -> i64 {
    journal.members().add(&MemberDetails { name: name.into(), ..Default::default() }).unwrap()
}

/// A loan of `principal` over `term` months at 15%, issued on `issued` and
/// deducted through `through`.
fn loan_of(journal: &Journal, name: &str, principal: f64, term: i64, issued: &str, through: Option<&str>) -> i64 {
    let id = member(journal, name);
    issue(journal, id, principal, term, issued, Some(0.15)).unwrap();
    if let Some(through) = through {
        catch_up(journal, id, "L-001", Some(through)).unwrap();
    }
    id
}

/// §6's loan, deducted February through `through`.
fn loan(journal: &Journal, name: &str, through: &str) -> i64 {
    loan_of(journal, name, 100000.0, 20, "2026-01-01", Some(through))
}

fn grid(journal: &Journal, paid: bool) -> LoanGrid {
    load(journal, &months(), true, paid, false).unwrap()
}

fn row(grid: &LoanGrid, id: i64) -> &LoanRow {
    grid.rows.iter().find(|r| r.member_id == id && r.reference == "L-001").unwrap()
}

fn stage_text(journal: &Journal, id: i64, month: &str, text: &str) -> std::result::Result<Option<Change>, String> {
    let g = grid(journal, true);
    stage(&g, row(&g, id), month, text)
}

fn save(journal: &Journal, id: i64, month: &str, text: &str) -> BatchResult {
    let change = stage_text(journal, id, month, text).unwrap().expect("a change");
    apply_changes(journal, &[change]).unwrap()
}

fn delete_months(journal: &Journal, id: i64, months: &[&str], paid: bool) -> (DeletePlan, BatchResult) {
    let g = grid(journal, paid);
    let months: Vec<String> = months.iter().map(|m| m.to_string()).collect();
    let plan = plan_delete_months(journal, &[row(&g, id)], &months).unwrap();
    let result = apply_changes(journal, &plan.loan_changes).unwrap();
    (plan, result)
}

fn the_loan(journal: &Journal, id: i64) -> Loan {
    journal.loans().by_ref(id, "L-001").unwrap().unwrap()
}

/// `(date, event, deducted, principal portion, interest portion)`, in order.
fn ledger(journal: &Journal, id: i64) -> Vec<(String, String, f64, f64, f64)> {
    let mut stmt = journal
        .conn()
        .prepare(
            "SELECT date, event_type, deducted, principal_portion, interest_portion FROM ledger \
             WHERE individual_id=? AND loan_id='L-001' ORDER BY date, id",
        )
        .unwrap();
    stmt.query_map([id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))
        .unwrap()
        .map(|r| r.unwrap())
        .collect()
}

/// `(date, deducted, interest portion, principal portion)` of each deduction.
fn payments(journal: &Journal, id: i64) -> Vec<(String, f64, f64, f64)> {
    ledger(journal, id)
        .into_iter()
        .filter(|r| r.1 == event::REPAYMENT)
        .map(|(date, _, deducted, principal, interest)| (date, deducted, interest, principal))
        .collect()
}

fn charges(journal: &Journal, id: i64) -> Vec<String> {
    ledger(journal, id).into_iter().filter(|r| r.1 == event::INTEREST).map(|r| r.0).collect()
}

/// §4.5: interest charged plus unearned is what the loan was written for.
fn conserved(journal: &Journal, id: i64, written: f64) -> bool {
    let charged: f64 = journal
        .conn()
        .query_row(
            "SELECT COALESCE(SUM(added), 0) FROM ledger WHERE individual_id=? AND loan_id='L-001' \
             AND event_type='Interest Earned'",
            [id],
            |r| r.get(0),
        )
        .unwrap();
    round2(charged + the_loan(journal, id).unearned_interest) == written
}

/// Every ledger and loan column, bar `loans.updated_at` — an undo is a write
/// like any other.
fn everything(journal: &Journal) -> (Vec<Vec<String>>, Vec<Vec<String>>) {
    let dump = |table: &str| -> Vec<Vec<String>> {
        let mut stmt = journal.conn().prepare(&format!("SELECT * FROM {table} ORDER BY id")).unwrap();
        let names: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
        stmt.query_map([], |r| {
            Ok((0..names.len())
                .filter(|i| names[*i] != "updated_at")
                .map(|i| format!("{:?}", r.get::<_, rusqlite::types::Value>(i).unwrap()))
                .collect())
        })
        .unwrap()
        .map(|r| r.unwrap())
        .collect()
    };
    (dump("ledger"), dump("loans"))
}

fn audit_entries(journal: &Journal) -> Vec<(String, String)> {
    let mut stmt = journal.conn().prepare("SELECT operation, entity FROM audit_log ORDER BY id").unwrap();
    stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?))).unwrap().map(|r| r.unwrap()).collect()
}

// ------------------------------------------------------------------ shape

#[test]
fn a_row_is_a_loan_not_a_member() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    issue(&journal, ann, 50000.0, 20, "2026-01-01", Some(0.15)).unwrap();
    let g = grid(&journal, false);
    let refs: Vec<&str> = g.rows.iter().map(|r| r.reference.as_str()).collect();
    assert_eq!(refs, ["L-001", "L-002"]);
    assert!(g.rows.iter().all(|r| r.member_id == ann));
}

#[test]
fn deductions_land_in_their_month() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    let g = grid(&journal, false);
    let r = row(&g, ann);
    assert_eq!((r.cell("2026-02").net, r.cell("2026-03").net), (5750.0, 5750.0));
    assert!(r.cell("2026-04").rows.is_empty());
    assert_eq!(edit_text(r, "2026-03"), "5,750");
    assert_eq!(edit_text(r, "2026-04"), "");
}

#[test]
fn paid_loans_are_left_out_unless_asked_for() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan_of(&journal, "Ann", 5000.0, 1, "2026-01-01", Some("2026-02-01"));
    assert_eq!(the_loan(&journal, ann).status, LOAN_PAID);
    assert!(grid(&journal, false).rows.is_empty());
    assert_eq!(grid(&journal, true).rows.len(), 1);
}

// ------------------------------------------------- what a cell refuses

#[test]
fn the_next_due_month_takes_a_deduction() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    let g = grid(&journal, false);
    assert_eq!(row(&g, ann).next_due_month().as_deref(), Some("2026-04"));
    assert_eq!(cell_refusal(&g, row(&g, ann), "2026-04"), None);
}

#[test]
fn a_month_beyond_the_due_date_names_the_month_that_is_owed() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    let g = grid(&journal, false);
    assert!(cell_refusal(&g, row(&g, ann), "2026-06").unwrap().contains("due for Apr 2026 first"));
}

#[test]
fn a_skipped_past_month_cannot_be_back_filled() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-04-01");
    journal.conn().execute("DELETE FROM ledger WHERE individual_id=? AND date LIKE '2026-03%'", [ann]).unwrap();
    let g = grid(&journal, false);
    assert!(cell_refusal(&g, row(&g, ann), "2026-03").unwrap().contains("passed over"));
}

#[test]
fn a_month_before_the_loan_existed_is_refused() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan_of(&journal, "Ann", 100000.0, 20, "2026-03-01", None);
    let g = grid(&journal, false);
    assert!(cell_refusal(&g, row(&g, ann), "2026-01").unwrap().contains("not issued until Mar 2026"));
}

#[test]
fn a_suspended_month_is_refused_and_says_so() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-02-01");
    suspensions::record(&journal, the_loan(&journal, ann).id, "2026-03-01", None, Some("2026-03-01")).unwrap();
    let g = grid(&journal, false);
    assert!(cell_refusal(&g, row(&g, ann), "2026-03").unwrap().contains("suspended"));
}

#[test]
fn a_month_with_two_payments_is_locked() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-02-01");
    journal
        .ledger()
        .add(&NewEntry {
            member_id: ann,
            date: "2026-02-20".into(),
            event_type: event::REPAYMENT.into(),
            loan_ref: "L-001".into(),
            deducted: 1000.0,
            notes: "extra".into(),
            ..NewEntry::default()
        })
        .unwrap();
    let g = grid(&journal, false);
    assert!(cell_refusal(&g, row(&g, ann), "2026-02").unwrap().contains("more than one payment"));
}

#[test]
fn a_buyoff_is_not_editable_as_a_deduction() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-02-01");
    buyoff(&journal, ann, "L-001", Some("2026-03-01")).unwrap();
    let g = grid(&journal, true);
    assert!(cell_refusal(&g, row(&g, ann), "2026-03").unwrap().contains("buy-off"));
}

// ------------------------------------------------------ taking a deduction

#[test]
fn a_grid_deduction_equals_what_the_engine_produces() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    let result = save(&journal, ann, "2026-04", "5750");
    assert_eq!(result.count(ChangeKind::Deduct), 1);

    let engine = Journal::open_in_memory().unwrap();
    let ben = loan(&engine, "Ann", "2026-04-01");
    assert_eq!(ledger(&journal, ann), ledger(&engine, ben));
    let (got, want) = (the_loan(&journal, ann), the_loan(&engine, ben));
    assert_eq!((got.balance, got.next_due_date), (want.balance, want.next_due_date));
}

#[test]
fn a_deduction_accrues_that_months_interest() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    save(&journal, ann, "2026-04", "5750");
    let april: Vec<String> = ledger(&journal, ann).into_iter().filter(|r| r.0.starts_with("2026-04")).map(|r| r.1).collect();
    assert_eq!(april, [event::INTEREST, event::REPAYMENT]);
    assert_eq!(the_loan(&journal, ann).balance, 85000.0);
}

#[test]
fn a_figure_other_than_the_instalment_is_honoured_and_split() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    save(&journal, ann, "2026-04", "9,000");
    let april = payments(&journal, ann).into_iter().find(|p| p.0.starts_with("2026-04")).unwrap();
    assert_eq!((april.1, april.2 + april.3), (9000.0, 9000.0));
    // 750 of interest accrued that month, so 8,250 came off principal.
    assert_eq!(the_loan(&journal, ann).balance, 90000.0 - 8250.0);
}

#[test]
fn several_months_for_one_loan_are_taken_in_order() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-02-01");
    let g = grid(&journal, false);
    let march = stage(&g, row(&g, ann), "2026-03", "5750").unwrap().unwrap();
    let mut april = march.clone();
    april.month = "2026-04".into();
    // Handed over newest first on purpose: the batch takes them in order.
    let result = apply_changes(&journal, &[april, march]).unwrap();
    assert_eq!(result.count(ChangeKind::Deduct), 2, "{:?}", result.refused);
    let dates: Vec<String> = payments(&journal, ann).into_iter().map(|p| p.0).collect();
    assert_eq!(dates, ["2026-02-01", "2026-03-01", "2026-04-01"]);
}

#[test]
fn a_corrected_figure_is_seen_by_the_next_month_in_the_same_save() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-02-01");
    let g = grid(&journal, false);
    let march = stage(&g, row(&g, ann), "2026-03", "9000").unwrap().unwrap();
    let mut april = stage(&g, row(&g, ann), "2026-03", "5750").unwrap().unwrap();
    april.month = "2026-04".into();
    apply_changes(&journal, &[march, april]).unwrap();

    let one_at_a_time = Journal::open_in_memory().unwrap();
    let ben = loan(&one_at_a_time, "Ann", "2026-02-01");
    save(&one_at_a_time, ben, "2026-03", "9000");
    save(&one_at_a_time, ben, "2026-04", "5750");
    assert_eq!(ledger(&journal, ann), ledger(&one_at_a_time, ben));
    assert_eq!(the_loan(&journal, ann).balance, the_loan(&one_at_a_time, ben).balance);
}

#[test]
fn a_member_who_left_takes_no_new_deduction() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    journal.members().set_status(ann, "Resigned", Some("2026-03-15")).unwrap();
    let result = save(&journal, ann, "2026-04", "5750");
    assert_eq!(result.count(ChangeKind::Deduct), 0);
    assert!(result.refused[0].reason.contains("has resigned"));
}

#[test]
fn the_final_deduction_takes_only_what_is_owed() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    journal.conn().execute("UPDATE loans SET installment=200000 WHERE individual_id=?", [ann]).unwrap();
    let before = the_loan(&journal, ann);
    let owed = before.balance + before.interest_balance + before.monthly_interest.min(before.unearned_interest);
    let result = save(&journal, ann, "2026-04", "200,000");
    assert!(result.refused.is_empty());
    assert_eq!(payments(&journal, ann).last().unwrap().1, round2(owed));
    assert_eq!(the_loan(&journal, ann).status, LOAN_PAID);
}

// ------------------------------------------------------------ corrections

#[test]
fn correcting_an_amount_re_derives_the_split() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    let change = stage_text(&journal, ann, "2026-03", "9000").unwrap().unwrap();
    assert_eq!(change.kind, ChangeKind::Update);
    apply_changes(&journal, &[change]).unwrap();
    let march = payments(&journal, ann).into_iter().find(|p| p.0.starts_with("2026-03")).unwrap();
    assert_eq!((march.1, round2(march.2 + march.3)), (9000.0, 9000.0));
    assert_eq!(the_loan(&journal, ann).balance, 100000.0 - 5000.0 - 8250.0);
}

#[test]
fn typing_the_same_figure_is_not_a_change() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    assert_eq!(stage_text(&journal, ann, "2026-03", "5,750"), Ok(None));
}

#[test]
fn correcting_is_allowed_for_a_member_who_left() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    journal.members().set_status(ann, "Retired", Some("2026-03-15")).unwrap();
    assert_eq!(save(&journal, ann, "2026-03", "6000").count(ChangeKind::Update), 1);
}

// ------------------------------------------------------------ operations

#[test]
fn a_top_up_runs_the_top_up_and_undoes_cleanly() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    let before = the_loan(&journal, ann);
    let change = stage_text(&journal, ann, "2026-04", "@T=20,000/12").unwrap().unwrap();
    assert_eq!((change.kind, change.amount, change.months), (ChangeKind::TopUp, 20000.0, Some(12)));
    assert_eq!(staged_text(&change).as_deref(), Some("@T=20,000/12"));

    let result = apply_changes(&journal, &[change]).unwrap();
    assert!(result.refused.is_empty(), "{:?}", result.refused);
    assert!(result.summary().contains("1 topped up"));
    let after = the_loan(&journal, ann);
    assert_eq!((after.balance, after.duration_months), (before.balance + 20000.0, Some(12)));
    let top_ups: Vec<(String, f64)> = journal
        .conn()
        .prepare("SELECT date, added FROM ledger WHERE event_type='Loan Top-Up'")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
        .unwrap()
        .map(|r| r.unwrap())
        .collect();
    assert_eq!(top_ups, [("2026-04-01".to_string(), 20000.0)]);

    revert(&journal, &result).unwrap();
    let restored = the_loan(&journal, ann);
    assert_eq!((restored.balance, restored.duration_months), (before.balance, before.duration_months));
    assert!(ledger(&journal, ann).iter().all(|r| r.1 != event::TOP_UP));
}

#[test]
fn a_buyoff_settles_the_loan_and_undoes_cleanly() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    let before = everything(&journal);
    let result = save(&journal, ann, "2026-04", "@B");
    assert!(result.refused.is_empty(), "{:?}", result.refused);
    assert_eq!(the_loan(&journal, ann).status, LOAN_PAID);
    assert!(ledger(&journal, ann).iter().any(|r| r.1 == event::BUYOFF && r.0 == "2026-04-01"));

    revert(&journal, &result).unwrap();
    assert_eq!(the_loan(&journal, ann).status, LOAN_ACTIVE);
    assert_eq!(everything(&journal), before);
}

#[test]
fn a_suspension_locks_its_months_and_undoes_cleanly() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    let change = stage_text(&journal, ann, "2026-04", "@S=2").unwrap().unwrap();
    assert_eq!((change.kind, change.months), (ChangeKind::Suspend, Some(2)));
    let result = apply_changes(&journal, &[change]).unwrap();
    assert!(result.refused.is_empty(), "{:?}", result.refused);

    let spans = journal.loans().suspensions_for_loan(the_loan(&journal, ann).id).unwrap();
    let spans: Vec<(String, Option<String>)> = spans.into_iter().map(|s| (s.start_date, s.suspend_until)).collect();
    assert_eq!(spans, [("2026-04-01".to_string(), Some("2026-06-01".to_string()))]);
    assert!(stage_text(&journal, ann, "2026-04", "5750").unwrap_err().contains("suspended"));

    revert(&journal, &result).unwrap();
    assert!(journal.loans().suspensions_for_loan(the_loan(&journal, ann).id).unwrap().is_empty());
}

#[test]
fn an_operation_only_goes_in_an_empty_due_month() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    assert!(stage_text(&journal, ann, "2026-03", "@B").unwrap_err().contains("already has its deduction"));
    assert!(stage_text(&journal, ann, "2026-05", "@B").unwrap_err().contains("due for Apr 2026 first"));
}

#[test]
fn a_loan_month_takes_one_thing() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    assert!(stage_text(&journal, ann, "2026-04", "5750, @S=1").unwrap_err().contains("one thing"));
}

#[test]
fn a_bare_zero_on_a_loan_is_still_refused() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    assert!(stage_text(&journal, ann, "2026-04", "0").unwrap_err().contains("more than zero"));
}

#[test]
fn a_deduction_staged_behind_a_buyoff_is_refused_not_misplaced() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    let buy = stage_text(&journal, ann, "2026-04", "@B").unwrap().unwrap();
    let later = Change { kind: ChangeKind::Deduct, month: "2026-05".into(), amount: 5750.0, ..buy.clone() };
    let result = apply_changes(&journal, &[buy, later]).unwrap();
    let applied: Vec<ChangeKind> = result.applied.iter().map(|a| a.change.kind).collect();
    assert_eq!(applied, [ChangeKind::Buyoff]);
    assert!(result.refused[0].reason.contains("paid now"));
}

// ------------------------------------- suspensions judged on the due date

/// A loan due on the 28th — where testing the 1st of the month goes wrong.
fn due_28th(journal: &Journal) -> (i64, i64) {
    let ann = loan_of(journal, "Ann", 100000.0, 20, "2026-01-28", Some("2026-03-28"));
    (ann, the_loan(journal, ann).id)
}

fn deduction_dates_since(journal: &Journal, id: i64, since: &str) -> Vec<String> {
    payments(journal, id).into_iter().map(|p| p.0).filter(|d| d.as_str() >= since).collect()
}

#[test]
fn a_live_suspension_skips_its_months_and_resumes_on_its_end_date() {
    let journal = Journal::open_in_memory().unwrap();
    let (ann, loan_id) = due_28th(&journal);
    suspensions::record(&journal, loan_id, "2026-04-28", Some("2026-06-28"), Some("2026-04-01")).unwrap();

    let g = grid(&journal, false);
    let r = row(&g, ann);
    assert!(cell_refusal(&g, r, "2026-04").unwrap().contains("resume from 2026-06-28"));
    assert!(cell_refusal(&g, r, "2026-05").is_some());
    assert_eq!(cell_refusal(&g, r, "2026-06"), None);
    assert_eq!(r.next_due_month().as_deref(), Some("2026-06"));

    let plan = plan_fill(&g, &[r], &months(), Some("2026-06-30"));
    let planned: Vec<&str> = plan.changes.iter().map(|c| c.month.as_str()).collect();
    assert_eq!((planned, plan.suspended), (vec!["2026-06"], 2));
    let result = apply_changes(&journal, &plan.changes).unwrap();
    assert!(result.refused.is_empty(), "{:?}", result.refused);
    assert_eq!(deduction_dates_since(&journal, ann, "2026-04-01"), ["2026-06-28"]);
    assert!(!the_loan(&journal, ann).is_suspended);
}

#[test]
fn a_suspension_set_from_the_grid_skips_the_due_month() {
    let journal = Journal::open_in_memory().unwrap();
    let (ann, _) = due_28th(&journal);
    save(&journal, ann, "2026-04", "@S=2");
    let g = grid(&journal, false);
    let open: Vec<bool> =
        ["2026-04", "2026-05", "2026-06"].iter().map(|m| cell_refusal(&g, row(&g, ann), m).is_none()).collect();
    assert_eq!(open, [false, false, true]);
}

// ------------------------------------------------------------ fill forward

#[test]
fn fill_matches_catch_up_exactly() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-02-01");
    let months = dates::month_range("2026-01", "2026-06");
    let g = load(&journal, &months, true, false, false).unwrap();
    let plan = plan_fill(&g, &g.rows.iter().collect::<Vec<_>>(), &months, Some(LATER));
    apply_changes(&journal, &plan.changes).unwrap();

    let engine = Journal::open_in_memory().unwrap();
    let ben = loan(&engine, "Ann", "2026-02-01");
    catch_up(&engine, ben, "L-001", Some("2026-06-30")).unwrap();
    assert_eq!(ledger(&journal, ann), ledger(&engine, ben));
}

#[test]
fn fill_does_not_invent_the_future() {
    let journal = Journal::open_in_memory().unwrap();
    loan(&journal, "Ann", "2026-02-01");
    let months = dates::month_range("2026-01", "2026-12");
    let g = load(&journal, &months, true, false, false).unwrap();
    let plan = plan_fill(&g, &g.rows.iter().collect::<Vec<_>>(), &months, Some("2026-06-15"));
    let planned: Vec<&str> = plan.changes.iter().map(|c| c.month.as_str()).collect();
    assert_eq!(planned, ["2026-03", "2026-04", "2026-05", "2026-06"]);
    assert_eq!(plan.in_future, 6);
    assert!(plan.notes().join(" ").contains("in the future"));
}

#[test]
fn fill_skips_suspended_months() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-02-01");
    // A past, closed suspension: those months were deliberately skipped.
    suspensions::record(&journal, the_loan(&journal, ann).id, "2026-03-01", Some("2026-05-01"), Some(LATER)).unwrap();
    let g = grid(&journal, false);
    let months = dates::month_range("2026-01", "2026-06");
    let plan = plan_fill(&g, &[row(&g, ann)], &months, Some(LATER));
    let planned: Vec<&str> = plan.changes.iter().map(|c| c.month.as_str()).collect();
    assert_eq!((planned, plan.suspended), (vec!["2026-05", "2026-06"], 2));
}

#[test]
fn fill_skips_a_live_suspended_loan_entirely() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-02-01");
    suspensions::record(&journal, the_loan(&journal, ann).id, "2026-03-01", None, Some("2026-03-01")).unwrap();
    let g = grid(&journal, false);
    let plan = plan_fill(&g, &[row(&g, ann)], &months(), Some(LATER));
    assert!(plan.changes.is_empty());
    assert!(plan.skipped[0].1.contains("suspended"));
}

#[test]
fn fill_skips_members_who_have_left() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-02-01");
    journal.members().set_status(ann, "Deceased", Some("2026-02-15")).unwrap();
    let g = grid(&journal, false);
    let plan = plan_fill(&g, &[row(&g, ann)], &months(), Some(LATER));
    assert!(plan.changes.is_empty());
    assert!(plan.skipped[0].1.contains("has left"));
}

#[test]
fn fill_says_when_a_loan_is_already_up_to_date() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-08-01");
    let g = grid(&journal, false);
    let plan = plan_fill(&g, &[row(&g, ann)], &months(), Some(LATER));
    assert!(plan.changes.is_empty());
    assert!(plan.skipped[0].1.contains("up to date"));
}

#[test]
fn fill_across_many_loans_is_the_mass_deduction() {
    let journal = Journal::open_in_memory().unwrap();
    for i in 0..5 {
        loan(&journal, &format!("M{i}"), "2026-02-01");
    }
    let months = dates::month_range("2026-01", "2026-06");
    let g = load(&journal, &months, true, false, false).unwrap();
    let plan = plan_fill(&g, &g.rows.iter().collect::<Vec<_>>(), &months, Some(LATER));
    let result = apply_changes(&journal, &plan.changes).unwrap();
    assert_eq!(result.count(ChangeKind::Deduct), 5 * 4);
    assert!(result.refused.is_empty());
    let g = load(&journal, &months, true, false, false).unwrap();
    assert!(g.rows.iter().all(|r| r.next_due_month().as_deref() == Some("2026-07")));
}

#[test]
fn a_loan_that_pays_off_mid_fill_stops_there() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan_of(&journal, "Ann", 12000.0, 2, "2026-01-01", None);
    let g = grid(&journal, false);
    let plan = plan_fill(&g, &[row(&g, ann)], &months(), Some(LATER));
    let result = apply_changes(&journal, &plan.changes).unwrap();
    assert_eq!(the_loan(&journal, ann).status, LOAN_PAID);
    assert_eq!(result.count(ChangeKind::Deduct), 2);
    assert_eq!(result.count(ChangeKind::Deduct) + result.refused.len(), plan.changes.len());
    assert!(result.refused.iter().all(|r| r.reason.contains("paid now")));
}

#[test]
fn the_summary_reads_as_a_sentence() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    assert_eq!(save(&journal, ann, "2026-04", "5750").summary(), "1 deducted");
}

// -------------------------------------------------------- who is in it

#[test]
fn an_excluded_member_drops_out_of_the_loan_grid_only() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    journal.members().set_excluded(ann, "savings", true, "").unwrap();
    assert_eq!(grid(&journal, false).rows.len(), 1);
    journal.members().set_excluded(ann, "loans", true, "").unwrap();
    assert!(grid(&journal, false).rows.is_empty());
    let g = load(&journal, &months(), true, false, true).unwrap();
    assert_eq!(g.rows.len(), 1);
    let plan = plan_fill(&g, &[row(&g, ann)], &months(), Some(LATER));
    assert!(plan.changes.is_empty() && plan.skipped[0].1.contains("excluded"));
}

// ----------------------------------------- deleting months (docs §4, §6)

#[test]
fn the_fixture_is_the_worked_example() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let dates: Vec<String> = payments(&journal, ann).into_iter().map(|p| p.0).collect();
    assert_eq!(dates, ["2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01", "2026-06-01"]);
    let l = the_loan(&journal, ann);
    assert_eq!((l.balance, l.unearned_interest), (75000.0, 11250.0));
}

#[test]
fn deleting_the_last_month_un_runs_it() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let (plan, result) = delete_months(&journal, ann, &["2026-06"], false);
    assert_eq!(plan.loan_changes.iter().map(|c| c.removal).collect::<Vec<_>>(), [Some(Removal::UnRun)]);
    assert!(result.refused.is_empty());
    let l = the_loan(&journal, ann);
    assert_eq!((l.balance, l.unearned_interest, l.next_due_date.as_deref()), (80000.0, 12000.0, Some("2026-06-01")));
    assert!(!charges(&journal, ann).contains(&"2026-06-01".to_string()));
    assert!(conserved(&journal, ann, 15000.0));
}

#[test]
fn an_un_run_month_is_taken_again_by_the_next_deduction() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let before = payments(&journal, ann);
    delete_months(&journal, ann, &["2026-06"], false);
    deduct(&journal, ann, "L-001").unwrap();
    assert_eq!(payments(&journal, ann), before);
    assert!(conserved(&journal, ann, 15000.0));
}

#[test]
fn deleting_the_last_three_months_un_runs_all_three() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let (plan, _) = delete_months(&journal, ann, &["2026-04", "2026-05", "2026-06"], false);
    assert!(plan.loan_changes.iter().all(|c| c.removal == Some(Removal::UnRun)));
    let l = the_loan(&journal, ann);
    assert_eq!((l.balance, l.next_due_date.as_deref(), l.unearned_interest), (90000.0, Some("2026-04-01"), 13500.0));
    assert!(conserved(&journal, ann, 15000.0));
}

#[test]
fn deleting_an_earlier_month_records_it_missed() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let (plan, result) = delete_months(&journal, ann, &["2026-03"], false);
    assert_eq!(plan.loan_changes.iter().map(|c| c.removal).collect::<Vec<_>>(), [Some(Removal::Missed)]);
    assert!(result.refused.is_empty());
    assert!(charges(&journal, ann).contains(&"2026-03-01".to_string()));
    let p = |d: &str, deducted, interest, principal| (d.to_string(), deducted, interest, principal);
    assert_eq!(
        payments(&journal, ann),
        [
            p("2026-02-01", 5750.0, 750.0, 5000.0),
            p("2026-04-01", 5750.0, 1500.0, 4250.0), // settles March first
            p("2026-05-01", 5750.0, 750.0, 5000.0),
            p("2026-06-01", 5750.0, 750.0, 5000.0),
        ]
    );
    let l = the_loan(&journal, ann);
    assert_eq!((l.balance, l.next_due_date.as_deref(), l.unearned_interest), (80750.0, Some("2026-07-01"), 11250.0));
    assert!(conserved(&journal, ann, 15000.0));
    assert!(replay_matches(&journal, ann, "L-001").unwrap().0);
}

#[test]
fn a_missed_loan_is_collected_after_its_last_scheduled_month() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let twin = loan(&journal, "Ben", "2026-06-01");
    delete_months(&journal, ann, &["2026-03"], false);
    for id in [ann, twin] {
        catch_up(&journal, id, "L-001", Some("2028-12-01")).unwrap();
    }
    let paid = payments(&journal, ann);
    assert_eq!(the_loan(&journal, ann).status, LOAN_PAID);
    assert_eq!(round2(paid.iter().map(|p| p.3).sum()), 100000.0);
    assert_eq!(round2(paid.iter().map(|p| p.2).sum()), 15000.0);
    assert_eq!((paid.len(), payments(&journal, twin).len()), (20, 20));
    assert_eq!(payments(&journal, twin).last().unwrap().0, "2027-09-01");
    assert_eq!(paid.last().unwrap().0, "2027-10-01");
}

#[test]
fn un_run_and_missed_in_one_batch() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let (plan, _) = delete_months(&journal, ann, &["2026-03", "2026-06"], false);
    let kinds: Vec<(&str, Option<Removal>)> = plan.loan_changes.iter().map(|c| (c.month.as_str(), c.removal)).collect();
    assert_eq!(kinds, [("2026-03", Some(Removal::Missed)), ("2026-06", Some(Removal::UnRun))]);
    let l = the_loan(&journal, ann);
    assert_eq!((l.next_due_date.as_deref(), l.balance), (Some("2026-06-01"), 85750.0));
    assert!(conserved(&journal, ann, 15000.0));
}

#[test]
fn deleting_a_paid_loans_deduction_reopens_it() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan_of(&journal, "Ann", 10000.0, 2, "2026-01-01", Some("2026-06-01"));
    assert_eq!(the_loan(&journal, ann).status, LOAN_PAID);
    let (plan, result) = delete_months(&journal, ann, &["2026-02"], true);
    assert!(result.refused.is_empty());
    assert!(plan.consequences.iter().any(|c| c.contains("1 paid-off loan(s) reopen")));
    let l = the_loan(&journal, ann);
    assert!(l.status == LOAN_ACTIVE && l.balance > 0.0);
}

#[test]
fn a_top_up_in_the_month_is_not_deleted() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-04-01");
    top_up(&journal, ann, "L-001", 20000.0, 20, Some("2026-05-01")).unwrap();
    deduct(&journal, ann, "L-001").unwrap();
    let (plan, _) = delete_months(&journal, ann, &["2026-05"], false);
    assert!(plan.untouched.iter().any(|line| line.contains("not deleted")));
    let may: Vec<String> = ledger(&journal, ann).into_iter().filter(|r| r.0.starts_with("2026-05")).map(|r| r.1).collect();
    assert_eq!(may, [event::TOP_UP]);
}

#[test]
fn the_last_deduction_before_a_top_up_is_refused() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-04-01");
    top_up(&journal, ann, "L-001", 20000.0, 20, Some("2026-05-01")).unwrap();
    assert!(stage_text(&journal, ann, "2026-04", "").unwrap_err().contains("top-up on 2026-05-01"));
}

#[test]
fn a_deduction_before_a_top_up_with_later_deductions_is_missed() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-04-01");
    top_up(&journal, ann, "L-001", 20000.0, 20, Some("2026-05-01")).unwrap();
    deduct(&journal, ann, "L-001").unwrap();
    let (plan, result) = delete_months(&journal, ann, &["2026-04"], false);
    assert_eq!(plan.loan_changes.iter().map(|c| c.removal).collect::<Vec<_>>(), [Some(Removal::Missed)]);
    assert!(result.refused.is_empty());
    assert!(replay_matches(&journal, ann, "L-001").unwrap().0);
}

#[test]
fn a_loans_deletions_are_all_or_nothing() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-04-01");
    top_up(&journal, ann, "L-001", 20000.0, 20, Some("2026-05-01")).unwrap();
    deduct(&journal, ann, "L-001").unwrap();
    let before = payments(&journal, ann);

    // May un-runs, which leaves April the last deduction before the top-up.
    let (plan, _) = delete_months(&journal, ann, &["2026-04", "2026-05"], false);
    assert!(plan.loan_changes.is_empty() && plan.refused[0].1.contains("top-up"));

    // Staged by hand anyway, the save refuses the loan as a whole.
    let g = grid(&journal, false);
    let r = row(&g, ann);
    let changes: Vec<Change> = ["2026-04", "2026-05"]
        .iter()
        .map(|m| Change { row_id: r.cell(m).row_id(), ..Change::new(r, m, ChangeKind::Delete) })
        .collect();
    let result = apply_changes(&journal, &changes).unwrap();
    assert_eq!((result.refused.len(), result.applied.len()), (2, 0));
    assert_eq!(payments(&journal, ann), before);
}

#[test]
fn a_month_behind_a_buy_off_is_refused() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    buyoff(&journal, ann, "L-001", Some("2026-07-01")).unwrap();
    let g = grid(&journal, true);
    let r = row(&g, ann);
    assert!(stage(&g, r, "2026-03", "").unwrap_err().contains("bought off on 2026-07-01"));
    assert!(cell_refusal(&g, r, "2026-07").unwrap().contains("buy-off"));
    let plan = plan_delete_months(&journal, &[r], &["2026-03".into(), "2026-07".into()]).unwrap();
    assert!(plan.loan_changes.is_empty() && !plan.refused.is_empty());
}

#[test]
fn interest_is_conserved_through_a_grid_deduction_and_its_undo() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    let result = save(&journal, ann, "2026-04", "5750");
    revert(&journal, &result).unwrap();
    assert!(conserved(&journal, ann, 15000.0));
    assert_eq!(the_loan(&journal, ann).unearned_interest, 13500.0);
}

#[test]
fn interest_is_conserved_through_a_deletion_and_its_undo() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let (_, result) = delete_months(&journal, ann, &["2026-05", "2026-06"], false);
    assert!(conserved(&journal, ann, 15000.0));
    revert(&journal, &result).unwrap();
    assert!(conserved(&journal, ann, 15000.0));
}

#[test]
fn a_missed_month_can_take_its_deduction_again() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let original = payments(&journal, ann);
    delete_months(&journal, ann, &["2026-03"], false);

    let g = grid(&journal, false);
    let r = row(&g, ann);
    assert!(r.missed("2026-03") && cell_refusal(&g, r, "2026-03").is_none());
    assert!(cell_note(r, "2026-03").unwrap().starts_with("Missed"));
    let change = stage(&g, r, "2026-03", "5750").unwrap().unwrap();
    assert_eq!(change.kind, ChangeKind::Late);

    let result = apply_changes(&journal, &[change]).unwrap();
    assert!(result.summary().contains("1 missed month(s) recorded"));
    assert_eq!(payments(&journal, ann), original);
    assert_eq!(the_loan(&journal, ann).balance, 75000.0);
}

#[test]
fn an_operation_cannot_go_in_a_missed_month() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    delete_months(&journal, ann, &["2026-03"], false);
    assert!(stage_text(&journal, ann, "2026-03", "@B").unwrap_err().contains("goes in the month"));
}

#[test]
fn a_months_deletion_is_described_as_it_will_run() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let g = grid(&journal, false);
    let r = row(&g, ann);
    let may = stage(&g, r, "2026-05", "").unwrap().unwrap();
    let june = stage(&g, r, "2026-06", "").unwrap().unwrap();
    assert!(describe(Some(r), &may, &[]).starts_with("Marks this month missed"));
    // With June going too, May is the last deduction left: it un-runs.
    assert!(describe(Some(r), &may, &[&june]).starts_with("Un-runs this month"));
    assert_eq!(staged_edit_text(&may), "");
}

// --------------------------------------------------------- undo is exact

#[test]
fn undo_restores_every_column_of_every_row() {
    for months in [vec!["2026-06"], vec!["2026-03"], vec!["2026-03", "2026-06"], vec![
        "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
    ]] {
        let journal = Journal::open_in_memory().unwrap();
        let ann = loan(&journal, "Ann", "2026-06-01");
        loan(&journal, "Ben", "2026-06-01");
        let before = everything(&journal);
        let (_, result) = delete_months(&journal, ann, &months, false);
        assert_ne!(everything(&journal), before, "{months:?}");
        revert(&journal, &result).unwrap();
        assert_eq!(everything(&journal), before, "{months:?}");
    }
}

#[test]
fn undoing_a_correction_restores_later_splits_exactly() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let before = everything(&journal);
    let result = save(&journal, ann, "2026-03", "500"); // less than the month's interest
    assert_eq!(payments(&journal, ann)[2].2, 1000.0); // April settles March's 250 first
    revert(&journal, &result).unwrap();
    assert_eq!(everything(&journal), before);
}

#[test]
fn undo_refuses_a_loan_changed_since_the_save() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    let result = save(&journal, ann, "2026-04", "5750");
    deduct(&journal, ann, "L-001").unwrap(); // May, from the member's ledger
    let after = everything(&journal);
    let refused = revert(&journal, &result).unwrap_err().to_string();
    assert!(refused.contains("Ann · L-001 has changed since the save"), "{refused}");
    assert_eq!(everything(&journal), after);
}

#[test]
fn undo_is_not_refused_by_a_rewrite_that_changes_nothing() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-03-01");
    let before = everything(&journal);
    let result = save(&journal, ann, "2026-04", "5750");
    project_member(&journal, ann).unwrap();
    journal.conn().execute("UPDATE loans SET status=status WHERE individual_id=?", [ann]).unwrap();
    revert(&journal, &result).unwrap();
    assert_eq!(everything(&journal), before);
}

#[test]
fn undo_records_only_what_it_changed_in_the_activity_log() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let result = save(&journal, ann, "2026-07", "5750");
    let logged = audit_entries(&journal).len();
    revert(&journal, &result).unwrap();
    // July's charge and deduction go; the loan and its other rows are untouched.
    let new = audit_entries(&journal).split_off(logged);
    assert_eq!(new, [("DELETE".to_string(), "ledger".to_string()), ("DELETE".into(), "ledger".into())]);
}

// ------------------------------------------------------------ the preview

#[test]
fn only_the_rows_given_are_touched_and_the_rest_are_counted() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let ben = loan(&journal, "Ben", "2026-06-01");
    let (plan, _) = delete_months(&journal, ann, &["2026-06"], false);
    assert!(plan.loan_changes.iter().all(|c| c.member_id == ann));
    assert!(plan.untouched.iter().any(|line| line.contains("not on screen")));
    assert_eq!(payments(&journal, ben).len(), 5);
}

#[test]
fn the_preview_says_what_each_kind_of_deletion_does() {
    let journal = Journal::open_in_memory().unwrap();
    let ann = loan(&journal, "Ann", "2026-06-01");
    let g = grid(&journal, false);
    let plan = plan_delete_months(&journal, &[row(&g, ann)], &["2026-03".into(), "2026-06".into()]).unwrap();
    assert_eq!((plan.tally_of("un-run"), plan.tally_of("missed")), ((1, 5750.0), (1, 5750.0)));
    let text = plan.consequences.join(" ");
    assert!(text.contains("Un-run (1)") && text.contains("Missed (1)") && text.contains("suspend"));
}
