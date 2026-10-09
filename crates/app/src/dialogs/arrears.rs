//! Who is behind on a loan, how far, and a printable list of exactly what is
//! on screen.
//!
//! The same population the dashboard tile counts, suspended loans included
//! and marked: whether a deliberate hold belongs in an arrears chase is the
//! user's call, so a filter takes them out rather than this list hiding them.

use eframe::egui::{self, RichText};
use egui_extras::{Column, TableBuilder};
use loanmaster_core::arrears::{ArrearsRow, BANDS, summarize};
use loanmaster_core::money::shillings;
use loanmaster_core::reports::{ARREARS_COLUMNS, arrears_report, arrears_status};
use loanmaster_core::{Journal, dates};

use super::files;
use crate::app::{Action, Notice};
use crate::theme;
use crate::widgets;

/// Days past due beyond which a loan counts towards portfolio at risk.
const AT_RISK_DAYS: i64 = 30;

#[derive(Default)]
pub struct Arrears {
    as_of: String,
    rows: Option<Vec<ArrearsRow>>,
    search: String,
    band: Option<&'static str>,
    hide_suspended: bool,
    /// Column and direction the user sorted by.
    sort: Option<(usize, bool)>,
}

fn cell(row: &ArrearsRow, column: usize) -> String {
    match column {
        0 => row.name.clone(),
        1 => row.pf_no.clone(),
        2 => row.phone.clone(),
        3 => row.reference.clone(),
        4 => row.next_due_date.clone(),
        5 => format!("{}", row.days),
        6 => format!("{}", row.months_missed),
        7 => shillings(row.installment),
        8 => shillings(row.overdue_amount),
        9 => shillings(row.balance),
        10 => row.band.to_string(),
        _ => arrears_status(row),
    }
}

fn compare(a: &ArrearsRow, b: &ArrearsRow, column: usize) -> std::cmp::Ordering {
    let number = |r: &ArrearsRow| match column {
        5 => Some(r.days as f64),
        6 => Some(r.months_missed as f64),
        7 => Some(r.installment),
        8 => Some(r.overdue_amount),
        9 => Some(r.balance),
        _ => None,
    };
    match (number(a), number(b)) {
        (Some(x), Some(y)) => x.total_cmp(&y),
        _ => cell(a, column).to_lowercase().cmp(&cell(b, column).to_lowercase()),
    }
}

impl Arrears {
    /// The rows shown: filtered, then sorted as the user asked.
    fn visible(&self) -> Vec<ArrearsRow> {
        let needle = self.search.trim().to_lowercase();
        let mut rows: Vec<ArrearsRow> = self
            .rows
            .iter()
            .flatten()
            .filter(|r| !(self.hide_suspended && r.suspended))
            .filter(|r| self.band.is_none_or(|b| r.band == b))
            .filter(|r| needle.is_empty() || format!("{} {} {} {}", r.name, r.pf_no, r.reference, r.phone).to_lowercase().contains(&needle))
            .cloned()
            .collect();
        if let Some((column, ascending)) = self.sort {
            rows.sort_by(|a, b| if ascending { compare(a, b, column) } else { compare(b, a, column) });
        }
        rows
    }

