//! Backups beside a journal: the dated copies taken on open, the named safety
//! copies taken before an irreversible change, and restoring from either.
//!
//! A restore copies a backup *into the open connection* through SQLite's
//! backup API rather than replacing the file: overwriting a database that is
//! open fails on Windows, and every repository keeps pointing at a valid
//! connection. The current state is snapshotted first, so restoring the wrong
//! file is itself reversible.

use std::path::{Path, PathBuf};

use rusqlite::{Connection, OpenFlags};

use super::Journal;
use crate::error::{Error, OLDEST_SCHEMA_VERSION, Result, SCHEMA_VERSION};

/// What a candidate backup holds, read without modifying it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct BackupInfo {
    pub path: PathBuf,
    pub ok: bool,
    pub problem: Option<String>,
    /// Rows in the main tables, for the restore screen.
    pub counts: Vec<(String, i64)>,
    pub schema_version: Option<i64>,
}

/// One file in the backups folder.
#[derive(Debug, Clone, PartialEq)]
pub struct BackupFile {
    pub path: PathBuf,
    pub name: String,
    pub size: u64,
    pub modified: std::time::SystemTime,
}

impl Journal {
    fn backups_dir(&self) -> Option<PathBuf> {
        let path = self.path()?;
        Some(path.parent().unwrap_or(Path::new(".")).join("backups"))
    }

    /// A named safety copy before an irreversible change. Never reuses a path:
    /// two snapshots in the same second would otherwise overwrite each other.
    pub fn snapshot(&self, tag: &str) -> Result<Option<PathBuf>> {
        let (Some(dir), Some(path)) = (self.backups_dir(), self.path()) else { return Ok(None) };
        std::fs::create_dir_all(&dir)?;
        let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("journal");
        let safe: String = tag
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '-' })
            .take(60)
            .collect();
        let base = format!("{stem}.pre-{safe}-{}", chrono::Local::now().format("%Y%m%d-%H%M%S"));
        let mut dest = dir.join(format!("{base}.db"));
        let mut suffix = 1;
        while dest.exists() {
            dest = dir.join(format!("{base}-{suffix}.db"));
            suffix += 1;
        }
        self.backup_to(&dest)?;
        Ok(Some(dest))
    }

    /// Every backup beside this journal, newest first.
    pub fn available_backups(&self) -> Vec<BackupFile> {
        let Some(dir) = self.backups_dir() else { return Vec::new() };
        let Ok(entries) = std::fs::read_dir(dir) else { return Vec::new() };
        let mut found: Vec<BackupFile> = entries
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                let name = e.file_name().into_string().ok()?;
                if !name.ends_with(".db") {
                    return None;
                }
                let meta = e.metadata().ok()?;
                Some(BackupFile { path: e.path(), name, size: meta.len(), modified: meta.modified().ok()? })
            })
            .collect();
        found.sort_by_key(|b| std::cmp::Reverse(b.modified));
        found
    }

    /// Replace this journal's contents with a backup's. The backup must be a
    /// readable, undamaged journal this version opens; one from before 2.0's
    /// additions is given them, as opening it would. Returns the safety copy
    /// of what was here before.
    pub fn restore_from(&mut self, path: &Path) -> Result<Option<PathBuf>> {
        let info = inspect_backup(path);
        if !info.ok {
            return Err(Error::Invalid(info.problem.unwrap_or_else(|| "The backup cannot be used.".into())));
        }
        let safety = self.snapshot("pre-restore")?;
        let source = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        {
            let backup = rusqlite::backup::Backup::new(&source, &mut self.conn)?;
            backup.run_to_completion(256, std::time::Duration::from_millis(0), None)?;
        }
        self.ensure_schema()?;
        log::warn!("Journal restored from {} (safety copy: {safety:?})", path.display());
        Ok(safety)
    }
}

