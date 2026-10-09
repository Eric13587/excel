//! The Savings, Christmas and Benevolent tabs: the balance, what can be done
//! to the fund, and every movement.

use chrono::{Datelike, NaiveDate};
use eframe::egui::{self, RichText};
use egui_extras::{Column, TableBuilder};
use loanmaster_core::db::model::{Fund, FundRow};
use loanmaster_core::money::shillings;
use loanmaster_core::{dates, funds};

use super::{MemberData, MemberScreen, slot};
use crate::app::{Action, outcome};
use crate::dialogs::confirm::Confirm;
use crate::dialogs::fund as fund_dialogs;
use crate::dialogs::loan::CatchUp;
use crate::theme;
use crate::widgets;

fn title(fund: Fund) -> &'static str {
    match fund {
        Fund::Savings => "Savings / Shares",
        Fund::Christmas => "Christmas Fund",
        Fund::Benevolent => "Benevolent Fund",
    }
}

pub fn show(screen: &mut MemberScreen, ui: &mut egui::Ui, fund: Fund, data: &MemberData, last_date: NaiveDate, actions: &mut Vec<Action>) {
    let p = theme::palette(ui.ctx());
    let member = screen.id;
    let i = slot(fund);
    let rows = &data.funds[i];
    egui::Frame::new().fill(p.card).stroke(egui::Stroke::new(1.0, p.border)).corner_radius(10).inner_margin(12).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            ui.label(RichText::new(title(fund)).size(17.0).strong());
            ui.label(RichText::new(format!("Balance {}", shillings(data.balances[i]))).size(17.0).color(p.accent));
        });
        match fund {
            Fund::Christmas => {
                ui.label(
                    RichText::new(format!("Withdrawals are allowed in {} only.", dates::month_name(data.unlock_month)))
                        .small()
                        .color(p.muted),
                );
            }
            Fund::Benevolent => {
                let text = match &data.enrolment {
                    Some(e) if e.active && e.monthly_amount > 0.0 => format!(
                        "Enrolled: {} a month from {}; next contribution due {}.",
                        shillings(e.monthly_amount),
                        e.start_date.as_deref().unwrap_or("—"),
                        e.next_due_date.as_deref().unwrap_or("—")
                    ),
                    Some(_) => "Enrolment paused — no contributions are taken.".into(),
                    None => "Not enrolled — use Enrol to set a monthly contribution.".into(),
                };
                ui.label(RichText::new(text).small().color(p.muted));
            }
            Fund::Savings => {}
        }
        ui.add_space(6.0);
        ui.horizontal_wrapped(|ui| {
            let monthly_text = &mut screen.monthly[i];
            if fund != Fund::Benevolent {
                ui.label("Monthly");
                ui.add(egui::TextEdit::singleline(monthly_text).desired_width(80.0).hint_text("2,500"));
            }
            let monthly = monthly_text.replace(',', "").trim().parse::<f64>().ok().filter(|m| *m > 0.0);
            match fund {
                Fund::Benevolent => {
                    if widgets::solid(ui, "Enrol / Edit…", p.accent).clicked() {
                        actions.push(Action::dialog(fund_dialogs::Enrol::new(member, data.enrolment.as_ref(), last_date)));
                    }
                    let enrolled = data.enrolment.as_ref().is_some_and(|e| e.active && e.monthly_amount > 0.0);
                    if widgets::solid_enabled(ui, enrolled, "Contribute", p.success, "Enrol the member first.").on_hover_text("Take the contribution that is due next").clicked() {
                        actions.push(Action::run(move |ws, actions| {
                            let result = funds::contribute_once(&ws.journal, member);
                            outcome(actions, "No contribution was taken", result, |n| {
                                if n > 0 { "One benevolent contribution taken.".into() } else { "Nothing is due.".into() }
                            });
                        }));
                    }
                    if widgets::solid_enabled(ui, enrolled, "Catch Up…", p.info, "Enrol the member first.").clicked() {
                        actions.push(Action::dialog(CatchUp::benevolent(member)));
                    }
                    if widgets::solid(ui, "Payout…", p.purple).clicked() {
                        actions.push(Action::dialog(fund_dialogs::Movement::payout(member, data.balances[i], last_date)));
                    }
                }
                _ => {
                    if widgets::solid(ui, "Deposit…", p.success).clicked() {
                        actions.push(Action::dialog(fund_dialogs::Movement::deposit(fund, member, monthly, last_date)));
                    }
                    if widgets::solid(ui, "Withdraw…", p.warning).clicked() {
                        actions.push(Action::dialog(fund_dialogs::Movement::withdraw(fund, member, data.balances[i], data.unlock_month, last_date)));
                    }
                    let why = "Type the monthly amount first.";
                    if widgets::solid_enabled(ui, monthly.is_some(), "Catch Up…", p.info, why).on_hover_text("A deposit of the monthly amount for every month up to the one you choose").clicked()
                        && let Some(monthly) = monthly
                    {
                        actions.push(Action::dialog(CatchUp::fund(fund, member, monthly)));
                    }
                    if widgets::solid_enabled(ui, monthly.is_some(), "+ One Month", p.accent, why).on_hover_text("One deposit of the monthly amount, in the month after the last one").clicked()
                        && let Some(monthly) = monthly
                    {
                        actions.push(quick(fund, member, monthly, rows));
                    }
                }
            }
            ui.separator();
            let last = rows.iter().max_by(|a, b| (dates::date_part(&a.date), a.id).cmp(&(dates::date_part(&b.date), b.id)));
            if ui.add_enabled(last.is_some(), egui::Button::new("Delete Last…")).clicked()
                && let Some(row) = last
            {
                actions.push(Action::dialog(delete_row(fund, row, true)));
            }
            if ui.add_enabled(!rows.is_empty(), egui::Button::new(RichText::new("Delete All…").color(p.danger))).clicked() {
                actions.push(Action::dialog(delete_all(fund, member, &data.member.name, rows.len())));
            }
        });
    });
    ui.add_space(10.0);
    if rows.is_empty() {
        ui.add_space(30.0);
        ui.vertical_centered(|ui| ui.label(RichText::new("Nothing recorded yet.").color(p.muted)));
        return;
    }
    egui::Frame::new().fill(p.card).stroke(egui::Stroke::new(1.0, p.border)).corner_radius(10).inner_margin(6).show(ui, |ui| {
        TableBuilder::new(ui)
            .id_salt(("fund_table", i))
            .stick_to_bottom(true)
            .striped(true)
            .sense(egui::Sense::click())
            .cell_layout(egui::Layout::left_to_right(egui::Align::Center))
            .column(Column::exact(100.0))
            .column(Column::exact(120.0))
            .column(Column::exact(120.0))
            .column(Column::exact(130.0))
            .column(Column::remainder().at_least(160.0))
            .header(24.0, |mut header| {
                for (title, right) in [("Date", false), ("Type", false), ("Amount", true), ("Balance", true), ("Notes", false)] {
                    header.col(|ui| {
                        let text = RichText::new(title).strong().color(p.muted);
                        if right { widgets::right(ui, text) } else { ui.label(text); }
                    });
                }
            })
            .body(|body| {
                body.rows(24.0, rows.len(), |mut table_row| {
                    let row = &rows[table_row.index()];
                    let out = row.is_withdrawal();
                    table_row.col(|ui| {
                        ui.label(dates::date_part(&row.date));
                    });
                    table_row.col(|ui| {
                        ui.label(RichText::new(&row.transaction_type).color(if out { p.danger } else { p.text }));
                    });
                    table_row.col(|ui| {
                        let text = if out { format!("−{}", shillings(row.amount)) } else { shillings(row.amount) };
                        widgets::right(ui, RichText::new(text).color(if out { p.danger } else { p.text }));
                    });
                    table_row.col(|ui| widgets::right(ui, shillings(row.balance)));
                    table_row.col(|ui| {
                        ui.label(RichText::new(&row.notes).color(p.muted));
                    });
                    let response = table_row.response();
                    if response.double_clicked() {
                        actions.push(Action::dialog(fund_dialogs::EditRow::new(fund, row)));
                    }
                    response.context_menu(|ui| {
                        if ui.button("Edit…").clicked() {
                            actions.push(Action::dialog(fund_dialogs::EditRow::new(fund, row)));
                        }
                        if ui.button("Delete…").clicked() {
                            actions.push(Action::dialog(delete_row(fund, row, false)));
                        }
                    });
                });
            });
    });
}

