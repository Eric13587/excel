//! Normalising rows copied from another journal.
//!
//! An import reads a database this application did not write. Two policies,
//! chosen per field:
//!
//! * **Repair** where the intent is unambiguous — trim a name, map a known
//!   synonym, collapse a nonzero edit lock to 1 — and say what was changed.
//! * **Reject** where it is not. A ledger event the engine has no branch for
//!   cannot be replayed into a balance; a date like `01/02/2026` could be
//!   either month, and guessing wrong puts money in the wrong one. Rejected
//!   rows are counted and reported, never silently dropped.
//!
//! Nothing here writes.

use std::collections::HashMap;

use rusqlite::types::Value as Sql;

/// A source row: column name to value.
pub type SourceRow = HashMap<String, Sql>;

/// The outcome of normalising one row.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Normalised<T> {
    pub values: Option<T>,
    pub warnings: Vec<String>,
    /// Why the row must not be imported.
    pub reason: Option<String>,
}

impl<T> Normalised<T> {
    fn reject(mut self, reason: impl Into<String>) -> Self {
        self.reason = Some(reason.into());
        self.values = None;
        self
    }
}

pub fn text(row: &SourceRow, key: &str) -> String {
    match row.get(key) {
        Some(Sql::Text(t)) => t.clone(),
        Some(Sql::Integer(i)) => i.to_string(),
        Some(Sql::Real(r)) => r.to_string(),
        _ => String::new(),
    }
}

/// A number, `0` when absent, `None` when present but not a number.
fn amount(row: &SourceRow, key: &str) -> Option<f64> {
    match row.get(key) {
        None | Some(Sql::Null) => Some(0.0),
        Some(Sql::Integer(i)) => Some(*i as f64),
        Some(Sql::Real(r)) => Some(*r),
        Some(Sql::Text(t)) if t.trim().is_empty() => Some(0.0),
        Some(Sql::Text(t)) => t.trim().parse().ok(),
        Some(Sql::Blob(_)) => None,
    }
}

fn truthy(row: &SourceRow, key: &str) -> bool {
    match row.get(key) {
        Some(Sql::Integer(i)) => *i != 0,
        Some(Sql::Real(r)) => *r != 0.0,
        Some(Sql::Text(t)) => !t.is_empty() && t != "0",
        _ => false,
    }
}

/// An unambiguous `YYYY-MM-DD`, or `None`.
///
/// Accepts `YYYY-MM-DD`, `YYYY/MM/DD` and `YYYYMMDD`, with anything after
/// (a time). Refuses `01/02/2026`: 1 February or 2 January depends on who
/// wrote it.
pub fn normalise_date(value: &str) -> Option<String> {
    let text = value.trim();
    let b = text.as_bytes();
    let digits = |r: std::ops::Range<usize>| b.get(r.clone()).is_some_and(|s| s.iter().all(u8::is_ascii_digit));
    let (y, m, d) = if digits(0..4) && matches!(b.get(4), Some(b'-' | b'/')) && digits(5..7) && b.get(7) == b.get(4) && digits(8..10) {
        (&text[0..4], &text[5..7], &text[8..10])
    } else if b.len() == 8 && digits(0..8) {
        (&text[0..4], &text[4..6], &text[6..8])
    } else {
        return None;
    };
    let (month, day): (u32, u32) = (m.parse().ok()?, d.parse().ok()?);
    ((1..=12).contains(&month) && (1..=31).contains(&day)).then(|| format!("{y}-{m}-{d}"))
}

fn date_of(row: &SourceRow, key: &str) -> Option<String> {
    normalise_date(&text(row, key))
}

// ---------------------------------------------------------------- members

#[derive(Debug, Clone, Default, PartialEq)]
pub struct MemberValues {
    pub name: String,
    pub phone: String,
    pub email: String,
    pub pf_no: String,
    pub id_no: String,
    pub employment_status: String,
    pub default_deduction: f64,
    pub is_retired: bool,
    pub retired_date: Option<String>,
}

