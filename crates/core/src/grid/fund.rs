//! A members × months grid over one of the three funds.
//!
//! Rows are members, columns are months, a cell is what moved in that
//! member-month. Every write goes through the same repositories the member
//! screens use, so balances and the audit trail behave identically whichever
//! screen an edit came from.
//!
//! A cell is a list of entries (see [`super::ops`]). A deposit is assumed
//! every month and a withdrawal happens *as well*, so what is typed replaces
//! only the kinds it names: `2,500` sets the deposit and keeps the month's
//! withdrawal. Clearing a cell clears the month. A month holding a row no
//! operation describes (an `Interest` posting) is read only.
//!
//! A batch is one transaction, but refusals are per cell — one ineligible
//! member must not cost the other ninety edits — and a save may not leave a
//! Savings or Christmas withdrawal bigger than the balance it came from
//! (docs/grid/deleting-months.md §3.3).

use std::collections::{BTreeSet, HashMap};
use std::sync::LazyLock;

use super::ops::{Entry, Figure, Op, OpSet};
use super::{DeletePlan, Fingerprint};
use crate::Journal;
use crate::dates::{self, month_bounds, month_key, month_label};
use crate::db::ledger::Snapshot;
use crate::db::model::{Fund, FundRow, WITHDRAWAL};
use crate::error::Result;
use crate::funds::{Posting, check as check_posting};
use crate::money::{round2, shillings};
use crate::policy;

// ------------------------------------------------------------------ ops

static DEPOSIT: Op = Op {
    code: "D",
    name: "Deposit",
    words: &["d", "deposit", "i", "increment"],
    help: "Money in — the default, so a bare figure means this.",
    value: Some(Figure::Money),
    arg: None,
};
static WITHDRAW: Op = Op {
    code: "W",
    name: "Withdrawal",
    words: &["w", "withdrawal", "withdraw"],
    help: "Money out. A bare negative figure (-500) means the same.",
    value: Some(Figure::Money),
    arg: None,
};
static XMAS_WITHDRAW: Op = Op {
    code: "W",
    name: "Withdrawal",
    words: &["w", "withdrawal", "withdraw"],
    help: "Money out — only in the unlock month set in Settings. A bare negative figure (-500) means the same.",
    value: Some(Figure::Money),
    arg: None,
};
static CONTRIBUTE: Op = Op {
    code: "C",
    name: "Contribution",
    words: &["c", "contribution", "contribute", "i", "increment", "d", "deposit"],
    help: "The monthly contribution — the default, so a bare figure means this.",
    value: Some(Figure::Money),
    arg: None,
};
static PAYOUT: Op = Op {
    code: "P",
    name: "Payout",
    words: &["p", "payout", "w", "withdrawal", "withdraw"],
    help: "A benefit paid out of the fund (recorded as a withdrawal). A bare negative figure means the same.",
    value: Some(Figure::Money),
    arg: None,
};

static SAVINGS_OPS: LazyLock<OpSet> =
    LazyLock::new(|| OpSet::new(vec![&DEPOSIT, &WITHDRAW], &DEPOSIT, Some(&WITHDRAW)));
static CHRISTMAS_OPS: LazyLock<OpSet> =
    LazyLock::new(|| OpSet::new(vec![&DEPOSIT, &XMAS_WITHDRAW], &DEPOSIT, Some(&XMAS_WITHDRAW)));
static BENEVOLENT_OPS: LazyLock<OpSet> =
    LazyLock::new(|| OpSet::new(vec![&CONTRIBUTE, &PAYOUT], &CONTRIBUTE, Some(&PAYOUT)));

/// The operations a fund's cells understand.
pub fn operations(fund: Fund) -> &'static OpSet {
    match fund {
        Fund::Savings => &SAVINGS_OPS,
        Fund::Christmas => &CHRISTMAS_OPS,
        Fund::Benevolent => &BENEVOLENT_OPS,
    }
}

/// The member's own money: a withdrawal may not exceed the balance it came
/// from. Not benevolent — a payout draws on a pooled fund.
pub fn own_money(fund: Fund) -> bool {
    fund != Fund::Benevolent
}

fn type_for(fund: Fund, op: &'static Op) -> &'static str {
    if operations(fund).is_default(op) { fund.credit() } else { WITHDRAWAL }
}

fn op_for(fund: Fund, transaction_type: &str) -> Option<&'static Op> {
    let ops = operations(fund);
    if transaction_type == fund.credit() {
        Some(ops.default)
    } else if transaction_type == WITHDRAWAL {
        ops.negative
    } else {
        None
    }
}

// ---------------------------------------------------------------- cells

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CellState {
    Empty,
    Single,
    Mixed,
}

/// One member-month.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Cell {
    pub net: f64,
    pub rows: Vec<FundRow>,
}

static EMPTY_CELL: Cell = Cell { net: 0.0, rows: Vec::new() };

impl Cell {
    pub fn state(&self) -> CellState {
        match self.rows.len() {
            0 => CellState::Empty,
            1 => CellState::Single,
            _ => CellState::Mixed,
        }
    }

    pub fn row_id(&self) -> Option<i64> {
        (self.rows.len() == 1).then(|| self.rows[0].id)
    }

