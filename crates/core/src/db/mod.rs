//! The journal: one SQLite file holding a SACCO's members, loans and funds.
//!
//! [`Journal`] owns the connection and the rules for opening one safely:
//!
//! * **a new journal** is created whole from `schema.sql` — the version-12
//!   schema, byte-identical to what LoanMaster 1.x creates, so a journal can
//!   move between the two;
//! * **an existing journal** is never altered on open. It must already be at
//!   [`SCHEMA_VERSION`]; older ones are refused with the remedy named. (The 1.x
//!   migration chain is not carried forward: existing journals are sanitized
//!   and brought up to date by hand.)
//! * **WAL** only on storage positively identified as local. SQLite's locking
//!   is unreliable on network shares and WAL needs shared memory they do not
//!   provide, so anything else keeps the rollback journal.
//! * **a backup on open**, through the online-backup API (a file copy would
//!   miss whatever sits in the WAL), keeping the last ten dated copies.
//!
//! Repositories ([`Journal::members`], [`Journal::loans`], …) are thin views
//! over the connection. They never commit: the caller owns the transaction,
//! through [`Journal::transaction`] and [`Journal::savepoint`].

pub mod funds;
pub mod history;
pub mod ledger;
pub mod loans;
pub mod members;
pub mod model;
pub mod settings;

use std::cell::Cell;
use std::path::{Path, PathBuf};

use log::{info, warn};
use rusqlite::{Connection, OpenFlags};

use crate::error::{Error, Result, SCHEMA_VERSION};

const SCHEMA: &str = include_str!("../schema.sql");

/// How many dated on-open backups to keep per journal.
pub const BACKUP_KEEP_COUNT: usize = 10;
/// How long a writer waits for a competing writer before giving up.
const BUSY_TIMEOUT_MS: u32 = 30_000;
/// Page cache, negative meaning KiB: 64 MiB keeps the ledger resident.
const CACHE_SIZE_KIB: i64 = -65_536;
/// Cap the WAL after a checkpoint so one large batch cannot leave it inflated.
const WAL_SIZE_LIMIT_BYTES: i64 = 64 * 1024 * 1024;
/// Override the journal-mode decision: `wal`, `delete` or `auto`.
pub const JOURNAL_MODE_ENV: &str = "LOANMASTER_JOURNAL_MODE";

/// Filesystems where SQLite's locking is unreliable, or WAL's shared memory
/// is unavailable.
const NETWORK_FILESYSTEMS: &[&str] = &[
    "nfs", "nfs4", "cifs", "smbfs", "smb2", "smb3", "afs", "ncpfs", "9p", "davfs",
    "fuse.davfs", "fuse.sshfs", "glusterfs", "ceph", "lustre",
];

#[derive(Debug, Clone, Copy, Default)]
pub struct OpenOptions {
    /// Snapshot an existing journal into `backups/` before using it.
    pub backup_on_open: bool,
}

/// What opening a journal found, for the UI to report.
#[derive(Debug, Clone, Default)]
pub struct OpenReport {
    /// The journal was created by this open.
    pub created: bool,
    /// SQLite's `quick_check` passed (true when there was nothing to check).
    pub integrity_ok: bool,
    /// `wal` or `delete`, and why.
    pub journal_mode: String,
    pub journal_mode_reason: String,
    /// Rows that reference a member or loan that no longer exists. Each one is
    /// something to sanitize; enforcement stays on regardless.
    pub orphaned_rows: usize,
    /// Where the on-open backup went, if one was taken.
    pub backup: Option<PathBuf>,
}

pub struct Journal {
    conn: Connection,
    path: Option<PathBuf>,
    report: OpenReport,
    /// Savepoint names must be unique while nested.
    savepoint_depth: Cell<u32>,
}

