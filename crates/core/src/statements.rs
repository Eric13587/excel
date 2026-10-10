//! A member's statement: their loans and savings over a period, laid out for
//! printing. The `reports` crate writes it as a PDF or a workbook.
//!
//! Each loan is replayed from its first entry, so the running figures at the
//! start of the period are right even when the period begins mid-loan, and a
//! "Balance b/f" line carries them in. The "gross" figure is principal plus
//! all the interest the loan was written to charge, less what has been paid —
//! at the loan's own rate.

use chrono::{Datelike, NaiveDate};

use crate::Journal;
use crate::dates;
use crate::db::model::{Fund, LedgerRow, Suspension, event};
use crate::error::{Error, Result};
use crate::loans::service::loan_rate;
use crate::money::{ceil, round2, total};
use crate::notes;

/// The loan columns a statement can show, in their default order.
pub const LOAN_COLUMNS: [&str; 8] = ["Date", "Type", "Debit", "Interest", "Credit", "Balance", "Gross", "Notes"];
/// The savings columns a statement can show, in their default order.
pub const SAVINGS_COLUMNS: [&str; 5] = ["Date", "Type", "Amount", "Balance", "Notes"];

/// Which loans a statement shows.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum LoanScope {
    /// Loans with something still owed when the period closes.
    Active,
    /// Every loan with an entry in the period, or a balance carried into it.
    #[default]
    All,
}

#[derive(Debug, Clone, PartialEq)]
pub struct StatementConfig {
    pub show_loans: bool,
    pub show_savings: bool,
    pub loans: LoanScope,
    pub title: String,
    pub footer: String,
    /// How the period and statement date are written (`strftime`).
    pub date_format: String,
    pub columns: Vec<String>,
    pub savings_columns: Vec<String>,
}

impl Default for StatementConfig {
    fn default() -> Self {
        StatementConfig {
            show_loans: true,
            show_savings: true,
            loans: LoanScope::All,
            title: "ACCOUNT STATEMENT".into(),
            footer: String::new(),
            date_format: "%B %d, %Y".into(),
            columns: LOAN_COLUMNS.map(String::from).to_vec(),
            savings_columns: SAVINGS_COLUMNS.map(String::from).to_vec(),
        }
    }
}

/// One line of a loan section: an entry, or a greyed note across the table.
#[derive(Debug, Clone, PartialEq)]
pub struct LoanLine {
    pub date: String,
    pub event_type: String,
    pub debit: f64,
    pub interest: f64,
    pub credit: f64,
    pub balance: f64,
    /// Shown for disbursements and payments only.
    pub gross: Option<f64>,
    pub notes: String,
    /// A note spanning the row — a suspension — instead of an entry.
    pub annotation: Option<String>,
    /// The loan as it stood when the period began, carried in.
    pub brought_forward: bool,
}

/// The line that carries a balance into the period.
pub const BROUGHT_FORWARD: &str = "Balance b/f";

