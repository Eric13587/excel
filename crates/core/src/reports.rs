//! The figures behind every exported report, as tables.
//!
//! A report here is a [`Table`] of rows and columns; the `reports` crate
//! writes it as a workbook, a CSV file or a PDF. Members are listed in the
//! order they were added, as LoanMaster 1.x listed them, which is usually
//! payroll order. Money is rounded up to whole shillings, as 1.x rounded it.

use chrono::{Datelike, NaiveDate};

use crate::Journal;
use crate::arrears::{ArrearsRow, summarize};
use crate::dates;
use crate::db::model::{Fund, LedgerRow, Member, WITHDRAWAL, event};
use crate::error::{Error, Result};
use crate::money::{ceil, total};

/// One cell of a report.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Text(String),
    Number(f64),
    Blank,
}

impl Value {
    pub fn text(text: impl Into<String>) -> Value {
        Value::Text(text.into())
    }

    pub fn number(&self) -> Option<f64> {
        match self {
            Value::Number(n) => Some(*n),
            _ => None,
        }
    }
}

/// A report: a title, the columns in order, the rows, and a totals row.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Table {
    pub title: String,
    /// Under the title: the period, or the date the listing stands at.
    pub subtitle: String,
    /// The workbook's sheet name — at most 31 characters, as Excel requires.
    pub sheet: String,
    pub columns: Vec<String>,
    pub rows: Vec<Vec<Value>>,
    /// Shown bold and shaded beneath the rows.
    pub total: Option<Vec<Value>>,
}

impl Table {
    /// Whether a column holds money (right-aligned, thousands separated).
    pub fn is_numeric(&self, column: usize) -> bool {
        self.rows.iter().any(|r| matches!(r.get(column), Some(Value::Number(_))))
            && self.rows.iter().all(|r| matches!(r.get(column), Some(Value::Number(_)) | Some(Value::Blank) | None))
    }
}

/// Called once per member: `(done, of, what)`. Returning `false` stops the
/// report with [`Error::Cancelled`].
pub type Progress<'a> = &'a mut dyn FnMut(usize, usize, &str) -> bool;

/// A report row before the user's column choice is applied.
type Row = Vec<(String, Value)>;

/// The chosen columns in the chosen order (all of `default` when none were
/// chosen), and each row cut to them. A chosen column the report does not
/// produce stays blank.
fn project(default: &[String], chosen: Option<&[String]>, rows: Vec<Row>) -> (Vec<String>, Vec<Vec<Value>>) {
    let columns: Vec<String> = chosen.map(<[String]>::to_vec).unwrap_or_else(|| default.to_vec());
    let rows = rows
        .into_iter()
        .map(|row| {
            columns
                .iter()
                .map(|c| row.iter().find(|(k, _)| k == c).map(|(_, v)| v.clone()).unwrap_or(Value::Blank))
                .collect()
        })
        .collect();
    (columns, rows)
}

/// A TOTAL row: every money column summed, `Name` reading TOTAL. None when
/// there is nothing to total.
fn totals(columns: &[String], rows: &[Vec<Value>]) -> Option<Vec<Value>> {
    if rows.is_empty() {
        return None;
    }
    Some(
        columns
            .iter()
            .enumerate()
            .map(|(i, c)| {
                if c == "Name" {
                    return Value::text("TOTAL");
                }
                let numbers: Vec<f64> = rows.iter().filter_map(|r| r[i].number()).collect();
                if numbers.is_empty() { Value::Blank } else { Value::Number(total(numbers)) }
            })
            .collect(),
    )
}

fn members_in_order(journal: &Journal) -> Result<Vec<Member>> {
    let mut members = journal.members().all()?;
    members.sort_by_key(|m| m.id);
    Ok(members)
}

fn step(progress: &mut Progress<'_>, done: usize, of: usize, member: &Member) -> Result<()> {
    if progress(done, of, &format!("Processing {}...", member.name)) { Ok(()) } else { Err(Error::Cancelled) }
}

fn retired_before(member: &Member, date: &str) -> bool {
    member.is_retired && member.retired_date.as_deref().is_some_and(|d| !d.is_empty() && d < date)
}

// --------------------------------------------------------------- quarters

