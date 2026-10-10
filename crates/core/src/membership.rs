//! Members: who belongs to the SACCO, and what joining, leaving and being
//! deleted do.
//!
//! Leaving is a status, not a deletion: a member who retires, resigns, is
//! terminated or dies keeps their history, may still repay and withdraw, and
//! may not take on anything new (see [`crate::policy`]). Leaving, however it
//! is recorded, pays out their own money: their savings and their Christmas
//! savings. Deleting is the irreversible act —
//! everything they own goes with them — so it is preceded by a backup the
//! caller can point to.

use std::path::PathBuf;
use std::sync::LazyLock;

use regex::Regex;

use crate::Journal;
use crate::dates;
use crate::db::members::MemberDetails;
use crate::db::model::{Fund, MEMBER_ACTIVE, MEMBER_STATUSES};
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

/// Change a member's details. A status that takes them out of the society
/// pays them out, as [`leave`] does.
pub fn update(journal: &Journal, id: i64, details: &MemberDetails) -> Result<Payout> {
    let details = normalise(details);
    if let Some(error) = validate(&details).into_iter().next() {
        return Err(Error::Invalid(error.message));
    }
    if let Some(clash) = number_clash(journal, &details, Some(id))? {
        return Err(Error::Invalid(clash));
    }
    journal.transaction(|| {
        let before = journal.members().get(id)?.ok_or(Error::MemberNotFound(id))?.employment_status;
        journal.members().update(id, &details)?;
        settle_status_change(journal, id, &before, &details.employment_status)
    })
}

/// The statuses that mean a member has left the society, and how the
/// withdrawals paying them out are noted for each. A suspended member has not
/// left: their money stays.
pub const LEAVING: [(&str, &str); 4] = [
    ("Retired", "Retirement withdrawal"),
    ("Resigned", "Resignation withdrawal"),
    ("Terminated", "Termination withdrawal"),
    ("Deceased", "Withdrawal on death"),
];

pub fn is_leaving(status: &str) -> bool {
    LEAVING.iter().any(|(s, _)| *s == status.trim())
}

/// The funds a leaving member is paid out of: their own money. The
/// benevolent fund is a pool, and its contributions stay in it.
pub const PAID_OUT: [Fund; 2] = [Fund::Savings, Fund::Christmas];

/// What a member was paid out on leaving.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Payout {
    pub savings: f64,
    pub christmas: f64,
}

impl Payout {
    pub fn total(&self) -> f64 {
        crate::money::total([self.savings, self.christmas])
    }

    /// "savings of 22,500 and Christmas savings of 3,000"; `None` when
    /// nothing was paid out.
    pub fn describe(&self) -> Option<String> {
        let mut parts = Vec::new();
        if self.savings > 0.0 {
            parts.push(format!("savings of {}", crate::money::shillings(self.savings)));
        }
        if self.christmas > 0.0 {
            parts.push(format!("Christmas savings of {}", crate::money::shillings(self.christmas)));
        }
        (!parts.is_empty()).then(|| parts.join(" and "))
    }
}

/// Pay a leaving member out of their savings and Christmas savings, in full,
/// on the day they left. Refused when either fund has entries dated later: a
/// withdrawal before them would run the balance below zero in between.
fn pay_out(journal: &Journal, id: i64, status: &str, date: &str) -> Result<Payout> {
    for fund in PAID_OUT {
        let latest = journal.funds().rows(fund, id, None, None)?.iter().map(|r| dates::date_part(&r.date).to_string()).max();
        if let Some(latest) = latest.filter(|latest| latest.as_str() > date) {
            let name = journal.members().name_of(id)?.unwrap_or_default();
            let what = if fund == Fund::Christmas { "Christmas savings" } else { "savings" };
            return Err(Error::invalid(format!(
                "{name} has {what} entries up to {latest}, after {date}. Record the leaving on or after {latest}, or correct those entries first."
            )));
        }
    }
    let note = LEAVING.iter().find(|(s, _)| *s == status.trim()).map(|(_, note)| *note).unwrap_or("Withdrawal on leaving");
    Ok(Payout {
        savings: funds::withdraw_all(journal, Fund::Savings, id, date, note)?,
        christmas: funds::withdraw_all(journal, Fund::Christmas, id, date, note)?,
    })
}

/// A member leaves the society — retires, resigns, is terminated or has
/// died: their savings and Christmas savings are paid out in full on the
/// leaving date, and they stop taking on new obligations.
pub fn leave(journal: &Journal, id: i64, status: &str, date: Option<&str>) -> Result<Payout> {
    if !is_leaving(status) {
        return Err(Error::invalid(format!("{status} is not a way of leaving the society.")));
    }
    let date = date.map(str::to_string).unwrap_or_else(dates::today);
    if dates::parse(&date).is_none() {
        return Err(Error::invalid(format!("{date} is not a date (use YYYY-MM-DD).")));
    }
    if date > dates::today() {
        return Err(Error::invalid("A member cannot be recorded as leaving on a date still to come."));
    }
    journal.transaction(|| {
        let paid = pay_out(journal, id, status, &date)?;
        journal.members().set_status(id, status, Some(&date))?;
        Ok(paid)
    })
}

