//! The `ledger` table: the loan subledger, and the journal's source of truth
//! for loans.
//!
//! Two column names are historical and load-bearing: `loan_id` holds the loan's
//! **reference** (`L-001`), meaningful only with `individual_id`; and
//! `is_edited` is an edit lock that legacy rows set to the edited amount
//! rather than 1 — any nonzero value means locked.

use rusqlite::{Connection, OptionalExtension, params, params_from_iter};

use super::model::{FromRow, LedgerRow, collect};
use crate::error::Result;

pub struct Ledger<'a>(pub(crate) &'a Connection);

/// What a new ledger row carries. Running balances are written by the
/// projection afterwards, but every writer supplies its best figures too.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct NewEntry {
    pub member_id: i64,
    pub date: String,
    pub event_type: String,
    pub loan_ref: String,
    pub added: f64,
    pub deducted: f64,
    pub balance: f64,
    pub notes: String,
    pub installment_amount: f64,
    pub interest_amount: f64,
    pub batch_id: Option<String>,
    pub principal_balance: f64,
    pub interest_balance: f64,
    pub principal_portion: f64,
    pub interest_portion: f64,
    pub previous_state: Option<String>,
}

/// A loan's running position as at its most recent row.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Position {
    pub balance: f64,
    pub principal_balance: f64,
    pub interest_balance: f64,
}

/// A row exactly as stored, every column, for putting it back after a delete.
#[derive(Debug, Clone, PartialEq)]
pub struct Snapshot {
    pub id: i64,
    columns: Vec<(String, rusqlite::types::Value)>,
}

impl Snapshot {
    pub fn get_f64(&self, column: &str) -> f64 {
        self.columns
            .iter()
            .find(|(c, _)| c == column)
            .and_then(|(_, v)| match v {
                rusqlite::types::Value::Real(x) => Some(*x),
                rusqlite::types::Value::Integer(i) => Some(*i as f64),
                _ => None,
            })
            .unwrap_or(0.0)
    }

    pub fn get_text(&self, column: &str) -> String {
        self.columns
            .iter()
            .find(|(c, _)| c == column)
            .and_then(|(_, v)| match v {
                rusqlite::types::Value::Text(s) => Some(s.clone()),
                _ => None,
            })
            .unwrap_or_default()
    }

    /// Equal in every column but those named.
    pub fn eq_ignoring(&self, other: &Snapshot, ignored: &[&str]) -> bool {
        let kept = |s: &Snapshot| -> Vec<(String, rusqlite::types::Value)> {
            s.columns.iter().filter(|(c, _)| !ignored.contains(&c.as_str())).cloned().collect()
        };
        self.id == other.id && kept(self) == kept(other)
    }
}

/// Read every column of one row of `table`.
pub(crate) fn snapshot_row(conn: &Connection, table: &str, id: i64) -> Result<Option<Snapshot>> {
    let mut stmt = conn.prepare(&format!("SELECT * FROM {table} WHERE id=?"))?;
    let names: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
    Ok(stmt
        .query_row([id], |r| {
            let mut columns = Vec::with_capacity(names.len());
            for (i, name) in names.iter().enumerate() {
                columns.push((name.clone(), r.get::<_, rusqlite::types::Value>(i)?));
            }
            Ok(Snapshot { id, columns })
        })
        .optional()?)
}

/// Write back the columns where `current` differs from `target`, bar those
/// in `skip`. Only differing columns are named, so an audit trigger watching
/// a column fires only when its value actually changes. Returns whether
/// anything was written.
pub(crate) fn update_row_to(conn: &Connection, table: &str, target: &Snapshot, current: &Snapshot, skip: &[&str]) -> Result<bool> {
    let changed: Vec<&(String, rusqlite::types::Value)> = target
        .columns
        .iter()
        .filter(|(c, v)| c != "id" && !skip.contains(&c.as_str()) && current.columns.iter().find(|(k, _)| k == c).is_none_or(|(_, now)| now != v))
        .collect();
    if changed.is_empty() {
        return Ok(false);
    }
    let set = changed.iter().map(|(c, _)| format!("{c}=?")).collect::<Vec<_>>().join(", ");
    let id = rusqlite::types::Value::Integer(target.id);
    let values = changed.iter().map(|(_, v)| v).chain(std::iter::once(&id));
    conn.execute(&format!("UPDATE {table} SET {set} WHERE id=?"), params_from_iter(values))?;
    Ok(true)
}