/// The first day of the financial year holding `date`.
pub fn fy_start(journal: &Journal, date: NaiveDate) -> NaiveDate {
    let month = journal.settings().fy_start_month();
    let year = if date.month() >= month { date.year() } else { date.year() - 1 };
    NaiveDate::from_ymd_opt(year, month, 1).unwrap_or(date)
}

/// The last day of the financial year holding `date`.
pub fn fy_end(journal: &Journal, date: NaiveDate) -> NaiveDate {
    dates::add_days(dates::add_months(fy_start(journal, date), 12), -1)
}

/// The months a quarter may start in, given the financial year's first month.
fn quarter_months(journal: &Journal) -> [u32; 4] {
    let first = journal.settings().fy_start_month();
    [0, 3, 6, 9].map(|offset| (first - 1 + offset) % 12 + 1)
}

/// Every quarter start from the year before the journal's first entry to the
/// year after `today`, oldest first.
pub fn quarter_starts(journal: &Journal, today: NaiveDate) -> Result<Vec<NaiveDate>> {
    let earliest: Option<String> = journal.conn().query_row("SELECT MIN(date) FROM ledger", [], |r| r.get(0))?;
    let first_year = earliest.as_deref().and_then(dates::parse).map(|d| d.year()).unwrap_or(today.year());
    let months = quarter_months(journal);
    let mut starts: Vec<NaiveDate> = (first_year - 1..=today.year() + 1)
        .flat_map(|year| {
            let first = months[0];
            months.map(move |m| NaiveDate::from_ymd_opt(if m < first { year + 1 } else { year }, m, 1))
        })
        .flatten()
        .collect();
    starts.sort();
    starts.dedup();
    Ok(starts)
}

/// The latest quarter to have begun by `today`.
pub fn default_quarter(journal: &Journal, today: NaiveDate) -> Result<NaiveDate> {
    Ok(quarter_starts(journal, today)?
        .into_iter()
        .rfind(|d| *d <= today)
        .unwrap_or_else(|| today.with_day(1).unwrap_or(today)))
}

/// A quarter's start: the first of a month the financial year starts a
/// quarter in.
pub fn check_quarter_start(journal: &Journal, start: &str) -> Result<NaiveDate> {
    let date = dates::parse(start).ok_or_else(|| Error::invalid(format!("{start} is not a date (use YYYY-MM-DD).")))?;
    if date.day() != 1 {
        return Err(Error::invalid("Start date must be the 1st day of the month."));
    }
    let mut months = quarter_months(journal);
    if !months.contains(&date.month()) {
        months.sort();
        return Err(Error::invalid(format!(
            "Invalid quarter start month ({}).\nBased on FY start ({}), quarters must start in: {}.",
            dates::month_name(date.month()),
            dates::month_name(journal.settings().fy_start_month()),
            months.map(dates::month_name).join(", ")
        )));
    }
    Ok(date)
}

/// A quarter's three months: `[start, next)` each, and their labels.
struct Quarter {
    bounds: [(String, String); 3],
    labels: [String; 3],
    start: String,
    end: String,
    period: String,
}

impl Quarter {
    fn new(start: NaiveDate) -> Quarter {
        let month = |n: i32| dates::add_months(start, n);
        Quarter {
            bounds: [0, 1, 2].map(|n| (dates::format(month(n)), dates::format(month(n + 1)))),
            labels: [0, 1, 2].map(|n| month(n).format("%b-%y").to_string()),
            start: dates::format(start),
            end: dates::format(month(3)),
            period: format!("Period: {} - {}", start.format("%b %Y"), month(2).format("%b %Y")),
        }
    }
}

/// "B/F 31ST JUL 2025": the day before the quarter, as the shares report heads it.
fn brought_forward_label(start: NaiveDate) -> String {
    let day = dates::add_days(start, -1);
    let suffix = match day.day() {
        11..=13 => "TH",
        d if d % 10 == 1 => "ST",
        d if d % 10 == 2 => "ND",
        d if d % 10 == 3 => "RD",
        _ => "TH",
    };
    format!("B/F {}{suffix} {}", day.day(), day.format("%b %Y")).to_uppercase()
}

// ------------------------------------------------------ interest earned

/// A loan's principal outstanding as a row recorded it. A loan with no
/// interest charges at all predates the split balances, and its total stands
/// in.
fn principal_at(row: &LedgerRow, has_charges: bool) -> f64 {
    if row.principal_balance > 0.0 {
        row.principal_balance
    } else if !has_charges && row.balance > 0.0 {
        row.balance
    } else {
        row.principal_balance
    }
}