impl Journal {
    /// Open a journal, creating it if the file is new or empty.
    pub fn open(path: impl AsRef<Path>, options: OpenOptions) -> Result<Journal> {
        let path = path.as_ref().to_path_buf();
        let pre_existing = path.exists() && std::fs::metadata(&path)?.len() > 0;
        let conn = Connection::open_with_flags(
            &path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE
                | OpenFlags::SQLITE_OPEN_URI | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        let mut journal = Journal {
            conn,
            path: Some(path.clone()),
            report: OpenReport::default(),
            savepoint_depth: Cell::new(0),
        };
        journal.report.integrity_ok = true;
        if pre_existing {
            if !journal.looks_like_sqlite() {
                return Err(Error::NotAJournal);
            }
            if options.backup_on_open {
                journal.report.backup = journal.backup_on_open(BACKUP_KEEP_COUNT);
            }
            journal.report.integrity_ok = journal.quick_check();
        }
        journal.configure()?;
        journal.ensure_schema()?;
        journal.enforce_foreign_keys()?;
        Ok(journal)
    }

    /// A private journal in memory, for tests and previews.
    pub fn open_in_memory() -> Result<Journal> {
        let mut journal = Journal {
            conn: Connection::open_in_memory()?,
            path: None,
            report: OpenReport { integrity_ok: true, ..OpenReport::default() },
            savepoint_depth: Cell::new(0),
        };
        journal.configure()?;
        journal.ensure_schema()?;
        journal.enforce_foreign_keys()?;
        Ok(journal)
    }

    pub fn conn(&self) -> &Connection {
        &self.conn
    }

    pub fn members(&self) -> members::Members<'_> {
        members::Members(&self.conn)
    }

