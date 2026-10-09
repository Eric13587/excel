//! The window: the journal picker until a journal is open, then the
//! workspace with its menus, status bar and dialogs.
//!
//! Screens and dialogs never change the application directly. They queue
//! [`Action`]s while they draw, and the queue is worked through once the
//! frame is drawn — so nothing is borrowed twice, and every change to the
//! journal goes through the same few doors.

use std::path::{Path, PathBuf};

use eframe::egui::{self, Key, KeyboardShortcut, Modifiers};
use loanmaster_core::Error;

use crate::dialogs::Dialog;
use crate::screens::{self, startup};
use crate::theme;
use crate::workspace::{Screen, Workspace};

/// Kept between runs: the journals opened lately and the last theme.
#[derive(Debug, Default, Clone, serde::Serialize, serde::Deserialize)]
pub struct Remembered {
    pub recent: Vec<PathBuf>,
    pub dark: bool,
}

impl Remembered {
    fn remember(&mut self, path: &Path) {
        self.recent.retain(|p| p != path);
        self.recent.insert(0, path.to_path_buf());
        self.recent.truncate(8);
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    Info,
    Warning,
    Error,
}

/// A message the user must acknowledge.
#[derive(Debug, Clone)]
pub struct Notice {
    pub title: String,
    pub body: String,
    pub level: Level,
}

impl Notice {
    pub fn info(title: impl Into<String>, body: impl Into<String>) -> Notice {
        Notice { title: title.into(), body: body.into(), level: Level::Info }
    }

    pub fn warning(title: impl Into<String>, body: impl Into<String>) -> Notice {
        Notice { title: title.into(), body: body.into(), level: Level::Warning }
    }

    /// An error, worded for the user. A refusal is shown as written; a fault
    /// of the machine is logged and said plainly.
    pub fn from_error(what: &str, error: &Error) -> Notice {
        if error.is_user_facing() {
            Notice { title: what.to_string(), body: error.to_string(), level: Level::Warning }
        } else {
            log::error!("{what}: {error}");
            Notice {
                title: what.to_string(),
                body: format!("{error}\n\nNothing was changed. The details are in the log ({}).", crate::logging::log_file().display()),
                level: Level::Error,
            }
        }
    }
}

/// Queue what a finished job says: the status line and a refresh on
/// success, the reason on failure.
pub fn outcome<T>(actions: &mut Vec<Action>, failed: &str, result: loanmaster_core::Result<T>, done: impl FnOnce(T) -> String) {
    match result {
        Ok(value) => {
            actions.push(Action::Status(done(value)));
            actions.push(Action::Changed);
        }
        Err(e) => actions.push(Action::Notice(Notice::from_error(failed, &e))),
    }
}

/// Work to do against the open journal, queued from a screen or a dialog.
pub type Job = Box<dyn FnOnce(&mut Workspace, &mut Vec<Action>)>;

/// Everything that changes what the window shows or what the journal holds.
pub enum Action {
    OpenJournal(PathBuf),
    CloseJournal,
    ShowDashboard,
    OpenMember(i64),
    OpenGrid,
    Dialog(Box<Dialog>),
    Notice(Notice),
    /// A line in the status bar.
    Status(String),
    /// The journal changed: every screen re-reads it.
    Changed,
    Run(Job),
    Undo,
    Redo,
    Quit,
}

impl Action {
    pub fn dialog(dialog: impl Into<Dialog>) -> Action {
        Action::Dialog(Box::new(dialog.into()))
    }

