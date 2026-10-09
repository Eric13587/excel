//! Open a journal and print what opening it found.
//! `cargo run --example inspect -- path/to/journal.db`
use loanmaster_core::{Journal, OpenOptions};

fn main() {
    let path = std::env::args().nth(1).expect("journal path");
    match Journal::open(&path, OpenOptions::default()) {
        Ok(journal) => {
            println!("{:#?}", journal.report());
            let members = journal.members().summaries().unwrap();
            let active = journal.loans().all_active().unwrap();
            let savings = journal.funds().total(loanmaster_core::db::model::Fund::Savings).unwrap();
            println!("members: {}, active loans: {}, savings total: {}", members.len(), active.len(),
                     loanmaster_core::money::format_money(savings, 2));
        }
        Err(e) => println!("refused: {e}"),
    }
}
