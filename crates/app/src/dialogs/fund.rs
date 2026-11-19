//! Dialogs for a member's funds: money in and out, corrections, the
//! benevolent enrolment, and suspending savings contributions.

use chrono::{Datelike, NaiveDate};
use eframe::egui::{self, RichText};
use loanmaster_core::db::model::{Enrolment, Fund, FundRow};
use loanmaster_core::funds::suspensions::{self, Window};
use loanmaster_core::money::{figure, shillings};
use loanmaster_core::{Journal, dates, funds};

use super::footer;
use crate::app::{Action, Notice, outcome};
use crate::theme;
use crate::widgets::{self, amount};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Deposit,
    Withdraw,
    Payout,
}

/// Money into or out of a fund.
pub struct Movement {
    kind: Kind,
    fund: Fund,
    member: i64,
    amount: String,
    date: NaiveDate,
    notes: String,
    balance: f64,
    unlock_month: u32,
    override_lock: bool,
    /// Savings deposits: when the member's contributions are suspended.
    paused: Vec<Window>,
}

impl Movement {
    pub fn deposit(fund: Fund, member: i64, monthly: Option<f64>, date: NaiveDate) -> Movement {
        Movement::new(Kind::Deposit, fund, member, monthly.map(figure).unwrap_or_default(), date, 0.0, 12)
    }

    /// A savings deposit, warned off the months the member's contributions
    /// are suspended in.
    pub fn savings_deposit(member: i64, monthly: Option<f64>, date: NaiveDate, paused: Vec<Window>) -> Movement {
        Movement { paused, ..Movement::deposit(Fund::Savings, member, monthly, date) }
    }

    pub fn withdraw(fund: Fund, member: i64, balance: f64, unlock_month: u32, date: NaiveDate) -> Movement {
        Movement::new(Kind::Withdraw, fund, member, String::new(), date, balance, unlock_month)
    }

    pub fn payout(member: i64, balance: f64, date: NaiveDate) -> Movement {
        Movement::new(Kind::Payout, Fund::Benevolent, member, String::new(), date, balance, 12)
    }

    fn new(kind: Kind, fund: Fund, member: i64, amount: String, date: NaiveDate, balance: f64, unlock_month: u32) -> Movement {
        Movement { kind, fund, member, amount, date, notes: String::new(), balance, unlock_month, override_lock: false, paused: Vec::new() }
    }

