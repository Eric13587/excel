//! Bringing members in: from another journal, from a spreadsheet, and the
//! history of imports with a way to take one back out.
//!
//! Nothing is written until the last step, and each import is one
//! transaction after a safety copy of the journal.

use std::collections::{BTreeSet, HashMap};
use std::path::PathBuf;

use chrono::{Datelike, NaiveDate};
use eframe::egui::{self, RichText};
use egui_extras::{Column, TableBuilder};
use loanmaster_core::db::model::{Fund, ImportRun};
use loanmaster_core::imports::excel::{self, Field, Mapping, Plan, SheetKind};
use loanmaster_core::imports::names::Verdict;
use loanmaster_core::imports::{self, Conflict, Decision, Options, Preview, Source, SourceMember};
use loanmaster_core::money::shillings;
use loanmaster_core::{Journal, dates};

use super::files;
use crate::app::{Action, Notice};
use crate::theme;
use crate::widgets;

/// Warnings beyond this many are counted, not listed.
const WARNINGS_SHOWN: usize = 15;

fn warnings_text(warnings: &[String]) -> String {
    if warnings.is_empty() {
        return String::new();
    }
    let mut text = format!("\n\n{} warning(s):\n{}", warnings.len(), warnings.iter().take(WARNINGS_SHOWN).cloned().collect::<Vec<_>>().join("\n"));
    if warnings.len() > WARNINGS_SHOWN {
        text.push_str(&format!("\n…and {} more (all of them are in the log).", warnings.len() - WARNINGS_SHOWN));
    }
    text
}

/// The import entries, for the File menu and the dashboard's Import button.
pub fn menu(ui: &mut egui::Ui, actions: &mut Vec<Action>) {
    if ui.button("Import From Another Journal (.db)…").clicked() {
        actions.push(Action::dialog(JournalImport::default()));
    }
    if ui.button("Import From Excel (.xlsx)…").clicked() {
        actions.push(Action::dialog(ExcelImport::default()));
    }
    if ui.button("Import History…").on_hover_text("Every import, and a way to take one back out").clicked() {
        actions.push(Action::dialog(ImportHistory::default()));
    }
}

// ------------------------------------------------------- another journal

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Step {
    Source,
    Members,
    Preview,
}

pub struct JournalImport {
    step: Step,
    source: Option<Source>,
    members: Vec<SourceMember>,
    chosen: BTreeSet<i64>,
    filter: String,
    loans: bool,
    savings: bool,
    funds: bool,
    by_date: bool,
    from: NaiveDate,
    to: NaiveDate,
    /// Everyone among the chosen who resembles a member on record, worked
    /// out once: a decision takes a member off the core's own list.
    conflicts: Vec<Conflict>,
    decisions: HashMap<i64, Decision>,
    preview: Option<Preview>,
}

impl Default for JournalImport {
    fn default() -> JournalImport {
        let today = chrono::Local::now().date_naive();
        JournalImport {
            step: Step::Source,
            source: None,
            members: Vec::new(),
            chosen: BTreeSet::new(),
            filter: String::new(),
            loans: false,
            savings: false,
            funds: false,
            by_date: false,
            from: dates::add_months(today, -1),
            to: today,
            conflicts: Vec::new(),
            decisions: HashMap::new(),
            preview: None,
        }
    }
}

impl JournalImport {
    fn options(&self) -> Options {
        Options {
            loans: self.loans,
            savings: self.savings,
            funds: self.funds,
            dates: self.by_date.then(|| (dates::format(self.from), dates::format(self.to))),
        }
    }

    fn choose_file(&mut self, actions: &mut Vec<Action>) {
        let Some(path) = files::pick("Select Source Database", &[("LoanMaster journal", &["db"])]) else { return };
        match Source::open(&path).and_then(|source| Ok((source.members()?, source))) {
            Ok((members, _)) if members.is_empty() => {
                actions.push(Action::Notice(Notice::info("Import", "That journal has no members to import.")));
            }
            Ok((members, source)) => {
                self.chosen = members.iter().map(|m| m.id).collect();
                self.members = members;
                self.source = Some(source);
                self.preview = None;
            }
            Err(e) => actions.push(Action::Notice(Notice::warning("The selected file cannot be imported", e.to_string()))),
        }
    }

