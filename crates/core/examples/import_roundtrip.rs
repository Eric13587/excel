//! Import every member of one journal into a fresh one and compare what
//! matters: members, loans and their balances, ledger rows, fund balances.
//!
//! `cargo run --example import_roundtrip -- source.db fresh.db`
use std::collections::HashMap;

use loanmaster_core::db::model::Fund;
use loanmaster_core::imports::{Options, Source, run};
use loanmaster_core::{Journal, OpenOptions};

/// Members, each loan's (member, ref, balance, due), ledger rows, each
/// member's fund balances.
type Figures = (usize, Vec<(String, String, String, String)>, usize, Vec<(String, String)>);

fn figures(journal: &Journal) -> Figures {
    let members = journal.members().all().unwrap();
    let mut loans = Vec::new();
    let mut funds = Vec::new();
    for m in &members {
        for l in journal.loans().for_member(m.id).unwrap() {
            loans.push((m.name.clone(), l.reference.clone(), format!("{:.2}", l.balance), format!("{:?}", l.next_due_date)));
        }
        let balances: Vec<String> = Fund::ALL.iter().map(|f| format!("{:.2}", journal.funds().balance(*f, m.id).unwrap())).collect();
        funds.push((m.name.clone(), balances.join("/")));
    }
    loans.sort();
    funds.sort();
    let ledger: i64 = journal.conn().query_row("SELECT COUNT(*) FROM ledger", [], |r| r.get(0)).unwrap();
    (members.len(), loans, ledger as usize, funds)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let before = figures(&Journal::open(&args[1], OpenOptions::default()).unwrap());
    let source = Source::open(args[1].as_ref()).unwrap();
    let journal = Journal::open(&args[2], OpenOptions::default()).unwrap();
    let ids: Vec<i64> = source.members().unwrap().iter().map(|m| m.id).collect();
    let options = Options { loans: true, savings: true, funds: true, dates: None };
    let started = std::time::Instant::now();
    let out = run(&journal, &source, &ids, &options, &HashMap::new(), &mut |_, _, _| true).unwrap();
    println!("{} in {:?}; {} warning(s)", out.summary(), started.elapsed(), out.warnings.len());
    for w in out.warnings.iter().take(8) {
        println!("  {w}");
    }
    let after = figures(&journal);
    println!("members {} -> {}, ledger {} -> {}", before.0, after.0, before.2, after.2);
    let loans_same = before.1 == after.1;
    println!("loans identical: {loans_same}; funds identical: {}", before.3 == after.3);
    if !loans_same {
        for (a, b) in before.1.iter().zip(after.1.iter()).filter(|(a, b)| a != b).take(5) {
            println!("  {a:?}\n  {b:?}");
        }
    }
}
