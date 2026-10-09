//! The Reports menu's dialogs: a fund over a quarter or any period, the
//! quarterly loan interest report, and the members list. Each chooses its
//! period and columns, asks where to save, reads the journal, and leaves the
//! typesetting and writing to a task.

use chrono::NaiveDate;
use eframe::egui::{self, RichText};
use loanmaster_core::db::model::Fund;
use loanmaster_core::reports::{self, MEMBER_COLUMNS, Table};
use loanmaster_core::{Journal, dates};
use loanmaster_reports::Branding;

use super::columns::Columns;
use super::files;
use crate::app::{Action, Notice};
use crate::task::Task;
use crate::theme;
use crate::widgets;

/// Workbook first: these reports are mostly worked on further in Excel.
const WORKBOOK_FIRST: [(&str, &str); 3] = [("Excel workbook", "xlsx"), ("PDF document", "pdf"), ("CSV file", "csv")];

fn today() -> NaiveDate {
    chrono::Local::now().date_naive()
}

/// "Jan 2026 - Mar 2026".
fn quarter_label(start: NaiveDate) -> String {
    format!("{} - {}", start.format("%b %Y"), dates::add_months(start, 2).format("%b %Y"))
}

/// Write `table` to `path` off the window's thread, then open it.
fn write_in_background(ctx: &egui::Context, journal: &Journal, table: Table, path: std::path::PathBuf, what: &'static str, actions: &mut Vec<Action>) {
    let branding = Branding::of(journal);
    let task = Task::spawn(ctx, format!("Saving {}", what.to_lowercase()), move |reporter| {
        reporter.step(0, 1, &format!("Writing {}…", path.display()));
        let written = loanmaster_reports::write_table(&table, &branding, &path);
        Box::new(move |actions: &mut Vec<Action>| match written {
            Ok(()) => files::saved(actions, what, &path),
            Err(e) => actions.push(Action::Notice(Notice::warning(format!("{what} could not be saved"), e.to_string()))),
        })
    });
    actions.push(Action::Task(Box::new(task)));
}

/// What the journal gave, or a notice saying why it could not. Reports are
/// read on the window's thread: a member at a time, they take well under a
/// second, so only the writing is left to a task.
fn read<T>(result: loanmaster_core::Result<T>, what: &str, actions: &mut Vec<Action>) -> Option<T> {
    match result {
        Ok(value) => Some(value),
        Err(e) => {
            actions.push(Action::Notice(Notice::from_error(&format!("{what} could not be prepared"), &e)));
            None
        }
    }
}

fn quarter_picker(ui: &mut egui::Ui, id: &str, quarters: &[NaiveDate], chosen: &mut usize) -> bool {
    let before = *chosen;
    let current = quarters.get(*chosen).map(|q| quarter_label(*q)).unwrap_or_default();
    egui::ComboBox::from_id_salt(id).selected_text(current).width(220.0).height(320.0).show_ui(ui, |ui| {
        for (i, start) in quarters.iter().enumerate().rev() {
            ui.selectable_value(chosen, i, quarter_label(*start));
        }
    });
    before != *chosen
}

/// The quarters to offer and the one to start on.
fn quarters(journal: &Journal) -> (Vec<NaiveDate>, usize) {
    let today = today();
    let starts = reports::quarter_starts(journal, today).unwrap_or_default();
    let default = reports::default_quarter(journal, today).ok();
    let at = starts.iter().position(|q| Some(*q) == default).unwrap_or(starts.len().saturating_sub(1));
    (starts, at)
}

// ------------------------------------------------------------------ funds

pub struct FundReport {
    fund: Fund,
    quarters: Vec<NaiveDate>,
    quarter: usize,
    /// Savings may report a quarter; the other funds report a period only.
    by_quarter: bool,
    from: NaiveDate,
    to: NaiveDate,
    columns: Columns,
}