    /// Work out the preview again, after a choice changed.
    fn refresh(&mut self, journal: &Journal, actions: &mut Vec<Action>) {
        let Some(source) = &self.source else { return };
        let ids: Vec<i64> = self.chosen.iter().copied().collect();
        match imports::preview(journal, source, &ids, &self.options(), &self.decisions) {
            Ok(preview) => self.preview = Some(preview),
            Err(e) => {
                actions.push(Action::Notice(Notice::from_error("The import could not be previewed", &e)));
                self.step = Step::Members;
            }
        }
    }

    fn start_preview(&mut self, journal: &Journal, actions: &mut Vec<Action>) {
        let Some(source) = &self.source else { return };
        let ids: Vec<i64> = self.chosen.iter().copied().collect();
        // The conflicts as they stand before any decision.
        match imports::preview(journal, source, &ids, &self.options(), &HashMap::new()) {
            Ok(first) => {
                self.conflicts = first.conflicts.clone();
                let ids: BTreeSet<i64> = self.conflicts.iter().map(|c| c.source.id).collect();
                self.decisions.retain(|id, _| ids.contains(id));
                for conflict in &self.conflicts {
                    self.decisions.entry(conflict.source.id).or_insert_with(|| conflict.suggested());
                }
                self.step = Step::Preview;
                self.refresh(journal, actions);
            }
            Err(e) => actions.push(Action::Notice(Notice::from_error("The import could not be previewed", &e))),
        }
    }

    fn start_import(&mut self, actions: &mut Vec<Action>) -> bool {
        let Some(source) = self.source.take() else { return false };
        let ids: Vec<i64> = self.chosen.iter().copied().collect();
        let (options, decisions) = (self.options(), self.decisions.clone());
        actions.push(Action::run(move |ws, actions| {
            match imports::run(&ws.journal, &source, &ids, &options, &decisions, &mut |_, _, _| true) {
                Ok(out) => {
                    for warning in &out.warnings {
                        log::warn!("Import from {}: {warning}", source.path.display());
                    }
                    let backup = out.backup.as_ref().map(|b| format!("\n\nThe journal as it was is saved at:\n{}", b.display())).unwrap_or_default();
                    actions.push(Action::Changed);
                    actions.push(Action::Status(format!("Imported {}.", out.summary())));
                    actions.push(Action::Notice(Notice::info(
                        "Import Complete",
                        format!("Imported {}.{}{backup}\n\nFile › Import History can take this import back out.", out.summary(), warnings_text(&out.warnings)),
                    )));
                }
                Err(e) => actions.push(Action::Notice(Notice::from_error("Nothing was imported", &e))),
            }
        }));
        true
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let mut close = false;
        egui::Window::new("Import From Another Journal").open(&mut open).collapsible(false).auto_sized().show(ctx, |ui| {
            ui.set_width(560.0);
            match self.step {
                Step::Source => self.source_step(ui, actions, &mut close),
                Step::Members => self.members_step(ui, journal, actions),
                Step::Preview => {
                    if self.preview_step(ui, journal, actions) && self.start_import(actions) {
                        close = true;
                    }
                }
            }
            if self.step != Step::Source {
                ui.add_space(2.0);
                ui.label(RichText::new(self.source.as_ref().map(|s| format!("From {}", s.path.display())).unwrap_or_default()).small().color(p.muted));
            }
        });
        open && !close
    }