    pub fn loans(&self) -> loans::Loans<'_> {
        loans::Loans(&self.conn)
    }

    pub fn ledger(&self) -> ledger::Ledger<'_> {
        ledger::Ledger(&self.conn)
    }

    pub fn funds(&self) -> funds::Funds<'_> {
        funds::Funds(&self.conn)
    }

    pub fn settings(&self) -> settings::Settings<'_> {
        settings::Settings(&self.conn)
    }

    pub fn imports(&self) -> history::Imports<'_> {
        history::Imports(&self.conn)
    }

    pub fn audit(&self) -> history::Audit<'_> {
        history::Audit(&self.conn)
    }

    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }

    pub fn report(&self) -> &OpenReport {
        &self.report
    }

    fn looks_like_sqlite(&self) -> bool {
        self.conn
            .query_row("SELECT COUNT(*) FROM sqlite_master", [], |r| r.get::<_, i64>(0))
            .is_ok()
    }

    // ------------------------------------------------------------------ //
    // Opening safely
    // ------------------------------------------------------------------ //

    fn quick_check(&self) -> bool {
        match self.conn.query_row("PRAGMA quick_check", [], |r| r.get::<_, String>(0)) {
            Ok(result) if result == "ok" => {
                info!("Integrity check passed for {}", self.display_path());
                true
            }
            Ok(result) => {
                log::error!("Integrity check FAILED for {}: {result}", self.display_path());
                false
            }
            Err(e) => {
                log::error!("Integrity check could not run on {}: {e}", self.display_path());
                false
            }
        }
    }

    fn display_path(&self) -> String {
        self.path.as_ref().map(|p| p.display().to_string()).unwrap_or_else(|| ":memory:".into())
    }

    fn configure(&mut self) -> Result<()> {
        self.conn.busy_timeout(std::time::Duration::from_millis(BUSY_TIMEOUT_MS as u64))?;
        let (desired, reason) = self.select_journal_mode();
        let actual: String = self
            .conn
            .query_row(&format!("PRAGMA journal_mode = {desired}"), [], |r| r.get(0))
            .unwrap_or_else(|e| {
                warn!("Could not set journal_mode={desired}: {e}");
                "unknown".into()
            });
        let actual = actual.to_lowercase();
        if desired == "wal" && actual != "wal" {
            warn!("WAL requested ({reason}) but the journal is in {actual:?} mode");
        } else {
            info!("journal_mode={actual} ({reason})");
        }
        self.conn.execute_batch(&format!(
            "PRAGMA synchronous = FULL; PRAGMA cache_size = {CACHE_SIZE_KIB}; \
             PRAGMA temp_store = MEMORY;"
        ))?;
        if actual == "wal" {
            self.conn.execute_batch(&format!("PRAGMA journal_size_limit = {WAL_SIZE_LIMIT_BYTES};"))?;
        }
        self.report.journal_mode = actual;
        self.report.journal_mode_reason = reason;
        Ok(())
    }

    fn select_journal_mode(&self) -> (&'static str, String) {
        let override_ = std::env::var(JOURNAL_MODE_ENV).unwrap_or_default().trim().to_lowercase();
        match override_.as_str() {
            "wal" => return ("wal", format!("explicit {JOURNAL_MODE_ENV}=wal")),
            "delete" => return ("delete", format!("explicit {JOURNAL_MODE_ENV}=delete")),
            "" | "auto" => {}
            other => warn!("Ignoring unrecognised {JOURNAL_MODE_ENV}={other:?}"),
        }
        match self.path.as_deref().map(storage_kind) {
            None => ("delete", "in-memory journal".into()),
            Some(StorageKind::Local) => ("wal", "local storage".into()),
            Some(kind) => ("delete", format!("{kind:?} storage (WAL unsafe)").to_lowercase()),
        }
    }

    fn table_count(&self) -> Result<i64> {
        Ok(self.conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            [],
            |r| r.get(0),
        )?)
    }

    /// Create a new journal whole, or check an existing one is current.
    fn ensure_schema(&mut self) -> Result<()> {
        if self.table_count()? == 0 {
            self.transaction(|| Ok(self.conn.execute_batch(SCHEMA)?))?;
            self.report.created = true;
            info!("Created a new journal at {}", self.display_path());
            return Ok(());
        }
        let has_versions: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations'",
            [],
            |r| r.get(0),
        )?;
        let found: i64 = if has_versions == 1 {
            self.conn.query_row("SELECT COALESCE(MAX(version), 0) FROM schema_migrations", [], |r| {
                r.get(0)
            })?
        } else {
            0
        };
        if found != SCHEMA_VERSION {
            if found == 0 && !self.has_table("individuals")? {
                return Err(Error::NotAJournal);
            }
            return Err(Error::OutdatedSchema {
                found,
                expected: SCHEMA_VERSION,
                app: env!("CARGO_PKG_VERSION"),
            });
        }
        Ok(())
    }

    fn has_table(&self, name: &str) -> Result<bool> {
        Ok(self.conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            [name],
            |r| r.get::<_, i64>(0),
        )? > 0)
    }

    /// Foreign keys are always enforced. Rows that already break them are
    /// counted for the UI — they are what sanitizing a journal removes.
    fn enforce_foreign_keys(&mut self) -> Result<()> {
        let mut stmt = self.conn.prepare("PRAGMA foreign_key_check")?;
        let violations = stmt.query_map([], |_| Ok(()))?.count();
        drop(stmt);
        self.conn.execute_batch("PRAGMA foreign_keys = ON;")?;
        if violations > 0 {
            warn!("{violations} row(s) reference a member or loan that no longer exists");
        }
        self.report.orphaned_rows = violations;
        Ok(())
    }

    // ------------------------------------------------------------------ //
    // Backups
    // ------------------------------------------------------------------ //

    /// Snapshot into `<journal dir>/backups/<stem>.<YYYYmmdd-HHMMSS>.db` and
    /// keep the last `keep` such copies. Never fails the open.
    fn backup_on_open(&self, keep: usize) -> Option<PathBuf> {
        let path = self.path.as_ref()?;
        let result = (|| -> Result<PathBuf> {
            let dir = path.parent().unwrap_or(Path::new(".")).join("backups");
            std::fs::create_dir_all(&dir)?;
            let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("journal").to_string();
            let stamp = chrono::Local::now().format("%Y%m%d-%H%M%S");
            let dest = dir.join(format!("{stem}.{stamp}.db"));
            if !dest.exists() {
                self.backup_to(&dest)?;
            }
            rotate_backups(&dir, &stem, keep)?;
            Ok(dest)
        })();
        match result {
            Ok(dest) => Some(dest),
            Err(e) => {
                log::error!("Automatic backup failed (continuing without it): {e}");
                None
            }
        }
    }

    /// A consistent copy of the journal, through SQLite's online-backup API.
    pub fn backup_to(&self, dest: impl AsRef<Path>) -> Result<()> {
        let mut target = Connection::open(dest.as_ref())?;
        {
            let backup = rusqlite::backup::Backup::new(&self.conn, &mut target)?;
            backup.run_to_completion(256, std::time::Duration::from_millis(0), None)?;
        }
        info!("Journal backed up to {}", dest.as_ref().display());
        Ok(())
    }

    // ------------------------------------------------------------------ //
    // Transactions
    // ------------------------------------------------------------------ //

    pub fn in_transaction(&self) -> bool {
        !self.conn.is_autocommit()
    }

    /// Run `work` in a transaction: `BEGIN IMMEDIATE`, commit on `Ok`, roll
    /// back on `Err`. Re-entrant — inside a transaction already in flight the
    /// work simply joins it, and the outermost caller owns commit/rollback.
    ///
    /// `IMMEDIATE` takes the write lock up front. A deferred `BEGIN` takes a
    /// read lock and upgrades at the first write, and if another connection
    /// wrote in between SQLite returns `SQLITE_BUSY` without waiting — which is
    /// exactly the read-compute-write shape of every operation here.
    pub fn transaction<T>(&self, work: impl FnOnce() -> Result<T>) -> Result<T> {
        if self.in_transaction() {
            return work();
        }
        self.conn.execute_batch("BEGIN IMMEDIATE")?;
        match work() {
            Ok(value) => {
                self.conn.execute_batch("COMMIT")?;
                Ok(value)
            }
            Err(e) => {
                if let Err(rollback) = self.conn.execute_batch("ROLLBACK") {
                    log::error!("Rollback failed after {e}: {rollback}");
                }
                Err(e)
            }
        }
    }

    /// Run `work` so that a failure undoes only what it did. Must be called
    /// inside a transaction; nests freely.
    pub fn savepoint<T>(&self, work: impl FnOnce() -> Result<T>) -> Result<T> {
        let depth = self.savepoint_depth.get() + 1;
        self.savepoint_depth.set(depth);
        let name = format!("lm_sp_{depth}");
        let outcome = (|| {
            self.conn.execute_batch(&format!("SAVEPOINT {name}"))?;
            match work() {
                Ok(value) => {
                    self.conn.execute_batch(&format!("RELEASE {name}"))?;
                    Ok(value)
                }
                Err(e) => {
                    self.conn.execute_batch(&format!("ROLLBACK TO {name}; RELEASE {name}"))?;
                    Err(e)
                }
            }
        })();
        self.savepoint_depth.set(depth - 1);
        outcome
    }

    /// Refresh the planner's statistics and close.
    pub fn close(self) {
        if let Err(e) = self.conn.execute_batch("PRAGMA optimize;") {
            log::debug!("PRAGMA optimize failed on close: {e}");
        }
        if let Err((_, e)) = self.conn.close() {
            log::warn!("Closing the journal failed: {e}");
        }
    }
}

