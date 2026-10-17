//! The Savings, Christmas and Benevolent tabs: the balance, what can be done
//! to the fund, and every movement — on Savings, with the months its
//! contributions were suspended.

use chrono::{Datelike, NaiveDate};
use eframe::egui::{self, RichText};
use egui_extras::{Column, TableBuilder};
use loanmaster_core::db::model::{Fund, FundRow, SavingsSuspension};
use loanmaster_core::funds::suspensions::{self, Window};
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

/// What a suspension window says on a badge: when it ends.
fn badge_text(window: &Window) -> String {
    match window.1.as_deref().and_then(dates::parse) {
        Some(resume) => format!("⏸ SUSPENDED — RESUMES {}", resume.format("%B %Y").to_string().to_uppercase()),
        None => "⏸ SUSPENDED UNTIL RESUMED".into(),
    }
}

pub fn show(screen: &mut MemberScreen, ui: &mut egui::Ui, fund: Fund, data: &MemberData, last_date: NaiveDate, actions: &mut Vec<Action>) {
    let p = theme::palette(ui.ctx());
    let member = screen.id;
    let i = slot(fund);
    let rows = &data.funds[i];
    // Savings: the suspension in force this month, the next one to come, and
    // every window, for the deposit dialog and "+ One Month".
    let spans: &[SavingsSuspension] = if fund == Fund::Savings { &data.savings_suspensions } else { &[] };
    let paused: Vec<Window> = spans.iter().map(suspensions::window).collect();
    let this_month = dates::format(suspensions::month_start(chrono::Local::now().date_naive()));
    let now = spans.iter().find(|s| suspensions::covering(&this_month, &[suspensions::window(s)]).is_some());
    let upcoming = spans.iter().filter(|s| dates::date_part(&s.start_date) > this_month.as_str()).min_by(|a, b| a.start_date.cmp(&b.start_date));
    egui::Frame::new().fill(p.card).stroke(egui::Stroke::new(1.0, p.border)).corner_radius(10).inner_margin(12).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            ui.label(RichText::new(title(fund)).size(17.0).strong());
            ui.label(RichText::new(format!("Balance {}", shillings(data.balances[i]))).size(17.0).color(p.accent));
            if let Some(span) = now {
                widgets::badge(ui, &badge_text(&suspensions::window(span)), p.warning);
            }
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
            Fund::Savings => {
                let note = match (now, upcoming) {
                    (Some(span), _) => Some(format!("No savings deposit is taken {}.", suspensions::describe(&suspensions::window(span)))),
                    (None, Some(span)) => Some(format!("Contributions will be suspended {}.", suspensions::describe(&suspensions::window(span)))),
                    (None, None) => None,
                };
                if let Some(note) = note {
                    ui.label(RichText::new(note).small().color(p.muted));
                }
            }
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
                        let dialog = if fund == Fund::Savings {
                            fund_dialogs::Movement::savings_deposit(member, monthly, last_date, paused.clone())
                        } else {
                            fund_dialogs::Movement::deposit(fund, member, monthly, last_date)
                        };
                        actions.push(Action::dialog(dialog));
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
                        actions.push(quick(fund, member, monthly, rows, paused.clone()));
                    }
                    if fund == Fund::Savings {
                        match now {
                            Some(span) => {
                                if widgets::solid(ui, "▶ Resume…", p.success).on_hover_text("Take contributions again from this month").clicked() {
                                    actions.push(Action::dialog(resume(member, &data.member.name, span)));
                                }
                            }
                            None => {
                                let why = format!("{} has left the SACCO.", data.member.name);
                                if widgets::solid_enabled(ui, data.member.is_active(), "⏸ Suspend…", p.warning, &why)
                                    .on_hover_text("Pause the monthly contribution for some months")
                                    .clicked()
                                {
                                    let start = first_free_month(rows, &paused);
                                    actions.push(Action::dialog(fund_dialogs::SuspendSavings::new(member, &data.member.name, start)));
                                }
                            }
                        }
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
    if rows.is_empty() && spans.is_empty() {
        ui.add_space(30.0);
        ui.vertical_centered(|ui| ui.label(RichText::new("Nothing recorded yet.").color(p.muted)));
        return;
    }
    let lines = lines(rows, spans);
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
                body.rows(24.0, lines.len(), |mut table_row| {
                    let row = match lines[table_row.index()] {
                        Line::Entry(row) => row,
                        Line::Band(span) => {
                            band(&mut table_row, p, &data.member.name, span, actions);
                            return;
                        }
                    };
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
                        // Worded as the member's statement words it; as written on hover.
                        let shown = loanmaster_core::notes::fund_note(&row.transaction_type, &row.notes);
                        let label = ui.label(RichText::new(&shown).color(p.muted));
                        if shown != row.notes.trim() && !row.notes.trim().is_empty() {
                            label.on_hover_text(format!("Noted as written: {}", row.notes.trim()));
                        }
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

/// A row of a fund's table: one of its entries, or a savings suspension.
enum Line<'a> {
    Entry(&'a FundRow),
    Band(&'a SavingsSuspension),
}

/// The entries in date order, each suspension placed where it begins.
fn lines<'a>(rows: &'a [FundRow], spans: &'a [SavingsSuspension]) -> Vec<Line<'a>> {
    let mut out: Vec<Line<'a>> = rows.iter().map(Line::Entry).collect();
    let mut spans: Vec<&SavingsSuspension> = spans.iter().collect();
    spans.sort_by(|a, b| b.start_date.cmp(&a.start_date));
    for span in spans {
        let start = dates::date_part(&span.start_date);
        let at = out.iter().position(|l| matches!(l, Line::Entry(r) if dates::date_part(&r.date) >= start)).unwrap_or(out.len());
        out.insert(at, Line::Band(span));
    }
    out
}

/// A suspension across the table, with a way to remove it.
fn band(table_row: &mut egui_extras::TableRow<'_, '_>, p: &theme::Palette, name: &str, span: &SavingsSuspension, actions: &mut Vec<Action>) {
    let window = suspensions::window(span);
    for column in 0..5 {
        table_row.col(|ui| {
            widgets::fill_cell(ui, p.warning_bg);
            match column {
                0 => {
                    ui.label(RichText::new(dates::date_part(&span.start_date)).color(p.warning));
                }
                1 => {
                    ui.label(RichText::new("⏸ Suspended").strong().color(p.warning));
                }
                4 => {
                    ui.label(RichText::new(format!("No contributions {}", suspensions::describe(&window))).italics().color(p.muted));
                    if ui.small_button("Remove…").on_hover_text("Remove this suspension: its months become ordinary months again").clicked() {
                        actions.push(Action::dialog(remove(name, span)));
                    }
                }
                _ => {}
            }
        });
    }
}

/// The month a new suspension most likely starts in: this month, or the
/// next if this one already has its deposit — stepped past any suspension.
fn first_free_month(rows: &[FundRow], paused: &[Window]) -> NaiveDate {
    let today = chrono::Local::now().date_naive();
    let this_month = dates::format(today).get(..7).unwrap_or_default().to_string();
    let deposited = rows.iter().any(|r| r.transaction_type == Fund::Savings.credit() && r.date.starts_with(&this_month));
    let from = if deposited { dates::add_months(suspensions::month_start(today), 1) } else { today };
    suspensions::first_unpaused_month(from, paused).unwrap_or(from)
}

fn resume(member: i64, name: &str, span: &SavingsSuspension) -> Confirm {
    let name = name.to_string();
    let month = chrono::Local::now().date_naive().format("%B %Y").to_string();
    Confirm::new(
        "Resume Savings Contributions",
        format!(
            "Resume {name}'s savings contributions from {month}?\n\nThey have been suspended {}. The months before stay \
             without contributions; deposits are taken again from {month}.",
            suspensions::describe(&suspensions::window(span))
        ),
        "Resume",
        move |ws, actions| {
            let result = suspensions::resume(&ws.journal, member, &dates::today());
            outcome(actions, "The contributions could not be resumed", result, |_| format!("{name}'s savings contributions resume from {month}."));
        },
    )
}

fn remove(name: &str, span: &SavingsSuspension) -> Confirm {
    let (id, name) = (span.id, name.to_string());
    Confirm::new(
        "Remove Savings Suspension",
        format!(
            "Remove the suspension of {name}'s contributions {}?\n\nIt is taken as never having applied: its months become \
             ordinary months, which Catch Up and the Ledger Grid's Fill Forward will offer deposits for. Nothing already \
             recorded changes.",
            suspensions::describe(&suspensions::window(span))
        ),
        "Remove",
        move |ws, actions| {
            let result = suspensions::remove(&ws.journal, id);
            outcome(actions, "The suspension could not be removed", result, |_| format!("The suspension of {name}'s contributions was removed."));
        },
    )
}

/// One deposit of the monthly amount: on the 1st of the month after the
/// last deposit — the first such month the member's savings are not
/// suspended in — or today when there is none.
fn quick(fund: Fund, member: i64, monthly: f64, rows: &[FundRow], paused: Vec<Window>) -> Action {
    let last = rows.iter().filter(|r| r.transaction_type == fund.credit()).filter_map(|r| dates::parse(dates::date_part(&r.date))).max();
    Action::run(move |ws, actions| {
        let result = match last {
            Some(last) => {
                let after = dates::add_months(last.with_day(1).unwrap_or(last), 1);
                match suspensions::first_unpaused_month(after, &paused) {
                    Some(month) => funds::catch_up_pot(&ws.journal, fund, member, Some(monthly), Some(&dates::format(month))).map(|_| month),
                    None => Err(loanmaster_core::Error::invalid(format!(
                        "Savings contributions are suspended {}; resume them first.",
                        suspensions::covering(&dates::format(after), &paused).map(suspensions::describe).unwrap_or_default()
                    ))),
                }
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