/// Employment wording seen in older journals, read onto the vocabulary.
pub fn status_synonym(raw: &str) -> Option<&'static str> {
    Some(match raw.to_lowercase().as_str() {
        "" | "active" | "employed" | "working" | "current" | "in service" | "serving" => "Active",
        "retired" | "retiree" | "pensioner" => "Retired",
        "resigned" | "quit" => "Resigned",
        "terminated" | "dismissed" | "sacked" => "Terminated",
        "deceased" | "dead" | "late" => "Deceased",
        "suspended" => "Suspended",
        _ => return None,
    })
}

/// Trim identity fields; reject a member with no usable name.
pub fn normalise_member(row: &SourceRow) -> Normalised<MemberValues> {
    let mut out = Normalised::default();
    let raw_name = text(row, "name");
    let name = raw_name.trim().to_string();
    if name.is_empty() {
        return out.reject("member has no name");
    }
    if name != raw_name {
        out.warnings.push(format!("name {raw_name:?} trimmed to {name:?}"));
    }
    let raw_status = text(row, "employment_status").trim().to_string();
    let flagged = truthy(row, "is_retired");
    let leaving = date_of(row, "retired_date");
    let mut employment_status = match status_synonym(&raw_status) {
        Some(mapped) => {
            // A blank status is the default, not a repair.
            if mapped != raw_status && !raw_status.is_empty() {
                out.warnings.push(format!("{name}: employment status {raw_status:?} read as {mapped:?}"));
            }
            mapped.to_string()
        }
        None => {
            let fallback = if flagged { "Retired" } else { "Active" };
            out.warnings.push(format!("{name}: unrecognised employment status {raw_status:?} — recorded as {fallback:?}"));
            fallback.to_string()
        }
    };
    if employment_status == "Active" && flagged && let Some(date) = &leaving {
        out.warnings.push(format!("{name}: marked retired on {date} — status read as \"Retired\""));
        employment_status = "Retired".into();
    }
    // The schema pairs the flag with the date, and a later change of status
    // sets the flag from it — so a member who has left always has both.
    let (is_retired, retired_date) = if employment_status == "Active" {
        if flagged {
            out.warnings.push(format!("{name}: retired flag dropped (no retirement date)"));
        }
        (false, None)
    } else {
        match leaving {
            Some(date) => (true, Some(date)),
            None => {
                let today = crate::dates::today();
                out.warnings.push(format!("{name}: {employment_status} with no leaving date — recorded as leaving on {today}"));
                (true, Some(today))
            }
        }
    };
    out.values = Some(MemberValues {
        phone: text(row, "phone").trim().to_string(),
        email: text(row, "email").trim().to_string(),
        pf_no: text(row, "pf_no").trim().to_string(),
        id_no: text(row, "id_no").trim().to_string(),
        default_deduction: amount(row, "default_deduction").unwrap_or(0.0).max(0.0),
        name,
        employment_status,
        is_retired,
        retired_date,
    });
    out
}

// ------------------------------------------------------------------ loans

#[derive(Debug, Clone, Default, PartialEq)]
pub struct LoanValues {
    pub reference: String,
    pub start_date: String,
    pub next_due_date: String,
    pub status: String,
    pub principal: f64,
    pub total_amount: f64,
    pub balance: f64,
    pub installment: f64,
    pub monthly_interest: f64,
    pub unearned_interest: f64,
    pub interest_balance: f64,
    pub is_suspended: bool,
    pub suspend_until: Option<String>,
    pub duration_months: Option<i64>,
    pub interest_rate: Option<f64>,
}

