//! The Ledger Grid: members × months for each fund, and loans × months.

use eframe::egui;
use loanmaster_core::Journal;

use crate::app::Action;

pub struct GridScreen;

impl GridScreen {
    pub fn new() -> GridScreen {
        GridScreen
    }

    pub fn show(&mut self, ui: &mut egui::Ui, _journal: &Journal, _revision: u64, actions: &mut Vec<Action>) {
        egui::CentralPanel::default().show(ui, |ui| {
            if ui.button("⬅ Dashboard").clicked() {
                actions.push(Action::ShowDashboard);
            }
        });
    }
}
