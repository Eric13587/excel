//! The `individuals` table: SACCO members, and who takes part in which grid.
//!
//! The root of every ownership chain: loans, ledger, every fund, enrolments
//! and suspensions hang off a member, and deleting one cascades to all of them
//! through the schema's triggers.
//!
//! Blank PF/ID numbers are never duplicates of each other, matching the
//! partial unique indexes on those columns.

use rusqlite::{Connection, OptionalExtension, params};

use super::model::{FromRow, MEMBER_ACTIVE, Member, collect};
use crate::dates;
use crate::error::Result;

/// The fields a listing needs, without a query per member.
#[derive(Debug, Clone, PartialEq)]
pub struct MemberSummary {
    pub id: i64,
    pub name: String,
    pub pf_no: String,
    pub employment_status: String,
    pub is_retired: bool,
    pub retired_date: Option<String>,
}

impl MemberSummary {
    pub fn is_active(&self) -> bool {
        !self.is_retired && self.employment_status == MEMBER_ACTIVE
    }
}

/// What a member is created or edited with.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct MemberDetails {
    pub name: String,
    pub phone: String,
    pub email: String,
    pub employment_status: String,
    pub pf_no: String,
    pub id_no: String,
}

pub struct Members<'a>(pub(crate) &'a Connection);

/// `(is_retired, retired_date)` for a status: anything but Active has left,
/// keeping an existing retirement date, otherwise stamping today.
pub fn retirement_for_status(status: &str, existing: Option<&str>) -> (bool, Option<String>) {
    if status.is_empty() || status == MEMBER_ACTIVE {
        (false, None)
    } else {
        (true, Some(existing.map(str::to_string).unwrap_or_else(dates::today)))
    }
}

