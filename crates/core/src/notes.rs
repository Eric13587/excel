//! What an entry's note says to the member reading it.
//!
//! A note is written as an entry is made, and says as much about how it was
//! made — a catch-up, the Ledger Grid, an import — as about what it is, with
//! figures as the engine wrote them. A member needs to know what the entry
//! is. These rules reword the notes LoanMaster writes, and those 1.x wrote,
//! and keep a person's own words as they were typed. The journal keeps the
//! note as written; only what is shown changes.

use std::sync::LazyLock;

use regex::Regex;

use crate::db::model::{WITHDRAWAL, event};
use crate::money::shillings;

/// How an entry was made, in brackets after its note.
const MARKERS: [&str; 4] = ["(Auto)", "(Catch-up)", "(grid entry)", "(Excel import)"];

/// A figure as the engine wrote it: `15000.0`, or `15,000` with separators.
const FIGURE: &str = r"(\d+(?:,\d{3})*(?:\.\d+)?)";
static ISSUED: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(r"(?i)^Principal:\s*{FIGURE},\s*Total Interest:\s*{FIGURE}(?:,\s*Duration:\s*(\d+)\s*m)?")).unwrap()
});
static TOPPED_UP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(r"(?i)^Top-Up:\s*{FIGURE},\s*Add'l Interest:\s*{FIGURE}(?:,\s*Duration:\s*(\d+)\s*m)?")).unwrap()
});

/// The note without the markers saying how the entry was made.
pub fn without_markers(note: &str) -> String {
    let mut out = note.to_string();
    for marker in MARKERS {
        while let Some(at) = out.find(marker) {
            let start = out[..at].trim_end().len();
            out.replace_range(start..at + marker.len(), "");
        }
    }
    out.trim().to_string()
}

/// "1 month", "12 months".
fn months(n: &str) -> String {
    if n.trim() == "1" { "1 month".into() } else { format!("{} months", n.trim()) }
}

fn money(text: &str) -> String {
    text.replace(',', "").parse::<f64>().map(shillings).unwrap_or_else(|_| text.to_string())
}

/// How a loan's issue is noted: "Principal 100,000; interest 15,000 over 20
/// months".
pub fn issued(principal: f64, interest: f64, term: i64) -> String {
    format!("Principal {}; interest {} over {}", shillings(principal), shillings(interest), months(&term.to_string()))
}

/// How a top-up is noted: "Top-up 37,000; added interest 5,550; new term 18
/// months".
pub fn topped_up(amount: f64, interest: f64, term: i64) -> String {
    format!("Top-up {}; added interest {}; new term {}", shillings(amount), shillings(interest), months(&term.to_string()))
}

/// A loan entry's kind as the member sees it: the society earns interest,
/// the member is charged it.
pub fn loan_entry(event_type: &str) -> &str {
    match event_type {
        event::INTEREST => "Interest Charged",
        event::BUYOFF => "Loan Settled",
        other => other,
    }
}

/// A loan entry's note as the member sees it.
pub fn loan_note(note: &str) -> String {
    let lower = note.trim().to_lowercase();
    if lower.starts_with("monthly interest") {
        return "Monthly interest".into();
    }
    if lower.starts_with("monthly deduction") {
        return if lower.contains("recorded late") { "Monthly deduction, recorded late".into() } else { "Monthly deduction".into() };
    }
    if lower.starts_with("buyoff: realizing unearned interest") {
        return "Remaining interest, due on settlement".into();
    }
    if lower.starts_with("full loan settlement") {
        return "Loan settled in full".into();
    }
    if let Some(c) = ISSUED.captures(note.trim()) {
        let mut text = format!("Principal {}; interest {}", money(&c[1]), money(&c[2]));
        if let Some(term) = c.get(3) {
            text.push_str(&format!(" over {}", months(term.as_str())));
        }
        return text;
    }
    if let Some(c) = TOPPED_UP.captures(note.trim()) {
        let mut text = format!("Top-up {}; added interest {}", money(&c[1]), money(&c[2]));
        if let Some(term) = c.get(3) {
            text.push_str(&format!("; new term {}", months(term.as_str())));
        }
        return text;
    }
    without_markers(note)
}