    /// What the cell looked like when read — compared again at save time.
    pub fn fingerprint(&self) -> Fingerprint {
        fingerprint_of(self.rows.iter().map(|r| (r.id, r.amount, r.date.as_str(), r.transaction_type.as_str())))
    }
}

fn fingerprint_of<'a>(rows: impl Iterator<Item = (i64, f64, &'a str, &'a str)>) -> Fingerprint {
    let mut out: Fingerprint = rows
        .map(|(id, amount, date, kind)| (id, (round2(amount) * 100.0).round() as i64, dates::date_part(date).into(), kind.into()))
        .collect();
    out.sort();
    out
}

/// A cell's rows as entries, in date order — or `None` if one has no operation.
pub fn cell_entries(fund: Fund, cell: &Cell) -> Option<Vec<Entry>> {
    let mut rows: Vec<&FundRow> = cell.rows.iter().collect();
    rows.sort_by(|a, b| (dates::date_part(&a.date), a.id).cmp(&(dates::date_part(&b.date), b.id)));
    rows.into_iter().map(|r| op_for(fund, &r.transaction_type).map(|op| Entry::new(op, round2(r.amount)))).collect()
}

/// Entries as sorted `(type, cents)` pairs, zero rows left out — reading a
/// zero row back must not stage its deletion.
fn as_types(fund: Fund, entries: &[Entry]) -> Vec<(&'static str, i64)> {
    let mut out: Vec<(&'static str, i64)> = entries
        .iter()
        .filter(|e| round2(e.amount) != 0.0)
        .map(|e| (type_for(fund, e.op), (round2(e.amount) * 100.0).round() as i64))
        .collect();
    out.sort();
    out
}

// ----------------------------------------------------------------- rows

/// A member as the grid needs them.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct MemberRow {
    pub member_id: i64,
    pub name: String,
    pub pf_no: String,
    pub status: String,
    pub retired: bool,
    /// Benevolent: has an active enrolment.
    pub enrolled: bool,
    /// Benevolent: the enrolled amount.
    pub monthly_amount: f64,
    pub cells: HashMap<String, Cell>,
    /// What the member held before the first visible column.
    pub opening_balance: f64,
    pub total_balance: f64,
    /// Left out of this ledger's grid by a deliberate decision.
    pub excluded: bool,
    pub exclusion_note: String,
    /// The month this member's account began: months before it are months the
    /// account did not exist for, which must not be filled.
    pub account_start: Option<String>,
}

impl MemberRow {
    pub fn key(&self) -> i64 {
        self.member_id
    }

    pub fn active(&self) -> bool {
        !self.retired && (self.status.is_empty() || self.status == "Active")
    }

    pub fn cell(&self, month: &str) -> &Cell {
        self.cells.get(month).unwrap_or(&EMPTY_CELL)
    }