/// Keep the newest `keep` dated copies of `<stem>`. Named safety backups
/// (`<stem>.pre-…db`) are neither counted nor removed: counting them once
/// meant the copy just written was always the first deleted.
pub fn rotate_backups(dir: &Path, stem: &str, keep: usize) -> Result<()> {
    let prefix = format!("{stem}.");
    let mut dated: Vec<PathBuf> = std::fs::read_dir(dir)?
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .and_then(|n| n.strip_prefix(&prefix))
                .is_some_and(is_dated_backup_suffix)
        })
        .collect();
    dated.sort();
    if dated.len() > keep {
        for stale in &dated[..dated.len() - keep] {
            std::fs::remove_file(stale)?;
        }
    }
    Ok(())
}

/// `YYYYmmdd-HHMMSS.db`, exactly.
fn is_dated_backup_suffix(suffix: &str) -> bool {
    let bytes = suffix.as_bytes();
    bytes.len() == 18
        && bytes[..8].iter().all(u8::is_ascii_digit)
        && bytes[8] == b'-'
        && bytes[9..15].iter().all(u8::is_ascii_digit)
        && &suffix[15..] == ".db"
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StorageKind {
    Local,
    Network,
    Unknown,
}

/// Where a file lives. Only a positively identified local disk counts as
/// local; anything unrecognised is treated as unsafe for WAL.
pub fn storage_kind(path: &Path) -> StorageKind {
    let absolute = std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf());
    platform_storage_kind(&absolute)
}

#[cfg(target_os = "linux")]
fn platform_storage_kind(path: &Path) -> StorageKind {
    let Ok(mounts) = std::fs::read_to_string("/proc/mounts") else { return StorageKind::Unknown };
    let path = path.to_string_lossy();
    let mut best: Option<(&str, &str)> = None;
    for line in mounts.lines() {
        let mut parts = line.split_whitespace();
        let (Some(_device), Some(point), Some(fstype)) = (parts.next(), parts.next(), parts.next())
        else {
            continue;
        };
        let under = path == point || path.starts_with(&format!("{}/", point.trim_end_matches('/')));
        if under && best.is_none_or(|(p, _)| point.len() >= p.len()) {
            best = Some((point, fstype));
        }
    }
    match best {
        None => StorageKind::Unknown,
        Some((_, fstype)) if NETWORK_FILESYSTEMS.contains(&fstype.to_lowercase().as_str()) => {
            StorageKind::Network
        }
        Some(_) => StorageKind::Local,
    }
}

