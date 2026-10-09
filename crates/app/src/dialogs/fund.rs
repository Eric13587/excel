//! Dialogs for a member's funds: money in and out, corrections, and the
//! benevolent enrolment.

use chrono::{Datelike, NaiveDate};
use eframe::egui::{self, RichText};
use loanmaster_core::db::model::{Enrolment, Fund, FundRow};
use loanmaster_core::money::{figure, shillings};
use loanmaster_core::{dates, funds};

use super::footer;
use crate::app::{Action, outcome};
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
}

impl Movement {
    pub fn deposit(fund: Fund, member: i64, monthly: Option<f64>, date: NaiveDate) -> Movement {
        Movement::new(Kind::Deposit, fund, member, monthly.map(figure).unwrap_or_default(), date, 0.0, 12)
    }

    pub fn withdraw(fund: Fund, member: i64, balance: f64, unlock_month: u32, date: NaiveDate) -> Movement {
        Movement::new(Kind::Withdraw, fund, member, String::new(), date, balance, unlock_month)
    }

    pub fn payout(member: i64, balance: f64, date: NaiveDate) -> Movement {
        Movement::new(Kind::Payout, Fund::Benevolent, member, String::new(), date, balance, 12)
    }

    fn new(kind: Kind, fund: Fund, member: i64, amount: String, date: NaiveDate, balance: f64, unlock_month: u32) -> Movement {
        Movement { kind, fund, member, amount, date, notes: String::new(), balance, unlock_month, override_lock: false }
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
                if widgets::solid_enabled(ui, ready, label, p.accent, problem.as_deref().unwrap_or("Enter an amount above zero.")).clicked()
                    && let Some(value) = value
                {
                    let (kind, fund, member, date, notes, override_lock) =
                        (self.kind, self.fund, self.member, self.date, self.notes.trim().to_string(), self.override_lock);
                    actions.push(Action::run(move |ws, actions| {
                        let day = dates::format(date);
                        let result = match kind {
                            Kind::Deposit => funds::deposit(&ws.journal, fund, member, value, &day, &notes),
                            Kind::Withdraw => {
                                let notes = if notes.is_empty() { "Withdrawal".to_string() } else { notes };
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
                if widgets::solid_enabled(ui, value.is_some(), "Save", p.accent, "Enter an amount above zero.").clicked()
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
                if widgets::solid_enabled(ui, value.is_some(), "Save", p.accent, "Enter a monthly amount above zero.").clicked()
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
