use super::*;
use crate::db::members::MemberDetails;
use crate::funds::{self, Posting};
use crate::loans::service::{catch_up, issue};
use crate::loans::suspensions;
use crate::{OpenOptions, db::ledger::NewEntry};

fn member(journal: &Journal, name: &str, pf: &str) -> i64 {
    journal.members().add(&MemberDetails { name: name.into(), phone: format!("07{name}"), pf_no: pf.into(), ..Default::default() }).unwrap()
}

/// A source journal on disk: Ann with a loan, savings, Christmas money, a
/// benevolent enrolment and a past suspension; Ben with his own L-001.
fn source_journal(dir: &Path) -> PathBuf {
    let path = dir.join("source.db");
    let journal = Journal::open(&path, OpenOptions::default()).unwrap();
    let ann = member(&journal, "Ann", "PF1");
    let ben = member(&journal, "Ben", "PF2");
    for id in [ann, ben] {
        issue(&journal, id, 100000.0, 20, "2026-01-01", Some(0.15)).unwrap();
        catch_up(&journal, id, "L-001", Some("2026-04-01")).unwrap();
    }
    let loan = journal.loans().by_ref(ann, "L-001").unwrap().unwrap();
    suspensions::record(&journal, loan.id, "2026-05-01", Some("2026-06-01"), Some("2026-10-09")).unwrap();
    funds::post(&journal, Posting::new(Fund::Savings, ann, "2026-01-05", "Deposit", 2000.0, "")).unwrap();
    funds::post(&journal, Posting::new(Fund::Savings, ann, "2026-03-05", "Deposit", 2000.0, "")).unwrap();
    funds::post(&journal, Posting::new(Fund::Christmas, ann, "2026-02-05", "Deposit", 1000.0, "")).unwrap();
    funds::enrol(&journal, ann, 500.0, "2026-01-01").unwrap();
    funds::post(&journal, Posting::new(Fund::Benevolent, ann, "2026-01-01", "Contribution", 500.0, "")).unwrap();
    journal.close();
    path
}

fn everyone(source: &Source) -> Vec<i64> {
    source.members().unwrap().iter().map(|m| m.id).collect()
}

fn all_options() -> Options {
    Options { loans: true, savings: true, funds: true, dates: None }
}

fn go() -> impl FnMut(usize, usize, &str) -> bool {
    |_, _, _| true
}

fn count(journal: &Journal, sql: &str) -> i64 {
    journal.conn().query_row(sql, [], |r| r.get(0)).unwrap()
}

#[test]
fn a_journal_that_is_not_one_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    assert!(Source::open(&dir.path().join("missing.db")).is_err());
    let other = dir.path().join("other.db");
    Connection::open(&other).unwrap().execute_batch("CREATE TABLE x (a);").unwrap();
    assert!(Source::open(&other).unwrap_err().to_string().contains("individuals"));
}

#[test]
fn everything_comes_across_into_an_empty_journal() {
    let dir = tempfile::tempdir().unwrap();
    let source = Source::open(&source_journal(dir.path())).unwrap();
    let journal = Journal::open_in_memory().unwrap();
    let out = run(&journal, &source, &everyone(&source), &all_options(), &HashMap::new(), &mut go()).unwrap();
    assert_eq!((out.members, out.loans, out.ledger, out.savings, out.christmas, out.benevolent, out.enrolments, out.suspensions), (2, 2, 14, 2, 1, 1, 1, 1));
    assert_eq!(out.rejected, 0, "{:?}", out.warnings);

    let ann = journal.members().all().unwrap().into_iter().find(|m| m.name == "Ann").unwrap();
    let ben = journal.members().all().unwrap().into_iter().find(|m| m.name == "Ben").unwrap();
    // Each keeps their own L-001; 1.x renamed both because the reference
    // existed somewhere in the journal.
    assert!(journal.loans().by_ref(ann.id, "L-001").unwrap().is_some());
    assert!(journal.loans().by_ref(ben.id, "L-001").unwrap().is_some());
    assert_eq!(journal.ledger().for_loan(ann.id, "L-001").unwrap().len(), 7);
    assert_eq!(journal.loans().by_ref(ann.id, "L-001").unwrap().unwrap().balance, 85000.0);
    assert_eq!(journal.funds().balance(Fund::Savings, ann.id).unwrap(), 4000.0);
    assert_eq!(journal.funds().balance(Fund::Christmas, ann.id).unwrap(), 1000.0);
    assert!(journal.funds().enrolment(ann.id).unwrap().is_some());
    assert_eq!(journal.loans().suspensions_for_member(ann.id).unwrap().len(), 1);
    assert_eq!(ann.pf_no, "PF1");

    let history = journal.imports().all().unwrap();
    assert_eq!(history.len(), 1);
    undo(&journal, out.import_id).unwrap();
    assert_eq!(count(&journal, "SELECT COUNT(*) FROM individuals"), 0);
    assert_eq!(count(&journal, "SELECT COUNT(*) FROM ledger"), 0);
    assert_eq!(count(&journal, "SELECT COUNT(*) FROM christmas_savings"), 0);
    assert!(journal.imports().all().unwrap().is_empty());
}

