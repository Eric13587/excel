//! Reports and statements as Excel workbooks, formatted as LoanMaster 1.x
//! formatted them: a shaded header row, whole-shilling numbers, and a bold
//! shaded totals row.

use std::path::Path;

use loanmaster_core::reports::{Table, Value};
use loanmaster_core::statements::{LoanLine, SavingsLine, Statement, StatementConfig};
use rust_xlsxwriter::{Color, Format, FormatAlign, FormatBorder, Workbook, Worksheet};

use crate::{Branding, Result};

/// Title bands (white text).
const ACCENT: &str = "#2563EB";
/// Column header rows.
const HEADER: &str = "#E2E8F0";
/// Total and summary rows.
const TOTAL: &str = "#F1F5F9";
const WHOLE: &str = "#,##0";

fn color(hex: &str, fallback: &str) -> Color {
    let parse = |h: &str| u32::from_str_radix(h.trim().trim_start_matches('#'), 16).ok().filter(|_| h.trim().len() == 7);
    Color::RGB(parse(hex).or_else(|| parse(fallback)).unwrap_or(0xFFFFFF))
}

fn col(i: usize) -> u16 {
    i as u16
}

fn write_value(sheet: &mut Worksheet, row: u32, column: u16, value: &Value, text: &Format, number: &Format) -> Result<()> {
    match value {
        Value::Text(t) => sheet.write_string_with_format(row, column, t, text)?,
        Value::Number(n) => sheet.write_number_with_format(row, column, *n, number)?,
        Value::Blank => sheet.write_blank(row, column, text)?,
    };
    Ok(())
}

/// A report as a one-sheet workbook.
pub fn write_table(table: &Table, branding: &Branding, path: &Path) -> Result<()> {
    let mut workbook = Workbook::new();
    let sheet = workbook.add_worksheet();
    let name: String = table.sheet.chars().filter(|c| !"[]:*?/\\".contains(*c)).take(31).collect();
    sheet.set_name(if name.is_empty() { "Report" } else { &name })?;

    let header = Format::new().set_bold().set_border(FormatBorder::Thin).set_background_color(color(&branding.header_bg, HEADER));
    let number = Format::new().set_num_format(WHOLE);
    let plain = Format::new();
    let total_text = Format::new().set_bold().set_border(FormatBorder::Thin).set_background_color(color(&branding.total_bg, TOTAL));
    let total_number = total_text.clone().set_num_format(WHOLE);

    for (i, title) in table.columns.iter().enumerate() {
        sheet.write_string_with_format(0, col(i), title, &header)?;
        let width = match title.as_str() {
            "Name" | "Member" => 26.0,
            "Employment Status" => 18.0,
            "PF No" => 12.0,
            _ => 14.0,
        };
        sheet.set_column_width(col(i), width)?;
    }
    for (r, row) in table.rows.iter().enumerate() {
        for (i, value) in row.iter().enumerate() {
            write_value(sheet, r as u32 + 1, col(i), value, &plain, &number)?;
        }
    }
    if let Some(total) = &table.total {
        let at = table.rows.len() as u32 + 1;
        for (i, value) in total.iter().enumerate() {
            write_value(sheet, at, col(i), value, &total_text, &total_number)?;
        }
    }
    workbook.save(path)?;
    Ok(())
}

fn loan_value(line: &LoanLine, column: &str) -> Value {
    match column {
        "Date" => Value::text(&line.date),
        "Type" => Value::text(&line.event_type),
        "Debit" => Value::Number(line.debit),
        "Interest" => Value::Number(line.interest),
        "Credit" => Value::Number(line.credit),
        "Balance" => Value::Number(line.balance),
        "Gross" => line.gross.map(Value::Number).unwrap_or_else(|| Value::text("")),
        "Notes" => Value::text(&line.notes),
        _ => Value::text(""),
    }
}

fn savings_value(line: &SavingsLine, column: &str) -> Value {
    match column {
        "Date" => Value::text(&line.date),
        "Type" => Value::text(&line.kind),
        "Amount" => Value::Number(line.amount),
        "Balance" => Value::Number(line.balance),
        "Notes" => Value::text(&line.notes),
        _ => Value::text(""),
    }
}

