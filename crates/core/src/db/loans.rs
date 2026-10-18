//! The `loans` and `loan_suspensions` tables.
//!
//! A loan's `ref` (`L-001`) is generated per member, so it is **unique per
//! member only**: every read and write that takes a ref also takes the member.
//! A reference is never given out twice for one member, not even after the
//! loan that had it is deleted.
//!
//! `loans.is_suspended` / `suspend_until` hold only the *current* state; the
//! history of spans lives in `loan_suspensions`, which is what lets a past
//! suspension still be shown, and replayed, after the loan has resumed.

use rusqlite::{Connection, OptionalExtension, params};

use super::model::{FromRow, Loan, Suspension, collect};
use crate::dates;
use crate::error::Result;

pub struct Loans<'a>(pub(crate) &'a Connection);

/// What issuing a loan writes.
#[derive(Debug, Clone, PartialEq)]
pub struct NewLoan<'a> {
    pub member_id: i64,
    pub reference: &'a str,
    pub principal: f64,
    pub total_amount: f64,
    pub installment: f64,
    pub monthly_interest: f64,
    pub start_date: &'a str,
    pub next_due_date: &'a str,
    pub unearned_interest: f64,
    pub duration_months: i64,
    pub interest_rate: f64,
}

/// A loan's figures as they stood before an operation, for undo.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct LoanState {
    pub balance: f64,
    pub principal: f64,
    pub total_amount: f64,
    pub installment: f64,
    pub monthly_interest: f64,
    pub unearned_interest: f64,
    pub interest_balance: f64,
    pub next_due_date: String,
    pub status: String,
}

impl LoanState {
    pub fn of(loan: &Loan) -> LoanState {
        LoanState {
            balance: loan.balance,
            principal: loan.principal,
            total_amount: loan.total_amount,
            installment: loan.installment,
            monthly_interest: loan.monthly_interest,
            unearned_interest: loan.unearned_interest,
            interest_balance: loan.interest_balance,
            next_due_date: loan.due().to_string(),
            status: loan.status.clone(),
        }
    }

    /// Stored as the `previous_state` JSON of a ledger row, in the field names
    /// LoanMaster 1.x wrote, so either version can restore from either's rows.
    pub fn to_json(&self) -> String {
        serde_json::to_string(self).unwrap_or_default()
    }

    pub fn from_json(text: &str) -> Option<LoanState> {
        // Tolerant: 1.x rows sometimes miss a key, and a null stands for zero.
        let value: serde_json::Value = serde_json::from_str(text).ok()?;
        let num = |key: &str| value.get(key).and_then(|v| v.as_f64()).unwrap_or(0.0);
        let text = |key: &str| value.get(key).and_then(|v| v.as_str()).unwrap_or("").to_string();
        Some(LoanState {
            balance: num("balance"),
            principal: num("principal"),
            total_amount: num("total_amount"),
            installment: num("installment"),
            monthly_interest: num("monthly_interest"),
            unearned_interest: num("unearned_interest"),
            interest_balance: num("interest_balance"),
            next_due_date: text("next_due_date"),
            status: text("status"),
        })
    }
}

/// An overdue loan with the member behind it, for the arrears report.
#[derive(Debug, Clone, PartialEq)]
pub struct OverdueLoan {
    pub loan_id: i64,
    pub individual_id: i64,
    pub reference: String,
    pub balance: f64,
    pub installment: f64,
    pub interest_balance: f64,
    pub next_due_date: String,
    pub start_date: String,
    pub is_suspended: bool,
    pub suspend_until: Option<String>,
    pub name: String,
    pub phone: String,
    pub pf_no: String,
    pub employment_status: String,
    pub is_retired: bool,
}

