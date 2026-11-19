//! A loans × months grid over the deduction schedule.
//!
//! A loan's rows are the consequence of a schedule, not entries in a list, so
//! this never fabricates a posting: every deduction goes through
//! [`crate::loans::service::deduct`] — what the Deduct button does — and every
//! correction clears the derived split and re-derives the loan.
//!
//! What a cell accepts:
//! * the loan's **due month**, empty — a deduction (type a figure other than
//!   the instalment to take that instead), or one operation: `@T=20,000/12`
//!   tops up, `@B` buys off, `@S=3` suspends;
//! * a month with **one deduction** — a corrected figure, or clearing it, which
//!   is un-run or missed by the rules in [`crate::loans::deletions`];
//! * a **missed month** (interest charged, nothing paid) — a figure records it;
//! * anything else is refused, naming the month that *can* be worked on.
//!
//! Every applied change keeps a [`RestorePoint`] of its loan, so Undo Last
//! Save puts each loan back exactly, last change first.

use std::collections::{BTreeSet, HashMap};
use std::sync::LazyLock;

use rusqlite::params;

use super::ops::{Entry, Figure, Op, OpSet};
use super::{DeletePlan, Fingerprint};
use crate::Journal;
use crate::dates::{self, month_bounds, month_key, month_label};
use crate::db::model::{LOAN_ACTIVE, LOAN_PAID, event};
use crate::error::{Error, Result};
use crate::loans::deletions::{self, Removal, Timeline};
use crate::loans::guards;
use crate::loans::projection::{clear_split, project_loan, project_member};
use crate::loans::service::{self as loan_service, recalculate_default_deduction};
use crate::loans::suspensions;
use crate::money::{figure, round2, shillings};
use crate::policy::{self, Operation};
use crate::undo::RestorePoint;

// ------------------------------------------------------------------ ops

static REPAY: Op = Op {
    code: "R",
    name: "Repayment",
    words: &["r", "repayment", "repay", "deduction", "deduct", "d"],
    help: "The month's deduction — the default, so a bare figure means this. Type what was actually \
           taken if it was not the instalment.",
    value: Some(Figure::Money),
    arg: None,
};
static TOP_UP: Op = Op {
    code: "T",
    name: "Top-up",
    words: &["t", "topup"],
    help: "Lend more on this loan, re-termed over the months after the slash: @T=20,000/12.",
    value: Some(Figure::Money),
    arg: Some(Figure::Months),
};
static BUYOFF: Op = Op {
    code: "B",
    name: "Buy-off",
    words: &["b", "buyoff", "settle"],
    help: "Settle the loan in full this month — principal plus all interest. Takes no figure; the amount \
           owed is worked out.",
    value: None,
    arg: None,
};
static SUSPEND: Op = Op {
    code: "S",
    name: "Suspend",
    words: &["s", "suspend", "pause"],
    help: "Pause deductions for this many months, starting this month: @S=3. The term extends by the same.",
    value: Some(Figure::Months),
    arg: None,
};

/// What a loan cell understands. No negative figure: a loan is not repaid by
/// a withdrawal.
pub static OPS: LazyLock<OpSet> =
    LazyLock::new(|| OpSet::new(vec![&REPAY, &TOP_UP, &BUYOFF, &SUSPEND], &REPAY, None));

// ---------------------------------------------------------------- cells

#[derive(Debug, Clone, PartialEq)]
pub struct PaymentRow {
    pub id: i64,
    pub date: String,
    pub kind: String,
    pub amount: f64,
    pub notes: String,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Cell {
    pub net: f64,
    pub rows: Vec<PaymentRow>,
}

static EMPTY_CELL: Cell = Cell { net: 0.0, rows: Vec::new() };

impl Cell {
    pub fn row_id(&self) -> Option<i64> {
        (self.rows.len() == 1).then(|| self.rows[0].id)
    }

    pub fn fingerprint(&self) -> Fingerprint {
        let mut out: Fingerprint = self
            .rows
            .iter()
            .map(|r| (r.id, (round2(r.amount) * 100.0).round() as i64, dates::date_part(&r.date).into(), r.kind.clone()))
            .collect();
        out.sort();
        out
    }
}

/// Interest charged in a visible month.
#[derive(Debug, Clone, PartialEq)]
pub struct Accrual {
    pub id: i64,
    pub date: String,
    pub amount: f64,
}

/// One loan — the unit of a row. Two loans are two schedules, so a member
/// holding two is two rows.
#[derive(Debug, Clone, PartialEq)]
pub struct LoanRow {
    pub member_id: i64,
    pub loan_id: i64,
    pub reference: String,
    pub name: String,
    pub pf_no: String,
    pub installment: f64,
    pub balance: f64,
    /// When the loan is next due, stepped over fixed-term suspensions.
    pub next_due: Option<String>,
    /// `next_due_date` as stored — where Fill Forward starts counting.
    pub stored_due: Option<String>,
    pub status: String,
    pub is_suspended: bool,
    pub suspend_until: Option<String>,
    pub member_active: bool,
    pub excluded: bool,
    pub exclusion_note: String,
    pub issued_month: Option<String>,
    pub latest_payment_id: Option<i64>,
    pub cells: HashMap<String, Cell>,
    /// Every non-interest row on the loan, whole history — what a deletion is
    /// judged against.
    pub timeline: Timeline,
    /// Interest charged in the visible months.
    pub accruals: HashMap<String, Accrual>,
}

impl LoanRow {
    pub fn key(&self) -> i64 {
        self.loan_id
    }

    pub fn label(&self) -> String {
        format!("{} · {}", self.name, self.reference)
    }

    pub fn next_due_month(&self) -> Option<String> {
        self.next_due.as_deref().filter(|d| !d.is_empty()).map(month_key)
    }

    pub fn due_day(&self) -> u32 {
        self.next_due.as_deref().and_then(|d| d.get(8..10)).and_then(|d| d.parse().ok()).unwrap_or(1)
    }

    pub fn cell(&self, month: &str) -> &Cell {
        self.cells.get(month).unwrap_or(&EMPTY_CELL)
    }

    pub fn period_total(&self, months: &[String]) -> f64 {
        round2(months.iter().map(|m| self.cell(m).net).sum())
    }

