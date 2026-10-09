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
