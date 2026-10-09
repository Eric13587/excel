//! Restore the journal from a backup, without leaving the application.
//!
//! Restoring is itself destructive, so the window shows what each file holds
//! before one is chosen, refuses a file that is damaged or not a journal
//! before anything is touched, and the journal as it stands is copied first —
//! restoring the wrong file is the commonest way to lose data this way.

use std::path::PathBuf;
use std::time::SystemTime;

use eframe::egui::{self, RichText};
use egui_extras::{Column, TableBuilder};
use loanmaster_core::Journal;
use loanmaster_core::db::backups::{BackupInfo, inspect_backup};

use super::files;
use crate::app::{Action, Notice};
use crate::theme;
use crate::widgets;
use crate::workspace::Screen;

struct Candidate {
    path: PathBuf,
    name: String,
    size: u64,
    modified: SystemTime,
    info: BackupInfo,
}

#[derive(Default)]
pub struct Restore {
    candidates: Option<Vec<Candidate>>,
    selected: Option<usize>,
    /// The file the user is being asked about.
    confirming: Option<(PathBuf, BackupInfo)>,
}

fn human_size(bytes: u64) -> String {
    let mut n = bytes as f64;
    for unit in ["B", "KB", "MB"] {
        if n < 1024.0 {
            return if unit == "B" { format!("{bytes} B") } else { format!("{n:.1} {unit}") };
        }
        n /= 1024.0;
    }
    format!("{n:.1} GB")
}

fn taken(time: SystemTime) -> String {
    chrono::DateTime::<chrono::Local>::from(time).format("%Y-%m-%d %H:%M").to_string()
}

fn count(info: &BackupInfo, table: &str) -> i64 {
    info.counts.iter().find(|(t, _)| t == table).map(|(_, n)| *n).unwrap_or(0)
}

impl Restore {
    fn load(&mut self, journal: &Journal) {
        self.candidates = Some(
            journal
                .available_backups()
                .into_iter()
                .map(|b| Candidate { info: inspect_backup(&b.path), path: b.path, name: b.name, size: b.size, modified: b.modified })
                .collect(),
        );
        self.selected = None;
    }

    /// Ask before restoring `path` — or say why it cannot be.
    fn ask(&mut self, path: PathBuf, actions: &mut Vec<Action>) {
        let info = inspect_backup(&path);
        if !info.ok {
            actions.push(Action::Notice(Notice::warning("Cannot Restore", format!("That file cannot be restored:\n\n{}", info.problem.unwrap_or_default()))));
            return;
        }
        self.confirming = Some((path, info));
    }

