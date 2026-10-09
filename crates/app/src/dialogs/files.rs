//! Choosing where to save a file, and opening what was saved.

use std::path::{Path, PathBuf};

use crate::app::{Action, Notice};

/// Ask where to save. The first filter is the default; a name typed without
/// an extension gets that filter's.
pub fn save_as(title: &str, file_name: &str, filters: &[(&str, &str)]) -> Option<PathBuf> {
    let mut dialog = rfd::FileDialog::new().set_title(title).set_file_name(file_name);
    for (label, extension) in filters {
        dialog = dialog.add_filter(*label, &[*extension]);
    }
    let path = dialog.save_file()?;
    let known = path
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| filters.iter().any(|(_, x)| x.eq_ignore_ascii_case(e)));
    Some(if known { path } else { path.with_extension(filters.first().map(|(_, x)| *x).unwrap_or("pdf")) })
}

/// The filters for a report: PDF, workbook or CSV.
pub const REPORT_FORMATS: [(&str, &str); 3] = [("PDF document", "pdf"), ("Excel workbook", "xlsx"), ("CSV file", "csv")];

/// Say a file was saved and open it. If it will not open, say where it is —
/// the file is there either way.
pub fn saved(actions: &mut Vec<Action>, what: &str, path: &Path) {
    actions.push(Action::Status(format!("{what} saved to {}", path.display())));
    if let Err(e) = open::that_detached(path) {
        log::warn!("Could not open {}: {e}", path.display());
        actions.push(Action::Notice(Notice::info(
            format!("{what} saved"),
            format!("{what} was saved to:\n{}\n\nIt could not be opened automatically ({e}).", path.display()),
        )));
    }
}
