//! Savings suspensions: months a member's monthly savings contribution is
//! paused, as a loan's deductions are paused.
//!
//! A suspension covers whole months. It starts on the 1st of its first month
//! and runs up to the 1st of the month contributions resume in — the half-open
//! window `[start, end)` a loan's suspension uses — or, until further notice,
//! until it is resumed. Inside it a savings deposit is refused, and catch-up
//! and the Ledger Grid's Fill Forward step over its months. Withdrawals,
//! interest and corrections go on as before: the money is still the
//! member's. Statements say which months were paused.

use std::collections::HashMap;

use chrono::{Datelike, NaiveDate};

use crate::Journal;
use crate::dates;
use crate::db::model::SavingsSuspension;
use crate::error::{Error, Result};
use crate::policy;

/// The longest suspension for a set number of months, as for a loan.
pub const MAX_MONTHS: u32 = 60;

/// `(start, end)`: the 1st of the first month paused, and the 1st of the
/// month contributions resume in — `None` until further notice.
pub type Window = (String, Option<String>);

/// The 1st of the month `date` falls in.
pub fn month_start(date: NaiveDate) -> NaiveDate {
    date.with_day(1).unwrap_or(date)
}

fn first_of(text: &str) -> Result<NaiveDate> {
    dates::parse(text).map(month_start).ok_or_else(|| Error::invalid(format!("{text} is not a date (use YYYY-MM-DD).")))
}

pub fn window(span: &SavingsSuspension) -> Window {
    (dates::date_part(&span.start_date).to_string(), span.end().map(|e| dates::date_part(e).to_string()))
}

/// A member's windows, earliest first.
pub fn windows(journal: &Journal, member_id: i64) -> Result<Vec<Window>> {
    Ok(journal.funds().suspensions(member_id)?.iter().map(window).collect())
}

/// Every member's windows.
pub fn all_windows(journal: &Journal) -> Result<HashMap<i64, Vec<Window>>> {
    Ok(journal.funds().all_suspensions()?.into_iter().map(|(id, spans)| (id, spans.iter().map(window).collect())).collect())
}

/// The window `date` falls in, if any.
pub fn covering<'a>(date: &str, windows: &'a [Window]) -> Option<&'a Window> {
    let date = dates::date_part(date);
    windows.iter().find(|(start, end)| date >= start.as_str() && end.as_deref().is_none_or(|end| date < end))
}

fn month_words(date: NaiveDate) -> String {
    date.format("%B %Y").to_string()
}

/// A window in words, to follow "suspended": "for November 2026", "from
/// November 2026 to January 2027", "from November 2026 until further notice".
pub fn describe(window: &Window) -> String {
    let Some(start) = dates::parse(&window.0) else { return format!("from {}", window.0) };
    match window.1.as_deref().and_then(dates::parse) {
        Some(end) => {
            let last = dates::add_months(month_start(end), -1);
            if last <= start {
                format!("for {}", month_words(start))
            } else {
                format!("from {} to {}", month_words(start), month_words(last))
            }
        }
        None => format!("from {} until further notice", month_words(start)),
    }
}

/// The first month, from the one `date` falls in, that no suspension covers
/// — `None` when one until further notice does.
pub fn first_unpaused_month(date: NaiveDate, windows: &[Window]) -> Option<NaiveDate> {
    let mut month = month_start(date);
    while let Some((_, end)) = covering(&dates::format(month), windows) {
        month = month_start(dates::parse(end.as_deref()?)?);
    }
    Some(month)
}

/// The member's suspension in force in `month` (any day of it), if any.
pub fn in_force(journal: &Journal, member_id: i64, month: &str) -> Result<Option<SavingsSuspension>> {
    let date = dates::format(first_of(month)?);
    Ok(journal.funds().suspensions(member_id)?.into_iter().find(|s| covering(&date, &[window(s)]).is_some()))
}

/// Why a savings deposit dated `date` may not be made, or `None`.
pub fn deposit_refusal(journal: &Journal, member_id: i64, date: &str) -> Result<Option<String>> {
    let spans = windows(journal, member_id)?;
    let Some(window) = covering(date, &spans) else { return Ok(None) };
    let name = journal.members().name_of(member_id)?.unwrap_or_default();
    Ok(Some(format!(
        "Savings contributions for {name} are suspended {}. Resume them first, or date the deposit outside the \
         suspension.",
        describe(window)
    )))
}

/// What [`suspend`] did, so the Ledger Grid can take it back.
#[derive(Debug, Clone, PartialEq)]
pub enum Suspended {
    /// A new suspension.
    Added(i64),
    /// A suspension already covering the first month was made longer;
    /// `before` is the span as it was.
    Extended { before: SavingsSuspension },
}

/// Do two windows share a month?
pub fn overlaps(a: &Window, b: &Window) -> bool {
    let before_end = |date: &str, end: &Option<String>| end.as_deref().is_none_or(|e| date < e);
    before_end(&a.0, &b.1) && before_end(&b.0, &a.1)
}

