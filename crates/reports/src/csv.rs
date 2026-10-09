//! Reports as CSV: a header line, the rows, and the totals line last.

use std::path::Path;

use loanmaster_core::reports::{Table, Value};

use crate::Result;

fn field(text: &str) -> String {
    if text.contains([',', '"', '\n', '\r']) { format!("\"{}\"", text.replace('"', "\"\"")) } else { text.to_string() }
}

fn cell(value: &Value) -> String {
    match value {
        Value::Text(text) => field(text),
        Value::Number(n) if n.fract() == 0.0 && n.abs() < 1e15 => format!("{}", *n as i64),
        Value::Number(n) => format!("{n}"),
        Value::Blank => String::new(),
    }
}

/// The report as CSV text.
pub fn render(table: &Table) -> String {
    let mut out = String::new();
    out.push_str(&table.columns.iter().map(|c| field(c)).collect::<Vec<_>>().join(","));
    out.push('\n');
    for row in table.rows.iter().chain(table.total.iter()) {
        out.push_str(&row.iter().map(cell).collect::<Vec<_>>().join(","));
        out.push('\n');
    }
    out
}

pub fn write_table(table: &Table, path: &Path) -> Result<()> {
    std::fs::write(path, render(table))?;
    Ok(())
}
