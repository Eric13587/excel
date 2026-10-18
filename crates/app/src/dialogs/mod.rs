//! Dialogs: each one draws itself, queues what it does as [`Action`]s, and
//! says when it has closed.

use eframe::egui;
use loanmaster_core::Journal;

use crate::app::Action;

pub mod activity;
pub mod arrears;
pub mod columns;
pub mod confirm;
pub mod files;
pub mod fund;
pub mod imports;
pub mod loan;
pub mod member;
pub mod reports;
pub mod restore;
pub mod settings;
pub mod statements;

macro_rules! dialogs {
    ($($variant:ident($ty:ty)),* $(,)?) => {
        pub enum Dialog {
            $($variant($ty)),*
        }

        $(impl From<$ty> for Dialog {
            fn from(dialog: $ty) -> Dialog {
                Dialog::$variant(dialog)
            }
        })*
    };
}

dialogs! {
    Confirm(confirm::Confirm),
    Member(member::MemberForm),
    Leave(member::Leave),
    Arrears(arrears::Arrears),
    Principal(loan::PrincipalCalculator),
    CatchUp(loan::CatchUp),
    TopUp(loan::TopUp),
    Suspend(loan::Suspend),
    Suspensions(loan::Suspensions),
    ClearSuspension(loan::ClearSuspension),
    EntryDate(loan::EntryDate),
    EntryAmount(loan::EntryAmount),
    EntryNotes(loan::EntryNotes),
    Movement(fund::Movement),
    EditRow(fund::EditRow),
    Enrol(fund::Enrol),
    SuspendSavings(fund::SuspendSavings),
    FundReport(reports::FundReport),
    LoanReport(reports::LoanReport),
    MembersList(reports::MembersList),
    Statements(statements::Statements),
    Settings(settings::SettingsForm),
    Activity(activity::ActivityLog),
    Restore(restore::Restore),
    JournalImport(imports::JournalImport),
    ExcelImport(imports::ExcelImport),
    ImportHistory(imports::ImportHistory),
}

impl Dialog {
    /// Draw the dialog. `false` once it has closed.
    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        match self {
            Dialog::Confirm(d) => d.show(ctx, actions),
            Dialog::Member(d) => d.show(ctx, journal, actions),
            Dialog::Leave(d) => d.show(ctx, journal, actions),
            Dialog::Arrears(d) => d.show(ctx, journal, actions),
            Dialog::Principal(d) => d.show(ctx, actions),
            Dialog::CatchUp(d) => d.show(ctx, actions),
            Dialog::TopUp(d) => d.show(ctx, actions),
            Dialog::Suspend(d) => d.show(ctx, journal, actions),
            Dialog::Suspensions(d) => d.show(ctx, journal, actions),
            Dialog::ClearSuspension(d) => d.show(ctx, journal, actions),
            Dialog::EntryDate(d) => d.show(ctx, actions),
            Dialog::EntryAmount(d) => d.show(ctx, journal, actions),
            Dialog::EntryNotes(d) => d.show(ctx, actions),
            Dialog::Movement(d) => d.show(ctx, actions),
            Dialog::EditRow(d) => d.show(ctx, actions),
            Dialog::Enrol(d) => d.show(ctx, actions),
            Dialog::SuspendSavings(d) => d.show(ctx, journal, actions),
            Dialog::FundReport(d) => d.show(ctx, journal, actions),
            Dialog::LoanReport(d) => d.show(ctx, journal, actions),
            Dialog::MembersList(d) => d.show(ctx, journal, actions),
            Dialog::Statements(d) => d.show(ctx, journal, actions),
            Dialog::Settings(d) => d.show(ctx, actions),
            Dialog::Activity(d) => d.show(ctx, journal, actions),
            Dialog::Restore(d) => d.show(ctx, journal, actions),
            Dialog::JournalImport(d) => d.show(ctx, journal, actions),
            Dialog::ExcelImport(d) => d.show(ctx, journal, actions),
            Dialog::ImportHistory(d) => d.show(ctx, journal, actions),
        }
    }
}

/// The row of buttons along a dialog's foot, right-aligned. One row high:
/// a layout given the rest of the window would centre the buttons in it.
pub fn footer<R>(ui: &mut egui::Ui, add: impl FnOnce(&mut egui::Ui) -> R) -> R {
    ui.add_space(8.0);
    ui.separator();
    ui.allocate_ui_with_layout(egui::vec2(ui.available_width(), 0.0), egui::Layout::right_to_left(egui::Align::Center), add).inner
}
