//! The home screen: the portfolio at a glance, and every member.

use std::collections::HashMap;

use eframe::egui::{self, RichText};
use egui_extras::{Column, TableBuilder};
use loanmaster_core::db::model::Fund;
use loanmaster_core::money::shillings;
use loanmaster_core::{Journal, Result, dates, membership};

use crate::app::{Action, Notice};
use crate::dialogs::confirm::Confirm;
use crate::dialogs::member::MemberForm;
use crate::theme;
use crate::widgets;

const ICON: &[u8] = include_bytes!("../../../../resources/icon.png");

/// One member's line in the list.
#[derive(Debug, Clone)]
pub struct Line {
    pub id: i64,
    pub name: String,
    pub pf_no: String,
    pub phone: String,
    pub status: String,
    pub retired: bool,
    pub loans: usize,
    pub owed: f64,
    pub savings: f64,
    /// Name, phone and PF number, lowercased, for the search box.
    haystack: String,
}

#[derive(Debug, Clone, Default)]
pub struct Totals {
    pub members: usize,
    pub active_loans: usize,
    pub outstanding: f64,
    pub arrears: i64,
    pub savings: f64,
}

#[derive(Debug, Clone, Default)]
pub struct Data {
    pub lines: Vec<Line>,
    pub totals: Totals,
}

pub fn load(journal: &Journal) -> Result<Data> {
    let mut loans: HashMap<i64, (usize, f64)> = HashMap::new();
    let active = journal.loans().all_active()?;
    for loan in &active {
        let entry = loans.entry(loan.individual_id).or_default();
        entry.0 += 1;
        entry.1 += loan.balance;
    }
    let savings = journal.funds().balances(Fund::Savings)?;
    let lines: Vec<Line> = journal
        .members()
        .all()?
        .into_iter()
        .map(|m| {
            let (count, owed) = loans.get(&m.id).copied().unwrap_or_default();
            Line {
                haystack: format!("{} {} {}", m.name, m.phone, m.pf_no).to_lowercase(),
                id: m.id,
                retired: !m.is_active(),
                status: if m.employment_status.is_empty() { "Active".into() } else { m.employment_status.clone() },
                name: m.name,
                pf_no: m.pf_no,
                phone: m.phone,
                loans: count,
                owed,
                savings: savings.get(&m.id).copied().unwrap_or(0.0),
            }
        })
        .collect();
    let totals = Totals {
        members: lines.len(),
        active_loans: active.len(),
        outstanding: loanmaster_core::money::total(active.iter().map(|l| l.balance)),
        arrears: journal.loans().overdue_count(&dates::today())?,
        savings: journal.funds().total(Fund::Savings)?,
    };
    Ok(Data { lines, totals })
}

#[derive(Default)]
pub struct Dashboard {
    pub search: String,
    pub selected: Option<i64>,
    data: Option<(u64, Data)>,
    problem: Option<String>,
}

impl Dashboard {
    fn data(&mut self, journal: &Journal, revision: u64) -> Option<&Data> {
        if self.data.as_ref().is_none_or(|(r, _)| *r != revision) {
            match load(journal) {
                Ok(data) => {
                    self.data = Some((revision, data));
                    self.problem = None;
                }
                Err(e) => {
                    log::error!("Could not read the dashboard: {e}");
                    self.problem = Some(e.to_string());
                }
            }
        }
        self.data.as_ref().map(|(_, d)| d)
    }

