//! Apply a JSON script of loan operations to a journal and dump the result,
//! for comparing this implementation with LoanMaster 1.x on the same data.
//!
//! `cargo run --example replay_ops -- journal.db script.json out.json`
use loanmaster_core::grid::loan as loan_grid;
use loanmaster_core::{Journal, OpenOptions, dates, loans};
use serde_json::{Value, json};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let journal = Journal::open(&args[1], OpenOptions::default()).expect("journal");
    let script: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(&args[2]).unwrap()).unwrap();
    let mut outcomes = Vec::new();
    let mut last_batch: Option<loan_grid::BatchResult> = None;
    for step in &script {
        let s = |k: &str| step[k].as_str().unwrap_or_default().to_string();
        let member = step["member"].as_i64().unwrap_or_default();
        let outcome: Result<String, String> = match s("op").as_str() {
            "project_all" => {
                let ids: Vec<i64> = journal.members().summaries().unwrap().iter().map(|m| m.id).collect();
                let mut changed = 0;
                for id in ids {
                    changed += loans::projection::project_member(&journal, id).map_err(|e| e.to_string()).unwrap();
                }
                Ok(changed.to_string())
            }
            "catch_up" => loans::service::catch_up(&journal, member, &s("ref"), Some(&s("target")))
                .map(|n| n.to_string()).map_err(|e| e.to_string()),
            "deduct" => loans::service::deduct(&journal, member, &s("ref")).map(|_| "ok".into()).map_err(|e| e.to_string()),
            "top_up" => loans::service::top_up(&journal, member, &s("ref"), step["amount"].as_f64().unwrap(),
                step["months"].as_i64().unwrap(), Some(&s("date"))).map(|_| "ok".into()).map_err(|e| e.to_string()),
            "buyoff" => loans::service::buyoff(&journal, member, &s("ref"), None)
                .map(|a| format!("{a}")).map_err(|e| e.to_string()),
            "issue" => loans::service::issue(&journal, member, step["amount"].as_f64().unwrap(),
                step["months"].as_i64().unwrap(), &s("date"), Some(step["rate"].as_f64().unwrap()))
                .map(|i| i.reference).map_err(|e| e.to_string()),
            "grid_fill" => {
                let months = dates::month_range(&s("from"), &s("to"));
                let g = loan_grid::load(&journal, &months, true, step["paid"].as_bool().unwrap_or(false), false).unwrap();
                let plan = loan_grid::plan_fill(&g, &g.rows.iter().collect::<Vec<_>>(), &months, Some(&s("today")));
                let result = loan_grid::apply_changes(&journal, &plan.changes).unwrap();
                let summary = format!("{} planned: {}", plan.changes.len(), result.summary());
                last_batch = Some(result);
                Ok(summary)
            }
            "grid_delete" => {
                let months = dates::month_range(&s("from"), &s("to"));
                let g = loan_grid::load(&journal, &months, true, step["paid"].as_bool().unwrap_or(false), false).unwrap();
                let wanted: Vec<String> = step["months"].as_array().unwrap().iter().map(|m| m.as_str().unwrap().to_string()).collect();
                let plan = loan_grid::plan_delete_months(&journal, &g.rows.iter().collect::<Vec<_>>(), &wanted).unwrap();
                let result = loan_grid::apply_changes(&journal, &plan.loan_changes).unwrap();
                let summary = format!("{} planned, {} loans refused: {}", plan.loan_changes.len(), plan.refused.len(), result.summary());
                last_batch = Some(result);
                Ok(summary)
            }
            "grid_stage" => {
                let months = dates::month_range(&s("from"), &s("to"));
                let g = loan_grid::load(&journal, &months, true, true, false).unwrap();
                match g.rows.iter().find(|r| r.member_id == member && r.reference == s("ref")) {
                    None => Err("no such row".into()),
                    Some(row) => {
                        let month = match s("month").as_str() {
                            "due" => row.next_due_month().unwrap_or_default(),
                            "missed" => months.iter().find(|m| row.missed(m)).cloned().unwrap_or_default(),
                            "last" => months.iter().rev().find(|m| row.cell(m).rows.len() == 1).cloned().unwrap_or_default(),
                            other => other.to_string(),
                        };
                        let text = s("text").replace("inst", &row.installment.to_string());
                        match loan_grid::stage(&g, row, &month, &text) {
                            Err(problem) => Err(format!("{month}: {problem}")),
                            Ok(None) => Ok(format!("{month}: nothing")),
                            Ok(Some(change)) => {
                                let result = loan_grid::apply_changes(&journal, &[change]).unwrap();
                                let summary = format!("{month}: {}", result.summary());
                                last_batch = Some(result);
                                Ok(summary)
                            }
                        }
                    }
                }
            }
            "grid_revert_last" => match last_batch.take() {
                Some(result) => loan_grid::revert(&journal, &result).map(|t| format!("{} members", t.len())).map_err(|e| e.to_string()),
                None => Err("nothing to revert".into()),
            },
            other => Err(format!("unknown op {other}")),
        };
        outcomes.push(match outcome { Ok(v) => json!({"ok": v}), Err(e) => json!({"error": e}) });
    }
    let dump = |sql: &str| -> Vec<Vec<Value>> {
        let mut stmt = journal.conn().prepare(sql).unwrap();
        let n = stmt.column_count();
        stmt.query_map([], |r| {
            Ok((0..n).map(|i| match r.get::<_, rusqlite::types::Value>(i).unwrap() {
                rusqlite::types::Value::Null => Value::Null,
                rusqlite::types::Value::Integer(v) => json!(v as f64),
                rusqlite::types::Value::Real(v) => json!((v * 100.0).round() / 100.0),
                rusqlite::types::Value::Text(t) => json!(t),
                rusqlite::types::Value::Blob(_) => Value::Null,
            }).collect())
        }).unwrap().map(|r| r.unwrap()).collect()
    };
    let out = json!({
        "outcomes": outcomes,
        "ledger": dump("SELECT individual_id, date, event_type, loan_id, added, deducted, balance, principal_balance, \
                        interest_balance, principal_portion, interest_portion, installment_amount, interest_amount, notes \
                        FROM ledger ORDER BY individual_id, loan_id, date, event_type, added, deducted"),
        "loans": dump("SELECT individual_id, ref, principal, total_amount, balance, installment, next_due_date, status, \
                       monthly_interest, unearned_interest, interest_balance, is_suspended, suspend_until, duration_months \
                       FROM loans ORDER BY individual_id, ref"),
        "members": dump("SELECT id, default_deduction FROM individuals ORDER BY id"),
    });
    std::fs::write(&args[3], serde_json::to_string(&out).unwrap()).unwrap();
}
