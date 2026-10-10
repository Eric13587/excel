//! The Loans tab: issuing a loan, and each loan's history and actions.

use chrono::NaiveDate;
use eframe::egui::{self, Color32, RichText};
use egui_extras::{Column, TableBuilder};
use loanmaster_core::db::model::{LOAN_ACTIVE, LedgerRow, Suspension, event};
use loanmaster_core::loans::service::{self, terms_for};
use loanmaster_core::loans::suspensions;
use loanmaster_core::money::shillings;
use loanmaster_core::{Journal, dates};

use super::{LoanView, MemberData, MemberScreen};
use crate::app::{Action, Notice, outcome};
use crate::dialogs::confirm::Confirm;
use crate::dialogs::loan as loan_dialogs;
use crate::theme::{self, Palette};
use crate::widgets;
use crate::workspace::Screen;

/// The new-loan form, as typed.
#[derive(Debug, Clone, Default)]
pub struct NewLoan {
    pub principal: String,
    pub months: String,
    /// Percent; blank means the journal's default rate.
    pub rate: String,
    pub date: Option<NaiveDate>,
}

fn number(text: &str) -> Option<f64> {
    text.replace(',', "").trim().parse::<f64>().ok().filter(|v| v.is_finite())
}

pub fn show(screen: &mut MemberScreen, ui: &mut egui::Ui, journal: &Journal, data: &MemberData, last_date: NaiveDate, actions: &mut Vec<Action>) {
    let p = theme::palette(ui.ctx());
    egui::Panel::left("loans_side").resizable(false).exact_size(280.0).frame(egui::Frame::new().fill(p.background)).show(ui, |ui| {
        new_loan(screen, ui, data, last_date, actions);
        ui.add_space(12.0);
        if !data.loans.is_empty() {
            card(ui, p, |ui| {
                ui.label(RichText::new("Loans").strong());
                for view in &data.loans {
                    let loan = &view.loan;
                    let state = if loan.status != LOAN_ACTIVE {
                        loan.status.clone()
                    } else if loan.is_suspended {
                        "Suspended".into()
                    } else if view.overdue {
                        "Overdue".into()
                    } else {
                        "Active".into()
                    };
                    let owed = view.rows.last().map(|r| r.balance).unwrap_or(loan.balance);
                    let line = format!("{}  ·  {state}  ·  {}", loan.reference, shillings(owed));
                    if ui.selectable_label(false, line).clicked() {
                        screen.scroll_to = Some(loan.reference.clone());
                    }
                }
            });
        }
    });
    egui::CentralPanel::default().frame(egui::Frame::new().fill(p.background).inner_margin(egui::Margin { left: 10, ..Default::default() })).show(ui, |ui| {
        if data.loans.is_empty() {
            ui.add_space(40.0);
            ui.vertical_centered(|ui| ui.label(RichText::new("No loans yet. Issue one with the form on the left.").color(p.muted)));
            return;
        }
        egui::ScrollArea::vertical().auto_shrink(false).show(ui, |ui| {
            for view in &data.loans {
                section(screen, ui, journal, data, view, last_date, actions);
                ui.add_space(12.0);
            }
        });
    });
}

fn card<R>(ui: &mut egui::Ui, p: &Palette, add: impl FnOnce(&mut egui::Ui) -> R) -> R {
    egui::Frame::new()
        .fill(p.card)
        .stroke(egui::Stroke::new(1.0, p.border))
        .corner_radius(10)
        .inner_margin(egui::Margin::same(12))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            add(ui)
        })
        .inner
}