impl FundReport {
    pub fn new(journal: &Journal, fund: Fund) -> FundReport {
        let (quarters, quarter) = quarters(journal);
        let mut report = FundReport {
            fund,
            quarters,
            quarter,
            by_quarter: fund == Fund::Savings,
            // A period defaults to the financial year so far.
            from: reports::fy_start(journal, today()),
            to: today(),
            columns: Columns::default(),
        };
        report.offer_columns();
        report
    }

    fn offer_columns(&mut self) {
        let names = match self.quarters.get(self.quarter) {
            Some(start) if self.by_quarter => reports::savings_quarter_columns(*start),
            _ => reports::fund_period_columns(self.from, self.to),
        };
        self.columns.offer(&names);
    }

    fn generate(&self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let label = reports::fund_label(self.fund);
        let columns = self.columns.chosen();
        if columns.is_empty() {
            actions.push(Action::Notice(Notice::info(format!("{label} Report"), "Tick at least one column to include.")));
            return false;
        }
        let quarter = self.quarters.get(self.quarter).copied().filter(|_| self.by_quarter);
        let (from, to) = (dates::format(self.from), dates::format(self.to));
        if quarter.is_none() && self.to < self.from {
            actions.push(Action::Notice(Notice::info(format!("{label} Report"), "The period ends before it starts.")));
            return false;
        }
        let file = match quarter {
            Some(start) => format!("{}_Quarterly_{}.xlsx", label.replace('/', "-"), dates::format(start)),
            None => format!("{}_Report_{from}_to_{to}.xlsx", label.replace('/', "-")),
        };
        let Some(path) = files::save_as("Save Report", &file, &WORKBOOK_FIRST) else { return false };
        let mut keep_going = |_: usize, _: usize, _: &str| true;
        let table = match quarter {
            Some(start) => reports::savings_quarter(journal, &dates::format(start), Some(&columns), &mut keep_going),
            None => reports::fund_period(journal, self.fund, &from, &to, Some(&columns), &mut keep_going),
        };
        let Some(table) = read(table, &format!("The {label} report"), actions) else { return false };
        write_in_background(ctx, journal, table, path, "The report", actions);
        true
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let label = reports::fund_label(self.fund);
        let mut open = true;
        let mut close = false;
        egui::Window::new(format!("{label} Report")).open(&mut open).collapsible(false).resizable(false).show(ctx, |ui| {
            let mut changed = false;
            if self.fund == Fund::Savings {
                ui.horizontal(|ui| {
                    changed |= ui.radio_value(&mut self.by_quarter, true, "Quarter").changed();
                    changed |= ui.radio_value(&mut self.by_quarter, false, "Custom period").changed();
                });
                ui.add_enabled_ui(self.by_quarter, |ui| {
                    ui.horizontal(|ui| {
                        ui.label("Quarter");
                        changed |= quarter_picker(ui, "fund_report_quarter", &self.quarters, &mut self.quarter);
                    });
                });
            }
            ui.add_enabled_ui(!self.by_quarter, |ui| {
                ui.horizontal(|ui| {
                    ui.label("From");
                    let (from, to) = (self.from, self.to);
                    widgets::date(ui, "fund_report_from", &mut self.from);
                    ui.label("to");
                    widgets::date(ui, "fund_report_to", &mut self.to);
                    changed |= (from, to) != (self.from, self.to);
                });
            });
            if !self.by_quarter {
                ui.label(RichText::new("Each member's payments in, month by month, with a total for the period.").small().color(p.muted));
            } else {
                ui.label(RichText::new("The balance brought forward, each month's deposits, withdrawals and the balance carried.").small().color(p.muted));
            }
            if changed {
                self.offer_columns();
            }
            ui.add_space(8.0);
            ui.label(RichText::new("Columns to include, in order").strong());
            self.columns.show(ui, "fund_report_columns", 200.0);
            ui.label(RichText::new("Workbook colours are set in Tools › Settings.").small().color(p.muted));
            super::footer(ui, |ui| {
                if widgets::solid(ui, "Save Report…", p.accent).clicked() && self.generate(ctx, journal, actions) {
                    close = true;
                }
                if ui.button("Cancel").clicked() {
                    close = true;
                }
            });
        });
        open && !close
    }
}

// ------------------------------------------------------------------ loans