/// Pause a member's contributions from the month `from` falls in, for
/// `months` months or, with `None`, until further notice.
///
/// Refused when the member has left, when savings deposits are already
/// recorded in the months it would pause, or when it would overlap another
/// suspension. The exception is a suspension already covering the first
/// month: asking for longer extends it, so a suspension made longer stays
/// one span.
pub fn suspend(journal: &Journal, member_id: i64, from: &str, months: Option<u32>) -> Result<Suspended> {
    if months.is_some_and(|m| !(1..=MAX_MONTHS).contains(&m)) {
        return Err(Error::invalid(format!("A suspension runs for 1 to {MAX_MONTHS} months.")));
    }
    if let Some(reason) = policy::blocked_reason(journal, "savings.deposit", member_id)? {
        return Err(Error::NotAllowed(vec![reason]));
    }
    let first = first_of(from)?;
    let asked: Window = (dates::format(first), months.map(|m| dates::format(dates::add_months(first, m as i32))));
    journal.transaction(|| {
        let mut extended: Option<SavingsSuspension> = None;
        for span in journal.funds().suspensions(member_id)? {
            let existing = window(&span);
            if !overlaps(&existing, &asked) {
                continue;
            }
            let longer = match (&existing.1, &asked.1) {
                (None, _) => false,
                (Some(_), None) => true,
                (Some(end), Some(wanted)) => wanted > end,
            };
            if existing.0 <= asked.0 && longer && extended.is_none() {
                extended = Some(span);
                continue;
            }
            return Err(Error::invalid(format!(
                "Savings contributions are already suspended {}. Change or remove that suspension instead.",
                describe(&existing)
            )));
        }
        let deposits = journal.funds().deposits_between(member_id, &asked.0, asked.1.as_deref())?;
        if !deposits.is_empty() {
            let mut months: Vec<String> = deposits.iter().map(|d| dates::month_label(&dates::month_key(&d.date))).collect();
            months.dedup();
            let shown = if months.len() > 4 { format!("{} and {} more", months[..3].join(", "), months.len() - 3) } else { months.join(", ") };
            return Err(Error::invalid(format!(
                "Savings deposits are already recorded in {shown}, which the suspension would cover. Delete them, or \
                 suspend from a later month."
            )));
        }
        match extended {
            Some(before) => {
                journal.conn().execute(
                    "UPDATE savings_suspensions SET suspend_until=?, resumed_date=?, status=? WHERE id=?",
                    rusqlite::params![
                        asked.1,
                        asked.1,
                        if asked.1.is_some() { "resumed" } else { "active" },
                        before.id
                    ],
                )?;
                Ok(Suspended::Extended { before })
            }
            None => Ok(Suspended::Added(journal.funds().add_suspension(
                member_id,
                &asked.0,
                asked.1.as_deref(),
                asked.1.as_deref(),
            )?)),
        }
    })
}

/// Take back what [`suspend`] did — the Ledger Grid's undo.
pub fn unsuspend(journal: &Journal, done: &Suspended) -> Result<()> {
    match done {
        Suspended::Added(id) => journal.funds().delete_suspension(*id),
        Suspended::Extended { before } => {
            journal.conn().execute(
                "UPDATE savings_suspensions SET suspend_until=?, resumed_date=?, status=? WHERE id=?",
                rusqlite::params![before.suspend_until, before.resumed_date, before.status, before.id],
            )?;
            Ok(())
        }
    }
}

/// End the suspension in force in the month `from` falls in: contributions
/// are taken again from that month. One that begins in that month has paused
/// nothing yet, and is removed. Returns the suspension as it was.
pub fn resume(journal: &Journal, member_id: i64, from: &str) -> Result<SavingsSuspension> {
    let first = first_of(from)?;
    let resumed = dates::format(first);
    let span = in_force(journal, member_id, &resumed)?
        .ok_or_else(|| Error::invalid(format!("Savings contributions are not suspended in {}.", month_words(first))))?;
    journal.transaction(|| {
        if dates::date_part(&span.start_date) >= resumed.as_str() {
            journal.funds().delete_suspension(span.id)
        } else {
            journal.funds().close_suspension(span.id, &resumed)
        }
    })?;
    Ok(span)
}