    pub fn period_total(&self, months: &[String]) -> f64 {
        round2(months.iter().map(|m| self.cell(m).net).sum())
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Grid {
    pub fund: Fund,
    pub months: Vec<String>,
    pub members: Vec<MemberRow>,
}

impl Grid {
    pub fn column_total(&self, month: &str) -> f64 {
        round2(self.members.iter().map(|m| m.cell(month).net).sum())
    }

    pub fn grand_total(&self) -> f64 {
        round2(self.members.iter().map(|m| m.period_total(&self.months)).sum())
    }
}

/// Read one fund's grid for a list of month keys.
pub fn load(journal: &Journal, fund: Fund, months: &[String], include_inactive: bool, include_excluded: bool) -> Result<Grid> {
    let Some((start, end)) = month_bounds(months) else {
        return Ok(Grid { fund, months: Vec::new(), members: Vec::new() });
    };
    let mut movements: HashMap<i64, HashMap<String, Cell>> = HashMap::new();
    for row in journal.funds().movements_in_range(fund, &start, &end)? {
        let cell = movements.entry(row.individual_id).or_default().entry(month_key(&row.date)).or_default();
        cell.net = round2(cell.net + row.signed());
        cell.rows.push(row);
    }
    let first_seen = journal.funds().first_movement_dates(fund)?;
    let mut enrolments: HashMap<i64, (f64, bool)> = HashMap::new();
    let mut enrolled_from: HashMap<i64, String> = HashMap::new();
    if fund == Fund::Benevolent {
        for e in journal.funds().enrolments()? {
            enrolments.insert(e.individual_id, (e.monthly_amount, e.active));
            if let Some(start) = e.start_date.filter(|s| !s.is_empty()) {
                enrolled_from.insert(e.individual_id, month_key(&start));
            }
        }
    }
    let excluded = journal.members().excluded_from(fund.table())?;
    let balances = journal.funds().balances(fund)?;

    let mut members = Vec::new();
    for person in journal.members().summaries()? {
        let id = person.id;
        if excluded.contains_key(&id) && !include_excluded {
            continue;
        }
        if !include_inactive && !person.is_active() {
            continue;
        }
        let (amount, enrolled) = enrolments.get(&id).copied().unwrap_or((0.0, fund != Fund::Benevolent));
        let cells = movements.remove(&id).unwrap_or_default();
        let total = balances.get(&id).copied().unwrap_or(0.0);
        // An enrolment date states when the account began; a first movement is
        // evidence of it. The earlier wins.
        let account_start = [first_seen.get(&id).map(|d| month_key(d)), enrolled_from.get(&id).cloned()]
            .into_iter()
            .flatten()
            .min();
        let shown: f64 = cells.values().map(|c| c.net).sum();
        members.push(MemberRow {
            member_id: id,
            name: person.name.clone(),
            pf_no: person.pf_no.clone(),
            status: person.employment_status.clone(),
            retired: person.is_retired,
            enrolled,
            monthly_amount: amount,
            opening_balance: round2(total - shown),
            total_balance: total,
            cells,
            excluded: excluded.contains_key(&id),
            exclusion_note: excluded.get(&id).cloned().unwrap_or_default(),
            account_start,
        });
    }
    Ok(Grid { fund, months: months.to_vec(), members })
}

// -------------------------------------------------------------- changes

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChangeKind {
    Insert,
    Update,
    Delete,
    /// Make the month hold exactly these entries.
    Set,
}

/// One staged cell edit.
#[derive(Debug, Clone, PartialEq)]
pub struct Change {
    pub fund: Fund,
    pub member_id: i64,
    pub month: String,
    pub kind: ChangeKind,
    /// Signed: negative means money out.
    pub amount: f64,
    pub row_id: Option<i64>,
    /// The cell's fingerprint when it was read.
    pub before: Fingerprint,
    pub entries: Vec<Entry>,
}

impl Change {
    fn new(fund: Fund, member_id: i64, month: &str, kind: ChangeKind) -> Change {
        Change {
            fund,
            member_id,
            month: month.to_string(),
            kind,
            amount: 0.0,
            row_id: None,
            before: Vec::new(),
            entries: Vec::new(),
        }
    }

    pub fn key(&self) -> i64 {
        self.member_id
    }

    /// What the cell nets to once saved — for staged totals.
    pub fn effect(&self) -> f64 {
        if self.kind == ChangeKind::Delete { 0.0 } else { self.amount }
    }

    pub fn transaction_type(&self) -> &'static str {
        if self.amount < 0.0 { WITHDRAWAL } else { self.fund.credit() }
    }

    pub fn magnitude(&self) -> f64 {
        round2(self.amount.abs())
    }
}

/// Turn typed text into a change. `Ok(None)` when there is nothing to do;
/// `Err` with a sentence when the text cannot be accepted.
pub fn stage(grid: &Grid, row: &MemberRow, month: &str, text: &str) -> std::result::Result<Option<Change>, String> {
    let cell = row.cell(month);
    let ops = operations(grid.fund);
    let current = cell_entries(grid.fund, cell)
        .ok_or("This month holds a transaction no grid operation describes. Open the member's ledger to edit it.")?;
    let (typed, append) = ops.parse(text)?;
    let entries: Vec<Entry> = if append {
        current.iter().copied().chain(typed.into_iter().filter(|e| e.amount != 0.0)).collect()
    } else if typed.is_empty() {
        Vec::new()
    } else {
        // Only the kinds named are replaced; the credit is listed first.
        let named: Vec<&'static Op> = typed.iter().map(|e| e.op).collect();
        let mut kept: Vec<Entry> =
            current.iter().copied().filter(|e| !named.iter().any(|n| std::ptr::eq(*n, e.op))).collect();
        kept.extend(typed.into_iter().filter(|e| e.amount != 0.0));
        kept.sort_by_key(|e| !ops.is_default(e.op));
        kept
    };
    if as_types(grid.fund, &entries) == as_types(grid.fund, &current) {
        return Ok(None);
    }

    let before = cell.fingerprint();
    if cell.rows.len() <= 1 && entries.len() <= 1 {
        let Some(entry) = entries.first() else {
            return Ok(Some(Change {
                row_id: cell.row_id(),
                before,
                ..Change::new(grid.fund, row.member_id, month, ChangeKind::Delete)
            }));
        };
        let signed = if ops.is_default(entry.op) { entry.amount } else { -entry.amount };
        let kind = if cell.state() == CellState::Empty { ChangeKind::Insert } else { ChangeKind::Update };
        return Ok(Some(Change {
            amount: signed,
            row_id: cell.row_id(),
            before,
            ..Change::new(grid.fund, row.member_id, month, kind)
        }));
    }
    let net = round2(entries.iter().map(|e| if ops.is_default(e.op) { e.amount } else { -e.amount }).sum());
    Ok(Some(Change { amount: net, before, entries, ..Change::new(grid.fund, row.member_id, month, ChangeKind::Set) }))
}

/// What a staged change will leave in its month, as entries.
pub fn change_entries(change: &Change) -> Vec<Entry> {
    match change.kind {
        ChangeKind::Set => change.entries.clone(),
        ChangeKind::Delete => Vec::new(),
        _ => {
            let ops = operations(change.fund);
            let op = if change.amount >= 0.0 { ops.default } else { ops.negative.unwrap_or(ops.default) };
            vec![Entry::new(op, change.magnitude())]
        }
    }
}

/// The date a newly inserted cell should carry: the day the member's last
/// credit landed on (a withdrawal on the 20th must not drag next month's
/// deposit to the 20th), else the enrolment's day, else the 1st.
pub fn default_day(journal: &Journal, fund: Fund, member_id: i64, month: &str) -> Result<String> {
    use chrono::Datelike;
    let last = match journal.funds().last_dated_row(fund, member_id, &[fund.credit()])? {
        Some(row) => Some(row),
        None => journal.funds().last_dated_row(fund, member_id, &[])?,
    };
    let mut day = 1;
    if let Some(date) = last.as_ref().and_then(|r| dates::parse(&r.date)) {
        day = date.day();
    } else if fund == Fund::Benevolent
        && let Some(start) = journal.funds().enrolment(member_id)?.and_then(|e| e.start_date).and_then(|s| dates::parse(&s))
    {
        day = start.day();
    }
    Ok(dates::day_in_month(month, day))
}

// --------------------------------------------------------------- saving

/// How to undo one applied change.
#[derive(Debug, Clone, PartialEq)]
pub enum Restore {
    /// An insert: delete the row it made.
    Inserted(i64),
    /// An update: put the row's date, amount, notes and type back.
    Updated { id: i64, date: String, amount: f64, notes: String, kind: String },
    /// A delete: reinstate the row exactly.
    Deleted(Snapshot),
    /// A set: undo each step, last first.
    Steps(Vec<Restore>),
}

#[derive(Debug, Clone)]
pub struct Applied {
    pub change: Change,
    pub restore: Restore,
}

#[derive(Debug, Clone)]
pub struct Refused {
    pub change: Change,
    pub reason: String,
}

#[derive(Debug, Clone)]
pub struct BatchResult {
    pub fund: Fund,
    pub applied: Vec<Applied>,
    pub refused: Vec<Refused>,
    pub members_touched: BTreeSet<i64>,
}

impl BatchResult {
    fn count(&self, kind: ChangeKind) -> usize {
        self.applied.iter().filter(|a| a.change.kind == kind).count()
    }