#[test]
fn a_member_on_record_is_merged_into_and_undo_leaves_them_as_they_were() {
    let dir = tempfile::tempdir().unwrap();
    let source = Source::open(&source_journal(dir.path())).unwrap();
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "Ann", "PF9");
    issue(&journal, ann, 50000.0, 10, "2025-06-01", Some(0.15)).unwrap();
    funds::post(&journal, Posting::new(Fund::Christmas, ann, "2025-12-05", "Deposit", 700.0, "")).unwrap();
    let before = (
        count(&journal, "SELECT COUNT(*) FROM ledger"),
        journal.funds().balance(Fund::Christmas, ann).unwrap(),
        journal.loans().by_ref(ann, "L-001").unwrap().unwrap().balance,
    );

    let ids: Vec<i64> = source.members().unwrap().iter().filter(|m| m.name == "Ann").map(|m| m.id).collect();
    let pre = preview(&journal, &source, &ids, &all_options(), &HashMap::new()).unwrap();
    assert_eq!(pre.merged, [("Ann".to_string(), "Ann".to_string())]);
    assert_eq!(pre.renamed, [("Ann".to_string(), "L-001".to_string(), "L-002".to_string())]);

    let out = run(&journal, &source, &ids, &all_options(), &HashMap::new(), &mut go()).unwrap();
    assert_eq!((out.members, out.merged), (0, 1));
    // Her own L-001 stays hers; the incoming one is L-002, ledger and all.
    assert_eq!(journal.ledger().for_loan(ann, "L-002").unwrap().len(), 7);
    assert_eq!(journal.loans().by_ref(ann, "L-002").unwrap().unwrap().balance, 85000.0);
    assert_eq!(journal.funds().balance(Fund::Christmas, ann).unwrap(), 1700.0);
    assert_eq!(journal.members().get(ann).unwrap().unwrap().pf_no, "PF9");
    assert!(out.warnings.iter().any(|w| w.contains("already holds L-001")));

    undo(&journal, out.import_id).unwrap();
    let after = (
        count(&journal, "SELECT COUNT(*) FROM ledger"),
        journal.funds().balance(Fund::Christmas, ann).unwrap(),
        journal.loans().by_ref(ann, "L-001").unwrap().unwrap().balance,
    );
    assert_eq!(after, before);
    assert!(journal.loans().by_ref(ann, "L-002").unwrap().is_none());
    assert!(journal.funds().enrolment(ann).unwrap().is_none());
    assert!(journal.loans().suspensions_for_member(ann).unwrap().is_empty());
}