#[derive(Debug, Clone, PartialEq)]
pub struct LoanSection {
    pub reference: String,
    pub lines: Vec<LoanLine>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SavingsLine {
    pub date: String,
    pub kind: String,
    /// Negative for a withdrawal.
    pub amount: f64,
    pub balance: f64,
    pub notes: String,
    /// The savings as they stood when the period began, carried in.
    pub brought_forward: bool,
}

impl SavingsLine {
    pub fn is_withdrawal(&self) -> bool {
        self.amount < 0.0
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Statement {
    pub member_id: i64,
    pub name: String,
    pub pf_no: String,
    pub phone: String,
    pub from: String,
    pub to: String,
    /// "January 01, 2026 to March 31, 2026", in the configured format.
    pub period: String,
    /// Active while the member owes on a loan or holds savings.
    pub active: bool,
    pub loans: Vec<LoanSection>,
    pub savings: Vec<SavingsLine>,
    /// Owed at the period's end: principal plus interest charged.
    pub net_outstanding: f64,
    /// Owed at the period's end: principal plus all interest to be charged.
    pub gross_outstanding: f64,
    /// Savings at the period's end.
    pub savings_balance: f64,
}

impl Statement {
    /// Nothing to print for the period: no entries and no balance carried in.
    pub fn is_empty(&self) -> bool {
        self.loans.is_empty() && self.savings.is_empty()
    }
}

/// A member's name made safe for a file name: letters, digits, spaces, `-`
/// and `_`, at most 100 characters.
pub fn safe_file_name(name: &str) -> String {
    let safe: String = name.chars().filter(|c| c.is_alphanumeric() || matches!(c, ' ' | '-' | '_')).collect();
    let safe = safe.trim();
    let safe = if safe.is_empty() { if name.is_empty() { "Unknown" } else { "Statement" } } else { safe };
    safe.chars().take(100).collect()
}

/// `Ann_statement_20261009.pdf`, as 1.x named them.
pub fn pdf_file_name(name: &str, today: NaiveDate) -> String {
    format!("{}_statement_{}.pdf", safe_file_name(name), today.format("%Y%m%d"))
}

/// `Statement_Ann_2026-01-01_to_2026-03-31.xlsx`, as 1.x named them.
pub fn excel_file_name(name: &str, from: &str, to: &str) -> String {
    format!("Statement_{}_{from}_to_{to}.xlsx", safe_file_name(name))
}

/// A period widened to whole months: the 1st of the first month to the last
/// day of the last.
pub fn whole_months(from: NaiveDate, to: NaiveDate) -> (NaiveDate, NaiveDate) {
    let first = from.with_day(1).unwrap_or(from);
    let last = dates::add_days(dates::add_months(to.with_day(1).unwrap_or(to), 1), -1);
    (first, last)
}

fn display(date: &str, format: &str) -> String {
    dates::parse(date).map(|d| d.format(format).to_string()).unwrap_or_else(|| date.to_string())
}

/// `(year, month)` for each month from `start`'s to `end`'s, the last only
/// when `inclusive`.
fn months_spanned(start: NaiveDate, end: NaiveDate, inclusive: bool) -> Vec<(i32, u32)> {
    let mut out = Vec::new();
    let (mut y, mut m) = (start.year(), start.month());
    while (y, m) < (end.year(), end.month()) || (inclusive && (y, m) == (end.year(), end.month())) {
        out.push((y, m));
        (y, m) = if m == 12 { (y + 1, 1) } else { (y, m + 1) };
    }
    out
}

/// "Mar, Apr 2026; Jan 2027".
fn month_list(months: &[(i32, u32)]) -> String {
    let mut groups: Vec<(i32, Vec<&str>)> = Vec::new();
    for (y, m) in months {
        let short = &dates::month_name(*m)[..3];
        match groups.iter_mut().find(|(year, _)| year == y) {
            Some((_, names)) => names.push(short),
            None => groups.push((*y, vec![short])),
        }
    }
    groups.iter().map(|(y, names)| format!("{} {y}", names.join(", "))).collect::<Vec<_>>().join("; ")
}

/// Greyed lines for the suspensions on one loan that touch the period, each
/// naming only the months inside it.
fn suspension_lines(reference: &str, spans: &[Suspension], from: &str, to: &str, format: &str) -> Vec<LoanLine> {
    let (Some(lo), Some(hi)) = (dates::parse(from), dates::parse(to)) else { return Vec::new() };
    let window = ((lo.year(), lo.month()), (hi.year(), hi.month()));
    let mut lines: Vec<LoanLine> = Vec::new();
    for span in spans.iter().filter(|s| s.loan_ref.as_deref() == Some(reference)) {
        let start_text = dates::date_part(&span.start_date);
        let Some(start) = dates::parse(start_text) else { continue };
        let resumed = span.resumed_date.as_deref().map(dates::date_part).filter(|d| !d.is_empty());
        let until = span.suspend_until.as_deref().map(dates::date_part).filter(|d| !d.is_empty());
        let end = resumed.or(until);
        if start_text > to || end.is_some_and(|e| e < from) {
            continue;
        }
        let text = match end {
            Some(end_text) => {
                // The month it resumes in takes a deduction, so it is not named.
                let Some(end) = dates::parse(end_text) else { continue };
                let mut months = months_spanned(start, end, false);
                if months.is_empty() {
                    months = months_spanned(start, start, true);
                }
                months.retain(|m| *m >= window.0 && *m <= window.1);
                if months.is_empty() {
                    continue;
                }
                match (resumed, until) {
                    (None, Some(until)) => format!(
                        "Loan suspended — no deductions for {} (resumes {})",
                        month_list(&months),
                        display(until, format)
                    ),
                    _ => format!("Loan suspended — no deductions for {}", month_list(&months)),
                }
            }
            None => format!("Loan suspended on {} — deductions paused indefinitely", display(start_text, format)),
        };
        if lines.iter().any(|l| l.annotation.as_deref() == Some(&text)) {
            continue;
        }
        lines.push(LoanLine {
            date: start_text.max(from).to_string(),
            event_type: "Loan Suspended".into(),
            debit: 0.0,
            interest: 0.0,
            credit: 0.0,
            balance: 0.0,
            gross: None,
            notes: String::new(),
            annotation: Some(text),
            brought_forward: false,
        });
    }
    lines
}

/// A member's statement from `from` to `to` (inclusive, `YYYY-MM-DD`).
pub fn prepare(journal: &Journal, member_id: i64, from: &str, to: &str, config: &StatementConfig) -> Result<Statement> {
    let member = journal.members().get(member_id)?.ok_or(Error::MemberNotFound(member_id))?;
    let (Some(start), Some(end)) = (dates::parse(from), dates::parse(to)) else {
        return Err(Error::invalid("Invalid date format. Expected YYYY-MM-DD."));
    };
    if start > end {
        return Err(Error::invalid("Start date cannot be after end date."));
    }
    let (from, to) = (dates::format(start), dates::format(end));
    let default_rate = journal.settings().default_interest_rate();
    let suspensions = journal.loans().suspensions_for_member(member_id)?;
    let savings_now = journal.funds().balance(Fund::Savings, member_id)?;
    let active = !journal.loans().active_for_member(member_id)?.is_empty() || savings_now != 0.0;

    let mut loans = Vec::new();
    let (mut net, mut gross_total) = (0.0, 0.0);
    if config.show_loans {
        let ledger = journal.ledger().for_member(member_id)?;
        let mut references: Vec<String> =
            ledger.iter().map(|r| if r.loan_ref.is_empty() { "-".to_string() } else { r.loan_ref.clone() }).collect();
        references.sort();
        references.dedup();
        for reference in references {
            let rate = match journal.loans().by_ref(member_id, &reference)? {
                Some(loan) => loan_rate(journal, &loan),
                None => default_rate,
            };
            let entries: Vec<&LedgerRow> =
                ledger.iter().filter(|r| r.loan_ref == reference || (r.loan_ref.is_empty() && reference == "-")).collect();
            let mut lines = Vec::new();
            let (mut gross, mut at_end) = (0.0, (0.0, 0.0));
            // Where the loan stood when the period began, if it began earlier.
            let mut carried: Option<(f64, f64)> = None;
            for row in entries {
                if dates::date_part(&row.date) > to.as_str() {
                    break;
                }
                // Kept to the cent: 100,000 × 1.1 is 110,000.00000000001 in
                // binary, which rounds up to a shilling too many.
                match row.event_type.as_str() {
                    event::LOAN_ISSUED | event::TOP_UP => gross = round2(gross + row.added + round2(row.added * rate)),
                    event::REPAYMENT | event::BUYOFF => gross = round2(gross - row.deducted),
                    _ => {}
                }
                at_end = (row.balance, gross);
                if dates::date_part(&row.date) < from.as_str() {
                    carried = Some(at_end);
                    continue;
                }
                let shows_gross =
                    [event::LOAN_ISSUED, event::TOP_UP, event::REPAYMENT, event::BUYOFF].contains(&row.event_type.as_str());
                lines.push(LoanLine {
                    date: row.date.clone(),
                    event_type: notes::loan_entry(&row.event_type).to_string(),
                    debit: ceil(row.added),
                    interest: ceil(row.interest_amount),
                    credit: ceil(row.deducted),
                    balance: ceil(row.balance),
                    gross: shows_gross.then(|| ceil(gross)),
                    notes: notes::loan_note(&row.notes),
                    annotation: None,
                    brought_forward: false,
                });
            }
            let notes = suspension_lines(&reference, &suspensions, &from, &to, &config.date_format);
            if !notes.is_empty() {
                lines.extend(notes);
                lines.sort_by(|a, b| a.date.cmp(&b.date));
            }
            // A loan still owing when the period began opens with what it owed.
            if let Some((balance, gross)) = carried.filter(|(balance, _)| *balance > 0.005) {
                lines.insert(
                    0,
                    LoanLine {
                        date: from.clone(),
                        event_type: BROUGHT_FORWARD.into(),
                        debit: 0.0,
                        interest: 0.0,
                        credit: 0.0,
                        balance: ceil(balance),
                        gross: Some(ceil(gross)),
                        notes: String::new(),
                        annotation: None,
                        brought_forward: true,
                    },
                );
            }
            let shown = match config.loans {
                LoanScope::All => !lines.is_empty(),
                LoanScope::Active => at_end.0 > 0.005,
            };
            if shown {
                loans.push(LoanSection { reference: reference.clone(), lines });
            }
            // What the loan stood at when the period closed, even with no
            // entry inside the period, as long as something was owed.
            if at_end.0 > 0.0 {
                net = round2(net + at_end.0);
                gross_total = round2(gross_total + at_end.1);
            }
        }
    }

    let history = journal.funds().rows(Fund::Savings, member_id, None, None)?;
    let mut running = 0.0;
    let mut savings_balance = 0.0;
    let mut savings = Vec::new();
    let mut carried: Option<f64> = None;
    for row in &history {
        running = total([running, row.signed()]);
        let date = dates::date_part(&row.date);
        if date < from.as_str() {
            carried = Some(running);
        }
        if date <= to.as_str() {
            savings_balance = running;
        }
        if config.show_savings && date >= from.as_str() && date <= to.as_str() {
            savings.push(SavingsLine {
                date: row.date.clone(),
                kind: row.transaction_type.clone(),
                amount: row.signed(),
                balance: running,
                notes: notes::fund_note(&row.transaction_type, &row.notes),
                brought_forward: false,
            });
        }
    }
    if config.show_savings
        && let Some(balance) = carried.filter(|balance| balance.abs() > 0.005)
    {
        savings.insert(
            0,
            SavingsLine { date: from.clone(), kind: BROUGHT_FORWARD.into(), amount: 0.0, balance, notes: String::new(), brought_forward: true },
        );
    }

    Ok(Statement {
        member_id,
        period: format!("{} to {}", display(&from, &config.date_format), display(&to, &config.date_format)),
        pf_no: member.pf_no,
        name: member.name,
        phone: member.phone,
        from,
        to,
        active,
        loans,
        savings,
        net_outstanding: net,
        gross_outstanding: gross_total,
        savings_balance,
    })
}

#[cfg(test)]
mod tests;
