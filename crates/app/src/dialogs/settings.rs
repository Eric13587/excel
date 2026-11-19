//! The journal's settings: who the society is, its financial year and
//! interest rate, the funds' defaults, report colours, and the theme.
//!
//! Every value lives in the journal, so a journal carried to another machine
//! keeps its branding and rules.

use eframe::egui::{self, RichText};
use loanmaster_core::db::settings::{self as keys, default_for};
use loanmaster_core::money::figure;
use loanmaster_core::{Journal, dates};

use super::files;
use crate::app::{Action, outcome};
use crate::theme;
use crate::widgets;

pub struct SettingsForm {
    org_name: String,
    logo: String,
    fy_month: u32,
    rate: String,
    deduct_same_month: bool,
    unlock_month: u32,
    savings_increment: String,
    christmas_increment: String,
    header_bg: [u8; 3],
    total_bg: [u8; 3],
    dark: bool,
    error: Option<String>,
    /// The chosen logo file, read once for its preview.
    logo_bytes: Option<(String, Option<egui::load::Bytes>)>,
}

fn rgb(hex: &str) -> Option<[u8; 3]> {
    let hex = hex.trim().strip_prefix('#')?;
    if hex.len() != 6 {
        return None;
    }
    let byte = |i: usize| u8::from_str_radix(hex.get(i..i + 2)?, 16).ok();
    Some([byte(0)?, byte(2)?, byte(4)?])
}

fn hex([r, g, b]: [u8; 3]) -> String {
    format!("#{r:02X}{g:02X}{b:02X}")
}

fn month_picker(ui: &mut egui::Ui, id: &str, month: &mut u32) {
    egui::ComboBox::from_id_salt(id).selected_text(dates::month_name(*month)).width(160.0).show_ui(ui, |ui| {
        for m in 1..=12 {
            ui.selectable_value(month, m, dates::month_name(m));
        }
    });
}

impl SettingsForm {
    pub fn new(journal: &Journal) -> SettingsForm {
        let settings = journal.settings();
        let colour = |value: String, key: &str| rgb(&value).or_else(|| rgb(default_for(key))).unwrap_or([0xE2, 0xE8, 0xF0]);
        SettingsForm {
            org_name: settings.org_name(),
            logo: settings.org_logo_path(),
            fy_month: settings.fy_start_month(),
            rate: figure(settings.default_interest_rate() * 100.0),
            deduct_same_month: settings.deduct_same_month(),
            unlock_month: settings.christmas_unlock_month(),
            savings_increment: figure(settings.default_savings_increment()),
            christmas_increment: figure(settings.default_christmas_increment()),
            header_bg: colour(settings.excel_header_bg(), keys::EXCEL_HEADER_BG),
            total_bg: colour(settings.excel_total_bg(), keys::EXCEL_TOTAL_BG),
            dark: settings.app_theme() == "Dark",
            error: None,
            logo_bytes: None,
        }
    }

    /// The logo as it will print, for showing: the bundled one, or the chosen
    /// file when it is an image this window can draw.
    fn preview(&mut self) -> Option<(String, egui::load::Bytes)> {
        if self.logo.trim().is_empty() {
            return Some(("bytes://logo/bundled.png".into(), egui::load::Bytes::Static(loanmaster_reports::BUNDLED_LOGO)));
        }
        if self.logo_bytes.as_ref().is_none_or(|(path, _)| *path != self.logo) {
            let path = std::path::Path::new(self.logo.trim());
            let drawable = path.extension().and_then(|e| e.to_str()).is_some_and(|e| ["png", "jpg", "jpeg"].contains(&e.to_ascii_lowercase().as_str()));
            let bytes = if drawable { std::fs::read(path).ok().map(|b| egui::load::Bytes::Shared(b.into())) } else { None };
            self.logo_bytes = Some((self.logo.clone(), bytes));
        }
        let (path, bytes) = self.logo_bytes.as_ref()?;
        Some((format!("bytes://logo/{path}"), bytes.clone()?))
    }

