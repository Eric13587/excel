//! Writing reports and statements to files: Excel workbooks, CSV and PDF.
//!
//! The figures come from `loanmaster_core::reports` and
//! `loanmaster_core::statements`; this crate only lays them out. PDFs are
//! typeset with Typst from fixed templates, with Liberation Sans built in so a
//! document looks the same on every machine.

use std::path::{Path, PathBuf};

use loanmaster_core::Journal;
use loanmaster_core::reports::Table;
use loanmaster_core::statements::{Statement, StatementConfig};

pub mod csv;
pub mod pdf;
pub mod xlsx;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{0}")]
    Io(#[from] std::io::Error),
    #[error("Could not write the workbook: {0}")]
    Xlsx(#[from] rust_xlsxwriter::XlsxError),
    #[error("Could not typeset the PDF: {0}")]
    Pdf(String),
}

pub type Result<T> = std::result::Result<T, Error>;

/// The society's identity on every document, and the workbook colours.
#[derive(Debug, Clone, PartialEq)]
pub struct Branding {
    pub org_name: String,
    /// The logo to print, if one is configured or shipped and exists.
    pub logo: Option<PathBuf>,
    /// Workbook header and total row backgrounds (`#RRGGBB`).
    pub header_bg: String,
    pub total_bg: String,
}

/// Where the shipped logo lives: `resources/` beside the executable.
pub fn bundled_logo() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let path = exe.parent()?.join("resources").join("nairobi_water_sacco_logo.png");
    path.exists().then_some(path)
}

impl Branding {
    /// The journal's own branding: its organisation name, and its logo —
    /// the configured one if the file still exists, else the shipped one.
    pub fn of(journal: &Journal) -> Branding {
        let settings = journal.settings();
        let configured = settings.org_logo_path();
        let logo = if !configured.is_empty() && Path::new(&configured).exists() {
            Some(PathBuf::from(&configured))
        } else {
            if !configured.is_empty() {
                log::warn!("Configured logo not found on disk: {configured}");
            }
            bundled_logo()
        };
        Branding { org_name: settings.org_name(), logo, header_bg: settings.excel_header_bg(), total_bg: settings.excel_total_bg() }
    }
}

impl Default for Branding {
    fn default() -> Self {
        Branding { org_name: String::new(), logo: None, header_bg: "#E2E8F0".into(), total_bg: "#F1F5F9".into() }
    }
}

/// The formats a report can be saved in, by file extension.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Xlsx,
    Csv,
    Pdf,
}

impl Format {
    /// The format a path asks for; a workbook unless it ends `.csv` or `.pdf`.
    pub fn of(path: &Path) -> Format {
        match path.extension().and_then(|e| e.to_str()).map(str::to_ascii_lowercase).as_deref() {
            Some("csv") => Format::Csv,
            Some("pdf") => Format::Pdf,
            _ => Format::Xlsx,
        }
    }
}

/// Save a report in the format its file name asks for.
pub fn write_table(table: &Table, branding: &Branding, path: &Path) -> Result<()> {
    match Format::of(path) {
        Format::Xlsx => xlsx::write_table(table, branding, path),
        Format::Csv => csv::write_table(table, path),
        Format::Pdf => std::fs::write(path, pdf::table(table, branding)?).map_err(Error::from),
    }
}

/// Save a statement as a PDF.
pub fn write_statement_pdf(statement: &Statement, config: &StatementConfig, branding: &Branding, path: &Path) -> Result<()> {
    std::fs::write(path, pdf::statement(statement, config, branding)?)?;
    Ok(())
}

/// Save a statement as a workbook.
pub fn write_statement_xlsx(statement: &Statement, config: &StatementConfig, path: &Path) -> Result<()> {
    xlsx::write_statement(statement, config, path)
}