pub struct LoanReport {
    quarters: Vec<NaiveDate>,
    quarter: usize,
    columns: Columns,
}

impl LoanReport {
    pub fn new(journal: &Journal) -> LoanReport {
        let (quarters, quarter) = quarters(journal);
        let mut report = LoanReport { quarters, quarter, columns: Columns::default() };
        report.offer_columns();
        report
    }

    fn offer_columns(&mut self) {
        if let Some(start) = self.quarters.get(self.quarter) {
            self.columns.offer(&reports::loan_interest_columns(*start));
        }
    }

    fn generate(&self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let Some(start) = self.quarters.get(self.quarter).copied() else { return false };
        let columns = self.columns.chosen();
        if columns.is_empty() {
            actions.push(Action::Notice(Notice::info("Quarterly Loan Report", "Tick at least one column to include.")));
            return false;
        }
        let start = dates::format(start);
        let Some(path) = files::save_as("Save Report", &format!("Quarterly_Report_{start}.xlsx"), &WORKBOOK_FIRST) else { return false };
        let table = reports::loan_interest(journal, &start, Some(&columns), &mut |_, _, _| true);
        let Some(table) = read(table, "The quarterly loan report", actions) else { return false };
        write_in_background(ctx, journal, table, path, "The report", actions);
        true
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let mut close = false;
        egui::Window::new("Quarterly Loan Report").open(&mut open).collapsible(false).resizable(false).show(ctx, |ui| {
            ui.horizontal(|ui| {
                ui.label("Quarter");
                if quarter_picker(ui, "loan_report_quarter", &self.quarters, &mut self.quarter) {
                    self.offer_columns();
                }
            });
            ui.label(
                RichText::new("Per loan: the principal outstanding at the quarter's end, interest brought forward from the start of the financial year, and each month's interest.")
                    .small()
                    .color(p.muted),
            );
            ui.add_space(8.0);
            ui.label(RichText::new("Columns to include, in order").strong());
            self.columns.show(ui, "loan_report_columns", 200.0);
            ui.label(RichText::new("Workbook colours are set in Tools › Settings.").small().color(p.muted));
            super::footer(ui, |ui| {
                if widgets::solid(ui, "Save Report…", p.accent).clicked() && self.generate(ctx, journal, actions) {
                    close = true;
                }
                if ui.button("Cancel").clicked() {
                    close = true;
                }
            });
        });
        open && !close
    }
}

// ---------------------------------------------------------------- members

pub struct MembersList {
    columns: Columns,
}

impl Default for MembersList {
    fn default() -> MembersList {
        let shown = ["name", "pf_no", "id_no", "phone", "employment_status"];
        MembersList { columns: Columns::keyed(MEMBER_COLUMNS.iter().map(|(key, heading)| (*key, *heading, shown.contains(key)))) }
    }
}

impl MembersList {
    fn generate(&self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let keys = self.columns.chosen();
        if keys.is_empty() {
            actions.push(Action::Notice(Notice::info("Members List", "Tick at least one column to include.")));
            return false;
        }
        let Some(path) = files::save_as("Save Members List", "Members_List.xlsx", &WORKBOOK_FIRST) else { return false };
        let keys: Vec<&str> = keys.iter().map(String::as_str).collect();
        let table = reports::members_list(journal, &keys, &mut |_, _, _| true);
        let Some(table) = read(table, "The members list", actions) else { return false };
        write_in_background(ctx, journal, table, path, "The members list", actions);
        true
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let mut close = false;
        egui::Window::new("Members List").open(&mut open).collapsible(false).resizable(false).show(ctx, |ui| {
            ui.label("Tick the columns to include, and use ⏶ / ⏷ to set their order.");
            ui.add_space(4.0);
            self.columns.show(ui, "members_list_columns", 300.0);
            super::footer(ui, |ui| {
                if widgets::solid(ui, "Export…", p.accent).clicked() && self.generate(ctx, journal, actions) {
                    close = true;
                }
                if ui.button("Cancel").clicked() {
                    close = true;
                }
            });
        });
        open && !close
    }
}