fn new_loan(screen: &mut MemberScreen, ui: &mut egui::Ui, data: &MemberData, last_date: NaiveDate, actions: &mut Vec<Action>) {
    let p = theme::palette(ui.ctx());
    let form = &mut screen.new_loan;
    let date = form.date.get_or_insert(last_date);
    card(ui, p, |ui| {
        ui.label(RichText::new("New Loan").strong());
        ui.add_space(4.0);
        egui::Grid::new("new_loan").num_columns(2).spacing([10.0, 8.0]).show(ui, |ui| {
            ui.label("Principal");
            ui.horizontal(|ui| {
                ui.add(egui::TextEdit::singleline(&mut form.principal).hint_text("100,000").desired_width(120.0));
                if ui.small_button("?").on_hover_text("Work out the principal from a monthly deduction").clicked() {
                    actions.push(Action::dialog(loan_dialogs::PrincipalCalculator::new(data.default_rate)));
                }
            });
            ui.end_row();
            ui.label("Months");
            ui.add(egui::TextEdit::singleline(&mut form.months).hint_text("12").desired_width(120.0));
            ui.end_row();
            ui.label("Rate (%)");
            ui.add(egui::TextEdit::singleline(&mut form.rate).hint_text(format!("{}", data.default_rate * 100.0)).desired_width(120.0));
            ui.end_row();
            ui.label("Date");
            widgets::date(ui, "new_loan_date", date);
            ui.end_row();
        });
        let principal = number(&form.principal).filter(|v| *v > 0.0);
        let months = form.months.trim().parse::<i64>().ok().filter(|m| *m >= 1);
        let rate = if form.rate.trim().is_empty() { Some(data.default_rate) } else { number(&form.rate).filter(|r| *r >= 0.0).map(|r| r / 100.0) };
        ui.add_space(6.0);
        match (principal, months, rate) {
            (Some(principal), Some(months), Some(rate)) => {
                let terms = terms_for(principal, months, rate);
                ui.label(
                    RichText::new(format!(
                        "{} a month; {} interest; {} to repay in all.",
                        shillings(terms.installment),
                        shillings(terms.interest_total),
                        shillings(terms.total_repayment)
                    ))
                    .small()
                    .color(p.muted),
                );
                ui.add_space(4.0);
                if widgets::solid(ui, "Issue Loan", p.success).clicked() {
                    let when = *date;
                    let (member, date) = (screen.id, dates::format(when));
                    actions.push(Action::run(move |ws, actions| {
                        let result = service::issue(&ws.journal, member, principal, months, &date, Some(rate));
                        if let Ok(issued) = &result {
                            ws.last_date = when;
                            if let Screen::Member(m) = &mut ws.screen {
                                m.new_loan = NewLoan::default();
                                m.scroll_to = Some(issued.reference.clone());
                            }
                        }
                        outcome(actions, "The loan could not be issued", result, |issued| {
                            format!("Issued {}: {} a month.", issued.reference, shillings(issued.installment))
                        });
                    }));
                }
            }
            _ => {
                let hint = if form.principal.trim().is_empty() || form.months.trim().is_empty() {
                    "Enter the principal and the number of months."
                } else {
                    "The principal, months and rate must be numbers above zero."
                };
                ui.label(RichText::new(hint).small().color(p.muted));
            }
        }
    });
}

/// What a row of a loan's table is: one of its entries, or a suspension.
enum Line<'a> {
    Entry(usize, &'a LedgerRow),
    Band(&'a Suspension),
}

fn months_between(start: &str, end: Option<&str>) -> Option<i32> {
    let (a, b) = (dates::parse(start)?, dates::parse(end?)?);
    use chrono::Datelike;
    Some(((b.year() - a.year()) * 12 + b.month() as i32 - a.month() as i32).max(0))
}

fn lines(view: &LoanView) -> Vec<Line<'_>> {
    let mut out: Vec<Line<'_>> = view.rows.iter().enumerate().map(|(i, r)| Line::Entry(i, r)).collect();
    let mut spans: Vec<&Suspension> = view.spans.iter().collect();
    spans.sort_by(|a, b| b.start_date.cmp(&a.start_date));
    for span in spans {
        let start = dates::date_part(&span.start_date);
        let at = out
            .iter()
            .position(|l| matches!(l, Line::Entry(_, r) if dates::date_part(&r.date) >= start))
            .unwrap_or(out.len());
        out.insert(at, Line::Band(span));
    }
    out
}