    fn source_step(&mut self, ui: &mut egui::Ui, actions: &mut Vec<Action>, close: &mut bool) {
        let p = theme::palette(ui.ctx());
        ui.label(RichText::new("Source journal").strong());
        ui.horizontal(|ui| {
            match &self.source {
                Some(source) => ui.label(format!("{} — {} member(s)", source.path.display(), self.members.len())),
                None => ui.label(RichText::new("No file selected").italics().color(p.muted)),
            };
        });
        if ui.button("Select Database File…").clicked() {
            self.choose_file(actions);
        }
        ui.add_space(8.0);
        ui.label(RichText::new("What to bring across").strong());
        ui.add_enabled(false, egui::Checkbox::new(&mut true, "Members (always)"));
        ui.checkbox(&mut self.loans, "Loans, their ledger history and suspensions");
        ui.checkbox(&mut self.savings, "Savings history");
        ui.checkbox(&mut self.funds, "Christmas and benevolent funds");
        ui.checkbox(&mut self.by_date, "Only transactions dated");
        ui.add_enabled_ui(self.by_date, |ui| {
            ui.horizontal(|ui| {
                ui.label("from");
                widgets::date(ui, "import_from", &mut self.from);
                ui.label("to");
                widgets::date(ui, "import_to", &mut self.to);
            });
        });
        ui.add_space(4.0);
        ui.label(
            RichText::new("Members are matched against yours by name, phone and email; you decide what happens to anyone who resembles a member already on record.")
                .small()
                .color(p.muted),
        );
        super::footer(ui, |ui| {
            if widgets::solid_enabled(ui, self.source.is_some(), "Choose Members ›", p.accent, "Select a journal first").clicked() {
                self.step = Step::Members;
            }
            if ui.button("Cancel").clicked() {
                *close = true;
            }
        });
    }

    fn members_step(&mut self, ui: &mut egui::Ui, journal: &Journal, actions: &mut Vec<Action>) {
        let p = theme::palette(ui.ctx());
        ui.label(format!("Found {} member(s). Choose who to import:", self.members.len()));
        ui.add(egui::TextEdit::singleline(&mut self.filter).hint_text("Filter by name…").desired_width(f32::INFINITY));
        let needle = self.filter.trim().to_lowercase();
        let shown: Vec<usize> = (0..self.members.len())
            .filter(|&i| needle.is_empty() || format!("{} {}", self.members[i].name, self.members[i].phone).to_lowercase().contains(&needle))
            .collect();
        egui::Frame::new().stroke(egui::Stroke::new(1.0, p.border)).corner_radius(6).inner_margin(egui::Margin::same(4)).show(ui, |ui| {
            egui::ScrollArea::vertical().id_salt("import_members").max_height(320.0).auto_shrink([false, false]).show(ui, |ui| {
                for &i in &shown {
                    let member = &self.members[i];
                    let mut ticked = self.chosen.contains(&member.id);
                    let label = if member.phone.is_empty() { member.name.clone() } else { format!("{} | {}", member.name, member.phone) };
                    if ui.checkbox(&mut ticked, label).changed() {
                        if ticked {
                            self.chosen.insert(member.id);
                        } else {
                            self.chosen.remove(&member.id);
                        }
                    }
                }
            });
        });
        ui.horizontal(|ui| {
            if ui.button(if needle.is_empty() { "Select All" } else { "Select Shown" }).clicked() {
                self.chosen.extend(shown.iter().map(|&i| self.members[i].id));
            }
            if ui.button("Clear All").clicked() {
                self.chosen.clear();
            }
            ui.label(RichText::new(format!("{} selected", self.chosen.len())).color(p.muted));
        });
        super::footer(ui, |ui| {
            if widgets::solid_enabled(ui, !self.chosen.is_empty(), "Preview ›", p.accent, "Choose at least one member").clicked() {
                self.start_preview(journal, actions);
            }
            if ui.button("‹ Back").clicked() {
                self.step = Step::Source;
            }
        });
    }

