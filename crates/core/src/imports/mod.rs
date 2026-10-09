//! Bringing members and their history in from elsewhere: another LoanMaster
//! journal (here), or a spreadsheet ([`excel`]).
//!
//! An import from a journal copies the chosen members — and, as asked, their
//! loans and ledger, savings, Christmas and benevolent funds and suspensions.
//! A member whose name is already on record exactly is merged into; one who
//! only resembles somebody is a conflict the user decides. Every row is
//! normalised by [`rules`]; what cannot be read is refused and reported.
//!
//! The whole import is one transaction, after a safety copy: it lands
//! completely or not at all. Everything it creates is recorded against the
//! run, so [`undo`] can take it all out again — including the fund rows and
//! suspensions it adds to members who were merged into, which the schema has
//! no `import_id` column for, and so are listed in the run's details.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};

use rusqlite::types::Value as Sql;
use rusqlite::{Connection, OpenFlags, OptionalExtension, params, params_from_iter};
use serde_json::{Value as Json, json};

use self::rules::{SourceRow, normalise_date, text};
use crate::Journal;
use crate::dates;
use crate::db::model::Fund;
use crate::error::{Error, Result};
use crate::loans::projection::project_member;
use crate::loans::service::recalculate_default_deduction;

pub mod excel;
pub mod names;
pub mod rules;

/// A member in the journal being imported from.
#[derive(Debug, Clone, PartialEq)]
pub struct SourceMember {
    pub id: i64,
    pub name: String,
    pub phone: String,
    pub email: String,
}

/// Another journal, opened read-only. Only these tables are ever read.
#[derive(Debug)]
pub struct Source {
    conn: Connection,
    pub path: PathBuf,
}

const TABLES: [&str; 8] =
    ["individuals", "loans", "ledger", "savings", "christmas_savings", "benevolent_ledger", "benevolent_accounts", "loan_suspensions"];

