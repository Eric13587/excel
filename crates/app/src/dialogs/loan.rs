//! Dialogs for a member's loans: working out a principal, catching up,
//! topping up, suspending, and correcting entries.

use chrono::{Datelike, NaiveDate};
use eframe::egui::{self, RichText};
use loanmaster_core::db::model::{Fund, LedgerRow, Suspension, event};
use loanmaster_core::loans::service::{self, principal_for_deduction, terms_for};
use loanmaster_core::loans::{edits, suspensions};
use loanmaster_core::money::{figure, shillings};
use loanmaster_core::{Journal, dates, funds};

use super::footer;
use crate::app::{Action, Notice, outcome};
use crate::theme;
use crate::widgets;
use crate::workspace::Screen;

fn amount(text: &str) -> Option<f64> {
    text.replace(',', "").trim().parse::<f64>().ok().filter(|v| v.is_finite() && *v > 0.0)
}

fn month_end(date: NaiveDate) -> NaiveDate {
    dates::add_days(dates::add_months(date.with_day(1).unwrap_or(date), 1), -1)
}

fn modal(id: &str) -> egui::Modal {
    egui::Modal::new(egui::Id::new(id))
}

// ------------------------------------------------------------ principal

/// "What can this member borrow if they can afford so much a month?"
pub struct PrincipalCalculator {
    deduction: String,
    months: String,
    rate: String,
}

impl PrincipalCalculator {
    pub fn new(default_rate: f64) -> PrincipalCalculator {
        PrincipalCalculator { deduction: String::new(), months: String::new(), rate: format!("{}", default_rate * 100.0) }
    }

    pub fn show(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let response = modal("principal_calculator").show(ctx, |ui| {
            ui.set_width(400.0);
            ui.heading("Work Out the Principal");
            ui.label(RichText::new("The largest loan whose instalment fits a monthly deduction.").color(p.muted));
            ui.add_space(8.0);
            egui::Grid::new("calculator").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                ui.label("Monthly deduction");
                ui.add(egui::TextEdit::singleline(&mut self.deduction).hint_text("2,300").desired_width(140.0));
                ui.end_row();
                ui.label("Months");
                ui.add(egui::TextEdit::singleline(&mut self.months).hint_text("11").desired_width(140.0));
                ui.end_row();
                ui.label("Rate (%)");
                ui.add(egui::TextEdit::singleline(&mut self.rate).desired_width(140.0));
                ui.end_row();
            });
            let months = self.months.trim().parse::<i64>().ok();
            let rate = self.rate.replace(',', "").trim().parse::<f64>().ok().map(|r| r / 100.0);
            let found = match (amount(&self.deduction), months, rate) {
                (Some(d), Some(m), Some(r)) => principal_for_deduction(d, m, r).map(|p| (p, m, r)),
                _ => None,
            };
            ui.add_space(8.0);
            match found {
                Some((principal, months, rate)) => {
                    let terms = terms_for(principal, months, rate);
                    ui.label(RichText::new(format!("Principal: {}", shillings(principal))).size(18.0).strong().color(p.accent));
                    ui.label(RichText::new(format!("Instalment {} a month over {months} months.", shillings(terms.installment))).color(p.muted));
                }
                None => {
                    ui.label(RichText::new("Enter the deduction, the months and the rate.").color(p.muted));
                }
            }
            footer(ui, |ui| {
                if widgets::solid_enabled(ui, found.is_some(), "Use This", p.success, "Enter the figures first.").clicked()
                    && let Some((principal, months, rate)) = found
                {
                    actions.push(Action::run(move |ws, _| {
                        if let Screen::Member(m) = &mut ws.screen {
                            m.new_loan.principal = figure(principal);
                            m.new_loan.months = months.to_string();
                            m.new_loan.rate = format!("{}", rate * 100.0);
                        }
                    }));
                    open = false;
                }
                if ui.button("Cancel").clicked() {
                    open = false;
                }
            });
        });
        open && !response.should_close()
    }
}

// ------------------------------------------------------------- catch up

enum Target {
    Loan { member: i64, reference: String },
    Fund { fund: Fund, member: i64, monthly: f64 },
    Benevolent { member: i64 },
}

