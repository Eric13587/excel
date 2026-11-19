//! What one tab of the Ledger Grid is: the fund grids (a member per row) and
//! the loan grid (a loan per row) behind one interface, so the screen —
//! staging, clipboard, totals, Fill Forward, deleting months, save and undo —
//! is written once.

use std::collections::HashMap;

use loanmaster_core::db::model::{Fund, LOAN_ACTIVE};
use loanmaster_core::funds::suspensions;
use loanmaster_core::grid::ops::OpSet;
use loanmaster_core::grid::{DeletePlan, fund, loan};
use loanmaster_core::money::shillings;
use loanmaster_core::{Journal, Result, dates};

/// What Fill Forward proposes.
pub struct Fill<C> {
    pub changes: Vec<C>,
    /// Why rows were left out.
    pub skipped: Vec<String>,
    pub notes: Vec<String>,
    /// How many rows the proposals touch.
    pub rows: usize,
}

pub trait Sheet: 'static {
    type Row;
    type Grid;
    type Change: Clone;
    type Batch;

    /// "Savings / Shares", "Loans".
    fn label(&self) -> &'static str;
    /// What one cell's usual entry is called: deposit, contribution, deduction.
    fn noun(&self) -> &'static str;
    /// What a row is: member, loan.
    fn per_row(&self) -> &'static str;
    /// The scope exclusions are recorded under.
    fn scope(&self) -> &'static str;
    fn ops(&self) -> &'static OpSet;
    fn is_loans(&self) -> bool {
        false
    }

    fn load(&self, journal: &Journal, months: &[String], include_inactive: bool, include_excluded: bool, include_paid: bool) -> Result<Self::Grid>;
    fn rows<'g>(&self, grid: &'g Self::Grid) -> &'g [Self::Row];

    fn key(&self, row: &Self::Row) -> i64;
    fn member_id(&self, row: &Self::Row) -> i64;
    fn name<'r>(&self, row: &'r Self::Row) -> &'r str;
    /// How a row is named in messages: the member, and the loan if any.
    fn row_label(&self, row: &Self::Row) -> String;
    fn excluded(&self, row: &Self::Row) -> bool;

    /// The read-only columns before the months: `(heading, numeric)`.
    fn lead_columns(&self) -> &'static [(&'static str, bool)];
    fn lead_values(&self, row: &Self::Row) -> Vec<String>;
    /// A numeric lead column's value, for its total.
    fn lead_number(&self, row: &Self::Row, column: usize) -> Option<f64>;
    /// After the months: the period's total (worked out by the screen, so it
    /// follows staged edits) and one more figure.
    fn tail_columns(&self) -> [&'static str; 2];
    fn closing(&self, row: &Self::Row) -> f64;

    fn net(&self, row: &Self::Row, month: &str) -> f64;
    fn has_rows(&self, row: &Self::Row, month: &str) -> bool;
    fn refusal(&self, grid: &Self::Grid, row: &Self::Row, month: &str) -> Option<String>;
    /// The cell's text when one figure cannot say it, else `None`.
    fn display_text(&self, row: &Self::Row, month: &str) -> Option<String>;
    /// What editing a cell starts from.
    fn edit_text(&self, row: &Self::Row, month: &str) -> String;
    /// The cell's entries in words, for its tooltip.
    fn rows_tooltip(&self, row: &Self::Row, month: &str) -> Option<String>;
    /// A remark on an ordinary cell: a missed loan month.
    fn cell_note(&self, row: &Self::Row, month: &str) -> Option<String>;
    /// Why a whole row is greyed.
    fn row_note(&self, row: &Self::Row, blocked: &HashMap<i64, String>) -> Option<String>;

    fn stage(&self, grid: &Self::Grid, row: &Self::Row, month: &str, text: &str) -> std::result::Result<Option<Self::Change>, String>;
    fn change_key(&self, change: &Self::Change) -> i64;
    fn change_month(&self, change: &Self::Change) -> String;
    /// What the cell nets to once the change is saved.
    fn effect(&self, change: &Self::Change) -> f64;
    fn is_delete(&self, change: &Self::Change) -> bool;
    fn staged_text(&self, change: &Self::Change) -> Option<String>;
    fn staged_edit_text(&self, change: &Self::Change) -> String;
    fn describe(&self, row: Option<&Self::Row>, change: &Self::Change, staged: &[&Self::Change]) -> String;

    fn apply(&self, journal: &Journal, changes: &[Self::Change]) -> Result<Self::Batch>;
    fn revert(&self, journal: &Journal, batch: &Self::Batch) -> Result<()>;
    fn summary(&self, batch: &Self::Batch) -> String;
    fn applied(&self, batch: &Self::Batch) -> usize;
    fn refused(&self, batch: &Self::Batch) -> Vec<(Self::Change, String)>;

    fn fill(&self, journal: &Journal, grid: &Self::Grid, rows: &[&Self::Row], months: &[String]) -> Result<Fill<Self::Change>>;
    fn blocked(&self, journal: &Journal, rows: &[&Self::Row]) -> Result<HashMap<i64, String>>;
    fn plan_delete(&self, journal: &Journal, grid: &Self::Grid, rows: &[&Self::Row], months: &[String]) -> Result<(DeletePlan, Vec<Self::Change>)>;
}

