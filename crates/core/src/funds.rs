//! The three member funds: savings/shares, Christmas and benevolent.
//!
//! Every write goes through one pipeline — normalise, validate, check the
//! membership rules, insert, re-derive the running balances — so the funds
//! cannot disagree about what a row means. The sign lives in the transaction
//! type, never in the amount: only a `Withdrawal` takes money out.
//!
//! Savings and Christmas are pots with a monthly contribution that catch-up
//! fills forward from the last deposit; savings contributions can be
//! suspended for some months ([`suspensions`]). Christmas withdrawals are
//! locked outside the unlock month. Benevolent is a perpetual contribution on
//! a due-date schedule, like a loan's; payouts are welfare claims on the pool.

use std::collections::HashMap;

use chrono::Datelike;

use crate::Journal;
use crate::dates;
use crate::db::model::{Fund, FundRow, WITHDRAWAL};
use crate::error::{Error, Result};
use crate::money::round2;
use crate::policy::{self, Operation};

pub mod suspensions;

/// One fund transaction before it is written.
#[derive(Debug, Clone, PartialEq)]
pub struct Posting {
    pub fund: Fund,
    pub member_id: i64,
    pub date: String,
    pub kind: String,
    pub amount: f64,
    pub notes: String,
}

impl Posting {
    pub fn new(fund: Fund, member_id: i64, date: &str, kind: &str, amount: f64, notes: &str) -> Posting {
        Posting {
            fund,
            member_id,
            date: date.into(),
            kind: kind.into(),
            amount,
            notes: notes.into(),
        }
    }
}

fn policy_kind(fund: Fund) -> &'static str {
    match fund {
        Fund::Savings => "savings.deposit",
        Fund::Christmas => "christmas.deposit",
        Fund::Benevolent => "benevolent.contribute",
    }
}

/// Tidy the inputs without judging them: a known type in its canonical case,
/// the amount to two places, a date in `YYYY-MM-DD`, trimmed notes.
pub fn normalise(mut posting: Posting) -> Posting {
    if let Some(known) = posting.fund.types().into_iter().find(|t| t.eq_ignore_ascii_case(posting.kind.trim())) {
        posting.kind = known.to_string();
    }
    posting.amount = round2(posting.amount);
    let text = posting.date.trim();
    for format in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"] {
        let candidate = if format == "%Y-%m-%d" { text.get(..10).unwrap_or(text) } else { text };
        if let Ok(date) = chrono::NaiveDate::parse_from_str(candidate, format) {
            posting.date = dates::format(date);
            break;
        }
    }
    posting.notes = posting.notes.trim().to_string();
    posting
}

/// Everything wrong with a posting, as sentences. Empty means writable.
pub fn validate(posting: &Posting) -> Vec<String> {
    let mut problems = Vec::new();
    let known = posting.fund.types();
    if !known.contains(&posting.kind.as_str()) {
        problems.push(format!(
            "'{}' is not a {} transaction type ({}).",
            posting.kind,
            posting.fund.table(),
            known.join(", ")
        ));
    }
    if posting.amount.is_nan() || posting.amount <= 0.0 {
        problems.push("The amount must be greater than zero — use a Withdrawal to take money out.".into());
    }
    if dates::parse(&posting.date).is_none() || posting.date.len() != 10 {
        problems.push(format!("'{}' is not a date (use YYYY-MM-DD).", posting.date));
    }
    problems
}

/// Normalise, validate and apply the membership rules, writing nothing.
/// Only money going *in* is a new obligation; a member who has left may still
/// withdraw what they saved.
pub fn check(journal: &Journal, posting: Posting) -> Result<(Posting, Vec<String>)> {
    let tidy = normalise(posting);
    let mut problems = validate(&tidy);
    if !problems.is_empty() {
        return Ok((tidy, problems));
    }
    if tidy.kind != WITHDRAWAL {
        let op = Operation::new(policy_kind(tidy.fund), tidy.member_id).on(&tidy.date);
        problems.extend(policy::check(journal, &op)?.into_iter().map(|r| r.reason));
        // A suspension pauses the monthly contribution; interest still lands.
        if tidy.fund == Fund::Savings
            && tidy.kind == tidy.fund.credit()
            && let Some(reason) = suspensions::deposit_refusal(journal, tidy.member_id, &tidy.date)?
        {
            problems.push(reason);
        }
    } else if journal.members().get(tidy.member_id)?.is_none() {
        problems.push(format!("Member {} no longer exists.", tidy.member_id));
    }
    Ok((tidy, problems))
}

