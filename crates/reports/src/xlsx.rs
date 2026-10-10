//! Reports and statements as Excel workbooks, formatted as LoanMaster 1.x
//! formatted them: a shaded header row, whole-shilling numbers, and a bold
//! shaded totals row.

use std::path::Path;

use loanmaster_core::reports::{Table, Value};
use loanmaster_core::statements::{LoanLine, SavingsLine, Statement, StatementConfig};
use rust_xlsxwriter::{Color, Format, FormatAlign, FormatBorder, Image, Workbook, Worksheet};

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
    // A balance carried in has no movement of its own.
    if line.brought_forward && matches!(column, "Debit" | "Interest" | "Credit" | "Notes") {
        return Value::Blank;
    }
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
    if line.brought_forward && matches!(column, "Amount" | "Notes") {
        return Value::Blank;
    }
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

/// The logo in the top-left corner, about as tall as the three letterhead
/// rows beside it. False when there is none, or it is not an image a
/// workbook can hold (SVG).
fn letterhead_logo(sheet: &mut Worksheet, branding: &Branding) -> bool {
    let Some(logo) = &branding.logo else { return false };
    let image = match Image::new_from_buffer(&logo.bytes) {
        Ok(image) => image.set_scale_to_size(58, 58, true),
        Err(e) => {
            log::warn!("The logo cannot go in a workbook ({e}); it is left out");
            return false;
        }
    };
    match sheet.insert_image(0, 0, &image) {
        Ok(_) => true,
        Err(e) => {
            log::warn!("The logo could not be placed ({e}); it is left out");
            false
        }
    }
}

/// A statement as a one-sheet workbook: the letterhead, the loans, each
/// under its reference, then savings.
pub fn write_statement(statement: &Statement, config: &StatementConfig, branding: &Branding, path: &Path) -> Result<()> {
    let mut workbook = Workbook::new();
    let sheet = workbook.add_worksheet();
    sheet.set_name("Statement")?;
    // Printed as the PDF is: landscape, every column on the page.
    sheet.set_landscape();
    sheet.set_print_fit_to_pages(1, 0);

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

    let carried = cell.clone().set_italic().set_font_color(color("#666666", "#666666"));
    let carried_money = money.clone().set_italic().set_font_color(color("#666666", "#666666"));
    let org = Format::new().set_bold().set_font_size(14);
    let details = Format::new().set_font_size(10);

    // The letterhead: the logo at the top left, the society and the
    // statement beside it.
    let first = if letterhead_logo(sheet, branding) { 1 } else { 0 };
    let last = 7u16;
    sheet.merge_range(0, first, 0, last, &branding.org_name, &org)?;
    sheet.merge_range(1, first, 1, last, &format!("{} - {}", config.title, statement.name), &title)?;
    sheet.merge_range(2, first, 2, last - 2, &format!("For the period: {}", statement.period), &period)?;
    sheet.merge_range(2, last - 1, 2, last, if statement.active { "Active" } else { "Inactive" }, &status)?;
    let mut who: Vec<String> = Vec::new();
    if !statement.pf_no.is_empty() {
        who.push(format!("PF No: {}", statement.pf_no));
    }
    if !statement.phone.is_empty() {
        who.push(format!("Contact: {}", statement.phone));
    }
    if !who.is_empty() {
        sheet.merge_range(3, first, 3, last, &who.join("    "), &details)?;
    }
    for i in 0..config.columns.len().max(8) {
        sheet.set_column_width(col(i), 15.0)?;
    }

    let mut row = 5u32;
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
                    let (text, number) = if line.brought_forward { (&carried, &carried_money) } else { (&cell, &money) };
                    for (i, name) in config.columns.iter().enumerate() {
                        write_value(sheet, row, col(i), &loan_value(line, name), text, number)?;
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
            let (text, number) = if line.brought_forward { (&carried, &carried_money) } else { (&cell, &money) };
            for (i, name) in config.savings_columns.iter().enumerate() {
                write_value(sheet, row, col(i), &savings_value(line, name), text, number)?;
            }
            row += 1;
        }
    }
    workbook.save(path)?;
    Ok(())
}