    /// Interest charged this month, nothing paid, before the due date: a
    /// missed deduction, and the one earlier month a figure can go into.
    pub fn missed(&self, month: &str) -> bool {
        self.accruals.contains_key(month)
            && self.cell(month).rows.is_empty()
            && self.next_due_month().is_some_and(|due| month < due.as_str())
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct LoanGrid {
    pub months: Vec<String>,
    pub rows: Vec<LoanRow>,
    /// `loan_id -> windows`
    pub suspensions: HashMap<i64, Vec<(String, Option<String>)>>,
}

impl LoanGrid {
    pub fn grand_total(&self) -> f64 {
        round2(self.rows.iter().map(|r| r.period_total(&self.months)).sum())
    }
}

fn window_for<'a>(month: &str, windows: &'a [(String, Option<String>)], day: u32) -> Option<&'a (String, Option<String>)> {
    let due = dates::day_in_month(month, day);
    windows.iter().find(|(start, end)| due.as_str() >= start.as_str() && end.as_deref().is_none_or(|e| due.as_str() < e))
}

fn in_suspension(month: &str, windows: &[(String, Option<String>)], day: u32) -> bool {
    window_for(month, windows, day).is_some()
}

/// Step a due date past fixed-term suspensions, as a deduction would. An
/// open-ended suspension is left where it is: the loan is on hold.
fn skip_suspended(due: Option<&str>, windows: &[(String, Option<String>)]) -> Option<String> {
    let due = due.filter(|d| !d.is_empty())?;
    if windows.is_empty() {
        return Some(due.to_string());
    }
    let mut cursor = dates::parse(due)?;
    for _ in 0..600 {
        let date = dates::format(cursor);
        match windows.iter().find(|(s, e)| date.as_str() >= s.as_str() && e.as_deref().is_none_or(|e| date.as_str() < e)) {
            None => return Some(date),
            Some((_, None)) => return Some(dates::date_part(due).to_string()),
            Some(_) => cursor = dates::add_months(cursor, 1),
        }
    }
    Some(dates::date_part(due).to_string())
}

/// Read the loan grid for a list of month keys.
pub fn load(journal: &Journal, months: &[String], include_inactive: bool, include_paid: bool, include_excluded: bool) -> Result<LoanGrid> {
    let Some((start, end)) = month_bounds(months) else { return Ok(LoanGrid::default()) };
    let conn = journal.conn();

    let mut payments: HashMap<(i64, String), HashMap<String, Vec<PaymentRow>>> = HashMap::new();
    {
        let mut stmt = conn.prepare(
            "SELECT id, individual_id, loan_id, date, event_type, deducted, notes FROM ledger \
             WHERE date >= ? AND date <= ? AND event_type IN ('Repayment', 'Loan Buyoff') \
             ORDER BY individual_id, date, id",
        )?;
        let rows = stmt.query_map(params![start, end], |r| {
            Ok((
                r.get::<_, i64>(1)?,
                r.get::<_, String>(2)?,
                PaymentRow {
                    id: r.get(0)?,
                    date: r.get(3)?,
                    kind: r.get(4)?,
                    amount: round2(r.get::<_, Option<f64>>(5)?.unwrap_or(0.0)),
                    notes: r.get::<_, Option<String>>(6)?.unwrap_or_default(),
                },
            ))
        })?;
        for row in rows {
            let (member, reference, payment) = row?;
            payments.entry((member, reference)).or_default().entry(month_key(&payment.date)).or_default().push(payment);
        }
    }
    let mut issued: HashMap<(i64, String), String> = HashMap::new();
    {
        let mut stmt = conn.prepare(
            "SELECT individual_id, loan_id, MIN(date) FROM ledger WHERE event_type='Loan Issued' \
             GROUP BY individual_id, loan_id",
        )?;
        for row in stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?)))? {
            let (m, reference, date) = row?;
            issued.insert((m, reference), month_key(&date));
        }
    }
    let mut timelines: HashMap<(i64, String), Timeline> = HashMap::new();
    let mut latest: HashMap<(i64, String), i64> = HashMap::new();
    {
        let mut stmt = conn.prepare(
            "SELECT individual_id, loan_id, date, id, event_type FROM ledger WHERE event_type != 'Interest Earned' \
             ORDER BY individual_id, loan_id, date, id",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, i64>(3)?, r.get::<_, String>(4)?))
        })?;
        for row in rows {
            let (m, reference, date, id, kind) = row?;
            if kind == event::REPAYMENT {
                latest.insert((m, reference.clone()), id);
            }
            timelines.entry((m, reference)).or_default().push((dates::date_part(&date).to_string(), id, kind));
        }
    }
    let mut accruals: HashMap<(i64, String), HashMap<String, Accrual>> = HashMap::new();
    {
        let mut stmt = conn.prepare(
            "SELECT id, individual_id, loan_id, date, added FROM ledger WHERE event_type='Interest Earned' \
             AND date >= ? AND date <= ? ORDER BY id",
        )?;
        let rows = stmt.query_map(params![start, end], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?, r.get::<_, String>(2)?, r.get::<_, String>(3)?, r.get::<_, Option<f64>>(4)?))
        })?;
        for row in rows {
            let (id, m, reference, date, added) = row?;
            accruals.entry((m, reference)).or_default().insert(
                month_key(&date),
                Accrual { id, date: dates::date_part(&date).to_string(), amount: round2(added.unwrap_or(0.0)) },
            );
        }
    }

    let excluded = journal.members().excluded_from("loans")?;
    let statuses = if include_paid { "('Active','Paid')" } else { "('Active')" };
    let mut stmt = conn.prepare(&format!(
        "SELECT l.id, l.individual_id, l.ref, l.installment, l.balance, l.next_due_date, l.status, \
         l.is_suspended, l.suspend_until, i.name, i.pf_no, i.employment_status, i.is_retired \
         FROM loans l JOIN individuals i ON i.id = l.individual_id WHERE l.status IN {statuses} \
         ORDER BY i.name COLLATE NOCASE, l.ref"
    ))?;
    let loans: Vec<_> = stmt
        .query_map([], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, Option<f64>>(3)?.unwrap_or(0.0),
                r.get::<_, Option<f64>>(4)?.unwrap_or(0.0),
                r.get::<_, Option<String>>(5)?,
                r.get::<_, String>(6)?,
                r.get::<_, Option<i64>>(7)?.unwrap_or(0) != 0,
                r.get::<_, Option<String>>(8)?,
                r.get::<_, Option<String>>(9)?.unwrap_or_default(),
                r.get::<_, Option<String>>(10)?.unwrap_or_default(),
                r.get::<_, Option<String>>(11)?.unwrap_or_default(),
                r.get::<_, Option<i64>>(12)?.unwrap_or(0) != 0,
            ))
        })?
        .collect::<rusqlite::Result<_>>()?;
    drop(stmt);

    let mut grid = LoanGrid { months: months.to_vec(), ..LoanGrid::default() };
    for (loan_id, member_id, reference, installment, balance, next_due, status, suspended, suspend_until, name, pf_no, employment, retired) in loans {
        let member_active = !retired && (employment.is_empty() || employment == "Active");
        if !include_inactive && !member_active {
            continue;
        }
        if excluded.contains_key(&member_id) && !include_excluded {
            continue;
        }
        let key = (member_id, reference.clone());
        let cells: HashMap<String, Cell> = payments
            .remove(&key)
            .unwrap_or_default()
            .into_iter()
            .map(|(month, rows)| (month, Cell { net: round2(rows.iter().map(|r| r.amount).sum()), rows }))
            .collect();
        let windows = suspensions::loan_windows(journal, loan_id)?;
        if !windows.is_empty() {
            grid.suspensions.insert(loan_id, windows.clone());
        }
        grid.rows.push(LoanRow {
            member_id,
            loan_id,
            reference: reference.clone(),
            name,
            pf_no,
            installment: round2(installment),
            balance: round2(balance),
            next_due: skip_suspended(next_due.as_deref(), &windows),
            stored_due: next_due,
            status,
            is_suspended: suspended,
            suspend_until,
            member_active,
            excluded: excluded.contains_key(&member_id),
            exclusion_note: excluded.get(&member_id).cloned().unwrap_or_default(),
            issued_month: issued.get(&key).cloned(),
            latest_payment_id: latest.get(&key).copied(),
            cells,
            timeline: timelines.remove(&key).unwrap_or_default(),
            accruals: accruals.remove(&key).unwrap_or_default(),
        });
    }
    Ok(grid)
}