    pub fn show(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let title = match self.kind {
            Kind::Deposit => format!("{} Deposit", self.fund.label()),
            Kind::Withdraw => format!("{} Withdrawal", self.fund.label()),
            Kind::Payout => "Benevolent Payout".into(),
        };
        let response = egui::Modal::new(egui::Id::new("fund_movement")).show(ctx, |ui| {
            ui.set_width(420.0);
            ui.heading(&title);
            if self.kind != Kind::Deposit {
                ui.label(RichText::new(format!("Balance {}", shillings(self.balance))).color(p.muted));
            }
            ui.add_space(6.0);
            egui::Grid::new("movement").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                ui.label("Amount");
                ui.add(egui::TextEdit::singleline(&mut self.amount).hint_text("2,500").desired_width(140.0));
                ui.end_row();
                ui.label("Date");
                widgets::date(ui, "movement_date", &mut self.date);
                ui.end_row();
                ui.label("Notes");
                ui.add(egui::TextEdit::singleline(&mut self.notes).desired_width(260.0));
                ui.end_row();
            });
            let value = amount(&self.amount);
            let mut problem = None;
            if self.kind == Kind::Withdraw && value.is_some_and(|v| v > self.balance + 0.005) {
                problem = Some(format!("That is more than the balance of {}.", shillings(self.balance)));
            }
            if self.kind == Kind::Deposit
                && let Some(window) = suspensions::covering(&dates::format(self.date), &self.paused)
            {
                problem = Some(format!(
                    "Savings contributions are suspended {}. Choose a date outside it, or resume them first.",
                    suspensions::describe(window)
                ));
            }
            let locked = self.kind == Kind::Withdraw && self.fund == Fund::Christmas && self.date.month() != self.unlock_month;
            if locked {
                ui.add_space(4.0);
                ui.label(RichText::new(format!("Christmas withdrawals are allowed in {} only.", dates::month_name(self.unlock_month))).color(p.warning));
                ui.checkbox(&mut self.override_lock, "Override the lock — an administrator's decision");
                if !self.override_lock && problem.is_none() {
                    problem = Some("Choose a date in the unlock month, or override the lock.".into());
                }
            }
            if let Some(problem) = &problem {
                ui.label(RichText::new(problem).color(p.danger));
            }
            footer(ui, |ui| {
                let label = match self.kind {
                    Kind::Deposit => "Deposit",
                    Kind::Withdraw => "Withdraw",
                    Kind::Payout => "Pay Out",
                };
                let ready = value.is_some() && problem.is_none();
                if widgets::primary_enabled(ui, ready, label, p.accent, problem.as_deref().unwrap_or("Enter an amount above zero."))
                    && let Some(value) = value
                {
                    let (kind, fund, member, date, notes, override_lock) =
                        (self.kind, self.fund, self.member, self.date, self.notes.trim().to_string(), self.override_lock);
                    actions.push(Action::run(move |ws, actions| {
                        let day = dates::format(date);
                        let result = match kind {
                            Kind::Deposit => funds::deposit(&ws.journal, fund, member, value, &day, &notes),
                            Kind::Withdraw => {
                                funds::withdraw(&ws.journal, fund, member, value, &day, &notes, override_lock)
                            }
                            Kind::Payout => funds::payout(&ws.journal, member, value, &day, &notes),
                        };
                        if result.is_ok() {
                            ws.last_date = date;
                        }
                        outcome(actions, "Nothing was recorded", result, |balance| {
                            format!("{} {}. The balance is now {}.", label, shillings(value), shillings(balance))
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

/// Correct a fund entry's date, amount or notes.
pub struct EditRow {
    fund: Fund,
    row: FundRow,
    amount: String,
    date: NaiveDate,
    notes: String,
}

impl EditRow {
    pub fn new(fund: Fund, row: &FundRow) -> EditRow {
        EditRow {
            fund,
            row: row.clone(),
            amount: figure(row.amount),
            date: dates::parse(dates::date_part(&row.date)).unwrap_or_else(|| chrono::Local::now().date_naive()),
            notes: row.notes.clone(),
        }
    }

    pub fn show(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let response = egui::Modal::new(egui::Id::new("fund_edit")).show(ctx, |ui| {
            ui.set_width(420.0);
            ui.heading(format!("Edit {}", self.row.transaction_type));
            ui.label(RichText::new(self.fund.label()).color(p.muted));
            ui.add_space(6.0);
            egui::Grid::new("fund_edit").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                ui.label("Amount");
                ui.add(egui::TextEdit::singleline(&mut self.amount).desired_width(140.0));
                ui.end_row();
                ui.label("Date");
                widgets::date(ui, "fund_edit_date", &mut self.date);
                ui.end_row();
                ui.label("Notes");
                ui.add(egui::TextEdit::singleline(&mut self.notes).desired_width(260.0));
                ui.end_row();
            });
            footer(ui, |ui| {
                let value = amount(&self.amount);
                if widgets::primary_enabled(ui, value.is_some(), "Save", p.accent, "Enter an amount above zero.")
                    && let Some(value) = value
                {
                    let (fund, id, date, notes) = (self.fund, self.row.id, dates::format(self.date), self.notes.clone());
                    actions.push(Action::run(move |ws, actions| {
                        let result = funds::edit_row(&ws.journal, fund, id, &date, value, &notes);
                        outcome(actions, "The entry could not be saved", result, |()| "Entry saved; balances worked out again.".into());
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

/// Enrol a member in the benevolent fund, or change their contribution.
pub struct Enrol {
    member: i64,
    monthly: String,
    start: NaiveDate,
    existing: bool,
}

impl Enrol {
    pub fn new(member: i64, enrolment: Option<&Enrolment>, today: NaiveDate) -> Enrol {
        Enrol {
            member,
            monthly: enrolment.map(|e| figure(e.monthly_amount)).unwrap_or_default(),
            start: enrolment.and_then(|e| e.start_date.as_deref()).and_then(dates::parse).unwrap_or(today),
            existing: enrolment.is_some(),
        }
    }

    pub fn show(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let response = egui::Modal::new(egui::Id::new("enrol")).show(ctx, |ui| {
            ui.set_width(420.0);
            ui.heading(if self.existing { "Benevolent Contribution" } else { "Enrol in the Benevolent Fund" });
            ui.add_space(6.0);
            egui::Grid::new("enrol").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                ui.label("Monthly contribution");
                ui.add(egui::TextEdit::singleline(&mut self.monthly).hint_text("500").desired_width(140.0));
                ui.end_row();
                ui.label("First contribution due");
                widgets::date(ui, "enrol_start", &mut self.start);
                ui.end_row();
            });
            ui.label(RichText::new("Contributions are then due on the same day each month.").small().color(p.muted));
            footer(ui, |ui| {
                let value = amount(&self.monthly);
                if widgets::primary_enabled(ui, value.is_some(), "Save", p.accent, "Enter a monthly amount above zero.")
                    && let Some(value) = value
                {
                    let (member, start) = (self.member, dates::format(self.start));
                    actions.push(Action::run(move |ws, actions| {
                        let result = funds::enrol(&ws.journal, member, value, &start);
                        outcome(actions, "The enrolment could not be saved", result, |()| {
                            format!("Enrolled at {} a month from {start}.", shillings(value))
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

/// Suspend a member's savings contributions for some months, or until they
/// are resumed.
pub struct SuspendSavings {
    member: i64,
    name: String,
    /// Any day of the first month paused.
    start: NaiveDate,
    open_ended: bool,
    months: u32,
    problem: Option<String>,
}

impl SuspendSavings {
    pub fn new(member: i64, name: &str, start: NaiveDate) -> SuspendSavings {
        SuspendSavings { member, name: name.into(), start, open_ended: false, months: 3, problem: None }
    }

    fn window(&self) -> Window {
        let first = suspensions::month_start(self.start);
        let end = (!self.open_ended).then(|| dates::format(dates::add_months(first, self.months as i32)));
        (dates::format(first), end)
    }

    fn save(&mut self, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let months = (!self.open_ended).then_some(self.months);
        match suspensions::suspend(journal, self.member, &dates::format(self.start), months) {
            Ok(_) => {
                let span = suspensions::in_force(journal, self.member, &dates::format(self.start)).ok().flatten();
                let when = span.map(|s| suspensions::describe(&suspensions::window(&s))).unwrap_or_else(|| suspensions::describe(&self.window()));
                actions.push(Action::Status(format!("{}'s savings contributions are suspended {when}.", self.name)));
                actions.push(Action::Changed);
                true
            }
            Err(e) if e.is_user_facing() => {
                self.problem = Some(e.to_string());
                false
            }
            Err(e) => {
                actions.push(Action::Notice(Notice::from_error("The suspension could not be recorded", &e)));
                true
            }
        }
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let response = egui::Modal::new(egui::Id::new("suspend_savings")).show(ctx, |ui| {
            ui.set_width(460.0);
            ui.heading("Suspend Savings Contributions");
            ui.label(RichText::new(&self.name).color(p.muted));
            ui.add_space(6.0);
            egui::Grid::new("suspend_savings").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                ui.label("From");
                ui.horizontal(|ui| {
                    if widgets::date(ui, "suspend_savings_start", &mut self.start).changed() {
                        self.problem = None;
                    }
                    ui.label(RichText::new(self.start.format("%B %Y").to_string()).strong());
                });
                ui.end_row();
                ui.label("For");
                ui.horizontal(|ui| {
                    ui.radio_value(&mut self.open_ended, false, "");
                    ui.add_enabled(!self.open_ended, egui::DragValue::new(&mut self.months).range(1..=suspensions::MAX_MONTHS).suffix(" month(s)"));
                });
                ui.end_row();
                ui.label("");
                ui.radio_value(&mut self.open_ended, true, "Until I resume them");
                ui.end_row();
            });
            ui.add_space(6.0);
            let window = self.window();
            let effect = match window.1.as_deref().and_then(dates::parse) {
                Some(resume) => format!(
                    "⏸ No savings deposit is taken {}; contributions resume in {} by themselves.",
                    suspensions::describe(&window),
                    resume.format("%B %Y")
                ),
                None => format!("⏸ No savings deposit is taken {} — until you press Resume.", suspensions::describe(&window)),
            };
            ui.add(egui::Label::new(RichText::new(effect).color(p.muted)).wrap());
            ui.add(
                egui::Label::new(
                    RichText::new(
                        "Withdrawals are still allowed. Catch Up and the Ledger Grid's Fill Forward pass over these months, \
                         and the member's statement says they were suspended. A past month records a suspension that \
                         already happened.",
                    )
                    .small()
                    .color(p.muted),
                )
                .wrap(),
            );
            if let Some(problem) = &self.problem {
                ui.add_space(6.0);
                ui.add(egui::Label::new(RichText::new(problem).color(p.danger)).wrap());
            }
            footer(ui, |ui| {
                if widgets::primary(ui, "Suspend", p.warning) && self.save(journal, actions) {
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