/// Normalise a loan. `fallback_start` is the loan's earliest ledger date.
pub fn normalise_loan(row: &SourceRow, fallback_start: Option<&str>) -> Normalised<LoanValues> {
    let mut out = Normalised::default();
    let reference = text(row, "ref").trim().to_string();
    if reference.is_empty() {
        return out.reject("loan has no reference");
    }
    let raw_start = text(row, "start_date");
    let Some(start) = normalise_date(&raw_start).or_else(|| fallback_start.and_then(normalise_date)) else {
        return out.reject(format!("loan {reference} has no usable start date"));
    };
    if start != raw_start {
        out.warnings.push(format!("loan {reference}: start date {raw_start:?} -> {start}"));
    }
    let raw_status = text(row, "status");
    let mut status = raw_status.trim().to_string();
    if let Some(first) = status.get(..1) {
        status = first.to_uppercase() + &status[1..].to_lowercase();
    }
    if status != "Active" && status != "Paid" {
        // An unrecognised status keeps the debt outstanding rather than
        // writing it off.
        out.warnings.push(format!("loan {reference}: status {raw_status:?} -> \"Active\""));
        status = "Active".into();
    }
    let mut figures = [0.0; 7];
    for (slot, key) in figures.iter_mut().zip(["principal", "total_amount", "balance", "installment", "monthly_interest", "unearned_interest", "interest_balance"]) {
        match amount(row, key) {
            Some(v) => *slot = v,
            None => return out.reject(format!("loan {reference}: {key} is not a number ({:?})", text(row, key))),
        }
    }
    if figures[0] < 0.0 || figures[3] < 0.0 {
        return out.reject(format!("loan {reference}: negative principal or installment"));
    }
    let suspend_until = date_of(row, "suspend_until");
    let duration_months = amount(row, "duration_months").filter(|d| *d >= 1.0 && d.fract() == 0.0).map(|d| d as i64);
    let interest_rate = match row.get("interest_rate") {
        None | Some(Sql::Null) => None,
        _ => amount(row, "interest_rate").filter(|r| *r >= 0.0),
    };
    out.values = Some(LoanValues {
        next_due_date: date_of(row, "next_due_date").unwrap_or_default(),
        status,
        principal: figures[0],
        total_amount: figures[1],
        balance: figures[2],
        installment: figures[3],
        monthly_interest: figures[4],
        unearned_interest: figures[5],
        interest_balance: figures[6],
        is_suspended: truthy(row, "is_suspended"),
        suspend_until,
        duration_months,
        interest_rate,
        reference,
        start_date: start,
    });
    out
}

// ----------------------------------------------------------------- ledger

/// The ledger's events.
pub const LEDGER_EVENT_TYPES: [&str; 6] =
    ["Loan Issued", "Loan Top-Up", "Interest Earned", "Repayment", "Loan Buyoff", "Loan Restructure"];

/// Legacy spellings seen in real journals.
fn event_synonym(raw: &str) -> Option<&'static str> {
    Some(match raw.to_lowercase().as_str() {
        "loan disbursed" | "disbursement" | "loan disbursement" => "Loan Issued",
        "top-up" | "top up" | "loan topup" => "Loan Top-Up",
        "interest" | "interest accrued" => "Interest Earned",
        "payment" => "Repayment",
        "buyoff" | "settlement" => "Loan Buyoff",
        "restructure" => "Loan Restructure",
        _ => return None,
    })
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct LedgerValues {
    pub date: String,
    pub event_type: String,
    pub loan_ref: String,
    pub added: f64,
    pub deducted: f64,
    pub balance: f64,
    pub installment_amount: f64,
    pub interest_amount: f64,
    pub principal_balance: f64,
    pub interest_balance: f64,
    pub principal_portion: f64,
    pub interest_portion: f64,
    pub is_edited: i64,
    pub notes: String,
    pub batch_id: Option<String>,
    pub previous_state: Option<String>,
}