/// A band across `columns` columns: merged when there is more than one.
fn band(sheet: &mut Worksheet, row: u32, columns: usize, text: &str, format: &Format) -> Result<()> {
    if columns > 1 {
        sheet.merge_range(row, 0, row, col(columns - 1), text, format)?;
    } else {
        sheet.write_string_with_format(row, 0, text, format)?;
    }
    Ok(())
}

/// A statement as a one-sheet workbook: the loans, each under its reference,
/// then savings.
pub fn write_statement(statement: &Statement, config: &StatementConfig, path: &Path) -> Result<()> {
    let mut workbook = Workbook::new();
    let sheet = workbook.add_worksheet();
    sheet.set_name("Statement")?;

    let status_color = if statement.active { "#28A745" } else { "#DC3545" };
    let title = Format::new()
        .set_bold()
        .set_font_size(14)
        .set_align(FormatAlign::Center)
        .set_background_color(color(ACCENT, ACCENT))
        .set_font_color(Color::White);
    let status = Format::new().set_bold().set_font_size(12).set_align(FormatAlign::Center).set_font_color(color(status_color, status_color));
    let section = Format::new().set_bold().set_font_size(12).set_background_color(color(HEADER, HEADER)).set_border(FormatBorder::Thin);
    let column_header = Format::new().set_bold().set_background_color(color(TOTAL, TOTAL)).set_border(FormatBorder::Thin);
    let savings_title = Format::new()
        .set_bold()
        .set_font_size(12)
        .set_background_color(color(ACCENT, ACCENT))
        .set_font_color(Color::White)
        .set_border(FormatBorder::Thin);
    let cell = Format::new().set_border(FormatBorder::Thin);
    let money = Format::new().set_border(FormatBorder::Thin).set_num_format(WHOLE);
    let period = Format::new().set_italic().set_font_size(10).set_align(FormatAlign::Center);
    let note = Format::new()
        .set_border(FormatBorder::Thin)
        .set_italic()
        .set_font_color(color("#888888", "#888888"))
        .set_background_color(color("#F0F0F0", "#F0F0F0"));

    sheet.merge_range(0, 0, 0, 7, &format!("{} - {}", config.title, statement.name), &title)?;
    sheet.merge_range(1, 0, 1, 5, &format!("For the period: {}", statement.period), &period)?;
    sheet.merge_range(1, 6, 1, 7, if statement.active { "Active" } else { "Inactive" }, &status)?;
    for i in 0..config.columns.len().max(1) {
        sheet.set_column_width(col(i), 15.0)?;
    }

    let mut row = 3u32;
    if config.show_loans {
        for loan in &statement.loans {
            band(sheet, row, config.columns.len(), &format!("Loan Reference: {}", loan.reference), &section)?;
            row += 1;
            for (i, name) in config.columns.iter().enumerate() {
                sheet.write_string_with_format(row, col(i), name, &column_header)?;
            }
            row += 1;
            for line in &loan.lines {
                if let Some(text) = &line.annotation {
                    band(sheet, row, config.columns.len(), text, &note)?;
                } else {
                    for (i, name) in config.columns.iter().enumerate() {
                        write_value(sheet, row, col(i), &loan_value(line, name), &cell, &money)?;
                    }
                }
                row += 1;
            }
            row += 1;
        }
    }
    if config.show_savings && !statement.savings.is_empty() {
        band(sheet, row, config.savings_columns.len(), "SAVINGS / SHARES", &savings_title)?;
        row += 1;
        for (i, name) in config.savings_columns.iter().enumerate() {
            sheet.write_string_with_format(row, col(i), name, &column_header)?;
        }
        row += 1;
        for line in &statement.savings {
            for (i, name) in config.savings_columns.iter().enumerate() {
                write_value(sheet, row, col(i), &savings_value(line, name), &cell, &money)?;
            }
            row += 1;
        }
    }
    workbook.save(path)?;
    Ok(())
}