impl Members<'_> {
    pub fn get(&self, id: i64) -> Result<Option<Member>> {
        Ok(self
            .0
            .query_row("SELECT * FROM individuals WHERE id=?", [id], Member::from_row)
            .optional()?)
    }

    pub fn all(&self) -> Result<Vec<Member>> {
        let mut stmt = self.0.prepare("SELECT * FROM individuals ORDER BY name COLLATE NOCASE, id")?;
        Ok(collect(&mut stmt, [])?)
    }

    pub fn summaries(&self) -> Result<Vec<MemberSummary>> {
        let mut stmt = self.0.prepare(
            "SELECT id, name, pf_no, employment_status, is_retired, retired_date \
             FROM individuals ORDER BY name COLLATE NOCASE, id",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok(MemberSummary {
                id: r.get(0)?,
                name: r.get(1)?,
                pf_no: r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                employment_status: r
                    .get::<_, Option<String>>(3)?
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| MEMBER_ACTIVE.into()),
                is_retired: r.get::<_, Option<i64>>(4)?.unwrap_or(0) != 0,
                retired_date: r.get(5)?,
            })
        })?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    pub fn name_of(&self, id: i64) -> Result<Option<String>> {
        Ok(self
            .0
            .query_row("SELECT name FROM individuals WHERE id=?", [id], |r| r.get(0))
            .optional()?)
    }

    /// Case-insensitive.
    pub fn name_exists(&self, name: &str, exclude_id: Option<i64>) -> Result<bool> {
        let count: i64 = self.0.query_row(
            "SELECT COUNT(*) FROM individuals WHERE LOWER(name) = LOWER(?) AND id <> ?",
            params![name, exclude_id.unwrap_or(-1)],
            |r| r.get(0),
        )?;
        Ok(count > 0)
    }

    /// The member already using this PF number, if any. Blank is never taken.
    pub fn owner_of_pf_no(&self, pf_no: &str, exclude_id: Option<i64>) -> Result<Option<String>> {
        self.owner_of("pf_no", pf_no, exclude_id)
    }

    pub fn owner_of_id_no(&self, id_no: &str, exclude_id: Option<i64>) -> Result<Option<String>> {
        self.owner_of("id_no", id_no, exclude_id)
    }

    fn owner_of(&self, column: &str, value: &str, exclude_id: Option<i64>) -> Result<Option<String>> {
        debug_assert!(column == "pf_no" || column == "id_no");
        let value = value.trim();
        if value.is_empty() {
            return Ok(None);
        }
        Ok(self
            .0
            .query_row(
                &format!("SELECT name FROM individuals WHERE {column}=? AND id<>? LIMIT 1"),
                params![value, exclude_id.unwrap_or(-1)],
                |r| r.get(0),
            )
            .optional()?)
    }

    pub fn add(&self, details: &MemberDetails) -> Result<i64> {
        let status = if details.employment_status.is_empty() {
            MEMBER_ACTIVE
        } else {
            details.employment_status.as_str()
        };
        let (is_retired, retired_date) = retirement_for_status(status, None);
        self.0.execute(
            "INSERT INTO individuals (name, phone, email, default_deduction, created_at, \
             employment_status, pf_no, id_no, is_retired, retired_date) \
             VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
            params![
                details.name.trim(), details.phone.trim(), details.email.trim(),
                dates::now_stamp(), status, details.pf_no.trim(), details.id_no.trim(),
                is_retired, retired_date
            ],
        )?;
        Ok(self.0.last_insert_rowid())
    }

    /// Edit a member. The status and the retirement flag move together — the
    /// schema's CHECK constraint insists, and a bare update of one is refused.
    pub fn update(&self, id: i64, details: &MemberDetails) -> Result<()> {
        let existing: Option<String> = self
            .0
            .query_row("SELECT retired_date FROM individuals WHERE id=?", [id], |r| r.get(0))
            .optional()?
            .flatten();
        let status = if details.employment_status.is_empty() {
            MEMBER_ACTIVE
        } else {
            details.employment_status.as_str()
        };
        let (is_retired, retired_date) = retirement_for_status(status, existing.as_deref());
        self.0.execute(
            "UPDATE individuals SET name=?, phone=?, email=?, employment_status=?, \
             is_retired=?, retired_date=?, pf_no=?, id_no=? WHERE id=?",
            params![
                details.name.trim(), details.phone.trim(), details.email.trim(), status,
                is_retired, retired_date, details.pf_no.trim(), details.id_no.trim(), id
            ],
        )?;
        Ok(())
    }

    /// Record that a member has left — or come back — on a given date.
    pub fn set_status(&self, id: i64, status: &str, effective: Option<&str>) -> Result<()> {
        let (is_retired, retired_date) = if status == MEMBER_ACTIVE {
            (false, None)
        } else {
            (true, Some(effective.map(str::to_string).unwrap_or_else(dates::today)))
        };
        self.0.execute(
            "UPDATE individuals SET employment_status=?, is_retired=?, retired_date=? WHERE id=?",
            params![status, is_retired, retired_date, id],
        )?;
        Ok(())
    }

    /// Cached sum of instalments over the member's active loans.
    pub fn set_default_deduction(&self, id: i64, amount: f64) -> Result<()> {
        self.0.execute("UPDATE individuals SET default_deduction=? WHERE id=?", params![amount, id])?;
        Ok(())
    }

    /// Delete a member. The schema's cascade triggers take every child row.
    pub fn delete(&self, id: i64) -> Result<()> {
        self.0.execute("DELETE FROM ledger WHERE individual_id=?", [id])?;
        self.0.execute("DELETE FROM individuals WHERE id=?", [id])?;
        Ok(())
    }

    /// What deleting a member would remove, per non-empty table.
    pub fn record_counts(&self, id: i64) -> Result<Vec<(&'static str, i64)>> {
        let mut out = Vec::new();
        for (table, label) in [
            ("ledger", "loan ledger entries"),
            ("loans", "loans"),
            ("savings", "savings transactions"),
            ("christmas_savings", "Christmas savings entries"),
            ("benevolent_ledger", "benevolent entries"),
            ("benevolent_accounts", "benevolent enrolments"),
            ("loan_suspensions", "suspension records"),
        ] {
            let n: i64 = self.0.query_row(
                &format!("SELECT COUNT(*) FROM {table} WHERE individual_id=?"),
                [id],
                |r| r.get(0),
            )?;
            if n > 0 {
                out.push((label, n));
            }
        }
        Ok(out)
    }

    // -- grid participation: stored as exceptions only ----------------- //

    /// `{member_id: note}` for everybody left out of one ledger's grid.
    pub fn excluded_from(&self, scope: &str) -> Result<std::collections::HashMap<i64, String>> {
        let mut stmt = self.0.prepare(
            "SELECT individual_id, note FROM grid_participation WHERE scope=? AND excluded=1",
        )?;
        let rows = stmt.query_map([scope], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default()))
        })?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    pub fn set_excluded(&self, id: i64, scope: &str, excluded: bool, note: &str) -> Result<()> {
        if excluded {
            self.0.execute(
                "INSERT INTO grid_participation (individual_id, scope, excluded, note, created_at) \
                 VALUES (?, ?, 1, ?, ?) \
                 ON CONFLICT(individual_id, scope) DO UPDATE SET excluded=1, note=excluded.note",
                params![id, scope, note, dates::now_stamp()],
            )?;
        } else {
            self.0.execute(
                "DELETE FROM grid_participation WHERE individual_id=? AND scope=?",
                params![id, scope],
            )?;
        }
        Ok(())
    }
}
