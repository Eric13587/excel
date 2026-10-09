//! Every document format, written from a small journal.

use loanmaster_core::Journal;
use loanmaster_core::db::members::MemberDetails;
use loanmaster_core::db::model::Fund;
use loanmaster_core::funds;
use loanmaster_core::loans::service::{catch_up, issue};
use loanmaster_core::reports::{self, Table, Value};
use loanmaster_core::statements::{StatementConfig, prepare};
use loanmaster_reports::{Branding, Format, csv, pdf, write_statement_pdf, write_statement_xlsx, write_table};

fn journal_with(name: &str) -> (Journal, i64) {
    let journal = Journal::open_in_memory().unwrap();
    let id = journal.members().add(&MemberDetails { name: name.into(), phone: "0700".into(), ..Default::default() }).unwrap();
    issue(&journal, id, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
    catch_up(&journal, id, "L-001", Some("2026-06-01")).unwrap();
    funds::post(&journal, funds::Posting::new(Fund::Savings, id, "2026-02-01", "Deposit", 2000.0, "")).unwrap();
    funds::post(&journal, funds::Posting::new(Fund::Savings, id, "2026-03-15", "Withdrawal", 500.0, "")).unwrap();
    (journal, id)
}

fn interest(journal: &Journal) -> Table {
    reports::loan_interest(journal, "2026-02-01", None, &mut |_, _, _| true).unwrap()
}

#[test]
fn the_format_follows_the_file_name() {
    assert_eq!(Format::of("a/report.PDF".as_ref()), Format::Pdf);
    assert_eq!(Format::of("report.csv".as_ref()), Format::Csv);
    assert_eq!(Format::of("report.xlsx".as_ref()), Format::Xlsx);
    assert_eq!(Format::of("report".as_ref()), Format::Xlsx);
}

#[test]
fn csv_quotes_what_needs_it_and_writes_whole_numbers_plainly() {
    let table = Table {
        columns: vec!["Name".into(), "Amount".into()],
        rows: vec![vec![Value::text("Smith, \"Jr\""), Value::Number(1250.0)], vec![Value::text("Ann"), Value::Number(12.5)]],
        total: Some(vec![Value::text("TOTAL"), Value::Number(1262.5)]),
        ..Table::default()
    };
    assert_eq!(csv::render(&table), "Name,Amount\n\"Smith, \"\"Jr\"\"\",1250\nAnn,12.5\nTOTAL,1262.5\n");
}

#[test]
fn every_report_format_is_written() {
    let (journal, _) = journal_with("Ann");
    let dir = tempfile::tempdir().unwrap();
    let table = interest(&journal);
    for name in ["r.xlsx", "r.csv", "r.pdf"] {
        let path = dir.path().join(name);
        write_table(&table, &Branding::default(), &path).unwrap();
        let bytes = std::fs::read(&path).unwrap();
        match Format::of(&path) {
            Format::Xlsx => assert!(bytes.starts_with(b"PK")),
            Format::Csv => assert!(String::from_utf8(bytes).unwrap().starts_with("Name,Loan Amount")),
            Format::Pdf => assert!(bytes.starts_with(b"%PDF-")),
        }
    }
}

#[test]
fn a_statement_is_written_as_pdf_and_workbook() {
    let (journal, id) = journal_with("Ann");
    let dir = tempfile::tempdir().unwrap();
    let config = StatementConfig::default();
    let statement = prepare(&journal, id, "2026-01-01", "2026-06-30", &config).unwrap();
    write_statement_pdf(&statement, &config, &Branding::default(), &dir.path().join("s.pdf")).unwrap();
    write_statement_xlsx(&statement, &config, &dir.path().join("s.xlsx")).unwrap();
    assert!(std::fs::read(dir.path().join("s.pdf")).unwrap().starts_with(b"%PDF-"));

    // One column, and savings alone: no merged single cells, nothing missing.
    let narrow = StatementConfig { show_loans: false, savings_columns: vec!["Amount".into()], columns: vec!["Date".into()], ..config };
    let statement = prepare(&journal, id, "2026-01-01", "2026-06-30", &narrow).unwrap();
    write_statement_xlsx(&statement, &narrow, &dir.path().join("narrow.xlsx")).unwrap();
    pdf::statement(&statement, &narrow, &Branding::default()).unwrap();
}

#[test]
fn a_name_is_printed_as_written_never_read_as_markup() {
    let (journal, id) = journal_with("#set page(width: 1pt) [*Ann*] $x$ \\ @ref <label> // not a comment");
    let branding = Branding { org_name: "#panic(\"no\")".into(), ..Branding::default() };
    pdf::table(&interest(&journal), &branding).unwrap();
    let config = StatementConfig { footer: "#panic()".into(), title: "]{".into(), ..StatementConfig::default() };
    let statement = prepare(&journal, id, "2026-01-01", "2026-06-30", &config).unwrap();
    pdf::statement(&statement, &config, &branding).unwrap();
}

#[test]
fn a_logo_that_cannot_be_read_is_left_out() {
    let (journal, _) = journal_with("Ann");
    let branding = Branding { logo: Some("/no/such/logo.png".into()), ..Branding::default() };
    assert!(pdf::table(&interest(&journal), &branding).unwrap().starts_with(b"%PDF-"));
}

#[test]
fn the_shipped_logos_print() {
    let (journal, id) = journal_with("Ann");
    for logo in ["nairobi_water_sacco_logo.png", "nairobi_water_sacco_logo.jpg"] {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../resources").join(logo);
        let branding = Branding { logo: Some(path), org_name: "Society".into(), ..Branding::default() };
        assert!(pdf::table(&interest(&journal), &branding).unwrap().starts_with(b"%PDF-"));
        let config = StatementConfig::default();
        let statement = prepare(&journal, id, "2026-01-01", "2026-06-30", &config).unwrap();
        assert!(pdf::statement(&statement, &config, &branding).unwrap().starts_with(b"%PDF-"));
    }
}