    pub fn show(&mut self, ui: &mut egui::Ui, journal: &Journal, revision: u64, org_name: &str, actions: &mut Vec<Action>) {
        let p = theme::palette(ui.ctx());
        egui::Panel::top("dashboard_header")
            .frame(egui::Frame::new().fill(p.header).inner_margin(egui::Margin::symmetric(24, 12)))
            .show(ui, |ui| {
                ui.horizontal(|ui| {
                    ui.add(egui::Image::from_bytes("bytes://icon.png", ICON).fit_to_exact_size(egui::vec2(34.0, 34.0)));
                    ui.vertical(|ui| {
                        ui.label(RichText::new("LoanMaster").size(20.0).strong().color(egui::Color32::WHITE));
                        if !org_name.is_empty() {
                            ui.label(RichText::new(org_name).color(egui::Color32::from_gray(200)));
                        }
                    });
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if widgets::solid(ui, "+ New Member", p.accent).clicked() {
                            actions.push(Action::dialog(MemberForm::add()));
                        }
                        let import = egui::Button::new(RichText::new("Import Data ⏷").strong().color(egui::Color32::WHITE))
                            .fill(egui::Color32::from_white_alpha(24))
                            .stroke(egui::Stroke::NONE)
                            .corner_radius(6)
                            .min_size(egui::vec2(0.0, 28.0));
                        egui::containers::menu::MenuButton::from_button(import).ui(ui, |ui| crate::dialogs::imports::menu(ui, actions));
                    });
                });
            });

        egui::CentralPanel::default().frame(egui::Frame::new().fill(p.background).inner_margin(egui::Margin::symmetric(28, 18))).show(ui, |ui| {
            if self.data(journal, revision).is_none() {
                ui.colored_label(p.danger, format!("The journal could not be read: {}", self.problem.clone().unwrap_or_default()));
                return;
            }
            let Some((_, data)) = &self.data else { return };
            tiles(ui, &data.totals, actions);
            ui.add_space(14.0);
            ui.add(
                egui::TextEdit::singleline(&mut self.search)
                    .hint_text("Search by name, phone or PF number…")
                    .desired_width(f32::INFINITY)
                    .margin(egui::Margin::symmetric(10, 8)),
            );
            let search = self.search.trim();
            let needle = search.to_lowercase();
            let shown: Vec<&Line> = data.lines.iter().filter(|l| needle.is_empty() || l.haystack.contains(&needle)).collect();
            ui.add_space(6.0);
            let heading = if needle.is_empty() {
                format!("{} members", data.lines.len())
            } else {
                format!("{} of {} members match “{search}”", shown.len(), data.lines.len())
            };
            ui.label(RichText::new(heading).strong().color(p.muted));
            ui.add_space(4.0);
            if data.lines.is_empty() {
                ui.add_space(30.0);
                ui.vertical_centered(|ui| ui.label(RichText::new("No members yet. Click “+ New Member” above to add one.").color(p.muted)));
                return;
            }
            if shown.is_empty() {
                ui.add_space(30.0);
                ui.vertical_centered(|ui| ui.label(RichText::new(format!("No members match “{search}”.")).color(p.muted)));
                return;
            }
            if let Some(clicked) = table(ui, journal, &shown, self.selected, actions) {
                self.selected = Some(clicked);
            }
        });
    }
}

fn tiles(ui: &mut egui::Ui, totals: &Totals, actions: &mut Vec<Action>) {
    let p = theme::palette(ui.ctx());
    ui.horizontal_wrapped(|ui| {
        ui.spacing_mut().item_spacing.x = 14.0;
        widgets::stat_tile(ui, p, &format!("{}", totals.members), "Members", p.text, false);
        widgets::stat_tile(ui, p, &format!("{}", totals.active_loans), "Active Loans", p.text, false);
        widgets::stat_tile(ui, p, &shillings(totals.outstanding), "Outstanding", p.text, false);
        let arrears_colour = if totals.arrears > 0 { p.danger } else { p.text };
        if widgets::stat_tile(ui, p, &format!("{}", totals.arrears), "Loans in Arrears", arrears_colour, true)
            .on_hover_text("Who is behind on a loan, and by how much — with a printable list")
            .clicked()
        {
            actions.push(Action::dialog(crate::dialogs::arrears::Arrears::default()));
        }
        widgets::stat_tile(ui, p, &shillings(totals.savings), "Total Savings", p.text, false);
    });
}

/// The member table. Returns the member clicked, if any.
fn table(ui: &mut egui::Ui, journal: &Journal, lines: &[&Line], selected: Option<i64>, actions: &mut Vec<Action>) -> Option<i64> {
    let p = theme::palette(ui.ctx());
    let mut clicked = None;
    egui::Frame::new().fill(p.card).stroke(egui::Stroke::new(1.0, p.border)).corner_radius(10).inner_margin(6).show(ui, |ui| {
        widgets::clickable_rows(ui);
        TableBuilder::new(ui)
            .striped(true)
            .sense(egui::Sense::click())
            .cell_layout(egui::Layout::left_to_right(egui::Align::Center))
            .column(Column::remainder().at_least(220.0))
            .column(Column::exact(90.0))
            .column(Column::exact(130.0))
            .column(Column::exact(120.0))
            .column(Column::exact(70.0))
            .column(Column::exact(130.0))
            .column(Column::exact(130.0))
            .header(26.0, |mut header| {
                for (title, right) in [("Name", false), ("PF No", false), ("Phone", false), ("Status", false), ("Loans", true), ("Owed", true), ("Savings", true)] {
                    header.col(|ui| {
                        let text = RichText::new(title).strong().color(p.muted);
                        if right { widgets::right(ui, text) } else { ui.label(text); }
                    });
                }
            })
            .body(|body| {
                body.rows(30.0, lines.len(), |mut row| {
                    let line = lines[row.index()];
                    row.set_selected(selected == Some(line.id));
                    row.col(|ui| {
                        ui.label(RichText::new(&line.name).strong());
                    });
                    row.col(|ui| {
                        ui.label(&line.pf_no);
                    });
                    row.col(|ui| {
                        ui.label(&line.phone);
                    });
                    row.col(|ui| {
                        if line.retired {
                            widgets::badge(ui, &line.status.to_uppercase(), p.warning);
                        } else {
                            ui.label(RichText::new("Active").color(p.success));
                        }
                    });
                    row.col(|ui| widgets::right(ui, if line.loans > 0 { line.loans.to_string() } else { "—".into() }));
                    row.col(|ui| widgets::right(ui, widgets::money_or_dash(line.owed)));
                    row.col(|ui| widgets::right(ui, widgets::money_or_dash(line.savings)));
                    let response = row.response();
                    if response.clicked() {
                        clicked = Some(line.id);
                    }
                    if response.double_clicked() {
                        actions.push(Action::OpenMember(line.id));
                    }
                    response.context_menu(|ui| member_menu(ui, journal, line, actions));
                });
            });
    });
    clicked
}

