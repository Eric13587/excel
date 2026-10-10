//! Importing members and contributions from a spreadsheet.
//!
//! Two sheet shapes, told apart by the header row:
//!
//! * a **roster** — one member per row with any of Name, ID No, PF No, Phone,
//!   Email, Employment Status. A recognised member is filled in; anyone else
//!   becomes a new member.
//! * a **contribution sheet** — Name, PF number and a column per month (three
//!   or more). Each month's figure becomes a Christmas deposit or a benevolent
//!   contribution, on the 1st of that month.
//!
//! Names are matched with [`super::names::NameMatcher`]. [`build_plan`]
//! writes nothing, so the plan can be shown and each row's action changed
//! before [`apply`] writes it — in one transaction, after a safety copy.

use std::collections::{BTreeSet, HashMap};
use std::path::Path;

use calamine::{Data, Reader, open_workbook_auto};

use super::names::{Candidate, NameMatcher, Verdict};
use super::rules;
use crate::Journal;
use crate::db::members::MemberDetails;
use crate::db::model::Fund;
use crate::error::{Error, Result};

/// A sheet as read: the header row and the rows under it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Sheet {
    pub headers: Vec<String>,
    pub rows: Vec<HashMap<String, Data>>,
}

/// A cell as text: trimmed, whole numbers without a decimal point.
pub fn cell_text(value: &Data) -> String {
    match value {
        Data::Empty | Data::Error(_) => String::new(),
        Data::String(s) | Data::DateTimeIso(s) | Data::DurationIso(s) => s.trim().to_string(),
        Data::Float(f) if f.fract() == 0.0 && f.abs() < 1e15 => format!("{}", *f as i64),
        Data::Float(f) => f.to_string(),
        Data::Int(i) => i.to_string(),
        Data::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        Data::DateTime(dt) => dt.as_datetime().map(|d| d.format("%Y-%m-%d %H:%M:%S").to_string()).unwrap_or_default(),
    }
}

/// A cell as an amount: `2,500` reads as 2500; anything else unreadable is 0.
fn cell_number(value: &Data) -> f64 {
    match value {
        Data::Float(f) => *f,
        Data::Int(i) => *i as f64,
        Data::String(s) => s.replace(',', "").trim().parse().unwrap_or(0.0),
        _ => 0.0,
    }
}

/// Read the first sheet of a workbook (`.xlsx`, `.xls` or `.ods`).
pub fn read_sheet(path: &Path) -> Result<Sheet> {
    let mut workbook = open_workbook_auto(path).map_err(|e| Error::invalid(format!("Could not read the spreadsheet: {e}")))?;
    let Some(first) = workbook.sheet_names().first().cloned() else { return Ok(Sheet::default()) };
    let range = workbook.worksheet_range(&first).map_err(|e| Error::invalid(format!("Could not read the spreadsheet: {e}")))?;
    let mut rows = range.rows();
    let Some(header) = rows.next() else { return Ok(Sheet::default()) };
    let headers: Vec<String> = header
        .iter()
        .enumerate()
        .map(|(i, h)| Some(cell_text(h)).filter(|t| !t.is_empty()).unwrap_or_else(|| format!("col{i}")))
        .collect();
    let rows = rows.map(|row| headers.iter().cloned().zip(row.iter().cloned()).collect()).collect();
    Ok(Sheet { headers, rows })
}

/// A column a roster can carry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Field {
    Name,
    IdNo,
    PfNo,
    Phone,
    Email,
    EmploymentStatus,
}

/// Words that identify each field's header, checked in this order.
const HINTS: [(Field, &[&str]); 6] = [
    (Field::Name, &["name"]),
    (Field::IdNo, &["idno", "id no", "id number", "national id"]),
    (Field::PfNo, &["pf", "employee number", "employee no", "emp no", "payroll"]),
    (Field::Phone, &["phone", "mobile", "tel"]),
    (Field::Email, &["email", "e-mail"]),
    (Field::EmploymentStatus, &["employment", "status"]),
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SheetKind {
    Roster,
    Contribution,
}

/// Which header holds what.
#[derive(Debug, Clone, PartialEq)]
pub struct Mapping {
    pub fields: Vec<(Field, String)>,
    /// `(header, month number)` for each month column.
    pub months: Vec<(String, u32)>,
    pub kind: SheetKind,
}

impl Mapping {
    pub fn header(&self, field: Field) -> Option<&str> {
        self.fields.iter().find(|(f, _)| *f == field).map(|(_, h)| h.as_str())
    }
}

/// The month a header names: its first three letters, whatever follows
/// (`Jan`, `JANUARY`, `Jan-26`).
pub fn month_of(header: &str) -> Option<u32> {
    let letters: String = header.to_lowercase().chars().filter(char::is_ascii_lowercase).take(3).collect();
    (letters.len() == 3).then_some(())?;
    (1..=12).find(|m| crate::dates::month_name(*m).to_lowercase().starts_with(&letters))
}

/// Work out what each column holds. Three or more month columns make a
/// contribution sheet.
pub fn detect_mapping(headers: &[String]) -> Mapping {
    let mut fields: Vec<(Field, String)> = Vec::new();
    for (field, hints) in HINTS {
        if let Some(header) = headers
            .iter()
            .find(|h| !fields.iter().any(|(_, used)| used == *h) && hints.iter().any(|hint| h.to_lowercase().contains(hint)))
        {
            fields.push((field, header.clone()));
        }
    }
    let mut months: Vec<(String, u32)> = Vec::new();
    for header in headers {
        if let Some(m) = month_of(header)
            && !months.iter().any(|(h, _)| h == header)
        {
            months.push((header.clone(), m));
        }
    }
    let kind = if months.len() >= 3 { SheetKind::Contribution } else { SheetKind::Roster };
    Mapping { fields, months, kind }
}

/// What to do with one row.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    /// Fill in this existing member.
    Update(i64),
    Create,
    Skip,
}