    /// True when the user asked to import.
    fn preview_step(&mut self, ui: &mut egui::Ui, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ui.ctx());
        let Some(preview) = self.preview.clone() else { return false };
        let mut go = false;
        let mut changed = false;
        ui.label(RichText::new("What the import will do").strong());
        egui::Grid::new("import_summary").num_columns(2).spacing([16.0, 4.0]).show(ui, |ui| {
            let mut row = |caption: &str, value: String| {
                ui.label(caption);
                ui.label(RichText::new(value).strong());
                ui.end_row();
            };
            row("New members", preview.new.len().to_string());
            row("Merged into members on record", preview.merged.len().to_string());
            if !preview.skipped.is_empty() {
                row("Skipped", preview.skipped.len().to_string());
            }
            if self.loans {
                row("Loans", preview.loans.to_string());
                row("Ledger entries", preview.ledger.to_string());
            }
            if self.savings {
                row("Savings entries", preview.savings.to_string());
            }
            if self.funds {
                row("Christmas and benevolent entries", preview.fund_rows.to_string());
            }
        });
        if !preview.renamed.is_empty() {
            ui.add_space(4.0);
            ui.label(RichText::new(format!("{} loan reference(s) are already taken by the member merged into, and will be renamed:", preview.renamed.len())).color(p.warning));
            for (member, from, to) in preview.renamed.iter().take(6) {
                ui.label(RichText::new(format!("• {member}: {from} becomes {to}")).small());
            }
            if preview.renamed.len() > 6 {
                ui.label(RichText::new(format!("• …and {} more", preview.renamed.len() - 6)).small());
            }
        }
        if !self.conflicts.is_empty() {
            ui.add_space(8.0);
            ui.label(RichText::new(format!("{} member(s) resemble someone already on record — choose for each:", self.conflicts.len())).strong().color(p.warning));
            egui::ScrollArea::vertical().id_salt("import_conflicts").max_height(220.0).show(ui, |ui| {
                for conflict in &self.conflicts {
                    let id = conflict.source.id;
                    let decision = self.decisions.entry(id).or_insert_with(|| conflict.suggested());
                    egui::Frame::new().stroke(egui::Stroke::new(1.0, p.border)).corner_radius(6).inner_margin(egui::Margin::same(6)).show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        ui.label(RichText::new(&conflict.source.name).strong());
                        for m in &conflict.matches {
                            let contact = [m.phone.as_str(), m.email.as_str()].into_iter().filter(|s| !s.is_empty()).collect::<Vec<_>>().join(", ");
                            let contact = if contact.is_empty() { String::new() } else { format!(" · {contact}") };
                            ui.label(RichText::new(format!("resembles {} (ID {}){contact} — same {}", m.name, m.id, m.reasons.join(", ").to_lowercase())).small().color(p.muted));
                        }
                        let label = |d: &Decision| match d {
                            Decision::New => "Create new (keep both)".to_string(),
                            Decision::Skip => "Skip (do not import)".to_string(),
                            Decision::Merge(into) => {
                                let name = conflict.matches.iter().find(|m| m.id == *into).map(|m| m.name.clone()).unwrap_or_default();
                                format!("Merge with {name} (ID {into})")
                            }
                        };
                        let before = *decision;
                        egui::ComboBox::from_id_salt(("import_decision", id)).selected_text(label(decision)).width(320.0).show_ui(ui, |ui| {
                            for choice in [Decision::New, Decision::Skip].into_iter().chain(conflict.matches.iter().map(|m| Decision::Merge(m.id))) {
                                let text = label(&choice);
                                ui.selectable_value(decision, choice, text);
                            }
                        });
                        changed |= before != *decision;
                    });
                }
            });
        } else {
            ui.add_space(6.0);
            ui.label(RichText::new("No conflicts: nobody resembles a member on record except by an exact name match, which merges.").color(p.muted));
        }
        if changed {
            self.refresh(journal, actions);
        }
        super::footer(ui, |ui| {
            let moving = preview.new.len() + preview.merged.len();
            if widgets::solid_enabled(ui, moving > 0, "Start Import", p.accent, "Everybody is skipped").clicked() {
                go = true;
            }
            if ui.button("‹ Back").clicked() {
                self.step = Step::Members;
            }
        });
        go
    }
}

// ------------------------------------------------------------ spreadsheet

pub struct ExcelImport {
    path: Option<PathBuf>,
    sheet: Option<excel::Sheet>,
    mapping: Option<Mapping>,
    fund: Fund,
    /// The financial year the first month column falls in.
    year: i32,
    plan: Option<Plan>,
    confirming: bool,
}

