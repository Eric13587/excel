//! Choosing where to save a file, and opening what was saved.

use std::path::{Path, PathBuf};

use crate::app::{Action, Notice};

/// Ask where to save. The first filter is the default; a name typed without
/// an extension gets that filter's.
pub fn save_as(title: &str, file_name: &str, filters: &[(&str, &str)]) -> Option<PathBuf> {
    let path = ask::save(title, file_name, filters)?;
    let known = path
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| filters.iter().any(|(_, x)| x.eq_ignore_ascii_case(e)));
    Some(if known { path } else { path.with_extension(filters.first().map(|(_, x)| *x).unwrap_or("pdf")) })
}

/// Ask for a folder to write into.
pub fn folder(title: &str) -> Option<PathBuf> {
    ask::folder(title)
}

/// The filters for a report: PDF, workbook or CSV.
pub const REPORT_FORMATS: [(&str, &str); 3] = [("PDF document", "pdf"), ("Excel workbook", "xlsx"), ("CSV file", "csv")];

/// Say a file was saved and open it. If it will not open, say where it is —
/// the file is there either way.
pub fn saved(actions: &mut Vec<Action>, what: &str, path: &Path) {
    actions.push(Action::Status(format!("{what} saved to {}", path.display())));
    if let Err(e) = ask::open(path) {
        log::warn!("Could not open {}: {e}", path.display());
        actions.push(Action::Notice(Notice::info(
            format!("{what} saved"),
            format!("{what} was saved to:\n{}\n\nIt could not be opened automatically ({e}).", path.display()),
        )));
    }
}

/// The desktop's own dialogs and file opener.
#[cfg(not(test))]
mod ask {
    use std::path::{Path, PathBuf};

    pub fn save(title: &str, file_name: &str, filters: &[(&str, &str)]) -> Option<PathBuf> {
        let mut dialog = rfd::FileDialog::new().set_title(title).set_file_name(file_name);
        for (label, extension) in filters {
            dialog = dialog.add_filter(*label, &[*extension]);
        }
        dialog.save_file()
    }

    pub fn folder(title: &str) -> Option<PathBuf> {
        rfd::FileDialog::new().set_title(title).pick_folder()
    }

    pub fn open(path: &Path) -> std::io::Result<()> {
        open::that_detached(path)
    }
}

/// In tests a dialog answers what the test chose beforehand, and nothing is
/// opened.
#[cfg(test)]
pub mod ask {
    use std::cell::RefCell;
    use std::path::{Path, PathBuf};

    thread_local! {
        static CHOSEN: RefCell<Option<PathBuf>> = const { RefCell::new(None) };
    }

    /// What the next file or folder dialog answers.
    pub fn answer(path: impl Into<PathBuf>) {
        CHOSEN.with(|chosen| *chosen.borrow_mut() = Some(path.into()));
    }

    pub fn save(_: &str, _: &str, _: &[(&str, &str)]) -> Option<PathBuf> {
        CHOSEN.with(|chosen| chosen.borrow_mut().take())
    }

    pub fn folder(_: &str) -> Option<PathBuf> {
        CHOSEN.with(|chosen| chosen.borrow_mut().take())
    }

    pub fn open(_: &Path) -> std::io::Result<()> {
        Ok(())
    }
}
