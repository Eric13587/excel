//! What may happen: one list of rules, asked from every entry point.
//!
//! Leaving the SACCO does not erase what a member owes or is owed. A member
//! who has left must still be able to repay a loan and withdraw savings, and
//! the books must still be correctable. So the line is drawn at *creating new
//! obligations*:
//!
//! * **refused** — catch-up, a grid deduction, a new loan, a new scheduled
//!   contribution: all invent activity going forward;
//! * **allowed** — repayments, withdrawals, payouts, edits and corrections.
//!
//! A rule is a pure question of (journal, operation). The grid asks
//! [`check`] to grey out ineligible members before anything runs; services
//! call [`enforce`] at the point of writing. Same rules, same wording.

use rusqlite::OptionalExtension;

use crate::Journal;
use crate::error::{Error, Result};

pub const ACTIVE: &str = "Active";

/// Operations that create new obligations, and so require an active member.
pub const ACCRUING_OPERATIONS: [&str; 11] = [
    "loan.issue", "loan.top_up", "loan.catch_up", "loan.deduct", "savings.catch_up",
    "savings.deposit", "christmas.catch_up", "christmas.deposit", "benevolent.catch_up",
    "benevolent.contribute", "benevolent.enrol",
];

/// What is about to happen. `kind` is dotted (`savings.catch_up`) so a rule
/// can match a family.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Operation<'a> {
    pub kind: &'a str,
    pub member_id: Option<i64>,
    pub date: Option<&'a str>,
    pub amount: Option<f64>,
    pub loan_ref: Option<&'a str>,
}

impl<'a> Operation<'a> {
    pub fn new(kind: &'a str, member_id: i64) -> Operation<'a> {
        Operation { kind, member_id: Some(member_id), ..Operation::default() }
    }

    pub fn on(mut self, date: &'a str) -> Operation<'a> {
        self.date = Some(date);
        self
    }

    pub fn for_loan(mut self, reference: &'a str) -> Operation<'a> {
        self.loan_ref = Some(reference);
        self
    }

    pub fn family(&self) -> &str {
        self.kind.split('.').next().unwrap_or(self.kind)
    }

    fn accrues(&self) -> bool {
        ACCRUING_OPERATIONS.contains(&self.kind)
    }
}

/// Why an operation may not proceed, in words a user can act on.
#[derive(Debug, Clone, PartialEq)]
pub struct Refusal {
    pub rule: &'static str,
    pub reason: String,
}

fn reads(status: &str) -> String {
    match status {
        "Retired" => "has retired".into(),
        "Resigned" => "has resigned".into(),
        "Terminated" => "has been terminated".into(),
        "Deceased" => "is deceased".into(),
        "Suspended" => "is suspended".into(),
        other => format!("is marked {other}"),
    }
}

struct MemberStanding {
    name: String,
    status: String,
    is_retired: bool,
    retired_date: Option<String>,
}

fn standing(journal: &Journal, member_id: i64) -> Result<Option<MemberStanding>> {
    Ok(journal
        .conn()
        .query_row(
            "SELECT name, employment_status, is_retired, retired_date FROM individuals WHERE id=?",
            [member_id],
            |r| {
                Ok(MemberStanding {
                    name: r.get(0)?,
                    status: r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    is_retired: r.get::<_, Option<i64>>(2)?.unwrap_or(0) != 0,
                    retired_date: r.get::<_, Option<String>>(3)?.filter(|s| !s.is_empty()),
                })
            },
        )
        .optional()?)
}

/// Every refusal, without raising.
pub fn check(journal: &Journal, op: &Operation<'_>) -> Result<Vec<Refusal>> {
    let mut out = Vec::new();
    let Some(member_id) = op.member_id else { return Ok(out) };
    let Some(member) = standing(journal, member_id)? else {
        out.push(Refusal { rule: "member_must_exist", reason: format!("Member {member_id} no longer exists.") });
        return Ok(out);
    };
    if !op.accrues() {
        return Ok(out);
    }

    // New obligations require a member who is still in the SACCO.
    let status = if member.status.is_empty() {
        if member.is_retired { "Retired".to_string() } else { ACTIVE.to_string() }
    } else {
        member.status.clone()
    };
    if status != ACTIVE || member.is_retired {
        let since = member.retired_date.as_ref().map(|d| format!(" ({d})")).unwrap_or_default();
        out.push(Refusal {
            rule: "member_must_be_active",
            reason: format!(
                "{} {}{since}, so no new {} activity can be recorded. Repayments and \
                 withdrawals are still allowed.",
                member.name,
                reads(&status),
                op.family()
            ),
        });
    }

    // A contribution dated after someone left is almost always a mistake;
    // back-dated corrections covering their working period still go through.
    if let (Some(date), Some(left)) = (op.date, member.retired_date.as_deref())
        && date > left
    {
        out.push(Refusal {
            rule: "date_must_not_precede_departure",
            reason: format!("{date} is after {} left on {left}.", member.name),
        });
    }
    Ok(out)
}

pub fn allows(journal: &Journal, op: &Operation<'_>) -> Result<bool> {
    Ok(check(journal, op)?.is_empty())
}

/// Refuse with every reason if any rule objects.
pub fn enforce(journal: &Journal, op: &Operation<'_>) -> Result<()> {
    let refusals = check(journal, op)?;
    if refusals.is_empty() {
        Ok(())
    } else {
        Err(Error::NotAllowed(refusals.into_iter().map(|r| r.reason).collect()))
    }
}

/// Whether a member's standing alone refuses new obligations of a family, and
/// why — for greying out rows before anything is typed.
pub fn blocked_reason(journal: &Journal, kind: &str, member_id: i64) -> Result<Option<String>> {
    Ok(check(journal, &Operation::new(kind, member_id))?.into_iter().next().map(|r| r.reason))
}