/// What can be done to one member, from their row's right-click menu.
fn member_menu(ui: &mut egui::Ui, journal: &Journal, line: &Line, actions: &mut Vec<Action>) {
    if ui.button("Open Ledger").clicked() {
        actions.push(Action::OpenMember(line.id));
    }
    if ui.button("Edit Details…").clicked() {
        match MemberForm::edit(journal, line.id) {
            Ok(form) => actions.push(Action::dialog(form)),
            Err(e) => actions.push(Action::Notice(Notice::from_error("The member could not be read", &e))),
        }
    }
    if line.retired {
        if ui.button("Reinstate…").clicked() {
            actions.push(Action::dialog(reinstate(line)));
        }
    } else if ui.button("Retire…").clicked() {
        actions.push(Action::dialog(retire(journal, line)));
    }
    ui.separator();
    if ui.button(RichText::new("Delete…").color(theme::palette(ui.ctx()).danger)).clicked() {
        actions.push(Action::dialog(delete(journal, line)));
    }
}

fn retire(journal: &Journal, line: &Line) -> Confirm {
    let (id, name) = (line.id, line.name.clone());
    let mut body = format!("Retire {name}?\n");
    if line.savings > 0.0 {
        body.push_str(&format!("\n• Their savings of {} are withdrawn in full.", shillings(line.savings)));
    }
    if membership::has_outstanding_loans(journal, id).unwrap_or(false) {
        body.push_str("\n• Their loans stay active until they are cleared, and still appear in the loan report.");
    }
    body.push_str("\n• They take on nothing new — no loans, deposits or contributions.");
    Confirm::new("Retire Member", body, "Retire", move |ws, actions| match membership::retire(&ws.journal, id, None) {
        Ok(withdrawn) => {
            let mut status = format!("{name} has retired.");
            if withdrawn > 0.0 {
                status.push_str(&format!(" Savings of {} were withdrawn.", shillings(withdrawn)));
            }
            actions.push(Action::Status(status));
            actions.push(Action::Changed);
        }
        Err(e) => actions.push(Action::Notice(Notice::from_error("The member could not be retired", &e))),
    })
}

fn reinstate(line: &Line) -> Confirm {
    let (id, name) = (line.id, line.name.clone());
    Confirm::new(
        "Reinstate Member",
        format!("Reinstate {name}? They become an active member again and appear in every report."),
        "Reinstate",
        move |ws, actions| match membership::reinstate(&ws.journal, id) {
            Ok(()) => {
                actions.push(Action::Status(format!("{name} is an active member again.")));
                actions.push(Action::Changed);
            }
            Err(e) => actions.push(Action::Notice(Notice::from_error("The member could not be reinstated", &e))),
        },
    )
}

fn delete(journal: &Journal, line: &Line) -> Confirm {
    let (id, name) = (line.id, line.name.clone());
    let held: Vec<String> = journal
        .members()
        .record_counts(id)
        .unwrap_or_default()
        .into_iter()
        .filter(|(_, n)| *n > 0)
        .map(|(what, n)| format!("{n} {what}"))
        .collect();
    let mut body = format!("Delete {name} and everything recorded for them?");
    if !held.is_empty() {
        body.push_str(&format!("\n\nThis removes {}.", held.join(", ")));
    }
    body.push_str("\n\nA backup of the journal is taken first. To keep their history, retire them instead.");
    Confirm::new("Delete Member", body, "Delete", move |ws, actions| match membership::delete(&ws.journal, id) {
        Ok(backup) => {
            let mut status = format!("{name} was deleted.");
            if let Some(path) = backup {
                status.push_str(&format!(" Backup: {}", path.display()));
            }
            ws.dashboard.selected = None;
            actions.push(Action::Status(status));
            actions.push(Action::Changed);
        }
        Err(e) => actions.push(Action::Notice(Notice::from_error("The member could not be deleted", &e))),
    })
    .danger()
}