// -------------------------------------------------------------- changes

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChangeKind {
    /// A new deduction in the due month.
    Deduct,
    /// A corrected figure on an existing deduction.
    Update,
    /// Remove a deduction — un-run or missed.
    Delete,
    TopUp,
    Buyoff,
    Suspend,
    /// The deduction for a missed month.
    Late,
}

/// One staged loan-cell edit.
#[derive(Debug, Clone, PartialEq)]
pub struct Change {
    pub loan_id: i64,
    pub member_id: i64,
    pub reference: String,
    pub month: String,
    pub kind: ChangeKind,
    pub amount: f64,
    pub row_id: Option<i64>,
    pub before: Fingerprint,
    /// Top-up: the new term. Suspend: how long.
    pub months: Option<i64>,
    /// Delete: as judged when staged; judged again when saved.
    pub removal: Option<Removal>,
    /// Late: the date the missed month was charged on.
    pub date: Option<String>,
}

impl Change {
    fn new(row: &LoanRow, month: &str, kind: ChangeKind) -> Change {
        Change {
            loan_id: row.loan_id,
            member_id: row.member_id,
            reference: row.reference.clone(),
            month: month.to_string(),
            kind,
            amount: 0.0,
            row_id: None,
            before: row.cell(month).fingerprint(),
            months: None,
            removal: None,
            date: None,
        }
    }

    /// The row a change belongs to: the loan, not the member.
    pub fn key(&self) -> i64 {
        self.loan_id
    }

    /// What this adds to the month's payments once saved. Only a deduction is
    /// a payment; a top-up lends, a suspension moves nothing, and a buy-off's
    /// figure is not known until it is worked out.
    pub fn effect(&self) -> f64 {
        match self.kind {
            ChangeKind::Deduct | ChangeKind::Update | ChangeKind::Late => self.amount,
            _ => 0.0,
        }
    }

    pub fn magnitude(&self) -> f64 {
        round2(self.amount.abs())
    }

    /// The change as a cell entry, for display.
    pub fn entry(&self) -> Entry {
        match self.kind {
            ChangeKind::TopUp => Entry { op: &TOP_UP, amount: self.amount, arg: self.months },
            ChangeKind::Buyoff => Entry::new(&BUYOFF, 0.0),
            ChangeKind::Suspend => Entry::new(&SUSPEND, self.months.unwrap_or(0) as f64),
            _ => Entry::new(&REPAY, self.amount),
        }
    }
}

/// Why a cell cannot be typed into, or `None`. Every refusal names the month
/// that *can* be worked on.
pub fn cell_refusal(grid: &LoanGrid, row: &LoanRow, month: &str) -> Option<String> {
    let cell = row.cell(month);
    match cell.rows.len() {
        0 => {}
        1 if cell.rows[0].kind == event::BUYOFF => {
            return Some("This is a buy-off, which settles the loan. Edit it in the ledger.".into());
        }
        1 => return None,
        _ => {
            return Some(
                "This month holds more than one payment, so one figure cannot stand for it. Open the member's \
                 ledger to work on them."
                    .into(),
            );
        }
    }
    if row.status != LOAN_ACTIVE {
        return Some(format!("{} is {}; it takes no more deductions.", row.reference, row.status.to_lowercase()));
    }
    let windows = grid.suspensions.get(&row.loan_id).map(Vec::as_slice).unwrap_or(&[]);
    if let Some((_, end)) = window_for(month, windows, row.due_day()) {
        return Some(match end {
            None => format!(
                "{} is suspended until further notice. Resume it in the member's ledger before deducting.",
                row.reference
            ),
            Some(end) => format!("{} is suspended for this month; deductions resume from {end}.", row.reference),
        });
    }
    if let Some(issued) = &row.issued_month
        && month < issued.as_str()
    {
        return Some(format!("{} was not issued until {}.", row.reference, month_label(issued)));
    }
    let Some(due) = row.next_due_month() else {
        return Some(format!("{} has no due date recorded.", row.reference));
    };
    if month < due.as_str() {
        if row.missed(month) {
            return None;
        }
        return Some(
            "This month was passed over and cannot be filled in here — the interest for it depends on the \
             balance at the time. Use the member's ledger."
                .into(),
        );
    }
    if month > due.as_str() {
        return Some(format!(
            "{} is due for {} first. A loan is paid in order — fill that month, or use Fill Forward.",
            row.reference,
            month_label(&due)
        ));
    }
    None
}