    fn export(&self, rows: &[ArrearsRow], journal: &Journal, actions: &mut Vec<Action>) {
        if rows.is_empty() {
            actions.push(Action::Notice(Notice::info("Print / Export", "There is nothing in the list to print.")));
            return;
        }
        let Some(path) = files::save_as("Save Arrears List", &format!("Loans_In_Arrears_{}.pdf", self.as_of), &files::REPORT_FORMATS) else {
            return;
        };
        let written = arrears_report(rows, &self.as_of)
            .map_err(|e| e.to_string())
            .and_then(|table| loanmaster_reports::write_table(&table, &loanmaster_reports::Branding::of(journal), &path).map_err(|e| e.to_string()));
        match written {
            Ok(()) => files::saved(actions, "The arrears list", &path),
            Err(e) => actions.push(Action::Notice(Notice::warning("The list could not be saved", e))),
        }
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        if self.rows.is_none() {
            self.as_of = dates::today();
            match loanmaster_core::arrears::arrears(journal, Some(&self.as_of)) {
                Ok(rows) if rows.is_empty() => {
                    actions.push(Action::Notice(Notice::info(
                        "Loans in Arrears",
                        format!("Nothing is in arrears — every active loan is up to date as at {}.", self.as_of),
                    )));
                    return false;
                }
                Ok(rows) => self.rows = Some(rows),
                Err(e) => {
                    actions.push(Action::Notice(Notice::from_error("Could not work out who is in arrears", &e)));
                    return false;
                }
            }
        }
        let p = theme::palette(ctx);
        let mut open = true;
        let mut close = false;
        egui::Window::new("Loans in Arrears")
            .open(&mut open)
            .default_size([1080.0, 600.0])
            .collapsible(false)
            .show(ctx, |ui| {
                let visible = self.visible();
                let all = self.rows.as_deref().unwrap_or_default();
                let totals = summarize(&visible);
                ui.label(
                    RichText::new(format!(
                        "{} loan(s) · {} member(s) · {} overdue",
                        totals.loans,
                        totals.members,
                        shillings(totals.overdue_amount)
                    ))
                    .strong()
                    .size(15.0),
                );
                let mut sub = format!(
                    "As at {}. Worst is {} day(s) past due; {} of principal sits behind these loans.",
                    self.as_of,
                    totals.worst_days,
                    shillings(totals.balance)
                );
                if totals.suspended > 0 {
                    sub.push_str(&format!(" {} of them are suspended.", totals.suspended));
                }
                if visible.len() != all.len() {
                    sub.push_str(&format!(" Showing {} of {}.", visible.len(), all.len()));
                }
                ui.label(RichText::new(sub).color(p.muted));
                ui.add_space(6.0);
                ui.horizontal(|ui| {
                    ui.add(egui::TextEdit::singleline(&mut self.search).hint_text("Filter by member, PF number or loan…").desired_width(320.0));
                    egui::ComboBox::from_id_salt("arrears_band").selected_text(self.band.unwrap_or("All classifications")).show_ui(ui, |ui| {
                        ui.selectable_value(&mut self.band, None, "All classifications");
                        for (label, _, _) in BANDS {
                            ui.selectable_value(&mut self.band, Some(label), label);
                        }
                    });
                    ui.checkbox(&mut self.hide_suspended, "Hide suspended loans").on_hover_text(
                        "Suspended loans are past due because deductions were deliberately paused, not because a member defaulted.",
                    );
                });
                ui.add_space(6.0);
                egui::Panel::bottom("arrears_footer").show(ui, |ui| {
                    ui.add_space(4.0);
                    ui.label(
                        RichText::new(
                            "Double-click a row to open that member's ledger. “Overdue (est.)” is missed instalments × the \
                             current instalment, capped at the balance — the ledger records what was received, not what was due.",
                        )
                        .small()
                        .color(p.muted),
                    );
                    super::footer(ui, |ui| {
                        if ui.button("Close").clicked() {
                            close = true;
                        }
                        if widgets::solid(ui, "Print / Export…", p.accent).on_hover_text("Save the list shown here as PDF, Excel or CSV").clicked() {
                            self.export(&visible, journal, actions);
                        }
                    });
                });
                egui::CentralPanel::default().show(ui, |ui| {
                    widgets::clickable_rows(ui);
                    TableBuilder::new(ui)
                        .striped(true)
                        .sense(egui::Sense::click())
                        .cell_layout(egui::Layout::left_to_right(egui::Align::Center))
                        .column(Column::remainder().at_least(180.0))
                        .columns(Column::auto().at_least(60.0), ARREARS_COLUMNS.len() - 1)
                        .header(26.0, |mut header| {
                            for (i, (_, title)) in ARREARS_COLUMNS.iter().enumerate() {
                                header.col(|ui| {
                                    let arrow = match self.sort {
                                        Some((c, true)) if c == i => " ⏶",
                                        Some((c, false)) if c == i => " ⏷",
                                        _ => "",
                                    };
                                    let label = egui::Label::new(RichText::new(format!("{title}{arrow}")).strong()).sense(egui::Sense::click());
                                    let response = if (5..=9).contains(&i) {
                                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| ui.add(label)).inner
                                    } else {
                                        ui.add(label)
                                    };
                                    if response.on_hover_text("Sort by this column").clicked() {
                                        self.sort = match self.sort {
                                            Some((c, true)) if c == i => Some((i, false)),
                                            _ => Some((i, true)),
                                        };
                                    }
                                });
                            }
                        })
                        .body(|body| {
                            body.rows(26.0, visible.len(), |mut row| {
                                let r = &visible[row.index()];
                                let colour = if r.suspended {
                                    p.warning
                                } else if r.days > AT_RISK_DAYS {
                                    p.danger
                                } else {
                                    p.text
                                };
                                for column in 0..ARREARS_COLUMNS.len() {
                                    row.col(|ui| {
                                        let text = RichText::new(cell(r, column)).color(colour);
                                        if (5..=9).contains(&column) { widgets::right(ui, text) } else { ui.label(text); }
                                    });
                                }
                                if row.response().double_clicked() {
                                    actions.push(Action::OpenMember(r.member_id));
                                    close = true;
                                }
                            });
                        });
                });
            });
        open && !close
    }
}
