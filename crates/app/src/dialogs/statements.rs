//! Member statements, a batch at a time: who, which loans and which months,
//! what to show, PDF or workbook — then one file per member in a folder of
//! the user's choosing.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use chrono::{Datelike, NaiveDate};
use eframe::egui::{self, RichText};
use loanmaster_core::statements::{self, LOAN_COLUMNS, LoanScope, SAVINGS_COLUMNS, Statement, StatementConfig};
use loanmaster_core::{Journal, dates};
use loanmaster_reports::Branding;

use super::columns::Columns;
use crate::app::{Action, Notice};
use crate::task::Task;
use crate::theme;
use crate::widgets;

struct Person {
    id: i64,
    name: String,
    phone: String,
    retired: bool,
}

/// The months a statement covers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Period {
    /// From the first day of the financial year to today.
    FinancialYear,
    /// From each member's own first entry to today.
    WholeHistory,
    /// The whole months between two dates.
    Dates,
}

pub struct Statements {
    people: Vec<Person>,
    chosen: BTreeSet<i64>,
    filter: String,
    loans: LoanScope,
    period: Period,
    /// The first day of this financial year.
    year_start: NaiveDate,
    from: NaiveDate,
    to: NaiveDate,
    show_loans: bool,
    show_savings: bool,
    loan_columns: Columns,
    savings_columns: Columns,
    excel: bool,
}

impl Statements {
    pub fn new(journal: &Journal) -> Statements {
        let people = journal
            .members()
            .all()
            .unwrap_or_default()
            .into_iter()
            .map(|m| Person { id: m.id, retired: !m.is_active(), name: m.name, phone: m.phone })
            .collect();
        let today = chrono::Local::now().date_naive();
        let this_month = today.with_day(1).unwrap_or(today);
        let names = |list: &[&str]| list.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        Statements {
            people,
            chosen: BTreeSet::new(),
            filter: String::new(),
            loans: LoanScope::Active,
            period: Period::FinancialYear,
            year_start: loanmaster_core::reports::fy_start(journal, today),
            // Chosen dates start as last month: the last one complete.
            from: dates::add_months(this_month, -1),
            to: dates::add_days(this_month, -1),
            show_loans: true,
            show_savings: true,
            loan_columns: Columns::named(&names(&LOAN_COLUMNS)),
            savings_columns: Columns::named(&names(&SAVINGS_COLUMNS)),
            excel: false,
        }
    }

    /// The dates one member's statement covers.
    fn span(&self, journal: &Journal, member: i64) -> (NaiveDate, NaiveDate) {
        let today = chrono::Local::now().date_naive();
        match self.period {
            Period::FinancialYear => (self.year_start, today),
            Period::WholeHistory => {
                let first = journal.ledger().earliest_date_for(&[member]).ok().flatten();
                (first.as_deref().and_then(|d| dates::parse(dates::date_part(d))).unwrap_or(today).min(today), today)
            }
            Period::Dates => statements::whole_months(self.from, self.to),
        }
    }

    fn config(&self) -> StatementConfig {
        StatementConfig {
            show_loans: self.show_loans,
            show_savings: self.show_savings,
            loans: self.loans,
            columns: self.loan_columns.chosen(),
            savings_columns: self.savings_columns.chosen(),
            ..StatementConfig::default()
        }
    }

