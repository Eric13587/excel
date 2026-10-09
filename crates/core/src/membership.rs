//! Members: who belongs to the SACCO, and what joining, leaving and being
//! deleted do.
//!
//! Leaving is a status, not a deletion: a member who retires keeps their
//! history, may still repay and withdraw, and may not take on anything new
//! (see [`crate::policy`]). Retiring withdraws their savings. Deleting is the
//! irreversible act — everything they own goes with them — so it is preceded
//! by a backup the caller can point to.

use std::path::PathBuf;
use std::sync::LazyLock;

use regex::Regex;

use crate::Journal;
use crate::dates;
use crate::db::members::MemberDetails;
use crate::db::model::{MEMBER_ACTIVE, MEMBER_STATUSES};
use crate::error::{Error, Result};
use crate::funds;

static PHONE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[\d\s\-\+\(\)]{7,20}$").unwrap());
static EMAIL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[^@\s]+@[^@\s]+\.[^@\s]+$").unwrap());

/// A problem with one field of the member form.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FieldError {
    pub field: &'static str,
    pub message: String,
}

/// Collapse a name's whitespace, trim everything else.
pub fn normalise(details: &MemberDetails) -> MemberDetails {
    MemberDetails {
        name: details.name.split_whitespace().collect::<Vec<_>>().join(" "),
        phone: details.phone.trim().into(),
        email: details.email.trim().into(),
        employment_status: if details.employment_status.trim().is_empty() {
            MEMBER_ACTIVE.into()
        } else {
            details.employment_status.trim().into()
        },
        pf_no: details.pf_no.trim().into(),
        id_no: details.id_no.trim().into(),
    }
}

/// The form's own rules: a name, and well-formed phone and email if given.
pub fn validate(details: &MemberDetails) -> Vec<FieldError> {
    let mut errors = Vec::new();
    let name = details.name.trim();
    if name.is_empty() {
        errors.push(FieldError { field: "name", message: "Name is required".into() });
    } else if name.chars().count() > 100 {
        errors.push(FieldError { field: "name", message: "Name too long (max 100 characters)".into() });
    }
    let phone = details.phone.trim();
    if !phone.is_empty() && !PHONE.is_match(phone) {
        errors.push(FieldError {
            field: "phone",
            message: "Invalid phone format (use digits, spaces, +, -, parentheses)".into(),
        });
    }
    let email = details.email.trim();
    if !email.is_empty() && !EMAIL.is_match(email) {
        errors.push(FieldError { field: "email", message: "Invalid email format (e.g., name@example.com)".into() });
    }
    if !MEMBER_STATUSES.contains(&details.employment_status.trim()) && !details.employment_status.trim().is_empty() {
        errors.push(FieldError { field: "status", message: "Choose a membership status".into() });
    }
    errors
}

/// A PF or ID number another member already uses, as a refusal.
fn number_clash(journal: &Journal, details: &MemberDetails, exclude: Option<i64>) -> Result<Option<String>> {
    if let Some(owner) = journal.members().owner_of_pf_no(&details.pf_no, exclude)? {
        return Ok(Some(format!("PF number {} already belongs to {owner}.", details.pf_no)));
    }
    if let Some(owner) = journal.members().owner_of_id_no(&details.id_no, exclude)? {
        return Ok(Some(format!("ID number {} already belongs to {owner}.", details.id_no)));
    }
    Ok(None)
}

/// Add a member. A duplicate name is the caller's to confirm (people share
/// names); a PF or ID number already in use is refused.
pub fn add(journal: &Journal, details: &MemberDetails) -> Result<i64> {
    let details = normalise(details);
    if let Some(error) = validate(&details).into_iter().next() {
        return Err(Error::Invalid(error.message));
    }
    if let Some(clash) = number_clash(journal, &details, None)? {
        return Err(Error::Invalid(clash));
    }
    journal.transaction(|| journal.members().add(&details))
}

pub fn update(journal: &Journal, id: i64, details: &MemberDetails) -> Result<()> {
    let details = normalise(details);
    if let Some(error) = validate(&details).into_iter().next() {
        return Err(Error::Invalid(error.message));
    }
    if let Some(clash) = number_clash(journal, &details, Some(id))? {
        return Err(Error::Invalid(clash));
    }
    journal.transaction(|| journal.members().update(id, &details))
}

/// Retire a member: their savings are withdrawn in full and they stop taking
/// on new obligations. Returns the amount withdrawn.
pub fn retire(journal: &Journal, id: i64, date: Option<&str>) -> Result<f64> {
    let date = date.map(str::to_string).unwrap_or_else(dates::today);
    journal.transaction(|| {
        let withdrawn = funds::withdraw_all_savings(journal, id, &date, "Retirement withdrawal")?;
        journal.members().set_status(id, "Retired", Some(&date))?;
        Ok(withdrawn)
    })
}

/// Bring a member back into active membership.
pub fn reinstate(journal: &Journal, id: i64) -> Result<()> {
    journal.transaction(|| journal.members().set_status(id, MEMBER_ACTIVE, None))
}

pub fn has_outstanding_loans(journal: &Journal, id: i64) -> Result<bool> {
    journal.loans().has_active(id)
}

/// Delete a member and everything belonging to them, after a backup. If the
/// backup cannot be written nothing is deleted. Returns where the backup went
/// (none for an in-memory journal).
pub fn delete(journal: &Journal, id: i64) -> Result<Option<PathBuf>> {
    let backup = journal.snapshot(&format!("delete-member-{id}"))?;
    journal.transaction(|| journal.members().delete(id))?;
    Ok(backup)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn details(name: &str, pf: &str) -> MemberDetails {
        MemberDetails { name: name.into(), pf_no: pf.into(), ..Default::default() }
    }

    #[test]
    fn the_form_rules_hold() {
        assert!(validate(&details("Ann", "")).is_empty());
        assert_eq!(validate(&details("  ", ""))[0].field, "name");
        let bad = MemberDetails { name: "Ann".into(), phone: "abc".into(), email: "nope".into(), ..Default::default() };
        assert_eq!(validate(&bad).len(), 2);
        assert_eq!(normalise(&details("  Ann   Wanjiru ", "")).name, "Ann Wanjiru");
    }

    #[test]
    fn a_pf_number_in_use_is_refused_but_blank_ones_never_clash() {
        let journal = Journal::open_in_memory().unwrap();
        add(&journal, &details("Ann", "100")).unwrap();
        add(&journal, &details("Ben", "")).unwrap();
        add(&journal, &details("Cy", "")).unwrap();
        assert!(matches!(add(&journal, &details("Dee", "100")), Err(Error::Invalid(m)) if m.contains("Ann")));
    }

    #[test]
    fn retiring_withdraws_savings_and_reinstating_restores_membership() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = add(&journal, &details("Ann", "")).unwrap();
        funds::deposit(&journal, crate::db::model::Fund::Savings, ann, 5000.0, "2026-01-01", "").unwrap();
        assert_eq!(retire(&journal, ann, Some("2026-02-01")).unwrap(), 5000.0);
        let member = journal.members().get(ann).unwrap().unwrap();
        assert!(!member.is_active() && member.retired_date.as_deref() == Some("2026-02-01"));
        reinstate(&journal, ann).unwrap();
        assert!(journal.members().get(ann).unwrap().unwrap().is_active());
    }
}
