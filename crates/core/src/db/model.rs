//! Rows as the application reads them.
//!
//! One struct per table, field for field, read by column name so a column
//! added to the schema never shifts another into the wrong field. Nullable
//! columns are `Option`; the rest carry the schema's own defaults.

use rusqlite::Row;
use serde::{Deserialize, Serialize};

/// Read a row into a value. Implemented per table below.
pub trait FromRow: Sized {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self>;
}

fn opt_f64(row: &Row<'_>, column: &str) -> rusqlite::Result<f64> {
    Ok(row.get::<_, Option<f64>>(column)?.unwrap_or(0.0))
}

fn opt_string(row: &Row<'_>, column: &str) -> rusqlite::Result<String> {
    Ok(row.get::<_, Option<String>>(column)?.unwrap_or_default())
}

/// The statuses a member can hold. Anything but `Active` has left.
pub const MEMBER_ACTIVE: &str = "Active";
pub const MEMBER_STATUSES: &[&str] = &["Active", "Retired", "Resigned", "Terminated", "Deceased", "Suspended"];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Member {
    pub id: i64,
    pub name: String,
    pub phone: String,
    pub email: String,
    pub created_at: String,
    pub default_deduction: f64,
    pub import_id: Option<i64>,
    pub is_retired: bool,
    pub retired_date: Option<String>,
    pub employment_status: String,
    pub pf_no: String,
    pub id_no: String,
}

impl Member {
    /// Still a member: not retired, and status `Active`.
    pub fn is_active(&self) -> bool {
        !self.is_retired && self.employment_status == MEMBER_ACTIVE
    }
}

impl FromRow for Member {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(Member {
            id: row.get("id")?,
            name: row.get("name")?,
            phone: opt_string(row, "phone")?,
            email: opt_string(row, "email")?,
            created_at: opt_string(row, "created_at")?,
            default_deduction: opt_f64(row, "default_deduction")?,
            import_id: row.get("import_id")?,
            is_retired: row.get::<_, Option<i64>>("is_retired")?.unwrap_or(0) != 0,
            retired_date: row.get("retired_date")?,
            employment_status: row
                .get::<_, Option<String>>("employment_status")?
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| MEMBER_ACTIVE.into()),
            pf_no: opt_string(row, "pf_no")?,
            id_no: opt_string(row, "id_no")?,
        })
    }
}

pub const LOAN_ACTIVE: &str = "Active";
pub const LOAN_PAID: &str = "Paid";

/// A loan. Segregated model: `balance` is outstanding **principal**,
/// `interest_balance` is interest charged but unpaid, `unearned_interest` is
/// interest the loan was written for and has not charged yet.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Loan {
    pub id: i64,
    pub individual_id: i64,
    pub reference: String,
    pub principal: f64,
    pub total_amount: f64,
    pub balance: f64,
    pub installment: f64,
    pub start_date: String,
    pub next_due_date: Option<String>,
    pub status: String,
    pub monthly_interest: f64,
    pub unearned_interest: f64,
    pub interest_balance: f64,
    pub import_id: Option<i64>,
    pub is_suspended: bool,
    pub suspend_until: Option<String>,
    pub created_at: Option<String>,
    pub duration_months: Option<i64>,
    pub interest_rate: Option<f64>,
}

impl Loan {
    pub fn is_active(&self) -> bool {
        self.status == LOAN_ACTIVE
    }

    /// The due date, or empty when none is recorded.
    pub fn due(&self) -> &str {
        self.next_due_date.as_deref().unwrap_or("")
    }
}

impl FromRow for Loan {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(Loan {
            id: row.get("id")?,
            individual_id: row.get("individual_id")?,
            reference: row.get("ref")?,
            principal: opt_f64(row, "principal")?,
            total_amount: opt_f64(row, "total_amount")?,
            balance: opt_f64(row, "balance")?,
            installment: opt_f64(row, "installment")?,
            start_date: opt_string(row, "start_date")?,
            next_due_date: row.get("next_due_date")?,
            status: opt_string(row, "status")?,
            monthly_interest: opt_f64(row, "monthly_interest")?,
            unearned_interest: opt_f64(row, "unearned_interest")?,
            interest_balance: opt_f64(row, "interest_balance")?,
            import_id: row.get("import_id")?,
            is_suspended: row.get::<_, Option<i64>>("is_suspended")?.unwrap_or(0) != 0,
            suspend_until: row.get("suspend_until")?,
            created_at: row.get("created_at")?,
            duration_months: row.get("duration_months")?,
            interest_rate: row.get("interest_rate")?,
        })
    }
}