    /// The values to write, or what is wrong with the form.
    fn values(&self) -> Result<Vec<(&'static str, String)>, String> {
        let rate = widgets::number(&self.rate).filter(|r| (0.0..=100.0).contains(r)).ok_or("The interest rate must be a percentage from 0 to 100.")?;
        let savings = widgets::number(&self.savings_increment).filter(|v| *v >= 0.0).ok_or("The default monthly savings must be a figure of 0 or more.")?;
        let christmas = widgets::number(&self.christmas_increment).filter(|v| *v >= 0.0).ok_or("The default monthly Christmas must be a figure of 0 or more.")?;
        Ok(vec![
            (keys::ORG_NAME, self.org_name.trim().to_string()),
            (keys::ORG_LOGO_PATH, self.logo.trim().to_string()),
            (keys::FY_START_MONTH, dates::month_name(self.fy_month).to_string()),
            // Kept to the fraction 1.x stored: 15% is 0.15.
            (keys::DEFAULT_INTEREST_RATE, format!("{}", loanmaster_core::money::round_places(rate / 100.0, 6))),
            (keys::DEDUCT_SAME_MONTH, if self.deduct_same_month { "true" } else { "false" }.to_string()),
            (keys::CHRISTMAS_UNLOCK_MONTH, self.unlock_month.to_string()),
            (keys::DEFAULT_SAVINGS_INCREMENT, format!("{savings}")),
            (keys::DEFAULT_CHRISTMAS_INCREMENT, format!("{christmas}")),
            (keys::EXCEL_HEADER_BG, hex(self.header_bg)),
            (keys::EXCEL_TOTAL_BG, hex(self.total_bg)),
            (keys::APP_THEME, if self.dark { "Dark" } else { "Light" }.to_string()),
        ])
    }

    fn save(&mut self, actions: &mut Vec<Action>) -> bool {
        let values = match self.values() {
            Ok(values) => values,
            Err(problem) => {
                self.error = Some(problem);
                return false;
            }
        };
        actions.push(Action::run(move |ws, actions| {
            let journal = &ws.journal;
            let saved = journal.transaction(|| {
                for (key, value) in &values {
                    journal.settings().set(key, value)?;
                }
                Ok(())
            });
            outcome(actions, "The settings could not be saved", saved, |_| "Settings saved.".to_string());
        }));
        true
    }