/// Turn typed text into a change. `Ok(None)` when there is nothing to do.
pub fn stage(grid: &LoanGrid, row: &LoanRow, month: &str, text: &str) -> std::result::Result<Option<Change>, String> {
    if let Some(refusal) = cell_refusal(grid, row, month) {
        return Err(refusal);
    }
    let cell = row.cell(month);
    let (entries, _append) = OPS.parse(text)?;
    // A zero figure is no figure: "0" is refused below, "@S=0" asks for nothing.
    let entries: Vec<Entry> = entries.into_iter().filter(|e| e.amount != 0.0 || e.op.value.is_none()).collect();
    if entries.len() > 1 {
        return Err("A loan month takes one thing — a deduction, or one operation. Save one, then type the next.".into());
    }
    let Some(entry) = entries.first().copied() else {
        if !matches!(text.trim(), "" | "-" | "—" | "–") {
            return Err("A deduction must be more than zero — clear the cell to remove it.".into());
        }
        let Some(payment) = cell.rows.first() else { return Ok(None) };
        let removal = deletions::removal_kind(&row.timeline, payment.id, &payment.date, &[])?;
        return Ok(Some(Change { row_id: Some(payment.id), removal: Some(removal), ..Change::new(row, month, ChangeKind::Delete) }));
    };

    if row.missed(month) {
        if !std::ptr::eq(entry.op, &REPAY) {
            return Err(format!(
                "This month was missed; it can take its deduction, but @{} goes in the month {} is due next — {}.",
                entry.op.code,
                row.reference,
                row.next_due_month().map(|d| month_label(&d)).unwrap_or_default()
            ));
        }
        let accrual = &row.accruals[month];
        if let Some(when) = deletions::buyoff_after(&row.timeline, &accrual.date, accrual.id) {
            return Err(deletions::behind_buyoff(&when));
        }
        return Ok(Some(Change { amount: entry.amount, date: Some(accrual.date.clone()), ..Change::new(row, month, ChangeKind::Late) }));
    }

    if !std::ptr::eq(entry.op, &REPAY) {
        if !cell.rows.is_empty() {
            return Err(format!(
                "This month already has its deduction. @{} ({}) goes in the month {} is due next — {}.",
                entry.op.code,
                entry.op.name.to_lowercase(),
                row.reference,
                row.next_due_month().map(|d| month_label(&d)).unwrap_or_else(|| "its due month".into())
            ));
        }
        return Ok(Some(if std::ptr::eq(entry.op, &SUSPEND) {
            if entry.amount > 60.0 {
                return Err("A suspension can run for at most 60 months here.".into());
            }
            Change { months: Some(entry.amount as i64), ..Change::new(row, month, ChangeKind::Suspend) }
        } else if std::ptr::eq(entry.op, &BUYOFF) {
            Change::new(row, month, ChangeKind::Buyoff)
        } else {
            Change { amount: entry.amount, months: entry.arg, ..Change::new(row, month, ChangeKind::TopUp) }
        }));
    }

    let Some(existing) = cell.rows.first() else {
        return Ok(Some(Change { amount: entry.amount, ..Change::new(row, month, ChangeKind::Deduct) }));
    };
    if round2(existing.amount) == entry.amount {
        return Ok(None);
    }
    if let Some(when) = deletions::buyoff_after(&row.timeline, &existing.date, existing.id) {
        return Err(deletions::behind_buyoff(&when));
    }
    Ok(Some(Change { amount: entry.amount, row_id: Some(existing.id), ..Change::new(row, month, ChangeKind::Update) }))
}

/// A staged change in words, for its tooltip. `staged` is everything else
/// staged, so a deletion is described as it will actually run.
pub fn describe(row: Option<&LoanRow>, change: &Change, staged: &[&Change]) -> String {
    match change.kind {
        ChangeKind::Delete => {
            let mut removal = change.removal;
            if let Some(row) = row {
                let mut payments: Vec<(i64, String, String)> = staged
                    .iter()
                    .filter(|c| c.kind == ChangeKind::Delete && c.loan_id == change.loan_id)
                    .filter_map(|c| row.cell(&c.month).rows.first().map(|p| (p.id, p.date.clone(), event::REPAYMENT.to_string())))
                    .collect();
                if let Some(own) = change.row_id
                    && !payments.iter().any(|p| p.0 == own)
                    && let Some(p) = row.cell(&change.month).rows.first()
                {
                    payments.push((p.id, p.date.clone(), event::REPAYMENT.into()));
                }
                if let Ok(kinds) = deletions::judge(&row.timeline, &payments)
                    && let Some(at) = payments.iter().position(|p| Some(p.0) == change.row_id)
                {
                    removal = Some(kinds[at]);
                }
            }
            if removal == Some(Removal::UnRun) {
                "Un-runs this month: the deduction and its interest charge go, the interest returns to unearned, \
                 and the month falls due again."
                    .into()
            } else {
                "Marks this month missed: the deduction goes, its interest stays charged, and later deductions \
                 are re-split interest first."
                    .into()
            }
        }
        ChangeKind::Late => format!(
            "Records the missed deduction of {} for {}; later deductions are re-split.",
            shillings(change.amount),
            month_label(&change.month)
        ),
        ChangeKind::TopUp => format!(
            "Top up {} by {}, re-termed over {} month(s).",
            change.reference,
            shillings(change.amount),
            change.months.unwrap_or(0)
        ),
        ChangeKind::Buyoff => format!("Buy off {}: settle everything owed on it this month.", change.reference),
        ChangeKind::Suspend => format!(
            "Suspend {} for {} month(s) from {}.",
            change.reference,
            change.months.unwrap_or(0),
            month_label(&change.month)
        ),
        ChangeKind::Deduct | ChangeKind::Update => format!("Deduct {} from {}.", shillings(change.amount), change.reference),
    }
}

