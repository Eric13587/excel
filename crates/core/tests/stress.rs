//! Stress: random sequences of what the screens do — money in and out,
//! corrections, loans and their schedules, suspensions, members leaving, the
//! Ledger Grid — with the books checked after every step.
//!
//! An operation may be refused: that is a guard rail doing its job. One that
//! goes through must leave the books whole. A failure names the seed, the step
//! and the steps before it, so it can be replayed.
//!
//! `cargo test -p loanmaster-core --test stress` runs a few seeds; set
//! `LOANMASTER_STRESS=<seeds>` (and optionally `LOANMASTER_STRESS_STEPS`) for
//! a longer run, in release mode — seeds run in parallel.
//! `LOANMASTER_STRESS_SEEDS=<seed>,…` replays chosen seeds. Each seed sets
//! its own today on the journal's clock, so a replay is the same on any day.

use std::collections::HashMap;
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};

use chrono::{Datelike, NaiveDate};
use loanmaster_core::db::members::MemberDetails;
use loanmaster_core::db::model::{Fund, LOAN_PAID, event};
use loanmaster_core::funds::{self, Posting, suspensions as savings};
use loanmaster_core::grid;
use loanmaster_core::loans::projection::{project_loan, reconcile_problem};
use loanmaster_core::loans::{edits, service, suspensions};
use loanmaster_core::money::round2;
use loanmaster_core::undo::UndoStack;
use loanmaster_core::{Error, Journal, Result, dates, membership};

// ------------------------------------------------------------- randomness

/// xorshift64*: small, and the same sequence on every machine.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn below(&mut self, n: usize) -> usize {
        if n == 0 { 0 } else { (self.next() % n as u64) as usize }
    }

    fn range(&mut self, lo: i64, hi: i64) -> i64 {
        lo + (self.next() % ((hi - lo + 1) as u64)) as i64
    }

    fn chance(&mut self, percent: u64) -> bool {
        self.next() % 100 < percent
    }

    fn pick<T: Clone>(&mut self, items: &[T]) -> Option<T> {
        (!items.is_empty()).then(|| items[self.below(items.len())].clone())
    }
}

// ------------------------------------------------------------- the books

fn end_of_month(date: NaiveDate) -> NaiveDate {
    dates::add_days(dates::add_months(date.with_day(1).unwrap(), 1), -1)
}