/// The member fields a row offers, each only when the sheet has it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct RowFields {
    pub id_no: Option<String>,
    pub pf_no: Option<String>,
    pub employment_status: Option<String>,
    pub phone: Option<String>,
    pub email: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PlanRow {
    pub source_name: String,
    pub verdict: Verdict,
    pub best: Option<Candidate>,
    pub action: Action,
    pub fields: RowFields,
    /// Contribution sheets: `(date, amount)` per month with money in it.
    pub months: Vec<(String, f64)>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Plan {
    pub kind: SheetKind,
    pub fund: Fund,
    pub rows: Vec<PlanRow>,
}

/// Each month column's date: the 1st of the month, the first month column
/// being in the financial year's first year and earlier months in the next.
fn month_dates(months: &[(String, u32)], year: i32) -> HashMap<u32, String> {
    let start = months.first().map(|(_, m)| *m).unwrap_or(1);
    months.iter().map(|(_, m)| (*m, format!("{:04}-{m:02}-01", if *m >= start { year } else { year + 1 }))).collect()
}

fn present(row: &HashMap<String, Data>, header: Option<&str>) -> Option<String> {
    header.and_then(|h| row.get(h)).map(cell_text).filter(|t| !t.is_empty())
}

/// Match each row to a member and propose an action, writing nothing.
/// `fund` and `year` matter only for a contribution sheet.
pub fn build_plan(journal: &Journal, sheet: &Sheet, mapping: &Mapping, fund: Fund, year: i32) -> Result<Plan> {
    let Some(name_header) = mapping.header(Field::Name) else {
        return Err(Error::invalid("No Name column detected."));
    };
    let matcher = NameMatcher::new(journal.members().all()?.into_iter().map(|m| (m.id, m.name)));
    let dates = month_dates(&mapping.months, year);
    let mut rows = Vec::new();
    for row in &sheet.rows {
        let Some(name) = present(row, Some(name_header)) else { continue };
        if name.eq_ignore_ascii_case("total") {
            continue;
        }
        let (verdict, best) = matcher.classify(&name);
        let action = match (&best, verdict) {
            (Some(b), Verdict::Match | Verdict::Review) => Action::Update(b.id),
            _ => Action::Create,
        };
        let mut fields = RowFields { pf_no: present(row, mapping.header(Field::PfNo)), ..RowFields::default() };
        let mut months = Vec::new();
        match mapping.kind {
            SheetKind::Roster => {
                fields.id_no = present(row, mapping.header(Field::IdNo));
                fields.employment_status = present(row, mapping.header(Field::EmploymentStatus));
                fields.phone = present(row, mapping.header(Field::Phone)).map(|p| p.split_whitespace().collect());
                fields.email = present(row, mapping.header(Field::Email));
            }
            SheetKind::Contribution => {
                for (header, month) in &mapping.months {
                    let amount = row.get(header).map(cell_number).unwrap_or(0.0);
                    if amount > 0.0 {
                        months.push((dates[month].clone(), amount));
                    }
                }
            }
        }
        rows.push(PlanRow { source_name: name, verdict, best, action, fields, months });
    }
    Ok(Plan { kind: mapping.kind, fund, rows })
}

/// What an import did.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Outcome {
    pub updated: usize,
    pub created: usize,
    pub skipped: usize,
    /// Contribution sheets: members credited, deposits written and their total.
    pub members_credited: usize,
    pub deposits: usize,
    pub total: f64,
    pub warnings: Vec<String>,
    /// Members the sheet marked as having left, and what was withdrawn from
    /// their savings because of it.
    pub left: Vec<(String, f64)>,
    /// The safety copy taken before writing.
    pub backup: Option<std::path::PathBuf>,
}

