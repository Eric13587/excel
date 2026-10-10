//! Adding a member, editing one's details, and a member leaving.

use chrono::NaiveDate;
use eframe::egui::{self, RichText};
use loanmaster_core::db::members::MemberDetails;
use loanmaster_core::db::model::{Fund, MEMBER_STATUSES};
use loanmaster_core::membership::{self, FieldError, LEAVING, Payout};
use loanmaster_core::{Journal, Result, dates};

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
    /// The status the member had when the form opened, and what they hold:
    /// a change of status that takes them out of the society pays it out.
    was: String,
    holding: Payout,
}

impl MemberForm {
    pub fn add() -> MemberForm {
        MemberForm {
            id: None,
            details: MemberDetails { employment_status: "Active".into(), ..MemberDetails::default() },
            errors: Vec::new(),
            warned_name: None,
            problem: None,
            was: "Active".into(),
            holding: Payout::default(),
        }
    }

    pub fn edit(journal: &Journal, id: i64) -> Result<MemberForm> {
        let member = journal.members().get(id)?.ok_or(loanmaster_core::Error::MemberNotFound(id))?;
        let holding = holding(journal, id)?;
        Ok(MemberForm {
            was: if member.employment_status.is_empty() { "Active".into() } else { member.employment_status.clone() },
            holding,
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

    /// Saving would take the member out of the society, paying them out.
    fn pays_out(&self) -> bool {
        self.id.is_some() && self.holding.total() > 0.0 && membership::is_leaving(&self.details.employment_status) && !membership::is_leaving(&self.was)
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
            Some(id) => membership::update(journal, id, &self.details).map(|paid| (id, paid)),
            None => membership::add(journal, &self.details).map(|id| (id, Payout::default())),
        };
        match result {
            Ok((id, paid)) => {
                let verb = if self.id.is_some() { "saved" } else { "added" };
                let mut status = format!("{name} {verb}.");
                if let Some(paid) = paid.describe() {
                    status.push_str(&format!(" Paid out {paid}."));
                }
                actions.push(Action::Status(status));
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
                    if self.pays_out() {
                        ui.label("");
                        ui.add(
                            egui::Label::new(
                                RichText::new(format!(
                                    "Saving records them as having left today and pays out their {} in full. \
                                     To record another leaving date, use Retire or Leave on the dashboard.",
                                    self.holding.describe().unwrap_or_default()
                                ))
                                .small()
                                .color(p.warning),
                            )
                            .wrap(),
                        );
                        ui.end_row();
                    }
                }
                submitted = ui.input(|i| i.key_pressed(egui::Key::Enter));
            });
            if let Some(problem) = &self.problem {
                ui.add_space(6.0);
                ui.label(RichText::new(problem).color(p.warning));
            }
            super::footer(ui, |ui| {
                let label = if self.pays_out() { "Save and Pay Out" } else if self.id.is_some() { "Save" } else { "Add Member" };
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

/// A member leaving the society: how and when, and what that does — before
/// it is done.
pub struct Leave {
    id: i64,
    name: String,
    status: &'static str,
    date: NaiveDate,
    holding: Payout,
    loans: bool,
    problem: Option<String>,
}

impl Leave {
    pub fn new(journal: &Journal, id: i64) -> Result<Leave> {
        let name = journal.members().name_of(id)?.ok_or(loanmaster_core::Error::MemberNotFound(id))?;
        Ok(Leave {
            id,
            name,
            status: LEAVING[0].0,
            date: chrono::Local::now().date_naive(),
            holding: holding(journal, id)?,
            loans: membership::has_outstanding_loans(journal, id)?,
            problem: None,
        })
    }

    fn confirm(&mut self, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        match membership::leave(journal, self.id, self.status, Some(&dates::format(self.date))) {
            Ok(paid) => {
                let mut status = format!("{} is recorded as {}.", self.name, self.status.to_lowercase());
                if let Some(paid) = paid.describe() {
                    status.push_str(&format!(" Paid out {paid}."));
                }
                actions.push(Action::Status(status));
                actions.push(Action::Changed);
                true
            }
            Err(e) if e.is_user_facing() => {
                self.problem = Some(e.to_string());
                false
            }
            Err(e) => {
                actions.push(Action::Notice(Notice::from_error("The member could not be recorded as leaving", &e)));
                true
            }
        }
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let response = egui::Modal::new(egui::Id::new("member_leave")).show(ctx, |ui| {
            ui.set_width(460.0);
            ui.heading("Retire or Leave");
            ui.add_space(4.0);
            ui.label(format!("{} leaves the society:", self.name));
            ui.horizontal_wrapped(|ui| {
                for (status, _) in LEAVING {
                    ui.radio_value(&mut self.status, status, status);
                }
            });
            ui.horizontal(|ui| {
                ui.label("On");
                widgets::date(ui, "leave_date", &mut self.date);
            });
            ui.add_space(6.0);
            let note = LEAVING.iter().find(|(s, _)| *s == self.status).map(|(_, n)| *n).unwrap_or_default();
            if let Some(holding) = self.holding.describe() {
                ui.label(format!("• Their {holding} are paid out in full that day, noted “{note}”."));
            }
            if self.loans {
                ui.label("• Their loans stay active until they are cleared, and still appear in the loan report.");
            }
            ui.label("• They take on nothing new — no loans, deposits or contributions — unless they are reinstated.");
            if let Some(problem) = &self.problem {
                ui.add_space(6.0);
                ui.add(egui::Label::new(RichText::new(problem).color(p.danger)).wrap());
            }
            super::footer(ui, |ui| {
                if widgets::solid(ui, "Confirm", p.warning).clicked() && self.confirm(journal, actions) {
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

/// What a member holds that leaving pays out: their savings and Christmas
/// savings, as they stand.
fn holding(journal: &Journal, id: i64) -> Result<Payout> {
    Ok(Payout { savings: journal.funds().balance(Fund::Savings, id)?, christmas: journal.funds().balance(Fund::Christmas, id)? })
}