impl Loans<'_> {
    pub fn get(&self, loan_id: i64) -> Result<Option<Loan>> {
        Ok(self.0.query_row("SELECT * FROM loans WHERE id=?", [loan_id], Loan::from_row).optional()?)
    }

    pub fn by_ref(&self, member_id: i64, reference: &str) -> Result<Option<Loan>> {
        Ok(self
            .0
            .query_row(
                "SELECT * FROM loans WHERE individual_id=? AND ref=?",
                params![member_id, reference],
                Loan::from_row,
            )
            .optional()?)
    }

    pub fn for_member(&self, member_id: i64) -> Result<Vec<Loan>> {
        let mut stmt = self.0.prepare("SELECT * FROM loans WHERE individual_id=? ORDER BY ref")?;
        Ok(collect(&mut stmt, [member_id])?)
    }

    pub fn active_for_member(&self, member_id: i64) -> Result<Vec<Loan>> {
        let mut stmt = self
            .0
            .prepare("SELECT * FROM loans WHERE individual_id=? AND status='Active' ORDER BY ref")?;
        Ok(collect(&mut stmt, [member_id])?)
    }

    pub fn all_active(&self) -> Result<Vec<Loan>> {
        let mut stmt = self.0.prepare("SELECT * FROM loans WHERE status='Active'")?;
        Ok(collect(&mut stmt, [])?)
    }

    pub fn has_active(&self, member_id: i64) -> Result<bool> {
        let n: i64 = self.0.query_row(
            "SELECT COUNT(*) FROM loans WHERE individual_id=? AND status='Active'",
            [member_id],
            |r| r.get(0),
        )?;
        Ok(n > 0)
    }

    pub fn overdue_count(&self, as_of: &str) -> Result<i64> {
        Ok(self.0.query_row(
            "SELECT COUNT(*) FROM loans WHERE status='Active' AND next_due_date < ?",
            [as_of],
            |r| r.get(0),
        )?)
    }

    /// Every active loan past its due date, oldest due first. Suspended loans
    /// are included and flagged: whether a hold counts is the reader's call.
    pub fn overdue(&self, as_of: &str) -> Result<Vec<OverdueLoan>> {
        let mut stmt = self.0.prepare(
            "SELECT l.id, l.individual_id, l.ref, l.balance, l.installment, l.interest_balance, \
                    l.next_due_date, l.start_date, l.is_suspended, l.suspend_until, \
                    i.name, i.phone, i.pf_no, i.employment_status, i.is_retired \
             FROM loans l JOIN individuals i ON i.id = l.individual_id \
             WHERE l.status='Active' AND l.next_due_date < ? \
             ORDER BY l.next_due_date, i.name, l.ref",
        )?;
        let rows = stmt.query_map([as_of], |r| {
            Ok(OverdueLoan {
                loan_id: r.get(0)?,
                individual_id: r.get(1)?,
                reference: r.get(2)?,
                balance: r.get::<_, Option<f64>>(3)?.unwrap_or(0.0),
                installment: r.get::<_, Option<f64>>(4)?.unwrap_or(0.0),
                interest_balance: r.get::<_, Option<f64>>(5)?.unwrap_or(0.0),
                next_due_date: r.get::<_, Option<String>>(6)?.unwrap_or_default(),
                start_date: r.get::<_, Option<String>>(7)?.unwrap_or_default(),
                is_suspended: r.get::<_, Option<i64>>(8)?.unwrap_or(0) != 0,
                suspend_until: r.get(9)?,
                name: r.get::<_, Option<String>>(10)?.unwrap_or_default(),
                phone: r.get::<_, Option<String>>(11)?.unwrap_or_default(),
                pf_no: r.get::<_, Option<String>>(12)?.unwrap_or_default(),
                employment_status: r.get::<_, Option<String>>(13)?.unwrap_or_default(),
                is_retired: r.get::<_, Option<i64>>(14)?.unwrap_or(0) != 0,
            })
        })?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    /// Every reference the member has held: their loans now, and the loans
    /// deleted since, as the audit trail recorded their ledger rows going.
    pub fn references_ever(&self, member_id: i64) -> Result<std::collections::BTreeSet<String>> {
        let mut stmt = self.0.prepare(
            "SELECT ref FROM loans WHERE individual_id = ?1 \
             UNION SELECT json_extract(before_image, '$.loan_id') FROM audit_log \
             WHERE entity = 'ledger' AND operation = 'DELETE' AND before_image IS NOT NULL \
             AND json_extract(before_image, '$.individual_id') = ?1",
        )?;
        let references = stmt.query_map([member_id], |r| r.get::<_, Option<String>>(0))?;
        let mut out = std::collections::BTreeSet::new();
        for reference in references {
            if let Some(reference) = reference?.filter(|r| !r.trim().is_empty()) {
                out.insert(reference);
            }
        }
        Ok(out)
    }

    /// The next reference for a member: one past the highest `L-nnn` they
    /// have ever held. A deleted loan's number is not given out again, so a
    /// reference in the activity log, a backup or a printed statement always
    /// means one loan, and undoing a deletion cannot collide with a newer loan.
    pub fn next_reference(&self, member_id: i64) -> Result<String> {
        let highest = self.references_ever(member_id)?.iter().filter_map(|r| r.split('-').nth(1)?.parse::<u32>().ok()).max().unwrap_or(0);
        Ok(format!("L-{:03}", highest + 1))
    }

    // -------------------------------------------------------------- writes

    pub fn add(&self, loan: &NewLoan<'_>) -> Result<i64> {
        self.0.execute(
            "INSERT INTO loans (individual_id, ref, principal, total_amount, balance, installment, \
             monthly_interest, start_date, next_due_date, unearned_interest, interest_balance, \
             status, duration_months, interest_rate) \
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'Active', ?, ?)",
            params![
                loan.member_id, loan.reference, loan.principal, loan.total_amount, loan.principal,
                loan.installment, loan.monthly_interest, loan.start_date, loan.next_due_date,
                loan.unearned_interest, loan.duration_months, loan.interest_rate
            ],
        )?;
        Ok(self.0.last_insert_rowid())
    }

    /// Move interest between charged and still-to-charge. For every loan,
    /// interest charged plus unearned interest is what it was written for.
    pub fn adjust_unearned(&self, loan_id: i64, delta: f64) -> Result<()> {
        self.0.execute(
            "UPDATE loans SET unearned_interest = ROUND(COALESCE(unearned_interest, 0) + ?, 2) \
             WHERE id=?",
            params![delta, loan_id],
        )?;
        Ok(())
    }

    /// Record a new duration (and optionally rate) after a top-up or restructure.
    pub fn set_terms(&self, loan_id: i64, duration_months: i64, interest_rate: Option<f64>) -> Result<()> {
        match interest_rate {
            Some(rate) => self.0.execute(
                "UPDATE loans SET duration_months=?, interest_rate=? WHERE id=?",
                params![duration_months, rate, loan_id],
            )?,
            None => self.0.execute(
                "UPDATE loans SET duration_months=? WHERE id=?",
                params![duration_months, loan_id],
            )?,
        };
        Ok(())
    }

    /// The state after a deduction. The optional columns are written only when
    /// supplied, so a caller that does not know them cannot zero them.
    pub fn set_status(
        &self,
        loan_id: i64,
        balance: f64,
        next_due_date: &str,
        status: &str,
        interest_balance: Option<f64>,
        unearned_interest: Option<f64>,
    ) -> Result<()> {
        self.0.execute(
            "UPDATE loans SET balance=?, next_due_date=?, status=?, \
             interest_balance=COALESCE(?, interest_balance), \
             unearned_interest=COALESCE(?, unearned_interest) WHERE id=?",
            params![balance, next_due_date, status, interest_balance, unearned_interest, loan_id],
        )?;
        Ok(())
    }

    /// New terms after a top-up or restructure.
    #[allow(clippy::too_many_arguments)]
    pub fn set_details(
        &self,
        loan_id: i64,
        total_amount: f64,
        balance: f64,
        installment: f64,
        monthly_interest: f64,
        next_due_date: &str,
        unearned_interest: Option<f64>,
        principal: Option<f64>,
        interest_balance: Option<f64>,
    ) -> Result<()> {
        self.0.execute(
            "UPDATE loans SET total_amount=?, balance=?, installment=?, monthly_interest=?, \
             next_due_date=?, unearned_interest=COALESCE(?, unearned_interest), \
             principal=COALESCE(?, principal), interest_balance=COALESCE(?, interest_balance) \
             WHERE id=?",
            params![
                total_amount, balance, installment, monthly_interest, next_due_date,
                unearned_interest, principal, interest_balance, loan_id
            ],
        )?;
        Ok(())
    }

    pub fn set_next_due(&self, loan_id: i64, next_due_date: &str) -> Result<()> {
        self.0.execute("UPDATE loans SET next_due_date=? WHERE id=?", params![next_due_date, loan_id])?;
        Ok(())
    }

    /// The final state after a catch-up run.
    pub fn set_catch_up_state(
        &self,
        loan_id: i64,
        balance: f64,
        interest_balance: f64,
        unearned_interest: f64,
        next_due_date: &str,
        status: &str,
    ) -> Result<()> {
        self.0.execute(
            "UPDATE loans SET balance=?, interest_balance=?, unearned_interest=?, next_due_date=?, \
             status=? WHERE id=?",
            params![balance, interest_balance, unearned_interest, next_due_date, status, loan_id],
        )?;
        Ok(())
    }

    /// Return a loan to its issued state before a schedule rebuild.
    pub fn reset_to_issued(&self, loan_id: i64, principal: f64, unearned: f64, next_due: &str) -> Result<()> {
        self.0.execute(
            "UPDATE loans SET balance=?, interest_balance=0, unearned_interest=?, next_due_date=?, \
             status='Active' WHERE id=?",
            params![principal, unearned, next_due, loan_id],
        )?;
        Ok(())
    }

    /// Put a loan's figures back exactly as a [`LoanState`] recorded them.
    pub fn restore_state(&self, loan_id: i64, state: &LoanState) -> Result<()> {
        self.0.execute(
            "UPDATE loans SET principal=?, total_amount=?, balance=?, installment=?, \
             interest_balance=?, unearned_interest=?, monthly_interest=?, next_due_date=?, status=? \
             WHERE id=?",
            params![
                state.principal, state.total_amount, state.balance, state.installment,
                state.interest_balance, state.unearned_interest, state.monthly_interest,
                state.next_due_date, state.status, loan_id
            ],
        )?;
        Ok(())
    }

    /// Projection's write: what replaying the loan's ledger says it owes.
    pub fn set_projection(
        &self,
        loan_id: i64,
        balance: f64,
        interest_balance: f64,
        status: &str,
        next_due_date: Option<&str>,
    ) -> Result<()> {
        self.0.execute(
            "UPDATE loans SET balance=?, interest_balance=?, status=?, next_due_date=? WHERE id=?",
            params![balance, interest_balance, status, next_due_date, loan_id],
        )?;
        Ok(())
    }

    pub fn set_installment(&self, loan_id: i64, installment: f64, monthly_interest: f64) -> Result<()> {
        self.0.execute(
            "UPDATE loans SET installment=?, monthly_interest=? WHERE id=?",
            params![installment, monthly_interest, loan_id],
        )?;
        Ok(())
    }

    /// Delete a loan and its ledger rows. The caller supplies the transaction.
    pub fn delete_with_ledger(&self, member_id: i64, reference: &str) -> Result<()> {
        self.0.execute(
            "DELETE FROM ledger WHERE individual_id=? AND loan_id=?",
            params![member_id, reference],
        )?;
        self.0.execute("DELETE FROM loans WHERE individual_id=? AND ref=?", params![member_id, reference])?;
        Ok(())
    }

    // --------------------------------------------------------- suspensions

    pub fn suspensions_for_member(&self, member_id: i64) -> Result<Vec<Suspension>> {
        let mut stmt = self.0.prepare(
            "SELECT * FROM loan_suspensions WHERE individual_id=? ORDER BY start_date, id",
        )?;
        Ok(collect(&mut stmt, [member_id])?)
    }

    /// Every recorded span for one loan, earliest first — by primary key,
    /// because a ref is unique per member only.
    pub fn suspensions_for_loan(&self, loan_id: i64) -> Result<Vec<Suspension>> {
        let mut stmt =
            self.0.prepare("SELECT * FROM loan_suspensions WHERE loan_id=? ORDER BY start_date, id")?;
        Ok(collect(&mut stmt, [loan_id])?)
    }

    pub fn suspension(&self, id: i64) -> Result<Option<Suspension>> {
        Ok(self
            .0
            .query_row("SELECT * FROM loan_suspensions WHERE id=?", [id], Suspension::from_row)
            .optional()?)
    }

    /// The one open span for a loan, if any.
    pub fn open_suspension(&self, loan_id: i64) -> Result<Option<Suspension>> {
        Ok(self
            .0
            .query_row(
                "SELECT * FROM loan_suspensions WHERE loan_id=? AND resumed_date IS NULL \
                 AND status='active' ORDER BY id DESC LIMIT 1",
                [loan_id],
                Suspension::from_row,
            )
            .optional()?)
    }

    pub fn set_suspended(&self, loan_id: i64, until: Option<&str>) -> Result<()> {
        self.0.execute(
            "UPDATE loans SET is_suspended=1, suspend_until=? WHERE id=?",
            params![until, loan_id],
        )?;
        Ok(())
    }

    pub fn clear_suspended(&self, loan_id: i64) -> Result<()> {
        self.0.execute("UPDATE loans SET is_suspended=0, suspend_until=NULL WHERE id=?", [loan_id])?;
        Ok(())
    }

    /// Move only the intended resume date; the original start is kept so the
    /// span still covers the whole skipped period.
    pub fn extend_suspension(&self, suspension_id: i64, until: Option<&str>) -> Result<()> {
        self.0.execute(
            "UPDATE loan_suspensions SET suspend_until=? WHERE id=?",
            params![until, suspension_id],
        )?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn add_suspension(
        &self,
        loan_id: i64,
        member_id: i64,
        reference: &str,
        start: &str,
        until: Option<&str>,
        resumed: Option<&str>,
        status: &str,
    ) -> Result<i64> {
        self.0.execute(
            "INSERT INTO loan_suspensions (loan_id, individual_id, loan_ref, start_date, \
             suspend_until, resumed_date, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            params![loan_id, member_id, reference, start, until, resumed, status, dates::now_stamp()],
        )?;
        Ok(self.0.last_insert_rowid())
    }

    pub fn close_open_suspension(&self, loan_id: i64, resumed: &str) -> Result<()> {
        self.0.execute(
            "UPDATE loan_suspensions SET resumed_date=?, status='resumed' \
             WHERE id = (SELECT id FROM loan_suspensions WHERE loan_id=? \
             AND resumed_date IS NULL ORDER BY id DESC LIMIT 1)",
            params![resumed, loan_id],
        )?;
        Ok(())
    }

    /// Remove a span entirely — "this never applied", unlike resuming.
    pub fn delete_suspension(&self, id: i64) -> Result<()> {
        self.0.execute("DELETE FROM loan_suspensions WHERE id=?", [id])?;
        Ok(())
    }
}
