//! Prepare every member's statement and dump it as JSON, for comparing with
//! LoanMaster 1.x's statement presentation on the same journal.
//!
//! `cargo run --example statement_dump -- journal.db out.json`
use loanmaster_core::statements::{StatementConfig, prepare};
use loanmaster_core::{Journal, OpenOptions};
use serde_json::{Value, json};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let journal = Journal::open(&args[1], OpenOptions::default()).expect("journal");
    let mut out = serde_json::Map::new();
    let mut members = journal.members().all().unwrap();
    members.sort_by_key(|m| m.id);
    for member in members {
        for (from, to) in [("2025-11-01", "2026-10-31"), ("2000-01-01", "2026-10-09")] {
            let s = prepare(&journal, member.id, from, to, &StatementConfig::default()).unwrap();
            let loans: Vec<Value> = s
                .loans
                .iter()
                .map(|section| {
                    let lines: Vec<Value> = section
                        .lines
                        .iter()
                        .map(|l| json!([l.date, l.event_type, l.debit, l.interest, l.credit, l.balance, l.gross, l.notes, l.annotation]))
                        .collect();
                    json!([section.reference, lines])
                })
                .collect();
            let savings: Vec<Value> = s.savings.iter().map(|l| json!([l.date, l.kind, l.amount, l.balance, l.notes])).collect();
            out.insert(
                format!("{} {from}", member.id),
                json!({"period": s.period, "status": if s.active { "Active" } else { "Inactive" }, "loans": loans,
                       "savings": savings, "net": s.net_outstanding, "gross": s.gross_outstanding,
                       "savings_balance": s.savings_balance}),
            );
        }
    }
    std::fs::write(&args[2], serde_json::to_string(&Value::Object(out)).unwrap()).unwrap();
}