    fn generate(&self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        if self.chosen.is_empty() {
            actions.push(Action::Notice(Notice::info("Statements", "Tick the members to print statements for.")));
            return false;
        }
        if !self.show_loans && !self.show_savings {
            actions.push(Action::Notice(Notice::info("Statements", "Include the loans section, the savings section, or both.")));
            return false;
        }
        if self.period == Period::Dates && self.to < self.from {
            actions.push(Action::Notice(Notice::info("Statements", "The period ends before it starts.")));
            return false;
        }
        let Some(folder) = super::files::folder("Select Folder to Save Statements") else { return false };
        let config = self.config();
        // Read now, on this thread; the task only typesets and writes.
        let mut prepared: Vec<(String, Result<Statement, String>)> = Vec::new();
        for person in self.people.iter().filter(|p| self.chosen.contains(&p.id)) {
            let (from, to) = self.span(journal, person.id);
            let statement = statements::prepare(journal, person.id, &dates::format(from), &dates::format(to), &config).map_err(|e| {
                log::error!("Statement for {} ({}) could not be prepared: {e}", person.name, person.id);
                e.to_string()
            });
            prepared.push((person.name.clone(), statement));
        }
        let task = batch(ctx, prepared, config, Branding::of(journal), folder, self.excel);
        actions.push(Action::Task(Box::new(task)));
        true
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let mut close = false;
        egui::Window::new("Statements").open(&mut open).collapsible(false).resizable(false).show(ctx, |ui| {
            egui::ScrollArea::vertical().max_height(widgets::dialog_room(ctx)).show(ui, |ui| ui.horizontal_top(|ui| {
                ui.vertical(|ui| {
                    ui.set_width(330.0);
                    ui.label(RichText::new("Members").strong());
                    widgets::filter_box(ui, &mut self.filter, "Filter list…", f32::INFINITY);
                    let needle = self.filter.trim().to_lowercase();
                    let shown: Vec<usize> = (0..self.people.len())
                        .filter(|&i| needle.is_empty() || format!("{} {}", self.people[i].name, self.people[i].phone).to_lowercase().contains(&needle))
                        .collect();
                    egui::Frame::new().stroke(egui::Stroke::new(1.0, p.border)).corner_radius(6).inner_margin(egui::Margin::same(4)).show(ui, |ui| {
                        egui::ScrollArea::vertical().id_salt("statement_people").max_height(330.0).auto_shrink([false, false]).show(ui, |ui| {
                            for &i in &shown {
                                let person = &self.people[i];
                                let mut ticked = self.chosen.contains(&person.id);
                                let mut text = RichText::new(format!("{} | {}", person.name, person.phone));
                                if person.retired {
                                    text = text.color(p.muted);
                                }
                                if ui.checkbox(&mut ticked, text).changed() {
                                    if ticked {
                                        self.chosen.insert(person.id);
                                    } else {
                                        self.chosen.remove(&person.id);
                                    }
                                }
                            }
                            if shown.is_empty() {
                                ui.label(RichText::new("No member matches.").color(p.muted));
                            }
                        });
                    });
                    ui.horizontal(|ui| {
                        let all = if needle.is_empty() { "Select All" } else { "Select Shown" };
                        if ui.button(all).clicked() {
                            self.chosen.extend(shown.iter().map(|&i| self.people[i].id));
                        }
                        if ui.button("Clear All").clicked() {
                            self.chosen.clear();
                        }
                        ui.label(RichText::new(format!("{} selected", self.chosen.len())).color(p.muted));
                    });
                });
                ui.add_space(16.0);
                ui.vertical(|ui| {
                    ui.label(RichText::new("Loans").strong());
                    ui.horizontal(|ui| {
                        ui.radio_value(&mut self.loans, LoanScope::Active, "Active loans");
                        ui.radio_value(&mut self.loans, LoanScope::All, "All loans");
                    });
                    let loans = match self.loans {
                        LoanScope::Active => "Loans still owing when the period ends, each from the balance it carried in.",
                        LoanScope::All => "Every loan with an entry in the period, paid off or not.",
                    };
                    ui.label(RichText::new(loans).small().color(p.muted));
                    ui.add_space(8.0);
                    ui.label(RichText::new("Period").strong());
                    ui.horizontal(|ui| {
                        ui.radio_value(&mut self.period, Period::FinancialYear, "This financial year");
                        ui.radio_value(&mut self.period, Period::WholeHistory, "Whole history");
                        ui.radio_value(&mut self.period, Period::Dates, "Dates");
                    });
                    let today = chrono::Local::now().date_naive();
                    let period = match self.period {
                        Period::FinancialYear => format!("{} to today, {}.", self.year_start.format("%-d %b %Y"), today.format("%-d %b %Y")),
                        Period::WholeHistory => "From each member's first entry to today: a full statement.".to_string(),
                        Period::Dates => {
                            ui.horizontal(|ui| {
                                ui.label("From");
                                widgets::date(ui, "statements_from", &mut self.from);
                                ui.label("to");
                                widgets::date(ui, "statements_to", &mut self.to);
                            });
                            let (from, to) = statements::whole_months(self.from, self.to);
                            format!("Whole months: {} to {}.", from.format("%-d %b %Y"), to.format("%-d %b %Y"))
                        }
                    };
                    ui.label(RichText::new(period).small().color(p.muted));
                    ui.label(
                        RichText::new("A loan or savings balance from before the period starts the statement as “Balance b/f”.")
                            .small()
                            .color(p.muted),
                    );
                    ui.add_space(8.0);
                    ui.label(RichText::new("Content").strong());
                    ui.checkbox(&mut self.show_loans, "Include Loans Section");
                    ui.checkbox(&mut self.show_savings, "Include Savings Section");
                    ui.add_space(8.0);
                    ui.label(RichText::new("Visible columns").strong());
                    ui.add_enabled_ui(self.show_loans, |ui| {
                        ui.label("Loans");
                        self.loan_columns.show(ui, "statement_loan_columns", 150.0);
                    });
                    ui.add_enabled_ui(self.show_savings, |ui| {
                        ui.label("Savings / Shares");
                        self.savings_columns.show(ui, "statement_savings_columns", 110.0);
                    });
                });
            }));
            super::footer(ui, |ui| {
                if widgets::primary(ui, "Generate…", p.accent) && self.generate(ctx, journal, actions) {
                    close = true;
                }
                if ui.button("Cancel").clicked() || widgets::escape(ui) {
                    close = true;
                }
                ui.add_space(12.0);
                ui.radio_value(&mut self.excel, true, "Excel");
                ui.radio_value(&mut self.excel, false, "PDF");
                ui.label("Save as");
            });
        });
        open && !close
    }
}

