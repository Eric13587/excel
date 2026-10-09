//! One tab of the Ledger Grid, for any [`Sheet`].
//!
//! Typing stages; nothing is written until Save Changes. A staged cell is
//! amber, a refused one red with its reason on hover, and every total on the
//! screen reads through the staged figures, so the consequence of an edit is
//! seen before it is committed.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use chrono::{Datelike, NaiveDate};
use eframe::egui::{self, Color32, Key, RichText};
use egui_extras::{Column, TableBuilder};
use loanmaster_core::grid::DeletePlan;
use loanmaster_core::money::{shillings, total};
use loanmaster_core::reports::{Table, Value};
use loanmaster_core::{Journal, dates};

use super::sheet::{Fill, Sheet};
use crate::app::{Action, Notice};
use crate::dialogs::files;
use crate::theme::{self, Palette};
use crate::widgets;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Tone {
    Muted,
    Warning,
    Danger,
    Success,
}

/// A cell by its row's key and its month.
type CellKey = (i64, String);

/// A cell being typed into.
struct Editing {
    key: i64,
    month: String,
    text: String,
    focus: bool,
}

/// A question the page is waiting on.
enum Pending<C> {
    Fill { fill: Fill<C>, scope: String },
    Delete { plan: DeletePlan, changes: Vec<C>, label: String },
    Discard,
    UndoSave,
    /// A locked cell offers the screen that can edit it.
    OpenLedger { member_id: i64, text: String },
}

struct Loaded {
    revision: u64,
    months: Vec<String>,
    flags: (bool, bool, bool),
}

pub struct Page<S: Sheet> {
    pub sheet: S,
    from: NaiveDate,
    to: NaiveDate,
    search: String,
    include_inactive: bool,
    show_excluded: bool,
    include_paid: bool,
    grid: Option<S::Grid>,
    loaded: Option<Loaded>,
    problem: Option<String>,
    blocked: HashMap<i64, String>,
    staged: BTreeMap<CellKey, S::Change>,
    refused: HashMap<CellKey, String>,
    /// Each staged edit and what the cell held before it, for Ctrl+Z.
    history: Vec<(CellKey, Option<S::Change>)>,
    selection: BTreeSet<CellKey>,
    anchor: Option<CellKey>,
    /// Whole months selected by their headers.
    columns: BTreeSet<String>,
    editing: Option<Editing>,
    last_batch: Option<S::Batch>,
    /// Refused changes to re-stage against the journal once it is re-read.
    rebase: bool,
    status: Option<(String, Tone)>,
    pending: Option<Pending<S::Change>>,
}

impl<S: Sheet> Page<S> {
    pub fn new(sheet: S) -> Page<S> {
        let today = chrono::Local::now().date_naive().with_day(1).unwrap_or_default();
        // A loan that is up to date is due next month — the one cell that takes
        // its deduction — so the Loans tab reaches a month further.
        let to = dates::add_months(today, if sheet.is_loans() { 1 } else { 0 });
        Page {
            sheet,
            from: dates::add_months(today, -11),
            to,
            search: String::new(),
            include_inactive: false,
            show_excluded: false,
            include_paid: false,
            grid: None,
            loaded: None,
            problem: None,
            blocked: HashMap::new(),
            staged: BTreeMap::new(),
            refused: HashMap::new(),
            history: Vec::new(),
            selection: BTreeSet::new(),
            anchor: None,
            columns: BTreeSet::new(),
            editing: None,
            last_batch: None,
            rebase: false,
            status: None,
            pending: None,
        }
    }

    #[cfg(test)]
    pub fn set_range(&mut self, from: NaiveDate, to: NaiveDate) {
        self.from = from;
        self.to = to;
    }

    pub fn unsaved(&self) -> usize {
        self.staged.len()
    }

    pub fn discard_all(&mut self) {
        self.staged.clear();
        self.refused.clear();
        self.history.clear();
        self.editing = None;
    }

    fn months(&self) -> Vec<String> {
        let (from, to) = if self.from <= self.to { (self.from, self.to) } else { (self.to, self.from) };
        dates::month_range(&dates::format(from), &dates::format(to))
    }

    fn say(&mut self, text: impl Into<String>, tone: Tone) {
        self.status = Some((text.into(), tone));
    }

    // ------------------------------------------------------------- reading

    fn ensure_loaded(&mut self, journal: &Journal, revision: u64) {
        let months = self.months();
        let flags = (self.include_inactive, self.show_excluded, self.include_paid);
        let current = self.loaded.as_ref().is_some_and(|l| l.revision == revision && l.months == months && l.flags == flags);
        if current {
            return;
        }
        match self.sheet.load(journal, &months, flags.0, flags.1, flags.2) {
            Ok(grid) => {
                let rows: Vec<&S::Row> = self.sheet.rows(&grid).iter().collect();
                self.blocked = self.sheet.blocked(journal, &rows).unwrap_or_default();
                self.grid = Some(grid);
                self.problem = None;
            }
            Err(e) => {
                log::error!("The {} grid could not be read: {e}", self.sheet.label());
                self.problem = Some(e.to_string());
                self.grid = None;
            }
        }
        self.loaded = Some(Loaded { revision, months, flags });
        if self.rebase {
            self.rebase = false;
            self.rebase_staged();
        }
    }

    /// After a save: the refused edits stay, re-read against what the journal
    /// now holds, so a cell refused because something changed under it does
    /// not stay refused for ever.
    fn rebase_staged(&mut self) {
        let Some(grid) = &self.grid else { return };
        let staged = std::mem::take(&mut self.staged);
        let refused = std::mem::take(&mut self.refused);
        for ((key, month), change) in staged {
            let Some(row) = self.sheet.rows(grid).iter().find(|r| self.sheet.key(r) == key) else { continue };
            if self.sheet.refusal(grid, row, &month).is_some() && !self.sheet.is_delete(&change) {
                continue;
            }
            let text = self.sheet.staged_edit_text(&change);
            if let Ok(Some(again)) = self.sheet.stage(grid, row, &month, &text) {
                if let Some(reason) = refused.get(&(key, month.clone())) {
                    self.refused.insert((key, month.clone()), reason.clone());
                }
                self.staged.insert((key, month), again);
            }
        }
    }