// ----------------------------------------------------------------- funds

pub struct FundSheet(pub Fund);

impl Sheet for FundSheet {
    type Row = fund::MemberRow;
    type Grid = fund::Grid;
    type Change = fund::Change;
    type Batch = fund::BatchResult;

    fn label(&self) -> &'static str {
        self.0.label()
    }

    fn noun(&self) -> &'static str {
        if self.0 == Fund::Benevolent { "contribution" } else { "deposit" }
    }

    fn per_row(&self) -> &'static str {
        "member"
    }

    fn scope(&self) -> &'static str {
        self.0.table()
    }

    fn ops(&self) -> &'static OpSet {
        fund::operations(self.0)
    }

    fn load(&self, journal: &Journal, months: &[String], include_inactive: bool, include_excluded: bool, _: bool) -> Result<fund::Grid> {
        fund::load(journal, self.0, months, include_inactive, include_excluded)
    }

    fn rows<'g>(&self, grid: &'g fund::Grid) -> &'g [fund::MemberRow] {
        &grid.members
    }

    fn key(&self, row: &fund::MemberRow) -> i64 {
        row.member_id
    }

    fn member_id(&self, row: &fund::MemberRow) -> i64 {
        row.member_id
    }

    fn name<'r>(&self, row: &'r fund::MemberRow) -> &'r str {
        &row.name
    }

    fn row_label(&self, row: &fund::MemberRow) -> String {
        row.name.clone()
    }

    fn excluded(&self, row: &fund::MemberRow) -> bool {
        row.excluded
    }

    fn lead_columns(&self) -> &'static [(&'static str, bool)] {
        &[("Member", false), ("PF No", false), ("Opening", true)]
    }

    fn lead_values(&self, row: &fund::MemberRow) -> Vec<String> {
        vec![row.name.clone(), row.pf_no.clone(), shillings(row.opening_balance)]
    }

    fn lead_number(&self, row: &fund::MemberRow, column: usize) -> Option<f64> {
        (column == 2).then_some(row.opening_balance)
    }

    fn tail_columns(&self) -> [&'static str; 2] {
        ["Period", "Balance"]
    }

    fn closing(&self, row: &fund::MemberRow) -> f64 {
        row.total_balance
    }

    fn net(&self, row: &fund::MemberRow, month: &str) -> f64 {
        row.cell(month).net
    }

    fn has_rows(&self, row: &fund::MemberRow, month: &str) -> bool {
        !row.cell(month).rows.is_empty()
    }

    fn refusal(&self, grid: &fund::Grid, row: &fund::MemberRow, month: &str) -> Option<String> {
        fund::cell_refusal(grid, row, month)
    }

    fn display_text(&self, row: &fund::MemberRow, month: &str) -> Option<String> {
        let cell = row.cell(month);
        // A suspended month with nothing in it says so.
        if cell.rows.is_empty() && row.suspended_in(month).is_some() {
            return Some("⏸".into());
        }
        let entries = fund::cell_entries(self.0, cell).unwrap_or_default();
        (entries.len() > 1).then(|| self.ops().format(&entries))
    }

    fn edit_text(&self, row: &fund::MemberRow, month: &str) -> String {
        self.ops().format(&fund::cell_entries(self.0, row.cell(month)).unwrap_or_default())
    }

    fn rows_tooltip(&self, row: &fund::MemberRow, month: &str) -> Option<String> {
        let mut rows: Vec<_> = row.cell(month).rows.iter().collect();
        if rows.is_empty() {
            return None;
        }
        rows.sort_by(|a, b| (dates::date_part(&a.date), a.id).cmp(&(dates::date_part(&b.date), b.id)));
        Some(
            rows.iter()
                .map(|r| {
                    let notes = loanmaster_core::notes::fund_note(&r.transaction_type, &r.notes);
                    let notes = if notes.is_empty() { String::new() } else { format!(" — {notes}") };
                    format!("{} of {} on {}{notes}", r.transaction_type, shillings(r.amount), dates::date_part(&r.date))
                })
                .collect::<Vec<_>>()
                .join("\n"),
        )
    }

    fn cell_note(&self, _: &fund::MemberRow, _: &str) -> Option<String> {
        None
    }

    fn row_note(&self, row: &fund::MemberRow, blocked: &HashMap<i64, String>) -> Option<String> {
        if row.excluded {
            let note = if row.exclusion_note.is_empty() { String::new() } else { format!(" {}", row.exclusion_note) };
            return Some(format!("{} is excluded from this ledger.{note}", row.name));
        }
        if !row.active() {
            return Some(blocked.get(&row.member_id).cloned().unwrap_or_else(|| row.status.clone()));
        }
        if self.0 == Fund::Benevolent && !row.enrolled {
            return Some("Not enrolled in the benevolent fund.".into());
        }
        if let Some(window) = row.suspended_in(&dates::month_key(&dates::today())) {
            return Some(format!("{}'s savings contributions are suspended {}.", row.name, suspensions::describe(window)));
        }
        None
    }

    fn stage(&self, grid: &fund::Grid, row: &fund::MemberRow, month: &str, text: &str) -> std::result::Result<Option<fund::Change>, String> {
        fund::stage(grid, row, month, text)
    }

    fn change_key(&self, change: &fund::Change) -> i64 {
        change.member_id
    }

    fn change_month(&self, change: &fund::Change) -> String {
        change.month.clone()
    }

    fn effect(&self, change: &fund::Change) -> f64 {
        change.effect()
    }

    fn is_delete(&self, change: &fund::Change) -> bool {
        change.kind == fund::ChangeKind::Delete
    }

    fn staged_text(&self, change: &fund::Change) -> Option<String> {
        let entries = fund::change_entries(change);
        (entries.len() > 1 || change.kind == fund::ChangeKind::Suspend).then(|| self.ops().format(&entries))
    }

    fn staged_edit_text(&self, change: &fund::Change) -> String {
        self.ops().format(&fund::change_entries(change))
    }

    fn describe(&self, _: Option<&fund::MemberRow>, change: &fund::Change, _: &[&fund::Change]) -> String {
        fund::describe(change)
    }

    fn apply(&self, journal: &Journal, changes: &[fund::Change]) -> Result<fund::BatchResult> {
        fund::apply_changes(journal, self.0, changes)
    }

    fn revert(&self, journal: &Journal, batch: &fund::BatchResult) -> Result<()> {
        fund::revert(journal, self.0, batch).map(|_| ())
    }

    fn summary(&self, batch: &fund::BatchResult) -> String {
        batch.summary()
    }

    fn applied(&self, batch: &fund::BatchResult) -> usize {
        batch.applied.len()
    }

    fn refused(&self, batch: &fund::BatchResult) -> Vec<(fund::Change, String)> {
        batch.refused.iter().map(|r| (r.change.clone(), r.reason.clone())).collect()
    }

    fn fill(&self, journal: &Journal, grid: &fund::Grid, rows: &[&fund::MemberRow], months: &[String]) -> Result<Fill<fund::Change>> {
        let plan = fund::plan_fill(journal, grid, rows, months, None)?;
        Ok(Fill {
            rows: plan.members(),
            skipped: plan.skipped.iter().map(|(_, why)| why.clone()).collect(),
            notes: plan.notes(),
            changes: plan.changes,
        })
    }

    fn blocked(&self, journal: &Journal, rows: &[&fund::MemberRow]) -> Result<HashMap<i64, String>> {
        let ids: Vec<i64> = rows.iter().map(|r| r.member_id).collect();
        fund::blocked_members(journal, self.0, &ids)
    }

    fn plan_delete(&self, journal: &Journal, grid: &fund::Grid, rows: &[&fund::MemberRow], months: &[String]) -> Result<(DeletePlan, Vec<fund::Change>)> {
        let plan = fund::plan_delete_months(journal, grid, rows, months)?;
        let changes = plan.fund_changes.clone();
        Ok((plan, changes))
    }
}