fn rows_of(stmt: &mut rusqlite::Statement<'_>, params: impl rusqlite::Params) -> Result<Vec<SourceRow>> {
    let names: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
    let rows = stmt.query_map(params, |r| {
        let mut row = SourceRow::new();
        for (i, name) in names.iter().enumerate() {
            row.insert(name.clone(), r.get::<_, Sql>(i)?);
        }
        Ok(row)
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

fn id_of(row: &SourceRow, key: &str) -> Option<i64> {
    match row.get(key) {
        Some(Sql::Integer(i)) => Some(*i),
        Some(Sql::Real(r)) if r.fract() == 0.0 => Some(*r as i64),
        Some(Sql::Text(t)) => t.trim().parse().ok(),
        _ => None,
    }
}

impl Source {
    /// Open a journal to import from. It must be a readable SQLite file with
    /// an `individuals` table that has a `name` column.
    pub fn open(path: &Path) -> Result<Source> {
        if !path.exists() {
            return Err(Error::invalid("File does not exist."));
        }
        if std::fs::metadata(path)?.len() == 0 {
            return Err(Error::invalid("File is empty."));
        }
        let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
        let source = Source { conn, path: path.to_path_buf() };
        if source.conn.query_row("SELECT count(*) FROM sqlite_master", [], |r| r.get::<_, i64>(0)).is_err() {
            return Err(Error::invalid("File is not a valid SQLite database or is encrypted."));
        }
        if !source.has_table("individuals") {
            return Err(Error::invalid("Missing required table: 'individuals'."));
        }
        if !source.columns("individuals")?.iter().any(|c| c == "name") {
            return Err(Error::invalid("Table 'individuals' missing required column: 'name'."));
        }
        Ok(source)
    }

    fn has_table(&self, table: &str) -> bool {
        TABLES.contains(&table)
            && self
                .conn
                .query_row("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [table], |_| Ok(()))
                .optional()
                .ok()
                .flatten()
                .is_some()
    }

    fn columns(&self, table: &str) -> Result<Vec<String>> {
        let mut stmt = self.conn.prepare(&format!("PRAGMA table_info({table})"))?;
        Ok(stmt.query_map([], |r| r.get::<_, String>(1))?.collect::<rusqlite::Result<_>>()?)
    }

    /// Every member, by name, for choosing whom to import.
    pub fn members(&self) -> Result<Vec<SourceMember>> {
        let columns = self.columns("individuals")?;
        let pick = |c: &str| if columns.iter().any(|x| x == c) { c.to_string() } else { "''".into() };
        let mut stmt = self
            .conn
            .prepare(&format!("SELECT id, name, {}, {} FROM individuals ORDER BY name", pick("phone"), pick("email")))?;
        let rows = stmt.query_map([], |r| {
            Ok(SourceMember {
                id: r.get(0)?,
                name: r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                phone: r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                email: r.get::<_, Option<String>>(3)?.unwrap_or_default(),
            })
        })?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    /// Rows of `table` for these source members, optionally within dates.
    fn rows_for(&self, table: &str, members: &[i64], dates: Option<&(String, String)>) -> Result<Vec<SourceRow>> {
        if members.is_empty() || !self.has_table(table) {
            return Ok(Vec::new());
        }
        let marks = vec!["?"; members.len()].join(",");
        let mut sql = format!("SELECT * FROM {table} WHERE individual_id IN ({marks})");
        let mut values: Vec<Sql> = members.iter().map(|m| Sql::Integer(*m)).collect();
        if let Some((from, to)) = dates {
            sql.push_str(" AND date >= ? AND date <= ?");
            values.push(Sql::Text(from.clone()));
            values.push(Sql::Text(to.clone()));
        }
        let mut stmt = self.conn.prepare(&format!("{sql} ORDER BY id"))?;
        rows_of(&mut stmt, params_from_iter(values))
    }

    /// The earliest well-formed ledger date of one loan: the start date of a
    /// loan that records none.
    fn first_ledger_date(&self, member: i64, reference: &str) -> Option<String> {
        if !self.has_table("ledger") {
            return None;
        }
        self.conn
            .query_row(
                "SELECT MIN(date) FROM ledger WHERE individual_id=? AND loan_id=? \
                 AND date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'",
                params![member, reference],
                |r| r.get::<_, Option<String>>(0),
            )
            .ok()
            .flatten()
    }
}

/// What to bring across besides the members themselves.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Options {
    /// Loans, their ledger and their suspensions.
    pub loans: bool,
    pub savings: bool,
    /// Christmas and benevolent funds, and benevolent enrolments.
    pub funds: bool,
    /// Only transactions dated within this range (inclusive).
    pub dates: Option<(String, String)>,
}

/// What to do with a member who resembles somebody on record.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    New,
    Skip,
    Merge(i64),
}

/// A member on record whom an incoming member resembles.
#[derive(Debug, Clone, PartialEq)]
pub struct Match {
    pub id: i64,
    pub name: String,
    pub phone: String,
    pub email: String,
    pub reasons: Vec<&'static str>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Conflict {
    pub source: SourceMember,
    pub matches: Vec<Match>,
}

impl Conflict {
    /// The suggestion: merge when there is exactly one candidate.
    pub fn suggested(&self) -> Decision {
        match self.matches.as_slice() {
            [only] => Decision::Merge(only.id),
            _ => Decision::New,
        }
    }
}

/// Members on record who share a name (ignoring case), a phone number or an
/// email address (ignoring case) with `member`.
fn matches_for(journal: &Journal, member: &SourceMember) -> Result<Vec<Match>> {
    let mut found: Vec<Match> = Vec::new();
    let mut probe = |sql: &str, value: &str, reason: &'static str| -> Result<()> {
        if value.trim().is_empty() {
            return Ok(());
        }
        let mut stmt = journal.conn().prepare(sql)?;
        let rows = stmt.query_map([value.trim()], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<String>>(2)?, r.get::<_, Option<String>>(3)?))
        })?;
        for row in rows {
            let (id, name, phone, email) = row?;
            match found.iter_mut().find(|m| m.id == id) {
                Some(m) => m.reasons.push(reason),
                None => found.push(Match { id, name, phone: phone.unwrap_or_default(), email: email.unwrap_or_default(), reasons: vec![reason] }),
            }
        }
        Ok(())
    };
    probe("SELECT id, name, phone, email FROM individuals WHERE lower(trim(name)) = lower(?)", &member.name, "Name")?;
    probe("SELECT id, name, phone, email FROM individuals WHERE phone = ?", &member.phone, "Phone")?;
    probe("SELECT id, name, phone, email FROM individuals WHERE lower(trim(email)) = lower(?)", &member.email, "Email")?;
    Ok(found)
}

