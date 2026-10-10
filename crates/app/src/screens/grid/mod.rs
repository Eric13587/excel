//! The Ledger Grid: every member's month-by-month figures for one fund — or
//! every loan's deductions — edited like a spreadsheet and saved as a batch.
//!
//! It is where the old mass deduct and mass increment went: Fill Forward
//! proposes each row's usual figure for the months that lack one, and the
//! proposals are reviewed and saved like any other edit.

use eframe::egui::{self, RichText};
use loanmaster_core::Journal;
use loanmaster_core::db::model::Fund;

use crate::app::Action;
use crate::theme;

mod help;
mod page;
pub mod sheet;

use page::Page;
use sheet::{FundSheet, LoanSheet, Sheet};

/// The tabs, in the order offered. Loans come last: they are the constrained
/// one — most of what a loan cell accepts is decided by the loan's schedule,
/// not by the typist.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tab {
    Savings,
    Christmas,
    Benevolent,
    Loans,
}

impl Tab {
    fn help(self) -> help::Kind {
        match self {
            Tab::Savings => help::Kind::Savings,
            Tab::Christmas => help::Kind::Christmas,
            Tab::Benevolent => help::Kind::Benevolent,
            Tab::Loans => help::Kind::Loans,
        }
    }
}

pub struct GridScreen {
    pub tab: Tab,
    savings: Page<FundSheet>,
    christmas: Page<FundSheet>,
    benevolent: Page<FundSheet>,
    loans: Page<LoanSheet>,
    help: bool,
    /// Where the user asked to go while changes were staged, and how the
    /// question names it.
    leaving: Option<(Action, &'static str)>,
}

impl Default for GridScreen {
    fn default() -> GridScreen {
        GridScreen::new()
    }
}

impl GridScreen {
    pub fn new() -> GridScreen {
        GridScreen {
            tab: Tab::Savings,
            savings: Page::new(FundSheet(Fund::Savings)),
            christmas: Page::new(FundSheet(Fund::Christmas)),
            benevolent: Page::new(FundSheet(Fund::Benevolent)),
            loans: Page::new(LoanSheet),
            help: false,
            leaving: None,
        }
    }