/// What the funds' own notes call a month's money in: whoever wrote it, it
/// is the member's monthly contribution.
const CONTRIBUTIONS: [&str; 5] = ["monthly contribution", "monthly increment", "monthly christmas contribution", "benevolent contribution", "xmas import"];
/// Notes that only name the fund the entry is in.
const FUND_NAMES: [&str; 3] = ["savings / shares", "christmas", "benevolent"];

/// A fund entry's note as the member sees it. `kind` is the entry's
/// transaction type — a note that only repeats it says nothing more.
pub fn fund_note(kind: &str, note: &str) -> String {
    let plain = without_markers(note);
    let lower = plain.to_lowercase();
    if lower.is_empty() || lower == kind.to_lowercase() || (FUND_NAMES.contains(&lower.as_str()) && kind == WITHDRAWAL) {
        return String::new();
    }
    if lower == "retired" {
        return "Retirement withdrawal".into();
    }
    if lower.starts_with("benevolent payout") {
        return "Benevolent payout".into();
    }
    if kind != WITHDRAWAL && (CONTRIBUTIONS.iter().any(|c| lower.starts_with(c)) || FUND_NAMES.contains(&lower.as_str()) || lower == "excel import") {
        return "Monthly contribution".into();
    }
    plain
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn loan_notes_say_what_the_entry_is() {
        for (note, shown) in [
            ("Monthly Interest Accrual (Auto)", "Monthly interest"),
            ("Monthly Interest Accrual", "Monthly interest"),
            ("Monthly Deduction (Auto)", "Monthly deduction"),
            ("Monthly Deduction (grid entry, recorded late)", "Monthly deduction, recorded late"),
            ("Principal: 100000.0, Total Interest: 15000.0", "Principal 100,000; interest 15,000"),
            ("Principal: 60000.0, Total Interest: 9000.0, Duration: 12m", "Principal 60,000; interest 9,000 over 12 months"),
            ("Top-Up: 37000.0, Add'l Interest: 5550.0, Duration: 18m", "Top-up 37,000; added interest 5,550; new term 18 months"),
            ("Buyoff: Realizing unearned interest", "Remaining interest, due on settlement"),
            ("Full Loan Settlement (Buyoff)", "Loan settled in full"),
            ("Paid by cheque 0042", "Paid by cheque 0042"),
            ("", ""),
        ] {
            assert_eq!(loan_note(note), shown, "{note}");
        }
        // What LoanMaster writes now reads the same either way.
        assert_eq!(loan_note(&issued(100000.0, 15000.0, 20)), "Principal 100,000; interest 15,000 over 20 months");
        assert_eq!(loan_note(&topped_up(37000.0, 5550.0, 18)), "Top-up 37,000; added interest 5,550; new term 18 months");
        assert_eq!(loan_entry(event::INTEREST), "Interest Charged");
        assert_eq!(issued(20000.0, 3000.0, 1), "Principal 20,000; interest 3,000 over 1 month");
        assert_eq!(loan_entry(event::REPAYMENT), event::REPAYMENT);
    }

    #[test]
    fn fund_notes_keep_what_the_member_needs() {
        for (kind, note, shown) in [
            ("Deposit", "Monthly Contribution (Catch-up)", "Monthly contribution"),
            ("Deposit", "Monthly Increment (Auto)", "Monthly contribution"),
            ("Deposit", "Savings / Shares (grid entry)", "Monthly contribution"),
            ("Deposit", "Christmas (grid entry)", "Monthly contribution"),
            ("Deposit", "Monthly Christmas Contribution (Auto)", "Monthly contribution"),
            ("Deposit", "Xmas import (Mr. A. Member)", "Monthly contribution"),
            ("Deposit", "Excel import", "Monthly contribution"),
            ("Contribution", "Benevolent Contribution (Auto)", "Monthly contribution"),
            ("Contribution", "Benevolent (grid entry)", "Monthly contribution"),
            ("Withdrawal", "Withdrawal", ""),
            ("Withdrawal", "Savings / Shares (grid entry)", ""),
            ("Withdrawal", "Benevolent Payout (grid entry)", "Benevolent payout"),
            ("Withdrawal", "RETIRED", "Retirement withdrawal"),
            ("Withdrawal", "Resignation withdrawal", "Resignation withdrawal"),
            ("Deposit", "Bonus from the dividend", "Bonus from the dividend"),
            ("Deposit", "", ""),
        ] {
            assert_eq!(fund_note(kind, note), shown, "{kind}: {note}");
        }
    }
}