/// Ledger event types. The loan subledger's vocabulary.
pub mod event {
    pub const LOAN_ISSUED: &str = "Loan Issued";
    pub const TOP_UP: &str = "Loan Top-Up";
    pub const RESTRUCTURE: &str = "Loan Restructure";
    pub const INTEREST: &str = "Interest Earned";
    pub const REPAYMENT: &str = "Repayment";
    pub const BUYOFF: &str = "Loan Buyoff";
}

/// One row of the loan subledger.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LedgerRow {
    pub id: i64,
    pub individual_id: i64,
    pub date: String,
    pub event_type: String,
    /// The loan's **reference** (`L-001`), not `loans.id` — historical, and
    /// meaningful only together with `individual_id`.
    pub loan_ref: String,
    pub added: f64,
    pub deducted: f64,
    pub balance: f64,
    pub notes: String,
    pub installment_amount: f64,
    pub batch_id: Option<String>,
    pub interest_amount: f64,
    pub principal_balance: f64,
    pub interest_balance: f64,
    pub principal_portion: f64,
    pub interest_portion: f64,
    /// An edit lock. Legacy rows store the edited *amount* here, not 1.
    pub is_edited: f64,
    pub previous_state: Option<String>,
    pub import_id: Option<i64>,
    pub gross_balance: f64,
}

impl FromRow for LedgerRow {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(LedgerRow {
            id: row.get("id")?,
            individual_id: row.get("individual_id")?,
            date: opt_string(row, "date")?,
            event_type: opt_string(row, "event_type")?,
            loan_ref: opt_string(row, "loan_id")?,
            added: opt_f64(row, "added")?,
            deducted: opt_f64(row, "deducted")?,
            balance: opt_f64(row, "balance")?,
            notes: opt_string(row, "notes")?,
            installment_amount: opt_f64(row, "installment_amount")?,
            batch_id: row.get("batch_id")?,
            interest_amount: opt_f64(row, "interest_amount")?,
            principal_balance: opt_f64(row, "principal_balance")?,
            interest_balance: opt_f64(row, "interest_balance")?,
            principal_portion: opt_f64(row, "principal_portion")?,
            interest_portion: opt_f64(row, "interest_portion")?,
            is_edited: row.get::<_, Option<f64>>("is_edited")?.unwrap_or(0.0),
            previous_state: row.get("previous_state")?,
            import_id: row.get("import_id")?,
            gross_balance: opt_f64(row, "gross_balance")?,
        })
    }
}

/// The three member funds share one shape.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum Fund {
    Savings,
    Christmas,
    Benevolent,
}

impl Fund {
    pub const ALL: [Fund; 3] = [Fund::Savings, Fund::Christmas, Fund::Benevolent];

    pub fn table(self) -> &'static str {
        match self {
            Fund::Savings => "savings",
            Fund::Christmas => "christmas_savings",
            Fund::Benevolent => "benevolent_ledger",
        }
    }

    pub fn from_table(table: &str) -> Option<Fund> {
        Fund::ALL.into_iter().find(|f| f.table() == table)
    }

    pub fn label(self) -> &'static str {
        match self {
            Fund::Savings => "Savings / Shares",
            Fund::Christmas => "Christmas",
            Fund::Benevolent => "Benevolent",
        }
    }

    /// The transaction type money going in is recorded as.
    pub fn credit(self) -> &'static str {
        match self {
            Fund::Benevolent => "Contribution",
            _ => "Deposit",
        }
    }

    /// The vocabulary each fund accepts.
    pub fn types(self) -> [&'static str; 3] {
        [self.credit(), WITHDRAWAL, "Interest"]
    }
}

/// The one sign rule: only a withdrawal takes money out.
pub const WITHDRAWAL: &str = "Withdrawal";

