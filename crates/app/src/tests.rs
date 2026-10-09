//! The window, driven headlessly. Set `LOANMASTER_SHOTS` to a folder to have
//! each test save what it drew there, for looking at.

use std::path::Path;

use eframe::egui;
use egui_kittest::Harness;
use egui_kittest::kittest::Queryable;
use loanmaster_core::Journal;
use loanmaster_core::db::members::MemberDetails;
use loanmaster_core::db::model::Fund;
use loanmaster_core::funds::{self, Posting};
use loanmaster_core::loans::service::{catch_up, issue};
use loanmaster_core::membership;

use crate::app::LoanMasterApp;
use crate::workspace::Workspace;

/// A small society: ten members, most saving, several borrowing, one
/// behind, one retired.
pub fn sample() -> Journal {
    let journal = Journal::open_in_memory().unwrap();
    journal.settings().set("org_name", "Sample Savings & Credit Society").unwrap();
    let people = [
        ("ANN WANJIRU", "0722 111 201", "PF1001"),
        ("PETER MWANGI", "0722 111 202", "PF1002"),
        ("GRACE ACHIENG", "0722 111 203", "PF1003"),
        ("JOHN OTIENO", "0722 111 204", "PF1004"),
        ("MARY NJERI", "0722 111 205", "PF1005"),
        ("DAVID KIPROP", "0722 111 206", "PF1006"),
        ("ESTHER WAMBUI", "0722 111 207", "PF1007"),
        ("JAMES MUTUA", "0722 111 208", "PF1008"),
        ("LUCY AKINYI", "0722 111 209", "PF1009"),
        ("SAMUEL KARIUKI", "0722 111 210", "PF1010"),
    ];
    let mut ids = Vec::new();
    for (name, phone, pf) in people {
        let id = membership::add(&journal, &MemberDetails { name: name.into(), phone: phone.into(), pf_no: pf.into(), ..Default::default() }).unwrap();
        for month in 1..=9 {
            funds::post(&journal, Posting::new(Fund::Savings, id, &format!("2026-{month:02}-05"), "Deposit", 2500.0, "")).unwrap();
        }
        ids.push(id);
    }
    for (i, (principal, months, through)) in
        [(100_000.0, 20, "2026-10-01"), (250_000.0, 24, "2026-10-01"), (60_000.0, 12, "2026-06-01"), (150_000.0, 18, "2026-10-01")]
            .into_iter()
            .enumerate()
    {
        issue(&journal, ids[i], principal, months, "2026-01-01", Some(0.15)).unwrap();
        catch_up(&journal, ids[i], "L-001", Some(through)).unwrap();
    }
    membership::retire(&journal, ids[9], Some("2026-09-30")).unwrap();
    journal
}

fn harness(journal: Journal) -> Harness<'static, LoanMasterApp> {
    let mut harness = Harness::builder().with_size(egui::vec2(1240.0, 780.0)).build_eframe(move |cc| {
        egui_extras::install_image_loaders(&cc.egui_ctx);
        LoanMasterApp::with_workspace(Workspace::in_memory(journal))
    });
    harness.run();
    harness
}

fn shot(harness: &mut Harness<'_, LoanMasterApp>, name: &str) {
    if let Some(dir) = std::env::var_os("LOANMASTER_SHOTS") {
        let image = harness.render().expect("the window renders");
        image.save(Path::new(&dir).join(format!("{name}.png"))).unwrap();
    }
}

#[test]
fn the_dashboard_shows_the_portfolio_and_every_member() {
    let mut harness = harness(sample());
    harness.get_by_label("ANN WANJIRU");
    harness.get_by_label("SAMUEL KARIUKI");
    harness.get_by_label("Loans in Arrears  ›");
    shot(&mut harness, "dashboard");
}