/// "Catch up until when?" — every catch-up stops at a month the user names,
/// because one carried past the last payroll run invents deductions nobody
/// took.
pub struct CatchUp {
    target: Target,
    month: NaiveDate,
}

impl CatchUp {
    pub fn loan(member: i64, reference: &str, _due: &str) -> CatchUp {
        let last_month = dates::add_months(chrono::Local::now().date_naive(), -1);
        CatchUp { target: Target::Loan { member, reference: reference.into() }, month: last_month }
    }

    pub fn fund(fund: Fund, member: i64, monthly: f64) -> CatchUp {
        CatchUp { target: Target::Fund { fund, member, monthly }, month: chrono::Local::now().date_naive() }
    }

    pub fn benevolent(member: i64) -> CatchUp {
        CatchUp { target: Target::Benevolent { member }, month: chrono::Local::now().date_naive() }
    }

    pub fn show(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let what = match &self.target {
            Target::Loan { reference, .. } => format!("deductions on {reference}"),
            Target::Fund { fund, monthly, .. } => format!("{} deposits of {}", fund.label().to_lowercase(), shillings(*monthly)),
            Target::Benevolent { .. } => "benevolent contributions".into(),
        };
        let response = modal("catch_up").show(ctx, |ui| {
            ui.set_width(420.0);
            ui.heading("Catch Up To");
            ui.add_space(4.0);
            ui.label(format!("Record {what} for every month up to and including:"));
            ui.horizontal(|ui| {
                widgets::date(ui, "catch_up_month", &mut self.month);
                ui.label(RichText::new(self.month.format("%B %Y").to_string()).strong());
            });
            ui.add_space(4.0);
            ui.label(
                RichText::new(
                    "Nothing after this month is created. Set it to the month your last payroll run covered if you are \
                     reconciling the past.",
                )
                .small()
                .color(p.muted),
            );
            footer(ui, |ui| {
                if widgets::solid(ui, "Catch Up", p.info).clicked() {
                    let to = dates::format(month_end(self.month));
                    let job = match &self.target {
                        Target::Loan { member, reference } => {
                            let (member, reference) = (*member, reference.clone());
                            Action::run(move |ws, actions| {
                                let from = ws.journal.loans().by_ref(member, &reference).ok().flatten().map(|l| l.due().to_string()).unwrap_or_default();
                                let result = service::deduct_range(&ws.journal, member, &reference, &from, &to);
                                outcome(actions, "The loan could not be caught up", result, |n| match n {
                                    0 => format!("{reference} is already up to date."),
                                    n => format!("{n} deduction(s) taken on {reference}."),
                                });
                            })
                        }
                        Target::Fund { fund, member, monthly } => {
                            let (fund, member, monthly) = (*fund, *member, *monthly);
                            Action::run(move |ws, actions| {
                                let result = funds::catch_up_pot(&ws.journal, fund, member, Some(monthly), Some(&to));
                                outcome(actions, "The fund could not be caught up", result, |n| match n {
                                    0 => "Already up to date.".into(),
                                    n => format!("{n} deposit(s) of {} added.", shillings(monthly)),
                                });
                            })
                        }
                        Target::Benevolent { member } => {
                            let member = *member;
                            Action::run(move |ws, actions| {
                                let result = funds::catch_up_benevolent(&ws.journal, member, Some(&to));
                                outcome(actions, "The contributions could not be caught up", result, |n| match n {
                                    0 => "Already up to date.".into(),
                                    n => format!("{n} contribution(s) added."),
                                });
                            })
                        }
                    };
                    actions.push(job);
                    open = false;
                }
                if ui.button("Cancel").clicked() {
                    open = false;
                }
            });
        });
        open && !response.should_close()
    }
}

// --------------------------------------------------------------- top-up

pub struct TopUp {
    member: i64,
    reference: String,
    amount: String,
    months: String,
    date: NaiveDate,
    /// A top-up must fall in a later month than the loan's last entry.
    earliest: NaiveDate,
}

impl TopUp {
    pub fn new(member: i64, reference: &str, earliest: NaiveDate, duration: Option<i64>) -> TopUp {
        TopUp {
            member,
            reference: reference.into(),
            amount: String::new(),
            months: duration.map(|d| d.to_string()).unwrap_or_default(),
            date: earliest,
            earliest: earliest.with_day(1).unwrap_or(earliest),
        }
    }