/// Normalise one ledger row, or reject it with a reason.
pub fn normalise_ledger(row: &SourceRow) -> Normalised<LedgerValues> {
    let mut out = Normalised::default();
    let raw_date = text(row, "date");
    let Some(date) = normalise_date(&raw_date) else {
        return out.reject(format!("ledger row has an unreadable date ({raw_date:?})"));
    };
    if date != raw_date {
        out.warnings.push(format!("ledger date {raw_date:?} -> {date}"));
    }
    let raw_event = text(row, "event_type").trim().to_string();
    let Some(event) = LEDGER_EVENT_TYPES.iter().copied().find(|e| *e == raw_event).or_else(|| event_synonym(&raw_event)) else {
        return out.reject(format!("unknown ledger event type {raw_event:?}"));
    };
    if event != raw_event {
        out.warnings.push(format!("event type {raw_event:?} -> {event:?}"));
    }
    let loan_ref = text(row, "loan_id").trim().to_string();
    if loan_ref.is_empty() {
        return out.reject(format!("{event} row on {date} has no loan reference"));
    }
    let (Some(added), Some(deducted)) = (amount(row, "added"), amount(row, "deducted")) else {
        return out.reject(format!("{event} row on {date} has a non-numeric amount"));
    };
    if added < 0.0 || deducted < 0.0 {
        return out.reject(format!("{event} row on {date} has a negative amount"));
    }
    if added != 0.0 && deducted != 0.0 {
        // Every balance rule assumes a row moves debt one way.
        return out.reject(format!("{event} row on {date} both adds ({added}) and deducts ({deducted})"));
    }
    let raw_lock = amount(row, "is_edited").unwrap_or(1.0);
    if raw_lock != 0.0 && raw_lock != 1.0 {
        out.warnings.push(format!("edit lock {raw_lock} on {date} normalised to 1"));
    }
    let figure = |key: &str| amount(row, key).unwrap_or(0.0);
    let optional = |key: &str| match row.get(key) {
        Some(Sql::Text(t)) => Some(t.clone()),
        _ => None,
    };
    out.values = Some(LedgerValues {
        event_type: event.to_string(),
        loan_ref,
        added,
        deducted,
        balance: figure("balance"),
        installment_amount: figure("installment_amount"),
        interest_amount: figure("interest_amount"),
        principal_balance: figure("principal_balance"),
        interest_balance: figure("interest_balance"),
        principal_portion: figure("principal_portion"),
        interest_portion: figure("interest_portion"),
        is_edited: i64::from(raw_lock != 0.0),
        notes: text(row, "notes"),
        batch_id: optional("batch_id"),
        previous_state: optional("previous_state"),
        date,
    });
    out
}

// ------------------------------------------------------------------ funds

#[derive(Debug, Clone, Default, PartialEq)]
pub struct FundValues {
    pub date: String,
    pub transaction_type: String,
    pub amount: f64,
    pub notes: String,
    pub batch_id: Option<String>,
}

/// Normalise a fund movement against `vocabulary`, mapping `synonyms`.
pub fn normalise_fund_row(row: &SourceRow, vocabulary: &[&str], synonyms: &[(&str, &str)], label: &str) -> Normalised<FundValues> {
    let mut out = Normalised::default();
    let raw_date = text(row, "date");
    let Some(date) = normalise_date(&raw_date) else {
        return out.reject(format!("{label} row has an unreadable date ({raw_date:?})"));
    };
    let raw = text(row, "transaction_type").trim().to_string();
    let lower = raw.to_lowercase();
    let Some(kind) = vocabulary.iter().copied().find(|k| *k == raw).or_else(|| synonyms.iter().find(|(s, _)| *s == lower).map(|(_, k)| *k)) else {
        return out.reject(format!("unknown {label} transaction type {raw:?}"));
    };
    if kind != raw {
        out.warnings.push(format!("{label} type {raw:?} -> {kind:?}"));
    }
    let Some(value) = amount(row, "amount") else {
        return out.reject(format!("{label} row on {date} has a non-numeric amount"));
    };
    if value < 0.0 {
        // The type carries the direction; a negative amount would reverse it.
        return out.reject(format!("{label} row on {date} has a negative amount ({value})"));
    }
    out.values = Some(FundValues {
        date,
        transaction_type: kind.to_string(),
        amount: value,
        notes: text(row, "notes"),
        batch_id: match row.get("batch_id") {
            Some(Sql::Text(t)) => Some(t.clone()),
            _ => None,
        },
    });
    out
}

pub const SAVINGS_TYPES: [&str; 3] = ["Deposit", "Withdrawal", "Interest"];
pub const SAVINGS_SYNONYMS: [(&str, &str); 3] = [("contribution", "Deposit"), ("credit", "Deposit"), ("debit", "Withdrawal")];
pub const BENEVOLENT_TYPES: [&str; 4] = ["Contribution", "Withdrawal", "Deposit", "Interest"];
pub const BENEVOLENT_SYNONYMS: [(&str, &str); 1] = [("deposit", "Contribution")];

// ------------------------------------------------------------ suspensions

#[derive(Debug, Clone, Default, PartialEq)]
pub struct SuspensionValues {
    pub loan_ref: String,
    pub start_date: String,
    pub suspend_until: Option<String>,
    pub resumed_date: Option<String>,
    pub status: &'static str,
    pub created_at: Option<String>,
}