/// Everything wrong with the books, in words. Empty is whole.
fn check(j: &Journal, today: NaiveDate) -> Result<Vec<String>> {
    let mut wrong = Vec::new();
    let latest = dates::format(end_of_month(today));
    let orphans: i64 = j.conn().query_row("SELECT COUNT(*) FROM pragma_foreign_key_check", [], |r| r.get(0))?;
    if orphans > 0 {
        wrong.push(format!("{orphans} row(s) refer to something that no longer exists"));
    }
    let default_rate = j.settings().default_interest_rate();
    for member in j.members().all()? {
        let (id, name) = (member.id, member.name.clone());
        for fund in Fund::ALL {
            let mut running = 0.0;
            for row in j.funds().rows(fund, id, None, None)? {
                running = round2(running + row.signed());
                if (row.balance - running).abs() > 0.005 {
                    wrong.push(format!("{name} {}: row {} stores a running balance of {}, not {running}", fund.label(), row.id, row.balance));
                }
                if fund != Fund::Benevolent && running < -0.005 {
                    wrong.push(format!("{name} {}: overdrawn to {running} on {} (row {})", fund.label(), row.date, row.id));
                }
                if dates::date_part(&row.date) > latest.as_str() {
                    wrong.push(format!("{name} {}: row {} is dated {}, after this month", fund.label(), row.id, row.date));
                }
            }
        }
        let paused = savings::windows(j, id)?;
        for row in j.funds().rows(Fund::Savings, id, None, None)? {
            if row.transaction_type == "Deposit" && savings::covering(&row.date, &paused).is_some() {
                wrong.push(format!("{name}: a savings deposit on {} falls in a suspension", row.date));
            }
        }
        if let Some(enrolment) = j.funds().enrolment(id)?.filter(|e| e.active) {
            let last = j.funds().last_dated_row(Fund::Benevolent, id, &["Contribution"])?;
            let after_last = last.and_then(|last| dates::add_months_str(&last.date, 1));
            let expected = match (after_last, enrolment.start_date.clone()) {
                (Some(after), Some(start)) => Some(if start > after { start } else { after }),
                (after, start) => after.or(start),
            };
            let month = |d: &Option<String>| d.as_deref().map(dates::month_key);
            if month(&expected) != month(&enrolment.next_due_date) {
                wrong.push(format!("{name}: benevolent next due {:?}, but the contributions say {:?}", enrolment.next_due_date, expected));
            }
        }
        let mut contributions: HashMap<String, usize> = HashMap::new();
        for row in j.funds().rows(Fund::Benevolent, id, None, None)? {
            if row.transaction_type == "Contribution" {
                *contributions.entry(dates::month_key(&row.date)).or_default() += 1;
            }
        }
        for (month, n) in contributions.iter().filter(|(_, n)| **n > 1) {
            wrong.push(format!("{name}: {n} benevolent contributions in {month}"));
        }

        let mut installments = 0.0;
        for loan in j.loans().for_member(id)? {
            let label = format!("{name} {}", loan.reference);
            let rows = j.ledger().for_loan(id, &loan.reference)?;
            if !rows.iter().any(|r| r.event_type == event::LOAN_ISSUED) {
                wrong.push(format!("{label}: no disbursement"));
            }
            for row in &rows {
                if dates::date_part(&row.date) > latest.as_str() {
                    wrong.push(format!("{label}: {} row {} is dated {}, after this month", row.event_type, row.id, row.date));
                }
                if row.principal_balance < -0.005 || row.interest_balance < -0.005 {
                    wrong.push(format!(
                        "{label}: after row {} ({} {}) the loan owes principal {} and interest {}",
                        row.id, row.event_type, row.date, row.principal_balance, row.interest_balance
                    ));
                }
            }
            let gaps = loanmaster_core::loans::guards::unexplained_months(j, id, &loan.reference)?;
            if !gaps.is_empty() {
                wrong.push(format!("{label}: nothing accounts for {}", gaps.join(", ")));
            }
            match reconcile_problem(j, id, &loan.reference)? {
                Some(why) => wrong.push(format!("{label}: cannot be derived from its history ({why})")),
                None => {
                    // Nothing the projection would change: the stored figures
                    // are the ledger's.
                    let mut changed = 0;
                    let mut after = None;
                    let _ = j.savepoint(|| -> Result<()> {
                        changed = project_loan(j, id, &loan.reference)?;
                        after = j.loans().get(loan.id)?;
                        Err(Error::invalid("look, do not touch"))
                    });
                    if let Some(after) = after
                        && (changed > 0
                            || (after.balance - loan.balance).abs() > 0.005
                            || (after.interest_balance - loan.interest_balance).abs() > 0.005
                            || after.status != loan.status)
                    {
                        wrong.push(format!(
                            "{label}: stored {} {}/{} but its history says {} {}/{} ({changed} row(s) stale)",
                            loan.status, loan.balance, loan.interest_balance, after.status, after.balance, after.interest_balance
                        ));
                    }
                }
            }
            if loan.is_active() && loan.balance <= 0.005 {
                wrong.push(format!("{label}: Active with nothing owed"));
            }
            if loan.status == LOAN_PAID && (loan.balance.abs() > 0.005 || loan.interest_balance.abs() > 0.005) {
                wrong.push(format!("{label}: Paid with {} / {} still on it", loan.balance, loan.interest_balance));
            }
            if loan.is_active() {
                installments += loan.installment;
                // Charged and still to charge make up what it was written for.
                let rate = loan.interest_rate.unwrap_or(default_rate);
                let written: f64 = rows
                    .iter()
                    .filter(|r| r.event_type == event::LOAN_ISSUED || r.event_type == event::TOP_UP)
                    .map(|r| round2(r.added * rate))
                    .sum();
                let charges: Vec<f64> = rows.iter().filter(|r| r.event_type == event::INTEREST).map(|r| r.added).collect();
                let charged: f64 = charges.iter().sum();
                let slack = charges.len() as f64 + 0.01;
                if (charged + loan.unearned_interest - written).abs() > slack {
                    wrong.push(format!(
                        "{label}: written for interest {written:.2}, but charged {charged:.2} + still to charge {:.2}",
                        loan.unearned_interest
                    ));
                }
            }
            let spans = j.loans().suspensions_for_loan(loan.id)?;
            let open = spans.iter().filter(|s| s.resumed_date.is_none() && s.status == "active").count();
            if open > 1 || (open == 1) != loan.is_suspended {
                wrong.push(format!("{label}: suspended flag {} with {open} open suspension(s)", loan.is_suspended));
            }
        }
        if (member.default_deduction - round2(installments)).abs() > 0.5 {
            wrong.push(format!("{name}: monthly deduction {} but active instalments {}", member.default_deduction, round2(installments)));
        }
    }
    Ok(wrong)
}