    pub fn summary(&self) -> String {
        let mut bits = Vec::new();
        let added = self.count(ChangeKind::Insert);
        let changed = self.count(ChangeKind::Update) + self.count(ChangeKind::Set);
        let removed = self.count(ChangeKind::Delete);
        if added > 0 {
            bits.push(format!("{added} added"));
        }
        if changed > 0 {
            bits.push(format!("{changed} changed"));
        }
        if removed > 0 {
            bits.push(format!("{removed} removed"));
        }
        let mut text = if bits.is_empty() { "nothing changed".to_string() } else { bits.join(", ") };
        if !self.refused.is_empty() {
            text.push_str(&format!(" — {} refused", self.refused.len()));
        }
        text
    }
}

fn grid_note(fund: Fund, transaction_type: &str) -> String {
    if fund == Fund::Benevolent && transaction_type == WITHDRAWAL {
        "Benevolent Payout (grid entry)".into()
    } else {
        format!("{} (grid entry)", fund.label())
    }
}

fn current_fingerprint(journal: &Journal, fund: Fund, member_id: i64, month: &str) -> Result<Fingerprint> {
    let Some((start, end)) = month_bounds(&[month.to_string()]) else { return Ok(Vec::new()) };
    let rows = journal.funds().month_rows(fund, member_id, &start, &end)?;
    Ok(fingerprint_of(rows.iter().map(|r| (r.id, r.amount, r.date.as_str(), r.transaction_type.as_str()))))
}

/// Why a change may not be written, or `None`. The posting pipeline's rules
/// for money going in; the unlock month for Christmas money coming out.
fn refusal_for(journal: &Journal, fund: Fund, change: &Change, date: &str) -> Result<Option<String>> {
    match change.kind {
        ChangeKind::Delete => return Ok(None),
        ChangeKind::Set => {
            // One refusal holds back the whole cell: half a month is not what
            // anybody typed.
            for step in plan_set(journal, fund, change)? {
                if step.action == StepAction::Delete {
                    continue;
                }
                let signed = if step.transaction_type == WITHDRAWAL { -step.amount } else { step.amount };
                let probe = Change { amount: signed, ..Change::new(fund, change.member_id, &change.month, ChangeKind::Insert) };
                if let Some(reason) = refusal_for(journal, fund, &probe, date)? {
                    return Ok(Some(reason));
                }
            }
            return Ok(None);
        }
        _ => {}
    }
    if change.amount < 0.0 {
        if fund == Fund::Christmas {
            let unlock = journal.settings().christmas_unlock_month();
            if dates::parse(date).map(|d| chrono::Datelike::month(&d)) != Some(unlock) {
                return Ok(Some(format!(
                    "Christmas withdrawals are only allowed in {}. Use the member's Christmas tab to override.",
                    dates::month_name(unlock)
                )));
            }
        }
        return Ok(None); // a member who has left may still take their money out
    }
    let (_tidy, problems) = check_posting(
        journal,
        Posting::new(fund, change.member_id, date, change.transaction_type(), change.magnitude(), ""),
    )?;
    Ok((!problems.is_empty()).then(|| problems.join(" ")))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum StepAction {
    Insert,
    Update,
    Delete,
}

#[derive(Debug, Clone)]
struct Step {
    action: StepAction,
    transaction_type: String,
    amount: f64,
    row: Option<FundRow>,
}

/// The row edits that make a month hold exactly `change.entries`. An entry
/// whose amount a row already has keeps that row; the rest are paired in date
/// order; leftovers are inserted or deleted.
fn plan_set(journal: &Journal, fund: Fund, change: &Change) -> Result<Vec<Step>> {
    let Some((start, end)) = month_bounds(std::slice::from_ref(&change.month)) else { return Ok(Vec::new()) };
    let rows = journal.funds().month_rows(fund, change.member_id, &start, &end)?;
    let mut wanted_by_type: Vec<(String, Vec<f64>)> = Vec::new();
    for entry in &change.entries {
        let kind = type_for(fund, entry.op).to_string();
        match wanted_by_type.iter_mut().find(|(k, _)| *k == kind) {
            Some((_, amounts)) => amounts.push(round2(entry.amount)),
            None => wanted_by_type.push((kind, vec![round2(entry.amount)])),
        }
    }
    let mut types: Vec<String> = wanted_by_type.iter().map(|(k, _)| k.clone()).collect();
    for row in &rows {
        if !types.contains(&row.transaction_type) {
            types.push(row.transaction_type.clone());
        }
    }
    let mut steps = Vec::new();
    for kind in types {
        let mut wanted = wanted_by_type.iter().find(|(k, _)| *k == kind).map(|(_, a)| a.clone()).unwrap_or_default();
        let mut free: Vec<&FundRow> = rows.iter().filter(|r| r.transaction_type == kind).collect();
        for amount in wanted.clone() {
            if let Some(at) = free.iter().position(|r| round2(r.amount) == amount) {
                free.remove(at);
                let index = wanted.iter().position(|a| *a == amount).unwrap();
                wanted.remove(index);
            }
        }
        let paired = free.len().min(wanted.len());
        for (row, amount) in free.iter().zip(&wanted).take(paired) {
            steps.push(Step { action: StepAction::Update, transaction_type: kind.clone(), amount: *amount, row: Some((*row).clone()) });
        }
        for amount in &wanted[paired..] {
            steps.push(Step { action: StepAction::Insert, transaction_type: kind.clone(), amount: *amount, row: None });
        }
        for row in &free[paired..] {
            steps.push(Step { action: StepAction::Delete, transaction_type: kind.clone(), amount: round2(row.amount), row: Some((*row).clone()) });
        }
    }
    Ok(steps)
}

fn write(journal: &Journal, fund: Fund, change: &Change, date: &str) -> Result<Restore> {
    let funds = journal.funds();
    match change.kind {
        ChangeKind::Insert => {
            let kind = change.transaction_type();
            let id = funds.add(fund, change.member_id, date, kind, change.magnitude(), &grid_note(fund, kind), None)?;
            Ok(Restore::Inserted(id))
        }
        ChangeKind::Set => {
            let mut undo = Vec::new();
            for step in plan_set(journal, fund, change)? {
                match step.action {
                    StepAction::Insert => {
                        let id = funds.add(
                            fund,
                            change.member_id,
                            date,
                            &step.transaction_type,
                            step.amount,
                            &grid_note(fund, &step.transaction_type),
                            None,
                        )?;
                        undo.push(Restore::Inserted(id));
                    }
                    StepAction::Update => {
                        let row = step.row.expect("an update has its row");
                        undo.push(Restore::Updated {
                            id: row.id,
                            date: row.date.clone(),
                            amount: row.amount,
                            notes: row.notes.clone(),
                            kind: row.transaction_type.clone(),
                        });
                        funds.update(fund, row.id, dates::date_part(&row.date), step.amount, &row.notes)?;
                    }
                    StepAction::Delete => {
                        let row = step.row.expect("a delete has its row");
                        let snapshot = funds
                            .snapshot(fund, row.id)?
                            .ok_or_else(|| crate::Error::invalid("An entry in this month no longer exists."))?;
                        undo.push(Restore::Deleted(snapshot));
                        funds.delete(fund, row.id)?;
                    }
                }
            }
            Ok(Restore::Steps(undo))
        }
        ChangeKind::Update | ChangeKind::Delete => {
            let row_id = change.row_id.ok_or_else(|| crate::Error::invalid("That entry no longer exists."))?;
            let before = funds.get(fund, row_id)?.ok_or_else(|| crate::Error::invalid("That entry no longer exists."))?;
            if change.kind == ChangeKind::Delete {
                let snapshot = funds.snapshot(fund, row_id)?.expect("the row was just read");
                funds.delete(fund, row_id)?;
                return Ok(Restore::Deleted(snapshot));
            }
            funds.update(fund, row_id, dates::date_part(&before.date), change.magnitude(), &before.notes)?;
            if before.transaction_type != change.transaction_type() {
                funds.set_type(fund, row_id, change.transaction_type())?;
            }
            Ok(Restore::Updated {
                id: row_id,
                date: before.date,
                amount: before.amount,
                notes: before.notes,
                kind: before.transaction_type,
            })
        }
    }
}

fn undo_restore(journal: &Journal, fund: Fund, restore: &Restore) -> Result<()> {
    let funds = journal.funds();
    match restore {
        Restore::Inserted(id) => funds.delete(fund, *id),
        Restore::Updated { id, date, amount, notes, kind } => {
            funds.update(fund, *id, dates::date_part(date), *amount, notes)?;
            funds.set_type(fund, *id, kind)
        }
        Restore::Deleted(snapshot) => funds.reinstate(fund, snapshot),
        Restore::Steps(steps) => {
            for step in steps.iter().rev() {
                undo_restore(journal, fund, step)?;
            }
            Ok(())
        }
    }
}

/// The lowest running balance in a `(id, date, type, amount)` history, and
/// the `(date, amount)` of the withdrawal it falls at.
fn low_point(history: &[(i64, String, String, f64)]) -> (f64, Option<(String, f64)>) {
    let (mut running, mut low, mut at) = (0.0, 0.0, None);
    for (_, date, kind, amount) in history {
        running = round2(if kind == WITHDRAWAL { running - amount } else { running + amount });
        if running < low - 0.005 {
            low = running;
            at = Some((dates::date_part(date).to_string(), *amount));
        }
    }
    (low, at)
}

/// Why `after` may not replace `before`: a withdrawal left bigger than the
/// balance it came from. Already-overdrawn history is not a reason; only a
/// change that makes it worse is.
pub fn overdraw_reason(before: &[(i64, String, String, f64)], after: &[(i64, String, String, f64)]) -> Option<String> {
    let (was, _) = low_point(before);
    let (now, at) = low_point(after);
    if now >= was.min(0.0) - 0.005 {
        return None;
    }
    let (date, amount) = at?;
    Some(format!(
        "This would leave the withdrawal of {} on {date} taken from {} — overdrawn by {}. Include that \
         withdrawal's month, or correct it in the member's ledger.",
        shillings(amount),
        shillings(amount + now),
        shillings(-now)
    ))
}

/// Write a batch of staged edits: one transaction, per-cell refusals, the
/// overdraw guard per member, balances re-derived once per member.
pub fn apply_changes(journal: &Journal, fund: Fund, changes: &[Change]) -> Result<BatchResult> {
    let mut result =
        BatchResult { fund, applied: Vec::new(), refused: Vec::new(), members_touched: BTreeSet::new() };
    if changes.is_empty() {
        return Ok(result);
    }
    let mut by_member: Vec<(i64, Vec<&Change>)> = Vec::new();
    for change in changes {
        match by_member.iter_mut().find(|(m, _)| *m == change.member_id) {
            Some((_, list)) => list.push(change),
            None => by_member.push((change.member_id, vec![change])),
        }
    }
    journal.transaction(|| {
        for (member_id, member_changes) in &by_member {
            let before = journal.funds().history(fund, *member_id)?;
            let mut written = Vec::new();
            let mut guard_refusal = None;
            let outcome = journal.savepoint(|| {
                for change in member_changes {
                    if let Some(applied) = apply_one(journal, fund, change, &mut result.refused)? {
                        written.push(applied);
                    }
                }
                if own_money(fund) && !written.is_empty() {
                    guard_refusal = overdraw_reason(&before, &journal.funds().history(fund, *member_id)?);
                    if guard_refusal.is_some() {
                        return Err(crate::Error::invalid("overdrawn"));
                    }
                }
                Ok(())
            });
            match (outcome, guard_refusal) {
                (Err(_), Some(reason)) => {
                    result.refused.extend(written.drain(..).map(|a| Refused { change: a.change, reason: reason.clone() }));
                }
                (Err(e), None) => return Err(e),
                (Ok(()), _) => {
                    if !written.is_empty() {
                        result.members_touched.insert(*member_id);
                    }
                    result.applied.extend(written);
                }
            }
        }
        for member_id in &result.members_touched {
            journal.funds().recalculate(fund, *member_id)?;
            if fund == Fund::Benevolent {
                resync_benevolent_schedule(journal, *member_id)?;
            }
        }
        Ok(())
    })?;
    Ok(result)
}

fn apply_one(journal: &Journal, fund: Fund, change: &Change, refused: &mut Vec<Refused>) -> Result<Option<Applied>> {
    let date = default_day(journal, fund, change.member_id, &change.month)?;
    // Edited from another screen while the grid sat open: the staged figure
    // was typed against figures that have since moved.
    if change.before != current_fingerprint(journal, fund, change.member_id, &change.month)? {
        refused.push(Refused {
            change: change.clone(),
            reason: "This month changed elsewhere since the grid was loaded. Reload to see the current figures.".into(),
        });
        return Ok(None);
    }
    if let Some(reason) = refusal_for(journal, fund, change, &date)? {
        refused.push(Refused { change: change.clone(), reason });
        return Ok(None);
    }
    // A month can be several row writes; one failing must not leave the others.
    match journal.savepoint(|| write(journal, fund, change, &date)) {
        Ok(restore) => Ok(Some(Applied { change: change.clone(), restore })),
        Err(e) if e.is_user_facing() => {
            refused.push(Refused { change: change.clone(), reason: e.to_string() });
            Ok(None)
        }
        Err(e) => {
            log::error!("Fund grid change failed for member {} {}: {e}", change.member_id, change.month);
            refused.push(Refused { change: change.clone(), reason: e.to_string() });
            Ok(None)
        }
    }
}

/// Put a batch back exactly as it was — the grid's own undo.
pub fn revert(journal: &Journal, fund: Fund, result: &BatchResult) -> Result<BTreeSet<i64>> {
    let mut touched = BTreeSet::new();
    journal.transaction(|| {
        for applied in result.applied.iter().rev() {
            touched.insert(applied.change.member_id);
            undo_restore(journal, fund, &applied.restore)?;
        }
        for member_id in &touched {
            journal.funds().recalculate(fund, *member_id)?;
            if fund == Fund::Benevolent {
                resync_benevolent_schedule(journal, *member_id)?;
            }
        }
        Ok(())
    })?;
    Ok(touched)
}

/// Point the benevolent schedule at the month after the last contribution,
/// or the enrolment's start when none is left — otherwise the next catch-up
/// re-posts every month the grid just filled.
pub fn resync_benevolent_schedule(journal: &Journal, member_id: i64) -> Result<Option<String>> {
    let Some(account) = journal.funds().enrolment(member_id)? else { return Ok(None) };
    let due = match journal.funds().last_dated_row(Fund::Benevolent, member_id, &["Contribution"])? {
        Some(last) => match dates::add_months_str(&last.date, 1) {
            Some(due) => Some(due),
            None => return Ok(None),
        },
        None => account.start_date.clone(),
    };
    if let Some(due) = &due {
        journal.funds().set_next_due(member_id, due)?;
    }
    Ok(due)
}

// ------------------------------------------------------------ fill forward

/// The member's usual monthly figure: the enrolled amount for benevolent,
/// otherwise their most common credit in view (ties: the first seen), then
/// their latest credit, then the journal's default. 0 means nothing to go on.
pub fn suggested_amount(journal: &Journal, fund: Fund, row: &MemberRow) -> Result<f64> {
    if fund == Fund::Benevolent {
        return Ok(round2(row.monthly_amount));
    }
    let mut amounts: Vec<f64> = Vec::new();
    let mut months: Vec<&String> = row.cells.keys().collect();
    months.sort();
    for month in months {
        for r in &row.cells[month].rows {
            if r.transaction_type == fund.credit() {
                amounts.push(round2(r.amount));
            }
        }
    }
    if amounts.is_empty() {
        if let Some(last) = journal.funds().last_dated_row(fund, row.member_id, &[fund.credit()])? {
            return Ok(round2(last.amount));
        }
        return Ok(round2(if fund == Fund::Savings {
            journal.settings().default_savings_increment()
        } else {
            journal.settings().default_christmas_increment()
        }));
    }
    let best = amounts.iter().map(|a| amounts.iter().filter(|b| *b == a).count()).max().unwrap_or(0);
    Ok(amounts.iter().copied().find(|a| amounts.iter().filter(|b| *b == a).count() == best).unwrap_or(0.0))
}

/// What a fill proposes, and what it deliberately left alone.
#[derive(Debug, Clone, Default)]
pub struct FillPlan {
    pub changes: Vec<Change>,
    pub skipped: Vec<(i64, String)>,
    /// Cells skipped because the account did not exist yet.
    pub before_start: usize,
    /// Cells skipped because the month has not happened.
    pub in_future: usize,
}

impl FillPlan {
    pub fn members(&self) -> usize {
        self.changes.iter().map(|c| c.member_id).collect::<BTreeSet<_>>().len()
    }

    pub fn notes(&self) -> Vec<String> {
        let mut out = Vec::new();
        if self.before_start > 0 {
            out.push(format!("{} month(s) before the member's account began were left empty.", self.before_start));
        }
        if self.in_future > 0 {
            out.push(format!("{} month(s) in the future were left empty.", self.in_future));
        }
        out
    }
}

/// Propose each member's usual credit for every month that has none — a month
/// holding only a withdrawal still owed its deposit — never before the
/// account began, never after this month. A proposal, staged for review.
pub fn plan_fill(journal: &Journal, grid: &Grid, rows: &[&MemberRow], months: &[String], today: Option<&str>) -> Result<FillPlan> {
    let today = today.map(str::to_string).unwrap_or_else(dates::today);
    let this_month = month_key(&today);
    let ops = operations(grid.fund);
    let noun = if grid.fund == Fund::Benevolent { "contribution" } else { "deposit" };
    let mut plan = FillPlan::default();
    for row in rows {
        let skip = |reason: String| (row.member_id, reason);
        if !row.active() {
            plan.skipped.push(skip(format!("{} has left the SACCO.", row.name)));
            continue;
        }
        if row.excluded {
            plan.skipped.push(skip(format!("{} is excluded from this ledger.", row.name)));
            continue;
        }
        if grid.fund == Fund::Benevolent && !row.enrolled {
            plan.skipped.push(skip(format!("{} is not enrolled.", row.name)));
            continue;
        }
        let Some(account_start) = &row.account_start else {
            plan.skipped.push(skip(format!(
                "Nothing on record says when {}'s {} account began — record their first {noun}, then fill.",
                row.name,
                grid.fund.label()
            )));
            continue;
        };
        let amount = suggested_amount(journal, grid.fund, row)?;
        if amount <= 0.0 {
            plan.skipped.push(skip(format!("No {noun} history to infer an amount from.")));
            continue;
        }
        let mut fillable = Vec::new();
        for month in months {
            let Some(current) = cell_entries(grid.fund, row.cell(month)) else { continue };
            if current.iter().any(|e| ops.is_default(e.op)) {
                continue;
            }
            if month < account_start {
                plan.before_start += 1;
            } else if *month > this_month {
                plan.in_future += 1;
            } else {
                fillable.push(month.clone());
            }
        }
        if fillable.is_empty() {
            plan.skipped.push(skip(format!("No missed months in range for {}.", row.name)));
            continue;
        }
        for month in fillable {
            let cell = row.cell(&month);
            if cell.rows.is_empty() {
                plan.changes.push(Change {
                    amount,
                    before: cell.fingerprint(),
                    ..Change::new(grid.fund, row.member_id, &month, ChangeKind::Insert)
                });
            } else {
                // The deposit joins what the month holds; the change is the month.
                let mut entries = vec![Entry::new(ops.default, amount)];
                entries.extend(cell_entries(grid.fund, cell).unwrap_or_default());
                plan.changes.push(Change {
                    amount: round2(amount + cell.net),
                    before: cell.fingerprint(),
                    entries,
                    ..Change::new(grid.fund, row.member_id, &month, ChangeKind::Set)
                });
            }
        }
    }
    Ok(plan)
}

/// Members who may not receive new credits, with the reason — to grey their
/// rows before anything is typed.
pub fn blocked_members(journal: &Journal, fund: Fund, member_ids: &[i64]) -> Result<HashMap<i64, String>> {
    let kind = match fund {
        Fund::Savings => "savings.deposit",
        Fund::Christmas => "christmas.deposit",
        Fund::Benevolent => "benevolent.contribute",
    };
    let mut out = HashMap::new();
    for id in member_ids {
        if let Some(reason) = policy::blocked_reason(journal, kind, *id)? {
            out.insert(*id, reason);
        }
    }
    Ok(out)
}

// ------------------------------------------------------- deleting months

/// Plan deleting `months` for the given (on-screen) rows — docs §3. Every row
/// an operation describes goes; a month with a row none describes is left
/// whole; a member whose deletions would leave a withdrawal overdrawn is
/// refused as a whole.
pub fn plan_delete_months(journal: &Journal, grid: &Grid, rows: &[&MemberRow], months: &[String]) -> Result<DeletePlan> {
    let mut months = months.to_vec();
    months.sort();
    let mut plan = DeletePlan::new(&months);
    let shown: BTreeSet<i64> = rows.iter().filter(|r| !r.excluded).map(|r| r.member_id).collect();
    let noun = if grid.fund == Fund::Benevolent { "contribution" } else { "deposit" };

    for row in rows.iter().filter(|r| !r.excluded) {
        let mut staged = Vec::new();
        let mut removed = BTreeSet::new();
        for month in &months {
            let cell = row.cell(month);
            if cell.rows.is_empty() {
                continue;
            }
            match stage(grid, row, month, "") {
                Err(problem) => plan.refuse(&row.name, format!("{}: {problem}", month_label(month))),
                Ok(Some(change)) => {
                    removed.extend(cell.rows.iter().map(|r| r.id));
                    staged.push((change, cell));
                }
                Ok(None) => {}
            }
        }
        if staged.is_empty() {
            continue;
        }
        if own_money(grid.fund) {
            let history = journal.funds().history(grid.fund, row.member_id)?;
            let after: Vec<_> = history.iter().filter(|h| !removed.contains(&h.0)).cloned().collect();
            if let Some(reason) = overdraw_reason(&history, &after) {
                plan.refuse(&row.name, reason);
                continue;
            }
        }
        for (change, cell) in staged {
            for r in &cell.rows {
                let label = if r.is_withdrawal() {
                    if grid.fund == Fund::Benevolent { "payouts".to_string() } else { "withdrawals".to_string() }
                } else {
                    format!("{noun}s")
                };
                plan.tally(&label, r.amount);
            }
            plan.fund_changes.push(change);
        }
    }

    let mut not_shown = 0;
    for month in &months {
        let Some((start, end)) = month_bounds(std::slice::from_ref(month)) else { continue };
        not_shown += journal
            .funds()
            .movements_in_range(grid.fund, &start, &end)?
            .iter()
            .filter(|r| !shown.contains(&r.individual_id))
            .count();
    }
    if not_shown > 0 {
        plan.untouched.push(format!(
            "{not_shown} transaction(s) belonging to members not on screen (filtered out, past members, or \
             excluded) are not touched."
        ));
    }
    if !plan.refused.is_empty() {
        plan.untouched.push(format!(
            "{} member-month(s) are refused — listed below — and keep their transactions.",
            plan.refused.len()
        ));
    }
    if !plan.fund_changes.is_empty() {
        plan.consequences.push("Running balances after these months are recalculated for every member affected.".into());
        if plan.tallies.iter().any(|(label, _)| label.starts_with(noun)) {
            plan.consequences.push(format!(
                "These become missed months: Fill Forward will offer each member's {noun} for them again."
            ));
        }
        if grid.fund == Fund::Benevolent {
            plan.consequences.push(
                "Each member's contribution schedule is re-derived from their last remaining contribution — \
                 deleting the latest months moves it back."
                    .into(),
            );
        }
    }
    Ok(plan)
}

#[cfg(test)]
mod tests;