/// Remove a suspension entirely: it never applied, and its months are
/// ordinary months again, which Fill Forward and catch-up will offer deposits
/// for.
pub fn remove(journal: &Journal, id: i64) -> Result<Option<SavingsSuspension>> {
    let Some(span) = journal.funds().suspension(id)? else { return Ok(None) };
    journal.transaction(|| journal.funds().delete_suspension(id))?;
    Ok(Some(span))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::members::MemberDetails;
    use crate::db::model::Fund;
    use crate::funds;

    fn member(journal: &Journal, name: &str) -> i64 {
        journal.members().add(&MemberDetails { name: name.into(), ..Default::default() }).unwrap()
    }

    fn spans(journal: &Journal, id: i64) -> Vec<Window> {
        windows(journal, id).unwrap()
    }

    #[test]
    fn a_suspension_covers_whole_months_and_refuses_deposits_inside_them() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        assert_eq!(suspend(&journal, ann, "2026-11-17", Some(3)).unwrap(), Suspended::Added(1));
        assert_eq!(spans(&journal, ann), [("2026-11-01".to_string(), Some("2027-02-01".to_string()))]);
        assert_eq!(describe(&spans(&journal, ann)[0]), "from November 2026 to January 2027");

        let refused = funds::deposit(&journal, Fund::Savings, ann, 2500.0, "2026-12-28", "").unwrap_err();
        assert!(refused.to_string().contains("suspended from November 2026 to January 2027"), "{refused}");
        // Either side of it, and money going out, are untouched.
        funds::deposit(&journal, Fund::Savings, ann, 2500.0, "2026-10-31", "").unwrap();
        funds::deposit(&journal, Fund::Savings, ann, 2500.0, "2027-02-01", "").unwrap();
        funds::withdraw(&journal, Fund::Savings, ann, 500.0, "2026-12-05", "", false).unwrap();
        funds::post(&journal, funds::Posting::new(Fund::Savings, ann, "2026-12-31", "Interest", 40.0, "")).unwrap();
        // Christmas is a fund of its own.
        funds::deposit(&journal, Fund::Christmas, ann, 1000.0, "2026-12-01", "").unwrap();
    }

    #[test]
    fn asking_for_longer_extends_the_suspension_and_an_overlap_is_refused() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        suspend(&journal, ann, "2026-11-01", Some(2)).unwrap();
        let done = suspend(&journal, ann, "2026-12-01", Some(3)).unwrap();
        assert!(matches!(done, Suspended::Extended { .. }));
        assert_eq!(spans(&journal, ann), [("2026-11-01".to_string(), Some("2027-03-01".to_string()))]);
        unsuspend(&journal, &done).unwrap();
        assert_eq!(spans(&journal, ann), [("2026-11-01".to_string(), Some("2027-01-01".to_string()))]);

        // Inside it already, or starting before it: refused, with the span named.
        let inside = suspend(&journal, ann, "2026-11-01", Some(1)).unwrap_err().to_string();
        assert!(inside.contains("already suspended from November 2026 to December 2026"), "{inside}");
        assert!(suspend(&journal, ann, "2026-10-01", Some(3)).is_err());
        // Until further notice extends it too, and nothing extends that.
        suspend(&journal, ann, "2026-12-01", None).unwrap();
        assert_eq!(spans(&journal, ann), [("2026-11-01".to_string(), None)]);
        assert!(suspend(&journal, ann, "2027-06-01", Some(2)).is_err());
    }

    #[test]
    fn deposits_already_in_the_months_are_named_and_refuse_it() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        funds::deposit(&journal, Fund::Savings, ann, 2500.0, "2026-12-01", "").unwrap();
        let refused = suspend(&journal, ann, "2026-11-01", None).unwrap_err().to_string();
        assert!(refused.contains("Dec 2026"), "{refused}");
        assert!(spans(&journal, ann).is_empty());
        suspend(&journal, ann, "2027-01-01", None).unwrap();
    }

    #[test]
    fn the_next_month_open_steps_over_suspensions() {
        let windows: Vec<Window> = vec![
            ("2026-11-01".into(), Some("2027-01-01".into())),
            ("2027-01-01".into(), Some("2027-02-01".into())),
            ("2027-05-01".into(), None),
        ];
        let at = |d: &str| first_unpaused_month(dates::parse(d).unwrap(), &windows).map(dates::format);
        assert_eq!(at("2026-10-20").as_deref(), Some("2026-10-01"));
        assert_eq!(at("2026-12-05").as_deref(), Some("2027-02-01"));
        assert_eq!(at("2027-06-01"), None);
    }

    #[test]
    fn a_member_who_left_cannot_be_suspended() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        journal.members().set_status(ann, "Retired", Some("2026-02-01")).unwrap();
        assert!(matches!(suspend(&journal, ann, "2026-11-01", Some(2)), Err(Error::NotAllowed(_))));
    }

    #[test]
    fn resuming_ends_it_from_a_month_and_removing_forgets_it() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        // Resumed in the month it begins, it paused nothing: it goes.
        suspend(&journal, ann, "2026-11-01", Some(4)).unwrap();
        resume(&journal, ann, "2026-11-20").unwrap();
        assert!(spans(&journal, ann).is_empty());

        suspend(&journal, ann, "2026-11-01", None).unwrap();
        resume(&journal, ann, "2027-01-15").unwrap();
        assert_eq!(spans(&journal, ann), [("2026-11-01".to_string(), Some("2027-01-01".to_string()))]);
        funds::deposit(&journal, Fund::Savings, ann, 2500.0, "2027-01-01", "").unwrap();
        assert!(resume(&journal, ann, "2027-02-01").is_err(), "not suspended then");

        let id = journal.funds().suspensions(ann).unwrap()[0].id;
        remove(&journal, id).unwrap();
        funds::deposit(&journal, Fund::Savings, ann, 2500.0, "2026-12-01", "").unwrap();
    }
}
