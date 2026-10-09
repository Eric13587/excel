//! The three screens of an open journal: the dashboard, a member's ledger,
//! and the Ledger Grid.

use eframe::egui;

use crate::app::Action;
use crate::workspace::{Screen, Workspace};

pub mod dashboard;
pub mod grid;
pub mod member;
pub mod startup;

pub fn show(ui: &mut egui::Ui, ws: &mut Workspace, actions: &mut Vec<Action>) {
    let Workspace { screen, journal, dashboard, revision, org_name, last_date, .. } = ws;
    match screen {
        Screen::Dashboard => dashboard.show(ui, journal, *revision, org_name, actions),
        Screen::Member(member) => member.show(ui, journal, *revision, *last_date, actions),
        Screen::Grid(grid) => grid.show(ui, journal, *revision, actions),
    }
}