/// How each chosen member will be imported: merged into somebody on record
/// whose name matches exactly, a conflict for the user, or new.
/// Each member with what will happen to them, and the conflicts among them.
type Planned = (Vec<(SourceMember, Decision)>, Vec<Conflict>);

fn plan_members(journal: &Journal, members: &[SourceMember], decisions: &HashMap<i64, Decision>) -> Result<Planned> {
    let mut planned = Vec::new();
    let mut conflicts = Vec::new();
    for member in members {
        let matches = matches_for(journal, member)?;
        let decision = match (decisions.get(&member.id), matches.as_slice()) {
            (Some(decision), _) => *decision,
            (None, []) => Decision::New,
            (None, [only]) if only.name == member.name => Decision::Merge(only.id),
            (None, _) => {
                let conflict = Conflict { source: member.clone(), matches };
                planned.push((member.clone(), conflict.suggested()));
                conflicts.push(conflict);
                continue;
            }
        };
        planned.push((member.clone(), decision));
    }
    Ok((planned, conflicts))
}

/// What an import will do, before it does it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Preview {
    pub new: Vec<String>,
    /// `(incoming name, member merged into)`.
    pub merged: Vec<(String, String)>,
    pub skipped: Vec<String>,
    /// Members needing a decision; until one is made, the suggestion stands.
    pub conflicts: Vec<Conflict>,
    pub loans: usize,
    /// `(member, reference in the source, reference it will take)`.
    pub renamed: Vec<(String, String, String)>,
    pub ledger: usize,
    pub savings: usize,
    pub fund_rows: usize,
}

fn selected<'a>(all: &'a [SourceMember], ids: &[i64]) -> Vec<&'a SourceMember> {
    all.iter().filter(|m| ids.contains(&m.id)).collect()
}

/// A loan reference the member does not hold yet: the source's own when it
/// is free, else the next `L-NNN`.
fn free_reference(held: &mut HashSet<String>, wanted: &str) -> String {
    if held.insert(wanted.to_string()) {
        return wanted.to_string();
    }
    let mut n = held.iter().filter_map(|r| r.split('-').nth(1)?.parse::<u32>().ok()).max().unwrap_or(0) + 1;
    loop {
        let candidate = format!("L-{n:03}");
        if held.insert(candidate.clone()) {
            return candidate;
        }
        n += 1;
    }
}

fn held_references(journal: &Journal, member: i64) -> Result<HashSet<String>> {
    Ok(journal.loans().for_member(member)?.into_iter().map(|l| l.reference).collect())
}

/// What importing `ids` would do, given the decisions made so far.
pub fn preview(journal: &Journal, source: &Source, ids: &[i64], options: &Options, decisions: &HashMap<i64, Decision>) -> Result<Preview> {
    let all = source.members()?;
    let chosen: Vec<SourceMember> = selected(&all, ids).into_iter().cloned().collect();
    let (planned, conflicts) = plan_members(journal, &chosen, decisions)?;
    let mut preview = Preview { conflicts, ..Preview::default() };
    let mut moving = Vec::new();
    for (member, decision) in &planned {
        match decision {
            Decision::Skip => {
                preview.skipped.push(member.name.clone());
                continue;
            }
            Decision::New => preview.new.push(member.name.clone()),
            Decision::Merge(id) => {
                let into = journal.members().name_of(*id)?.unwrap_or_default();
                preview.merged.push((member.name.clone(), into));
            }
        }
        moving.push(member.id);
    }
    if options.loans {
        let loans = source.rows_for("loans", &moving, None)?;
        preview.loans = loans.len();
        for (member, decision) in &planned {
            let Decision::Merge(into) = decision else { continue };
            let mut held = held_references(journal, *into)?;
            for loan in loans.iter().filter(|l| id_of(l, "individual_id") == Some(member.id)) {
                let wanted = text(loan, "ref").trim().to_string();
                let taken = free_reference(&mut held, &wanted);
                if taken != wanted {
                    preview.renamed.push((member.name.clone(), wanted, taken));
                }
            }
        }
        preview.ledger = source.rows_for("ledger", &moving, options.dates.as_ref())?.len();
    }
    if options.savings {
        preview.savings = source.rows_for("savings", &moving, options.dates.as_ref())?.len();
    }
    if options.funds {
        for table in ["christmas_savings", "benevolent_ledger"] {
            preview.fund_rows += source.rows_for(table, &moving, options.dates.as_ref())?.len();
        }
    }
    Ok(preview)
}

