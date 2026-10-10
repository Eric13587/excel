//! The audit trail: who and what was created, changed or deleted, and for a
//! row that carried money, what it said before.

use eframe::egui::{self, RichText};
use egui_extras::{Column, TableBuilder};
use loanmaster_core::Journal;
use loanmaster_core::db::history::describe_before_image;
use loanmaster_core::db::model::AuditEntry;

use crate::app::{Action, Notice};
use crate::theme;

/// As many entries as 1.x showed.
const LIMIT: i64 = 1000;

/// One line of the log, with its before-image already in words.
struct Line {
    entry: AuditEntry,
    before: String,
    haystack: String,
}

#[derive(Default)]
pub struct ActivityLog {
    lines: Option<Vec<Line>>,
    search: String,
}

impl ActivityLog {
    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        if self.lines.is_none() {
            match journal.audit().recent(LIMIT, None) {
                Ok(entries) => {
                    self.lines = Some(
                        entries
                            .into_iter()
                            .map(|entry| {
                                let before = entry.before_image.as_deref().map(describe_before_image).unwrap_or_default();
                                let haystack = format!("{} {} {} {} {before}", entry.ts, entry.operation, entry.entity, entry.summary).to_lowercase();
                                Line { entry, before, haystack }
                            })
                            .collect(),
                    )
                }
                Err(e) => {
                    actions.push(Action::Notice(Notice::from_error("The activity log could not be read", &e)));
                    return false;
                }
            }
        }
        let p = theme::palette(ctx);
        let mut open = true;
        let mut close = false;
        egui::Window::new("Activity Log").open(&mut open).default_size([980.0, 560.0]).collapsible(false).show(ctx, |ui| {
            let lines = self.lines.as_deref().unwrap_or_default();
            let needle = self.search.trim().to_lowercase();
            let shown: Vec<&Line> = lines.iter().filter(|l| needle.is_empty() || l.haystack.contains(&needle)).collect();
            ui.horizontal(|ui| {
                ui.label(RichText::new(format!("The most recent {} create, update and delete events", lines.len())).strong());
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.add(egui::TextEdit::singleline(&mut self.search).hint_text("Filter…").desired_width(240.0));
                });
            });
            if shown.len() != lines.len() {
                ui.label(RichText::new(format!("Showing {} of {}.", shown.len(), lines.len())).color(p.muted));
            }
            ui.add_space(4.0);
            egui::Panel::bottom("activity_footer").show(ui, |ui| {
                ui.label(RichText::new("“Previous values” is what a changed or deleted row said before; hover it for the full record.").small().color(p.muted));
                super::footer(ui, |ui| {
                    if ui.button("Close").clicked() || crate::widgets::escape(ui) {
                        close = true;
                    }
                });
            });
            egui::CentralPanel::default().show(ui, |ui| {
                TableBuilder::new(ui)
                    .striped(true)
                    .cell_layout(egui::Layout::left_to_right(egui::Align::Center))
                    .column(Column::exact(150.0))
                    .column(Column::exact(80.0))
                    .column(Column::exact(130.0))
                    .column(Column::initial(260.0).at_least(120.0).resizable(true))
                    .column(Column::remainder().at_least(160.0))
                    .header(24.0, |mut header| {
                        for title in ["When", "Operation", "Type", "Who / What", "Previous values"] {
                            header.col(|ui| {
                                ui.label(RichText::new(title).strong().color(p.muted));
                            });
                        }
                    })
                    .body(|body| {
                        body.rows(24.0, shown.len(), |mut row| {
                            let line = shown[row.index()];
                            let entry = &line.entry;
                            row.col(|ui| {
                                ui.label(&entry.ts);
                            });
                            row.col(|ui| {
                                let colour = match entry.operation.as_str() {
                                    "DELETE" => p.danger,
                                    "UPDATE" => p.warning,
                                    _ => p.success,
                                };
                                ui.label(RichText::new(&entry.operation).color(colour));
                            });
                            row.col(|ui| {
                                ui.label(&entry.entity);
                            });
                            row.col(|ui| {
                                ui.add(egui::Label::new(&entry.summary).truncate());
                            });
                            row.col(|ui| {
                                let response = ui.add(egui::Label::new(RichText::new(&line.before).color(p.muted)).truncate());
                                if let Some(raw) = &entry.before_image {
                                    response.on_hover_text(raw);
                                }
                            });
                        });
                    });
            });
        });
        open && !close
    }
}
