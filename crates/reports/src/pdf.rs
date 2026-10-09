//! PDFs, typeset with Typst.
//!
//! Each document is a fixed template plus a `data.json` holding every figure
//! already formatted, and the logo when there is one. Names and notes reach
//! the page as data, never as markup, so nothing a member's name contains can
//! change the layout.

use std::collections::HashMap;
use std::sync::LazyLock;

use chrono::{Datelike, Local};
use loanmaster_core::money::shillings;
use loanmaster_core::reports::{Table, Value};
use loanmaster_core::statements::{LoanLine, SavingsLine, Statement, StatementConfig};
use serde_json::{Value as Json, json};
use typst::diag::{FileError, FileResult};
use typst::foundations::{Bytes, Datetime, Duration};
use typst_layout::PagedDocument;
use typst::syntax::{FileId, RootedPath, Source, VirtualPath, VirtualRoot};
use typst::text::{Font, FontBook};
use typst::utils::LazyHash;
use typst::{Library, LibraryExt, World};

use crate::{Branding, Error, Result};

const TABLE: &str = include_str!("table.typ");
const STATEMENT: &str = include_str!("statement.typ");

static FONT_FILES: [&[u8]; 3] = [
    include_bytes!("../fonts/LiberationSans-Regular.ttf"),
    include_bytes!("../fonts/LiberationSans-Bold.ttf"),
    include_bytes!("../fonts/LiberationSans-Italic.ttf"),
];

static LIBRARY: LazyLock<LazyHash<Library>> = LazyLock::new(|| LazyHash::new(Library::default()));
static FONTS: LazyLock<(LazyHash<FontBook>, Vec<Font>)> = LazyLock::new(|| {
    let fonts: Vec<Font> = FONT_FILES.iter().flat_map(|data| Font::iter(Bytes::new(*data))).collect();
    (LazyHash::new(FontBook::from_fonts(&fonts)), fonts)
});

fn file_id(path: &str) -> FileId {
    let vpath = VirtualPath::new(path).expect("a fixed, valid path");
    FileId::new(RootedPath::new(VirtualRoot::Project, vpath))
}

/// One document's files: the template, its data, and the logo.
struct Document {
    main: Source,
    files: HashMap<FileId, Bytes>,
}

impl World for Document {
    fn library(&self) -> &LazyHash<Library> {
        &LIBRARY
    }

    fn book(&self) -> &LazyHash<FontBook> {
        &FONTS.0
    }

    fn main(&self) -> FileId {
        self.main.id()
    }

    fn source(&self, id: FileId) -> FileResult<Source> {
        if id == self.main.id() { Ok(self.main.clone()) } else { Err(FileError::NotSource) }
    }

    fn file(&self, id: FileId) -> FileResult<Bytes> {
        self.files.get(&id).cloned().ok_or_else(|| FileError::NotFound(format!("{id:?}").into()))
    }

    fn font(&self, index: usize) -> Option<Font> {
        FONTS.1.get(index).cloned()
    }

    fn today(&self, _offset: Option<Duration>) -> Option<Datetime> {
        let today = Local::now().date_naive();
        Datetime::from_ymd(today.year(), today.month() as u8, today.day() as u8)
    }
}

/// Typeset `template` with `data`, adding the logo to the data when there is one.
fn render(template: &str, mut data: Json, branding: &Branding) -> Result<Vec<u8>> {
    let mut files = HashMap::new();
    let mut logo = Json::Null;
    if let Some(path) = &branding.logo {
        match std::fs::read(path) {
            Ok(bytes) => {
                let extension = path.extension().and_then(|e| e.to_str()).unwrap_or("png").to_ascii_lowercase();
                let name = format!("logo.{extension}");
                files.insert(file_id(&format!("/{name}")), Bytes::new(bytes));
                logo = json!(name);
            }
            Err(e) => log::warn!("Could not read the logo {}: {e}", path.display()),
        }
    }
    data["logo"] = logo;
    data["org"] = json!(branding.org_name);
    files.insert(file_id("/data.json"), Bytes::new(serde_json::to_vec(&data).map_err(|e| Error::Pdf(e.to_string()))?));
    let world = Document { main: Source::new(file_id("/main.typ"), template.to_string()), files };

    let document: PagedDocument = typst::compile(&world).output.map_err(|errors| {
        Error::Pdf(errors.iter().map(|e| e.message.to_string()).collect::<Vec<_>>().join("; "))
    })?;
    typst_pdf::pdf(&document, &typst_pdf::PdfOptions::default())
        .map_err(|errors| Error::Pdf(errors.iter().map(|e| e.message.to_string()).collect::<Vec<_>>().join("; ")))
}