/// What an import did.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Outcome {
    pub import_id: i64,
    pub members: usize,
    pub merged: usize,
    pub loans: usize,
    pub ledger: usize,
    pub savings: usize,
    pub christmas: usize,
    pub benevolent: usize,
    pub enrolments: usize,
    pub suspensions: usize,
    /// Rows refused by the normalisation rules, each with its reason among
    /// the warnings.
    pub rejected: usize,
    pub warnings: Vec<String>,
    pub backup: Option<PathBuf>,
}

impl Outcome {
    pub fn summary(&self) -> String {
        let mut parts = vec![format!("{} new member(s)", self.members)];
        for (n, what) in [
            (self.merged, "merged"),
            (self.loans, "loan(s)"),
            (self.ledger, "ledger entries"),
            (self.savings, "savings entries"),
            (self.christmas, "Christmas entries"),
            (self.benevolent, "benevolent entries"),
            (self.suspensions, "suspension(s)"),
            (self.rejected, "refused"),
        ] {
            if n > 0 {
                parts.push(format!("{n} {what}"));
            }
        }
        parts.join(", ")
    }
}

/// Rows the import added to members it merged into: no `import_id` column
/// marks them, so the run's details list them for [`undo`].
#[derive(Debug, Default)]
struct Added {
    rows: HashMap<&'static str, Vec<i64>>,
}

impl Added {
    fn note(&mut self, table: &'static str, id: i64) {
        self.rows.entry(table).or_default().push(id);
    }
}