/// A PF or ID number another member already holds is not assigned.
fn free(journal: &Journal, column: &str, value: Option<&str>, member: Option<i64>, name: &str, warnings: &mut Vec<String>) -> Result<Option<String>> {
    let Some(value) = value.filter(|v| !v.is_empty()) else { return Ok(None) };
    let owner = if column == "pf_no" {
        journal.members().owner_of_pf_no(value, member)?
    } else {
        journal.members().owner_of_id_no(value, member)?
    };
    match owner {
        Some(owner) => {
            warnings.push(format!("{} '{value}' for {name} already used by {owner} — skipped.", column.replace('_', " ").to_uppercase()));
            Ok(None)
        }
        None => Ok(Some(value.to_string())),
    }
}

/// An employment status from a sheet, on the application's vocabulary
/// ("employed" is Active, "pensioner" Retired). An unknown one is left out.
fn status(raw: Option<&str>, name: &str, warnings: &mut Vec<String>) -> Option<String> {
    let raw = raw?;
    let mapped = rules::status_synonym(raw.trim());
    if mapped.is_none() {
        warnings.push(format!("{name}: employment status '{raw}' not recognised — left unchanged."));
    }
    mapped.map(String::from)
}

/// Write a plan: one transaction, after a safety copy of the journal. A
/// clash of PF or ID numbers skips that number and says so; it never clears
/// the member's own.
pub fn apply(journal: &Journal, plan: &Plan) -> Result<Outcome> {
    let mut out = Outcome { backup: journal.snapshot("pre-excel-import")?, ..Outcome::default() };
    journal.transaction(|| {
        let mut credited = BTreeSet::new();
        for row in &plan.rows {
            let name = row.source_name.as_str();
            let member = match row.action {
                Action::Skip => {
                    out.skipped += 1;
                    continue;
                }
                Action::Update(id) => {
                    let current = journal.members().get(id)?.ok_or(Error::MemberNotFound(id))?;
                    let f = &row.fields;
                    let pf = free(journal, "pf_no", f.pf_no.as_deref(), Some(id), name, &mut out.warnings)?;
                    let id_no = free(journal, "id_no", f.id_no.as_deref(), Some(id), name, &mut out.warnings)?;
                    let status = status(f.employment_status.as_deref(), name, &mut out.warnings);
                    let details = MemberDetails {
                        name: current.name.clone(),
                        phone: f.phone.clone().unwrap_or(current.phone),
                        email: f.email.clone().unwrap_or(current.email),
                        employment_status: status.unwrap_or_else(|| current.employment_status.clone()),
                        pf_no: pf.unwrap_or(current.pf_no),
                        id_no: id_no.unwrap_or(current.id_no),
                    };
                    journal.members().update(id, &details)?;
                    // Leaving by the sheet pays the savings out, as leaving
                    // by any other door does.
                    match crate::membership::settle_status_change(journal, id, &current.employment_status, &details.employment_status) {
                        Ok(withdrawn) if withdrawn > 0.0 => out.left.push((current.name.clone(), withdrawn)),
                        Ok(_) => {}
                        Err(e) if e.is_user_facing() => out.warnings.push(format!("{}: {e} Their savings were left in place.", current.name)),
                        Err(e) => return Err(e),
                    }
                    if plan.kind == SheetKind::Roster {
                        out.updated += 1;
                    }
                    id
                }
                Action::Create => {
                    let f = &row.fields;
                    let pf = free(journal, "pf_no", f.pf_no.as_deref(), None, name, &mut out.warnings)?;
                    let id_no = free(journal, "id_no", f.id_no.as_deref(), None, name, &mut out.warnings)?;
                    let status = status(f.employment_status.as_deref(), name, &mut out.warnings);
                    let id = journal.members().add(&MemberDetails {
                        name: name.to_string(),
                        phone: f.phone.clone().unwrap_or_default(),
                        email: f.email.clone().unwrap_or_default(),
                        employment_status: status.unwrap_or_default(),
                        pf_no: pf.unwrap_or_default(),
                        id_no: id_no.unwrap_or_default(),
                    })?;
                    out.created += 1;
                    id
                }
            };
            for (date, amount) in &row.months {
                let kind = if plan.fund == Fund::Benevolent { "Contribution" } else { "Deposit" };
                journal.funds().add(plan.fund, member, date, kind, crate::money::round2(*amount), "Excel import", None)?;
                out.deposits += 1;
                out.total += amount;
                credited.insert(member);
            }
        }
        for member in &credited {
            journal.funds().recalculate(plan.fund, *member)?;
            if plan.fund == Fund::Benevolent {
                crate::grid::fund::resync_benevolent_schedule(journal, *member)?;
            }
        }
        out.members_credited = credited.len();
        Ok(())
    })?;
    out.total = crate::money::round2(out.total);
    Ok(out)
}

#[cfg(test)]
mod tests;
