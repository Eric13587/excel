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
    // A blinking cursor repaints on a timer, which a slow render can catch
    // in the middle of a run.
    harness.ctx.all_styles_mut(|style| style.visuals.text_cursor.blink = false);
    harness.run();
    harness
}

/// The software renderer the screenshots use crashes when several tests
/// render at once, so they take turns.
static RENDERING: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn shot(harness: &mut Harness<'_, LoanMasterApp>, name: &str) {
    if let Some(dir) = std::env::var_os("LOANMASTER_SHOTS") {
        let _turn = RENDERING.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
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


// ------------------------------------------------------------ Ledger Grid

fn open_grid(harness: &mut Harness<'_, LoanMasterApp>) {
    let ws = harness.state_mut().workspace.as_mut().unwrap();
    ws.open_grid();
    if let crate::workspace::Screen::Grid(grid) = &mut ws.screen {
        // Narrow enough that October is on screen: the harness clicks where
        // a cell is drawn.
        let month = |m: u32| chrono::NaiveDate::from_ymd_opt(2026, m, 1).unwrap();
        grid.set_range(month(4), month(10));
    }
    harness.run();
}

fn savings(harness: &Harness<'_, LoanMasterApp>, name: &str) -> f64 {
    let id = member_id(harness, name);
    harness.state().workspace.as_ref().unwrap().journal.funds().balance(Fund::Savings, id).unwrap()
}

fn on_grid(harness: &Harness<'_, LoanMasterApp>) -> bool {
    matches!(harness.state().workspace.as_ref().unwrap().screen, crate::workspace::Screen::Grid(_))
}

/// Select a cell and type into it, as a person would.
fn type_into(harness: &mut Harness<'_, LoanMasterApp>, cell: &str, text: &str) {
    harness.get_by_label(cell).click();
    harness.run();
    harness.event(egui::Event::Text(text.into()));
    harness.run();
    harness.key_press(egui::Key::Enter);
    harness.run();
}

#[test]
fn the_grid_shows_each_savers_months() {
    let mut harness = harness(sample());
    open_grid(&mut harness);
    harness.get_by_label("Ledger Grid");
    harness.get_by_label("ANN WANJIRU, Sep 2026");
    harness.get_by_label("TOTAL");
    // Retired Samuel is a past member: hidden until asked for.
    assert!(harness.query_by_label("SAMUEL KARIUKI, Sep 2026").is_none());
    shot(&mut harness, "grid_savings");
}

#[test]
fn a_typed_figure_is_staged_until_saved_and_the_save_can_be_undone() {
    let mut harness = harness(sample());
    open_grid(&mut harness);
    type_into(&mut harness, "ANN WANJIRU, Oct 2026", "3000");
    harness.get_by_label_contains("1 unsaved change(s).");
    assert_eq!(savings(&harness, "ANN WANJIRU"), 22_500.0, "nothing is written before Save");
    shot(&mut harness, "grid_staged");

    harness.get_by_label("Save Changes").click();
    harness.run();
    assert_eq!(savings(&harness, "ANN WANJIRU"), 25_500.0);
    harness.get_by_label_contains("Saved —");

    harness.get_by_label("Undo Last Save").click();
    harness.run();
    harness.get_by_label("Undo Save").click();
    harness.run();
    assert_eq!(savings(&harness, "ANN WANJIRU"), 22_500.0);
}

#[test]
fn a_busy_month_stays_readable_while_typed_and_after() {
    let mut harness = harness(sample());
    open_grid(&mut harness);
    harness.get_by_label("ANN WANJIRU, Oct 2026").click();
    harness.run();
    harness.event(egui::Event::Text("2,000, @W=500, +@W=250".into()));
    harness.run();
    // The bar above the grid says what the text will do before it is staged.
    harness.get_by_label_contains("Enter stages it");
    shot(&mut harness, "grid_editing_busy");
    harness.key_press(egui::Key::Enter);
    harness.run();
    harness.get_by_label("ANN WANJIRU, Oct 2026").click();
    harness.run();
    harness.get_by_label_contains("unsaved: Leaves: Deposit 2,000; Withdrawal 500; Withdrawal 250");
    shot(&mut harness, "grid_busy_cell");
}

#[test]
fn typing_something_the_cell_cannot_take_says_why_before_enter() {
    let mut harness = harness(sample());
    open_grid(&mut harness);
    harness.get_by_label("ANN WANJIRU, Oct 2026").click();
    harness.run();
    harness.event(egui::Event::Text("@X=5".into()));
    harness.run();
    assert!(harness.query_by_label_contains("Enter stages it").is_none());
    shot(&mut harness, "grid_editing_wrong");
}

#[test]
fn a_figure_the_cell_cannot_take_is_refused_with_the_reason() {
    let mut harness = harness(sample());
    open_grid(&mut harness);
    type_into(&mut harness, "ANN WANJIRU, Oct 2026", "lots");
    harness.get_by_label_contains("ANN WANJIRU, Oct 2026:");
    assert!(harness.query_by_label_contains("unsaved change").is_none());
}

#[test]
fn fill_forward_proposes_the_missing_month_for_review() {
    let mut harness = harness(sample());
    open_grid(&mut harness);
    harness.get_by_label("Fill Forward…").click();
    harness.run();
    // Nine active savers lack October; Samuel retired and is not shown.
    harness.get_by_label_contains("Propose 9 entries across 9 member(s)");
    shot(&mut harness, "grid_fill_forward");
    harness.get_by_label("Stage Them").click();
    harness.run();
    harness.get_by_label_contains("9 entries staged");
    assert_eq!(savings(&harness, "PETER MWANGI"), 22_500.0);
    harness.get_by_label("Save Changes").click();
    harness.run();
    assert_eq!(savings(&harness, "PETER MWANGI"), 25_000.0);
}

#[test]
fn a_whole_month_is_deleted_after_a_preview() {
    let mut harness = harness(sample());
    open_grid(&mut harness);
    harness.get_by_label("Sep 2026").click();
    harness.run();
    harness.key_press(egui::Key::Delete);
    harness.run();
    harness.get_by_label("Delete Months");
    shot(&mut harness, "grid_delete_preview");
    harness.get_by_label("Stage Deletions").click();
    harness.run();
    harness.get_by_label_contains("deletion(s) staged in Sep 2026");
    harness.get_by_label("Save Changes").click();
    harness.run();
    assert_eq!(savings(&harness, "ANN WANJIRU"), 20_000.0);
}

#[test]
fn leaving_the_grid_asks_before_dropping_unsaved_work() {
    let mut harness = harness(sample());
    open_grid(&mut harness);
    type_into(&mut harness, "ANN WANJIRU, Oct 2026", "3000");
    harness.get_by_label("⬅ Dashboard").click();
    harness.run();
    harness.get_by_label("Unsaved Changes");
    harness.get_by_label("Cancel").click();
    harness.run();
    assert!(on_grid(&harness), "Cancel stays");
    harness.get_by_label_contains("1 unsaved change(s).");

    harness.get_by_label("⬅ Dashboard").click();
    harness.run();
    harness.get_by_label("Discard").click();
    harness.run();
    assert!(!on_grid(&harness));
    assert_eq!(savings(&harness, "ANN WANJIRU"), 22_500.0);
}

#[test]
fn keys_pressed_in_front_of_the_grid_stay_with_the_window() {
    let mut harness = harness(sample());
    open_grid(&mut harness);
    harness.get_by_label("ANN WANJIRU, Sep 2026").click();
    harness.run();
    let window = crate::dialogs::activity::ActivityLog::default();
    harness.state_mut().workspace.as_mut().unwrap().dialogs.push(window.into());
    harness.run();
    harness.key_press(egui::Key::Delete);
    harness.run();
    assert_eq!(harness.state().workspace.as_ref().unwrap().unsaved(), None, "Delete went to the window, not the grid");
    // With the window gone, the grid has the keys again.
    harness.state_mut().workspace.as_mut().unwrap().dialogs.clear();
    harness.run();
    harness.key_press(egui::Key::Delete);
    harness.run();
    assert!(harness.state().workspace.as_ref().unwrap().unsaved().is_some());
}

#[test]
fn the_loans_tab_lists_each_loan_and_its_missed_months() {
    let mut harness = harness(sample());
    open_grid(&mut harness);
    harness.get_by_label("Loans").click();
    harness.run();
    harness.get_by_label("GRACE ACHIENG · L-001, Jul 2026");
    shot(&mut harness, "grid_loans");
    // Grace is due for July: the month takes her deduction.
    type_into(&mut harness, "GRACE ACHIENG · L-001, Jul 2026", "5750");
    harness.get_by_label_contains("1 unsaved change(s).");
    harness.get_by_label("Save Changes").click();
    harness.run();
    let grace = member_id(&harness, "GRACE ACHIENG");
    let loan = harness.state().workspace.as_ref().unwrap().journal.loans().by_ref(grace, "L-001").unwrap().unwrap();
    assert_eq!(loan.next_due_date.as_deref(), Some("2026-08-01"));
}

#[test]
fn f1_opens_the_help_for_the_tab_shown() {
    let mut harness = harness(sample());
    open_grid(&mut harness);
    harness.key_press(egui::Key::F1);
    harness.run();
    harness.get_by_label_contains("Ledger Grid Help — Savings / Shares");
    shot(&mut harness, "grid_help");
}

// ---------------------------------------------------------------- reports

/// Pick a menu entry the way a person would.
fn menu(harness: &mut Harness<'_, LoanMasterApp>, menu: &str, item: &str) {
    harness.get_by_label(menu).click();
    harness.run();
    harness.get_by_label(item).click();
    harness.run();
}

/// Let the task under way end, drawing frames as the window would. While
/// one runs, its progress keeps asking for frames, so `run` would not settle.
fn finish_task(harness: &mut Harness<'_, LoanMasterApp>) {
    let running = |harness: &Harness<'_, LoanMasterApp>| harness.state().workspace.as_ref().unwrap().task.is_some();
    for _ in 0..3000 {
        if !running(harness) {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
        harness.step();
    }
    assert!(!running(harness), "the task ended");
    harness.run();
}

#[test]
fn the_members_list_is_saved_where_asked() {
    let mut harness = harness(sample());
    menu(&mut harness, "Reports", "Members List (Excel / PDF)…");
    shot(&mut harness, "members_list");
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("members.csv");
    crate::dialogs::files::ask::answer(&path);
    harness.get_by_label("Export…").click();
    harness.step();
    finish_task(&mut harness);
    let csv = std::fs::read_to_string(&path).unwrap();
    assert!(csv.contains("Name,PF No,ID No,Phone,Employment Status"), "{csv}");
    assert!(csv.contains("ANN WANJIRU,PF1001"), "{csv}");
}

#[test]
fn a_savings_quarter_report_is_written() {
    let mut harness = harness(sample());
    menu(&mut harness, "Reports", "Savings / Shares Report…");
    shot(&mut harness, "savings_report");
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("savings.csv");
    crate::dialogs::files::ask::answer(&path);
    harness.get_by_label("Save Report…").click();
    harness.step();
    finish_task(&mut harness);
    let csv = std::fs::read_to_string(&path).unwrap();
    assert!(csv.contains("Grand Total"), "{csv}");
    assert!(csv.contains("ANN WANJIRU"), "{csv}");
}

#[test]
fn statements_are_written_one_file_per_member() {
    let mut harness = harness(sample());
    menu(&mut harness, "Reports", "Statements…");
    harness.get_by_label("ANN WANJIRU | 0722 111 201").click();
    harness.run();
    harness.get_by_label("GRACE ACHIENG | 0722 111 203").click();
    harness.run();
    harness.get_by_label("2 selected");
    shot(&mut harness, "statements");
    let dir = tempfile::tempdir().unwrap();
    crate::dialogs::files::ask::answer(dir.path());
    harness.get_by_label("Generate…").click();
    harness.step();
    finish_task(&mut harness);
    harness.get_by_label("Batch complete");
    let mut written: Vec<String> = std::fs::read_dir(dir.path()).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().to_string()).collect();
    written.sort();
    assert_eq!(written.len(), 2, "{written:?}");
    assert!(written[0].starts_with("ANN WANJIRU_statement_") && written[0].ends_with(".pdf"), "{written:?}");
}

// ------------------------------------------------------------------ tools

#[test]
fn settings_are_saved_to_the_journal() {
    let mut harness = harness(sample());
    menu(&mut harness, "Tools", "Settings…");
    shot(&mut harness, "settings");
    let name = || (egui::accesskit::Role::TextInput, "Organization name");
    harness.get_by_role_and_label(name().0, name().1).focus();
    harness.run();
    harness.key_combination_modifiers(egui::Modifiers::COMMAND, &[egui::Key::A]);
    harness.get_by_role_and_label(name().0, name().1).type_text("Umoja Staff Sacco");
    harness.run();
    harness.get_by_label("Save").click();
    harness.run();
    let journal = &harness.state().workspace.as_ref().unwrap().journal;
    assert_eq!(journal.settings().org_name(), "Umoja Staff Sacco");
    assert_eq!(journal.settings().default_interest_rate(), 0.15, "untouched values are kept");
    harness.get_by_label("Umoja Staff Sacco");
}

#[test]
fn an_impossible_interest_rate_is_not_saved() {
    let mut harness = harness(sample());
    menu(&mut harness, "Tools", "Settings…");
    let rate = || (egui::accesskit::Role::TextInput, "Loan interest rate");
    harness.get_by_role_and_label(rate().0, rate().1).focus();
    harness.run();
    harness.key_combination_modifiers(egui::Modifiers::COMMAND, &[egui::Key::A]);
    harness.get_by_role_and_label(rate().0, rate().1).type_text("150");
    harness.run();
    harness.get_by_label("Save").click();
    harness.run();
    harness.get_by_label("The interest rate must be a percentage from 0 to 100.");
    assert_eq!(harness.state().workspace.as_ref().unwrap().journal.settings().default_interest_rate(), 0.15);
}

#[test]
fn the_activity_log_lists_what_changed() {
    let mut harness = harness(sample());
    menu(&mut harness, "Tools", "Activity Log…");
    harness.get_by_label_contains("create, update and delete events");
    shot(&mut harness, "activity_log");
}

#[test]
fn a_backup_is_restored_after_asking() {
    use loanmaster_core::OpenOptions;
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("journal.db");
    sample().backup_to(&path).unwrap();
    // Opening takes the backup; a member added afterwards is not in it.
    let journal = Journal::open(&path, OpenOptions { backup_on_open: true }).unwrap();
    membership::add(&journal, &MemberDetails { name: "NEW MEMBER".into(), ..Default::default() }).unwrap();
    let mut harness = harness(journal);
    menu(&mut harness, "Tools", "Restore From Backup…");
    shot(&mut harness, "restore");
    harness.get_by_label_contains("journal.20").click();
    harness.run();
    harness.get_by_label_contains("Contains 10 members");
    harness.get_by_label("Restore Selected").click();
    harness.run();
    harness.get_by_label("Restore Journal");
    harness.get_by_label("Restore").click();
    harness.run();
    harness.get_by_label("Restored");
    let journal = &harness.state().workspace.as_ref().unwrap().journal;
    assert_eq!(journal.members().all().unwrap().len(), 10);
    assert_eq!(journal.available_backups().len(), 2, "the state before restoring was kept");
}

#[test]
fn clicking_a_members_row_selects_them() {
    let mut harness = harness(sample());
    harness.get_by_label("PETER MWANGI").click();
    harness.run();
    let peter = member_id(&harness, "PETER MWANGI");
    assert_eq!(harness.state().workspace.as_ref().unwrap().dashboard.selected, Some(peter));
}

// ---------------------------------------------------------------- imports

fn member_count(harness: &Harness<'_, LoanMasterApp>) -> usize {
    harness.state().workspace.as_ref().unwrap().journal.members().all().unwrap().len()
}

#[test]
fn members_come_in_from_another_journal_and_the_import_can_be_undone() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("branch.db");
    {
        let branch = Journal::open(&path, loanmaster_core::OpenOptions::default()).unwrap();
        for name in ["ANN WANJIRU", "Mary Njeri", "ZAWADI ACHIENG"] {
            let id = membership::add(&branch, &MemberDetails { name: name.into(), ..Default::default() }).unwrap();
            funds::post(&branch, Posting::new(Fund::Savings, id, "2026-02-05", "Deposit", 1000.0, "")).unwrap();
        }
        branch.close();
    }
    let mut harness = harness(sample());
    menu(&mut harness, "File", "Import From Another Journal (.db)…");
    crate::dialogs::files::ask::answer(&path);
    harness.get_by_label("Select Database File…").click();
    harness.run();
    harness.get_by_label("Savings history").click();
    harness.run();
    harness.get_by_label("Choose Members ›").click();
    harness.run();
    harness.get_by_label("3 selected");
    harness.get_by_label("Preview ›").click();
    harness.run();
    // Ann's name matches exactly and merges; Mary's differs in case, so the
    // user is asked; Zawadi is new.
    harness.get_by_label_contains("1 member(s) resemble someone already on record");
    shot(&mut harness, "import_preview");
    harness.get_by_label("Start Import").click();
    harness.run();
    harness.get_by_label("Import Complete");
    assert_eq!(member_count(&harness), 11, "Zawadi is new; Ann and Mary merged");
    assert_eq!(savings(&harness, "ANN WANJIRU"), 23_500.0);

    harness.get_by_label("OK").click();
    harness.run();
    menu(&mut harness, "File", "Import History…");
    harness.get_by_label("Ind: 1 | Sav: 3").click();
    harness.run();
    shot(&mut harness, "import_history");
    harness.get_by_label("Undo Selected Import").click();
    harness.run();
    // The question's heading, then its button.
    harness.get_all_by_label("Undo Import").last().unwrap().click();
    harness.run();
    harness.get_by_label("Import Undone");
    assert_eq!(member_count(&harness), 10);
    assert_eq!(savings(&harness, "ANN WANJIRU"), 22_500.0, "the deposit added to Ann went with the import");
}

#[test]
fn a_roster_spreadsheet_updates_and_adds_members() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("roster.xlsx");
    let mut book = rust_xlsxwriter::Workbook::new();
    let sheet = book.add_worksheet();
    for (row, cells) in [["Name", "PF No", "Phone"], ["Ann Wanjiru", "PF1001", "0711 000 001"], ["Zuberi Hamisi", "PF2001", "0711 000 002"]].iter().enumerate() {
        for (col, text) in cells.iter().enumerate() {
            sheet.write_string(row as u32, col as u16, *text).unwrap();
        }
    }
    book.save(&path).unwrap();

    let mut harness = harness(sample());
    menu(&mut harness, "File", "Import From Excel (.xlsx)…");
    crate::dialogs::files::ask::answer(&path);
    harness.get_by_label("Select Excel File…").click();
    harness.run();
    harness.get_by_label_contains("A roster sheet, 2 row(s)");
    harness.get_by_label("Preview").click();
    harness.run();
    harness.get_by_label_contains("2 row(s) — 1 update, 1 new");
    shot(&mut harness, "excel_import");
    harness.get_by_label("Import").click();
    harness.run();
    harness.get_by_label("Confirm Import");
    harness.get_all_by_label("Import").last().unwrap().click();
    harness.run();
    harness.get_by_label("Import Complete");
    assert_eq!(member_count(&harness), 11);
    let ann = member_id(&harness, "ANN WANJIRU");
    let journal = &harness.state().workspace.as_ref().unwrap().journal;
    assert_eq!(journal.members().get(ann).unwrap().unwrap().phone, "0711000001");
}