    pub fn run(job: impl FnOnce(&mut Workspace, &mut Vec<Action>) + 'static) -> Action {
        Action::Run(Box::new(job))
    }
}

pub struct LoanMasterApp {
    pub remembered: Remembered,
    pub workspace: Option<Workspace>,
    pub notices: Vec<Notice>,
    actions: Vec<Action>,
    shown_dark: Option<bool>,
    /// Closing the journal or the window, held while the user decides about
    /// the Ledger Grid's unsaved changes.
    leaving: Option<Action>,
}

impl LoanMasterApp {
    pub fn new(cc: &eframe::CreationContext<'_>, journal: Option<PathBuf>) -> LoanMasterApp {
        let remembered: Remembered = cc.storage.and_then(|s| eframe::get_value(s, eframe::APP_KEY)).unwrap_or_default();
        egui_extras::install_image_loaders(&cc.egui_ctx);
        let mut app = LoanMasterApp { remembered, workspace: None, notices: Vec::new(), actions: Vec::new(), shown_dark: None, leaving: None };
        if let Some(path) = journal {
            app.actions.push(Action::OpenJournal(path));
        }
        app
    }

    /// For tests: the app with a journal already open.
    #[cfg(test)]
    pub fn with_workspace(workspace: Workspace) -> LoanMasterApp {
        LoanMasterApp { remembered: Remembered::default(), workspace: Some(workspace), notices: Vec::new(), actions: Vec::new(), shown_dark: None, leaving: None }
    }

    fn dark(&self) -> bool {
        self.workspace.as_ref().map(|w| w.dark).unwrap_or(self.remembered.dark)
    }

    fn shortcuts(&mut self, ctx: &egui::Context) {
        let pressed = |shortcut: KeyboardShortcut| ctx.input_mut(|i| i.consume_shortcut(&shortcut));
        if self.workspace.is_none() {
            return;
        }
        if pressed(KeyboardShortcut::new(Modifiers::CTRL | Modifiers::ALT, Key::Z)) {
            self.actions.push(Action::Undo);
        }
        if pressed(KeyboardShortcut::new(Modifiers::CTRL | Modifiers::ALT, Key::Y)) {
            self.actions.push(Action::Redo);
        }
        if pressed(KeyboardShortcut::new(Modifiers::CTRL, Key::G)) {
            self.actions.push(Action::OpenGrid);
        }
    }

    fn menu_bar(&mut self, ui: &mut egui::Ui) {
        let actions = &mut self.actions;
        let Some(ws) = &self.workspace else { return };
        egui::MenuBar::new().ui(ui, |ui| {
            ui.menu_button("File", |ui| {
                if ui.button("Close Journal").clicked() {
                    actions.push(Action::CloseJournal);
                }
                if ui.add(egui::Button::new("Exit").shortcut_text("Ctrl+Q")).clicked() {
                    actions.push(Action::Quit);
                }
            });
            ui.menu_button("Edit", |ui| {
                let undo = ws.undo.undo_description().map(|d| format!("Undo {d}")).unwrap_or_else(|| "Undo".into());
                if ui.add_enabled(ws.undo.can_undo(), egui::Button::new(undo).shortcut_text("Ctrl+Alt+Z")).clicked() {
                    actions.push(Action::Undo);
                }
                let redo = ws.undo.redo_description().map(|d| format!("Redo {d}")).unwrap_or_else(|| "Redo".into());
                if ui.add_enabled(ws.undo.can_redo(), egui::Button::new(redo).shortcut_text("Ctrl+Alt+Y")).clicked() {
                    actions.push(Action::Redo);
                }
            });
            ui.menu_button("Operations", |ui| {
                if ui.add(egui::Button::new("Ledger Grid (bulk edit)…").shortcut_text("Ctrl+G")).clicked() {
                    actions.push(Action::OpenGrid);
                }
            });
            ui.menu_button("Help", |ui| {
                if ui.button("About LoanMaster").clicked() {
                    actions.push(Action::Notice(Notice::info(
                        "About LoanMaster",
                        format!(
                            "LoanMaster {}\nLoan, savings and fund management for SACCOs.\n\nLog folder:\n{}",
                            env!("CARGO_PKG_VERSION"),
                            crate::logging::log_dir().display()
                        ),
                    )));
                }
                if ui.button("Open Log Folder").clicked() {
                    ui.ctx().open_url(egui::OpenUrl::same_tab(format!("file://{}", crate::logging::log_dir().display())));
                }
            });
        });
    }