/// The ledgers and suspensions of every loan a finding names, for reading
/// what went wrong.
fn dump(j: &Journal, wrong: &[String]) -> String {
    let mut out = String::new();
    for member in j.members().all().unwrap() {
        for loan in j.loans().for_member(member.id).unwrap() {
            let label = format!("{} {}:", member.name, loan.reference);
            if !wrong.iter().any(|w| w.trim_start().starts_with(&label)) {
                continue;
            }
            out.push_str(&format!(
                "\n{label} {} balance {} interest {} unearned {} instalment {} monthly {} due {:?} suspended {} until {:?}\n",
                loan.status, loan.balance, loan.interest_balance, loan.unearned_interest, loan.installment, loan.monthly_interest,
                loan.next_due_date, loan.is_suspended, loan.suspend_until
            ));
            for r in j.ledger().for_loan(member.id, &loan.reference).unwrap() {
                out.push_str(&format!(
                    "    {:5} {} {:16} +{:<10} -{:<10} p {:<10} i {:<9} split {}/{} edited {} {}\n",
                    r.id, r.date, r.event_type, r.added, r.deducted, r.principal_balance, r.interest_balance,
                    r.principal_portion, r.interest_portion, r.is_edited, r.notes
                ));
            }
            for s in j.loans().suspensions_for_loan(loan.id).unwrap() {
                out.push_str(&format!("    suspension {} {} until {:?} resumed {:?} {}\n", s.id, s.start_date, s.suspend_until, s.resumed_date, s.status));
            }
        }
    }
    out
}

/// Months holding a credit, per member — what an automated step may not post
/// into again.
fn credit_months(j: &Journal, fund: Fund) -> Result<HashMap<(i64, String), usize>> {
    let mut out = HashMap::new();
    for member in j.members().all()? {
        for row in j.funds().rows(fund, member.id, None, None)? {
            if row.transaction_type == fund.credit() {
                *out.entry((member.id, dates::month_key(&row.date))).or_default() += 1;
            }
        }
    }
    Ok(out)
}

// ------------------------------------------------------------ the steps