#[test]
fn the_search_narrows_the_list() {
    let mut harness = harness(sample());
    harness.get_by_role(egui::accesskit::Role::TextInput).focus();
    harness.run();
    harness.get_by_role(egui::accesskit::Role::TextInput).type_text("pf1003");
    harness.run();
    harness.get_by_label("GRACE ACHIENG");
    assert!(harness.query_by_label("ANN WANJIRU").is_none());
    shot(&mut harness, "dashboard_search");
}

#[test]
fn a_new_member_is_added_from_the_form() {
    let mut harness = harness(sample());
    harness.get_by_label("+ New Member").click();
    harness.run();
    shot(&mut harness, "member_form");
    harness.get_by_label("Add Member").click();
    harness.run();
    harness.get_by_label("Name is required");
}

#[test]
fn the_arrears_tile_lists_who_is_behind() {
    let mut harness = harness(sample());
    harness.get_by_label("Loans in Arrears  ›").click();
    harness.run();
    // Grace has missed July to October; the window lists her beside the
    // dashboard's own row.
    harness.get_by_label_contains("member(s) ·");
    assert_eq!(harness.get_all_by_label("GRACE ACHIENG").count(), 2);
    shot(&mut harness, "arrears");
}

fn member_id(harness: &Harness<'_, LoanMasterApp>, name: &str) -> i64 {
    let journal = &harness.state().workspace.as_ref().unwrap().journal;
    journal.members().all().unwrap().into_iter().find(|m| m.name == name).unwrap().id
}

fn open_member(harness: &mut Harness<'_, LoanMasterApp>, name: &str) -> i64 {
    let id = member_id(harness, name);
    harness.state_mut().workspace.as_mut().unwrap().open_member(id);
    harness.run();
    id
}

#[test]
fn a_members_ledger_shows_each_loan_with_its_history() {
    let mut harness = harness(sample());
    open_member(&mut harness, "GRACE ACHIENG");
    harness.get_by_label_contains("OVERDUE SINCE");
    harness.get_by_label("Loan Issued");
    shot(&mut harness, "member_loans");
}

#[test]
fn a_loan_is_issued_from_the_form() {
    let mut harness = harness(sample());
    let david = open_member(&mut harness, "DAVID KIPROP");
    let inputs: Vec<_> = harness.get_all_by_role(egui::accesskit::Role::TextInput).collect();
    inputs[0].focus();
    harness.run();
    harness.get_all_by_role(egui::accesskit::Role::TextInput).next().unwrap().type_text("50000");
    harness.run();
    harness.get_all_by_role(egui::accesskit::Role::TextInput).nth(1).unwrap().focus();
    harness.run();
    harness.get_all_by_role(egui::accesskit::Role::TextInput).nth(1).unwrap().type_text("10");
    harness.run();
    harness.get_by_label_contains("a month;");
    shot(&mut harness, "member_new_loan");
    harness.get_by_label("Issue Loan").click();
    harness.run();
    let journal = &harness.state().workspace.as_ref().unwrap().journal;
    let loan = journal.loans().by_ref(david, "L-001").unwrap().expect("the loan was issued");
    assert_eq!((loan.principal, loan.installment), (50000.0, 5750.0));
}

#[test]
fn the_deduct_button_takes_the_next_month() {
    let mut harness = harness(sample());
    let grace = open_member(&mut harness, "GRACE ACHIENG");
    let before = harness.state().workspace.as_ref().unwrap().journal.loans().by_ref(grace, "L-001").unwrap().unwrap().next_due_date;
    harness.get_by_label("Deduct").click();
    harness.run();
    let after = harness.state().workspace.as_ref().unwrap().journal.loans().by_ref(grace, "L-001").unwrap().unwrap().next_due_date;
    assert_eq!((before.as_deref(), after.as_deref()), (Some("2026-07-01"), Some("2026-08-01")));
}

#[test]
fn the_savings_tab_lists_every_deposit() {
    let mut harness = harness(sample());
    open_member(&mut harness, "ANN WANJIRU");
    harness.get_by_label("Savings / Shares").click();
    harness.run();
    assert_eq!(harness.get_all_by_label("Deposit").count(), 9);
    shot(&mut harness, "member_savings");
}

