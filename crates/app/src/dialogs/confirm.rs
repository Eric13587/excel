//! "Are you sure?" — with the consequences spelled out, and the work to do
//! on yes.

use eframe::egui::{self, RichText};

use crate::app::{Action, Job};
use crate::theme;
use crate::widgets;
use crate::workspace::Workspace;

pub struct Confirm {
    title: String,
    body: String,
    confirm: String,
    danger: bool,
    job: Option<Job>,
}

impl Confirm {
    pub fn new(
        title: impl Into<String>,
        body: impl Into<String>,
        confirm: impl Into<String>,
        job: impl FnOnce(&mut Workspace, &mut Vec<Action>) + 'static,
    ) -> Confirm {
        Confirm { title: title.into(), body: body.into(), confirm: confirm.into(), danger: false, job: Some(Box::new(job)) }
    }

    /// The confirming button in red: the change cannot be taken back.
    pub fn danger(mut self) -> Confirm {
        self.danger = true;
        self
    }

    pub fn show(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let response = egui::Modal::new(egui::Id::new("confirm")).show(ctx, |ui| {
            ui.set_max_width(480.0);
            ui.label(RichText::new(&self.title).heading().color(if self.danger { p.danger } else { p.text }));
            ui.add_space(6.0);
            ui.label(&self.body);
            super::footer(ui, |ui| {
                if widgets::solid(ui, &self.confirm, if self.danger { p.danger } else { p.accent }).clicked() {
                    if let Some(job) = self.job.take() {
                        actions.push(Action::Run(job));
                    }
                    open = false;
                }
                if ui.button("Cancel").clicked() {
                    open = false;
                }
            });
        });
        open && !response.should_close()
    }
}