/// What a cell offers for editing: its one deduction's figure, or nothing.
pub fn edit_text(row: &LoanRow, month: &str) -> String {
    match row.cell(month).rows.as_slice() {
        [one] => figure(one.amount),
        _ => String::new(),
    }
}

/// How a staged operation shows in its cell; `None` shows the figure.
pub fn staged_text(change: &Change) -> Option<String> {
    matches!(change.kind, ChangeKind::TopUp | ChangeKind::Buyoff | ChangeKind::Suspend)
        .then(|| OPS.format(&[change.entry()]))
}

/// What a staged cell offers for editing again.
pub fn staged_edit_text(change: &Change) -> String {
    if change.kind == ChangeKind::Delete { String::new() } else { OPS.format(&[change.entry()]) }
}

/// Why a whole row is greyed or marked, or `None`.
pub fn row_note(row: &LoanRow, blocked: &HashMap<i64, String>) -> Option<String> {
    if row.excluded {
        let mut note = format!("{} is excluded from this ledger.", row.name);
        if !row.exclusion_note.is_empty() {
            note.push(' ');
            note.push_str(&row.exclusion_note);
        }
        return Some(note);
    }
    if row.is_suspended {
        let until = row.suspend_until.as_deref().filter(|u| !u.is_empty()).unwrap_or("resumed");
        return Some(format!("{} is suspended until {until}.", row.reference));
    }
    if !row.member_active {
        return Some(blocked.get(&row.loan_id).cloned().unwrap_or_else(|| format!("{} has left the SACCO.", row.name)));
    }
    if row.status != LOAN_ACTIVE {
        return Some(format!("{} is {}.", row.reference, row.status.to_lowercase()));
    }
    None
}

/// What a missed month's cell explains, or `None`.
pub fn cell_note(row: &LoanRow, month: &str) -> Option<String> {
    if !row.missed(month) {
        return None;
    }
    let charge = &row.accruals[month];
    Some(format!(
        "Missed: interest of {} was charged on {} and nothing was paid. Type a figure to record the deduction \
         for this month.",
        shillings(charge.amount),
        charge.date
    ))
}

// --------------------------------------------------------------- saving

#[derive(Debug, Clone)]
pub struct Applied {
    pub change: Change,
    /// The loan as it stood before this change — what undo puts back.
    pub before: RestorePoint,
}

#[derive(Debug, Clone)]
pub struct Refused {
    pub change: Change,
    pub reason: String,
}

#[derive(Debug, Clone, Default)]
pub struct BatchResult {
    pub applied: Vec<Applied>,
    pub refused: Vec<Refused>,
    pub members_touched: BTreeSet<i64>,
    /// Each touched loan as the batch left it, so undo can tell whether
    /// anything has changed it since.
    pub after: Vec<RestorePoint>,
}

impl BatchResult {
    pub fn count(&self, kind: ChangeKind) -> usize {
        self.applied.iter().filter(|a| a.change.kind == kind).count()
    }

    pub fn summary(&self) -> String {
        let mut bits = Vec::new();
        for (kind, verb) in [
            (ChangeKind::Deduct, "deducted"),
            (ChangeKind::TopUp, "topped up"),
            (ChangeKind::Buyoff, "bought off"),
            (ChangeKind::Suspend, "suspended"),
        ] {
            let n = self.count(kind);
            if n > 0 {
                bits.push(format!("{n} {verb}"));
            }
        }
        if self.count(ChangeKind::Late) > 0 {
            bits.push(format!("{} missed month(s) recorded", self.count(ChangeKind::Late)));
        }
        if self.count(ChangeKind::Update) > 0 {
            bits.push(format!("{} corrected", self.count(ChangeKind::Update)));
        }
        let removals: Vec<Option<Removal>> =
            self.applied.iter().filter(|a| a.change.kind == ChangeKind::Delete).map(|a| a.change.removal).collect();
        let un_run = removals.iter().filter(|r| **r == Some(Removal::UnRun)).count();
        let missed = removals.iter().filter(|r| **r == Some(Removal::Missed)).count();
        if un_run > 0 {
            bits.push(format!("{un_run} un-run"));
        }
        if missed > 0 {
            bits.push(format!("{missed} marked missed"));
        }
        let mut text = if bits.is_empty() { "nothing changed".to_string() } else { bits.join(", ") };
        if !self.refused.is_empty() {
            text.push_str(&format!(" — {} refused", self.refused.len()));
        }
        text
    }
}

/// Per loan: deletions newest first, then corrections and missed months,
/// then new deductions and operations in month order.
fn batch_order(change: &Change) -> (i64, u8, i64) {
    let month: i64 = change.month.replace('-', "").parse().unwrap_or(0);
    match change.kind {
        ChangeKind::Delete => (change.loan_id, 0, -month),
        ChangeKind::Update | ChangeKind::Late => (change.loan_id, 1, month),
        _ => (change.loan_id, 2, month),
    }
}

/// Decide every staged deletion against the journal as it is now — newest
/// first, all or nothing per loan.
/// How each staged deletion runs, `{(loan_id, row_id): removal}`, and the
/// loans refused outright, `{loan_id: reason}`.
type Judged = (HashMap<(i64, i64), Removal>, HashMap<i64, String>);

fn judge_batch_deletions(journal: &Journal, staged: &[&Change]) -> Result<Judged> {
    let mut kinds = HashMap::new();
    let mut refused = HashMap::new();
    let mut by_loan: Vec<(i64, Vec<&Change>)> = Vec::new();
    for change in staged {
        match by_loan.iter_mut().find(|(l, _)| *l == change.loan_id) {
            Some((_, list)) => list.push(change),
            None => by_loan.push((change.loan_id, vec![change])),
        }
    }
    for (loan_id, group) in by_loan {
        let first = group[0];
        if let Some(why) = deletions::unreconciled(journal, first.member_id, &first.reference)? {
            refused.insert(loan_id, why);
            continue;
        }
        let timeline = deletions::timeline(journal, first.member_id, &first.reference)?;
        let mut payments = Vec::new();
        let mut missing = None;
        for change in &group {
            match change.row_id.and_then(|id| timeline.iter().find(|(_, i, _)| *i == id)) {
                Some((date, id, kind)) => payments.push((*id, date.clone(), kind.clone())),
                None => missing = Some(change.month.clone()),
            }
        }
        if let Some(month) = missing {
            refused.insert(loan_id, format!("The deduction for {} no longer exists — reload the grid.", month_label(&month)));
            continue;
        }
        match deletions::judge(&timeline, &payments) {
            Ok(found) => {
                for ((id, _, _), kind) in payments.iter().zip(found) {
                    kinds.insert((loan_id, *id), kind);
                }
            }
            Err(why) => {
                refused.insert(loan_id, why);
            }
        }
    }
    Ok((kinds, refused))
}