#[allow(clippy::too_many_arguments)]
fn section(screen: &mut MemberScreen, ui: &mut egui::Ui, journal: &Journal, data: &MemberData, view: &LoanView, last_date: NaiveDate, actions: &mut Vec<Action>) {
    let p = theme::palette(ui.ctx());
    let loan = &view.loan;
    let reference = loan.reference.clone();
    let response = ui.scope(|ui| card(ui, p, |ui| {
        ui.horizontal(|ui| {
            ui.label(RichText::new(&reference).size(17.0).strong());
            if loan.status != LOAN_ACTIVE {
                widgets::badge(ui, &loan.status.to_uppercase(), p.success);
            } else if loan.is_suspended {
                let until = loan.suspend_until.as_deref().filter(|u| !u.is_empty()).map(|u| format!(" UNTIL {u}")).unwrap_or_else(|| " — UNTIL RESUMED".into());
                widgets::badge(ui, &format!("⏸ SUSPENDED{until}"), p.warning);
            } else if view.overdue {
                widgets::badge(ui, &format!("OVERDUE SINCE {}", loan.due()), p.danger);
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let term = loan.duration_months.map(|m| format!(" over {m} months")).unwrap_or_default();
                let mut facts = vec![
                    format!("Balance {}", shillings(view.rows.last().map(|r| r.balance).unwrap_or(loan.balance))),
                    format!("Instalment {}", shillings(loan.installment)),
                ];
                if loan.status == LOAN_ACTIVE {
                    facts.push(format!("Next due {}", loan.due()));
                }
                facts.push(format!("{}%{term}", view.rate * 100.0));
                ui.label(RichText::new(facts.join("   ·   ")).color(p.muted));
            });
        });
        ui.add_space(6.0);
        actions_row(screen, ui, journal, data, view, last_date, actions);
        ui.add_space(6.0);
        table(ui, p, data.member.id, view, actions);
    }))
    .response;
    if screen.scroll_to.as_deref() == Some(reference.as_str()) {
        ui.scroll_to_rect(response.rect, Some(egui::Align::TOP));
        screen.scroll_to = None;
    }
}

#[allow(clippy::too_many_arguments)]
fn actions_row(screen: &mut MemberScreen, ui: &mut egui::Ui, journal: &Journal, data: &MemberData, view: &LoanView, last_date: NaiveDate, actions: &mut Vec<Action>) {
    let p = theme::palette(ui.ctx());
    let loan = &view.loan;
    let member = data.member.id;
    let reference = loan.reference.clone();
    let active = loan.status == LOAN_ACTIVE;
    let suspended = loan.is_suspended;
    let paused = if !active { format!("{reference} is {}.", loan.status.to_lowercase()) } else { "The loan is suspended — resume it first.".into() };
    ui.horizontal_wrapped(|ui| {
        let times = screen.deduct_times.entry(reference.clone()).or_insert(1);
        let can_deduct = active && !suspended;
        let label = if *times == 1 { "Deduct".to_string() } else { format!("Deduct ×{times}") };
        if widgets::solid_enabled(ui, can_deduct, &label, p.accent, &paused).on_hover_text("Take the next month's deduction (or several)").clicked() {
            let (count, reference) = (*times, reference.clone());
            actions.push(Action::run(move |ws, actions| {
                let mut taken = 0;
                let mut failure = None;
                for _ in 0..count {
                    match service::deduct(&ws.journal, member, &reference) {
                        Ok(()) => taken += 1,
                        Err(e) => {
                            failure = Some(e);
                            break;
                        }
                    }
                }
                if taken > 0 {
                    actions.push(Action::Status(format!("{taken} deduction(s) taken on {reference}.")));
                    actions.push(Action::Changed);
                }
                if let Some(e) = failure {
                    actions.push(Action::Notice(Notice::from_error(&format!("Deduction {} could not be taken", taken + 1), &e)));
                }
            }));
        }
        ui.add_enabled(can_deduct, egui::DragValue::new(times).range(1..=12).prefix("× ")).on_hover_text("How many months the Deduct button takes");
        if widgets::solid_enabled(ui, can_deduct, "Catch Up…", p.info, &paused).clicked() {
            actions.push(Action::dialog(loan_dialogs::CatchUp::loan(member, &reference, loan.due())));
        }
        if widgets::solid_enabled(ui, active, "Top-Up…", p.success, &paused).clicked() {
            let after = view.rows.last().and_then(|r| dates::parse(dates::date_part(&r.date))).map(|d| dates::add_months(d, 1));
            actions.push(Action::dialog(loan_dialogs::TopUp::new(member, &reference, after.unwrap_or(last_date), loan.duration_months)));
        }
        if widgets::solid_enabled(ui, active, "Buy Off…", p.purple, &paused).clicked() {
            actions.push(Action::dialog(buyoff(member, view)));
        }
        if suspended {
            if widgets::solid(ui, "▶ Resume…", p.success).clicked() {
                actions.push(Action::dialog(resume(journal, view)));
            }
        } else if widgets::solid_enabled(ui, active, "⏸ Suspend…", p.warning, &paused).clicked() {
            let start = dates::parse(loan.due()).unwrap_or(last_date);
            actions.push(Action::dialog(loan_dialogs::Suspend::new(loan.id, member, &reference, start)));
        }
        if !view.spans.is_empty() && ui.button(format!("Suspensions ({})…", view.spans.len())).on_hover_text("View or remove recorded suspension periods").clicked() {
            actions.push(Action::dialog(loan_dialogs::Suspensions::new(loan.id, member, &reference)));
        }
        ui.menu_button("More ⏷", |ui| {
            if ui.button("Loan Details").clicked() {
                actions.push(Action::Notice(details(view)));
            }
            let last = view.rows.last();
            if ui.add_enabled(last.is_some(), egui::Button::new("Delete Last Entry…")).clicked()
                && let Some(row) = last
            {
                actions.push(Action::dialog(delete_entry(member, &reference, row, true)));
            }
            ui.separator();
            if ui.button(RichText::new("Delete Loan…").color(p.danger)).clicked() {
                actions.push(Action::dialog(delete_loan(member, view)));
            }
        });
    });
}