    pub fn show(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let response = modal("top_up").show(ctx, |ui| {
            ui.set_width(420.0);
            ui.heading(format!("Top Up {}", self.reference));
            ui.add_space(6.0);
            egui::Grid::new("top_up").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                ui.label("Amount to lend");
                ui.add(egui::TextEdit::singleline(&mut self.amount).hint_text("20,000").desired_width(140.0));
                ui.end_row();
                ui.label("New term (months)");
                ui.add(egui::TextEdit::singleline(&mut self.months).hint_text("12").desired_width(140.0));
                ui.end_row();
                ui.label("Date");
                widgets::date(ui, "top_up_date", &mut self.date);
                ui.end_row();
            });
            let months = self.months.trim().parse::<i64>().ok().filter(|m| *m >= 1);
            let too_early = self.date < self.earliest;
            ui.add_space(4.0);
            if too_early {
                ui.label(RichText::new(format!(
                    "A top-up must fall after the loan's last entry — {} or later.",
                    self.earliest.format("%B %Y")
                ))
                .color(p.warning));
            } else {
                ui.label(
                    RichText::new("The loan is re-termed: what is owed plus the top-up and its interest, over the new term.")
                        .small()
                        .color(p.muted),
                );
            }
            footer(ui, |ui| {
                let ready = amount(&self.amount).is_some() && months.is_some() && !too_early;
                if widgets::solid_enabled(ui, ready, "Top Up", p.success, "Enter the amount, the term and a date after the last entry.").clicked()
                    && let (Some(value), Some(months)) = (amount(&self.amount), months)
                {
                    let (member, reference, date) = (self.member, self.reference.clone(), self.date);
                    actions.push(Action::run(move |ws, actions| {
                        let result = service::top_up(&ws.journal, member, &reference, value, months, Some(&dates::format(date)));
                        if result.is_ok() {
                            ws.last_date = date;
                        }
                        let instalment = ws.journal.loans().by_ref(member, &reference).ok().flatten().map(|l| l.installment).unwrap_or(0.0);
                        outcome(actions, "The loan could not be topped up", result, |()| {
                            format!("{reference} topped up by {}; the instalment is now {}.", shillings(value), shillings(instalment))
                        });
                    }));
                    open = false;
                }
                if ui.button("Cancel").clicked() {
                    open = false;
                }
            });
        });
        open && !response.should_close()
    }
}

// ------------------------------------------------------------- suspending

enum Stage {
    Choose,
    /// Deductions already exist in the window: rebuild, or only record it.
    Overlap { existing: i64, rows: i64, rebuildable: bool },
}

/// Suspend a loan for a set number of months, or until resumed.
pub struct Suspend {
    loan_id: i64,
    member: i64,
    reference: String,
    start: NaiveDate,
    open_ended: bool,
    months: u32,
    stage: Stage,
}

impl Suspend {
    pub fn new(loan_id: i64, member: i64, reference: &str, start: NaiveDate) -> Suspend {
        Suspend { loan_id, member, reference: reference.into(), start, open_ended: false, months: 3, stage: Stage::Choose }
    }

    fn end(&self) -> Option<String> {
        (!self.open_ended).then(|| dates::format(dates::add_months(self.start, self.months as i32)))
    }