// ------------------------------------------------------------------ smoke

/// Every screen over a real journal, to catch what a small sample cannot.
/// Point `LOANMASTER_SMOKE` at a *copy* of a journal and run with
/// `cargo test --release -p loanmaster -- --ignored smoke`. It takes no
/// screenshots: they would hold the members' data.
#[test]
#[ignore = "needs LOANMASTER_SMOKE=<a copy of a journal>"]
fn smoke_every_screen_over_a_real_journal() {
    use crate::screens::grid::Tab as GridTab;
    use crate::screens::member::Tab;
    use crate::workspace::Screen;
    let Some(path) = std::env::var_os("LOANMASTER_SMOKE") else { return };
    let journal = Journal::open(&path, loanmaster_core::OpenOptions::default()).unwrap();
    let ids: Vec<i64> = journal.members().all().unwrap().iter().map(|m| m.id).collect();
    let mut harness = harness(journal);
    let timed = |what: &str, started: std::time::Instant| eprintln!("{what}: {:.2?}", started.elapsed());

    let started = std::time::Instant::now();
    for id in &ids {
        harness.state_mut().workspace.as_mut().unwrap().open_member(*id);
        harness.run();
        for tab in [Tab::Fund(Fund::Savings), Tab::Fund(Fund::Christmas), Tab::Fund(Fund::Benevolent)] {
            if let Screen::Member(member) = &mut harness.state_mut().workspace.as_mut().unwrap().screen {
                member.tab = tab;
            }
            harness.run();
        }
    }
    timed(&format!("{} members' ledgers, every tab", ids.len()), started);

    let started = std::time::Instant::now();
    harness.state_mut().workspace.as_mut().unwrap().open_grid();
    harness.run();
    for tab in [GridTab::Savings, GridTab::Christmas, GridTab::Benevolent, GridTab::Loans] {
        if let Screen::Grid(grid) = &mut harness.state_mut().workspace.as_mut().unwrap().screen {
            grid.tab = tab;
        }
        harness.run();
        harness.get_by_label("Fill Forward…").click();
        harness.run();
        // The proposal, or the note that there is nothing to fill.
        let close = if harness.query_by_label("Stage Them").is_some() { "Cancel" } else { "Close" };
        harness.get_by_label(close).click();
        harness.run();
    }
    timed("the grid's four tabs with Fill Forward", started);

    let started = std::time::Instant::now();
    harness.state_mut().workspace.as_mut().unwrap().screen = Screen::Dashboard;
    harness.run();
    for (menu_name, item) in [
        ("Reports", "Loan (Quarterly) Report…"),
        ("Reports", "Savings / Shares Report…"),
        ("Reports", "Christmas Report…"),
        ("Reports", "Benevolent Report…"),
        ("Reports", "Loans in Arrears…"),
        ("Reports", "Members List (Excel / PDF)…"),
        ("Reports", "Statements…"),
        ("Tools", "Activity Log…"),
    ] {
        menu(&mut harness, menu_name, item);
        // A window may answer with a notice instead ("Nothing is in
        // arrears"), which would hold the next menu shut.
        let app = harness.state_mut();
        app.notices.clear();
        app.workspace.as_mut().unwrap().dialogs.clear();
        harness.run();
    }
    timed("every report and tool window", started);
}