/// The one write path for all three funds. Returns the member's new balance.
pub fn post(journal: &Journal, posting: Posting) -> Result<f64> {
    post_with_batch(journal, posting, None)
}

pub fn post_with_batch(journal: &Journal, posting: Posting, batch_id: Option<&str>) -> Result<f64> {
    let (tidy, problems) = check(journal, posting)?;
    if !problems.is_empty() {
        return Err(Error::NotAllowed(problems));
    }
    journal.transaction(|| {
        journal.funds().add(tidy.fund, tidy.member_id, &tidy.date, &tidy.kind, tidy.amount, &tidy.notes, batch_id)?;
        journal.funds().recalculate(tidy.fund, tidy.member_id)?;
        journal.funds().balance(tidy.fund, tidy.member_id)
    })
}

// ---------------------------------------------------------------- reads

pub fn balance(journal: &Journal, fund: Fund, member_id: i64) -> Result<f64> {
    journal.funds().balance(fund, member_id)
}

pub fn rows(journal: &Journal, fund: Fund, member_id: i64) -> Result<Vec<FundRow>> {
    journal.funds().rows(fund, member_id, None, None)
}

/// The most common credit amount — on a tie, the smallest, as pandas'
/// `mode()` gave the history.
fn mode(amounts: &[f64]) -> Option<f64> {
    let mut counts: HashMap<u64, (f64, usize)> = HashMap::new();
    for &amount in amounts {
        counts.entry(round2(amount).to_bits()).or_insert((amount, 0)).1 += 1;
    }
    let best = counts.values().map(|(_, n)| *n).max()?;
    counts
        .values()
        .filter(|(_, n)| *n == best)
        .map(|(amount, _)| *amount)
        .min_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal))
}

/// What a member's monthly contribution appears to be: their most common
/// deposit, else the journal's default for the fund (benevolent: the enrolled
/// amount). Savings with no history at all is 0 — there is nothing to go on.
pub fn suggested_increment(journal: &Journal, fund: Fund, member_id: i64) -> Result<f64> {
    if fund == Fund::Benevolent {
        return Ok(journal.funds().enrolment(member_id)?.map(|e| e.monthly_amount).unwrap_or(0.0));
    }
    let rows = rows(journal, fund, member_id)?;
    if fund == Fund::Savings && rows.is_empty() {
        return Ok(0.0);
    }
    let deposits: Vec<f64> = rows.iter().filter(|r| r.transaction_type == fund.credit()).map(|r| r.amount).collect();
    match mode(&deposits) {
        Some(amount) => Ok(amount),
        None => Ok(match fund {
            Fund::Savings => journal.settings().default_savings_increment(),
            _ => journal.settings().default_christmas_increment(),
        }),
    }
}

// --------------------------------------------------------------- writes

pub fn deposit(journal: &Journal, fund: Fund, member_id: i64, amount: f64, date: &str, notes: &str) -> Result<f64> {
    post(journal, Posting::new(fund, member_id, date, fund.credit(), amount, notes))
}

/// Is a Christmas withdrawal dated `date` in the unlock month?
pub fn christmas_withdrawals_allowed(journal: &Journal, date: &str) -> bool {
    dates::parse(date).is_some_and(|d| d.month() == journal.settings().christmas_unlock_month())
}

/// Take money out. A Christmas withdrawal outside the unlock month is refused
/// unless `override_lock` — an administrator deliberately overriding it.
pub fn withdraw(
    journal: &Journal,
    fund: Fund,
    member_id: i64,
    amount: f64,
    date: &str,
    notes: &str,
    override_lock: bool,
) -> Result<f64> {
    if fund == Fund::Christmas && !override_lock && !christmas_withdrawals_allowed(journal, date) {
        return Err(Error::ChristmasLocked(
            dates::month_name(journal.settings().christmas_unlock_month()).to_string(),
        ));
    }
    post(journal, Posting::new(fund, member_id, date, WITHDRAWAL, amount, notes))
}

/// A benevolent claim paid out of the pool. Does not touch the contribution
/// schedule; contributions continue.
pub fn payout(journal: &Journal, member_id: i64, amount: f64, date: &str, notes: &str) -> Result<f64> {
    let notes = if notes.trim().is_empty() { "Benevolent payout" } else { notes };
    post(journal, Posting::new(Fund::Benevolent, member_id, date, WITHDRAWAL, amount, notes))
}