    fn status_bar(&mut self, ui: &mut egui::Ui) {
        let Some(ws) = &self.workspace else { return };
        let p = theme::palette(ui.ctx());
        ui.horizontal(|ui| {
            if let Some(status) = &ws.status {
                ui.label(egui::RichText::new(status).color(p.muted));
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let file = ws.path.file_name().map(|f| f.to_string_lossy().to_string()).unwrap_or_default();
                let label = if ws.org_name.is_empty() { file } else { format!("{}  •  {file}", ws.org_name) };
                ui.label(egui::RichText::new(label).color(p.muted)).on_hover_text(ws.path.display().to_string());
                let orphans = ws.journal.report().orphaned_rows;
                if orphans > 0 {
                    ui.label(egui::RichText::new(format!("⚠ {orphans} orphaned rows")).color(p.warning)).on_hover_text(
                        "Rows that refer to a member or loan that no longer exists. They are left untouched and do not \
                         affect anyone's balance; sanitize them in the journal by hand.",
                    );
                }
            });
        });
    }

    fn show_notice(&mut self, ctx: &egui::Context) {
        let Some(notice) = self.notices.first().cloned() else { return };
        let p = theme::palette(ctx);
        let colour = match notice.level {
            Level::Info => p.accent,
            Level::Warning => p.warning,
            Level::Error => p.danger,
        };
        let mut done = false;
        let response = egui::Modal::new(egui::Id::new("notice")).show(ctx, |ui| {
            ui.set_max_width(520.0);
            ui.label(egui::RichText::new(&notice.title).heading().color(colour));
            ui.add_space(6.0);
            ui.label(&notice.body);
            ui.add_space(10.0);
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("OK").clicked() || ui.input(|i| i.key_pressed(Key::Enter)) {
                    done = true;
                }
            });
        });
        if done || response.should_close() {
            self.notices.remove(0);
        }
    }

    /// Ask before closing over the Ledger Grid's unsaved changes.
    fn confirm_leaving(&mut self, ctx: &egui::Context) {
        let Some(action) = &self.leaving else { return };
        let what = if matches!(action, Action::CloseJournal) { "close the journal" } else { "quit" };
        let detail = self.workspace.as_ref().and_then(|ws| ws.unsaved()).unwrap_or_default();
        let p = theme::palette(ctx);
        let (mut discard, mut cancel) = (false, false);
        let response = egui::Modal::new(egui::Id::new("app_leaving")).show(ctx, |ui| {
            ui.set_max_width(480.0);
            ui.heading("Unsaved Changes");
            ui.add_space(4.0);
            ui.label(format!("The Ledger Grid has unsaved changes ({detail}).\n\nDiscard them and {what}?"));
            crate::dialogs::footer(ui, |ui| {
                if crate::widgets::solid(ui, "Discard", p.danger).clicked() {
                    discard = true;
                }
                if ui.button("Cancel").clicked() {
                    cancel = true;
                }
            });
        });
        if discard {
            if let Some(ws) = &mut self.workspace {
                ws.discard_unsaved();
            }
            if let Some(action) = self.leaving.take() {
                self.actions.push(action);
            }
        } else if cancel || response.should_close() {
            self.leaving = None;
        }
    }

    fn open_journal(&mut self, ctx: &egui::Context, path: PathBuf) {
        match Workspace::open(&path) {
            Ok((workspace, warnings)) => {
                self.remembered.remember(&path);
                self.remembered.dark = workspace.dark;
                let title = if workspace.org_name.is_empty() {
                    format!("LoanMaster {} — {}", env!("CARGO_PKG_VERSION"), file_name(&path))
                } else {
                    format!("{} — LoanMaster {} — {}", workspace.org_name, env!("CARGO_PKG_VERSION"), file_name(&path))
                };
                ctx.send_viewport_cmd(egui::ViewportCommand::Title(title));
                self.workspace = Some(workspace);
                self.notices.extend(warnings);
            }
            Err(error) => {
                self.remembered.recent.retain(|p| p != &path || path.exists());
                self.notices.push(Notice::from_error("The journal could not be opened", &error));
            }
        }
    }

    fn process(&mut self, ctx: &egui::Context) {
        while !self.actions.is_empty() {
            let actions: Vec<Action> = std::mem::take(&mut self.actions);
            for action in actions {
                self.apply(ctx, action);
            }
        }
    }

    fn apply(&mut self, ctx: &egui::Context, action: Action) {
        match action {
            Action::OpenJournal(path) => self.open_journal(ctx, path),
            Action::CloseJournal if self.workspace.as_ref().is_some_and(|ws| ws.unsaved().is_some()) => self.leaving = Some(Action::CloseJournal),
            Action::CloseJournal => {
                if let Some(ws) = self.workspace.take() {
                    ws.journal.close();
                }
                ctx.send_viewport_cmd(egui::ViewportCommand::Title("LoanMaster".into()));
            }
            Action::Quit => ctx.send_viewport_cmd(egui::ViewportCommand::Close),
            Action::Notice(notice) => self.notices.push(notice),
            action => {
                let Some(ws) = &mut self.workspace else { return };
                let mut follow = Vec::new();
                match action {
                    Action::ShowDashboard => ws.screen = Screen::Dashboard,
                    Action::OpenMember(id) => ws.open_member(id),
                    Action::OpenGrid => ws.open_grid(),
                    Action::Dialog(dialog) => ws.dialogs.push(*dialog),
                    Action::Status(text) => ws.status = Some(text),
                    Action::Changed => ws.changed(),
                    Action::Run(job) => job(ws, &mut follow),
                    Action::Undo => ws.undo(&mut follow),
                    Action::Redo => ws.redo(&mut follow),
                    _ => {}
                }
                if ws.dark != self.remembered.dark {
                    self.remembered.dark = ws.dark;
                }
                self.actions.extend(follow);
            }
        }
    }
}

