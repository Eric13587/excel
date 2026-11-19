//! Activity rather than entities: import runs and the audit trail.

use rusqlite::{Connection, params};

use super::model::{AuditEntry, ImportRun, collect};
use crate::error::Result;

pub struct Imports<'a>(pub(crate) &'a Connection);

impl Imports<'_> {
    pub fn start(&self, timestamp: &str, source_file: &str) -> Result<i64> {
        self.0.execute(
            "INSERT INTO import_history (timestamp, source_file, details, item_count) \
             VALUES (?, ?, 'Started', 0)",
            params![timestamp, source_file],
        )?;
        Ok(self.0.last_insert_rowid())
    }

    pub fn finish(&self, import_id: i64, details_json: &str, item_count: i64) -> Result<()> {
        self.0.execute(
            "UPDATE import_history SET details=?, item_count=? WHERE id=?",
            params![details_json, item_count, import_id],
        )?;
        Ok(())
    }

    pub fn all(&self) -> Result<Vec<ImportRun>> {
        let mut stmt = self.0.prepare(
            "SELECT id, timestamp, source_file, details, item_count FROM import_history ORDER BY id DESC",
        )?;
        Ok(collect(&mut stmt, [])?)
    }

    /// Remove everything one run *created*, children first. A member who
    /// already existed and was merged into carries no `import_id`, so an undo
    /// never removes them.
    pub fn undo(&self, import_id: i64) -> Result<()> {
        for table in ["savings", "ledger", "loans", "individuals"] {
            self.0.execute(&format!("DELETE FROM {table} WHERE import_id=?"), [import_id])?;
        }
        self.0.execute("DELETE FROM import_history WHERE id=?", [import_id])?;
        Ok(())
    }
}

pub struct Audit<'a>(pub(crate) &'a Connection);

/// An audit before-image as a short line — `date 2026-01-15, deducted
/// 5,750.00` — rather than the JSON the triggers store. Zeros, blanks and the
/// member's id are left out; anything that is not a JSON object is shown as
/// it is.
pub fn describe_before_image(raw: &str) -> String {
    let Ok(serde_json::Value::Object(fields)) = serde_json::from_str::<serde_json::Value>(raw) else {
        return raw.to_string();
    };
    let blank = |value: &serde_json::Value| match value {
        serde_json::Value::Null => true,
        serde_json::Value::Bool(b) => !b,
        serde_json::Value::Number(n) => n.as_f64() == Some(0.0),
        serde_json::Value::String(s) => s.is_empty(),
        _ => false,
    };
    fields
        .iter()
        .filter(|(key, value)| *key != "individual_id" && !blank(value))
        .map(|(key, value)| {
            let shown = match value {
                serde_json::Value::Number(n) if n.is_f64() => crate::money::format_money(n.as_f64().unwrap_or(0.0), 2),
                serde_json::Value::String(s) => s.clone(),
                serde_json::Value::Bool(_) => "True".to_string(),
                other => other.to_string(),
            };
            format!("{} {shown}", key.replace('_', " "))
        })
        .collect::<Vec<_>>()
        .join(", ")
}

/// Entities whose rows carry money: every edit and deletion of these keeps a
/// before-image.
pub const MONEY_ENTITIES: [&str; 4] = ["ledger", "savings", "christmas_savings", "benevolent_ledger"];

impl Audit<'_> {
    /// Entries, most recent first, optionally for one entity.
    pub fn recent(&self, limit: i64, entity: Option<(&str, i64)>) -> Result<Vec<AuditEntry>> {
        let mut stmt = self.0.prepare(
            "SELECT ts, operation, entity, entity_id, summary, before_image FROM audit_log \
             WHERE (?1 IS NULL OR (entity=?1 AND entity_id=?2)) ORDER BY id DESC LIMIT ?3",
        )?;
        let (name, id) = entity.map(|(n, i)| (Some(n), Some(i))).unwrap_or((None, None));
        Ok(collect(&mut stmt, params![name, id, limit])?)
    }

    /// Edits and deletions of rows that carry money — what was changed after
    /// the fact, and what it said before.
    pub fn money_changes(&self, limit: i64, since: Option<&str>) -> Result<Vec<AuditEntry>> {
        let mut stmt = self.0.prepare(
            "SELECT ts, operation, entity, entity_id, summary, before_image FROM audit_log \
             WHERE entity IN ('ledger','savings','christmas_savings','benevolent_ledger') \
             AND (?1 IS NULL OR ts >= ?1) ORDER BY id DESC LIMIT ?2",
        )?;
        Ok(collect(&mut stmt, params![since, limit])?)
    }
}

#[cfg(test)]
mod tests {
    use super::describe_before_image;

    #[test]
    fn a_before_image_reads_as_1x_wrote_it() {
        // Expected lines from 1.x's dashboard._format_before_image.
        let row = r#"{"date":"2026-09-09","event_type":"Repayment","added":0.0,"deducted":16611.0,"principal_portion":14444.0,"interest_portion":2167.0,"interest_amount":0.0,"individual_id":145,"loan_id":"L-003"}"#;
        assert_eq!(
            describe_before_image(row),
            "date 2026-09-09, event type Repayment, deducted 16,611.00, principal portion 14,444.00, interest portion 2,167.00, loan id L-003"
        );
        let odd = r#"{"amount":-1234.5,"count":3,"flag":true,"off":false,"none":null,"note":""}"#;
        assert_eq!(describe_before_image(odd), "amount -1,234.50, count 3, flag True");
        assert_eq!(describe_before_image("not json"), "not json");
    }
}