    pub fn show(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) -> bool {
        if self.candidates.is_none() {
            self.load(journal);
        }
        let p = theme::palette(ctx);
        let mut open = true;
        let mut close = false;
        let mut asked: Option<PathBuf> = None;
        egui::Window::new("Restore From Backup").open(&mut open).default_size([860.0, 480.0]).collapsible(false).show(ctx, |ui| {
            ui.label(
                "Restoring replaces everything in the current journal with the contents of the backup. A copy of the journal as it \
                 stands now is saved first, so this can be undone by restoring that copy.",
            );
            ui.add_space(6.0);
            let candidates = self.candidates.as_deref().unwrap_or_default();
            egui::Panel::bottom("restore_footer").show(ui, |ui| {
                ui.add_space(4.0);
                let detail = match self.selected.and_then(|i| candidates.get(i)) {
                    Some(c) if !c.info.ok => RichText::new(format!("This file cannot be used: {}", c.info.problem.clone().unwrap_or_default())).color(p.danger),
                    Some(c) => RichText::new(format!(
                        "{} — {}, taken {}. Contains {} members, {} loans, {} ledger entries and {} savings transactions.",
                        c.name,
                        human_size(c.size),
                        taken(c.modified),
                        count(&c.info, "individuals"),
                        count(&c.info, "loans"),
                        count(&c.info, "ledger"),
                        count(&c.info, "savings")
                    ))
                    .color(p.muted),
                    None if candidates.is_empty() => RichText::new(
                        "No backups found next to this journal. One is written every time the application opens it, and before any irreversible change.",
                    )
                    .color(p.muted),
                    None => RichText::new("Choose a backup to see what it holds.").color(p.muted),
                };
                ui.label(detail);
                super::footer(ui, |ui| {
                    if ui.button("Close").clicked() {
                        close = true;
                    }
                    let usable = self.selected.and_then(|i| candidates.get(i)).filter(|c| c.info.ok).map(|c| c.path.clone());
                    if widgets::solid_enabled(ui, usable.is_some(), "Restore Selected", p.danger, "Choose a usable backup first").clicked()
                        && let Some(path) = usable
                    {
                        asked = Some(path);
                    }
                    ui.with_layout(egui::Layout::left_to_right(egui::Align::Center), |ui| {
                        if ui.button("Choose a File…").on_hover_text("Restore from a backup kept somewhere else").clicked()
                            && let Some(path) = files::pick("Choose a Backup", &[("Journals", &["db", "bak"])])
                        {
                            asked = Some(path);
                        }
                    });
                });
            });
            egui::CentralPanel::default().show(ui, |ui| {
                let mut clicked = None;
                widgets::clickable_rows(ui);
                TableBuilder::new(ui)
                    .striped(true)
                    .sense(egui::Sense::click())
                    .cell_layout(egui::Layout::left_to_right(egui::Align::Center))
                    .column(Column::exact(130.0))
                    .columns(Column::exact(80.0), 3)
                    .column(Column::remainder().at_least(200.0))
                    .header(24.0, |mut header| {
                        for title in ["Taken", "Members", "Loans", "Ledger entries", "File"] {
                            header.col(|ui| {
                                ui.label(RichText::new(title).strong().color(p.muted));
                            });
                        }
                    })
                    .body(|body| {
                        body.rows(24.0, candidates.len(), |mut row| {
                            let i = row.index();
                            let c = &candidates[i];
                            row.set_selected(self.selected == Some(i));
                            let colour = if c.info.ok { p.text } else { p.muted };
                            let figure = |table: &str| if c.info.ok { count(&c.info, table).to_string() } else { "—".into() };
                            for text in [taken(c.modified), figure("individuals"), figure("loans"), figure("ledger"), c.name.clone()] {
                                row.col(|ui| {
                                    ui.label(RichText::new(text).color(colour));
                                });
                            }
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
        if let Some(path) = asked {
            self.ask(path, actions);
        }
        self.confirm(ctx, journal, actions);
        open && !close
    }

    fn confirm(&mut self, ctx: &egui::Context, journal: &Journal, actions: &mut Vec<Action>) {
        let Some((path, info)) = &self.confirming else { return };
        let p = theme::palette(ctx);
        let now = |table: &str| journal.conn().query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get::<_, i64>(0)).unwrap_or(0);
        let (mut go, mut cancel) = (false, false);
        let response = egui::Modal::new(egui::Id::new("restore_confirm")).show(ctx, |ui| {
            ui.set_max_width(520.0);
            ui.heading("Restore Journal");
            ui.add_space(4.0);
            ui.label("Replace the current journal with this backup?");
            ui.add_space(4.0);
            egui::Grid::new("restore_counts").num_columns(2).spacing([16.0, 4.0]).show(ui, |ui| {
                ui.label("Now");
                ui.label(format!("{} members, {} ledger entries", now("individuals"), now("ledger")));
                ui.end_row();
                ui.label("Backup");
                ui.label(format!("{} members, {} ledger entries", count(info, "individuals"), count(info, "ledger")));
                ui.end_row();
            });
            ui.add_space(4.0);
            ui.label(
                RichText::new(
                    "Everything recorded since this backup was taken will be removed from the journal. A copy of the current state \
                     is saved first, so this can be undone.",
                )
                .color(p.muted),
            );
            super::footer(ui, |ui| {
                if widgets::solid(ui, "Restore", p.danger).clicked() {
                    go = true;
                }
                if ui.button("Cancel").clicked() {
                    cancel = true;
                }
            });
        });
        if go {
            let path = path.clone();
            self.confirming = None;
            // Reloaded once restored: the safety copy is a new backup.
            self.candidates = None;
            actions.push(Action::run(move |ws, actions| {
                let name = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
                match ws.journal.restore_from(&path) {
                    Ok(safety) => {
                        // Nothing on screen, and nothing to undo, describes the
                        // journal any more.
                        ws.undo = Default::default();
                        ws.screen = Screen::Dashboard;
                        let safety = safety.map(|s| s.display().to_string()).unwrap_or_else(|| "(not needed)".into());
                        actions.push(Action::Changed);
                        actions.push(Action::Status(format!("The journal was restored from {name}.")));
                        actions.push(Action::Notice(Notice::info(
                            "Restored",
                            format!("The journal has been restored from:\n{name}\n\nThe state before restoring was saved to:\n{safety}"),
                        )));
                    }
                    Err(e) => actions.push(Action::Notice(Notice::from_error("The journal could not be restored", &e))),
                }
            }));
        } else if cancel || response.should_close() {
            self.confirming = None;
        }
    }
}
