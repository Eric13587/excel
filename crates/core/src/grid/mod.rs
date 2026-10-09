//! The Ledger Grid: every member (or loan) by every month, for bulk work.
//!
//! The screen lives in the application crate; this is what it stands on —
//! the cell language ([`ops`]), and for each ledger the rules for what its
//! cells accept and how a batch of edits is written and undone ([`fund`],
//! [`loan`]). Deleting whole months follows docs/grid/deleting-months.md.

pub mod fund;
pub mod loan;
pub mod ops;

/// What a cell looked like when it was read: `(row id, cents, date, kind)`,
/// sorted. Compared again at save time, so an edit made from another screen
/// while the grid sat open is refused rather than silently overwritten.
pub type Fingerprint = Vec<(i64, i64, String, String)>;

/// What deleting some months would do, before anything is staged — the
/// preview a column delete shows, and the changes it stages if confirmed.
#[derive(Debug, Clone, Default)]
pub struct DeletePlan {
    pub months: Vec<String>,
    pub fund_changes: Vec<fund::Change>,
    pub loan_changes: Vec<loan::Change>,
    /// `(row label, reason)` for rows that keep their transactions.
    pub refused: Vec<(String, String)>,
    /// `(label, (count, amount))`, in first-seen order.
    pub tallies: Vec<(String, (usize, f64))>,
    pub untouched: Vec<String>,
    pub consequences: Vec<String>,
}

impl DeletePlan {
    pub fn new(months: &[String]) -> DeletePlan {
        DeletePlan { months: months.to_vec(), ..DeletePlan::default() }
    }

    pub fn tally(&mut self, label: &str, amount: f64) {
        match self.tallies.iter_mut().find(|(l, _)| l == label) {
            Some((_, (count, total))) => {
                *count += 1;
                *total = crate::money::round2(*total + amount);
            }
            None => self.tallies.push((label.to_string(), (1, crate::money::round2(amount)))),
        }
    }

    pub fn tally_of(&self, label: &str) -> (usize, f64) {
        self.tallies.iter().find(|(l, _)| l == label).map(|(_, t)| *t).unwrap_or((0, 0.0))
    }

    pub fn refuse(&mut self, label: &str, reason: String) {
        self.refused.push((label.to_string(), reason));
    }

    pub fn change_count(&self) -> usize {
        self.fund_changes.len() + self.loan_changes.len()
    }

    /// How many rows (members or loans) the deletions touch.
    pub fn rows_affected(&self) -> usize {
        let members: std::collections::BTreeSet<i64> = self.fund_changes.iter().map(|c| c.member_id).collect();
        let loans: std::collections::BTreeSet<i64> = self.loan_changes.iter().map(|c| c.loan_id).collect();
        members.len() + loans.len()
    }

    pub fn is_empty(&self) -> bool {
        self.change_count() == 0 && self.refused.is_empty()
    }
}