fn details(view: &LoanView) -> Notice {
    let loan = &view.loan;
    let owed = loan.balance + loan.interest_balance + loan.unearned_interest;
    let remaining = if loan.installment > 0.0 { (owed / loan.installment).ceil() as i64 } else { 0 };
    let mut body = format!(
        "Principal lent: {}\nPrincipal outstanding: {}\nInterest charged, unpaid: {}\nInterest still to charge: {}\n\
         Instalment: {} a month (interest {} a month)\nRate: {}%{}\nEstimated months left: {remaining}\nNext due: {}\nStatus: {}",
        shillings(loan.principal),
        shillings(loan.balance),
        shillings(loan.interest_balance),
        shillings(loan.unearned_interest),
        shillings(loan.installment),
        shillings(loan.monthly_interest),
        view.rate * 100.0,
        loan.duration_months.map(|m| format!(" over {m} months")).unwrap_or_default(),
        if loan.status == LOAN_ACTIVE { loan.due() } else { "—" },
        loan.status
    );
    if loan.interest_rate.is_none() {
        body.push_str("\n\nThis loan records no rate of its own; the journal's default rate is used for it.");
    }
    Notice::info(format!("Loan {}", loan.reference), body)
}

fn buyoff(member: i64, view: &LoanView) -> Confirm {
    let loan = &view.loan;
    let reference = loan.reference.clone();
    let total = loan.balance + loan.interest_balance + loan.unearned_interest;
    Confirm::new(
        "Buy Off Loan",
        format!(
            "Settle {reference} in full on {}?\n\nPrincipal: {}\nInterest charged: {}\nInterest still to charge: {}\n\nTotal to settle: {}\n\n\
             The loan is closed, inclusive of all the interest it was written for.",
            loan.due(),
            shillings(loan.balance),
            shillings(loan.interest_balance),
            shillings(loan.unearned_interest),
            shillings(total)
        ),
        "Buy Off",
        move |ws, actions| {
            let result = service::buyoff(&ws.journal, member, &reference, None);
            outcome(actions, "The loan could not be bought off", result, |paid| format!("{reference} settled for {}.", shillings(paid)));
        },
    )
}

fn resume(journal: &Journal, view: &LoanView) -> Confirm {
    let (loan_id, reference) = (view.loan.id, view.loan.reference.clone());
    let since = journal.loans().open_suspension(loan_id).ok().flatten().map(|s| format!(" It has been suspended since {}.", s.start_date)).unwrap_or_default();
    Confirm::new(
        "Resume Loan",
        format!(
            "Resume deductions on {reference}?{since}\n\nThe months it skipped stay skipped and the term stays extended; only the \
             months from now on are deducted."
        ),
        "Resume",
        move |ws, actions| {
            let result = suspensions::resume(&ws.journal, loan_id, None);
            outcome(actions, "The loan could not be resumed", result, |()| format!("{reference} resumed."));
        },
    )
}

