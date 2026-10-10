//! Writing reports and statements to files: Excel workbooks, CSV and PDF.
//!
//! The figures come from `loanmaster_core::reports` and
//! `loanmaster_core::statements`; this crate only lays them out. PDFs are
//! typeset with Typst from fixed templates, with Liberation Sans built in so a
//! document looks the same on every machine.

use std::path::Path;

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

/// The logo that ships inside the program: the society's own, printed until
/// Settings names another.
pub const BUNDLED_LOGO: &[u8] = include_bytes!("../../../resources/nairobi_water_sacco_logo.png");

/// A letterhead image: its bytes, and the kind of image they are.
#[derive(Debug, Clone, PartialEq)]
pub struct Logo {
    /// `png`, `jpg`, `gif` or `svg`: how the bytes are read.
    pub kind: String,
    pub bytes: Vec<u8>,
}

impl Logo {
    pub fn bundled() -> Logo {
        Logo { kind: "png".into(), bytes: BUNDLED_LOGO.to_vec() }
    }

    /// An image file, its kind taken from its extension.
    pub fn read(path: &Path) -> std::io::Result<Logo> {
        let kind = match path.extension().and_then(|e| e.to_str()).map(str::to_ascii_lowercase).as_deref() {
            Some("jpeg" | "jpg") => "jpg".to_string(),
            Some(other) => other.to_string(),
            None => "png".to_string(),
        };
        Ok(Logo { kind, bytes: std::fs::read(path)? })
    }
}

/// The society's identity on every document, and the workbook colours.
#[derive(Debug, Clone, PartialEq)]
pub struct Branding {
    pub org_name: String,
    pub logo: Option<Logo>,
    /// Workbook header and total row backgrounds (`#RRGGBB`).
    pub header_bg: String,
    pub total_bg: String,
}

impl Branding {
    /// The journal's own branding: its organisation name, and its logo —
    /// the one Settings names if it can be read, else the bundled one.
    pub fn of(journal: &Journal) -> Branding {
        let settings = journal.settings();
        let configured = settings.org_logo_path();
        let logo = if configured.trim().is_empty() {
            Logo::bundled()
        } else {
            Logo::read(Path::new(configured.trim())).unwrap_or_else(|e| {
                log::warn!("The logo {configured} could not be read ({e}); printing the bundled one");
                Logo::bundled()
            })
        };
        Branding { org_name: settings.org_name(), logo: Some(logo), header_bg: settings.excel_header_bg(), total_bg: settings.excel_total_bg() }
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
pub fn write_statement_xlsx(statement: &Statement, config: &StatementConfig, branding: &Branding, path: &Path) -> Result<()> {
    xlsx::write_statement(statement, config, branding, path)
}
