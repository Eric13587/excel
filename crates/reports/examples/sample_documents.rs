//! Write one of each document from a journal into a folder, for looking at.
//!
//! `cargo run --example sample_documents -- journal.db out-dir member-id [logo]`
use std::path::Path;

use loanmaster_core::db::model::Fund;
use loanmaster_core::reports::{self, MEMBER_COLUMNS};
use loanmaster_core::statements::{StatementConfig, prepare};
use loanmaster_core::{Journal, OpenOptions, arrears};
use loanmaster_reports::{Branding, write_statement_pdf, write_statement_xlsx, write_table};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let journal = Journal::open(&args[1], OpenOptions::default()).expect("journal");
    let out = Path::new(&args[2]);
    let member: i64 = args[3].parse().unwrap();
    let mut branding = Branding::of(&journal);
    if let Some(logo) = args.get(4) {
        branding.logo = Some(logo.into());
        branding.org_name = "Sample Society".into();
    }
    let mut go = |_: usize, _: usize, _: &str| true;
    let started = std::time::Instant::now();
    let interest = reports::loan_interest(&journal, "2026-08-01", None, &mut go).unwrap();
    for ext in ["pdf", "xlsx", "csv"] {
        write_table(&interest, &branding, &out.join(format!("interest.{ext}"))).unwrap();
    }
    let fund = reports::fund_period(&journal, Fund::Savings, "2025-11-01", "2026-10-31", None, &mut go).unwrap();
    write_table(&fund, &branding, &out.join("savings_fund.pdf")).unwrap();
    write_table(&fund, &branding, &out.join("savings_fund.xlsx")).unwrap();
    let keys: Vec<&str> = MEMBER_COLUMNS.iter().map(|(k, _)| *k).take(8).collect();
    write_table(&reports::members_list(&journal, &keys, &mut go).unwrap(), &branding, &out.join("members.pdf")).unwrap();
    let rows = arrears::arrears(&journal, Some("2027-01-15")).unwrap();
    write_table(&reports::arrears_report(&rows, "2027-01-15").unwrap(), &branding, &out.join("arrears.pdf")).unwrap();
    let config = StatementConfig::default();
    let statement = prepare(&journal, member, "2025-11-01", "2026-10-31", &config).unwrap();
    write_statement_pdf(&statement, &config, &branding, &out.join("statement.pdf")).unwrap();
    write_statement_xlsx(&statement, &config, &out.join("statement.xlsx")).unwrap();
    println!("written in {:?}", started.elapsed());
}