/// Make `table`'s rows among `current` match `target`: rows only in
/// `current` are deleted, rows only in `target` reinstated, and rows in both
/// corrected column by column. Rows already equal are not touched.
pub(crate) fn sync_rows(conn: &Connection, table: &str, target: &[Snapshot], current: &[Snapshot]) -> Result<()> {
    for row in current.iter().filter(|r| !target.iter().any(|t| t.id == r.id)) {
        conn.execute(&format!("DELETE FROM {table} WHERE id=?"), [row.id])?;
    }
    for row in target {
        match current.iter().find(|c| c.id == row.id) {
            Some(now) => {
                update_row_to(conn, table, row, now, &[])?;
            }
            None => reinstate_row(conn, table, row)?,
        }
    }
    Ok(())
}

/// Insert a [`Snapshot`] back into `table`, original id included.
pub(crate) fn reinstate_row(conn: &Connection, table: &str, snapshot: &Snapshot) -> Result<()> {
    let names: Vec<&str> = snapshot.columns.iter().map(|(c, _)| c.as_str()).collect();
    let placeholders = vec!["?"; names.len()].join(", ");
    conn.execute(
        &format!("INSERT INTO {table} ({}) VALUES ({placeholders})", names.join(", ")),
        params_from_iter(snapshot.columns.iter().map(|(_, v)| v)),
    )?;
    Ok(())
}