/// The quarterly interest report: per loan, the principal outstanding at the
/// quarter's end, interest brought forward from the financial year's start,
/// and the interest of each month.
pub fn loan_interest(journal: &Journal, start: &str, columns: Option<&[String]>, mut progress: Progress<'_>) -> Result<Table> {
    let start_date = check_quarter_start(journal, start)?;
    let q = Quarter::new(start_date);
    let fy = dates::format(fy_start(journal, start_date));
    let bf_label = format!("B/F at {}", start_date.format("%b %Y"));
    let default: Vec<String> = ["Name", "Loan Amount", &bf_label, &q.labels[0], &q.labels[1], &q.labels[2], "Sub Total", "Grand Total"]
        .map(String::from)
        .to_vec();

    let members = members_in_order(journal)?;
    let mut rows: Vec<Row> = Vec::new();
    for (i, member) in members.iter().enumerate() {
        step(&mut progress, i + 1, members.len(), member)?;
        if retired_before(member, &q.start) && !journal.loans().has_active(member.id)? {
            continue;
        }
        let ledger = journal.ledger().for_member(member.id)?;
        let mut references: Vec<&str> = Vec::new();
        for row in &ledger {
            if !references.contains(&row.loan_ref.as_str()) {
                references.push(&row.loan_ref);
            }
        }
        for reference in references.into_iter().filter(|r| !r.is_empty() && *r != "-") {
            let loan: Vec<&LedgerRow> = ledger.iter().filter(|r| r.loan_ref == reference).collect();
            let charges: Vec<&LedgerRow> = loan.iter().copied().filter(|r| r.event_type == event::INTEREST).collect();
            let has_charges = !charges.is_empty();
            // Interest as charged; a loan from before charges were recorded
            // reports what its repayments said they took.
            let income: Vec<&LedgerRow> =
                if has_charges { charges } else { loan.iter().copied().filter(|r| r.event_type == event::REPAYMENT).collect() };
            let between = |from: &str, to: &str| -> f64 {
                total(income.iter().filter(|r| r.date.as_str() >= from && r.date.as_str() < to).map(|r| r.interest_amount))
            };
            let bf = if q.start == fy { 0.0 } else { between(&fy, &q.start) };
            let months = q.bounds.clone().map(|(from, to)| between(&from, &to));
            let balance = loan.iter().rfind(|r| r.date.as_str() < q.end.as_str()).map(|r| principal_at(r, has_charges)).unwrap_or(0.0);
            if balance == 0.0 && bf == 0.0 && months.iter().all(|m| *m == 0.0) {
                continue;
            }
            let (bf, months) = (ceil(bf), months.map(ceil));
            let sub_total = total(months);
            rows.push(vec![
                ("Name".into(), Value::text(&member.name)),
                ("Loan Amount".into(), Value::Number(ceil(balance))),
                (bf_label.clone(), Value::Number(bf)),
                (q.labels[0].clone(), Value::Number(months[0])),
                (q.labels[1].clone(), Value::Number(months[1])),
                (q.labels[2].clone(), Value::Number(months[2])),
                ("Sub Total".into(), Value::Number(sub_total)),
                ("Grand Total".into(), Value::Number(bf + sub_total)),
            ]);
        }
    }
    let (columns, rows) = project(&default, columns, rows);
    Ok(Table {
        title: "Quarterly Interest Report".into(),
        subtitle: q.period,
        sheet: "Quarterly Report".into(),
        total: totals(&columns, &rows),
        columns,
        rows,
    })
}

/// The columns the quarterly interest report offers for a quarter.
pub fn loan_interest_columns(start: NaiveDate) -> Vec<String> {
    let q = Quarter::new(start);
    let bf = format!("B/F at {}", start.format("%b %Y"));
    ["Name", "Loan Amount", &bf, &q.labels[0], &q.labels[1], &q.labels[2], "Sub Total", "Grand Total"].map(String::from).to_vec()
}

// ---------------------------------------------------------------- funds

/// What counts as money paid in, per fund.
fn credits(fund: Fund) -> &'static [&'static str] {
    match fund {
        Fund::Benevolent => &["Contribution"],
        _ => &["Deposit", "Interest"],
    }
}