/// A file name not yet used in this batch: two members of the same name
/// must not overwrite each other.
fn unused(folder: &Path, name: String, taken: &mut BTreeSet<PathBuf>) -> PathBuf {
    let mut path = folder.join(&name);
    let (stem, extension) = match name.rsplit_once('.') {
        Some((stem, extension)) => (stem.to_string(), format!(".{extension}")),
        None => (name.clone(), String::new()),
    };
    let mut n = 2;
    while taken.contains(&path) {
        path = folder.join(format!("{stem} ({n}){extension}"));
        n += 1;
    }
    taken.insert(path.clone());
    path
}

/// What became of each member's statement.
#[derive(Default)]
struct Outcome {
    saved: Vec<String>,
    empty: Vec<String>,
    failed: Vec<String>,
    stopped: bool,
}

/// "A, B and 3 more": a long list capped so the summary stays readable.
fn names(list: &[String]) -> String {
    const CAP: usize = 10;
    if list.len() <= CAP { list.join(", ") } else { format!("{}, and {} more", list[..CAP].join(", "), list.len() - CAP) }
}

fn batch(ctx: &egui::Context, prepared: Vec<(String, Result<Statement, String>)>, config: StatementConfig, branding: Branding, folder: PathBuf, excel: bool) -> Task {
    Task::spawn(ctx, "Generating Statements", move |reporter| {
        let today = chrono::Local::now().date_naive();
        let mut outcome = Outcome::default();
        let mut taken = BTreeSet::new();
        let count = prepared.len();
        for (i, (name, statement)) in prepared.into_iter().enumerate() {
            if !reporter.step(i, count, &format!("Generating statement for {name}…")) {
                outcome.stopped = true;
                break;
            }
            let statement = match statement {
                Ok(statement) => statement,
                Err(_) => {
                    outcome.failed.push(name);
                    continue;
                }
            };
            if statement.is_empty() {
                outcome.empty.push(name);
                continue;
            }
            let written = if excel {
                let path = unused(&folder, statements::excel_file_name(&statement.name, &statement.from, &statement.to), &mut taken);
                loanmaster_reports::write_statement_xlsx(&statement, &config, &branding, &path)
            } else {
                let path = unused(&folder, statements::pdf_file_name(&statement.name, today), &mut taken);
                loanmaster_reports::write_statement_pdf(&statement, &config, &branding, &path)
            };
            match written {
                Ok(()) => outcome.saved.push(name),
                Err(e) => {
                    log::error!("Statement for {name} could not be written: {e}");
                    outcome.failed.push(name);
                }
            }
        }
        reporter.step(count, count, "Done.");
        Box::new(move |actions: &mut Vec<Action>| {
            let mut lines = vec![format!("Saved {} statement(s) to:\n{}", outcome.saved.len(), folder.display())];
            if !outcome.empty.is_empty() {
                lines.push(format!("{} skipped — nothing to show for this period: {}", outcome.empty.len(), names(&outcome.empty)));
            }
            if !outcome.failed.is_empty() {
                lines.push(format!("{} failed — see the log: {}", outcome.failed.len(), names(&outcome.failed)));
            }
            if outcome.stopped {
                lines.push("Stopped before the rest; the statements already saved were kept.".into());
            }
            let text = lines.join("\n\n");
            actions.push(Action::Status(format!("Saved {} statement(s) to {}", outcome.saved.len(), folder.display())));
            actions.push(Action::Notice(if !outcome.failed.is_empty() {
                Notice::warning("Batch complete with problems", text)
            } else if outcome.saved.is_empty() {
                Notice::warning("Nothing saved", text)
            } else {
                Notice::info("Batch complete", text)
            }));
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn two_members_of_one_name_get_two_files() {
        let mut taken = BTreeSet::new();
        let folder = Path::new("/tmp/statements");
        let first = unused(folder, "JOHN_statement_20261009.pdf".into(), &mut taken);
        let second = unused(folder, "JOHN_statement_20261009.pdf".into(), &mut taken);
        assert_eq!(first, folder.join("JOHN_statement_20261009.pdf"));
        assert_eq!(second, folder.join("JOHN_statement_20261009 (2).pdf"));
    }
}