/// One deposit of the monthly amount: on the 1st of the month after the
/// last deposit, or today when there is none.
fn quick(fund: Fund, member: i64, monthly: f64, rows: &[FundRow]) -> Action {
    let last = rows.iter().filter(|r| r.transaction_type == fund.credit()).filter_map(|r| dates::parse(dates::date_part(&r.date))).max();
    Action::run(move |ws, actions| {
        let result = match last {
            Some(last) => {
                let month = dates::add_months(last.with_day(1).unwrap_or(last), 1);
                funds::catch_up_pot(&ws.journal, fund, member, Some(monthly), Some(&dates::format(month))).map(|_| month)
            }
            None => {
                let today = chrono::Local::now().date_naive();
                funds::deposit(&ws.journal, fund, member, monthly, &dates::format(today), "Monthly Increment").map(|_| today)
            }
        };
        outcome(actions, "The deposit could not be made", result, |month| {
            format!("Deposited {} for {}.", shillings(monthly), month.format("%B %Y"))
        });
    })
}

fn delete_row(fund: Fund, row: &FundRow, last: bool) -> Confirm {
    let row_id = row.id;
    Confirm::new(
        if last { "Delete Last Entry" } else { "Delete Entry" },
        format!(
            "Delete this {} entry?\n\n{}  ·  {}  ·  {}\n\nThe balances after it are worked out again.",
            fund.label().to_lowercase(),
            dates::date_part(&row.date),
            row.transaction_type,
            shillings(row.amount)
        ),
        "Delete",
        move |ws, actions| {
            let result = funds::delete_row(&ws.journal, fund, row_id);
            outcome(actions, "The entry could not be deleted", result, |()| "Entry deleted.".into());
        },
    )
}

fn delete_all(fund: Fund, member: i64, name: &str, count: usize) -> Confirm {
    let name = name.to_string();
    Confirm::new(
        "Delete All Entries",
        format!(
            "Delete all {count} {} entries for {name}? This cannot be undone in the application; a backup of the journal is saved first.",
            fund.label().to_lowercase()
        ),
        "Delete All",
        move |ws, actions| {
            if let Err(e) = ws.journal.snapshot(&format!("delete-all-{}-{member}", fund.table())) {
                actions.push(crate::app::Action::Notice(crate::app::Notice::from_error("No backup could be saved, so nothing was deleted", &e)));
                return;
            }
            let result = funds::delete_all(&ws.journal, fund, member);
            outcome(actions, "The entries could not be deleted", result, |()| format!("All {} entries for {name} deleted.", fund.label().to_lowercase()));
        },
    )
    .danger()
}