    fn job(&self, rebuild: bool) -> Action {
        let (loan_id, member, reference) = (self.loan_id, self.member, self.reference.clone());
        let (start, end) = (dates::format(self.start), self.end());
        Action::run(move |ws, actions| {
            if rebuild && let Err(e) = ws.journal.snapshot(&format!("rebuild-{reference}")) {
                actions.push(Action::Notice(Notice::from_error("No backup could be saved, so nothing was changed", &e)));
                return;
            }
            let journal = &ws.journal;
            let result = journal.transaction(|| {
                let recorded = suspensions::record(journal, loan_id, &start, end.as_deref(), None)?;
                let created = if rebuild { service::rebuild_schedule(journal, member, &reference, None)? } else { 0 };
                Ok((recorded, created))
            });
            outcome(actions, "The suspension could not be recorded", result, |(recorded, created)| {
                if rebuild {
                    format!("{reference} rebuilt: {created} deduction(s) re-created around the suspension.")
                } else if recorded == suspensions::Recorded::Active {
                    format!("{reference} suspended from {start}.")
                } else {
                    format!("A past suspension of {reference} was recorded.")
                }
            });
        })
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let response = modal("suspend").show(ctx, |ui| {
            ui.set_width(460.0);
            ui.heading(format!("Suspend {}", self.reference));
            ui.add_space(6.0);
            match self.stage {
                Stage::Choose => {
                    egui::Grid::new("suspend").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                        ui.label("Suspended from");
                        widgets::date(ui, "suspend_start", &mut self.start);
                        ui.end_row();
                        ui.label("For");
                        ui.horizontal(|ui| {
                            ui.radio_value(&mut self.open_ended, false, "");
                            ui.add_enabled(!self.open_ended, egui::DragValue::new(&mut self.months).range(1..=60).suffix(" month(s)"));
                        });
                        ui.end_row();
                        ui.label("");
                        ui.radio_value(&mut self.open_ended, true, "Until I resume it");
                        ui.end_row();
                    });
                    ui.add_space(6.0);
                    let effect = match self.end() {
                        Some(end) => format!("⏸ Deductions resume by themselves on {end} — {} month(s) skipped; the term extends by the same.", self.months),
                        None => "⏸ Open-ended: deductions stay paused until you press Resume. Catch Up and Deduct stay off on this loan, and the term extends for every month skipped.".into(),
                    };
                    ui.label(RichText::new(effect).color(p.muted));
                    ui.label(
                        RichText::new("A past start date records a suspension that already happened; statements show it as months without deductions.")
                            .small()
                            .color(p.muted),
                    );
                    footer(ui, |ui| {
                        if widgets::solid(ui, "Suspend", p.warning).clicked() {
                            let start = dates::format(self.start);
                            let end = self.end();
                            match suspensions::deductions_in_period(journal, self.member, &self.reference, &start, end.as_deref()) {
                                Ok(0) => {
                                    actions.push(self.job(false));
                                    open = false;
                                }
                                Ok(existing) => {
                                    let rows = journal.ledger().count_events(self.member, &self.reference, &[event::REPAYMENT, event::INTEREST], None, None).unwrap_or(0);
                                    let rebuildable = !service::has_restructure_events(journal, self.member, &self.reference).unwrap_or(true)
                                        && !service::has_manual_edits(journal, self.member, &self.reference).unwrap_or(true);
                                    self.stage = Stage::Overlap { existing, rows, rebuildable };
                                }
                                Err(e) => {
                                    actions.push(Action::Notice(Notice::from_error("The loan could not be checked", &e)));
                                    open = false;
                                }
                            }
                        }
                        if ui.button("Cancel").clicked() {
                            open = false;
                        }
                    });
                }
                Stage::Overlap { existing, rows, rebuildable } => {
                    let window = match self.end() {
                        Some(end) => format!("between {} and {end}", dates::format(self.start)),
                        None => format!("on or after {}", dates::format(self.start)),
                    };
                    ui.label(format!("{existing} deduction(s) are already recorded on {} {window}.", self.reference));
                    ui.add_space(4.0);
                    if rebuildable {
                        ui.label(format!(
                            "To apply the suspension, the loan's schedule is rebuilt: all {rows} repayment and interest entries on {} \
                             are deleted and re-created from the issue date, skipping the suspended months. A backup is saved first.",
                            self.reference
                        ));
                    } else {
                        ui.label(RichText::new("This loan has top-ups, buy-offs or hand-edited entries, so it cannot be rebuilt automatically.").color(p.warning));
                    }
                    ui.label(RichText::new("Recording it only as a note leaves those deductions in place; statements will show both.").color(p.muted));
                    footer(ui, |ui| {
                        if rebuildable && widgets::solid(ui, "Rebuild the Schedule", p.danger).clicked() {
                            actions.push(self.job(true));
                            open = false;
                        }
                        if ui.button("Only Record It").clicked() {
                            actions.push(self.job(false));
                            open = false;
                        }
                        if ui.button("Cancel").clicked() {
                            open = false;
                        }
                    });
                }
            }
        });
        open && !response.should_close()
    }
}