// ----------------------------------------------------------------- loans

pub struct LoanSheet;

impl Sheet for LoanSheet {
    type Row = loan::LoanRow;
    type Grid = loan::LoanGrid;
    type Change = loan::Change;
    type Batch = loan::BatchResult;

    fn label(&self) -> &'static str {
        "Loans"
    }

    fn noun(&self) -> &'static str {
        "deduction"
    }

    fn per_row(&self) -> &'static str {
        "loan"
    }

    fn scope(&self) -> &'static str {
        "loans"
    }

    fn ops(&self) -> &'static OpSet {
        &loan::OPS
    }

    fn is_loans(&self) -> bool {
        true
    }

    fn load(&self, journal: &Journal, months: &[String], include_inactive: bool, include_excluded: bool, include_paid: bool) -> Result<loan::LoanGrid> {
        loan::load(journal, months, include_inactive, include_paid, include_excluded)
    }

    fn rows<'g>(&self, grid: &'g loan::LoanGrid) -> &'g [loan::LoanRow] {
        &grid.rows
    }

    fn key(&self, row: &loan::LoanRow) -> i64 {
        row.loan_id
    }

    fn member_id(&self, row: &loan::LoanRow) -> i64 {
        row.member_id
    }

    fn name<'r>(&self, row: &'r loan::LoanRow) -> &'r str {
        &row.name
    }

    fn row_label(&self, row: &loan::LoanRow) -> String {
        row.label()
    }

    fn excluded(&self, row: &loan::LoanRow) -> bool {
        row.excluded
    }

    fn lead_columns(&self) -> &'static [(&'static str, bool)] {
        &[("Member", false), ("Loan", false), ("Due", false)]
    }

    fn lead_values(&self, row: &loan::LoanRow) -> Vec<String> {
        let due = if row.status == LOAN_ACTIVE { row.next_due_month().map(|m| dates::month_label(&m)).unwrap_or_else(|| "—".into()) } else { row.status.clone() };
        vec![row.name.clone(), row.reference.clone(), due]
    }

    fn lead_number(&self, _: &loan::LoanRow, _: usize) -> Option<f64> {
        None
    }

    fn tail_columns(&self) -> [&'static str; 2] {
        ["Paid in period", "Outstanding"]
    }

    fn closing(&self, row: &loan::LoanRow) -> f64 {
        row.balance
    }

    fn net(&self, row: &loan::LoanRow, month: &str) -> f64 {
        row.cell(month).net
    }

    fn has_rows(&self, row: &loan::LoanRow, month: &str) -> bool {
        !row.cell(month).rows.is_empty()
    }

    fn refusal(&self, grid: &loan::LoanGrid, row: &loan::LoanRow, month: &str) -> Option<String> {
        loan::cell_refusal(grid, row, month)
    }

    fn display_text(&self, _: &loan::LoanRow, _: &str) -> Option<String> {
        None
    }

    fn edit_text(&self, row: &loan::LoanRow, month: &str) -> String {
        loan::edit_text(row, month)
    }

    fn rows_tooltip(&self, row: &loan::LoanRow, month: &str) -> Option<String> {
        let rows = &row.cell(month).rows;
        (!rows.is_empty()).then(|| {
            rows.iter()
                .map(|r| {
                    let notes = loanmaster_core::notes::loan_note(&r.notes);
                    let notes = if notes.is_empty() { String::new() } else { format!(" — {notes}") };
                    format!("{} of {} on {}{notes}", r.kind, shillings(r.amount), dates::date_part(&r.date))
                })
                .collect::<Vec<_>>()
                .join("\n")
        })
    }

    fn cell_note(&self, row: &loan::LoanRow, month: &str) -> Option<String> {
        loan::cell_note(row, month)
    }

    fn row_note(&self, row: &loan::LoanRow, blocked: &HashMap<i64, String>) -> Option<String> {
        loan::row_note(row, blocked)
    }

    fn stage(&self, grid: &loan::LoanGrid, row: &loan::LoanRow, month: &str, text: &str) -> std::result::Result<Option<loan::Change>, String> {
        loan::stage(grid, row, month, text)
    }

    fn change_key(&self, change: &loan::Change) -> i64 {
        change.loan_id
    }

    fn change_month(&self, change: &loan::Change) -> String {
        change.month.clone()
    }

    fn effect(&self, change: &loan::Change) -> f64 {
        change.effect()
    }

    fn is_delete(&self, change: &loan::Change) -> bool {
        change.kind == loan::ChangeKind::Delete
    }

    fn staged_text(&self, change: &loan::Change) -> Option<String> {
        loan::staged_text(change)
    }

    fn staged_edit_text(&self, change: &loan::Change) -> String {
        loan::staged_edit_text(change)
    }

    fn describe(&self, row: Option<&loan::LoanRow>, change: &loan::Change, staged: &[&loan::Change]) -> String {
        loan::describe(row, change, staged)
    }

    fn apply(&self, journal: &Journal, changes: &[loan::Change]) -> Result<loan::BatchResult> {
        loan::apply_changes(journal, changes)
    }

    fn revert(&self, journal: &Journal, batch: &loan::BatchResult) -> Result<()> {
        loan::revert(journal, batch).map(|_| ())
    }

    fn summary(&self, batch: &loan::BatchResult) -> String {
        batch.summary()
    }

    fn applied(&self, batch: &loan::BatchResult) -> usize {
        batch.applied.len()
    }

    fn refused(&self, batch: &loan::BatchResult) -> Vec<(loan::Change, String)> {
        batch.refused.iter().map(|r| (r.change.clone(), r.reason.clone())).collect()
    }

    fn fill(&self, _: &Journal, grid: &loan::LoanGrid, rows: &[&loan::LoanRow], months: &[String]) -> Result<Fill<loan::Change>> {
        let plan = loan::plan_fill(grid, rows, months, None);
        Ok(Fill {
            rows: plan.loans(),
            skipped: plan.skipped.iter().map(|(_, why)| why.clone()).collect(),
            notes: plan.notes(),
            changes: plan.changes,
        })
    }

    fn blocked(&self, journal: &Journal, rows: &[&loan::LoanRow]) -> Result<HashMap<i64, String>> {
        loan::blocked_loans(journal, rows)
    }

    fn plan_delete(&self, journal: &Journal, _: &loan::LoanGrid, rows: &[&loan::LoanRow], months: &[String]) -> Result<(DeletePlan, Vec<loan::Change>)> {
        let plan = loan::plan_delete_months(journal, rows, months)?;
        let changes = plan.loan_changes.clone();
        Ok((plan, changes))
    }
}