/// What a fund report is called.
pub fn fund_label(fund: Fund) -> &'static str {
    match fund {
        Fund::Savings => "Savings / Shares",
        Fund::Christmas => "Christmas",
        Fund::Benevolent => "Benevolent",
    }
}

/// The columns the savings quarterly report offers.
pub fn savings_quarter_columns(start: NaiveDate) -> Vec<String> {
    let q = Quarter::new(start);
    let bf = brought_forward_label(start);
    ["Name", &bf, &q.labels[0], &q.labels[1], &q.labels[2], "Sub Total", "Cash Out", "Grand Total"].map(String::from).to_vec()
}

/// The quarterly shares report: balance brought forward, each month's
/// deposits, and what was withdrawn in the quarter.
pub fn savings_quarter(journal: &Journal, start: &str, columns: Option<&[String]>, mut progress: Progress<'_>) -> Result<Table> {
    let start_date = check_quarter_start(journal, start)?;
    let q = Quarter::new(start_date);
    let default = savings_quarter_columns(start_date);
    let bf_label = default[1].clone();
    let paid_in = credits(Fund::Savings);

    let members = members_in_order(journal)?;
    let mut rows: Vec<Row> = Vec::new();
    for (i, member) in members.iter().enumerate() {
        step(&mut progress, i + 1, members.len(), member)?;
        if retired_before(member, &q.start) {
            continue;
        }
        let history = journal.funds().rows(Fund::Savings, member.id, None, None)?;
        if history.is_empty() {
            continue;
        }
        let sum = |from: Option<&str>, to: &str, kinds: &[&str]| -> f64 {
            total(
                history
                    .iter()
                    .filter(|r| from.is_none_or(|f| r.date.as_str() >= f) && r.date.as_str() < to && kinds.contains(&r.transaction_type.as_str()))
                    .map(|r| r.amount),
            )
        };
        let bf = sum(None, &q.start, paid_in) - sum(None, &q.start, &[WITHDRAWAL]);
        let months = q.bounds.clone().map(|(from, to)| sum(Some(&from), &to, paid_in));
        let cash_out = sum(Some(&q.start), &q.end, &[WITHDRAWAL]);
        if bf == 0.0 && months.iter().all(|m| *m == 0.0) && cash_out == 0.0 {
            continue;
        }
        let (bf, months, cash_out) = (ceil(bf), months.map(ceil), ceil(cash_out));
        let sub_total = total(months);
        rows.push(vec![
            ("Name".into(), Value::text(&member.name)),
            (bf_label.clone(), Value::Number(bf)),
            (q.labels[0].clone(), Value::Number(months[0])),
            (q.labels[1].clone(), Value::Number(months[1])),
            (q.labels[2].clone(), Value::Number(months[2])),
            ("Sub Total".into(), Value::Number(sub_total)),
            ("Cash Out".into(), Value::Number(cash_out)),
            ("Grand Total".into(), Value::Number(bf + sub_total - cash_out)),
        ]);
    }
    let (columns, rows) = project(&default, columns, rows);
    Ok(Table {
        title: "Quarterly Savings Report".into(),
        subtitle: q.period,
        sheet: "Quarterly Savings Report".into(),
        total: totals(&columns, &rows),
        columns,
        rows,
    })
}

/// `(label, first day, first day of the next month)` for each month the
/// period touches.
fn months_between(start: NaiveDate, end: NaiveDate) -> Vec<(String, String, String)> {
    let mut out = Vec::new();
    let mut cursor = start.with_day(1).unwrap_or(start);
    let last = end.with_day(1).unwrap_or(end);
    while cursor <= last {
        let next = dates::add_months(cursor, 1);
        out.push((cursor.format("%b-%y").to_string(), dates::format(cursor), dates::format(next)));
        cursor = next;
    }
    out
}

/// The columns a fund report over a period offers.
pub fn fund_period_columns(start: NaiveDate, end: NaiveDate) -> Vec<String> {
    let mut columns: Vec<String> = ["PF No", "Name", "Employment Status"].map(String::from).to_vec();
    if end >= start {
        columns.extend(months_between(start, end).into_iter().map(|(label, _, _)| label));
    }
    columns.push("Total".into());
    columns
}