/// Every suspension recorded on a loan, and a way to remove one.
pub struct Suspensions {
    loan_id: i64,
    member: i64,
    reference: String,
    selected: Option<i64>,
}

impl Suspensions {
    pub fn new(loan_id: i64, member: i64, reference: &str) -> Suspensions {
        Suspensions { loan_id, member, reference: reference.into(), selected: None }
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let spans = journal.loans().suspensions_for_loan(self.loan_id).unwrap_or_default();
        let mut open = true;
        let response = modal("suspensions").show(ctx, |ui| {
            ui.set_width(620.0);
            ui.heading(format!("Suspensions — {}", self.reference));
            ui.label(
                RichText::new(
                    "Periods when deductions were skipped. Removing one means it never applied: you choose whether the months it \
                     skipped are deducted now. Resuming, by contrast, ends a suspension and leaves its months skipped.",
                )
                .color(p.muted),
            );
            ui.add_space(6.0);
            egui::Grid::new("spans").striped(true).num_columns(5).spacing([16.0, 6.0]).show(ui, |ui| {
                for title in ["From", "Until", "Status", "Resumed", "Recorded"] {
                    ui.label(RichText::new(title).strong());
                }
                ui.end_row();
                for span in &spans {
                    let selected = self.selected == Some(span.id);
                    if ui.selectable_label(selected, dates::date_part(&span.start_date)).clicked() {
                        self.selected = Some(span.id);
                    }
                    ui.label(span.suspend_until.as_deref().map(dates::date_part).unwrap_or("indefinite"));
                    ui.label(if span.status == "active" { "live" } else { "ended" });
                    ui.label(span.resumed_date.as_deref().map(dates::date_part).unwrap_or("—"));
                    ui.label(span.created_at.as_deref().map(dates::date_part).unwrap_or(""));
                    ui.end_row();
                }
            });
            footer(ui, |ui| {
                if ui.button("Close").clicked() {
                    open = false;
                }
                let chosen = spans.iter().find(|s| Some(s.id) == self.selected);
                if widgets::solid_enabled(ui, chosen.is_some(), "Remove…", p.warning, "Choose a suspension first.").clicked()
                    && let Some(span) = chosen
                {
                    actions.push(Action::dialog(ClearSuspension::new(span.id, self.member, &self.reference, span)));
                    open = false;
                }
            });
        });
        open && !response.should_close()
    }
}

/// Removing a suspension: leave its months skipped, or deduct them now.
pub struct ClearSuspension {
    id: i64,
    member: i64,
    reference: String,
    description: String,
}

impl ClearSuspension {
    pub fn new(id: i64, member: i64, reference: &str, span: &Suspension) -> ClearSuspension {
        let until = span.suspend_until.as_deref().map(dates::date_part).unwrap_or("no end date");
        ClearSuspension {
            id,
            member,
            reference: reference.into(),
            description: format!("from {} to {until}", dates::date_part(&span.start_date)),
        }
    }

    fn job(&self, rebuild: bool) -> Action {
        let (id, member, reference) = (self.id, self.member, self.reference.clone());
        Action::run(move |ws, actions| {
            if rebuild && let Err(e) = ws.journal.snapshot(&format!("clear-suspension-{reference}")) {
                actions.push(Action::Notice(Notice::from_error("No backup could be saved, so nothing was changed", &e)));
                return;
            }
            let result = suspensions::remove(&ws.journal, member, &reference, id, rebuild);
            outcome(actions, "The suspension could not be removed", result, |(_, created)| {
                if rebuild {
                    format!("Suspension removed; {reference} was rebuilt and {created} deduction(s) now cover the period.")
                } else {
                    format!("Suspension removed from {reference}; its months stay skipped.")
                }
            });
        })
    }