impl Default for ExcelImport {
    fn default() -> ExcelImport {
        ExcelImport {
            path: None,
            sheet: None,
            mapping: None,
            fund: Fund::Christmas,
            year: chrono::Local::now().date_naive().year(),
            plan: None,
            confirming: false,
        }
    }
}

fn field_name(field: Field) -> &'static str {
    match field {
        Field::Name => "Name",
        Field::IdNo => "ID No",
        Field::PfNo => "PF No",
        Field::Phone => "Phone",
        Field::Email => "Email",
        Field::EmploymentStatus => "Employment Status",
    }
}

impl ExcelImport {
    fn choose_file(&mut self, actions: &mut Vec<Action>) {
        let Some(path) = files::pick("Select Excel File", &[("Excel workbook", &["xlsx"])]) else { return };
        match excel::read_sheet(&path) {
            Ok(sheet) => {
                self.mapping = Some(excel::detect_mapping(&sheet.headers));
                self.sheet = Some(sheet);
                self.path = Some(path);
                self.plan = None;
            }
            Err(e) => actions.push(Action::Notice(Notice::warning("Could not read the spreadsheet", e.to_string()))),
        }
    }

    fn build(&mut self, journal: &Journal, actions: &mut Vec<Action>) {
        let (Some(sheet), Some(mapping)) = (&self.sheet, &self.mapping) else { return };
        match excel::build_plan(journal, sheet, mapping, self.fund, self.year) {
            Ok(plan) => self.plan = Some(plan),
            Err(e) => actions.push(Action::Notice(Notice::from_error("The import could not be previewed", &e))),
        }
    }