impl Ledger<'_> {
    pub fn get(&self, id: i64) -> Result<Option<LedgerRow>> {
        Ok(self.0.query_row("SELECT * FROM ledger WHERE id=?", [id], LedgerRow::from_row).optional()?)
    }

    /// A member's whole loan history, in statement order.
    pub fn for_member(&self, member_id: i64) -> Result<Vec<LedgerRow>> {
        let mut stmt =
            self.0.prepare("SELECT * FROM ledger WHERE individual_id=? ORDER BY date, id")?;
        Ok(collect(&mut stmt, [member_id])?)
    }

    /// A member's history between two dates (inclusive), either end optional.
    pub fn for_member_between(
        &self,
        member_id: i64,
        start: Option<&str>,
        end: Option<&str>,
    ) -> Result<Vec<LedgerRow>> {
        let mut stmt = self.0.prepare(
            "SELECT * FROM ledger WHERE individual_id=? AND (?2 IS NULL OR date >= ?2) \
             AND (?3 IS NULL OR date <= ?3) ORDER BY date, id",
        )?;
        Ok(collect(&mut stmt, params![member_id, start, end])?)
    }

    /// One loan's rows, in statement order.
    pub fn for_loan(&self, member_id: i64, reference: &str) -> Result<Vec<LedgerRow>> {
        let mut stmt = self
            .0
            .prepare("SELECT * FROM ledger WHERE individual_id=? AND loan_id=? ORDER BY date, id")?;
        Ok(collect(&mut stmt, params![member_id, reference])?)
    }

    /// The position carried on one loan's latest row. Per loan: a member's
    /// latest row usually belongs to a different loan.
    pub fn loan_position(&self, member_id: i64, reference: &str) -> Result<Position> {
        Ok(self
            .0
            .query_row(
                "SELECT balance, principal_balance, interest_balance FROM ledger \
                 WHERE individual_id=? AND loan_id=? ORDER BY date DESC, id DESC LIMIT 1",
                params![member_id, reference],
                |r| {
                    Ok(Position {
                        balance: r.get::<_, Option<f64>>(0)?.unwrap_or(0.0),
                        principal_balance: r.get::<_, Option<f64>>(1)?.unwrap_or(0.0),
                        interest_balance: r.get::<_, Option<f64>>(2)?.unwrap_or(0.0),
                    })
                },
            )
            .optional()?
            .unwrap_or_default())
    }

    pub fn count_events(
        &self,
        member_id: i64,
        reference: &str,
        kinds: &[&str],
        start: Option<&str>,
        end: Option<&str>,
    ) -> Result<i64> {
        let placeholders = vec!["?"; kinds.len()].join(",");
        let sql = format!(
            "SELECT COUNT(*) FROM ledger WHERE individual_id=? AND loan_id=? \
             AND event_type IN ({placeholders}) AND (? IS NULL OR date >= ?) \
             AND (? IS NULL OR date <= ?)"
        );
        let mut values: Vec<rusqlite::types::Value> = vec![member_id.into(), reference.to_string().into()];
        values.extend(kinds.iter().map(|k| rusqlite::types::Value::from(k.to_string())));
        for bound in [start, end] {
            let v: rusqlite::types::Value =
                bound.map(|s| s.to_string().into()).unwrap_or(rusqlite::types::Value::Null);
            values.push(v.clone());
            values.push(v);
        }
        Ok(self.0.query_row(&sql, params_from_iter(values), |r| r.get(0))?)
    }

    pub fn max_date(&self, member_id: i64, reference: &str, event_type: &str) -> Result<Option<String>> {
        Ok(self.0.query_row(
            "SELECT MAX(date) FROM ledger WHERE individual_id=? AND loan_id=? AND event_type=?",
            params![member_id, reference, event_type],
            |r| r.get(0),
        )?)
    }

    pub fn min_date(&self, member_id: i64, reference: &str, event_type: &str) -> Result<Option<String>> {
        Ok(self.0.query_row(
            "SELECT MIN(date) FROM ledger WHERE individual_id=? AND loan_id=? AND event_type=?",
            params![member_id, reference, event_type],
            |r| r.get(0),
        )?)
    }

    /// The latest date of any entry on a loan.
    pub fn last_date(&self, member_id: i64, reference: &str) -> Result<Option<String>> {
        Ok(self.0.query_row(
            "SELECT MAX(date) FROM ledger WHERE individual_id=? AND loan_id=?",
            params![member_id, reference],
            |r| r.get(0),
        )?)
    }

    /// Earliest activity across the ledger and savings for some members.
    pub fn earliest_date_for(&self, member_ids: &[i64]) -> Result<Option<String>> {
        if member_ids.is_empty() {
            return Ok(None);
        }
        let placeholders = vec!["?"; member_ids.len()].join(",");
        let sql = format!(
            "SELECT MIN(date) FROM (SELECT MIN(date) AS date FROM ledger WHERE individual_id IN \
             ({placeholders}) UNION ALL SELECT MIN(date) AS date FROM savings WHERE individual_id IN \
             ({placeholders}))"
        );
        let values: Vec<i64> = member_ids.iter().chain(member_ids).copied().collect();
        Ok(self.0.query_row(&sql, params_from_iter(values), |r| r.get(0))?)
    }

    // -------------------------------------------------------------- writes

    pub fn add(&self, entry: &NewEntry) -> Result<i64> {
        self.0.execute(
            "INSERT INTO ledger (individual_id, date, event_type, loan_id, added, deducted, balance, \
             notes, installment_amount, interest_amount, batch_id, principal_balance, \
             interest_balance, principal_portion, interest_portion, previous_state) \
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params![
                entry.member_id, entry.date, entry.event_type, entry.loan_ref, entry.added,
                entry.deducted, entry.balance, entry.notes, entry.installment_amount,
                entry.interest_amount, entry.batch_id, entry.principal_balance,
                entry.interest_balance, entry.principal_portion, entry.interest_portion,
                entry.previous_state
            ],
        )?;
        Ok(self.0.last_insert_rowid())
    }

    pub fn add_many(&self, entries: &[NewEntry]) -> Result<()> {
        for entry in entries {
            self.add(entry)?;
        }
        Ok(())
    }

    pub fn snapshot(&self, id: i64) -> Result<Option<Snapshot>> {
        snapshot_row(self.0, "ledger", id)
    }

    pub fn reinstate(&self, snapshot: &Snapshot) -> Result<()> {
        reinstate_row(self.0, "ledger", snapshot)
    }

    /// Edit a row's facts. `mark_edited` stores the amount as the edit lock,
    /// as LoanMaster 1.x did.
    #[allow(clippy::too_many_arguments)]
    pub fn update_core(
        &self,
        id: i64,
        date: &str,
        added: f64,
        deducted: f64,
        notes: &str,
        portions: Option<(f64, f64)>,
        mark_edited: bool,
        interest_amount: Option<f64>,
    ) -> Result<()> {
        self.0.execute(
            "UPDATE ledger SET date=?, added=?, deducted=?, notes=? WHERE id=?",
            params![date, added, deducted, notes, id],
        )?;
        if let Some((principal, interest)) = portions {
            self.set_portions(id, principal, interest)?;
        }
        if mark_edited {
            let lock = if deducted > 1.0 { deducted } else { 1.0 };
            self.0.execute("UPDATE ledger SET is_edited=? WHERE id=?", params![lock, id])?;
        }
        if let Some(amount) = interest_amount {
            self.0.execute("UPDATE ledger SET interest_amount=? WHERE id=?", params![amount, id])?;
        }
        Ok(())
    }

    pub fn set_portions(&self, id: i64, principal: f64, interest: f64) -> Result<()> {
        self.0.execute(
            "UPDATE ledger SET principal_portion=?, interest_portion=? WHERE id=?",
            params![principal, interest, id],
        )?;
        Ok(())
    }

    pub fn set_deducted(&self, id: i64, deducted: f64) -> Result<()> {
        self.0.execute("UPDATE ledger SET deducted=? WHERE id=?", params![deducted, id])?;
        Ok(())
    }

    pub fn set_running(&self, id: i64, balance: f64, principal: f64, interest: f64) -> Result<()> {
        self.0.execute(
            "UPDATE ledger SET balance=?, principal_balance=?, interest_balance=? WHERE id=?",
            params![balance, principal, interest, id],
        )?;
        Ok(())
    }

    pub fn set_derived(
        &self,
        id: i64,
        balance: f64,
        principal: f64,
        interest: f64,
        principal_portion: f64,
        interest_portion: f64,
    ) -> Result<()> {
        self.0.execute(
            "UPDATE ledger SET balance=?, principal_balance=?, interest_balance=?, \
             principal_portion=?, interest_portion=? WHERE id=?",
            params![balance, principal, interest, principal_portion, interest_portion, id],
        )?;
        Ok(())
    }

    pub fn delete(&self, id: i64) -> Result<()> {
        self.0.execute("DELETE FROM ledger WHERE id=?", [id])?;
        Ok(())
    }

    pub fn delete_events(&self, member_id: i64, reference: &str, kinds: &[&str]) -> Result<()> {
        let placeholders = vec!["?"; kinds.len()].join(",");
        let mut values: Vec<rusqlite::types::Value> = vec![member_id.into(), reference.to_string().into()];
        values.extend(kinds.iter().map(|k| rusqlite::types::Value::from(k.to_string())));
        self.0.execute(
            &format!(
                "DELETE FROM ledger WHERE individual_id=? AND loan_id=? AND event_type IN ({placeholders})"
            ),
            params_from_iter(values),
        )?;
        Ok(())
    }

    /// Clear (or set) the edit lock on a loan's interest rows after a date.
    pub fn unlock_interest_after(&self, member_id: i64, reference: &str, date: &str) -> Result<()> {
        self.0.execute(
            "UPDATE ledger SET is_edited=0 WHERE individual_id=? AND loan_id=? \
             AND event_type='Interest Earned' AND date > ?",
            params![member_id, reference, date],
        )?;
        Ok(())
    }
}