    pub fn show(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let response = modal("clear_suspension").show(ctx, |ui| {
            ui.set_width(460.0);
            ui.heading("Remove Suspension");
            ui.add_space(4.0);
            ui.label(format!("Remove the suspension on {} {}?", self.reference, self.description));
            ui.label(RichText::new("What should happen to the months it skipped?").strong());
            ui.label(
                RichText::new("Deducting them rebuilds the loan's schedule from its issue date; a backup is saved first.")
                    .small()
                    .color(p.muted),
            );
            footer(ui, |ui| {
                if ui.button("Cancel").clicked() {
                    open = false;
                }
                if widgets::solid(ui, "Deduct Them Now", p.danger).clicked() {
                    actions.push(self.job(true));
                    open = false;
                }
                if widgets::solid(ui, "Leave Them Skipped", p.accent).clicked() {
                    actions.push(self.job(false));
                    open = false;
                }
            });
        });
        open && !response.should_close()
    }
}

// ------------------------------------------------------- correcting entries

/// Move an entry to another date, and optionally every entry after it.
pub struct EntryDate {
    member: i64,
    row_id: i64,
    what: String,
    old: NaiveDate,
    date: NaiveDate,
    cascade: bool,
}

impl EntryDate {
    pub fn new(member: i64, row: &LedgerRow) -> EntryDate {
        let old = dates::parse(dates::date_part(&row.date)).unwrap_or_else(|| chrono::Local::now().date_naive());
        EntryDate { member, row_id: row.id, what: format!("{} of {}", row.event_type, dates::date_part(&row.date)), old, date: old, cascade: true }
    }

    pub fn show(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let response = modal("entry_date").show(ctx, |ui| {
            ui.set_width(440.0);
            ui.heading("Change Date");
            ui.label(format!("Move the {} to:", self.what));
            widgets::date(ui, "entry_date", &mut self.date);
            ui.checkbox(&mut self.cascade, "Move every later entry on this loan by the same amount");
            ui.label(
                RichText::new("Moving one month moves each later entry one month on, so the schedule keeps its shape.")
                    .small()
                    .color(p.muted),
            );
            footer(ui, |ui| {
                if widgets::solid_enabled(ui, self.date != self.old, "Move", p.accent, "Choose a different date.").clicked() {
                    let (member, row_id, date, cascade) = (self.member, self.row_id, dates::format(self.date), self.cascade);
                    actions.push(Action::run(move |ws, actions| {
                        let result = edits::move_date(&ws.journal, member, row_id, &date, cascade);
                        outcome(actions, "The entry could not be moved", result, |n| format!("{n} entr{} moved.", if n == 1 { "y" } else { "ies" }));
                    }));
                    open = false;
                }
                if ui.button("Cancel").clicked() {
                    open = false;
                }
            });
        });
        open && !response.should_close()
    }
}

/// Correct an amount: a repayment re-terms the loan around it, a top-up is
/// re-done with the corrected figures, an interest charge is set by hand.
pub struct EntryAmount {
    member: i64,
    row: LedgerRow,
    amount: String,
    months: String,
    date: NaiveDate,
    notes: String,
    loaded: bool,
}