#[cfg(windows)]
fn platform_storage_kind(path: &Path) -> StorageKind {
    use std::os::windows::ffi::OsStrExt;
    let text = path.to_string_lossy();
    if text.starts_with(r"\\") && !text.starts_with(r"\\?\") {
        return StorageKind::Network;
    }
    let trimmed = text.trim_start_matches(r"\\?\");
    let Some(drive) = trimmed.get(..2).filter(|d| d.ends_with(':')) else {
        return StorageKind::Unknown;
    };
    let root: Vec<u16> = std::ffi::OsStr::new(&format!("{drive}\\"))
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    // SAFETY: `root` is a valid NUL-terminated wide string for the call.
    let kind = unsafe { windows_sys::Win32::Storage::FileSystem::GetDriveTypeW(root.as_ptr()) };
    match kind {
        4 => StorageKind::Network, // DRIVE_REMOTE
        3 => StorageKind::Local,   // DRIVE_FIXED
        _ => StorageKind::Unknown,
    }
}

#[cfg(not(any(target_os = "linux", windows)))]
fn platform_storage_kind(_path: &Path) -> StorageKind {
    StorageKind::Unknown
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_new_journal_is_created_at_the_current_version() {
        let dir = tempfile::tempdir().unwrap();
        let journal = Journal::open(dir.path().join("j.db"), OpenOptions::default()).unwrap();
        assert!(journal.report().created);
        let version: i64 = journal
            .conn()
            .query_row("SELECT MAX(version) FROM schema_migrations", [], |r| r.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        let theme: String = journal
            .conn()
            .query_row("SELECT value FROM settings WHERE key='app_theme'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(theme, "Light");
    }

    #[test]
    fn an_older_journal_is_refused_not_altered() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("old.db");
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE individuals (id INTEGER PRIMARY KEY, name TEXT);
                 CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT,
                     applied_at TEXT, statements INTEGER);
                 INSERT INTO schema_migrations VALUES (11, 'x', '', 0);",
            )
            .unwrap();
        }
        match Journal::open(&path, OpenOptions::default()) {
            Err(Error::OutdatedSchema { found: 11, .. }) => {}
            other => panic!("expected OutdatedSchema, got {:?}", other.map(|_| ())),
        }
    }

    #[test]
    fn transactions_nest_and_roll_back_together() {
        let journal = Journal::open_in_memory().unwrap();
        let outcome: Result<()> = journal.transaction(|| {
            journal.conn().execute("INSERT INTO individuals (name) VALUES ('A')", [])?;
            journal.transaction(|| {
                journal.conn().execute("INSERT INTO individuals (name) VALUES ('B')", [])?;
                Ok(())
            })?;
            Err(Error::invalid("stop"))
        });
        assert!(outcome.is_err());
        let count: i64 = journal
            .conn()
            .query_row("SELECT COUNT(*) FROM individuals", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn a_savepoint_undoes_only_its_own_work() {
        let journal = Journal::open_in_memory().unwrap();
        journal
            .transaction(|| {
                journal.conn().execute("INSERT INTO individuals (name) VALUES ('Kept')", [])?;
                let _ = journal.savepoint(|| -> Result<()> {
                    journal.conn().execute("INSERT INTO individuals (name) VALUES ('Gone')", [])?;
                    Err(Error::invalid("refused"))
                });
                Ok(())
            })
            .unwrap();
        let names: Vec<String> = journal
            .conn()
            .prepare("SELECT name FROM individuals")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect();
        assert_eq!(names, ["Kept"]);
    }

    #[test]
    fn rotation_keeps_named_backups_and_the_newest_dated_ones() {
        let dir = tempfile::tempdir().unwrap();
        for name in ["j.pre-repair-01.db", "j.pre-delete-loan.db"] {
            std::fs::write(dir.path().join(name), b"x").unwrap();
        }
        for day in 1..=5 {
            std::fs::write(dir.path().join(format!("j.2026010{day}-120000.db")), b"x").unwrap();
        }
        rotate_backups(dir.path(), "j", 3).unwrap();
        let mut names: Vec<String> = std::fs::read_dir(dir.path())
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        names.sort();
        assert_eq!(
            names,
            [
                "j.20260103-120000.db", "j.20260104-120000.db", "j.20260105-120000.db",
                "j.pre-delete-loan.db", "j.pre-repair-01.db"
            ]
        );
    }

    #[test]
    fn a_backup_on_open_survives_its_own_rotation() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("j.db");
        Journal::open(&path, OpenOptions::default()).unwrap().close();
        std::fs::create_dir_all(dir.path().join("backups")).unwrap();
        for i in 0..12 {
            std::fs::write(dir.path().join(format!("backups/j.pre-repair-{i:02}.db")), b"x").unwrap();
        }
        let journal = Journal::open(&path, OpenOptions { backup_on_open: true }).unwrap();
        let backup = journal.report().backup.clone().expect("a backup was taken");
        assert!(backup.exists() && std::fs::metadata(&backup).unwrap().len() > 0);
    }
}
