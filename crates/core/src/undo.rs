//! Undo and redo for the member ledger's destructive edits.
//!
//! A command records a **restore point** before it runs: the loan's row and
//! every ledger row on it, every column, original ids. Undo puts exactly that
//! back. LoanMaster 1.x restored only the deleted row and its pair, so undoing
//! the deletion of a top-up — which cascades to every later entry — brought
//! back the top-up and lost the rest.

use crate::Journal;
use crate::db::ledger::{Snapshot, reinstate_row, snapshot_row};
use crate::error::Result;
use crate::loans::edits::delete_entry;
use crate::loans::projection::project_member;
use crate::loans::service::recalculate_default_deduction;

/// A loan as it stood: its row and all its entries.
#[derive(Debug, Clone)]
pub struct RestorePoint {
    member_id: i64,
    reference: String,
    loan: Option<Snapshot>,
    entries: Vec<Snapshot>,
}

impl RestorePoint {
    pub fn capture(journal: &Journal, member_id: i64, reference: &str) -> Result<RestorePoint> {
        let loan = match journal.loans().by_ref(member_id, reference)? {
            Some(loan) => snapshot_row(journal.conn(), "loans", loan.id)?,
            None => None,
        };
        let mut entries = Vec::new();
        for row in journal.ledger().for_loan(member_id, reference)? {
            if let Some(snapshot) = snapshot_row(journal.conn(), "ledger", row.id)? {
                entries.push(snapshot);
            }
        }
        Ok(RestorePoint { member_id, reference: reference.to_string(), loan, entries })
    }

    /// Put the loan back exactly as captured.
    pub fn restore(&self, journal: &Journal) -> Result<()> {
        journal.transaction(|| {
            journal.loans().delete_with_ledger(self.member_id, &self.reference)?;
            if let Some(loan) = &self.loan {
                reinstate_row(journal.conn(), "loans", loan)?;
            }
            for entry in &self.entries {
                reinstate_row(journal.conn(), "ledger", entry)?;
            }
            project_member(journal, self.member_id)?;
            recalculate_default_deduction(journal, self.member_id)
        })
    }
}

/// One undoable action.
#[derive(Debug, Clone)]
pub enum Command {
    DeleteEntry { member_id: i64, row_id: i64, description: String, before: RestorePoint },
}

impl Command {
    pub fn description(&self) -> &str {
        match self {
            Command::DeleteEntry { description, .. } => description,
        }
    }

    fn redo(&self, journal: &Journal) -> Result<()> {
        match self {
            Command::DeleteEntry { member_id, row_id, .. } => delete_entry(journal, *member_id, *row_id),
        }
    }

    fn undo(&self, journal: &Journal) -> Result<()> {
        match self {
            Command::DeleteEntry { before, .. } => before.restore(journal),
        }
    }
}

/// The undo history. New work clears the redo side.
#[derive(Debug, Default)]
pub struct UndoStack {
    done: Vec<Command>,
    undone: Vec<Command>,
}

const MAX_DEPTH: usize = 50;

impl UndoStack {
    /// Delete a ledger entry, remembering how to put it back.
    pub fn delete_entry(&mut self, journal: &Journal, member_id: i64, row_id: i64) -> Result<bool> {
        let Some(row) = journal.ledger().get(row_id)? else { return Ok(false) };
        let before = RestorePoint::capture(journal, member_id, &row.loan_ref)?;
        delete_entry(journal, member_id, row_id)?;
        self.push(Command::DeleteEntry {
            member_id,
            row_id,
            description: format!("Delete {} ({})", row.event_type, crate::dates::date_part(&row.date)),
            before,
        });
        Ok(true)
    }

    fn push(&mut self, command: Command) {
        self.done.push(command);
        if self.done.len() > MAX_DEPTH {
            self.done.remove(0);
        }
        self.undone.clear();
    }

    pub fn can_undo(&self) -> bool {
        !self.done.is_empty()
    }

    pub fn can_redo(&self) -> bool {
        !self.undone.is_empty()
    }

    pub fn undo_description(&self) -> Option<&str> {
        self.done.last().map(Command::description)
    }

    pub fn redo_description(&self) -> Option<&str> {
        self.undone.last().map(Command::description)
    }

    /// Undo the latest action. Returns what was undone.
    pub fn undo(&mut self, journal: &Journal) -> Result<Option<String>> {
        let Some(command) = self.done.pop() else { return Ok(None) };
        if let Err(e) = command.undo(journal) {
            self.done.push(command);
            return Err(e);
        }
        let description = command.description().to_string();
        self.undone.push(command);
        Ok(Some(description))
    }

    pub fn redo(&mut self, journal: &Journal) -> Result<Option<String>> {
        let Some(command) = self.undone.pop() else { return Ok(None) };
        if let Err(e) = command.redo(journal) {
            self.undone.push(command);
            return Err(e);
        }
        let description = command.description().to_string();
        self.done.push(command);
        Ok(Some(description))
    }

    pub fn clear(&mut self) {
        self.done.clear();
        self.undone.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::members::MemberDetails;
    use crate::loans::service::{catch_up, issue, top_up};

    fn everything(journal: &Journal) -> (Vec<Vec<String>>, Vec<Vec<String>>) {
        let dump = |table: &str, skip: &[&str]| -> Vec<Vec<String>> {
            let mut stmt = journal.conn().prepare(&format!("SELECT * FROM {table} ORDER BY id")).unwrap();
            let names: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
            stmt.query_map([], |r| {
                Ok((0..names.len())
                    .filter(|i| !skip.contains(&names[*i].as_str()))
                    .map(|i| format!("{:?}", r.get::<_, rusqlite::types::Value>(i).unwrap()))
                    .collect())
            })
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
        };
        (dump("ledger", &[]), dump("loans", &["updated_at", "created_at"]))
    }

    #[test]
    fn undoing_a_cascading_delete_restores_everything_exactly() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = journal.members().add(&MemberDetails { name: "Ann".into(), ..Default::default() }).unwrap();
        issue(&journal, ann, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
        catch_up(&journal, ann, "L-001", Some("2026-03-01")).unwrap();
        top_up(&journal, ann, "L-001", 20000.0, 20, Some("2026-04-01")).unwrap();
        catch_up(&journal, ann, "L-001", Some("2026-06-01")).unwrap();
        let before = everything(&journal);

        let top_up_row = journal
            .ledger()
            .for_loan(ann, "L-001")
            .unwrap()
            .iter()
            .find(|r| r.event_type == crate::db::model::event::TOP_UP)
            .unwrap()
            .id;
        let mut stack = UndoStack::default();
        stack.delete_entry(&journal, ann, top_up_row).unwrap();
        assert_ne!(everything(&journal), before);
        assert_eq!(stack.undo(&journal).unwrap().as_deref(), Some("Delete Loan Top-Up (2026-04-01)"));
        assert_eq!(everything(&journal), before);
        stack.redo(&journal).unwrap();
        assert_ne!(everything(&journal), before);
    }
}
