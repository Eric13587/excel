//! Adding a member or editing one's details.

use eframe::egui::{self, RichText};
use loanmaster_core::db::members::MemberDetails;
use loanmaster_core::db::model::MEMBER_STATUSES;
use loanmaster_core::membership::{self, FieldError};
use loanmaster_core::{Journal, Result};

use crate::app::{Action, Notice};
use crate::theme;
use crate::widgets;

pub struct MemberForm {
    id: Option<i64>,
    details: MemberDetails,
    errors: Vec<FieldError>,
    /// The name the user was told is taken: saving it again goes ahead.
    warned_name: Option<String>,
    problem: Option<String>,
}

impl MemberForm {
    pub fn add() -> MemberForm {
        MemberForm {
            id: None,
            details: MemberDetails { employment_status: "Active".into(), ..MemberDetails::default() },
            errors: Vec::new(),
            warned_name: None,
            problem: None,
        }
    }

    pub fn edit(journal: &Journal, id: i64) -> Result<MemberForm> {
        let member = journal.members().get(id)?.ok_or(loanmaster_core::Error::MemberNotFound(id))?;
        Ok(MemberForm {
            id: Some(id),
            details: MemberDetails {
                name: member.name,
                phone: member.phone,
                email: member.email,
                employment_status: if member.employment_status.is_empty() { "Active".into() } else { member.employment_status },
                pf_no: member.pf_no,
                id_no: member.id_no,
            },
            errors: Vec::new(),
            warned_name: None,
            problem: None,
        })
    }

    fn error(&self, field: &str) -> Option<&str> {
        self.errors.iter().find(|e| e.field == field).map(|e| e.message.as_str())
    }

    fn save(&mut self, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        self.errors = membership::validate(&self.details);
        if !self.errors.is_empty() {
            return false;
        }
        let name = self.details.name.split_whitespace().collect::<Vec<_>>().join(" ");
        if self.warned_name.as_deref() != Some(name.as_str()) && journal.members().name_exists(&name, self.id).unwrap_or(false) {
            self.problem = Some(format!("Another member is already called {name}. Save again to keep both."));
            self.warned_name = Some(name);
            return false;
        }
        let result = match self.id {
            Some(id) => membership::update(journal, id, &self.details).map(|()| id),
            None => membership::add(journal, &self.details),
        };
        match result {
            Ok(id) => {
                let verb = if self.id.is_some() { "saved" } else { "added" };
                actions.push(Action::Status(format!("{name} {verb}.")));
                actions.push(Action::Changed);
                actions.push(Action::run(move |ws, _| ws.dashboard.selected = Some(id)));
                true
            }
            Err(e) if e.is_user_facing() => {
                self.problem = Some(e.to_string());
                false
            }
            Err(e) => {
                actions.push(Action::Notice(Notice::from_error("The member could not be saved", &e)));
                true
            }
        }
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let response = egui::Modal::new(egui::Id::new("member_form")).show(ctx, |ui| {
            ui.set_width(460.0);
            ui.heading(if self.id.is_some() { "Edit Member" } else { "New Member" });
            ui.add_space(8.0);
            let mut submitted = false;
            egui::Grid::new("member_fields").num_columns(2).spacing([14.0, 8.0]).show(ui, |ui| {
                let name_error = self.error("name").map(String::from);
                let name = widgets::field(ui, "Name", &mut self.details.name, "Full name", name_error.as_deref());
                if self.id.is_none() && ui.memory(|m| m.focused().is_none()) {
                    name.request_focus();
                }
                let error = self.error("phone").map(String::from);
                widgets::field(ui, "Phone", &mut self.details.phone, "0700 000 000", error.as_deref());
                let error = self.error("email").map(String::from);
                widgets::field(ui, "Email", &mut self.details.email, "name@example.com", error.as_deref());
                widgets::field(ui, "PF No", &mut self.details.pf_no, "Payroll number", None);
                widgets::field(ui, "ID No", &mut self.details.id_no, "National ID", None);
                if self.id.is_some() {
                    ui.label("Status");
                    egui::ComboBox::from_id_salt("member_status").selected_text(&self.details.employment_status).show_ui(ui, |ui| {
                        for status in MEMBER_STATUSES {
                            ui.selectable_value(&mut self.details.employment_status, status.to_string(), *status);
                        }
                    });
                    ui.end_row();
                }
                submitted = ui.input(|i| i.key_pressed(egui::Key::Enter));
            });
            if let Some(problem) = &self.problem {
                ui.add_space(6.0);
                ui.label(RichText::new(problem).color(p.warning));
            }
            super::footer(ui, |ui| {
                let label = if self.id.is_some() { "Save" } else { "Add Member" };
                if (widgets::solid(ui, label, p.accent).clicked() || submitted) && self.save(journal, actions) {
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