/// Withdraw a member's whole balance in one fund — what leaving does, in any
/// month: a leaving payout is not held to the Christmas unlock month. Returns
/// the amount withdrawn (0 when there was nothing).
pub fn withdraw_all(journal: &Journal, fund: Fund, member_id: i64, date: &str, notes: &str) -> Result<f64> {
    let balance = journal.funds().balance(fund, member_id)?;
    if balance <= 0.0 {
        return Ok(0.0);
    }
    post(journal, Posting::new(fund, member_id, date, WITHDRAWAL, balance, notes))?;
    Ok(balance)
}

/// Correct a row's date, amount and notes; balances follow. A savings
/// deposit is not moved into a month the member's savings are suspended in.
pub fn edit_row(journal: &Journal, fund: Fund, row_id: i64, date: &str, amount: f64, notes: &str) -> Result<()> {
    if dates::parse(date).is_none() {
        return Err(Error::invalid(format!("{date} is not a date (use YYYY-MM-DD).")));
    }
    if amount.is_nan() || amount <= 0.0 {
        return Err(Error::invalid("The amount must be greater than zero."));
    }
    journal.transaction(|| {
        let row = journal.funds().get(fund, row_id)?.ok_or_else(|| Error::invalid("That entry no longer exists."))?;
        if fund == Fund::Savings
            && row.transaction_type == fund.credit()
            && dates::month_key(date) != dates::month_key(&row.date)
            && let Some(reason) = suspensions::deposit_refusal(journal, row.individual_id, date)?
        {
            return Err(Error::NotAllowed(vec![reason]));
        }
        journal.funds().update(fund, row_id, dates::date_part(date), round2(amount), notes.trim())?;
        journal.funds().recalculate(fund, row.individual_id)
    })
}

pub fn delete_row(journal: &Journal, fund: Fund, row_id: i64) -> Result<()> {
    journal.transaction(|| {
        let Some(row) = journal.funds().get(fund, row_id)? else { return Ok(()) };
        journal.funds().delete(fund, row_id)?;
        journal.funds().recalculate(fund, row.individual_id)
    })
}

/// Remove every entry a member has in a fund. For benevolent the schedule
/// goes back to the enrolment's start date.
pub fn delete_all(journal: &Journal, fund: Fund, member_id: i64) -> Result<()> {
    journal.transaction(|| {
        journal.funds().delete_all_for_member(fund, member_id)?;
        if fund == Fund::Benevolent
            && let Some(start) = journal.funds().enrolment(member_id)?.and_then(|e| e.start_date)
        {
            journal.funds().set_next_due(member_id, &start)?;
        }
        Ok(())
    })
}

/// What a catch-up did.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct CaughtUp {
    pub added: usize,
    /// Months passed over because the member's savings were suspended.
    pub suspended: usize,
}

/// Add the monthly contribution from the month after the member's last
/// deposit through the target month (default: this month), on the 1st of
/// each, stepping over months the member's savings are suspended. Savings and
/// Christmas only.
pub fn catch_up_pot(journal: &Journal, fund: Fund, member_id: i64, monthly: Option<f64>, target: Option<&str>) -> Result<CaughtUp> {
    assert!(fund != Fund::Benevolent, "benevolent catches up on its schedule");
    let kind = if fund == Fund::Savings { "savings.catch_up" } else { "christmas.catch_up" };
    policy::enforce(journal, &Operation::new(kind, member_id))?;
    let rows = rows(journal, fund, member_id)?;
    let Some(last) = rows.iter().rev().find(|r| r.transaction_type == fund.credit()).or(rows.last()) else {
        return Ok(CaughtUp::default());
    };
    let Some(last_date) = dates::parse(last.date.split_whitespace().next().unwrap_or("")) else {
        return Ok(CaughtUp::default());
    };
    let monthly = match monthly.filter(|m| *m > 0.0) {
        Some(m) => m,
        None => suggested_increment(journal, fund, member_id)?,
    };
    if monthly <= 0.0 {
        return Ok(CaughtUp::default());
    }
    let paused = if fund == Fund::Savings { suspensions::windows(journal, member_id)? } else { Vec::new() };
    let target = match target {
        Some(t) => dates::parse(t).ok_or_else(|| Error::invalid(format!("{t} is not a date.")))?,
        None => chrono::Local::now().date_naive(),
    };
    let limit = dates::add_months(target, 1).with_day(1).unwrap_or(target);
    let mut next = dates::add_months(last_date, 1).with_day(1).unwrap_or(last_date);
    let notes = "Monthly contribution (Auto)";
    journal.transaction(|| {
        let mut done = CaughtUp::default();
        while next < limit {
            let date = dates::format(next);
            if suspensions::covering(&date, &paused).is_some() {
                done.suspended += 1;
            } else {
                post(journal, Posting::new(fund, member_id, &date, fund.credit(), monthly, notes))?;
                done.added += 1;
            }
            next = dates::add_months(next, 1);
        }
        Ok(done)
    })
}