    /// The rows shown: filtered by the search box.
    fn visible<'g>(&self, grid: &'g S::Grid) -> Vec<&'g S::Row> {
        let needle = self.search.trim().to_lowercase();
        self.sheet
            .rows(grid)
            .iter()
            .filter(|r| needle.is_empty() || self.sheet.row_label(r).to_lowercase().contains(&needle) || self.sheet.lead_values(r).join(" ").to_lowercase().contains(&needle))
            .collect()
    }

    fn effective(&self, row: &S::Row, month: &str) -> f64 {
        match self.staged.get(&(self.sheet.key(row), month.to_string())) {
            Some(change) => self.sheet.effect(change),
            None => self.sheet.net(row, month),
        }
    }

    // ------------------------------------------------------------- staging

    /// Stage typed text into one cell. False — with the reason in the status
    /// line — when the cell will not take it.
    fn stage_text(&mut self, key: i64, month: &str, text: &str, record: bool) -> bool {
        let Some(grid) = &self.grid else { return false };
        let Some(row) = self.sheet.rows(grid).iter().find(|r| self.sheet.key(r) == key) else { return false };
        let cell = (key, month.to_string());
        if self.sheet.excluded(row) {
            self.status = Some((format!("{} is excluded from this ledger. Right-click the row to include them again.", self.sheet.name(row)), Tone::Danger));
            return false;
        }
        match self.sheet.stage(grid, row, month, text) {
            Err(problem) => {
                self.status = Some((format!("{}, {}: {problem}", self.sheet.row_label(row), dates::month_label(month)), Tone::Danger));
                false
            }
            Ok(change) => {
                if record {
                    self.history.push((cell.clone(), self.staged.get(&cell).cloned()));
                }
                self.refused.remove(&cell);
                match change {
                    Some(change) => {
                        self.staged.insert(cell, change);
                    }
                    None => {
                        self.staged.remove(&cell);
                    }
                }
                true
            }
        }
    }

    fn undo_edit(&mut self) {
        match self.history.pop() {
            Some((cell, previous)) => {
                self.refused.remove(&cell);
                match previous {
                    Some(change) => {
                        self.staged.insert(cell, change);
                    }
                    None => {
                        self.staged.remove(&cell);
                    }
                }
                self.say("Took back the last edit.", Tone::Muted);
            }
            None => self.say("Nothing to take back.", Tone::Muted),
        }
    }

    fn clear_selection(&mut self) {
        let cells: Vec<CellKey> = self.selection.iter().cloned().collect();
        let mut cleared = 0;
        for (key, month) in cells {
            let has_rows = self.grid.as_ref().and_then(|g| self.sheet.rows(g).iter().find(|r| self.sheet.key(r) == key)).is_some_and(|r| self.sheet.has_rows(r, &month));
            if (has_rows || self.staged.contains_key(&(key, month.clone()))) && self.stage_text(key, &month, "", true) {
                cleared += 1;
            }
        }
        if cleared > 0 {
            self.say(format!("{cleared} cell(s) cleared — press Save Changes to write it."), Tone::Warning);
        }
    }

    fn copy(&mut self, ctx: &egui::Context) {
        let Some(grid) = &self.grid else { return };
        let rows = self.visible(grid);
        let months = self.months();
        let mut lines = Vec::new();
        for row in &rows {
            let key = self.sheet.key(row);
            let line: Vec<String> = months
                .iter()
                .filter(|m| self.selection.contains(&(key, (*m).clone())))
                .map(|m| match self.staged.get(&(key, m.clone())) {
                    Some(change) => self.sheet.staged_edit_text(change),
                    None => self.sheet.edit_text(row, m),
                })
                .collect();
            if !line.is_empty() {
                lines.push(line.join("\t"));
            }
        }
        if lines.is_empty() {
            return;
        }
        ctx.copy_text(lines.join("\n"));
        self.say(format!("Copied {} cell(s).", self.selection.len()), Tone::Muted);
    }

    /// Paste a block from the top-left of the selection. What falls past the
    /// edge is dropped, not wrapped, and every cell that would not take its
    /// value is counted in one message.
    fn paste(&mut self, text: &str) {
        let Some(grid) = &self.grid else { return };
        let rows: Vec<i64> = self.visible(grid).iter().map(|r| self.sheet.key(r)).collect();
        let months = self.months();
        let Some((key, month)) = self.anchor.clone().or_else(|| self.selection.iter().next().cloned()) else {
            self.say("Choose a cell to paste into first.", Tone::Warning);
            return;
        };
        let (Some(first_row), Some(first_month)) = (rows.iter().position(|k| *k == key), months.iter().position(|m| *m == month)) else { return };
        let (mut pasted, mut skipped) = (0, 0);
        for (r, line) in text.replace("\r\n", "\n").split('\n').enumerate() {
            if line.trim().is_empty() {
                continue;
            }
            for (c, value) in line.split('\t').enumerate() {
                match (rows.get(first_row + r), months.get(first_month + c)) {
                    (Some(key), Some(month)) => {
                        if self.stage_text(*key, month, value.trim(), true) {
                            pasted += 1;
                        } else {
                            skipped += 1;
                        }
                    }
                    _ => skipped += usize::from(!value.trim().is_empty()),
                }
            }
        }
        let mut note = format!("Pasted {pasted} cell(s).");
        if skipped > 0 {
            note.push_str(&format!(" {skipped} did not fit or could not be set."));
        }
        self.say(note, if skipped > 0 { Tone::Warning } else { Tone::Muted });
    }

    // ------------------------------------------------------- whole-grid work

    fn fill_forward(&mut self, journal: &Journal) {
        let Some(grid) = &self.grid else { return };
        let visible = self.visible(grid);
        let chosen: BTreeSet<i64> = self.selection.iter().map(|(k, _)| *k).collect();
        let targets: Vec<&S::Row> = if chosen.is_empty() { visible } else { visible.into_iter().filter(|r| chosen.contains(&self.sheet.key(r))).collect() };
        let scope = if chosen.is_empty() { format!("every {} shown", self.sheet.per_row()) } else { format!("the selected {}s", self.sheet.per_row()) };
        match self.sheet.fill(journal, grid, &targets, &self.months()) {
            Ok(fill) => self.pending = Some(Pending::Fill { fill, scope }),
            Err(e) => self.say(format!("Fill Forward could not be worked out: {e}"), Tone::Danger),
        }
    }

    fn delete_months(&mut self, journal: &Journal, months: Vec<String>) {
        let Some(grid) = &self.grid else { return };
        let rows: Vec<&S::Row> = self.visible(grid).into_iter().filter(|r| !self.sheet.excluded(r)).collect();
        let mut sorted = months.clone();
        sorted.sort();
        let label = sorted.iter().map(|m| dates::month_label(m)).collect::<Vec<_>>().join(", ");
        match self.sheet.plan_delete(journal, grid, &rows, &sorted) {
            Ok((plan, _)) if plan.is_empty() => self.say(format!("Nothing recorded in {label} for the rows shown."), Tone::Muted),
            Ok((plan, changes)) => self.pending = Some(Pending::Delete { plan, changes, label }),
            Err(e) => self.say(format!("The deletion could not be worked out: {e}"), Tone::Danger),
        }
    }

    fn save(&mut self, journal: &Journal, actions: &mut Vec<Action>) {
        if self.staged.is_empty() {
            return;
        }
        let changes: Vec<S::Change> = self.staged.values().cloned().collect();
        match self.sheet.apply(journal, &changes) {
            Ok(batch) => {
                let summary = self.sheet.summary(&batch);
                let refused = self.sheet.refused(&batch);
                self.staged.clear();
                self.refused.clear();
                self.history.clear();
                for (change, reason) in &refused {
                    let cell = (self.sheet.change_key(change), self.sheet.change_month(change));
                    self.refused.insert(cell.clone(), reason.clone());
                    self.staged.insert(cell, change.clone());
                }
                if self.sheet.applied(&batch) > 0 {
                    self.last_batch = Some(batch);
                }
                self.rebase = !refused.is_empty();
                if refused.is_empty() {
                    self.say(format!("Saved — {summary}."), Tone::Success);
                } else {
                    let first = &refused[0].1;
                    self.say(format!("Saved — {summary}. Hover a red cell for why it was refused (e.g. {first})"), Tone::Danger);
                }
                actions.push(Action::Changed);
            }
            Err(e) => {
                actions.push(Action::Notice(Notice::from_error("Nothing was saved — the journal is unchanged", &e)));
            }
        }
    }

    fn export(&mut self, journal: &Journal, actions: &mut Vec<Action>) {
        let Some(grid) = &self.grid else { return };
        let months = self.months();
        let rows = self.visible(grid);
        if rows.is_empty() {
            actions.push(Action::Notice(Notice::info("Export", "There is nothing to export.")));
            return;
        }
        let mut columns: Vec<String> = self.sheet.lead_columns().iter().map(|(h, _)| h.to_string()).collect();
        columns.extend(months.iter().map(|m| dates::month_label(m)));
        columns.extend(self.sheet.tail_columns().iter().map(|h| h.to_string()));
        let table_rows = rows
            .iter()
            .map(|row| {
                let mut cells: Vec<Value> = self
                    .sheet
                    .lead_values(row)
                    .into_iter()
                    .enumerate()
                    .map(|(i, text)| match self.sheet.lead_number(row, i) {
                        Some(n) => Value::Number(n),
                        None => Value::Text(text),
                    })
                    .collect();
                cells.extend(months.iter().map(|m| Value::Number(self.sheet.net(row, m))));
                cells.push(Value::Number(total(months.iter().map(|m| self.sheet.net(row, m)))));
                cells.push(Value::Number(self.sheet.closing(row)));
                cells
            })
            .collect::<Vec<_>>();
        let mut totals = vec![Value::text("TOTAL")];
        for i in 1..self.sheet.lead_columns().len() {
            totals.push(if self.sheet.lead_columns()[i].1 { Value::Number(total(rows.iter().filter_map(|r| self.sheet.lead_number(r, i)))) } else { Value::Blank });
        }
        for month in &months {
            totals.push(Value::Number(total(rows.iter().map(|r| self.sheet.net(r, month)))));
        }
        totals.push(Value::Number(total(rows.iter().map(|r| total(months.iter().map(|m| self.sheet.net(r, m)))))));
        totals.push(Value::Number(total(rows.iter().map(|r| self.sheet.closing(r)))));
        let title = format!("{} Grid", self.sheet.label());
        let table = Table {
            subtitle: format!("{} to {}", dates::month_label(months.first().map(String::as_str).unwrap_or("")), dates::month_label(months.last().map(String::as_str).unwrap_or(""))),
            sheet: title.chars().take(31).collect(),
            title,
            columns,
            rows: table_rows,
            total: Some(totals),
        };
        let name = format!("{}_Grid_{}.xlsx", self.sheet.label().replace(" / ", "_").replace(' ', "_"), chrono::Local::now().format("%Y%m%d"));
        let Some(path) = files::save_as("Export Grid", &name, &[("Excel workbook", "xlsx"), ("CSV file", "csv"), ("PDF document", "pdf")]) else { return };
        match loanmaster_reports::write_table(&table, &loanmaster_reports::Branding::of(journal), &path) {
            Ok(()) => {
                if !self.staged.is_empty() {
                    actions.push(Action::Notice(Notice::info("Export", "The export shows the saved figures; unsaved changes are not in it.")));
                }
                files::saved(actions, "The grid", &path);
            }
            Err(e) => actions.push(Action::Notice(Notice::warning("The grid could not be exported", e.to_string()))),
        }
    }

    fn set_excluded(&mut self, journal: &Journal, keys: Vec<(i64, String)>, excluded: bool, actions: &mut Vec<Action>) {
        let scope = self.sheet.scope();
        let result = journal.transaction(|| {
            for (member, _) in &keys {
                journal.members().set_excluded(*member, scope, excluded, "")?;
            }
            Ok(())
        });
        match result {
            Ok(()) => {
                let names = keys.iter().map(|(_, n)| n.as_str()).collect::<Vec<_>>().join(", ");
                let note = if excluded && !self.show_excluded {
                    format!("{names} excluded from {} — tick “Show excluded” to see them.", self.sheet.label())
                } else {
                    format!("{names} {} {}.", if excluded { "excluded from" } else { "included in" }, self.sheet.label())
                };
                self.say(note, if excluded { Tone::Warning } else { Tone::Success });
                actions.push(Action::Changed);
            }
            Err(e) => actions.push(Action::Notice(Notice::from_error("The change could not be saved", &e))),
        }
    }

    // ---------------------------------------------------------------- drawing

    pub fn show(&mut self, ui: &mut egui::Ui, journal: &Journal, revision: u64, actions: &mut Vec<Action>) {
        self.ensure_loaded(journal, revision);
        let p = theme::palette(ui.ctx());
        let ctx = ui.ctx().clone();
        // While a cell is selected the arrow keys, Tab and Enter belong to the
        // grid: egui would otherwise walk its focus through the buttons, and
        // Enter would press whichever one it landed on.
        let editing = self.editing.is_some();
        if self.anchor.is_some() && !editing && self.pending.is_none() && !ctx.egui_wants_keyboard_input() {
            ctx.memory_mut(|m| {
                m.move_focus(egui::FocusDirection::None);
                if let Some(id) = m.focused() {
                    m.surrender_focus(id);
                }
            });
        }
        egui::Panel::top(egui::Id::new(("grid_controls", self.sheet.label()))).frame(egui::Frame::new().inner_margin(egui::Margin::symmetric(4, 6))).show(ui, |ui| {
            self.controls(ui, journal, revision);
        });
        egui::Panel::bottom(egui::Id::new(("grid_footer", self.sheet.label()))).frame(egui::Frame::new().inner_margin(egui::Margin::symmetric(4, 6))).show(ui, |ui| {
            self.footer(ui, journal, actions);
        });
        egui::CentralPanel::default().frame(egui::Frame::new().fill(p.card).stroke(egui::Stroke::new(1.0, p.border)).corner_radius(8).inner_margin(egui::Margin::same(4))).show(ui, |ui| {
            if let Some(problem) = &self.problem {
                ui.colored_label(p.danger, format!("The grid could not be read: {problem}"));
                return;
            }
            self.table(ui, journal, actions);
        });
        // The keys that closed an edit this frame are the edit's, not the grid's.
        if !editing {
            self.keyboard(&ctx, journal);
        }
        self.pending(&ctx, journal, actions);
    }

    fn controls(&mut self, ui: &mut egui::Ui, journal: &Journal, revision: u64) {
        let locked = !self.staged.is_empty();
        let why = "Save or discard your changes first.";
        ui.horizontal_wrapped(|ui| {
            ui.add_enabled_ui(!locked, |ui| {
                ui.label("From");
                widgets::date(ui, &format!("{}_from", self.sheet.label()), &mut self.from);
                ui.label("to");
                widgets::date(ui, &format!("{}_to", self.sheet.label()), &mut self.to);
            })
            .response
            .on_disabled_hover_text(why);
            self.from = self.from.with_day(1).unwrap_or(self.from);
            self.to = self.to.with_day(1).unwrap_or(self.to);
            ui.label(RichText::new(format!("{} month(s)", self.months().len())).small().color(theme::palette(ui.ctx()).muted));
            ui.add(egui::TextEdit::singleline(&mut self.search).hint_text("Filter members…").desired_width(200.0));
            ui.add_enabled(!locked, egui::Checkbox::new(&mut self.include_inactive, "Include past members"))
                .on_hover_text("Members who have retired, resigned or been terminated. They can still have withdrawals recorded, but not new contributions.")
                .on_disabled_hover_text(why);
            ui.add_enabled(!locked, egui::Checkbox::new(&mut self.show_excluded, "Show excluded"))
                .on_hover_text("Members deliberately left out of this ledger. Excluding is a display decision: it hides nobody's history and deletes nothing.")
                .on_disabled_hover_text(why);
            if self.sheet.is_loans() {
                ui.add_enabled(!locked, egui::Checkbox::new(&mut self.include_paid, "Include paid loans"))
                    .on_hover_text("Loans already repaid or bought off, shown so their deductions can be corrected — deleting one reopens the loan.")
                    .on_disabled_hover_text(why);
            }
            if ui.add_enabled(!locked, egui::Button::new("Reload")).on_hover_text("Read the journal again").on_disabled_hover_text(why).clicked() {
                self.loaded = None;
                self.ensure_loaded(journal, revision);
                self.say("The grid was read again.", Tone::Muted);
            }
        });
    }

    fn footer(&mut self, ui: &mut egui::Ui, journal: &Journal, actions: &mut Vec<Action>) {
        let p = theme::palette(ui.ctx());
        let (text, tone) = self.status.clone().unwrap_or_else(|| self.summary_line());
        let colour = match tone {
            Tone::Muted => p.muted,
            Tone::Warning => p.warning,
            Tone::Danger => p.danger,
            Tone::Success => p.success,
        };
        ui.label(RichText::new(text).color(colour));
        ui.add_space(4.0);
        ui.horizontal(|ui| {
            if ui.button("Fill Forward…").on_hover_text(format!("Propose each {}'s usual {} for every month in range that has none — staged for review, not written.", self.sheet.per_row(), self.sheet.noun())).clicked() {
                self.fill_forward(journal);
            }
            if ui.button("Export…").on_hover_text("Save the grid as shown to Excel, CSV or PDF").clicked() {
                self.export(journal, actions);
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let staged = !self.staged.is_empty();
                if widgets::solid_enabled(ui, staged, "Save Changes", p.success, "Nothing to save.").clicked() {
                    self.save(journal, actions);
                }
                if ui.add_enabled(staged, egui::Button::new("Discard Changes")).clicked() {
                    self.pending = Some(Pending::Discard);
                }
                if ui.add_enabled(self.last_batch.is_some(), egui::Button::new("Undo Last Save")).clicked() {
                    self.pending = Some(Pending::UndoSave);
                }
            });
        });
    }

    fn summary_line(&self) -> (String, Tone) {
        if !self.staged.is_empty() {
            let mut text = format!("{} unsaved change(s).", self.staged.len());
            if !self.refused.is_empty() {
                text.push_str(&format!(" {} refused — hover a red cell for the reason.", self.refused.len()));
            }
            return (text, if self.refused.is_empty() { Tone::Warning } else { Tone::Danger });
        }
        let Some(grid) = &self.grid else { return ("Nothing to show.".into(), Tone::Muted) };
        let rows = self.visible(grid);
        let months = self.months();
        let moved = total(rows.iter().map(|r| total(months.iter().map(|m| self.sheet.net(r, m)))));
        (
            format!(
                "{} {}(s) · {} month(s) · {} in this period. Type into a cell to stage a change; press F1 for help.",
                rows.len(),
                self.sheet.per_row(),
                months.len(),
                shillings(moved)
            ),
            Tone::Muted,
        )
    }

    fn table(&mut self, ui: &mut egui::Ui, journal: &Journal, actions: &mut Vec<Action>) {
        let Some(grid) = self.grid.take() else { return };
        let p = theme::palette(ui.ctx());
        let months = self.months();
        let rows = self.visible(&grid);
        let lead = self.sheet.lead_columns();
        let mut clicks: Vec<CellClick> = Vec::new();
        egui::ScrollArea::horizontal().id_salt(("grid_scroll", self.sheet.label())).show(ui, |ui| {
            let mut builder = TableBuilder::new(ui)
                .id_salt(("grid", self.sheet.label()))
                .striped(true)
                .cell_layout(egui::Layout::left_to_right(egui::Align::Center))
                .column(Column::exact(210.0))
                .column(Column::exact(84.0))
                .column(Column::exact(96.0));
            for _ in &months {
                builder = builder.column(Column::exact(96.0));
            }
            builder = builder.column(Column::exact(110.0)).column(Column::exact(120.0));
            builder
                .header(26.0, |mut header| {
                    for (title, numeric) in lead {
                        header.col(|ui| {
                            let text = RichText::new(*title).strong().color(p.muted);
                            if *numeric { widgets::right(ui, text) } else { ui.label(text); }
                        });
                    }
                    for month in &months {
                        header.col(|ui| {
                            let selected = self.columns.contains(month);
                            if selected {
                                widgets::fill_cell(ui, p.selected);
                            }
                            let response = ui
                                .with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                    ui.add(egui::Label::new(RichText::new(dates::month_label(month)).strong().color(if selected { p.accent } else { p.muted })).sense(egui::Sense::click()))
                                })
                                .inner;
                            let response = response.on_hover_text("Click to select the month; right-click to delete it for the rows shown");
                            if response.clicked() {
                                clicks.push(CellClick::Column(month.clone(), ui.input(|i| i.modifiers.command)));
                            }
                            response.context_menu(|ui| {
                                let chosen: Vec<String> = if self.columns.contains(month) { self.columns.iter().cloned().collect() } else { vec![month.clone()] };
                                let what = if chosen.len() == 1 { dates::month_label(month) } else { format!("these {} months", chosen.len()) };
                                if ui.button(format!("Delete {what} for the {} {}(s) shown…", rows.len(), self.sheet.per_row())).clicked() {
                                    clicks.push(CellClick::DeleteMonths(chosen));
                                }
                            });
                        });
                    }
                    for title in self.sheet.tail_columns() {
                        header.col(|ui| widgets::right(ui, RichText::new(title).strong().color(p.muted)));
                    }
                })
                .body(|body| {
                    body.rows(26.0, rows.len() + 1, |mut table_row| {
                        let index = table_row.index();
                        if index == rows.len() {
                            self.totals_row(&mut table_row, &rows, &months, p);
                            return;
                        }
                        let row = rows[index];
                        let key = self.sheet.key(row);
                        let note = self.sheet.row_note(row, &self.blocked);
                        let values = self.sheet.lead_values(row);
                        for (i, value) in values.iter().enumerate() {
                            table_row.col(|ui| {
                                let mut text = RichText::new(value);
                                if i == 0 {
                                    text = text.strong();
                                }
                                if note.is_some() {
                                    text = text.color(p.muted);
                                }
                                let response = if lead[i].1 {
                                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| ui.add(egui::Label::new(text).sense(egui::Sense::click()))).inner
                                } else {
                                    ui.add(egui::Label::new(text).truncate().sense(egui::Sense::click()))
                                };
                                let response = match &note {
                                    Some(note) => response.on_hover_text(note),
                                    None => response,
                                };
                                if response.double_clicked() {
                                    actions.push(Action::OpenMember(self.sheet.member_id(row)));
                                }
                                response.context_menu(|ui| self.row_menu(ui, row, None, &mut clicks, actions));
                            });
                        }
                        for month in &months {
                            table_row.col(|ui| self.cell(ui, &grid, row, key, month, p, &mut clicks, actions));
                        }
                        let period = total(months.iter().map(|m| self.effective(row, m)));
                        table_row.col(|ui| widgets::right(ui, RichText::new(widgets::money_or_dash(period)).color(p.muted)));
                        table_row.col(|ui| widgets::right(ui, RichText::new(shillings(self.sheet.closing(row))).color(p.muted)));
                    });
                });
        });
        self.grid = Some(grid);
        for click in clicks {
            self.handle(click, journal, actions);
        }
    }

    fn totals_row(&self, table_row: &mut egui_extras::TableRow<'_, '_>, rows: &[&S::Row], months: &[String], p: &Palette) {
        let lead = self.sheet.lead_columns();
        for (i, (_, numeric)) in lead.iter().enumerate() {
            table_row.col(|ui| {
                widgets::fill_cell(ui, p.background);
                if i == 0 {
                    ui.label(RichText::new("TOTAL").strong());
                } else if *numeric {
                    widgets::right(ui, RichText::new(shillings(total(rows.iter().filter_map(|r| self.sheet.lead_number(r, i))))).strong());
                }
            });
        }
        for month in months {
            table_row.col(|ui| {
                widgets::fill_cell(ui, p.background);
                widgets::right(ui, RichText::new(shillings(total(rows.iter().map(|r| self.effective(r, month))))).strong());
            });
        }
        table_row.col(|ui| {
            widgets::fill_cell(ui, p.background);
            let period = total(rows.iter().map(|r| total(months.iter().map(|m| self.effective(r, m)))));
            widgets::right(ui, RichText::new(shillings(period)).strong());
        });
        table_row.col(|ui| {
            widgets::fill_cell(ui, p.background);
            widgets::right(ui, RichText::new(shillings(total(rows.iter().map(|r| self.sheet.closing(r))))).strong());
        });
    }

    #[allow(clippy::too_many_arguments)]
    fn cell(&mut self, ui: &mut egui::Ui, grid: &S::Grid, row: &S::Row, key: i64, month: &str, p: &Palette, clicks: &mut Vec<CellClick>, actions: &mut Vec<Action>) {
        let id = (key, month.to_string());
        if let Some(editing) = self.editing.as_mut().filter(|e| e.key == key && e.month == month) {
            let response = ui.add(egui::TextEdit::singleline(&mut editing.text).desired_width(f32::INFINITY).horizontal_align(egui::Align::RIGHT));
            if editing.focus {
                response.request_focus();
                editing.focus = false;
            }
            let (enter, tab, escape) = ui.input(|i| (i.key_pressed(Key::Enter), i.key_pressed(Key::Tab), i.key_pressed(Key::Escape)));
            if escape {
                clicks.push(CellClick::CancelEdit);
            } else if response.lost_focus() || enter || tab {
                clicks.push(CellClick::Commit { key, month: month.to_string(), text: editing.text.clone(), next: if tab { Move::Right } else if enter { Move::Down } else { Move::Stay } });
            }
            return;
        }
        let staged = self.staged.get(&id);
        let refusal = if self.sheet.excluded(row) {
            Some(format!("{} is excluded from this ledger. Right-click the row to include them again.", self.sheet.name(row)))
        } else {
            self.sheet.refusal(grid, row, month)
        };
        let note = self.sheet.cell_note(row, month);
        let mut text = match staged {
            Some(change) => self.sheet.staged_text(change).unwrap_or_else(|| if self.sheet.is_delete(change) { "—".into() } else { shillings(self.sheet.effect(change)) }),
            None => self.sheet.display_text(row, month).unwrap_or_else(|| {
                if self.sheet.has_rows(row, month) { shillings(self.sheet.net(row, month)) } else { "—".into() }
            }),
        };
        let (fill, colour, tip): (Option<Color32>, Color32, Option<String>) = if let Some(reason) = self.refused.get(&id) {
            (Some(p.danger_bg), p.danger, Some(reason.clone()))
        } else if let Some(change) = staged {
            let others: Vec<&S::Change> = self.staged.values().collect();
            (Some(p.warning_bg), p.warning, Some(format!("{}\nUnsaved — press Save Changes to write it.", self.sheet.describe(Some(row), change, &others))))
        } else if let Some(reason) = &refusal {
            if self.sheet.has_rows(row, month) {
                text.push_str(" ·");
            }
            (None, p.muted, Some(reason.clone()))
        } else if let Some(note) = &note {
            (None, p.danger, Some(note.clone()))
        } else {
            (None, p.text, self.sheet.rows_tooltip(row, month))
        };
        let selected = self.selection.contains(&id) || self.columns.contains(month);
        if selected {
            widgets::fill_cell(ui, p.accent.gamma_multiply(0.18));
        } else if let Some(fill) = fill {
            widgets::fill_cell(ui, fill);
        }
        let mut rich = RichText::new(text).color(colour);
        if note.is_some() && staged.is_none() {
            rich = rich.strong();
        }
        let response = ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| ui.add(egui::Label::new(rich).selectable(false).sense(egui::Sense::click()))).inner;
        let response = ui.interact(ui.max_rect(), ui.id().with(("cell", key, month)), egui::Sense::click()).union(response);
        // Named for screen readers (and the tests): "ANN WANJIRU, Oct 2026".
        response.widget_info(|| egui::WidgetInfo::labeled(egui::WidgetType::Other, true, format!("{}, {}", self.sheet.row_label(row), dates::month_label(month))));
        let response = match tip {
            Some(tip) => response.on_hover_text(tip),
            None => response,
        };
        if response.double_clicked() {
            if refusal.is_some() && self.sheet.has_rows(row, month) {
                let name = self.sheet.name(row);
                let text = format!("{} for {name}: {}\n\nOpen {name}'s ledger to work on it?", dates::month_label(month), refusal.clone().unwrap_or_default());
                clicks.push(CellClick::OpenLedger(self.sheet.member_id(row), text));
            } else if refusal.is_none() {
                clicks.push(CellClick::Edit(key, month.to_string(), None));
            }
        } else if response.clicked() {
            let modifiers = ui.input(|i| i.modifiers);
            clicks.push(CellClick::Select(key, month.to_string(), modifiers.command, modifiers.shift));
        }
        if refusal.is_some() && response.clicked() && !self.sheet.has_rows(row, month)
            && let Some(reason) = &refusal
        {
            clicks.push(CellClick::Explain(format!("{}, {}: {reason}", self.sheet.row_label(row), dates::month_label(month))));
        }
        response.context_menu(|ui| self.row_menu(ui, row, Some(month), clicks, actions));
    }

    fn row_menu(&self, ui: &mut egui::Ui, row: &S::Row, month: Option<&str>, clicks: &mut Vec<CellClick>, actions: &mut Vec<Action>) {
        if ui.button(format!("Open {}'s ledger", self.sheet.name(row))).clicked() {
            actions.push(Action::OpenMember(self.sheet.member_id(row)));
        }
        if !self.selection.is_empty() && ui.button("Clear selected cells").clicked() {
            clicks.push(CellClick::Clear);
        }
        if let Some(month) = month
            && ui.button(format!("Delete {} for everyone shown…", dates::month_label(month))).clicked()
        {
            clicks.push(CellClick::DeleteMonths(vec![month.to_string()]));
        }
        ui.separator();
        let excluded = self.sheet.excluded(row);
        let selected = self.selected_members().len();
        let who = if selected > 1 { format!("{selected} selected members") } else { self.sheet.name(row).to_string() };
        let label = if excluded { format!("Include {who} in {}", self.sheet.label()) } else { format!("Exclude {who} from {}", self.sheet.label()) };
        if ui.button(label).on_hover_text("A display decision: it hides nobody's history and deletes nothing, but Fill Forward stops proposing entries for them.").clicked() {
            clicks.push(CellClick::Exclude(self.sheet.member_id(row), self.sheet.name(row).to_string(), !excluded));
        }
    }

    fn handle(&mut self, click: CellClick, journal: &Journal, actions: &mut Vec<Action>) {
        match click {
            CellClick::Select(key, month, add, extend) => {
                self.columns.clear();
                let cell = (key, month);
                if extend && let Some(anchor) = self.anchor.clone() {
                    self.select_range(&anchor, &cell);
                } else if add {
                    if !self.selection.remove(&cell) {
                        self.selection.insert(cell.clone());
                    }
                    self.anchor = Some(cell);
                } else {
                    self.selection = BTreeSet::from([cell.clone()]);
                    self.anchor = Some(cell);
                }
            }
            CellClick::Column(month, add) => {
                self.selection.clear();
                if add {
                    if !self.columns.remove(&month) {
                        self.columns.insert(month);
                    }
                } else {
                    self.columns = BTreeSet::from([month]);
                }
            }
            CellClick::Edit(key, month, typed) => self.start_edit(key, month, typed),
            CellClick::CancelEdit => self.editing = None,
            CellClick::Commit { key, month, text, next } => {
                self.editing = None;
                if self.stage_text(key, &month, &text, true) {
                    self.status = None;
                }
                self.step(next);
            }
            CellClick::Clear => self.clear_selection(),
            CellClick::DeleteMonths(months) => self.delete_months(journal, months),
            CellClick::OpenLedger(member_id, text) => self.pending = Some(Pending::OpenLedger { member_id, text }),
            CellClick::Explain(text) => self.say(text, Tone::Muted),
            CellClick::Exclude(member, name, excluded) => {
                let mut members = self.selected_members();
                if members.is_empty() {
                    members.push((member, name));
                }
                self.set_excluded(journal, members, excluded, actions);
            }
        }
    }

    /// The members behind the selected cells, once each, in grid order.
    fn selected_members(&self) -> Vec<(i64, String)> {
        let Some(grid) = &self.grid else { return Vec::new() };
        let keys: BTreeSet<i64> = self.selection.iter().map(|(k, _)| *k).collect();
        let mut members: Vec<(i64, String)> = Vec::new();
        for row in self.sheet.rows(grid).iter().filter(|r| keys.contains(&self.sheet.key(r))) {
            let entry = (self.sheet.member_id(row), self.sheet.name(row).to_string());
            if !members.contains(&entry) {
                members.push(entry);
            }
        }
        members
    }

    fn start_edit(&mut self, key: i64, month: String, typed: Option<String>) {
        let Some(grid) = &self.grid else { return };
        let Some(row) = self.sheet.rows(grid).iter().find(|r| self.sheet.key(r) == key) else { return };
        if self.sheet.excluded(row) || self.sheet.refusal(grid, row, &month).is_some() {
            let reason = self.sheet.refusal(grid, row, &month).unwrap_or_else(|| format!("{} is excluded from this ledger.", self.sheet.name(row)));
            self.say(format!("{}, {}: {reason}", self.sheet.row_label(row), dates::month_label(&month)), Tone::Danger);
            return;
        }
        let text = typed.unwrap_or_else(|| match self.staged.get(&(key, month.clone())) {
            Some(change) => self.sheet.staged_edit_text(change),
            None => self.sheet.edit_text(row, &month),
        });
        self.selection = BTreeSet::from([(key, month.clone())]);
        self.anchor = Some((key, month.clone()));
        self.editing = Some(Editing { key, month, text, focus: true });
    }

    fn select_range(&mut self, from: &CellKey, to: &CellKey) {
        let Some(grid) = &self.grid else { return };
        let keys: Vec<i64> = self.visible(grid).iter().map(|r| self.sheet.key(r)).collect();
        let months = self.months();
        let (Some(r1), Some(r2), Some(c1), Some(c2)) = (
            keys.iter().position(|k| *k == from.0),
            keys.iter().position(|k| *k == to.0),
            months.iter().position(|m| *m == from.1),
            months.iter().position(|m| *m == to.1),
        ) else {
            return;
        };
        self.selection = (r1.min(r2)..=r1.max(r2)).flat_map(|r| (c1.min(c2)..=c1.max(c2)).map(move |c| (r, c))).map(|(r, c)| (keys[r], months[c].clone())).collect();
    }

    /// Move the selection one cell.
    fn step(&mut self, direction: Move) {
        let Some((key, month)) = self.anchor.clone() else { return };
        let Some(grid) = &self.grid else { return };
        let keys: Vec<i64> = self.visible(grid).iter().map(|r| self.sheet.key(r)).collect();
        let months = self.months();
        let (Some(r), Some(c)) = (keys.iter().position(|k| *k == key), months.iter().position(|m| *m == month)) else { return };
        let (r, c) = match direction {
            Move::Stay => (r, c),
            Move::Up => (r.saturating_sub(1), c),
            Move::Down => ((r + 1).min(keys.len().saturating_sub(1)), c),
            Move::Left => (r, c.saturating_sub(1)),
            Move::Right => (r, (c + 1).min(months.len().saturating_sub(1))),
        };
        let cell = (keys[r], months[c].clone());
        self.selection = BTreeSet::from([cell.clone()]);
        self.anchor = Some(cell);
    }

    fn keyboard(&mut self, ctx: &egui::Context, journal: &Journal) {
        if self.editing.is_some() || self.pending.is_some() || ctx.egui_wants_keyboard_input() {
            return;
        }
        let shortcut = |key: Key| ctx.input_mut(|i| i.consume_shortcut(&egui::KeyboardShortcut::new(egui::Modifiers::COMMAND, key)));
        if shortcut(Key::Z) {
            self.undo_edit();
        }
        let events = ctx.input(|i| i.events.clone());
        for event in events {
            match event {
                egui::Event::Copy => self.copy(ctx),
                egui::Event::Paste(text) => self.paste(&text),
                egui::Event::Text(text) if !text.trim().is_empty() => {
                    if let Some((key, month)) = self.anchor.clone() {
                        self.start_edit(key, month, Some(text));
                        break;
                    }
                }
                egui::Event::Key { key, pressed: true, modifiers, .. } => match key {
                    Key::ArrowUp => self.step(Move::Up),
                    Key::ArrowDown => self.step(Move::Down),
                    Key::ArrowLeft => self.step(Move::Left),
                    Key::ArrowRight | Key::Tab => self.step(Move::Right),
                    Key::Enter | Key::F2 => {
                        if let Some((key, month)) = self.anchor.clone() {
                            self.start_edit(key, month, None);
                        }
                    }
                    Key::Delete | Key::Backspace if !modifiers.command => {
                        if !self.columns.is_empty() {
                            let months: Vec<String> = self.columns.iter().cloned().collect();
                            self.delete_months(journal, months);
                        } else {
                            self.clear_selection();
                        }
                    }
                    Key::Escape => {
                        self.selection.clear();
                        self.columns.clear();
                    }
                    _ => {}
                },
                _ => {}
            }
        }
    }

    fn pending(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) {
        let Some(pending) = self.pending.take() else { return };
        let p = theme::palette(ctx);
        let mut keep = true;
        let mut go = false;
        let response = egui::Modal::new(egui::Id::new(("grid_pending", self.sheet.label()))).show(ctx, |ui| {
            ui.set_max_width(560.0);
            match &pending {
                Pending::Fill { fill, scope } => {
                    ui.heading("Fill Forward");
                    if fill.changes.is_empty() {
                        ui.label(format!("Nothing to fill for {scope}."));
                        for reason in fill.skipped.iter().take(8) {
                            ui.label(RichText::new(format!("• {reason}")).color(p.muted));
                        }
                        for note in &fill.notes {
                            ui.label(RichText::new(format!("• {note}")).color(p.muted));
                        }
                    } else {
                        ui.label(format!(
                            "Propose {} entr{} across {} {}(s), using each one's usual {}?",
                            fill.changes.len(),
                            if fill.changes.len() == 1 { "y" } else { "ies" },
                            fill.rows,
                            self.sheet.per_row(),
                            self.sheet.noun()
                        ));
                        ui.add_space(4.0);
                        ui.label(
                            RichText::new(format!(
                                "Only months that were missed are filled — never one still to come, and never one that already has its {}. Nothing is written until you press Save Changes.",
                                self.sheet.noun()
                            ))
                            .color(p.muted),
                        );
                        for note in &fill.notes {
                            ui.label(RichText::new(format!("• {note}")).color(p.muted));
                        }
                        if let Some(first) = fill.skipped.first() {
                            ui.label(RichText::new(format!("• {} skipped — e.g. {first}", fill.skipped.len())).color(p.muted));
                        }
                    }
                }
                Pending::Delete { plan, label, .. } => {
                    ui.heading("Delete Months");
                    ui.label(format!("Delete {label} in {} for the rows shown?", self.sheet.label()));
                    ui.add_space(4.0);
                    egui::ScrollArea::vertical().max_height(360.0).show(ui, |ui| {
                        if !plan.tallies.is_empty() {
                            ui.label(RichText::new(format!("Deletes, across {} {}(s):", plan.rows_affected(), self.sheet.per_row())).strong());
                            for (name, (count, amount)) in &plan.tallies {
                                ui.label(format!("• {count} {name} — {}", shillings(*amount)));
                            }
                        }
                        if !plan.consequences.is_empty() {
                            ui.add_space(4.0);
                            ui.label(RichText::new("What follows:").strong());
                            for line in &plan.consequences {
                                ui.label(format!("• {line}"));
                            }
                        }
                        if !plan.untouched.is_empty() {
                            ui.add_space(4.0);
                            ui.label(RichText::new("Left alone:").strong());
                            for line in &plan.untouched {
                                ui.label(RichText::new(format!("• {line}")).color(p.muted));
                            }
                        }
                        if !plan.refused.is_empty() {
                            ui.add_space(4.0);
                            ui.label(RichText::new("Refused — these keep their transactions:").strong().color(p.danger));
                            for (row, reason) in &plan.refused {
                                ui.label(RichText::new(format!("• {row}: {reason}")).color(p.danger));
                            }
                        }
                    });
                    ui.add_space(4.0);
                    ui.label(RichText::new("Nothing is written until you press Save Changes, and Undo Last Save reverses it.").small().color(p.muted));
                }
                Pending::Discard => {
                    ui.heading("Discard Changes");
                    ui.label(format!("Throw away the {} unsaved change(s) in {}?", self.staged.len(), self.sheet.label()));
                }
                Pending::UndoSave => {
                    ui.heading("Undo Last Save");
                    ui.label("Put back what the last save changed, exactly — including anything it deleted?");
                }
                Pending::OpenLedger { text, .. } => {
                    ui.heading("Edit in the Ledger");
                    ui.label(text);
                }
            }
            crate::dialogs::footer(ui, |ui| {
                let (label, enabled, colour) = match &pending {
                    Pending::Fill { fill, .. } => ("Stage Them", !fill.changes.is_empty(), p.accent),
                    Pending::Delete { changes, .. } => ("Stage Deletions", !changes.is_empty(), p.danger),
                    Pending::Discard => ("Discard", true, p.danger),
                    Pending::UndoSave => ("Undo Save", true, p.warning),
                    Pending::OpenLedger { .. } => ("Open Ledger", true, p.accent),
                };
                if enabled && widgets::solid(ui, label, colour).clicked() {
                    go = true;
                    keep = false;
                }
                if ui.button(if enabled { "Cancel" } else { "Close" }).clicked() {
                    keep = false;
                }
            });
        });
        if response.should_close() {
            keep = false;
        }
        if go {
            match pending {
                Pending::Fill { fill, .. } => {
                    let count = fill.changes.len();
                    for change in fill.changes {
                        let cell = (self.sheet.change_key(&change), self.sheet.change_month(&change));
                        self.refused.remove(&cell);
                        self.staged.insert(cell, change);
                    }
                    self.history.clear();
                    self.say(format!("{count} entr{} staged — press Save Changes to write them.", if count == 1 { "y" } else { "ies" }), Tone::Warning);
                }
                Pending::Delete { plan, changes, label } => {
                    let months: BTreeSet<&String> = plan.months.iter().collect();
                    if let Some(grid) = &self.grid {
                        let keys: Vec<i64> = self.visible(grid).iter().map(|r| self.sheet.key(r)).collect();
                        self.staged.retain(|(k, m), _| !(keys.contains(k) && months.contains(m)));
                        self.refused.retain(|(k, m), _| !(keys.contains(k) && months.contains(m)));
                    }
                    let count = changes.len();
                    for change in changes {
                        let cell = (self.sheet.change_key(&change), self.sheet.change_month(&change));
                        self.staged.insert(cell, change);
                    }
                    self.history.clear();
                    self.columns.clear();
                    let mut note = format!("{count} deletion(s) staged in {label} — press Save Changes to write them.");
                    if !plan.refused.is_empty() {
                        note.push_str(&format!(" {} refused; see the preview for why.", plan.refused.len()));
                    }
                    self.say(note, Tone::Warning);
                }
                Pending::Discard => {
                    self.discard_all();
                    self.say("Changes discarded.", Tone::Muted);
                }
                Pending::OpenLedger { member_id, .. } => actions.push(Action::OpenMember(member_id)),
                Pending::UndoSave => {
                    if let Some(batch) = self.last_batch.take() {
                        match self.sheet.revert(journal, &batch) {
                            Ok(()) => {
                                self.say("The last save was undone.", Tone::Warning);
                                actions.push(Action::Changed);
                            }
                            Err(e) => {
                                self.last_batch = Some(batch);
                                actions.push(Action::Notice(Notice::from_error("The save could not be undone", &e)));
                            }
                        }
                    }
                }
            }
        } else if keep {
            self.pending = Some(pending);
        }
    }
}

#[derive(Clone, Copy)]
enum Move {
    Stay,
    Up,
    Down,
    Left,
    Right,
}

/// What the user did to the table this frame, handled once it is drawn.
enum CellClick {
    Select(i64, String, bool, bool),
    Column(String, bool),
    Edit(i64, String, Option<String>),
    CancelEdit,
    Commit { key: i64, month: String, text: String, next: Move },
    Clear,
    DeleteMonths(Vec<String>),
    OpenLedger(i64, String),
    Explain(String),
    Exclude(i64, String, bool),
}