pub fn delete_entry(member: i64, reference: &str, row: &LedgerRow, last: bool) -> Confirm {
    let (row_id, reference) = (row.id, reference.to_string());
    let amount = if row.deducted > 0.0 { row.deducted } else { row.added };
    let what = if last { "the last entry" } else { "this entry" };
    let effect = match row.event_type.as_str() {
        event::REPAYMENT => "If it is the loan's last entry the month is un-run: its interest returns to the loan and the month falls due again. Otherwise it is recorded as missed and later deductions are re-split.",
        event::TOP_UP | event::RESTRUCTURE => "Every later entry on the loan goes with it, and the loan returns to how it stood before.",
        event::LOAN_ISSUED => "Deleting the issue deletes the whole loan.",
        event::BUYOFF => "The loan returns to how it stood before it was settled.",
        _ => "The loan is re-derived without it.",
    };
    Confirm::new(
        "Delete Entry",
        format!(
            "Delete {what} on {reference}?\n\n{}  ·  {}  ·  {}\n\n{effect}\n\nEdit › Undo (Ctrl+Alt+Z) puts it back.",
            row.event_type,
            dates::date_part(&row.date),
            shillings(amount)
        ),
        "Delete",
        move |ws, actions| {
            let result = ws.undo.delete_entry(&ws.journal, member, row_id);
            outcome(actions, "The entry could not be deleted", result, |_| format!("Deleted an entry on {reference}. Ctrl+Alt+Z undoes it."));
        },
    )
}

fn delete_loan(member: i64, view: &LoanView) -> Confirm {
    let reference = view.loan.reference.clone();
    let owed = view.rows.last().map(|r| r.balance).unwrap_or(view.loan.balance);
    let mut body = format!("Permanently delete {reference} and all {} of its entries?", view.rows.len());
    if owed.abs() > 0.005 {
        body.push_str(&format!("\n\nThis loan still shows {} outstanding.", shillings(owed)));
    }
    body.push_str("\n\nThe member's other loans and funds are not affected. A backup is saved first; this cannot be undone in the application.");
    Confirm::new("Delete Loan", body, "Delete Loan", move |ws, actions| {
        let backup = match ws.journal.snapshot(&format!("delete-loan-{reference}")) {
            Ok(backup) => backup,
            Err(e) => {
                actions.push(Action::Notice(Notice::from_error("No backup could be saved, so nothing was deleted", &e)));
                return;
            }
        };
        let result = service::delete_loan(&ws.journal, member, &reference);
        outcome(actions, "The loan could not be deleted", result, |()| match &backup {
            Some(path) => format!("{reference} deleted. Backup: {}", path.display()),
            None => format!("{reference} deleted."),
        });
    })
    .danger()
}

fn signed(value: f64) -> String {
    if value.abs() < 0.005 {
        "—".into()
    } else if value > 0.0 {
        format!("+{}", shillings(value))
    } else {
        format!("−{}", shillings(-value))
    }
}