/// Import the chosen members. `decisions` settles conflicts; a member
/// without one is merged when exactly one member on record has their name
/// as written, and is otherwise new. `progress` may stop the import, which
/// then leaves nothing behind.
pub fn run(
    journal: &Journal,
    source: &Source,
    ids: &[i64],
    options: &Options,
    decisions: &HashMap<i64, Decision>,
    progress: &mut dyn FnMut(usize, usize, &str) -> bool,
) -> Result<Outcome> {
    let all = source.members()?;
    let chosen: Vec<SourceMember> = selected(&all, ids).into_iter().cloned().collect();
    if chosen.is_empty() {
        return Ok(Outcome::default());
    }
    let (planned, _) = plan_members(journal, &chosen, decisions)?;
    let mut out = Outcome { backup: journal.snapshot("pre-import")?, ..Outcome::default() };
    let mut step = 0usize;
    let total = planned.len();
    let mut tick = |what: &str| -> Result<()> {
        step += 1;
        if progress(step.min(total), total, what) { Ok(()) } else { Err(Error::Cancelled) }
    };

    journal.transaction(|| {
        let conn = journal.conn();
        let import_id = journal.imports().start(&dates::now_stamp(), &source.path.display().to_string())?;
        out.import_id = import_id;
        let mut added = Added::default();

        // ---- members
        let mut pf_used: HashSet<String> = column_values(conn, "pf_no")?;
        let mut id_used: HashSet<String> = column_values(conn, "id_no")?;
        let mut member_map: HashMap<i64, i64> = HashMap::new();
        let mut merged: HashSet<i64> = HashSet::new();
        let source_rows: HashMap<i64, SourceRow> = {
            let marks = vec!["?"; chosen.len()].join(",");
            let mut stmt = source.conn.prepare(&format!("SELECT * FROM individuals WHERE id IN ({marks})"))?;
            rows_of(&mut stmt, params_from_iter(chosen.iter().map(|m| m.id)))?.into_iter().filter_map(|r| Some((id_of(&r, "id")?, r))).collect()
        };
        for (member, decision) in &planned {
            tick(&format!("Importing {}", member.name))?;
            match decision {
                Decision::Skip => {}
                Decision::Merge(into) => {
                    member_map.insert(member.id, *into);
                    merged.insert(*into);
                    out.merged += 1;
                }
                Decision::New => {
                    let Some(row) = source_rows.get(&member.id) else { continue };
                    let read = rules::normalise_member(row);
                    out.warnings.extend(read.warnings);
                    let Some(mut v) = read.values else {
                        out.warnings.push(format!("Skipped a member: {}", read.reason.unwrap_or_default()));
                        out.rejected += 1;
                        continue;
                    };
                    if !v.pf_no.is_empty() && !pf_used.insert(v.pf_no.clone()) {
                        out.warnings.push(format!("PF '{}' ({}) already in use — imported without PF No.", v.pf_no, v.name));
                        v.pf_no.clear();
                    }
                    if !v.id_no.is_empty() && !id_used.insert(v.id_no.clone()) {
                        out.warnings.push(format!("ID '{}' ({}) already in use — imported without ID No.", v.id_no, v.name));
                        v.id_no.clear();
                    }
                    conn.execute(
                        "INSERT INTO individuals (name, phone, email, default_deduction, created_at, employment_status, \
                         pf_no, id_no, is_retired, retired_date, import_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        params![
                            v.name, v.phone, v.email, v.default_deduction, dates::now_stamp(), v.employment_status,
                            v.pf_no, v.id_no, v.is_retired, v.retired_date, import_id
                        ],
                    )?;
                    member_map.insert(member.id, conn.last_insert_rowid());
                    out.members += 1;
                }
            }
        }
        let moving: Vec<i64> = member_map.keys().copied().collect();

        // ---- loans and their ledger
        let mut loan_map: HashMap<(i64, String), String> = HashMap::new();
        if options.loans {
            let mut held: HashMap<i64, HashSet<String>> = HashMap::new();
            for loan in source.rows_for("loans", &moving, None)? {
                let Some(source_member) = id_of(&loan, "individual_id") else { continue };
                let member = member_map[&source_member];
                let wanted = text(&loan, "ref").trim().to_string();
                let read = rules::normalise_loan(&loan, source.first_ledger_date(source_member, &wanted).as_deref());
                out.warnings.extend(read.warnings);
                let Some(v) = read.values else {
                    out.warnings.push(format!("Skipped a loan: {}", read.reason.unwrap_or_default()));
                    out.rejected += 1;
                    continue;
                };
                let refs = match held.entry(member) {
                    std::collections::hash_map::Entry::Occupied(e) => e.into_mut(),
                    std::collections::hash_map::Entry::Vacant(e) => e.insert(held_references(journal, member)?),
                };
                let reference = free_reference(refs, &v.reference);
                if reference != v.reference {
                    let name = journal.members().name_of(member)?.unwrap_or_default();
                    out.warnings.push(format!("{name} already holds {}; the imported loan is {reference}.", v.reference));
                }
                conn.execute(
                    "INSERT INTO loans (individual_id, ref, principal, total_amount, balance, installment, start_date, \
                     next_due_date, status, monthly_interest, unearned_interest, interest_balance, import_id, \
                     is_suspended, suspend_until, duration_months, interest_rate) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    params![
                        member, reference, v.principal, v.total_amount, v.balance, v.installment, v.start_date,
                        v.next_due_date, v.status, v.monthly_interest, v.unearned_interest, v.interest_balance,
                        import_id, v.is_suspended, v.suspend_until, v.duration_months, v.interest_rate
                    ],
                )?;
                loan_map.insert((source_member, wanted), reference);
                out.loans += 1;
            }
            let mut orphans = 0;
            for entry in source.rows_for("ledger", &moving, options.dates.as_ref())? {
                let Some(source_member) = id_of(&entry, "individual_id") else { continue };
                let read = rules::normalise_ledger(&entry);
                out.warnings.extend(read.warnings);
                let Some(v) = read.values else {
                    out.warnings.push(format!("Skipped a transaction: {}", read.reason.unwrap_or_default()));
                    out.rejected += 1;
                    continue;
                };
                // A row whose loan was not brought across would belong to nothing.
                let Some(reference) = loan_map.get(&(source_member, v.loan_ref.clone())) else {
                    orphans += 1;
                    continue;
                };
                conn.execute(
                    "INSERT INTO ledger (individual_id, date, event_type, loan_id, added, deducted, balance, notes, \
                     installment_amount, batch_id, interest_amount, principal_balance, interest_balance, \
                     principal_portion, interest_portion, previous_state, is_edited, import_id) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    params![
                        member_map[&source_member], v.date, v.event_type, reference, v.added, v.deducted, v.balance,
                        v.notes, v.installment_amount, v.batch_id, v.interest_amount, v.principal_balance,
                        v.interest_balance, v.principal_portion, v.interest_portion, v.previous_state, v.is_edited,
                        import_id
                    ],
                )?;
                out.ledger += 1;
            }
            if orphans > 0 {
                out.warnings.push(format!("{orphans} ledger entries were not imported: their loan was not."));
                out.rejected += orphans;
            }
            for span in source.rows_for("loan_suspensions", &moving, None)? {
                let Some(source_member) = id_of(&span, "individual_id") else { continue };
                let member = member_map[&source_member];
                let read = rules::normalise_suspension(&span);
                let Some(v) = read.values else {
                    out.warnings.push(format!("Skipped a suspension: {}", read.reason.unwrap_or_default()));
                    out.rejected += 1;
                    continue;
                };
                let Some(reference) = loan_map.get(&(source_member, v.loan_ref.clone())) else { continue };
                let loan_id: Option<i64> = conn
                    .query_row("SELECT id FROM loans WHERE individual_id=? AND ref=?", params![member, reference], |r| r.get(0))
                    .optional()?;
                conn.execute(
                    "INSERT INTO loan_suspensions (loan_id, individual_id, loan_ref, start_date, suspend_until, \
                     resumed_date, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
                    params![loan_id, member, reference, v.start_date, v.suspend_until, v.resumed_date, v.status, v.created_at],
                )?;
                if merged.contains(&member) {
                    added.note("loan_suspensions", conn.last_insert_rowid());
                }
                out.suspensions += 1;
            }
        }

        // ---- savings and the funds
        let mut credited: BTreeSet<(Fund, i64)> = BTreeSet::new();
        let mut copy_fund = |fund: Fund, vocabulary: &[&str], synonyms: &[(&str, &str)], label: &str, out: &mut Outcome, added: &mut Added| -> Result<usize> {
            let mut n = 0;
            for row in source.rows_for(fund.table(), &moving, options.dates.as_ref())? {
                let Some(source_member) = id_of(&row, "individual_id") else { continue };
                let member = member_map[&source_member];
                let read = rules::normalise_fund_row(&row, vocabulary, synonyms, label);
                out.warnings.extend(read.warnings);
                let Some(v) = read.values else {
                    out.warnings.push(format!("Skipped a {label} row: {}", read.reason.unwrap_or_default()));
                    out.rejected += 1;
                    continue;
                };
                if fund == Fund::Savings {
                    conn.execute(
                        "INSERT INTO savings (individual_id, date, transaction_type, amount, balance, notes, import_id) \
                         VALUES (?, ?, ?, ?, 0, ?, ?)",
                        params![member, v.date, v.transaction_type, v.amount, v.notes, import_id],
                    )?;
                } else {
                    conn.execute(
                        &format!(
                            "INSERT INTO {} (individual_id, date, transaction_type, amount, balance, notes, batch_id) \
                             VALUES (?, ?, ?, ?, 0, ?, ?)",
                            fund.table()
                        ),
                        params![member, v.date, v.transaction_type, v.amount, v.notes, v.batch_id],
                    )?;
                    if merged.contains(&member) {
                        added.note(fund.table(), conn.last_insert_rowid());
                    }
                }
                credited.insert((fund, member));
                n += 1;
            }
            Ok(n)
        };
        if options.savings {
            out.savings = copy_fund(Fund::Savings, &rules::SAVINGS_TYPES, &rules::SAVINGS_SYNONYMS, "savings", &mut out, &mut added)?;
        }
        if options.funds {
            out.christmas = copy_fund(Fund::Christmas, &rules::SAVINGS_TYPES, &rules::SAVINGS_SYNONYMS, "Christmas", &mut out, &mut added)?;
            out.benevolent =
                copy_fund(Fund::Benevolent, &rules::BENEVOLENT_TYPES, &rules::BENEVOLENT_SYNONYMS, "benevolent", &mut out, &mut added)?;
            for account in source.rows_for("benevolent_accounts", &moving, None)? {
                let Some(source_member) = id_of(&account, "individual_id") else { continue };
                let member = member_map[&source_member];
                if journal.funds().enrolment(member)?.is_some() {
                    let name = journal.members().name_of(member)?.unwrap_or_default();
                    out.warnings.push(format!("{name} is already enrolled in the benevolent fund; their enrolment is kept."));
                    continue;
                }
                let monthly = rules::text(&account, "monthly_amount").parse::<f64>().unwrap_or(0.0).max(0.0);
                let active = !matches!(account.get("active"), Some(Sql::Integer(0)));
                conn.execute(
                    "INSERT INTO benevolent_accounts (individual_id, monthly_amount, start_date, next_due_date, active) \
                     VALUES (?, ?, ?, ?, ?)",
                    params![
                        member,
                        monthly,
                        normalise_date(&text(&account, "start_date")),
                        normalise_date(&text(&account, "next_due_date")),
                        active
                    ],
                )?;
                if merged.contains(&member) {
                    added.note("benevolent_accounts", conn.last_insert_rowid());
                }
                out.enrolments += 1;
            }
        }
        for (fund, member) in &credited {
            journal.funds().recalculate(*fund, *member)?;
        }
        if options.loans {
            for member in member_map.values().collect::<BTreeSet<_>>() {
                project_member(journal, *member)?;
                recalculate_default_deduction(journal, *member)?;
            }
        }

        let details = json!({
            "individuals": out.members,
            "merged": out.merged,
            "loans": out.loans,
            "ledger": out.ledger,
            "savings": out.savings,
            "christmas": out.christmas,
            "benevolent": out.benevolent,
            "benevolent_accounts": out.enrolments,
            "suspensions": out.suspensions,
            "rejected": out.rejected,
            "added": added.rows,
        });
        let items = out.members + out.loans + out.ledger + out.savings + out.christmas + out.benevolent + out.suspensions;
        journal.imports().finish(import_id, &details.to_string(), items as i64)?;
        Ok(())
    })?;
    Ok(out)
}