fn file_name(path: &Path) -> String {
    path.file_name().map(|f| f.to_string_lossy().to_string()).unwrap_or_default()
}

impl eframe::App for LoanMasterApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        if self.shown_dark != Some(self.dark()) {
            theme::apply(&ctx, self.dark());
            self.shown_dark = Some(self.dark());
        }
        self.shortcuts(&ctx);
        if ctx.input_mut(|i| i.consume_shortcut(&KeyboardShortcut::new(Modifiers::CTRL, Key::Q))) {
            self.actions.push(Action::Quit);
        }
        // The window's own close button, Alt+F4 and Quit all arrive here.
        if ctx.input(|i| i.viewport().close_requested()) && self.workspace.as_ref().is_some_and(|ws| ws.unsaved().is_some()) {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            self.leaving = Some(Action::Quit);
        }

        if self.workspace.is_none() {
            egui::CentralPanel::default().show(ui, |ui| startup::show(ui, &self.remembered, &mut self.actions));
        } else {
            egui::Panel::top("menu").show(ui, |ui| self.menu_bar(ui));
            egui::Panel::bottom("status").show(ui, |ui| self.status_bar(ui));
            if let Some(ws) = &mut self.workspace {
                screens::show(ui, ws, &mut self.actions);
                ws.show_dialogs(&ctx, &mut self.actions);
            }
        }
        self.confirm_leaving(&ctx);
        self.show_notice(&ctx);
        self.process(&ctx);
    }

    fn save(&mut self, storage: &mut dyn eframe::Storage) {
        eframe::set_value(storage, eframe::APP_KEY, &self.remembered);
    }

    fn on_exit(&mut self) {
        if let Some(ws) = self.workspace.take() {
            ws.journal.close();
        }
        log::info!("LoanMaster closed");
    }
}