    pub fn show(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let mut close = false;
        egui::Window::new("Settings").open(&mut open).collapsible(false).resizable(false).show(ctx, |ui| {
            ui.set_width(520.0);
            egui::ScrollArea::vertical().max_height(widgets::dialog_room(ctx)).show(ui, |ui| {
                let section = |ui: &mut egui::Ui, title: &str| {
                    ui.add_space(6.0);
                    ui.label(RichText::new(title).strong().size(15.0));
                    ui.separator();
                };

                section(ui, "Organization");
                egui::Grid::new("settings_org").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                    let label = ui.label("Organization name");
                    ui.add(egui::TextEdit::singleline(&mut self.org_name).hint_text("e.g. Umoja Staff Co-operative").desired_width(320.0)).labelled_by(label.id);
                    ui.end_row();
                    ui.label("Logo");
                    match self.preview() {
                        Some((uri, bytes)) => {
                            ui.add(egui::Image::from_bytes(uri, bytes).fit_to_exact_size(egui::vec2(140.0, 64.0)));
                        }
                        None => {
                            ui.label(RichText::new("(no preview for this kind of image)").small().color(p.muted));
                        }
                    }
                    ui.end_row();
                    ui.label("");
                    let shown = if self.logo.is_empty() { "Bundled default logo".to_string() } else { self.logo.clone() };
                    ui.add(egui::Label::new(RichText::new(shown).color(if self.logo.is_empty() { p.muted } else { p.text })).truncate()).on_hover_text(&self.logo);
                    ui.end_row();
                    ui.label("");
                    ui.horizontal(|ui| {
                        if ui.button("Browse…").clicked()
                            && let Some(path) = files::pick("Choose Logo", &[("Images", &["png", "jpg", "jpeg", "gif", "svg"])])
                        {
                            self.logo = path.display().to_string();
                        }
                        if ui.add_enabled(!self.logo.is_empty(), egui::Button::new("Clear")).clicked() {
                            self.logo.clear();
                        }
                    });
                    ui.end_row();
                });
                ui.label(RichText::new("Shown on statements, reports and exports. Leave the logo blank to use the bundled one.").small().color(p.muted));

                section(ui, "Financial");
                egui::Grid::new("settings_financial").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                    ui.label("Financial year starts in");
                    month_picker(ui, "settings_fy", &mut self.fy_month);
                    ui.end_row();
                    let label = ui.label("Loan interest rate");
                    ui.horizontal(|ui| {
                        ui.add(egui::TextEdit::singleline(&mut self.rate).desired_width(80.0).horizontal_align(egui::Align::RIGHT)).labelled_by(label.id);
                        ui.label("%");
                    })
                    .response
                    .on_hover_text("The rate a new loan is written at unless another is given. Existing loans keep their own rate.");
                    ui.end_row();
                    ui.label("");
                    ui.checkbox(&mut self.deduct_same_month, "Allow deduction in the same month as issue");
                    ui.end_row();
                });

                section(ui, "Funds");
                egui::Grid::new("settings_funds").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                    ui.label("Christmas withdrawal month");
                    month_picker(ui, "settings_unlock", &mut self.unlock_month);
                    ui.end_row();
                    let label = ui.label("Default monthly savings");
                    ui.add(egui::TextEdit::singleline(&mut self.savings_increment).desired_width(120.0).horizontal_align(egui::Align::RIGHT)).labelled_by(label.id);
                    ui.end_row();
                    let label = ui.label("Default monthly Christmas");
                    ui.add(egui::TextEdit::singleline(&mut self.christmas_increment).desired_width(120.0).horizontal_align(egui::Align::RIGHT)).labelled_by(label.id);
                    ui.end_row();
                });
                ui.label(RichText::new("The defaults apply when a member has no contribution history to infer an amount from.").small().color(p.muted));

                section(ui, "Reports");
                egui::Grid::new("settings_reports").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                    ui.label("Workbook header background");
                    ui.color_edit_button_srgb(&mut self.header_bg);
                    ui.end_row();
                    ui.label("Workbook total row background");
                    ui.color_edit_button_srgb(&mut self.total_bg);
                    ui.end_row();
                    ui.label("");
                    if ui.button("Reset Colours").clicked() {
                        self.header_bg = rgb(default_for(keys::EXCEL_HEADER_BG)).unwrap_or_default();
                        self.total_bg = rgb(default_for(keys::EXCEL_TOTAL_BG)).unwrap_or_default();
                    }
                    ui.end_row();
                });

                section(ui, "Appearance");
                ui.horizontal(|ui| {
                    ui.label("Theme");
                    ui.radio_value(&mut self.dark, false, "Light");
                    ui.radio_value(&mut self.dark, true, "Dark");
                });

                if let Some(error) = &self.error {
                    ui.add_space(6.0);
                    ui.colored_label(p.danger, error);
                }
            });
            super::footer(ui, |ui| {
                if widgets::primary(ui, "Save", p.accent) && self.save(actions) {
                    close = true;
                }
                if ui.button("Cancel").clicked() || widgets::escape(ui) {
                    close = true;
                }
            });
        });
        open && !close
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn colours_round_trip_through_hex() {
        assert_eq!(rgb("#e2e8f0"), Some([0xE2, 0xE8, 0xF0]));
        assert_eq!(hex([0xE2, 0xE8, 0xF0]), "#E2E8F0");
        assert_eq!(rgb("E2E8F0"), None);
        assert_eq!(rgb("#12345"), None);
    }
}