    fn apply(&mut self, actions: &mut Vec<Action>) -> bool {
        let Some(plan) = self.plan.take() else { return false };
        actions.push(Action::run(move |ws, actions| match excel::apply(&ws.journal, &plan) {
            Ok(out) => {
                for warning in &out.warnings {
                    log::warn!("Spreadsheet import: {warning}");
                }
                let mut parts = vec![format!("{} member(s) updated", out.updated), format!("{} created", out.created), format!("{} skipped", out.skipped)];
                if plan.kind == SheetKind::Contribution {
                    parts.push(format!("{} deposit(s) for {} member(s), {} in all", out.deposits, out.members_credited, shillings(out.total)));
                }
                let backup = out.backup.as_ref().map(|b| format!("\n\nThe journal as it was is saved at:\n{}", b.display())).unwrap_or_default();
                actions.push(Action::Changed);
                actions.push(Action::Status(format!("Spreadsheet imported: {}.", parts.join(", "))));
                actions.push(Action::Notice(Notice::info("Import Complete", format!("{}.{}{backup}", parts.join(", "), warnings_text(&out.warnings)))));
            }
            Err(e) => actions.push(Action::Notice(Notice::from_error("Nothing was imported", &e))),
        }));
        true
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        let p = theme::palette(ctx);
        let mut open = true;
        let mut close = false;
        egui::Window::new("Import From Excel").open(&mut open).default_size([760.0, 560.0]).collapsible(false).show(ctx, |ui| {
            ui.horizontal(|ui| {
                match &self.path {
                    Some(path) => ui.label(RichText::new(path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default()).strong()),
                    None => ui.label(RichText::new("No file selected").italics().color(p.muted)),
                };
                if ui.button("Select Excel File…").clicked() {
                    self.choose_file(actions);
                }
            });
            let mut rebuild = false;
            if let (Some(sheet), Some(mapping)) = (&self.sheet, &self.mapping) {
                let kind = if mapping.kind == SheetKind::Contribution { "contribution" } else { "roster" };
                let columns = mapping.fields.iter().map(|(f, h)| format!("{} › {h}", field_name(*f))).collect::<Vec<_>>().join(", ");
                let months = if mapping.kind == SheetKind::Contribution { format!(" · {} month columns", mapping.months.len()) } else { String::new() };
                ui.label(RichText::new(format!("A {kind} sheet, {} row(s). Columns: {}{months}", sheet.rows.len(), if columns.is_empty() { "none".into() } else { columns })).color(p.muted));
                if mapping.header(Field::Name).is_none() {
                    ui.colored_label(p.danger, "No Name column found — this sheet cannot be imported.");
                }
                if mapping.kind == SheetKind::Contribution {
                    ui.horizontal(|ui| {
                        ui.label("Fund");
                        for fund in [Fund::Christmas, Fund::Benevolent] {
                            rebuild |= ui.radio_value(&mut self.fund, fund, fund.label()).changed();
                        }
                        ui.add_space(12.0);
                        ui.label("Financial year starting in");
                        rebuild |= ui.add(egui::DragValue::new(&mut self.year).range(2000..=2100)).changed();
                    });
                }
                let can = mapping.header(Field::Name).is_some();
                if self.plan.is_none() && ui.add_enabled(can, egui::Button::new("Preview")).clicked() {
                    rebuild = true;
                }
            }
            if rebuild {
                self.build(journal, actions);
            }
            ui.add_space(4.0);
            let footer_height = 64.0;
            if let Some(plan) = &mut self.plan {
                let rows = plan.rows.len();
                let updates = plan.rows.iter().filter(|r| matches!(r.action, excel::Action::Update(_))).count();
                let creates = plan.rows.iter().filter(|r| r.action == excel::Action::Create).count();
                let contribution = plan.kind == SheetKind::Contribution;
                ui.label(RichText::new(format!("{rows} row(s) — {updates} update, {creates} new, {} skipped. Review the actions, then Import.", rows - updates - creates)).strong());
                let height = (ui.available_height() - footer_height).max(160.0);
                egui::ScrollArea::horizontal().id_salt("excel_plan_scroll").show(ui, |ui| {
                    let mut table = TableBuilder::new(ui)
                        .striped(true)
                        .max_scroll_height(height)
                        .cell_layout(egui::Layout::left_to_right(egui::Align::Center))
                        .column(Column::exact(200.0))
                        .column(Column::exact(200.0))
                        .column(Column::exact(60.0))
                        .column(Column::exact(230.0));
                    if contribution {
                        table = table.column(Column::exact(100.0));
                    }
                    table
                        .header(24.0, |mut header| {
                            let mut titles = vec!["Sheet name", "Best match", "Score", "Action"];
                            if contribution {
                                titles.push("Contributions");
                            }
                            for title in titles {
                                header.col(|ui| {
                                    ui.label(RichText::new(title).strong().color(p.muted));
                                });
                            }
                        })
                        .body(|body| {
                            body.rows(28.0, rows, |mut row| {
                                let index = row.index();
                                let entry = &mut plan.rows[index];
                                row.col(|ui| {
                                    ui.add(egui::Label::new(&entry.source_name).truncate());
                                });
                                row.col(|ui| {
                                    let (text, colour) = match (&entry.best, entry.verdict) {
                                        (Some(best), Verdict::Match) => (best.name.clone(), p.text),
                                        (Some(best), Verdict::Review) => (format!("{} ?", best.name), p.warning),
                                        _ => ("—".into(), p.muted),
                                    };
                                    ui.add(egui::Label::new(RichText::new(text).color(colour)).truncate());
                                });
                                row.col(|ui| {
                                    let score = entry.best.as_ref().filter(|_| entry.verdict != Verdict::None).map(|b| format!("{:.2}", b.score));
                                    ui.label(score.unwrap_or_else(|| "—".into()));
                                });
                                row.col(|ui| {
                                    let best = entry.best.clone();
                                    let label = |action: &excel::Action| match action {
                                        excel::Action::Update(_) => format!("Update: {}", best.as_ref().map(|b| b.name.as_str()).unwrap_or("")),
                                        excel::Action::Create => "Create new member".to_string(),
                                        excel::Action::Skip => "Skip".to_string(),
                                    };
                                    egui::ComboBox::from_id_salt(("excel_action", index)).selected_text(label(&entry.action)).width(220.0).show_ui(ui, |ui| {
                                        if let Some(best) = &best {
                                            ui.selectable_value(&mut entry.action, excel::Action::Update(best.id), label(&excel::Action::Update(best.id)));
                                        }
                                        ui.selectable_value(&mut entry.action, excel::Action::Create, label(&excel::Action::Create));
                                        ui.selectable_value(&mut entry.action, excel::Action::Skip, label(&excel::Action::Skip));
                                    });
                                });
                                if contribution {
                                    row.col(|ui| widgets::right(ui, shillings(entry.months.iter().map(|(_, a)| a).sum())));
                                }
                            });
                        });
                });
            }
            super::footer(ui, |ui| {
                let ready = self.plan.as_ref().is_some_and(|plan| plan.rows.iter().any(|r| r.action != excel::Action::Skip));
                if widgets::solid_enabled(ui, ready, "Import", p.accent, "Preview the sheet first").clicked() {
                    self.confirming = true;
                }
                if ui.button("Cancel").clicked() {
                    close = true;
                }
            });
        });
        if self.confirming {
            let (mut go, mut cancel) = (false, false);
            let response = egui::Modal::new(egui::Id::new("excel_import_confirm")).show(ctx, |ui| {
                ui.set_max_width(440.0);
                ui.heading("Confirm Import");
                ui.label("Apply this import? A copy of the journal is saved first.");
                super::footer(ui, |ui| {
                    if widgets::solid(ui, "Import", p.accent).clicked() {
                        go = true;
                    }
                    if ui.button("Cancel").clicked() {
                        cancel = true;
                    }
                });
            });
            if go {
                self.confirming = false;
                if self.apply(actions) {
                    close = true;
                }
            } else if cancel || response.should_close() {
                self.confirming = false;
            }
        }
        open && !close
    }
}