// ------------------------------------------------------------ benevolent

pub fn is_enrolled(journal: &Journal, member_id: i64) -> Result<bool> {
    Ok(journal.funds().enrolment(member_id)?.is_some_and(|e| e.active && e.monthly_amount > 0.0))
}

/// Enrol (or re-configure) a member. The first contribution is due on `start`.
pub fn enrol(journal: &Journal, member_id: i64, monthly: f64, start: &str) -> Result<()> {
    if monthly < 0.0 || dates::parse(start).is_none() {
        return Err(Error::invalid("Enter a monthly amount and a start date."));
    }
    journal.transaction(|| journal.funds().upsert_enrolment(member_id, monthly, start, start))
}

/// Take one contribution at the next due date and advance the schedule.
pub fn contribute_once(journal: &Journal, member_id: i64) -> Result<usize> {
    policy::enforce(journal, &Operation::new("benevolent.contribute", member_id))?;
    let Some(account) = journal.funds().enrolment(member_id)?.filter(|e| e.active) else { return Ok(0) };
    let Some(due) = account.next_due_date.clone().filter(|d| !d.is_empty()) else { return Ok(0) };
    if account.monthly_amount <= 0.0 {
        return Ok(0);
    }
    journal.transaction(|| {
        post(journal, Posting::new(Fund::Benevolent, member_id, &due, "Contribution", account.monthly_amount, "Monthly contribution"))?;
        journal.funds().set_next_due(member_id, &dates::add_months_str(&due, 1).unwrap_or(due.clone()))?;
        Ok(1)
    })
}