#[test]
fn a_resemblance_is_a_conflict_that_the_decision_settles() {
    let dir = tempfile::tempdir().unwrap();
    let source = Source::open(&source_journal(dir.path())).unwrap();
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "ANN", "PF7");
    let ids = everyone(&source);
    let pre = preview(&journal, &source, &ids, &Options::default(), &HashMap::new()).unwrap();
    assert_eq!(pre.conflicts.len(), 1);
    assert_eq!(pre.conflicts[0].matches[0].reasons, ["Name"]);
    assert_eq!(pre.conflicts[0].suggested(), Decision::Merge(ann));

    let incoming = pre.conflicts[0].source.id;
    let decisions = HashMap::from([(incoming, Decision::New)]);
    let out = run(&journal, &source, &ids, &Options::default(), &decisions, &mut go()).unwrap();
    assert_eq!((out.members, out.merged), (2, 0));
    // A new member keeps a PF number nobody holds.
    assert!(journal.members().all().unwrap().iter().any(|m| m.name == "Ann" && m.pf_no == "PF1"));

    let skip = Journal::open_in_memory().unwrap();
    member(&skip, "ANN", "PF7");
    let out = run(&skip, &source, &ids, &Options::default(), &HashMap::from([(incoming, Decision::Skip)]), &mut go()).unwrap();
    assert_eq!(out.members, 1);
}

#[test]
fn rows_that_cannot_be_read_are_refused_and_counted() {
    let dir = tempfile::tempdir().unwrap();
    let path = source_journal(dir.path());
    {
        let conn = Connection::open(&path).unwrap();
        // Rows a strict journal could not hold, as an old one might.
        conn.execute_batch(
            "PRAGMA ignore_check_constraints = ON;
             INSERT INTO ledger (individual_id, date, event_type, loan_id, added, deducted) VALUES (1, '01/02/2026', 'Repayment', 'L-001', 0, 10);
             INSERT INTO ledger (individual_id, date, event_type, loan_id, added, deducted) VALUES (1, '2026-05-01', 'Repayment', 'L-404', 0, 10);",
        )
        .unwrap();
    }
    let source = Source::open(&path).unwrap();
    let journal = Journal::open_in_memory().unwrap();
    let out = run(&journal, &source, &everyone(&source), &all_options(), &HashMap::new(), &mut go()).unwrap();
    assert_eq!(out.rejected, 2);
    assert!(out.warnings.iter().any(|w| w.contains("unreadable date")));
    assert!(out.warnings.iter().any(|w| w.contains("their loan was not")));
    assert_eq!(out.ledger, 14);
}

#[test]
fn a_date_range_limits_every_kind_of_transaction() {
    let dir = tempfile::tempdir().unwrap();
    let source = Source::open(&source_journal(dir.path())).unwrap();
    let journal = Journal::open_in_memory().unwrap();
    let options = Options { dates: Some(("2026-02-01".into(), "2026-02-28".into())), ..all_options() };
    let out = run(&journal, &source, &everyone(&source), &options, &HashMap::new(), &mut go()).unwrap();
    // February: each loan's charge and deduction, and Ann's Christmas deposit.
    assert_eq!((out.ledger, out.savings, out.christmas, out.benevolent), (4, 0, 1, 0));
}

#[test]
fn a_stopped_import_leaves_nothing_behind() {
    let dir = tempfile::tempdir().unwrap();
    let source = Source::open(&source_journal(dir.path())).unwrap();
    let journal = Journal::open_in_memory().unwrap();
    let mut stop_second = |done: usize, _: usize, _: &str| done < 2;
    assert!(matches!(run(&journal, &source, &everyone(&source), &all_options(), &HashMap::new(), &mut stop_second), Err(Error::Cancelled)));
    assert_eq!(count(&journal, "SELECT COUNT(*) FROM individuals"), 0);
    assert!(journal.imports().all().unwrap().is_empty());
}

#[test]
fn an_imported_ledger_row_keeps_its_figures() {
    let dir = tempfile::tempdir().unwrap();
    let path = source_journal(dir.path());
    {
        let source = Journal::open(&path, OpenOptions::default()).unwrap();
        source
            .ledger()
            .add(&NewEntry { member_id: 1, date: "2026-04-20".into(), event_type: "Repayment".into(), loan_ref: "L-001".into(), deducted: 1.0, notes: "odd".into(), ..NewEntry::default() })
            .unwrap();
        source.close();
    }
    let source = Source::open(&path).unwrap();
    let journal = Journal::open_in_memory().unwrap();
    run(&journal, &source, &everyone(&source), &all_options(), &HashMap::new(), &mut go()).unwrap();
    assert_eq!(count(&journal, "SELECT COUNT(*) FROM ledger WHERE notes='odd' AND deducted=1"), 1);
}
