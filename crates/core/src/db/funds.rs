//! The three member funds — savings, Christmas, benevolent — and benevolent
//! enrolments. One shape, one repository, the table chosen by [`Fund`].
//!
//! A balance is **summed** from the rows, never read off the latest one: the
//! stored `balance` column is a per-row running total for display, and it goes
//! stale the moment a back-dated row lands without a recalculation.

use std::collections::HashMap;

use rusqlite::{Connection, OptionalExtension, params};

use super::ledger::{Snapshot, reinstate_row, snapshot_row};
use super::model::{Enrolment, FromRow, Fund, FundRow, WITHDRAWAL, collect};
use crate::error::Result;
use crate::money::round2;

pub struct Funds<'a>(pub(crate) &'a Connection);

const SIGNED: &str = "CASE transaction_type WHEN 'Withdrawal' THEN -amount ELSE amount END";

impl Funds<'_> {
    pub fn balance(&self, fund: Fund, member_id: i64) -> Result<f64> {
        let total: f64 = self.0.query_row(
            &format!("SELECT COALESCE(SUM({SIGNED}), 0) FROM {} WHERE individual_id=?", fund.table()),
            [member_id],
            |r| r.get(0),
        )?;
        Ok(round2(total))
    }

    /// The whole fund across every member.
    pub fn total(&self, fund: Fund) -> Result<f64> {
        let total: f64 = self.0.query_row(
            &format!("SELECT COALESCE(SUM({SIGNED}), 0) FROM {}", fund.table()),
            [],
            |r| r.get(0),
        )?;
        Ok(round2(total))
    }

    /// Every member's balance at once.
    pub fn balances(&self, fund: Fund) -> Result<HashMap<i64, f64>> {
        let mut stmt = self.0.prepare(&format!(
            "SELECT individual_id, COALESCE(SUM({SIGNED}), 0) FROM {} GROUP BY individual_id",
            fund.table()
        ))?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, round2(r.get::<_, f64>(1)?))))?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    pub fn get(&self, fund: Fund, id: i64) -> Result<Option<FundRow>> {
        Ok(self
            .0
            .query_row(&format!("SELECT * FROM {} WHERE id=?", fund.table()), [id], FundRow::from_row)
            .optional()?)
    }

    /// A member's rows in statement order, optionally between two dates.
    pub fn rows(&self, fund: Fund, member_id: i64, start: Option<&str>, end: Option<&str>) -> Result<Vec<FundRow>> {
        let mut stmt = self.0.prepare(&format!(
            "SELECT * FROM {} WHERE individual_id=?1 AND (?2 IS NULL OR date >= ?2) \
             AND (?3 IS NULL OR date <= ?3) ORDER BY date, id",
            fund.table()
        ))?;
        Ok(collect(&mut stmt, params![member_id, start, end])?)
    }

    /// Every member's rows in a window, in one query.
    pub fn movements_in_range(&self, fund: Fund, start: &str, end: &str) -> Result<Vec<FundRow>> {
        let mut stmt = self.0.prepare(&format!(
            "SELECT * FROM {} WHERE date >= ? AND date <= ? ORDER BY individual_id, date, id",
            fund.table()
        ))?;
        Ok(collect(&mut stmt, params![start, end])?)
    }

    /// `{member: earliest date}` — when each member's account opened.
    pub fn first_movement_dates(&self, fund: Fund) -> Result<HashMap<i64, String>> {
        let mut stmt = self.0.prepare(&format!(
            "SELECT individual_id, MIN(date) FROM {} GROUP BY individual_id",
            fund.table()
        ))?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    /// The member's latest row by `(date, id)`, optionally of some types.
    pub fn last_dated_row(&self, fund: Fund, member_id: i64, kinds: &[&str]) -> Result<Option<FundRow>> {
        let filter = if kinds.is_empty() {
            String::new()
        } else {
            format!(
                " AND transaction_type IN ({})",
                kinds.iter().map(|k| format!("'{}'", k.replace('\'', "''"))).collect::<Vec<_>>().join(",")
            )
        };
        Ok(self
            .0
            .query_row(
                &format!(
                    "SELECT * FROM {} WHERE individual_id=?{filter} ORDER BY date DESC, id DESC LIMIT 1",
                    fund.table()
                ),
                [member_id],
                FundRow::from_row,
            )
            .optional()?)
    }

    /// `(id, date, type, amount)` in statement order — what a running
    /// balance is computed over.
    pub fn history(&self, fund: Fund, member_id: i64) -> Result<Vec<(i64, String, String, f64)>> {
        let mut stmt = self.0.prepare(&format!(
            "SELECT id, date, transaction_type, amount FROM {} WHERE individual_id=? \
             ORDER BY date ASC, id ASC",
            fund.table()
        ))?;
        let rows = stmt.query_map([member_id], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get::<_, Option<f64>>(3)?.unwrap_or(0.0)))
        })?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    /// One member's rows within a window — what a grid cell was read from.
    pub fn month_rows(&self, fund: Fund, member_id: i64, start: &str, end: &str) -> Result<Vec<FundRow>> {
        let mut stmt = self.0.prepare(&format!(
            "SELECT * FROM {} WHERE individual_id=? AND date >= ? AND date <= ? ORDER BY date, id",
            fund.table()
        ))?;
        Ok(collect(&mut stmt, params![member_id, start, end])?)
    }

    // -------------------------------------------------------------- writes

    #[allow(clippy::too_many_arguments)]
    pub fn add(
        &self,
        fund: Fund,
        member_id: i64,
        date: &str,
        transaction_type: &str,
        amount: f64,
        notes: &str,
        batch_id: Option<&str>,
    ) -> Result<i64> {
        self.0.execute(
            &format!(
                "INSERT INTO {} (individual_id, date, transaction_type, amount, balance, notes, batch_id) \
                 VALUES (?, ?, ?, ?, 0, ?, ?)",
                fund.table()
            ),
            params![member_id, date, transaction_type, amount, notes, batch_id],
        )?;
        Ok(self.0.last_insert_rowid())
    }

    /// Correct a row's date, amount and notes.
    pub fn update(&self, fund: Fund, id: i64, date: &str, amount: f64, notes: &str) -> Result<()> {
        self.0.execute(
            &format!("UPDATE {} SET date=?, amount=?, notes=? WHERE id=?", fund.table()),
            params![date, amount, notes, id],
        )?;
        Ok(())
    }

    /// Change what a row *is* — the type carries the sign of the movement.
    pub fn set_type(&self, fund: Fund, id: i64, transaction_type: &str) -> Result<()> {
        self.0.execute(
            &format!("UPDATE {} SET transaction_type=? WHERE id=?", fund.table()),
            params![transaction_type, id],
        )?;
        Ok(())
    }

    pub fn delete(&self, fund: Fund, id: i64) -> Result<()> {
        self.0.execute(&format!("DELETE FROM {} WHERE id=?", fund.table()), [id])?;
        Ok(())
    }

    pub fn delete_all_for_member(&self, fund: Fund, member_id: i64) -> Result<()> {
        self.0.execute(&format!("DELETE FROM {} WHERE individual_id=?", fund.table()), [member_id])?;
        Ok(())
    }

    pub fn snapshot(&self, fund: Fund, id: i64) -> Result<Option<Snapshot>> {
        snapshot_row(self.0, fund.table(), id)
    }

    /// Put a deleted row back, original id and all — rows sharing a date are
    /// ordered by id, so a new id could move it past its neighbours.
    pub fn reinstate(&self, fund: Fund, snapshot: &Snapshot) -> Result<()> {
        reinstate_row(self.0, fund.table(), snapshot)?;
        if snapshot.get_text("created_at").is_empty() {
            // The insert trigger stamps a missing creation time with "now"; a
            // restored row was not created now, and its time was never known.
            self.0.execute(
                &format!("UPDATE {} SET created_at=NULL WHERE id=?", fund.table()),
                [snapshot.id],
            )?;
        }
        Ok(())
    }

    /// Rewrite a member's stored running balance, in `(date, id)` order with
    /// withdrawals decreasing it, rounded at each step.
    pub fn recalculate(&self, fund: Fund, member_id: i64) -> Result<()> {
        let mut running = 0.0;
        let mut stmt = self.0.prepare(&format!(
            "UPDATE {} SET balance=? WHERE id=? AND balance IS NOT ?",
            fund.table()
        ))?;
        for (id, _date, kind, amount) in self.history(fund, member_id)? {
            running = round2(if kind == WITHDRAWAL { running - amount } else { running + amount });
            stmt.execute(params![running, id, running])?;
        }
        Ok(())
    }

    // ------------------------------------------------------------ enrolments

    pub fn enrolment(&self, member_id: i64) -> Result<Option<Enrolment>> {
        Ok(self
            .0
            .query_row(
                "SELECT * FROM benevolent_accounts WHERE individual_id=?",
                [member_id],
                Enrolment::from_row,
            )
            .optional()?)
    }

    pub fn enrolments(&self) -> Result<Vec<Enrolment>> {
        let mut stmt = self.0.prepare("SELECT * FROM benevolent_accounts")?;
        Ok(collect(&mut stmt, [])?)
    }

    pub fn upsert_enrolment(&self, member_id: i64, monthly: f64, start: &str, next_due: &str) -> Result<()> {
        if self.enrolment(member_id)?.is_some() {
            self.0.execute(
                "UPDATE benevolent_accounts SET monthly_amount=?, start_date=?, next_due_date=?, \
                 active=1 WHERE individual_id=?",
                params![monthly, start, next_due, member_id],
            )?;
        } else {
            self.0.execute(
                "INSERT INTO benevolent_accounts (individual_id, monthly_amount, start_date, \
                 next_due_date, active) VALUES (?, ?, ?, ?, 1)",
                params![member_id, monthly, start, next_due],
            )?;
        }
        Ok(())
    }

    pub fn set_enrolment_active(&self, member_id: i64, active: bool) -> Result<()> {
        self.0.execute(
            "UPDATE benevolent_accounts SET active=? WHERE individual_id=?",
            params![active, member_id],
        )?;
        Ok(())
    }

    pub fn set_next_due(&self, member_id: i64, next_due: &str) -> Result<()> {
        self.0.execute(
            "UPDATE benevolent_accounts SET next_due_date=? WHERE individual_id=?",
            params![next_due, member_id],
        )?;
        Ok(())
    }
}
