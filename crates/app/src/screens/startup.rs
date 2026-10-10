//! Choosing a journal: open one, create one, or pick a recent one.

use std::path::PathBuf;

use eframe::egui::{self, RichText};

use crate::app::{Action, Remembered};
use crate::theme;


fn pick_journal() -> Option<PathBuf> {
    rfd::FileDialog::new().set_title("Open Journal").add_filter("LoanMaster journal", &["db"]).pick_file()
}

fn new_journal() -> Option<PathBuf> {
    let path = rfd::FileDialog::new()
        .set_title("Create New Journal")
        .add_filter("LoanMaster journal", &["db"])
        .set_file_name("my_journal.db")
        .save_file()?;
    Some(if path.extension().is_some_and(|e| e.eq_ignore_ascii_case("db")) { path } else { path.with_extension("db") })
}

pub fn show(ui: &mut egui::Ui, remembered: &Remembered, actions: &mut Vec<Action>) {
    let p = theme::palette(ui.ctx());
    ui.vertical_centered(|ui| {
        ui.add_space((ui.available_height() / 2.0 - 230.0).max(24.0));
        egui::Frame::new()
            .fill(p.card)
            .stroke(egui::Stroke::new(1.0, p.border))
            .corner_radius(egui::CornerRadius::same(14))
            .inner_margin(egui::Margin::same(32))
            .show(ui, |ui| {
                ui.set_width(420.0);
                ui.vertical_centered(|ui| {
                    ui.add(egui::Image::from_bytes("bytes://icon.png", crate::ICON).fit_to_exact_size(egui::vec2(64.0, 64.0)));
                    ui.add_space(6.0);
                    ui.label(RichText::new("LoanMaster").size(26.0).strong().color(p.text));
                    ui.label(
                        RichText::new(format!("Loan, savings & fund management — v{}", env!("CARGO_PKG_VERSION")))
                            .small()
                            .color(p.muted),
                    );
                    ui.add_space(18.0);
                    let wide = egui::vec2(300.0, 40.0);
                    let open = egui::Button::new(RichText::new("Open Existing Journal").strong().color(egui::Color32::WHITE))
                        .fill(p.accent)
                        .stroke(egui::Stroke::NONE)
                        .min_size(wide);
                    if ui.add(open).clicked()
                        && let Some(path) = pick_journal()
                    {
                        actions.push(Action::OpenJournal(path));
                    }
                    ui.add_space(6.0);
                    if ui.add(egui::Button::new("Create New Journal").min_size(wide)).clicked()
                        && let Some(path) = new_journal()
                    {
                        actions.push(Action::OpenJournal(path));
                    }
                    let recent: Vec<&PathBuf> = remembered.recent.iter().filter(|p| p.exists()).collect();
                    if !recent.is_empty() {
                        ui.add_space(18.0);
                        ui.label(RichText::new("Recent journals").small().color(p.muted));
                        for path in recent {
                            let name = path.file_name().map(|f| f.to_string_lossy().to_string()).unwrap_or_default();
                            if ui.link(name).on_hover_text(path.display().to_string()).clicked() {
                                actions.push(Action::OpenJournal(path.clone()));
                            }
                        }
                    }
                });
            });
    });
}