/// One fund movement.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FundRow {
    pub id: i64,
    pub individual_id: i64,
    pub date: String,
    pub transaction_type: String,
    pub amount: f64,
    pub balance: f64,
    pub notes: String,
    pub batch_id: Option<String>,
}

impl FundRow {
    pub fn is_withdrawal(&self) -> bool {
        self.transaction_type == WITHDRAWAL
    }

    /// The row's effect on the balance.
    pub fn signed(&self) -> f64 {
        if self.is_withdrawal() { -self.amount } else { self.amount }
    }
}

impl FromRow for FundRow {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(FundRow {
            id: row.get("id")?,
            individual_id: row.get("individual_id")?,
            date: opt_string(row, "date")?,
            transaction_type: opt_string(row, "transaction_type")?,
            amount: opt_f64(row, "amount")?,
            balance: opt_f64(row, "balance")?,
            notes: opt_string(row, "notes")?,
            batch_id: row.get("batch_id")?,
        })
    }
}

/// A recorded suspension span.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Suspension {
    pub id: i64,
    pub loan_id: Option<i64>,
    pub individual_id: i64,
    pub loan_ref: Option<String>,
    pub start_date: String,
    pub suspend_until: Option<String>,
    pub resumed_date: Option<String>,
    pub status: String,
    pub created_at: Option<String>,
}

impl Suspension {
    /// Where the span stops: the resume date if it was resumed, else the
    /// intended end, else open-ended. The half-open window `[start, end)` is
    /// what every reader of a suspension uses.
    pub fn end(&self) -> Option<&str> {
        self.resumed_date.as_deref().or(self.suspend_until.as_deref())
    }
}

impl FromRow for Suspension {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(Suspension {
            id: row.get("id")?,
            loan_id: row.get("loan_id")?,
            individual_id: row.get("individual_id")?,
            loan_ref: row.get("loan_ref")?,
            start_date: opt_string(row, "start_date")?,
            suspend_until: row.get("suspend_until")?,
            resumed_date: row.get("resumed_date")?,
            status: opt_string(row, "status")?,
            created_at: row.get("created_at").ok().flatten(),
        })
    }
}

/// A member's benevolent-fund enrolment.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Enrolment {
    pub id: i64,
    pub individual_id: i64,
    pub monthly_amount: f64,
    pub start_date: Option<String>,
    pub next_due_date: Option<String>,
    pub active: bool,
}

impl FromRow for Enrolment {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(Enrolment {
            id: row.get("id")?,
            individual_id: row.get("individual_id")?,
            monthly_amount: opt_f64(row, "monthly_amount")?,
            start_date: row.get("start_date")?,
            next_due_date: row.get("next_due_date")?,
            active: row.get::<_, Option<i64>>("active")?.unwrap_or(1) != 0,
        })
    }
}

/// One entry of the trigger-maintained audit trail.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AuditEntry {
    pub ts: String,
    pub operation: String,
    pub entity: String,
    pub entity_id: i64,
    pub summary: String,
    pub before_image: Option<String>,
}

impl FromRow for AuditEntry {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(AuditEntry {
            ts: opt_string(row, "ts")?,
            operation: opt_string(row, "operation")?,
            entity: opt_string(row, "entity")?,
            entity_id: row.get("entity_id")?,
            summary: opt_string(row, "summary")?,
            before_image: row.get("before_image")?,
        })
    }
}

/// One import run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ImportRun {
    pub id: i64,
    pub timestamp: String,
    pub source_file: String,
    pub details: String,
    pub item_count: i64,
}

impl FromRow for ImportRun {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(ImportRun {
            id: row.get("id")?,
            timestamp: opt_string(row, "timestamp")?,
            source_file: opt_string(row, "source_file")?,
            details: opt_string(row, "details")?,
            item_count: row.get::<_, Option<i64>>("item_count")?.unwrap_or(0),
        })
    }
}

/// Collect a query into rows.
pub fn collect<T: FromRow>(
    stmt: &mut rusqlite::Statement<'_>,
    params: impl rusqlite::Params,
) -> rusqlite::Result<Vec<T>> {
    stmt.query_map(params, T::from_row)?.collect()
}
