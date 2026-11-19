//! Work long enough to be watched — a batch of statements, a large report —
//! run off the window's thread, with a progress bar and a Cancel button.
//!
//! The journal is read before a task starts, on the window's thread, so a
//! task only ever handles plain data (typesetting, writing files) and never
//! shares the journal's connection. What it finished with comes back as a
//! closure that queues [`Action`]s on the window's thread.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use eframe::egui::{self, RichText};

use crate::app::{Action, Notice};
use crate::theme;

/// What a task hands back: run on the window's thread when it ends.
pub type Completion = Box<dyn FnOnce(&mut Vec<Action>) + Send>;

/// A task is shown only once it has run this long, so a quick one does not
/// flash a window.
const SHOW_AFTER: Duration = Duration::from_millis(250);

#[derive(Default)]
struct Shared {
    /// `(done, of, what)`.
    progress: Mutex<(usize, usize, String)>,
    cancelled: AtomicBool,
}

/// The task's side: say how far it has got, and hear about Cancel.
pub struct Reporter {
    shared: Arc<Shared>,
    ctx: egui::Context,
}

impl Reporter {
    /// Record progress. `false` once the user has asked to stop.
    pub fn step(&self, done: usize, of: usize, what: &str) -> bool {
        if let Ok(mut progress) = self.shared.progress.lock() {
            *progress = (done, of, what.to_string());
        }
        self.ctx.request_repaint();
        !self.cancelled()
    }

    pub fn cancelled(&self) -> bool {
        self.shared.cancelled.load(Ordering::Relaxed)
    }
}

pub struct Task {
    title: String,
    shared: Arc<Shared>,
    done: Receiver<Completion>,
    started: Instant,
}

impl Task {
    /// Start `work` on its own thread.
    pub fn spawn(ctx: &egui::Context, title: impl Into<String>, work: impl FnOnce(&Reporter) -> Completion + Send + 'static) -> Task {
        let shared = Arc::new(Shared::default());
        let (sender, done) = mpsc::channel();
        let reporter = Reporter { shared: Arc::clone(&shared), ctx: ctx.clone() };
        let title = title.into();
        let spawned = std::thread::Builder::new().name("loanmaster-task".into()).spawn(move || {
            let completion = work(&reporter);
            let _ = sender.send(completion);
            reporter.ctx.request_repaint();
        });
        if let Err(e) = spawned {
            // The receiver sees the sender dropped and reports the failure.
            log::error!("Could not start “{title}”: {e}");
        }
        Task { title, shared, done, started: Instant::now() }
    }

    /// Draw the progress window. The completion once the task has ended.
    pub fn show(&mut self, ctx: &egui::Context) -> Option<Completion> {
        match self.done.try_recv() {
            Ok(completion) => return Some(completion),
            Err(TryRecvError::Disconnected) => {
                let title = self.title.clone();
                return Some(Box::new(move |actions: &mut Vec<Action>| {
                    actions.push(Action::Notice(Notice::warning(
                        format!("{title} stopped"),
                        format!("{title} stopped unexpectedly. Files already written were kept. The details are in the log ({}).", crate::logging::log_file().display()),
                    )));
                }));
            }
            Err(TryRecvError::Empty) => {}
        }
        // Poll for the end even when the task has nothing new to say.
        ctx.request_repaint_after(Duration::from_millis(100));
        if self.started.elapsed() < SHOW_AFTER {
            return None;
        }
        let (done, of, what) = self.shared.progress.lock().map(|p| p.clone()).unwrap_or_default();
        let cancelled = self.shared.cancelled.load(Ordering::Relaxed);
        let p = theme::palette(ctx);
        egui::Modal::new(egui::Id::new("task")).show(ctx, |ui| {
            ui.set_width(420.0);
            ui.heading(&self.title);
            ui.add_space(6.0);
            let fraction = if of == 0 { 0.0 } else { done as f32 / of as f32 };
            let text = if of == 0 { "Starting…".to_string() } else { format!("{done} of {of}") };
            ui.add(egui::ProgressBar::new(fraction).text(text).animate(of == 0));
            ui.add_space(4.0);
            ui.label(RichText::new(if cancelled { "Stopping after the current item…" } else { what.as_str() }).color(p.muted));
            crate::dialogs::footer(ui, |ui| {
                if ui.add_enabled(!cancelled, egui::Button::new("Cancel")).clicked() {
                    self.shared.cancelled.store(true, Ordering::Relaxed);
                }
            });
        });
        None
    }
}