impl EntryAmount {
    pub fn new(member: i64, row: &LedgerRow) -> EntryAmount {
        let value = if row.event_type == event::REPAYMENT { row.deducted } else { row.added };
        EntryAmount {
            member,
            row: row.clone(),
            amount: figure(value),
            months: String::new(),
            date: dates::parse(dates::date_part(&row.date)).unwrap_or_else(|| chrono::Local::now().date_naive()),
            notes: row.notes.clone(),
            loaded: false,
        }
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        if !self.loaded {
            self.loaded = true;
            // The term a top-up was made over: its own note, else the loan's.
            let noted = self.row.notes.split("Duration: ").nth(1).and_then(|rest| rest.split('m').next()).and_then(|m| m.trim().parse::<i64>().ok());
            let loan = journal.loans().by_ref(self.member, &self.row.loan_ref).ok().flatten().and_then(|l| l.duration_months);
            self.months = noted.or(loan).map(|m| m.to_string()).unwrap_or_default();
        }
        let p = theme::palette(ctx);
        let kind = self.row.event_type.clone();
        let mut open = true;
        let response = modal("entry_amount").show(ctx, |ui| {
            ui.set_width(480.0);
            ui.heading(match kind.as_str() {
                event::REPAYMENT => "Change Repayment",
                event::TOP_UP => "Edit Top-Up",
                _ => "Change Interest Charge",
            });
            ui.label(RichText::new(format!("{} · {} · {}", self.row.loan_ref, kind, dates::date_part(&self.row.date))).color(p.muted));
            ui.add_space(6.0);
            egui::Grid::new("entry_amount").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                ui.label(match kind.as_str() {
                    event::REPAYMENT => "Deducted",
                    event::TOP_UP => "Topped up",
                    _ => "Charged",
                });
                ui.add(egui::TextEdit::singleline(&mut self.amount).desired_width(140.0));
                ui.end_row();
                if kind == event::TOP_UP {
                    ui.label("Term (months)");
                    ui.add(egui::TextEdit::singleline(&mut self.months).desired_width(140.0));
                    ui.end_row();
                    ui.label("Date");
                    widgets::date(ui, "top_up_edit_date", &mut self.date);
                    ui.end_row();
                }
                ui.label("Notes");
                ui.add(egui::TextEdit::singleline(&mut self.notes).desired_width(280.0));
                ui.end_row();
            });
            ui.add_space(6.0);
            let explanation = match kind.as_str() {
                event::REPAYMENT => {
                    "The new figure becomes the loan's instalment: the remaining term is worked out from what is left, this month's \
                     and later interest charges move to the new monthly rate, and every later entry is re-derived."
                }
                event::TOP_UP => "The loan is re-termed as if the top-up had been made with these figures, at the loan's own rate.",
                _ => "A hand-set charge is locked: re-deriving the loan keeps it as typed.",
            };
            ui.label(RichText::new(explanation).small().color(p.muted));
            footer(ui, |ui| {
                let value = amount(&self.amount);
                let months = self.months.trim().parse::<i64>().ok().filter(|m| *m >= 1);
                let ready = value.is_some() && (kind != event::TOP_UP || months.is_some());
                if widgets::solid_enabled(ui, ready, "Save", p.accent, "Enter an amount above zero (and a term).").clicked()
                    && let Some(value) = value
                {
                    let (member, row, notes, date) = (self.member, self.row.clone(), self.notes.clone(), dates::format(self.date));
                    let months = months.unwrap_or(1);
                    actions.push(Action::run(move |ws, actions| {
                        let journal = &ws.journal;
                        let result = match row.event_type.as_str() {
                            event::REPAYMENT => edits::change_repayment(journal, member, row.id, value, &notes),
                            event::TOP_UP => edits::edit_top_up(journal, member, row.id, &date, value, months, &notes),
                            _ => edits::edit_entry(journal, member, row.id, &row.date, value, 0.0, &notes, true),
                        };
                        outcome(actions, "The entry could not be changed", result, |()| format!("{} on {} changed.", row.event_type, row.loan_ref));
                    }));
                    open = false;
                }
                if ui.button("Cancel").clicked() {
                    open = false;
                }
            });
        });
        open && !response.should_close()
    }
}

/// Correct an entry's notes; nothing else moves.
pub struct EntryNotes {
    member: i64,
    row: LedgerRow,
    notes: String,
}

impl EntryNotes {
    pub fn new(member: i64, row: &LedgerRow) -> EntryNotes {
        EntryNotes { member, row: row.clone(), notes: row.notes.clone() }
    }

    pub fn show(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let response = modal("entry_notes").show(ctx, |ui| {
            ui.set_width(460.0);
            ui.heading("Edit Notes");
            ui.label(RichText::new(format!("{} · {} · {}", self.row.loan_ref, self.row.event_type, dates::date_part(&self.row.date))).color(p.muted));
            ui.add(egui::TextEdit::multiline(&mut self.notes).desired_rows(3).desired_width(f32::INFINITY));
            footer(ui, |ui| {
                if widgets::solid(ui, "Save", p.accent).clicked() {
                    let (member, row, notes) = (self.member, self.row.clone(), self.notes.clone());
                    actions.push(Action::run(move |ws, actions| {
                        let result = edits::edit_entry(&ws.journal, member, row.id, &row.date, row.added, row.deducted, notes.trim(), false);
                        outcome(actions, "The notes could not be saved", result, |()| "Notes saved.".into());
                    }));
                    open = false;
                }
                if ui.button("Cancel").clicked() {
                    open = false;
                }
            });
        });
        open && !response.should_close()
    }
}
