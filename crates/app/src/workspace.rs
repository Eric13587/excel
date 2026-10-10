//! An open journal and everything shown for it.

use std::path::{Path, PathBuf};

use eframe::egui;
use loanmaster_core::undo::UndoStack;
use loanmaster_core::{Journal, OpenOptions, Result};

use crate::app::{Action, Notice};
use crate::dialogs::Dialog;
use crate::screens::dashboard::Dashboard;
use crate::screens::grid::GridScreen;
use crate::screens::member::MemberScreen;
use crate::task::Task;

pub enum Screen {
    Dashboard,
    Member(Box<MemberScreen>),
    Grid(Box<GridScreen>),
}

pub struct Workspace {
    pub journal: Journal,
    pub path: PathBuf,
    pub screen: Screen,
    /// Kept while other screens are shown, so the search and selection survive.
    pub dashboard: Dashboard,
    pub dialogs: Vec<Dialog>,
    pub undo: UndoStack,
    /// Bumped whenever the journal changes; screens re-read when it moves.
    pub revision: u64,
    pub status: Option<String>,
    pub org_name: String,
    pub dark: bool,
    /// The date last used in a dialog, offered again in the next one.
    pub last_date: chrono::NaiveDate,
    /// Long work under way, shown with its progress.
    pub task: Option<Task>,
}

impl Workspace {
    fn with(journal: Journal, path: PathBuf) -> Workspace {
        let org_name = journal.settings().org_name();
        let dark = journal.settings().app_theme() == "Dark";
        Workspace {
            journal,
            path,
            screen: Screen::Dashboard,
            dashboard: Dashboard::default(),
            dialogs: Vec::new(),
            undo: UndoStack::default(),
            revision: 0,
            status: None,
            org_name,
            dark,
            last_date: chrono::Local::now().date_naive(),
            task: None,
        }
    }

    /// Open a journal, taking the on-open backup, and say what needs saying
    /// about the file.
    pub fn open(path: &Path) -> Result<(Workspace, Vec<Notice>)> {
        let journal = Journal::open(path, OpenOptions { backup_on_open: true })?;
        let report = journal.report().clone();
        let mut notices = Vec::new();
        if !report.integrity_ok {
            notices.push(Notice::warning(
                "This journal file is damaged",
                "SQLite's check of this journal reported damage. The journal was opened so you can export your \
                 data, but restore a recent backup (Tools › Restore From Backup) as soon as possible.",
            ));
        }
        log::info!(
            "Opened {} ({}: {}; {} orphaned rows; backup {:?})",
            path.display(),
            report.journal_mode,
            report.journal_mode_reason,
            report.orphaned_rows,
            report.backup
        );
        let mut workspace = Workspace::with(journal, path.to_path_buf());
        if report.created {
            workspace.status = Some("A new, empty journal was created.".into());
        }
        Ok((workspace, notices))
    }

    /// A workspace over an in-memory journal, for tests.
    #[cfg(test)]
    pub fn in_memory(journal: Journal) -> Workspace {
        Workspace::with(journal, PathBuf::from("test.db"))
    }

    pub fn changed(&mut self) {
        self.revision += 1;
        self.org_name = self.journal.settings().org_name();
        self.dark = self.journal.settings().app_theme() == "Dark";
    }

    pub fn open_member(&mut self, id: i64) {
        self.dashboard.selected = Some(id);
        self.screen = Screen::Member(Box::new(MemberScreen::new(id)));
    }

    /// Show the Ledger Grid — the one already showing, staged work and all,
    /// if it is.
    pub fn open_grid(&mut self) {
        if !matches!(self.screen, Screen::Grid(_)) {
            self.screen = Screen::Grid(Box::default());
        }
    }

    /// The Ledger Grid's staged changes, described, if there are any.
    pub fn unsaved(&self) -> Option<String> {
        match &self.screen {
            Screen::Grid(grid) => grid.unsaved(),
            _ => None,
        }
    }

    /// Save the Ledger Grid's staged changes. True when none are left.
    pub fn save_unsaved(&mut self, actions: &mut Vec<Action>) -> bool {
        let Workspace { screen, journal, .. } = self;
        match screen {
            Screen::Grid(grid) => grid.save_all(journal, actions),
            _ => true,
        }
    }

    pub fn discard_unsaved(&mut self) {
        if let Screen::Grid(grid) = &mut self.screen {
            grid.discard();
        }
    }

    pub fn undo(&mut self, actions: &mut Vec<Action>) {
        match self.undo.undo(&self.journal) {
            Ok(Some(what)) => {
                actions.push(Action::Status(format!("Undone: {what}.")));
                actions.push(Action::Changed);
            }
            Ok(None) => actions.push(Action::Status("Nothing to undo.".into())),
            Err(e) => actions.push(Action::Notice(Notice::from_error("Could not undo", &e))),
        }
    }

    pub fn redo(&mut self, actions: &mut Vec<Action>) {
        match self.undo.redo(&self.journal) {
            Ok(Some(what)) => {
                actions.push(Action::Status(format!("Redone: {what}.")));
                actions.push(Action::Changed);
            }
            Ok(None) => actions.push(Action::Status("Nothing to redo.".into())),
            Err(e) => actions.push(Action::Notice(Notice::from_error("Could not redo", &e))),
        }
    }

    /// Draw the open dialogs, newest on top, dropping the ones that closed —
    /// then the task under way, above them all.
    pub fn show_dialogs(&mut self, ctx: &egui::Context, actions: &mut Vec<Action>) {
        let Workspace { dialogs, journal, .. } = self;
        let mut index = 0;
        while index < dialogs.len() {
            if dialogs[index].show(ctx, journal, actions) {
                index += 1;
            } else {
                dialogs.remove(index);
            }
        }
        if let Some(task) = &mut self.task
            && let Some(done) = task.show(ctx)
        {
            self.task = None;
            done(actions);
        }
    }
}
