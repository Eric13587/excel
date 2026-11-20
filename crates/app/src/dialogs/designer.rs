use super::{columns::Columns, files};
use crate::app::{Action, Notice};
use crate::task::Task;
use eframe::egui;
use loanmaster_core::{Journal, documents};
use loanmaster_reports::Branding;

pub struct Designer {
    source: usize,
    columns: Columns,
    header: String,
    title: String,
    subtitle: String,
    logo: bool,
    email: bool,
}
impl Designer {
    pub fn new(j: &Journal) -> Self {
        let mut d = Self {
            source: 0,
            columns: Columns::default(),
            header: j.settings().org_name(),
            title: String::new(),
            subtitle: String::new(),
            logo: true,
            email: false,
        };
        d.select(j);
        d
    }
    fn select(&mut self, j: &Journal) {
        self.title = documents::SOURCES[self.source].0.into();
        let names = documents::columns(j, self.source).unwrap_or_default();
        self.columns = Columns::keyed(names.iter().map(|n| {
            (
                n.as_str(),
                n.as_str(),
                matches!(
                    n.as_str(),
                    "Name" | "Member" | "Pf no" | "Payroll number" | "Date" | "Amount" | "Ref"
                ),
            )
        }));
    }
    pub fn show(&mut self, ctx: &egui::Context, j: &Journal, actions: &mut Vec<Action>) -> bool {
        let mut open = true;
        egui::Window::new("Document builder")
            .open(&mut open)
            .show(ctx, |ui| {
                let before = self.source;
                egui::ComboBox::from_label("Data source")
                    .selected_text(documents::SOURCES[before].0)
                    .show_ui(ui, |ui| {
                        for (i, (name, _)) in documents::SOURCES.iter().enumerate() {
                            ui.selectable_value(&mut self.source, i, *name);
                        }
                    });
                if before != self.source {
                    self.select(j);
                }
                for (label, field) in [
                    ("Letterhead", &mut self.header),
                    ("Document title", &mut self.title),
                    ("Subtitle", &mut self.subtitle),
                ] {
                    ui.horizontal(|ui| {
                        ui.label(label);
                        ui.text_edit_singleline(field);
                    });
                }
                ui.checkbox(&mut self.logo, "Include organization logo");
                self.columns.show(ui, "document_fields", 200.0);
                ui.checkbox(
                    &mut self.email,
                    "Open an email draft with this document attached",
                );
                if ui.button("Generate document…").clicked() {
                    let table = match documents::build(
                        j,
                        self.source,
                        &self.columns.chosen(),
                        self.title.clone(),
                        self.subtitle.clone(),
                    ) {
                        Ok(t) => t,
                        Err(e) => {
                            actions
                                .push(Action::Notice(Notice::from_error("Document builder", &e)));
                            return;
                        }
                    };
                    let Some(path) = files::save_as(
                        "Save document",
                        "Document.pdf",
                        &[("PDF", "pdf"), ("Excel", "xlsx")],
                    ) else {
                        return;
                    };
                    let mut brand = Branding::of(j);
                    brand.org_name.clone_from(&self.header);
                    if !self.logo {
                        brand.logo = None;
                    }
                    let email = self.email;
                    let task = Task::spawn(ctx, "Generating document", move |_| {
                        let subject = table.title.clone();
                        let result = loanmaster_reports::write_custom_table(&table, &brand, &path);
                        Box::new(move |actions: &mut Vec<Action>| match result {
                            Err(e) => actions.push(Action::Notice(Notice::warning(
                                "Document export failed",
                                e.to_string(),
                            ))),
                            Ok(()) if email => super::mail::draft(actions, "", &subject, &path),
                            Ok(()) => files::saved(actions, "Document", &path),
                        })
                    });
                    actions.push(Action::Task(Box::new(task)));
                }
            });
        open
    }
}