/// A fund over any period: each member's payments in, month by month, laid
/// out as the society's own spreadsheets are (PF No, Name, status, months,
/// Total). A member who retired stays listed, as a row of zeros, until the
/// end of the financial year they retired in.
pub fn fund_period(
    journal: &Journal,
    fund: Fund,
    start: &str,
    end: &str,
    columns: Option<&[String]>,
    mut progress: Progress<'_>,
) -> Result<Table> {
    let start_date = dates::parse(start).ok_or_else(|| Error::invalid(format!("{start} is not a date (use YYYY-MM-DD).")))?;
    let end_date = dates::parse(end).ok_or_else(|| Error::invalid(format!("{end} is not a date (use YYYY-MM-DD).")))?;
    if end_date < start_date {
        return Err(Error::invalid("End date is before start date."));
    }
    let months = months_between(start_date, end_date);
    let default = fund_period_columns(start_date, end_date);
    let paid_in = credits(fund);
    let start_text = dates::format(start_date);

    let members = members_in_order(journal)?;
    let mut rows: Vec<Row> = Vec::new();
    for (i, member) in members.iter().enumerate() {
        step(&mut progress, i + 1, members.len(), member)?;
        let gone = member.is_retired
            && member
                .retired_date
                .as_deref()
                .and_then(dates::parse)
                .is_some_and(|retired| start_text > dates::format(fy_end(journal, retired)));
        if gone {
            continue;
        }
        let history = journal.funds().rows(fund, member.id, None, None)?;
        let enrolled = fund == Fund::Benevolent && journal.funds().enrolment(member.id)?.is_some();
        if history.is_empty() && !enrolled {
            continue;
        }
        let mut row: Row = vec![
            ("PF No".into(), Value::text(&member.pf_no)),
            ("Name".into(), Value::text(&member.name)),
            ("Employment Status".into(), Value::text(&member.employment_status)),
        ];
        let mut sum = 0.0;
        for (label, from, to) in &months {
            let paid = ceil(total(
                history
                    .iter()
                    .filter(|r| r.date.as_str() >= from.as_str() && r.date.as_str() < to.as_str() && paid_in.contains(&r.transaction_type.as_str()))
                    .map(|r| r.amount),
            ));
            sum += paid;
            row.push((label.clone(), Value::Number(paid)));
        }
        row.push(("Total".into(), Value::Number(sum)));
        rows.push(row);
    }
    let (columns, rows) = project(&default, columns, rows);
    let title = format!("{} Report", fund_label(fund));
    Ok(Table {
        subtitle: format!("Period: {} - {}", start_date.format("%b %Y"), end_date.format("%b %Y")),
        sheet: title.chars().take(31).collect(),
        title,
        total: totals(&columns, &rows),
        columns,
        rows,
    })
}

// -------------------------------------------------------------- members

/// What the members list can show: `(key, heading)`, in display order.
pub const MEMBER_COLUMNS: [(&str, &str); 14] = [
    ("name", "Name"),
    ("pf_no", "PF No"),
    ("id_no", "ID No"),
    ("phone", "Phone"),
    ("email", "Email"),
    ("employment_status", "Employment Status"),
    ("is_retired", "Retired"),
    ("retired_date", "Retired Date"),
    ("default_deduction", "Monthly Deduction"),
    ("savings", "Savings Balance"),
    ("loan_balance", "Loan Balance"),
    ("christmas", "Christmas Balance"),
    ("benevolent", "Benevolent Total"),
    ("created_at", "Created"),
];

fn member_value(journal: &Journal, key: &str, member: &Member) -> Result<Value> {
    let text = |s: &str| Value::text(s);
    Ok(match key {
        "name" => text(&member.name),
        "pf_no" => text(&member.pf_no),
        "id_no" => text(&member.id_no),
        "phone" => text(&member.phone),
        "email" => text(&member.email),
        "retired_date" => text(member.retired_date.as_deref().unwrap_or("")),
        "created_at" => text(&member.created_at),
        "employment_status" => text(if member.employment_status.is_empty() { "Active" } else { &member.employment_status }),
        "is_retired" => text(if member.is_retired { "Yes" } else { "No" }),
        "default_deduction" => Value::Number(ceil(member.default_deduction)),
        "savings" => Value::Number(ceil(journal.funds().balance(Fund::Savings, member.id)?)),
        "christmas" => Value::Number(ceil(journal.funds().balance(Fund::Christmas, member.id)?)),
        "benevolent" => Value::Number(ceil(journal.funds().balance(Fund::Benevolent, member.id)?)),
        "loan_balance" => Value::Number(ceil(total(journal.loans().active_for_member(member.id)?.iter().map(|l| l.balance)))),
        _ => Value::Blank,
    })
}