/// The date a loan is next due, stepped past recorded suspensions — the walk
/// a deduction does before it deducts.
fn due_date(journal: &Journal, loan: &crate::db::model::Loan) -> Result<Option<String>> {
    let Some(mut cursor) = dates::parse(loan.due()) else { return Ok(None) };
    let windows = suspensions::loan_windows(journal, loan.id)?;
    for _ in 0..600 {
        if !suspensions::date_in_suspension(&dates::format(cursor), &windows) {
            break;
        }
        cursor = dates::add_months(cursor, 1);
    }
    Ok(Some(dates::format(cursor)))
}

/// Why a change may not go ahead, checked against the loan as earlier changes
/// in the batch have left it.
fn refusal_for(journal: &Journal, change: &Change) -> Result<Option<String>> {
    let kind = match change.kind {
        ChangeKind::Deduct => Some("loan.deduct"),
        ChangeKind::TopUp => Some("loan.top_up"),
        _ => None,
    };
    if let Some(kind) = kind
        && let Some(r) = policy::check(journal, &Operation::new(kind, change.member_id).for_loan(&change.reference))?.first()
    {
        return Ok(Some(r.reason.clone()));
    }
    if matches!(change.kind, ChangeKind::Update | ChangeKind::Late) {
        return deletions::unreconciled(journal, change.member_id, &change.reference);
    }
    if !matches!(change.kind, ChangeKind::Deduct | ChangeKind::TopUp | ChangeKind::Buyoff | ChangeKind::Suspend) {
        return Ok(None);
    }
    let Some(loan) = journal.loans().by_ref(change.member_id, &change.reference)? else {
        return Ok(Some(format!("{} no longer exists.", change.reference)));
    };
    if !loan.is_active() {
        return Ok(Some(format!("{} is {} now.", change.reference, loan.status.to_lowercase())));
    }
    let due = due_date(journal, &loan)?;
    if due.as_deref().map(month_key).as_deref() != Some(change.month.as_str()) {
        let when = due.map(|d| month_label(&month_key(&d))).unwrap_or_else(|| "no recorded month".into());
        return Ok(Some(format!("{} is now due for {when}, so this month was not taken.", change.reference)));
    }
    if change.kind == ChangeKind::Suspend && loan.is_suspended {
        return Ok(Some(format!("{} is already suspended.", change.reference)));
    }
    if change.kind == ChangeKind::TopUp
        && let Some(last) = journal.ledger().last_date(change.member_id, &change.reference)?
        && month_key(&last) >= change.month
    {
        // The Top-Up dialog's rule: a later month than the loan's last entry.
        return Ok(Some(format!(
            "{} already has an entry in {}; a top-up must fall in a later month.",
            change.reference,
            month_label(&month_key(&last))
        )));
    }
    Ok(None)
}