/// Describe a candidate backup without touching it (opened read-only).
pub fn inspect_backup(path: &Path) -> BackupInfo {
    let mut info = BackupInfo { path: path.to_path_buf(), ..BackupInfo::default() };
    if !path.exists() {
        info.problem = Some("The file does not exist.".into());
        return info;
    }
    let conn = match Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY) {
        Ok(conn) => conn,
        Err(e) => {
            info.problem = Some(format!("Cannot open the file: {e}"));
            return info;
        }
    };
    let outcome = (|| -> rusqlite::Result<Option<String>> {
        let check: String = conn.query_row("PRAGMA quick_check", [], |r| r.get(0))?;
        if check != "ok" {
            return Ok(Some(format!("The file is damaged: {check}")));
        }
        let mut stmt = conn.prepare("SELECT name FROM sqlite_master WHERE type='table'")?;
        let tables: Vec<String> = stmt.query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
        let missing: Vec<&str> =
            ["individuals", "loans", "ledger"].into_iter().filter(|t| !tables.iter().any(|x| x == t)).collect();
        if !missing.is_empty() {
            return Ok(Some(format!(
                "This does not look like a LoanMaster journal (missing {}).",
                missing.join(", ")
            )));
        }
        for table in ["individuals", "loans", "ledger", "savings"] {
            if tables.iter().any(|t| t == table) {
                let n: i64 = conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0))?;
                info.counts.push((table.to_string(), n));
            }
        }
        let version: i64 = if tables.iter().any(|t| t == "schema_migrations") {
            conn.query_row("SELECT COALESCE(MAX(version), 0) FROM schema_migrations", [], |r| r.get(0))?
        } else {
            0
        };
        info.schema_version = Some(version);
        if version > SCHEMA_VERSION {
            return Ok(Some(format!(
                "This backup was written by a newer LoanMaster (schema version {version}); this one reads up \
                 to version {SCHEMA_VERSION}."
            )));
        }
        if version < OLDEST_SCHEMA_VERSION {
            return Ok(Some(format!(
                "This backup is on schema version {version}; only backups from version {OLDEST_SCHEMA_VERSION} \
                 on can be restored here. Open it once in LoanMaster 1.x to bring it up to date."
            )));
        }
        Ok(None)
    })();
    match outcome {
        Ok(None) => info.ok = true,
        Ok(Some(problem)) => info.problem = Some(problem),
        Err(e) => info.problem = Some(format!("Could not read the file: {e}")),
    }
    info
}

#[cfg(test)]
mod tests {
    use super::super::OpenOptions;
    use super::*;

    #[test]
    fn a_restore_brings_back_the_backup_and_keeps_a_safety_copy() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("j.db");
        let mut journal = Journal::open(&path, OpenOptions::default()).unwrap();
        journal.conn().execute("INSERT INTO individuals (name) VALUES ('Before')", []).unwrap();
        let backup = journal.snapshot("test").unwrap().unwrap();
        journal.conn().execute("INSERT INTO individuals (name) VALUES ('After')", []).unwrap();

        let info = inspect_backup(&backup);
        assert!(info.ok, "{info:?}");
        assert_eq!(info.counts[0], ("individuals".to_string(), 1));

        let safety = journal.restore_from(&backup).unwrap().unwrap();
        let names: Vec<String> = journal
            .conn()
            .prepare("SELECT name FROM individuals")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect();
        assert_eq!(names, ["Before"]);
        assert!(inspect_backup(&safety).counts.contains(&("individuals".into(), 2)));
        assert!(journal.available_backups().len() >= 2);
    }

    #[test]
    fn a_backup_from_before_the_additions_is_restored_and_brought_up_to_date() {
        let dir = tempfile::tempdir().unwrap();
        let mut journal = Journal::open(dir.path().join("j.db"), OpenOptions::default()).unwrap();
        let old = dir.path().join("old.db");
        {
            let conn = Connection::open(&old).unwrap();
            conn.execute_batch(super::super::SCHEMA).unwrap();
            conn.execute("INSERT INTO individuals (name) VALUES ('Then')", []).unwrap();
        }
        assert_eq!(inspect_backup(&old).schema_version, Some(12));
        journal.restore_from(&old).unwrap();
        let version: i64 = journal.conn().query_row("SELECT MAX(version) FROM schema_migrations", [], |r| r.get(0)).unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        let spans: i64 = journal.conn().query_row("SELECT COUNT(*) FROM savings_suspensions", [], |r| r.get(0)).unwrap();
        assert_eq!(spans, 0);
    }

    #[test]
    fn a_file_that_is_not_a_journal_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("other.db");
        Connection::open(&path).unwrap().execute_batch("CREATE TABLE x (a);").unwrap();
        let info = inspect_backup(&path);
        assert!(!info.ok && info.problem.unwrap().contains("missing"));
    }
}
