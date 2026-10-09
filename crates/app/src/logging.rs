//! The application log: a size-rotated file in the per-user state folder,
//! where support can find it, echoed to stderr when there is a console.
//!
//! Windows: `%LOCALAPPDATA%\LoanMaster\logs`; macOS: `~/Library/Logs/LoanMaster`;
//! elsewhere `$XDG_STATE_HOME/loanmaster/logs` — the folders 1.x used.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

const MAX_BYTES: u64 = 1_000_000;
const KEEP: usize = 5;

/// The folder the log lives in, created if needed.
pub fn log_dir() -> PathBuf {
    let dir = if cfg!(windows) {
        std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(home).join("LoanMaster").join("logs")
    } else if cfg!(target_os = "macos") {
        home().join("Library/Logs/LoanMaster")
    } else {
        std::env::var_os("XDG_STATE_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join(".local/state"))
            .join("loanmaster")
            .join("logs")
    };
    let _ = std::fs::create_dir_all(&dir);
    dir
}

fn home() -> PathBuf {
    std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."))
}

pub fn log_file() -> PathBuf {
    log_dir().join("loanmaster.log")
}

/// `loanmaster.log` → `.1` → … → `.5`, oldest dropped.
fn rotate(path: &Path) {
    for n in (1..KEEP).rev() {
        let _ = std::fs::rename(path.with_extension(format!("log.{n}")), path.with_extension(format!("log.{}", n + 1)));
    }
    let _ = std::fs::rename(path, path.with_extension("log.1"));
}

struct FileLogger {
    path: PathBuf,
    file: Mutex<Option<File>>,
    console: bool,
}

impl FileLogger {
    fn open(path: &Path) -> Option<File> {
        if std::fs::metadata(path).is_ok_and(|m| m.len() >= MAX_BYTES) {
            rotate(path);
        }
        OpenOptions::new().create(true).append(true).open(path).ok()
    }
}

impl log::Log for FileLogger {
    fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
        // Graphics and windowing crates are chatty; keep their warnings only.
        let ours = metadata.target().starts_with("loanmaster");
        metadata.level() <= if ours { log::Level::Debug } else { log::Level::Warn }
    }

    fn log(&self, record: &log::Record<'_>) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let line = format!(
            "{} {:<5} {}: {}\n",
            chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f"),
            record.level(),
            record.target(),
            record.args()
        );
        if self.console && record.level() <= log::Level::Info {
            eprint!("{line}");
        }
        let Ok(mut guard) = self.file.lock() else { return };
        if guard.as_ref().and_then(|f| f.metadata().ok()).is_some_and(|m| m.len() >= MAX_BYTES) {
            *guard = None;
            rotate(&self.path);
        }
        if guard.is_none() {
            *guard = FileLogger::open(&self.path);
        }
        if let Some(file) = guard.as_mut() {
            let _ = file.write_all(line.as_bytes());
        }
    }

    fn flush(&self) {
        if let Ok(mut guard) = self.file.lock()
            && let Some(file) = guard.as_mut()
        {
            let _ = file.flush();
        }
    }
}

/// Install the logger. Returns the log file's path.
pub fn init() -> PathBuf {
    let path = log_file();
    let logger = FileLogger { file: Mutex::new(FileLogger::open(&path)), path: path.clone(), console: true };
    if log::set_boxed_logger(Box::new(logger)).is_ok() {
        log::set_max_level(log::LevelFilter::Debug);
    }
    path
}

/// Log a panic with its location and a backtrace, and tell the user where
/// the details went. The journal is safe: every write is a transaction, and
/// one interrupted by a panic is rolled back.
pub fn install_panic_hook() {
    let default = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let backtrace = std::backtrace::Backtrace::force_capture();
        log::error!("Unexpected error: {info}\n{backtrace}");
        log::logger().flush();
        default(info);
        let message = format!(
            "LoanMaster hit an unexpected error and must close. Your journal is safe — an interrupted change is \
             never half-written.\n\n{info}\n\nDetails were written to:\n{}",
            log_file().display()
        );
        rfd::MessageDialog::new()
            .set_title("LoanMaster — Unexpected Error")
            .set_level(rfd::MessageLevel::Error)
            .set_description(message)
            .show();
    }));
}