/// After a member's status was changed from `before` by other means — the
/// form, a roster import: when the change took them out of the society, they
/// leave today and are paid out.
pub(crate) fn settle_status_change(journal: &Journal, id: i64, before: &str, status: &str) -> Result<Payout> {
    if !is_leaving(status) || is_leaving(before) {
        return Ok(Payout::default());
    }
    let today = dates::today();
    let paid = pay_out(journal, id, status, &today)?;
    journal.members().set_status(id, status, Some(&today))?;
    Ok(paid)
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

    /// A member with 5,000 in savings and 1,200 in Christmas savings.
    fn saver(journal: &Journal, name: &str) -> i64 {
        let id = add(journal, &details(name, "")).unwrap();
        funds::deposit(journal, Fund::Savings, id, 5000.0, "2026-01-01", "").unwrap();
        funds::deposit(journal, Fund::Christmas, id, 1200.0, "2026-01-01", "").unwrap();
        id
    }

    fn paid() -> Payout {
        Payout { savings: 5000.0, christmas: 1200.0 }
    }

    fn last_note(journal: &Journal, fund: Fund, id: i64) -> String {
        journal.funds().rows(fund, id, None, None).unwrap().last().unwrap().notes.clone()
    }

    #[test]
    fn retiring_withdraws_savings_and_reinstating_restores_membership() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = saver(&journal, "Ann");
        assert_eq!(leave(&journal, ann, "Retired", Some("2026-02-01")).unwrap(), paid());
        let member = journal.members().get(ann).unwrap().unwrap();
        assert!(!member.is_active() && member.retired_date.as_deref() == Some("2026-02-01"));
        assert_eq!(last_note(&journal, Fund::Savings, ann), "Retirement withdrawal");
        reinstate(&journal, ann).unwrap();
        assert!(journal.members().get(ann).unwrap().unwrap().is_active());
    }

    #[test]
    fn every_way_of_leaving_pays_out_savings_and_christmas_savings() {
        let journal = Journal::open_in_memory().unwrap();
        // March is not the Christmas unlock month: leaving pays out anyway.
        for (status, note) in LEAVING {
            let id = saver(&journal, status);
            assert_eq!(leave(&journal, id, status, Some("2026-03-01")).unwrap(), paid(), "{status}");
            for fund in PAID_OUT {
                assert_eq!(journal.funds().balance(fund, id).unwrap(), 0.0, "{status}");
                assert_eq!(last_note(&journal, fund, id), note);
            }
            assert_eq!(journal.members().get(id).unwrap().unwrap().employment_status, status);
        }
        // The benevolent pool keeps its contributions.
        let ben = saver(&journal, "Ben");
        funds::enrol(&journal, ben, 500.0, "2026-01-01").unwrap();
        funds::post(&journal, funds::Posting::new(Fund::Benevolent, ben, "2026-01-01", "Contribution", 500.0, "")).unwrap();
        leave(&journal, ben, "Resigned", Some("2026-03-01")).unwrap();
        assert_eq!(journal.funds().balance(Fund::Benevolent, ben).unwrap(), 500.0);
        assert_eq!(paid().describe().unwrap(), "savings of 5,000 and Christmas savings of 1,200");
        // A suspension is not leaving.
        let id = saver(&journal, "Sam");
        assert!(leave(&journal, id, "Suspended", None).is_err());
    }

    #[test]
    fn leaving_before_savings_entries_or_in_the_future_is_refused() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = saver(&journal, "Ann");
        let err = leave(&journal, ann, "Resigned", Some("2025-12-31")).unwrap_err().to_string();
        assert!(err.contains("2026-01-01"), "{err}");
        // A later Christmas entry holds it back too.
        funds::deposit(&journal, Fund::Christmas, ann, 300.0, "2026-02-01", "").unwrap();
        let err = leave(&journal, ann, "Resigned", Some("2026-01-15")).unwrap_err().to_string();
        assert!(err.contains("Christmas savings entries up to 2026-02-01"), "{err}");
        assert!(leave(&journal, ann, "Resigned", Some("2999-01-01")).is_err());
        let member = journal.members().get(ann).unwrap().unwrap();
        assert!(member.is_active() && journal.funds().balance(Fund::Savings, ann).unwrap() == 5000.0, "nothing changed");
    }

    #[test]
    fn changing_the_status_on_the_form_withdraws_the_savings_once() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = saver(&journal, "Ann");
        let mut form = MemberDetails { name: "Ann".into(), employment_status: "Suspended".into(), ..Default::default() };
        assert_eq!(update(&journal, ann, &form).unwrap(), Payout::default(), "a suspension keeps the money");
        form.employment_status = "Deceased".into();
        assert_eq!(update(&journal, ann, &form).unwrap(), paid());
        assert_eq!(last_note(&journal, Fund::Christmas, ann), "Withdrawal on death");
        let member = journal.members().get(ann).unwrap().unwrap();
        assert_eq!(member.retired_date, Some(dates::today()));
        // Already gone: a further change takes nothing more.
        form.employment_status = "Retired".into();
        assert_eq!(update(&journal, ann, &form).unwrap(), Payout::default());
    }
}