fn table(ui: &mut egui::Ui, p: &Palette, member: i64, view: &LoanView, actions: &mut Vec<Action>) {
    let lines = lines(view);
    let reference = view.loan.reference.clone();
    TableBuilder::new(ui)
        .id_salt(("loan_table", &reference))
        .vscroll(false)
        .striped(true)
        .sense(egui::Sense::click())
        .cell_layout(egui::Layout::left_to_right(egui::Align::Center))
        .column(Column::exact(88.0))
        .column(Column::exact(118.0))
        .column(Column::exact(96.0))
        .column(Column::exact(86.0))
        .column(Column::exact(92.0))
        .column(Column::exact(104.0))
        .column(Column::exact(104.0))
        .column(Column::remainder().at_least(140.0))
        .header(24.0, |mut header| {
            for (title, right) in [
                ("Date", false),
                ("Event", false),
                ("Principal Δ", true),
                ("Interest Δ", true),
                ("Payment", true),
                ("Principal", true),
                ("Gross", true),
                ("Notes", false),
            ] {
                header.col(|ui| {
                    let text = RichText::new(title).strong().color(p.muted);
                    if right { widgets::right(ui, text) } else { ui.label(text); }
                });
            }
        })
        .body(|body| {
            body.rows(24.0, lines.len(), |mut row| match &lines[row.index()] {
                Line::Entry(i, entry) => {
                    let (fill, text): (Option<Color32>, Color32) = match entry.event_type.as_str() {
                        event::LOAN_ISSUED | event::TOP_UP => (Some(p.accent), Color32::WHITE),
                        event::INTEREST => (Some(p.warning_bg), p.text),
                        _ => (None, p.text),
                    };
                    let (principal, interest, payment) = match entry.event_type.as_str() {
                        event::LOAN_ISSUED | event::TOP_UP => (entry.added, 0.0, 0.0),
                        event::INTEREST => (0.0, entry.added, 0.0),
                        event::REPAYMENT | event::BUYOFF => (-entry.principal_portion, -entry.interest_portion, entry.deducted),
                        _ => (entry.added, 0.0, entry.deducted),
                    };
                    let cells = [
                        dates::date_part(&entry.date).to_string(),
                        entry.event_type.clone(),
                        signed(principal),
                        signed(interest),
                        widgets::money_or_dash(payment),
                        shillings(entry.principal_balance),
                        shillings(view.gross[*i]),
                        loanmaster_core::notes::loan_note(&entry.notes),
                    ];
                    for (column, value) in cells.into_iter().enumerate() {
                        row.col(|ui| {
                            if let Some(fill) = fill {
                                widgets::fill_cell(ui, fill);
                            }
                            let rich = RichText::new(value).color(text);
                            if (2..=6).contains(&column) { widgets::right(ui, rich) } else { ui.label(rich); }
                        });
                    }
                    let response = row.response();
                    let mut tip = format!("Entry #{}", entry.id);
                    if entry.is_edited != 0.0 {
                        tip.push_str(" · edited by hand");
                    }
                    // The note as it was written, where the screen words it for the member.
                    if !entry.notes.trim().is_empty() && loanmaster_core::notes::loan_note(&entry.notes) != entry.notes.trim() {
                        tip.push_str(&format!("\nNoted as written: {}", entry.notes.trim()));
                    }
                    let response = response.on_hover_text(tip);
                    response.context_menu(|ui| entry_menu(ui, member, &reference, entry, actions));
                }
                Line::Band(span) => {
                    let end = span.end();
                    let months = months_between(&span.start_date, end);
                    let mut note = match end {
                        Some(end) => format!("to {}", dates::date_part(end)),
                        None => "no end date".into(),
                    };
                    if let Some(m) = months {
                        note.push_str(&format!(" · {m} month(s) skipped"));
                    }
                    if span.status == "active" {
                        note.push_str(" · currently suspended");
                    }
                    for column in 0..8 {
                        row.col(|ui| {
                            widgets::fill_cell(ui, p.warning_bg);
                            match column {
                                0 => {
                                    ui.label(RichText::new(dates::date_part(&span.start_date)).color(p.warning));
                                }
                                1 => {
                                    ui.label(RichText::new("⏸ Suspended").strong().color(p.warning));
                                }
                                7 => {
                                    ui.label(RichText::new(&note).italics().color(p.muted));
                                    if ui.small_button("Clear…").on_hover_text("Remove this suspension; you choose what happens to the months it skipped").clicked() {
                                        actions.push(Action::dialog(loan_dialogs::ClearSuspension::new(span.id, member, &reference, span)));
                                    }
                                }
                                _ => {}
                            }
                        });
                    }
                }
            });
        });
}

fn entry_menu(ui: &mut egui::Ui, member: i64, reference: &str, entry: &LedgerRow, actions: &mut Vec<Action>) {
    if ui.button("Change Date…").clicked() {
        actions.push(Action::dialog(loan_dialogs::EntryDate::new(member, entry)));
    }
    let amount = match entry.event_type.as_str() {
        event::REPAYMENT => Some("Change Repayment…"),
        event::TOP_UP => Some("Edit Top-Up…"),
        event::INTEREST => Some("Change Interest Charge…"),
        _ => None,
    };
    if let Some(label) = amount
        && ui.button(label).clicked()
    {
        actions.push(Action::dialog(loan_dialogs::EntryAmount::new(member, entry)));
    }
    if ui.button("Edit Notes…").clicked() {
        actions.push(Action::dialog(loan_dialogs::EntryNotes::new(member, entry)));
    }
    ui.separator();
    if ui.button("Delete Entry…").clicked() {
        actions.push(Action::dialog(delete_entry(member, reference, entry, false)));
    }
}