    fn counts(&self) -> [(Tab, &'static str, usize); 4] {
        [
            (Tab::Savings, self.savings.sheet.label(), self.savings.unsaved()),
            (Tab::Christmas, self.christmas.sheet.label(), self.christmas.unsaved()),
            (Tab::Benevolent, self.benevolent.sheet.label(), self.benevolent.unsaved()),
            (Tab::Loans, self.loans.sheet.label(), self.loans.unsaved()),
        ]
    }

    /// The staged changes, tab by tab — "3 in Savings / Shares, 1 in Loans" —
    /// or `None` when there are none.
    pub fn unsaved(&self) -> Option<String> {
        let parts: Vec<String> = self.counts().iter().filter(|(_, _, n)| *n > 0).map(|(_, label, n)| format!("{n} in {label}")).collect();
        (!parts.is_empty()).then(|| parts.join(", "))
    }

    /// Every tab over the same months, for tests that must not depend on
    /// today's date.
    #[cfg(test)]
    pub fn set_range(&mut self, from: chrono::NaiveDate, to: chrono::NaiveDate) {
        self.savings.set_range(from, to);
        self.christmas.set_range(from, to);
        self.benevolent.set_range(from, to);
        self.loans.set_range(from, to);
    }

    /// Save every tab's staged changes. True when nothing is left unsaved;
    /// otherwise the first tab still holding a refused change is shown.
    pub fn save_all(&mut self, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let saved = [
            self.savings.save(journal, actions),
            self.christmas.save(journal, actions),
            self.benevolent.save(journal, actions),
            self.loans.save(journal, actions),
        ];
        if let Some((tab, _, _)) = self.counts().into_iter().find(|(_, _, n)| *n > 0) {
            self.tab = tab;
        }
        saved.iter().all(|s| *s)
    }

    pub fn discard(&mut self) {
        self.savings.discard_all();
        self.christmas.discard_all();
        self.benevolent.discard_all();
        self.loans.discard_all();
    }

    /// Go somewhere else — after asking, when that would throw work away.
    fn leave(&mut self, action: Action, what: &'static str, actions: &mut Vec<Action>) {
        if self.unsaved().is_some() {
            self.leaving = Some((action, what));
        } else {
            actions.push(action);
        }
    }

    pub fn show(&mut self, ui: &mut egui::Ui, journal: &Journal, revision: u64, keys: bool, actions: &mut Vec<Action>) {
        let p = theme::palette(ui.ctx());
        let ctx = ui.ctx().clone();
        egui::Panel::top("grid_header").frame(egui::Frame::new().fill(p.card).inner_margin(egui::Margin::symmetric(18, 10))).show(ui, |ui| {
            self.header(ui, actions);
        });
        let mut local = Vec::new();
        egui::CentralPanel::default().frame(egui::Frame::new().fill(p.background).inner_margin(egui::Margin::symmetric(14, 8))).show(ui, |ui| {
            match self.tab {
                Tab::Savings => self.savings.show(ui, journal, revision, keys, &mut local),
                Tab::Christmas => self.christmas.show(ui, journal, revision, keys, &mut local),
                Tab::Benevolent => self.benevolent.show(ui, journal, revision, keys, &mut local),
                Tab::Loans => self.loans.show(ui, journal, revision, keys, &mut local),
            }
        });
        // A page opening a member's ledger leaves the grid, so it is asked
        // about like the Back button.
        for action in local {
            match action {
                Action::OpenMember(_) => self.leave(action, "open a member's ledger", actions),
                other => actions.push(other),
            }
        }
        if keys && ctx.input_mut(|i| i.consume_key(egui::Modifiers::NONE, egui::Key::F1)) {
            self.help = !self.help;
        }
        if self.help {
            let (label, ops) = match self.tab {
                Tab::Savings => (self.savings.sheet.label(), self.savings.sheet.ops()),
                Tab::Christmas => (self.christmas.sheet.label(), self.christmas.sheet.ops()),
                Tab::Benevolent => (self.benevolent.sheet.label(), self.benevolent.sheet.ops()),
                Tab::Loans => (self.loans.sheet.label(), self.loans.sheet.ops()),
            };
            help::show(&ctx, &mut self.help, self.tab.help(), label, ops);
        }
        self.confirm_leaving(&ctx, journal, actions);
    }

    fn header(&mut self, ui: &mut egui::Ui, actions: &mut Vec<Action>) {
        let p = theme::palette(ui.ctx());
        ui.horizontal(|ui| {
            if ui.button("⬅ Dashboard").clicked() {
                self.leave(Action::ShowDashboard, "close the grid", actions);
            }
            ui.add_space(8.0);
            ui.label(RichText::new("Ledger Grid").size(20.0).strong());
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let help = ui
                    .add(egui::Button::new(RichText::new(" ? ").strong().color(p.accent)).corner_radius(12).stroke(egui::Stroke::new(1.5, p.accent)))
                    .on_hover_text("How this grid works, and the operations this tab accepts (F1)");
                if help.clicked() {
                    self.help = !self.help;
                }
                ui.add(
                    egui::Label::new(
                        RichText::new(
                            "Type a figure for the usual entry, or an operation such as @W=500 (withdrawal) or @T=20,000/12 \
                             (loan top-up). Several in one month: 2,000, @W=500. Nothing is written until you press Save.",
                        )
                        .color(p.muted),
                    )
                    .wrap(),
                );
            });
        });
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            for (tab, label, unsaved) in self.counts() {
                let selected = self.tab == tab;
                let title = if unsaved > 0 { format!("{label}  •{unsaved}") } else { label.to_string() };
                let colour = if unsaved > 0 { p.warning } else if selected { p.accent } else { p.muted };
                let response = ui.add(egui::Button::new(RichText::new(title).strong().color(colour)).fill(if selected { p.selected } else { egui::Color32::TRANSPARENT }).stroke(egui::Stroke::NONE));
                let response = if unsaved > 0 { response.on_hover_text(format!("{unsaved} unsaved change(s) on this tab")) } else { response };
                if selected {
                    ui.painter().hline(response.rect.x_range(), response.rect.bottom(), egui::Stroke::new(2.0, p.accent));
                }
                if response.clicked() {
                    self.tab = tab;
                }
            }
        });
    }

    fn confirm_leaving(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) {
        let Some((_, what)) = &self.leaving else { return };
        let what = *what;
        let detail = self.unsaved().unwrap_or_default();
        let p = theme::palette(ctx);
        let (mut save, mut discard, mut cancel) = (false, false, false);
        let response = egui::Modal::new(egui::Id::new("grid_leaving")).show(ctx, |ui| {
            ui.set_max_width(480.0);
            ui.heading("Unsaved Changes");
            ui.add_space(4.0);
            ui.label(format!("There are unsaved changes ({detail}).\n\nSave them before you {what}?"));
            crate::dialogs::footer(ui, |ui| {
                // Enter saves: the answer that loses nothing.
                if crate::widgets::primary(ui, "Save Changes", p.success) {
                    save = true;
                }
                if ui.button(RichText::new("Discard").color(p.danger)).clicked() {
                    discard = true;
                }
                if ui.button("Cancel").clicked() {
                    cancel = true;
                }
            });
        });
        if save {
            // A refused change keeps the grid open, its reason in red.
            if self.save_all(journal, actions)
                && let Some((action, _)) = self.leaving.take()
            {
                actions.push(action);
            }
            self.leaving = None;
        } else if discard {
            self.discard();
            if let Some((action, _)) = self.leaving.take() {
                actions.push(action);
            }
        } else if cancel || response.should_close() {
            self.leaving = None;
        }
    }
}