fn column_values(conn: &Connection, column: &str) -> Result<HashSet<String>> {
    let mut stmt = conn.prepare(&format!("SELECT {column} FROM individuals WHERE {column} IS NOT NULL AND {column} != ''"))?;
    Ok(stmt.query_map([], |r| r.get::<_, String>(0))?.collect::<rusqlite::Result<_>>()?)
}

/// Take an import back out: everything it created, and the fund rows,
/// suspensions and enrolments it added to members it merged into. Members
/// who existed before the import stay, with their own records as they were.
pub fn undo(journal: &Journal, import_id: i64) -> Result<Option<PathBuf>> {
    let details: Option<String> = journal
        .conn()
        .query_row("SELECT details FROM import_history WHERE id=?", [import_id], |r| r.get(0))
        .optional()?
        .ok_or_else(|| Error::invalid("That import is no longer in the history."))?;
    let backup = journal.snapshot("pre-undo-import")?;
    let added: HashMap<String, Vec<i64>> = details
        .as_deref()
        .and_then(|d| serde_json::from_str::<Json>(d).ok())
        .and_then(|d| serde_json::from_value(d.get("added").cloned().unwrap_or(Json::Null)).ok())
        .unwrap_or_default();
    journal.transaction(|| {
        let conn = journal.conn();
        // Members who were merged into keep their place; what changes for
        // them is re-derived afterwards.
        let mut touched: BTreeSet<i64> = BTreeSet::new();
        for (table, ids) in &added {
            if !["christmas_savings", "benevolent_ledger", "loan_suspensions", "benevolent_accounts"].contains(&table.as_str()) {
                continue;
            }
            for id in ids {
                if let Some(member) =
                    conn.query_row(&format!("SELECT individual_id FROM {table} WHERE id=?"), [id], |r| r.get::<_, i64>(0)).optional()?
                {
                    touched.insert(member);
                }
                conn.execute(&format!("DELETE FROM {table} WHERE id=?"), [id])?;
            }
        }
        for table in ["savings", "ledger", "loans"] {
            let mut stmt = conn.prepare(&format!(
                "SELECT DISTINCT t.individual_id FROM {table} t JOIN individuals i ON i.id = t.individual_id \
                 WHERE t.import_id=? AND i.import_id IS NOT ?"
            ))?;
            for member in stmt.query_map(params![import_id, import_id], |r| r.get::<_, i64>(0))? {
                touched.insert(member?);
            }
        }
        journal.imports().undo(import_id)?;
        for member in touched {
            if journal.members().get(member)?.is_none() {
                continue;
            }
            for fund in Fund::ALL {
                journal.funds().recalculate(fund, member)?;
            }
            project_member(journal, member)?;
            recalculate_default_deduction(journal, member)?;
        }
        Ok(())
    })?;
    Ok(backup)
}

#[cfg(test)]
mod tests;