// ---------------------------------------------------------------- history

#[derive(Default)]
pub struct ImportHistory {
    runs: Option<Vec<ImportRun>>,
    selected: Option<usize>,
    confirming: bool,
}

/// "Ind: 5 | Loans: 2 | Sav: 40 | Ledger: 90", as 1.x summarised a run.
fn run_summary(details: &str) -> (String, String) {
    let Ok(serde_json::Value::Object(fields)) = serde_json::from_str::<serde_json::Value>(details) else {
        return (details.to_string(), details.to_string());
    };
    let count = |key: &str| fields.get(key).and_then(|v| v.as_i64()).unwrap_or(0);
    let parts: Vec<String> = [("individuals", "Ind"), ("loans", "Loans"), ("savings", "Sav"), ("ledger", "Ledger")]
        .into_iter()
        .filter(|(key, _)| count(key) > 0)
        .map(|(key, short)| format!("{short}: {}", count(key)))
        .collect();
    let tip = fields
        .iter()
        .filter(|(_, v)| v.is_number())
        .map(|(k, v)| format!("{}: {v}", k.replace('_', " ")))
        .collect::<Vec<_>>()
        .join("\n");
    (if parts.is_empty() { "No items imported".into() } else { parts.join(" | ") }, tip)
}

impl ImportHistory {
    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        if self.runs.is_none() {
            match journal.imports().all() {
                Ok(runs) => self.runs = Some(runs),
                Err(e) => {
                    actions.push(Action::Notice(Notice::from_error("The import history could not be read", &e)));
                    return false;
                }
            }
        }
        let p = theme::palette(ctx);
        let mut open = true;
        let mut close = false;
        egui::Window::new("Import History").open(&mut open).default_size([820.0, 440.0]).collapsible(false).show(ctx, |ui| {
            let runs = self.runs.as_deref().unwrap_or_default();
            egui::Panel::bottom("import_history_footer").show(ui, |ui| {
                ui.label(
                    RichText::new("Undoing an import removes the members, loans and transactions it created, and the fund rows it added to members it merged into.")
                        .small()
                        .color(p.muted),
                );
                super::footer(ui, |ui| {
                    if ui.button("Close").clicked() {
                        close = true;
                    }
                    if widgets::solid_enabled(ui, self.selected.is_some(), "Undo Selected Import", p.danger, "Choose an import first").clicked() {
                        self.confirming = true;
                    }
                });
            });
            egui::CentralPanel::default().show(ui, |ui| {
                if runs.is_empty() {
                    ui.label(RichText::new("No imports yet.").color(p.muted));
                    return;
                }
                let mut clicked = None;
                widgets::clickable_rows(ui);
                TableBuilder::new(ui)
                    .striped(true)
                    .sense(egui::Sense::click())
                    .cell_layout(egui::Layout::left_to_right(egui::Align::Center))
                    .column(Column::exact(50.0))
                    .column(Column::exact(150.0))
                    .column(Column::remainder().at_least(200.0))
                    .column(Column::exact(60.0))
                    .column(Column::exact(240.0))
                    .header(24.0, |mut header| {
                        for title in ["ID", "Date", "Source file", "Items", "Details"] {
                            header.col(|ui| {
                                ui.label(RichText::new(title).strong().color(p.muted));
                            });
                        }
                    })
                    .body(|body| {
                        body.rows(24.0, runs.len(), |mut row| {
                            let i = row.index();
                            let run = &runs[i];
                            row.set_selected(self.selected == Some(i));
                            let (summary, tip) = run_summary(&run.details);
                            row.col(|ui| {
                                ui.label(run.id.to_string());
                            });
                            row.col(|ui| {
                                ui.label(&run.timestamp);
                            });
                            row.col(|ui| {
                                ui.add(egui::Label::new(&run.source_file).truncate());
                            });
                            row.col(|ui| widgets::right(ui, run.item_count.to_string()));
                            row.col(|ui| {
                                ui.label(summary).on_hover_text(tip);
                            });
                            if row.response().clicked() {
                                clicked = Some(i);
                            }
                        });
                    });
                if let Some(i) = clicked {
                    self.selected = Some(i);
                }
            });
        });
        self.confirm(ctx, actions);
        open && !close
    }

    fn confirm(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) {
        if !self.confirming {
            return;
        }
        let Some(run) = self.selected.and_then(|i| self.runs.as_ref()?.get(i)).cloned() else {
            self.confirming = false;
            return;
        };
        let p = theme::palette(ctx);
        let (mut go, mut cancel) = (false, false);
        let response = egui::Modal::new(egui::Id::new("import_undo_confirm")).show(ctx, |ui| {
            ui.set_max_width(480.0);
            ui.heading("Undo Import");
            ui.label(format!(
                "Undo the import of {}?\n\nSource: {}\n\nThe members, loans and transactions it created are deleted; members it merged into keep their own records. A copy of the journal is saved first.",
                run.timestamp, run.source_file
            ));
            super::footer(ui, |ui| {
                if widgets::solid(ui, "Undo Import", p.danger).clicked() {
                    go = true;
                }
                if ui.button("Cancel").clicked() {
                    cancel = true;
                }
            });
        });
        if go {
            self.confirming = false;
            self.selected = None;
            // Read again once undone.
            self.runs = None;
            let id = run.id;
            actions.push(Action::run(move |ws, actions| match imports::undo(&ws.journal, id) {
                Ok(backup) => {
                    let backup = backup.map(|b| format!("\n\nThe journal as it was is saved at:\n{}", b.display())).unwrap_or_default();
                    actions.push(Action::Changed);
                    actions.push(Action::Status("The import was undone.".into()));
                    actions.push(Action::Notice(Notice::info("Import Undone", format!("The import was taken back out.{backup}"))));
                }
                Err(e) => actions.push(Action::Notice(Notice::from_error("The import could not be undone", &e))),
            }));
        } else if cancel || response.should_close() {
            self.confirming = false;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::run_summary;

    #[test]
    fn a_run_is_summarised_as_1x_did() {
        let (summary, tip) = run_summary(r#"{"individuals":5,"merged":1,"loans":2,"ledger":90,"savings":40,"added":{"savings":[1,2]}}"#);
        assert_eq!(summary, "Ind: 5 | Loans: 2 | Sav: 40 | Ledger: 90");
        assert_eq!(tip, "individuals: 5\nmerged: 1\nloans: 2\nledger: 90\nsavings: 40");
        assert_eq!(run_summary(r#"{"individuals":0}"#).0, "No items imported");
        assert_eq!(run_summary("Started").0, "Started");
    }
}
