//! Build every report's table from a journal and dump them as JSON, for
//! comparing with LoanMaster 1.x's CSV exports of the same journal.
//!
//! `cargo run --example report_tables -- journal.db out.json`
use loanmaster_core::db::model::Fund;
use loanmaster_core::reports::{self, MEMBER_COLUMNS, Table, Value};
use loanmaster_core::{Journal, OpenOptions, arrears};
use serde_json::{Value as Json, json};

fn dump(table: &Table) -> Json {
    let cell = |v: &Value| match v {
        Value::Text(t) => json!(t),
        Value::Number(n) => json!(n),
        Value::Blank => json!(""),
    };
    let mut rows: Vec<Vec<Json>> = table.rows.iter().map(|r| r.iter().map(cell).collect()).collect();
    if let Some(total) = &table.total {
        rows.push(total.iter().map(cell).collect());
    }
    json!({"columns": table.columns, "rows": rows})
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let journal = Journal::open(&args[1], OpenOptions::default()).expect("journal");
    let mut go = |_: usize, _: usize, _: &str| true;
    let mut out = serde_json::Map::new();
    for q in ["2025-08-01", "2025-11-01", "2026-02-01", "2026-05-01", "2026-08-01"] {
        out.insert(format!("loans {q}"), dump(&reports::loan_interest(&journal, q, None, &mut go).unwrap()));
        out.insert(format!("savings {q}"), dump(&reports::savings_quarter(&journal, q, None, &mut go).unwrap()));
    }
    for (name, fund) in [("savings", Fund::Savings), ("christmas", Fund::Christmas), ("benevolent", Fund::Benevolent)] {
        let table = reports::fund_period(&journal, fund, "2025-11-01", "2026-10-31", None, &mut go).unwrap();
        out.insert(format!("fund {name}"), dump(&table));
    }
    let keys: Vec<&str> = MEMBER_COLUMNS.iter().map(|(k, _)| *k).collect();
    out.insert("members".into(), dump(&reports::members_list(&journal, &keys, &mut go).unwrap()));
    let rows = arrears::arrears(&journal, Some("2027-01-15")).unwrap();
    out.insert("arrears".into(), dump(&reports::arrears_report(&rows, "2027-01-15").unwrap()));
    std::fs::write(&args[2], serde_json::to_string(&Json::Object(out)).unwrap()).unwrap();
}
