//! Member statements, a batch at a time: who, which months, what to show,
//! PDF or workbook — then one file per member in a folder of the user's
//! choosing.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use chrono::{Datelike, NaiveDate};
use eframe::egui::{self, RichText};
use loanmaster_core::statements::{self, LOAN_COLUMNS, SAVINGS_COLUMNS, Statement, StatementConfig};
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

pub struct Statements {
    people: Vec<Person>,
    chosen: BTreeSet<i64>,
    filter: String,
    from: NaiveDate,
    to: NaiveDate,
    /// The user set the start date: choosing members no longer moves it.
    from_set: bool,
    /// The members the start date was last worked out for.
    from_for: BTreeSet<i64>,
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
            from: dates::add_months(today, -1),
            // To the end of last month: the last one complete.
            to: dates::add_days(this_month, -1),
            from_set: false,
            from_for: BTreeSet::new(),
            show_loans: true,
            show_savings: true,
            loan_columns: Columns::named(&names(&LOAN_COLUMNS)),
            savings_columns: Columns::named(&names(&SAVINGS_COLUMNS)),
            excel: false,
        }
    }

    /// Start from the earliest entry of the members chosen, so a statement
    /// covers their whole history unless told otherwise.
    fn follow_selection(&mut self, journal: &Journal) {
        if self.from_set || self.chosen == self.from_for {
            return;
        }
        self.from_for = self.chosen.clone();
        let ids: Vec<i64> = self.chosen.iter().copied().collect();
        let earliest = if ids.is_empty() { None } else { journal.ledger().earliest_date_for(&ids).ok().flatten() };
        self.from = earliest.as_deref().and_then(|d| dates::parse(dates::date_part(d))).unwrap_or_else(|| dates::add_months(chrono::Local::now().date_naive(), -1));
    }

    fn config(&self) -> StatementConfig {
        StatementConfig {
            show_loans: self.show_loans,
            show_savings: self.show_savings,
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
        let (from, to) = statements::whole_months(self.from, self.to);
        if to < from {
            actions.push(Action::Notice(Notice::info("Statements", "The period ends before it starts.")));
            return false;
        }
        let Some(folder) = super::files::folder("Select Folder to Save Statements") else { return false };
        let config = self.config();
        let (from, to) = (dates::format(from), dates::format(to));
        // Read now, on this thread; the task only typesets and writes.
        let mut prepared: Vec<(String, Result<Statement, String>)> = Vec::new();
        for person in self.people.iter().filter(|p| self.chosen.contains(&p.id)) {
            let statement = statements::prepare(journal, person.id, &from, &to, &config).map_err(|e| {
                log::error!("Statement for {} ({}) could not be prepared: {e}", person.name, person.id);
                e.to_string()
            });
            prepared.push((person.name.clone(), statement));
        }
        let task = batch(ctx, prepared, config, Branding::of(journal), folder, from, to, self.excel);
        actions.push(Action::Task(Box::new(task)));
        true
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let mut close = false;
        egui::Window::new("Statements").open(&mut open).collapsible(false).resizable(false).show(ctx, |ui| {
            ui.horizontal_top(|ui| {
                ui.vertical(|ui| {
                    ui.set_width(330.0);
                    ui.label(RichText::new("Members").strong());
                    ui.add(egui::TextEdit::singleline(&mut self.filter).hint_text("Filter list…").desired_width(f32::INFINITY));
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
                    self.follow_selection(journal);
                    ui.label(RichText::new("Period").strong());
                    ui.horizontal(|ui| {
                        ui.label("From");
                        let before = self.from;
                        widgets::date(ui, "statements_from", &mut self.from);
                        self.from_set |= before != self.from;
                        ui.label("to");
                        widgets::date(ui, "statements_to", &mut self.to);
                    });
                    let (from, to) = statements::whole_months(self.from, self.to);
                    ui.label(RichText::new(format!("Whole months: {} to {}.", from.format("%-d %b %Y"), to.format("%-d %b %Y"))).small().color(p.muted));
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
            });
            super::footer(ui, |ui| {
                if widgets::solid(ui, "Generate…", p.accent).on_hover_text("Choose a folder; one file is written per member").clicked() && self.generate(ctx, journal, actions) {
                    close = true;
                }
                if ui.button("Cancel").clicked() {
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

#[allow(clippy::too_many_arguments)]
fn batch(
    ctx: &egui::Context,
    prepared: Vec<(String, Result<Statement, String>)>,
    config: StatementConfig,
    branding: Branding,
    folder: PathBuf,
    from: String,
    to: String,
    excel: bool,
) -> Task {
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
                let path = unused(&folder, statements::excel_file_name(&statement.name, &from, &to), &mut taken);
                loanmaster_reports::write_statement_xlsx(&statement, &config, &path)
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
                lines.push(format!("{} skipped — no activity in this period: {}", outcome.empty.len(), names(&outcome.empty)));
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