/// A suspension span: a readable start, and a status that agrees with
/// whether it was resumed.
pub fn normalise_suspension(row: &SourceRow) -> Normalised<SuspensionValues> {
    let mut out = Normalised::default();
    let raw = text(row, "start_date");
    let Some(start) = normalise_date(&raw) else {
        return out.reject(format!("suspension has an unreadable start date ({raw:?})"));
    };
    let resumed = date_of(row, "resumed_date");
    out.values = Some(SuspensionValues {
        loan_ref: text(row, "loan_ref").trim().to_string(),
        start_date: start,
        suspend_until: date_of(row, "suspend_until"),
        status: if resumed.is_some() { "resumed" } else { "active" },
        resumed_date: resumed,
        created_at: Some(text(row, "created_at")).filter(|c| !c.is_empty()),
    });
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(pairs: &[(&str, Sql)]) -> SourceRow {
        pairs.iter().map(|(k, v)| (k.to_string(), v.clone())).collect()
    }

    fn t(s: &str) -> Sql {
        Sql::Text(s.into())
    }

    #[test]
    fn only_unambiguous_dates_are_read() {
        assert_eq!(normalise_date("2026-01-05").as_deref(), Some("2026-01-05"));
        assert_eq!(normalise_date("2026/01/05 10:00").as_deref(), Some("2026-01-05"));
        assert_eq!(normalise_date("20260105").as_deref(), Some("2026-01-05"));
        assert_eq!(normalise_date("01/02/2026"), None);
        assert_eq!(normalise_date("2026-13-01"), None);
        assert_eq!(normalise_date("2026-01/05"), None);
        assert_eq!(normalise_date(""), None);
    }

    #[test]
    fn a_member_is_trimmed_and_their_status_read_onto_the_vocabulary() {
        let m = normalise_member(&row(&[("name", t("  Ann ")), ("employment_status", t("pensioner")), ("is_retired", Sql::Integer(1))]));
        let v = m.values.unwrap();
        assert_eq!((v.name.as_str(), v.employment_status.as_str()), ("Ann", "Retired"));
        // Left without a date: recorded as leaving today, so flag, date and
        // status agree.
        assert!(v.is_retired && v.retired_date.is_some());
        assert_eq!(m.warnings.len(), 3);
        let active = normalise_member(&row(&[("name", t("Ben")), ("is_retired", Sql::Integer(1))])).values.unwrap();
        assert_eq!((active.employment_status.as_str(), active.is_retired), ("Active", false));
        assert_eq!(normalise_member(&row(&[("name", t(" "))])).reason.as_deref(), Some("member has no name"));
    }

    #[test]
    fn a_ledger_row_that_moves_both_ways_or_has_no_known_event_is_refused() {
        let base = |event: &str, added: f64, deducted: f64| {
            row(&[("date", t("2026-01-01")), ("event_type", t(event)), ("loan_id", t("L-001")), ("added", Sql::Real(added)), ("deducted", Sql::Real(deducted))])
        };
        assert!(normalise_ledger(&base("Repayment", 5.0, 5.0)).reason.unwrap().contains("both adds"));
        assert!(normalise_ledger(&base("Mystery", 0.0, 5.0)).reason.unwrap().contains("unknown ledger event"));
        let mapped = normalise_ledger(&base("payment", 0.0, 5.0));
        assert_eq!(mapped.values.unwrap().event_type, "Repayment");
    }

    #[test]
    fn a_loan_keeps_its_terms_and_an_unknown_status_stays_outstanding() {
        let l = normalise_loan(
            &row(&[("ref", t("L-001")), ("start_date", t("")), ("status", t("closed")), ("principal", Sql::Real(1000.0)), ("interest_rate", Sql::Real(0.1)), ("duration_months", Sql::Integer(10))]),
            Some("2026-02-01"),
        );
        let v = l.values.unwrap();
        assert_eq!((v.start_date.as_str(), v.status.as_str(), v.interest_rate, v.duration_months), ("2026-02-01", "Active", Some(0.1), Some(10)));
    }

    #[test]
    fn a_suspension_status_follows_its_resume_date() {
        let s = normalise_suspension(&row(&[("start_date", t("2026-01-01")), ("resumed_date", t("2026-03-01")), ("status", t("active"))]));
        assert_eq!(s.values.unwrap().status, "resumed");
    }
}