/// A directory of members with the chosen columns (keys from
/// [`MEMBER_COLUMNS`], in the order given), by name.
pub fn members_list(journal: &Journal, keys: &[&str], mut progress: Progress<'_>) -> Result<Table> {
    let mut keys: Vec<&str> = keys.iter().copied().filter(|k| MEMBER_COLUMNS.iter().any(|(c, _)| c == k)).collect();
    if keys.is_empty() {
        keys.push("name");
    }
    let heading = |key: &str| MEMBER_COLUMNS.iter().find(|(k, _)| *k == key).map(|(_, h)| h.to_string()).unwrap_or_default();
    let columns: Vec<String> = keys.iter().map(|k| heading(k)).collect();
    let members = members_in_order(journal)?;
    let mut rows = Vec::new();
    for (i, member) in members.iter().enumerate() {
        step(&mut progress, i + 1, members.len(), member)?;
        rows.push(keys.iter().map(|k| member_value(journal, k, member)).collect::<Result<Vec<_>>>()?);
    }
    if let Some(at) = columns.iter().position(|c| c == "Name") {
        rows.sort_by(|a, b| match (&a[at], &b[at]) {
            (Value::Text(x), Value::Text(y)) => x.cmp(y),
            _ => std::cmp::Ordering::Equal,
        });
    }
    Ok(Table { title: "Members List".into(), subtitle: String::new(), sheet: "Members List".into(), columns, rows, total: None })
}

// -------------------------------------------------------------- arrears

/// The arrears listing's columns: `(key, heading)`, in printed order.
pub const ARREARS_COLUMNS: [(&str, &str); 12] = [
    ("name", "Member"),
    ("pf_no", "PF No"),
    ("phone", "Phone"),
    ("ref", "Loan"),
    ("next_due_date", "Due Since"),
    ("days", "Days"),
    ("months_missed", "Missed"),
    ("installment", "Instalment"),
    ("overdue_amount", "Overdue (est.)"),
    ("balance", "Balance"),
    ("band", "Classification"),
    ("status", "Status"),
];

/// The one-word state that explains why a loan is on the arrears list.
pub fn arrears_status(row: &ArrearsRow) -> String {
    if row.suspended {
        return if row.suspend_until.is_empty() {
            "Suspended (indefinite)".into()
        } else {
            format!("Suspended until {}", row.suspend_until)
        };
    }
    if row.retired {
        return "Retired".into();
    }
    if row.employment_status.is_empty() { "Active".into() } else { row.employment_status.clone() }
}

/// The arrears rows as shown — including any filter applied on screen — with
/// a totals line, since the first question asked of the list is "how much?".
pub fn arrears_report(rows: &[ArrearsRow], as_of: &str) -> Result<Table> {
    if rows.is_empty() {
        return Err(Error::invalid("There are no loans in arrears to report."));
    }
    let columns: Vec<String> = ARREARS_COLUMNS.iter().map(|(_, h)| h.to_string()).collect();
    let table_rows = rows
        .iter()
        .map(|r| {
            vec![
                Value::text(&r.name),
                Value::text(&r.pf_no),
                Value::text(&r.phone),
                Value::text(&r.reference),
                Value::text(&r.next_due_date),
                Value::Number(r.days as f64),
                Value::Number(r.months_missed as f64),
                Value::Number(r.installment),
                Value::Number(r.overdue_amount),
                Value::Number(r.balance),
                Value::text(r.band),
                Value::text(arrears_status(r)),
            ]
        })
        .collect();
    let summary = summarize(rows);
    let mut total = vec![Value::Blank; columns.len()];
    total[0] = Value::text(format!("TOTAL — {} member(s), {} loan(s)", summary.members, summary.loans));
    total[8] = Value::Number(summary.overdue_amount);
    total[9] = Value::Number(summary.balance);
    Ok(Table {
        title: "Loans in Arrears".into(),
        subtitle: format!("As at {as_of}"),
        sheet: "Arrears".into(),
        columns,
        rows: table_rows,
        total: Some(total),
    })
}

#[cfg(test)]
mod tests;