fn generated() -> String {
    Local::now().format("%Y-%m-%d %H:%M").to_string()
}

fn text_of(value: &Value) -> String {
    match value {
        Value::Text(t) => t.clone(),
        Value::Number(n) => shillings(*n),
        Value::Blank => String::new(),
    }
}

/// A report as a PDF, landscape A4 under the letterhead.
pub fn table(table: &Table, branding: &Branding) -> Result<Vec<u8>> {
    let numeric: Vec<bool> = (0..table.columns.len()).map(|i| table.is_numeric(i)).collect();
    let rows: Vec<Vec<String>> = table.rows.iter().map(|r| r.iter().map(text_of).collect()).collect();
    let data = json!({
        "title": table.title,
        "subtitle": table.subtitle,
        "generated": generated(),
        "columns": table.columns,
        "numeric": numeric,
        "rows": rows,
        "total": table.total.as_ref().map(|t| t.iter().map(text_of).collect::<Vec<_>>()),
    });
    render(TABLE, data, branding)
}

fn loan_cell(line: &LoanLine, column: &str) -> String {
    match column {
        "Date" => line.date.clone(),
        "Type" => line.event_type.clone(),
        "Debit" => shillings(line.debit),
        "Interest" => shillings(line.interest),
        "Credit" => shillings(line.credit),
        "Balance" => shillings(line.balance),
        "Gross" => line.gross.map(shillings).unwrap_or_default(),
        "Notes" => line.notes.clone(),
        _ => String::new(),
    }
}

fn savings_cell(line: &SavingsLine, column: &str) -> String {
    match column {
        "Date" => line.date.clone(),
        "Type" => line.kind.clone(),
        "Amount" => shillings(line.amount),
        "Balance" => shillings(line.balance),
        "Notes" => line.notes.clone(),
        _ => String::new(),
    }
}

const NUMERIC: [&str; 7] = ["Debit", "Interest", "Credit", "Balance", "Gross", "Amount", ""];

/// A member's statement as a PDF.
pub fn statement(statement: &Statement, config: &StatementConfig, branding: &Branding) -> Result<Vec<u8>> {
    let loans: Vec<Json> = statement
        .loans
        .iter()
        .map(|loan| {
            let rows: Vec<Json> = loan
                .lines
                .iter()
                .map(|line| match &line.annotation {
                    Some(note) => json!({"note": note}),
                    None => json!({"cells": config.columns.iter().map(|c| loan_cell(line, c)).collect::<Vec<_>>()}),
                })
                .collect();
            json!({"ref": loan.reference, "rows": rows})
        })
        .collect();
    let savings: Vec<Json> = statement
        .savings
        .iter()
        .map(|line| {
            json!({
                "cells": config.savings_columns.iter().map(|c| savings_cell(line, c)).collect::<Vec<_>>(),
                "withdrawal": line.is_withdrawal(),
            })
        })
        .collect();
    let numeric = |columns: &[String]| columns.iter().map(|c| NUMERIC.contains(&c.as_str())).collect::<Vec<_>>();
    let data = json!({
        "title": config.title,
        "name": statement.name,
        "phone": statement.phone,
        "period": statement.period,
        "statement_date": Local::now().format(&config.date_format).to_string(),
        "active": statement.active,
        "status": if statement.active { "Active" } else { "Inactive" },
        "show_loans": config.show_loans,
        "show_savings": config.show_savings,
        "loan_columns": config.columns,
        "loan_numeric": numeric(&config.columns),
        "loans": loans,
        "savings_columns": config.savings_columns,
        "savings_numeric": numeric(&config.savings_columns),
        "savings": savings,
        "net": shillings(statement.net_outstanding),
        "gross": shillings(statement.gross_outstanding),
        "savings_balance": shillings(statement.savings_balance),
        "footer": config.footer,
        "generated": Local::now().format("%Y-%m-%d at %H:%M").to_string(),
    });
    render(STATEMENT, data, branding)
}