/// A contribution for every due month from the schedule through `target`
/// (default today). Returns how many were added.
pub fn catch_up_benevolent(journal: &Journal, member_id: i64, target: Option<&str>) -> Result<usize> {
    policy::enforce(journal, &Operation::new("benevolent.catch_up", member_id))?;
    let Some(account) = journal.funds().enrolment(member_id)?.filter(|e| e.active) else { return Ok(0) };
    let Some(mut due) = account.next_due_date.clone().filter(|d| !d.is_empty()) else { return Ok(0) };
    if account.monthly_amount <= 0.0 {
        return Ok(0);
    }
    let limit = target.map(str::to_string).unwrap_or_else(dates::today);
    journal.transaction(|| {
        let mut count = 0;
        while due <= limit {
            post(
                journal,
                Posting::new(Fund::Benevolent, member_id, &due, "Contribution", account.monthly_amount, "Monthly contribution (Auto)"),
            )?;
            count += 1;
            match dates::add_months_str(&due, 1) {
                Some(next) => due = next,
                None => break, // an unreadable date: stop rather than loop
            }
        }
        if count > 0 {
            journal.funds().set_next_due(member_id, &due)?;
        }
        Ok(count)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::members::MemberDetails;

    fn member(journal: &Journal, name: &str) -> i64 {
        journal.members().add(&MemberDetails { name: name.into(), ..Default::default() }).unwrap()
    }

    #[test]
    fn an_interest_credit_adds_and_a_withdrawal_subtracts() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        deposit(&journal, Fund::Savings, ann, 1000.0, "2026-01-01", "").unwrap();
        post(&journal, Posting::new(Fund::Savings, ann, "2026-01-15", "interest", 50.0, "")).unwrap();
        let balance = withdraw(&journal, Fund::Savings, ann, 300.0, "2026-02-01", "", false).unwrap();
        assert_eq!(balance, 750.0);
    }

    #[test]
    fn a_zero_or_misdated_posting_is_refused_with_reasons() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        match post(&journal, Posting::new(Fund::Savings, ann, "garbage", "Deposit", 0.0, "")) {
            Err(Error::NotAllowed(reasons)) => assert_eq!(reasons.len(), 2),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn a_member_who_left_may_withdraw_but_not_deposit() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        deposit(&journal, Fund::Savings, ann, 1000.0, "2026-01-01", "").unwrap();
        journal.members().set_status(ann, "Retired", Some("2026-02-01")).unwrap();
        assert!(deposit(&journal, Fund::Savings, ann, 100.0, "2026-01-15", "").is_err());
        assert_eq!(withdraw_all(&journal, Fund::Savings, ann, "2026-02-01", "Retirement withdrawal").unwrap(), 1000.0);
    }

    #[test]
    fn christmas_withdrawals_wait_for_the_unlock_month() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        deposit(&journal, Fund::Christmas, ann, 1000.0, "2026-01-01", "").unwrap();
        assert!(matches!(withdraw(&journal, Fund::Christmas, ann, 100.0, "2026-06-01", "", false), Err(Error::ChristmasLocked(_))));
        assert!(withdraw(&journal, Fund::Christmas, ann, 100.0, "2026-06-01", "", true).is_ok());
        assert!(withdraw(&journal, Fund::Christmas, ann, 100.0, "2026-12-01", "", false).is_ok());
    }

    #[test]
    fn catch_up_fills_from_the_month_after_the_last_deposit() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        deposit(&journal, Fund::Savings, ann, 1000.0, "2025-01-01", "").unwrap();
        deposit(&journal, Fund::Savings, ann, 2000.0, "2025-02-01", "").unwrap();
        deposit(&journal, Fund::Savings, ann, 2000.0, "2025-03-01", "").unwrap();
        assert_eq!(catch_up_pot(&journal, Fund::Savings, ann, None, Some("2025-05-15")).unwrap().added, 2);
        let dates: Vec<String> = rows(&journal, Fund::Savings, ann).unwrap().iter().map(|r| r.date.clone()).collect();
        assert_eq!(dates.last().map(String::as_str), Some("2025-05-01"));
        assert_eq!(balance(&journal, Fund::Savings, ann).unwrap(), 9000.0);
    }

    #[test]
    fn catch_up_steps_over_suspended_months() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        deposit(&journal, Fund::Savings, ann, 2000.0, "2025-01-01", "").unwrap();
        suspensions::suspend(&journal, ann, "2025-03-01", Some(2)).unwrap();
        let done = catch_up_pot(&journal, Fund::Savings, ann, None, Some("2025-06-30")).unwrap();
        assert_eq!(done, CaughtUp { added: 3, suspended: 2 });
        let months: Vec<String> = rows(&journal, Fund::Savings, ann).unwrap().iter().map(|r| r.date.clone()).collect();
        assert_eq!(months, ["2025-01-01", "2025-02-01", "2025-05-01", "2025-06-01"]);
        // Christmas keeps its own months.
        deposit(&journal, Fund::Christmas, ann, 500.0, "2025-02-01", "").unwrap();
        assert_eq!(catch_up_pot(&journal, Fund::Christmas, ann, None, Some("2025-04-30")).unwrap().added, 2);
    }

    #[test]
    fn a_deposit_is_not_moved_into_a_suspended_month() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        deposit(&journal, Fund::Savings, ann, 2000.0, "2025-02-01", "").unwrap();
        suspensions::suspend(&journal, ann, "2025-03-01", Some(2)).unwrap();
        let id = rows(&journal, Fund::Savings, ann).unwrap()[0].id;
        assert!(matches!(edit_row(&journal, Fund::Savings, id, "2025-03-10", 2000.0, ""), Err(Error::NotAllowed(_))));
        edit_row(&journal, Fund::Savings, id, "2025-02-20", 2500.0, "corrected").unwrap();
        assert_eq!(balance(&journal, Fund::Savings, ann).unwrap(), 2500.0);
    }

    #[test]
    fn the_usual_amount_is_the_mode_and_ties_take_the_smaller() {
        assert_eq!(mode(&[500.0, 1000.0, 1000.0, 500.0]), Some(500.0));
        assert_eq!(mode(&[2500.0, 2500.0, 3000.0]), Some(2500.0));
        assert_eq!(mode(&[]), None);
    }

    #[test]
    fn benevolent_runs_on_its_schedule() {
        let journal = Journal::open_in_memory().unwrap();
        let ann = member(&journal, "Ann");
        enrol(&journal, ann, 300.0, "2026-01-15").unwrap();
        assert_eq!(catch_up_benevolent(&journal, ann, Some("2026-03-20")).unwrap(), 3);
        assert_eq!(journal.funds().enrolment(ann).unwrap().unwrap().next_due_date.as_deref(), Some("2026-04-15"));
        contribute_once(&journal, ann).unwrap();
        payout(&journal, ann, 5000.0, "2026-05-01", "").unwrap();
        assert_eq!(balance(&journal, Fund::Benevolent, ann).unwrap(), 1200.0 - 5000.0);
        delete_all(&journal, Fund::Benevolent, ann).unwrap();
        assert_eq!(journal.funds().enrolment(ann).unwrap().unwrap().next_due_date.as_deref(), Some("2026-01-15"));
    }
}