/// One thing a person could do, with everything it needs chosen up front.
#[derive(Debug, Clone)]
enum Op {
    Deposit(Fund, i64, f64, String),
    Withdraw(Fund, i64, f64, String, bool),
    Interest(i64, f64, String),
    EditFundRow(Fund, i64, String, f64),
    DeleteFundRow(Fund, i64),
    CatchUpPot(Fund, i64, String, f64),
    Enrol(i64, f64, String),
    Contribute(i64),
    CatchUpBenevolent(i64, String),
    Payout(i64, f64, String),
    SuspendSavings(i64, String, Option<u32>),
    ResumeSavings(i64, String),
    RemoveSavingsSuspension(i64),
    DeleteAll(Fund, i64),
    Issue(i64, f64, i64, String, f64),
    Deduct(i64, String),
    CatchUpLoan(i64, String, String),
    DeductRange(i64, String, String, String),
    TopUp(i64, String, f64, i64, String),
    Buyoff(i64, String, Option<String>),
    /// The last flag records it by clearing what it would contradict.
    RecordSuspension(i64, String, Option<String>, bool),
    ResumeLoan(i64),
    RemoveLoanSuspension(i64, String, i64, bool),
    DeleteEntry(i64, i64, u8),
    ChangeRepayment(i64, i64, f64),
    ChangeInterest(i64, i64, String, f64),
    EditTopUp(i64, i64, String, f64, i64),
    MoveDate(i64, i64, String, bool),
    DeleteLoan(i64, String),
    Leave(i64, &'static str, String),
    Reinstate(i64),
    FundGrid { fund: Fund, months: Vec<String>, action: usize, text: &'static str, row: u64, month: u64 },
    LoanGrid { months: Vec<String>, action: usize, text: &'static str, row: u64, month: u64 },
    /// What was chosen had nothing to act on.
    Nothing,
}

impl Op {
    /// Steps that post on their own may never post into a month that already
    /// has its credit.
    fn automated(&self) -> Option<Fund> {
        match self {
            Op::CatchUpPot(fund, ..) => Some(*fund),
            Op::Contribute(_) | Op::CatchUpBenevolent(..) => Some(Fund::Benevolent),
            Op::FundGrid { fund, action, .. } if *action >= 8 => Some(*fund),
            _ => None,
        }
    }
}

struct Sim {
    j: Journal,
    rng: Rng,
    today: NaiveDate,
    undo: UndoStack,
    fund_batches: Vec<grid::fund::BatchResult>,
    loan_batches: Vec<grid::loan::BatchResult>,
}

const AMOUNTS: [f64; 9] = [1.0, 250.0, 1000.0, 2500.0, 7000.0, 12_345.5, 50_000.0, 250_000.0, 3_000_000.0];
const FUNDS: [Fund; 2] = [Fund::Savings, Fund::Christmas];
const FUND_TEXT: [&str; 12] = ["2500", "-500", "@W=1000", "@W=50000", "", "3,000, @W=500", "+@W=200", "@S=2", "100000", "@S=1", "@P=3000", "7000"];
const LOAN_TEXT: [&str; 7] = ["5,000", "", "@T=20,000/12", "@B", "@S=2", "999999", "1"];

impl Sim {
    /// Mostly the past, sometimes this month, now and then a month or two ahead.
    fn date(&mut self) -> String {
        let days = match self.rng.below(10) {
            0 => self.rng.range(1, 75),
            1 | 2 => self.rng.range(-20, 0),
            _ => self.rng.range(-540, 0),
        };
        dates::format(dates::add_days(self.today, days))
    }

    fn amount(&mut self) -> f64 {
        self.rng.pick(&AMOUNTS).unwrap()
    }

    fn member(&mut self) -> Option<i64> {
        let ids: Vec<i64> = self.j.members().all().unwrap().iter().map(|m| m.id).collect();
        self.rng.pick(&ids)
    }

    /// `(member, reference, loan id)` of any loan.
    fn loan(&mut self) -> Option<(i64, String, i64)> {
        let mut all = Vec::new();
        for m in self.j.members().all().unwrap() {
            for l in self.j.loans().for_member(m.id).unwrap() {
                all.push((m.id, l.reference.clone(), l.id));
            }
        }
        self.rng.pick(&all)
    }

    /// `(member, row id, date)` of an entry on some loan, of the given kinds.
    fn entry(&mut self, kinds: &[&str]) -> Option<(i64, i64, String)> {
        let (member, reference, _) = self.loan()?;
        let rows: Vec<(i64, i64, String)> = self
            .j
            .ledger()
            .for_loan(member, &reference)
            .unwrap()
            .into_iter()
            .filter(|r| kinds.is_empty() || kinds.contains(&r.event_type.as_str()))
            .map(|r| (member, r.id, r.date))
            .collect();
        self.rng.pick(&rows)
    }

    fn fund_row(&mut self, fund: Fund) -> Option<(i64, String)> {
        let member = self.member()?;
        let rows: Vec<(i64, String)> = self.j.funds().rows(fund, member, None, None).unwrap().into_iter().map(|r| (r.id, r.date)).collect();
        self.rng.pick(&rows)
    }

    fn months(&mut self) -> Vec<String> {
        let first = dates::add_months(self.today, -(self.rng.range(1, 14) as i32));
        let last = dates::add_months(self.today, self.rng.range(0, 2) as i32);
        dates::month_range(&dates::format(first), &dates::format(last))
    }

    /// Choose what happens next.
    fn plan(&mut self) -> Op {
        let fund = self.rng.pick(&FUNDS).unwrap();
        let roll = self.rng.below(100);
        let none = Op::Nothing;
        match roll {
            0..=7 => match self.member() {
                Some(m) => Op::Deposit(fund, m, self.amount(), self.date()),
                None => none,
            },
            8..=14 => match self.member() {
                Some(m) => Op::Withdraw(fund, m, self.amount(), self.date(), self.rng.chance(50)),
                None => none,
            },
            15 => match self.member() {
                Some(m) => Op::Interest(m, self.amount(), self.date()),
                None => none,
            },
            16..=18 => match self.fund_row(fund) {
                Some((id, date)) => {
                    let date = if self.rng.chance(50) { date } else { self.date() };
                    Op::EditFundRow(fund, id, date, self.amount())
                }
                None => none,
            },
            19..=20 => match self.fund_row(fund) {
                Some((id, _)) => Op::DeleteFundRow(fund, id),
                None => none,
            },
            21..=22 => match self.member() {
                Some(m) => Op::CatchUpPot(fund, m, self.date(), self.amount().min(20_000.0)),
                None => none,
            },
            23 => match self.member() {
                Some(m) => Op::Enrol(m, self.amount().min(5000.0), self.date()),
                None => none,
            },
            24..=25 => self.member().map(Op::Contribute).unwrap_or(none),
            26 => match self.member() {
                Some(m) => Op::CatchUpBenevolent(m, self.date()),
                None => none,
            },
            27 => match self.member() {
                Some(m) => Op::Payout(m, self.amount(), self.date()),
                None => none,
            },
            28..=29 => match (self.member(), self.rng.below(3)) {
                (Some(m), 0) => {
                    let months = if self.rng.chance(25) { None } else { Some(self.rng.range(1, 6) as u32) };
                    Op::SuspendSavings(m, self.date(), months)
                }
                (Some(m), 1) => Op::ResumeSavings(m, self.date()),
                (Some(m), _) => {
                    let spans: Vec<i64> = self.j.funds().suspensions(m).unwrap().iter().map(|s| s.id).collect();
                    self.rng.pick(&spans).map(Op::RemoveSavingsSuspension).unwrap_or(none)
                }
                (None, _) => none,
            },
            30 => self.member().map(|m| Op::DeleteAll(fund, m)).unwrap_or(none),
            31..=35 => match self.member() {
                Some(m) => {
                    let principal = self.rng.pick(&[10_000.0, 60_000.0, 100_000.0, 250_000.0]).unwrap();
                    let (months, rate) = (self.rng.range(1, 36), self.rng.pick(&[0.0, 0.1, 0.15]).unwrap());
                    Op::Issue(m, principal, months, self.date(), rate)
                }
                None => none,
            },
            36..=41 => self.loan().map(|(m, r, _)| Op::Deduct(m, r)).unwrap_or(none),
            42..=44 => match self.loan() {
                Some((m, r, _)) => Op::CatchUpLoan(m, r, self.date()),
                None => none,
            },
            45..=46 => match self.loan() {
                Some((m, r, _)) => Op::DeductRange(m, r, self.date(), self.date()),
                None => none,
            },
            47..=48 => match self.loan() {
                Some((m, r, _)) => Op::TopUp(m, r, self.amount().min(300_000.0), self.rng.range(1, 36), self.date()),
                None => none,
            },
            49..=50 => match self.loan() {
                Some((m, r, _)) => {
                    let date = self.rng.chance(50).then(|| self.date());
                    Op::Buyoff(m, r, date)
                }
                None => none,
            },
            51..=52 => match (self.loan(), self.rng.below(3)) {
                (Some((_, _, id)), 0) => {
                    let start = self.date();
                    let end = if self.rng.chance(30) { None } else { dates::add_months_str(&start, self.rng.range(1, 5) as i32) };
                    Op::RecordSuspension(id, start, end, self.rng.chance(50))
                }
                (Some((_, _, id)), 1) => Op::ResumeLoan(id),
                (Some((m, r, id)), _) => {
                    let spans: Vec<i64> = self.j.loans().suspensions_for_loan(id).unwrap().iter().map(|s| s.id).collect();
                    let rebuild = self.rng.chance(50);
                    self.rng.pick(&spans).map(|span| Op::RemoveLoanSuspension(m, r, span, rebuild)).unwrap_or(none)
                }
                (None, _) => none,
            },
            53..=56 => match self.entry(&[]) {
                Some((m, id, _)) => Op::DeleteEntry(m, id, self.rng.below(3) as u8),
                None => none,
            },
            57..=59 => match self.entry(&[event::REPAYMENT]) {
                Some((m, id, _)) => Op::ChangeRepayment(m, id, self.amount()),
                None => none,
            },
            60..=61 => match self.entry(&[event::INTEREST]) {
                Some((m, id, date)) => Op::ChangeInterest(m, id, date, self.rng.pick(&[0.0, 1.0, 100.0, 900.0, 5000.0]).unwrap()),
                None => none,
            },
            62 => match self.entry(&[event::TOP_UP]) {
                Some((m, id, _)) => Op::EditTopUp(m, id, self.date(), self.amount().min(300_000.0), self.rng.range(1, 36)),
                None => none,
            },
            63..=64 => match self.entry(&[]) {
                Some((m, id, _)) => Op::MoveDate(m, id, self.date(), self.rng.chance(50)),
                None => none,
            },
            65 => self.loan().map(|(m, r, _)| Op::DeleteLoan(m, r)).unwrap_or(none),
            66..=67 => match self.member() {
                Some(m) => Op::Leave(m, self.rng.pick(&["Retired", "Resigned", "Terminated", "Deceased"]).unwrap(), self.date()),
                None => none,
            },
            68 => self.member().map(Op::Reinstate).unwrap_or(none),
            69..=76 => Op::FundGrid {
                fund: self.rng.pick(&[Fund::Savings, Fund::Christmas, Fund::Benevolent]).unwrap(),
                months: self.months(),
                action: self.rng.below(10),
                text: self.rng.pick(&FUND_TEXT).unwrap(),
                row: self.rng.next(),
                month: self.rng.next(),
            },
            _ => Op::LoanGrid {
                months: self.months(),
                action: self.rng.below(10),
                text: self.rng.pick(&LOAN_TEXT).unwrap(),
                row: self.rng.next(),
                month: self.rng.next(),
            },
        }
    }

    /// Do it, as the screen that offers it would.
    fn apply(&mut self, op: &Op) -> Result<()> {
        let j = &self.j;
        match op.clone() {
            Op::Deposit(fund, m, a, d) => funds::deposit(j, fund, m, a, &d, "").map(|_| ()),
            Op::Withdraw(fund, m, a, d, over) => funds::withdraw(j, fund, m, a, &d, "", over).map(|_| ()),
            Op::Interest(m, a, d) => funds::post(j, Posting::new(Fund::Savings, m, &d, "Interest", a, "")).map(|_| ()),
            Op::EditFundRow(fund, id, d, a) => funds::edit_row(j, fund, id, &d, a, "edited"),
            Op::DeleteFundRow(fund, id) => funds::delete_row(j, fund, id),
            Op::CatchUpPot(fund, m, t, monthly) => funds::catch_up_pot(j, fund, m, Some(monthly), Some(&t)).map(|_| ()),
            Op::Enrol(m, a, d) => funds::enrol(j, m, a, &d),
            Op::Contribute(m) => funds::contribute_once(j, m).map(|_| ()),
            Op::CatchUpBenevolent(m, t) => funds::catch_up_benevolent(j, m, Some(&t)).map(|_| ()),
            Op::Payout(m, a, d) => funds::payout(j, m, a, &d, "").map(|_| ()),
            Op::SuspendSavings(m, d, months) => savings::suspend(j, m, &d, months).map(|_| ()),
            Op::ResumeSavings(m, d) => savings::resume(j, m, &d).map(|_| ()),
            Op::RemoveSavingsSuspension(id) => savings::remove(j, id).map(|_| ()),
            Op::DeleteAll(fund, m) => funds::delete_all(j, fund, m),
            Op::Issue(m, principal, months, d, rate) => service::issue(j, m, principal, months, &d, Some(rate)).map(|_| ()),
            Op::Deduct(m, r) => service::deduct(j, m, &r),
            Op::CatchUpLoan(m, r, t) => service::catch_up(j, m, &r, Some(&t)).map(|_| ()),
            Op::DeductRange(m, r, from, to) => service::deduct_range(j, m, &r, &from, &to).map(|_| ()),
            Op::TopUp(m, r, a, months, d) => service::top_up(j, m, &r, a, months, Some(&d)),
            Op::Buyoff(m, r, d) => service::buyoff(j, m, &r, d.as_deref()).map(|_| ()),
            Op::RecordSuspension(id, start, end, false) => suspensions::record(j, id, &start, end.as_deref(), None).map(|_| ()),
            Op::RecordSuspension(id, start, end, true) => suspensions::record_and_clear(j, id, &start, end.as_deref()).map(|_| ()),
            Op::ResumeLoan(id) => suspensions::resume(j, id, None),
            Op::RemoveLoanSuspension(m, r, span, rebuild) => suspensions::remove(j, m, &r, span, rebuild).map(|_| ()),
            Op::DeleteEntry(m, id, then) => {
                self.undo.delete_entry(j, m, id)?;
                if then >= 1 {
                    self.undo.undo(j)?;
                }
                if then == 2 {
                    self.undo.redo(j)?;
                }
                Ok(())
            }
            Op::ChangeRepayment(m, id, a) => edits::change_repayment(j, m, id, a, ""),
            Op::ChangeInterest(m, id, d, a) => edits::edit_entry(j, m, id, &d, a, 0.0, "", true),
            Op::EditTopUp(m, id, d, a, months) => edits::edit_top_up(j, m, id, &d, a, months, ""),
            Op::MoveDate(m, id, d, cascade) => edits::move_date(j, m, id, &d, cascade).map(|_| ()),
            Op::DeleteLoan(m, r) => service::delete_loan(j, m, &r),
            Op::Leave(m, status, d) => membership::leave(j, m, status, Some(&d)).map(|_| ()),
            Op::Reinstate(m) => membership::reinstate(j, m),
            Op::FundGrid { fund, months, action, text, row, month } => {
                if action == 7 {
                    let batch = self.fund_batches.pop().ok_or_else(|| Error::invalid("nothing saved"))?;
                    return grid::fund::revert(j, batch.fund, &batch).map(|_| ());
                }
                let g = grid::fund::load(j, fund, &months, false, false)?;
                let rows: Vec<&grid::fund::MemberRow> = g.members.iter().collect();
                let changes = match action {
                    0..=5 => {
                        let row = rows.get(row as usize % rows.len().max(1)).ok_or_else(|| Error::invalid("no rows"))?;
                        match grid::fund::stage(&g, row, &months[month as usize % months.len()], text) {
                            Ok(Some(change)) => vec![change],
                            Ok(None) => return Ok(()),
                            Err(why) => return Err(Error::invalid(why)),
                        }
                    }
                    6 => grid::fund::plan_delete_months(j, &g, &rows, &[months[month as usize % months.len()].clone()])?.fund_changes,
                    _ => grid::fund::plan_fill(j, &g, &rows, &months, None)?.changes,
                };
                let batch = grid::fund::apply_changes(j, fund, &changes)?;
                self.fund_batches.push(batch);
                Ok(())
            }
            Op::LoanGrid { months, action, text, row, month } => {
                if action == 7 {
                    let batch = self.loan_batches.pop().ok_or_else(|| Error::invalid("nothing saved"))?;
                    return grid::loan::revert(j, &batch).map(|_| ());
                }
                let g = grid::loan::load(j, &months, false, false, false)?;
                let rows: Vec<&grid::loan::LoanRow> = g.rows.iter().collect();
                let changes = match action {
                    0..=5 => {
                        let row = rows.get(row as usize % rows.len().max(1)).ok_or_else(|| Error::invalid("no rows"))?;
                        match grid::loan::stage(&g, row, &months[month as usize % months.len()], text) {
                            Ok(Some(change)) => vec![change],
                            Ok(None) => return Ok(()),
                            Err(why) => return Err(Error::invalid(why)),
                        }
                    }
                    6 => grid::loan::plan_delete_months(j, &rows, &[months[month as usize % months.len()].clone()])?.loan_changes,
                    _ => grid::loan::plan_fill(&g, &rows, &months, Some(&dates::format(j.today()))).changes,
                };
                let batch = grid::loan::apply_changes(j, &changes)?;
                self.loan_batches.push(batch);
                Ok(())
            }
            Op::Nothing => Ok(()),
        }
    }
}

/// The day a seed's journal takes as today: one in the 1,000 days from
/// June 2025, so runs meet every month end and a leap February.
fn today_for(seed: u64) -> NaiveDate {
    dates::add_days(NaiveDate::from_ymd_opt(2025, 6, 1).unwrap(), (seed % 1000) as i64)
}

/// Run one seed; the first breakage, described, if the books broke.
fn run(seed: u64, steps: usize) -> Option<String> {
    let today = today_for(seed);
    let j = Journal::open_in_memory().unwrap();
    j.set_today(today);
    for name in ["ANN", "BEN", "CAROL", "DAVID", "EVE"] {
        membership::add(&j, &MemberDetails { name: name.into(), ..Default::default() }).unwrap();
    }
    let mut sim = Sim { j, rng: Rng(seed.max(1)), today, undo: UndoStack::default(), fund_batches: Vec::new(), loan_batches: Vec::new() };
    let mut log: Vec<String> = Vec::new();
    let watch = |j: &Journal| -> HashMap<Fund, HashMap<(i64, String), usize>> {
        Fund::ALL.into_iter().map(|f| (f, credit_months(j, f).unwrap())).collect()
    };
    for step in 0..steps {
        let op = sim.plan();
        let before = watch(&sim.j);
        let result = sim.apply(&op);
        let outcome = match &result {
            Ok(()) => "ok".to_string(),
            Err(e) => format!("refused: {e}"),
        };
        log.push(format!("{step:4}  {op:?}  ->  {outcome}"));
        // LOANMASTER_STRESS_WATCH="BEN L-002" follows one loan step by step.
        if let Ok(watched) = std::env::var("LOANMASTER_STRESS_WATCH") {
            let shown = dump(&sim.j, &[format!("{watched}: watched")]);
            if !shown.is_empty() {
                eprintln!("{step:4}  {op:?}  ->  {outcome}{shown}");
            }
        }
        let mut wrong = check(&sim.j, today).unwrap();
        if let (Some(fund), Ok(())) = (op.automated(), &result) {
            let after = watch(&sim.j);
            for (key, n) in &after[&fund] {
                let had = before[&fund].get(key).copied().unwrap_or(0);
                if *n > had && had > 0 {
                    wrong.push(format!("{}: an automated step posted into {key:?}, which already had {had}", fund.label()));
                }
            }
        }
        if !wrong.is_empty() {
            let keep = std::env::var("LOANMASTER_STRESS_TAIL").ok().and_then(|n| n.parse().ok()).unwrap_or(25);
            let tail: Vec<&str> = log.iter().rev().take(keep).rev().map(String::as_str).collect();
            return Some(format!(
                "seed {seed} (today {today}), step {step}: the books broke.\n\nWhat is wrong:\n  {}\n\nThe steps before it:\n  {}\n{}",
                wrong.join("\n  "),
                tail.join("\n  "),
                dump(&sim.j, &wrong)
            ));
        }
    }
    None
}

#[test]
fn random_operations_leave_the_books_whole() {
    let seeds: u64 = std::env::var("LOANMASTER_STRESS").ok().and_then(|s| s.parse().ok()).unwrap_or(6);
    let steps: usize = std::env::var("LOANMASTER_STRESS_STEPS").ok().and_then(|s| s.parse().ok()).unwrap_or(400);
    let chosen: Vec<u64> = match std::env::var("LOANMASTER_STRESS_SEEDS") {
        Ok(list) => list.split(',').filter_map(|s| s.trim().parse().ok()).collect(),
        Err(_) => (1..=seeds).map(|n| n * 7919).collect(),
    };
    let seeds = chosen.len();
    let next = AtomicUsize::new(0);
    let broken = Mutex::new(Vec::new());
    let threads = std::thread::available_parallelism().map_or(1, |n| n.get()).min(seeds.max(1));
    std::thread::scope(|scope| {
        for _ in 0..threads {
            scope.spawn(|| {
                while let Some(seed) = chosen.get(next.fetch_add(1, Ordering::Relaxed)) {
                    if let Some(report) = run(*seed, steps) {
                        broken.lock().unwrap().push((*seed, report));
                    }
                }
            });
        }
    });
    let mut broken = broken.into_inner().unwrap();
    broken.sort_by_key(|(seed, _)| *seed);
    let broken: Vec<String> = broken.into_iter().map(|(_, report)| report).collect();
    assert!(broken.is_empty(), "{} of {seeds} run(s) broke the books:\n\n{}", broken.len(), broken.join("\n\n────────\n\n"));
}