fn write(journal: &Journal, change: &Change) -> Result<()> {
    let (member, reference) = (change.member_id, change.reference.as_str());
    match change.kind {
        ChangeKind::Deduct => {
            let loan = loan_service::require_loan(journal, member, reference)?;
            // Past a fixed-term suspension's end the loan resumes, recorded at
            // the boundary — what catch-up does when it reaches it.
            if loan.is_suspended
                && let Some(until) = loan.suspend_until.as_deref().filter(|u| !u.is_empty())
                && due_date(journal, &loan)?.as_deref().unwrap_or("") >= dates::date_part(until)
            {
                suspensions::resume(journal, loan.id, Some(dates::date_part(until)))?;
            }
            loan_service::deduct(journal, member, reference)?;
            let (row_id, taken): (i64, f64) = journal.conn().query_row(
                "SELECT id, deducted FROM ledger WHERE individual_id=? AND loan_id=? AND event_type='Repayment' \
                 ORDER BY date DESC, id DESC LIMIT 1",
                params![member, reference],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            let taken = round2(taken);
            // A final month owes less than the instalment, and the engine takes
            // only what is owed; forcing the staged figure on top over-collects.
            let settled = journal.loans().by_ref(member, reference)?.is_some_and(|l| !l.is_active());
            if !(settled && change.magnitude() > taken) && taken != change.magnitude() {
                journal.ledger().set_deducted(row_id, change.magnitude())?;
                clear_split(journal, row_id)?;
                // Re-derived now, not at the end of the batch: the next month
                // staged on this loan deducts from the balance this one left.
                project_loan(journal, member, reference)?;
            }
        }
        ChangeKind::Update => {
            let row_id = change.row_id.ok_or_else(|| Error::invalid("That deduction no longer exists."))?;
            let row = journal.ledger().get(row_id)?.ok_or_else(|| Error::invalid("That deduction no longer exists."))?;
            // The amount is an assertion, the split is not: clearing it — and
            // every split after it — is what makes the new figure count.
            deletions::resplit_after(journal, member, reference, &row.date, row_id)?;
            journal.ledger().set_deducted(row_id, change.magnitude())?;
            clear_split(journal, row_id)?;
            project_loan(journal, member, reference)?;
        }
        ChangeKind::Delete => {
            let loan = loan_service::require_loan(journal, member, reference)?;
            let row_id = change.row_id.ok_or_else(|| Error::invalid("That deduction no longer exists."))?;
            let removal = change.removal.ok_or_else(|| Error::invalid("The deletion was not judged."))?;
            deletions::remove(journal, member, loan.id, reference, row_id, removal)?;
        }
        ChangeKind::Late => {
            deletions::record_missed(journal, member, reference, &change.month, change.magnitude())?;
        }
        ChangeKind::TopUp => {
            let loan = loan_service::require_loan(journal, member, reference)?;
            let date = due_date(journal, &loan)?;
            loan_service::top_up(journal, member, reference, change.magnitude(), change.months.unwrap_or(1), date.as_deref())?;
        }
        ChangeKind::Buyoff => {
            let loan = loan_service::require_loan(journal, member, reference)?;
            let date = due_date(journal, &loan)?;
            loan_service::buyoff(journal, member, reference, date.as_deref())?;
        }
        ChangeKind::Suspend => {
            let loan = loan_service::require_loan(journal, member, reference)?;
            let start = due_date(journal, &loan)?.ok_or_else(|| Error::invalid("The loan has no due date."))?;
            let end = dates::add_months_str(&start, change.months.unwrap_or(1) as i32).unwrap_or_default();
            suspensions::record(journal, loan.id, &start, Some(&end), None)?;
        }
    }
    Ok(())
}

/// Write a batch of staged loan edits: one transaction; per-change refusals;
/// a loan's deletions judged together, newest first, all or nothing.
pub fn apply_changes(journal: &Journal, changes: &[Change]) -> Result<BatchResult> {
    let mut result = BatchResult::default();
    if changes.is_empty() {
        return Ok(result);
    }
    let mut ordered: Vec<Change> = changes.to_vec();
    ordered.sort_by_key(batch_order);
    journal.transaction(|| {
        let deletes: Vec<&Change> = ordered.iter().filter(|c| c.kind == ChangeKind::Delete).collect();
        let (kinds, refused_loans) = judge_batch_deletions(journal, &deletes)?;
        for change in &ordered {
            let mut change = change.clone();
            let reason = if change.kind == ChangeKind::Delete {
                match refused_loans.get(&change.loan_id) {
                    Some(why) => Some(why.clone()),
                    None => {
                        change.removal = change.row_id.and_then(|id| kinds.get(&(change.loan_id, id)).copied());
                        None
                    }
                }
            } else {
                refusal_for(journal, &change)?
            };
            if let Some(reason) = reason {
                result.refused.push(Refused { change, reason });
                continue;
            }
            let before = RestorePoint::capture(journal, change.member_id, &change.reference)?;
            // A change that leaves the loan out of step with its schedule, or
            // repaid beyond what it owed, is refused like any other.
            let sound = guards::soundness(journal, change.member_id, &change.reference)?;
            let checked = || -> Result<()> {
                write(journal, &change)?;
                if journal.loans().by_ref(change.member_id, &change.reference)?.is_some() {
                    project_loan(journal, change.member_id, &change.reference)?;
                    guards::keep_sound(journal, change.member_id, &change.reference, &sound)?;
                }
                Ok(())
            };
            match journal.savepoint(checked) {
                Ok(()) => {
                    result.members_touched.insert(change.member_id);
                    result.applied.push(Applied { change, before });
                }
                Err(e) => {
                    if !e.is_user_facing() {
                        log::error!("Loan grid change failed for {} {}: {e}", change.reference, change.month);
                    }
                    result.refused.push(Refused { change, reason: e.to_string() });
                }
            }
        }
        for member in &result.members_touched {
            project_member(journal, *member)?;
            recalculate_default_deduction(journal, *member)?;
        }
        let mut seen = BTreeSet::new();
        for applied in &result.applied {
            if seen.insert((applied.change.member_id, applied.change.reference.clone())) {
                result.after.push(RestorePoint::capture(journal, applied.change.member_id, &applied.change.reference)?);
            }
        }
        Ok(())
    })?;
    Ok(result)
}

/// Put a batch back exactly: each loan as it stood before the batch's first
/// change to it. Refused, with nothing undone, when a loan has changed since
/// the save — in the member's ledger, say — because putting the loan back
/// would silently throw that work away.
pub fn revert(journal: &Journal, result: &BatchResult) -> Result<BTreeSet<i64>> {
    let mut touched = BTreeSet::new();
    journal.transaction(|| {
        for after in &result.after {
            if !after.still_current(journal)? {
                let name = journal.members().name_of(after.member_id())?.unwrap_or_default();
                return Err(Error::invalid(format!(
                    "{name} · {} has changed since the save, so undoing it would overwrite that work. Nothing \
                     was undone.",
                    after.reference()
                )));
            }
        }
        let mut seen = BTreeSet::new();
        for applied in &result.applied {
            if seen.insert((applied.change.member_id, applied.change.reference.clone())) {
                applied.before.restore(journal)?;
                touched.insert(applied.change.member_id);
            }
        }
        Ok(())
    })?;
    Ok(touched)
}

// ------------------------------------------------------------ fill forward

#[derive(Debug, Clone, Default)]
pub struct FillPlan {
    pub changes: Vec<Change>,
    pub skipped: Vec<(i64, String)>,
    pub in_future: usize,
    pub suspended: usize,
}

impl FillPlan {
    pub fn loans(&self) -> usize {
        self.changes.iter().map(|c| c.loan_id).collect::<BTreeSet<_>>().len()
    }

    pub fn notes(&self) -> Vec<String> {
        let mut out = Vec::new();
        if self.in_future > 0 {
            out.push(format!("{} month(s) in the future were left empty.", self.in_future));
        }
        if self.suspended > 0 {
            out.push(format!("{} suspended month(s) were skipped.", self.suspended));
        }
        out
    }
}

/// Catch-up as a table: each loan's instalment for every month from its due
/// date through this month, stepping over suspended months. Nothing before the
/// due date — a loan is paid in order — and nothing still to come.
pub fn plan_fill(grid: &LoanGrid, rows: &[&LoanRow], months: &[String], today: Option<&str>) -> FillPlan {
    let today = today.map(str::to_string).unwrap_or_else(dates::today);
    let this_month = month_key(&today);
    let mut plan = FillPlan::default();
    for row in rows {
        let skip = |reason: String| (row.loan_id, reason);
        if row.status != LOAN_ACTIVE {
            plan.skipped.push(skip(format!("{} is {}.", row.reference, row.status.to_lowercase())));
            continue;
        }
        if row.excluded {
            plan.skipped.push(skip(format!("{} is excluded from this ledger.", row.name)));
            continue;
        }
        if !row.member_active {
            plan.skipped.push(skip(format!("{} has left the SACCO.", row.name)));
            continue;
        }
        if row.is_suspended && row.suspend_until.as_deref().is_none_or(str::is_empty) {
            plan.skipped.push(skip(format!("{} is suspended until further notice.", row.reference)));
            continue;
        }
        if row.installment <= 0.0 {
            plan.skipped.push(skip(format!("{} has no instalment recorded.", row.reference)));
            continue;
        }
        let Some(due) = row.next_due_month() else {
            plan.skipped.push(skip(format!("{} has no due date recorded.", row.reference)));
            continue;
        };
        let windows = grid.suspensions.get(&row.loan_id).map(Vec::as_slice).unwrap_or(&[]);
        let cursor = row.stored_due.as_deref().filter(|d| !d.is_empty()).map(month_key).unwrap_or(due);
        let mut proposed = Vec::new();
        for month in months {
            if *month < cursor {
                continue;
            }
            if *month > this_month {
                plan.in_future += 1;
                continue;
            }
            if in_suspension(month, windows, row.due_day()) {
                plan.suspended += 1;
                continue;
            }
            if !row.cell(month).rows.is_empty() {
                continue;
            }
            proposed.push(month.clone());
        }
        if proposed.is_empty() {
            plan.skipped.push(skip(format!("{} is up to date.", row.reference)));
            continue;
        }
        for month in proposed {
            plan.changes.push(Change { amount: row.installment, ..Change::new(row, &month, ChangeKind::Deduct) });
        }
    }
    plan
}

/// Loans whose member may take no new deduction, as `{loan_id: reason}`.
pub fn blocked_loans(journal: &Journal, rows: &[&LoanRow]) -> Result<HashMap<i64, String>> {
    let mut out = HashMap::new();
    for row in rows {
        if let Some(r) = policy::check(journal, &Operation::new("loan.deduct", row.member_id).for_loan(&row.reference))?.first() {
            out.insert(row.loan_id, r.reason.clone());
        }
    }
    Ok(out)
}

// ------------------------------------------------------- deleting months

/// Plan deleting `months` for the given (on-screen) loans — docs §4. Each
/// loan's deductions are judged together, newest first: un-run, missed, or —
/// if any is refused — none of them.
pub fn plan_delete_months(journal: &Journal, rows: &[&LoanRow], months: &[String]) -> Result<DeletePlan> {
    let mut months = months.to_vec();
    months.sort();
    let mut plan = DeletePlan::new(&months);
    let shown: BTreeSet<i64> = rows.iter().filter(|r| !r.excluded).map(|r| r.loan_id).collect();
    let (mut structural, mut reopened) = (0, 0);

    for row in rows.iter().filter(|r| !r.excluded) {
        structural += row
            .timeline
            .iter()
            .filter(|(d, _, k)| [event::LOAN_ISSUED, event::TOP_UP, event::RESTRUCTURE].contains(&k.as_str()) && months.contains(&month_key(d)))
            .count();
        let cells: Vec<(&String, &Cell)> = months.iter().map(|m| (m, row.cell(m))).filter(|(_, c)| !c.rows.is_empty()).collect();
        if cells.is_empty() {
            continue;
        }
        if cells.iter().any(|(_, c)| c.rows.len() > 1) {
            plan.refuse(&row.label(), "A month here holds two deductions; open the member's ledger to work on them.".into());
            continue;
        }
        let payments: Vec<(i64, String, String)> =
            cells.iter().map(|(_, c)| (c.rows[0].id, c.rows[0].date.clone(), c.rows[0].kind.clone())).collect();
        let judged = match deletions::judge(&row.timeline, &payments) {
            Ok(kinds) => match deletions::unreconciled(journal, row.member_id, &row.reference)? {
                Some(why) => Err(why),
                None => Ok(kinds),
            },
            Err(why) => Err(why),
        };
        let kinds = match judged {
            Ok(kinds) => kinds,
            Err(why) => {
                plan.refuse(&row.label(), why);
                continue;
            }
        };
        for ((month, cell), kind) in cells.iter().zip(kinds) {
            plan.loan_changes.push(Change {
                row_id: cell.row_id(),
                removal: Some(kind),
                ..Change::new(row, month, ChangeKind::Delete)
            });
            plan.tally(if kind == Removal::UnRun { "un-run" } else { "missed" }, cell.net);
        }
        if row.status == LOAN_PAID {
            reopened += 1;
        }
    }

    let placeholders = vec!["?"; months.len()].join(",");
    let mut stmt = journal.conn().prepare(&format!(
        "SELECT l.id FROM ledger g JOIN loans l ON l.individual_id = g.individual_id AND l.ref = g.loan_id \
         WHERE g.event_type IN ('Repayment', 'Loan Buyoff') AND substr(g.date, 1, 7) IN ({placeholders})"
    ))?;
    let others = stmt
        .query_map(rusqlite::params_from_iter(months.iter()), |r| r.get::<_, i64>(0))?
        .filter_map(|r| r.ok())
        .filter(|id| !shown.contains(id))
        .count();
    if others > 0 {
        plan.untouched.push(format!(
            "{others} deduction(s) on loans not on screen (paid off, filtered out, past members or excluded) are \
             not touched."
        ));
    }
    if structural > 0 {
        plan.untouched.push(format!(
            "{structural} loan issue(s), top-up(s) or restructure(s) in these months are not deleted — they are \
             not deductions."
        ));
    }
    if !plan.refused.is_empty() {
        plan.untouched.push(format!(
            "{} loan(s) are refused — listed below — and keep all their deductions.",
            plan.refused.len()
        ));
    }
    let (un_run, _) = plan.tally_of("un-run");
    let (missed, _) = plan.tally_of("missed");
    if un_run > 0 {
        plan.consequences.push(format!(
            "Un-run ({un_run}): the deduction is the loan's last entry. It goes with that month's interest charge, \
             the interest returns to the loan's unearned interest, and the loan falls due for that month again."
        ));
    }
    if missed > 0 {
        plan.consequences.push(format!(
            "Missed ({missed}): later deductions exist. Only the deduction goes — the month's interest stays \
             charged — and every later deduction is re-split, interest first. The shortfall stays in the \
             balance; the loan runs one instalment longer and its due date does not move."
        ));
    }
    if reopened > 0 {
        plan.consequences.push(format!("{reopened} paid-off loan(s) reopen."));
    }
    if missed > 0 {
        plan.consequences.push(
            "If a month was a payment holiday rather than a missed payment, suspend it instead (@S): no interest \
             for it, and the term extends."
                .into(),
        );
    }
    Ok(plan)
}

#[cfg(test)]
mod tests;
