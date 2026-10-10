use rust_xlsxwriter::Workbook;

use super::*;

fn workbook(dir: &Path, name: &str, rows: &[&[&str]]) -> std::path::PathBuf {
    let path = dir.join(name);
    let mut book = Workbook::new();
    let sheet = book.add_worksheet();
    for (r, row) in rows.iter().enumerate() {
        for (c, value) in row.iter().enumerate() {
            match value.parse::<f64>() {
                Ok(n) => sheet.write_number(r as u32, c as u16, n).unwrap(),
                Err(_) => sheet.write_string(r as u32, c as u16, *value).unwrap(),
            };
        }
    }
    book.save(&path).unwrap();
    path
}

fn member(journal: &Journal, name: &str, pf: &str) -> i64 {
    journal.members().add(&MemberDetails { name: name.into(), pf_no: pf.into(), phone: "0700".into(), ..Default::default() }).unwrap()
}

#[test]
fn the_sheet_shape_and_columns_are_recognised() {
    let roster = detect_mapping(&["Full Name", "ID Number", "PF No", "Mobile", "E-mail", "Employment Status"].map(String::from));
    assert_eq!(roster.kind, SheetKind::Roster);
    assert_eq!(roster.header(Field::Name), Some("Full Name"));
    assert_eq!(roster.header(Field::IdNo), Some("ID Number"));
    assert_eq!(roster.header(Field::PfNo), Some("PF No"));
    assert_eq!(roster.header(Field::Phone), Some("Mobile"));
    assert_eq!(roster.header(Field::EmploymentStatus), Some("Employment Status"));
    let sheet = detect_mapping(&["Name", "Employee Number", "NOV", "Dec-25", "January", "Total"].map(String::from));
    assert_eq!(sheet.kind, SheetKind::Contribution);
    assert_eq!(sheet.months, [("NOV".to_string(), 11), ("Dec-25".to_string(), 12), ("January".to_string(), 1)]);
    assert_eq!(month_of("Total"), None);
}

#[test]
fn a_roster_fills_in_members_and_adds_new_ones() {
    let dir = tempfile::tempdir().unwrap();
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "ANN WANJIRU KAMAU", "");
    member(&journal, "PETER MWANGI", "555");
    let path = workbook(
        dir.path(),
        "roster.xlsx",
        &[
            &["Name", "PF No", "Phone", "Employment Status"],
            &["Ann Kamau", "74433", "0722 111 222", "employed"],
            &["Zawadi Akinyi", "555", "", "Active"],
            &["TOTAL", "", "", ""],
        ],
    );
    let sheet = read_sheet(&path).unwrap();
    let mapping = detect_mapping(&sheet.headers);
    let plan = build_plan(&journal, &sheet, &mapping, Fund::Christmas, 2026).unwrap();
    assert_eq!(plan.rows.len(), 2);
    assert_eq!((plan.rows[0].verdict, plan.rows[0].action), (Verdict::Match, Action::Update(ann)));
    assert_eq!(plan.rows[1].action, Action::Create);
    assert_eq!(plan.rows[0].fields.pf_no.as_deref(), Some("74433")); // a number cell, read whole

    let out = apply(&journal, &plan).unwrap();
    assert_eq!((out.updated, out.created), (1, 1));
    let updated = journal.members().get(ann).unwrap().unwrap();
    assert_eq!((updated.pf_no.as_str(), updated.phone.as_str(), updated.name.as_str()), ("74433", "0722111222", "ANN WANJIRU KAMAU"));
    // Zawadi's PF number is Peter's: she is added without it.
    assert!(out.warnings.iter().any(|w| w.contains("PF NO '555'")));
    assert!(journal.members().all().unwrap().iter().any(|m| m.name == "Zawadi Akinyi" && m.pf_no.is_empty()));
}

#[test]
fn a_roster_that_marks_a_member_as_retired_withdraws_their_savings() {
    let dir = tempfile::tempdir().unwrap();
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "ANN WANJIRU", "");
    crate::funds::deposit(&journal, Fund::Savings, ann, 5000.0, "2026-01-05", "").unwrap();
    let path = workbook(dir.path(), "roster.xlsx", &[&["Name", "Employment Status"], &["Ann Wanjiru", "Retired"]]);
    let sheet = read_sheet(&path).unwrap();
    let plan = build_plan(&journal, &sheet, &detect_mapping(&sheet.headers), Fund::Christmas, 2026).unwrap();
    let out = apply(&journal, &plan).unwrap();
    assert_eq!(out.left, [("ANN WANJIRU".to_string(), 5000.0)]);
    assert_eq!(journal.funds().balance(Fund::Savings, ann).unwrap(), 0.0);
    assert_eq!(journal.members().get(ann).unwrap().unwrap().employment_status, "Retired");
}

#[test]
fn a_clashing_pf_number_never_clears_the_members_own() {
    let dir = tempfile::tempdir().unwrap();
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "ANN KAMAU", "111");
    member(&journal, "PETER MWANGI", "222");
    let path = workbook(dir.path(), "r.xlsx", &[&["Name", "PF No"], &["Ann Kamau", "222"]]);
    let sheet = read_sheet(&path).unwrap();
    let plan = build_plan(&journal, &sheet, &detect_mapping(&sheet.headers), Fund::Christmas, 2026).unwrap();
    apply(&journal, &plan).unwrap();
    assert_eq!(journal.members().get(ann).unwrap().unwrap().pf_no, "111");
}

#[test]
fn a_contribution_sheet_posts_each_month_on_the_first() {
    let dir = tempfile::tempdir().unwrap();
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "ANN KAMAU", "");
    let path = workbook(
        dir.path(),
        "xmas.xlsx",
        &[&["Name", "PF", "NOV", "DEC", "JAN", "Total"], &["Ann Kamau", "74433", "1000", "1,500", "500", "3000"], &["New Person", "", "200", "0", "", "200"]],
    );
    let sheet = read_sheet(&path).unwrap();
    let mapping = detect_mapping(&sheet.headers);
    let plan = build_plan(&journal, &sheet, &mapping, Fund::Christmas, 2025).unwrap();
    assert_eq!(plan.rows[0].months, [("2025-11-01".to_string(), 1000.0), ("2025-12-01".to_string(), 1500.0), ("2026-01-01".to_string(), 500.0)]);

    let out = apply(&journal, &plan).unwrap();
    assert_eq!((out.created, out.deposits, out.total, out.members_credited), (1, 4, 3200.0, 2));
    assert_eq!(journal.funds().balance(Fund::Christmas, ann).unwrap(), 3000.0);
    assert_eq!(journal.members().get(ann).unwrap().unwrap().pf_no, "74433");
}

#[test]
fn a_benevolent_sheet_moves_the_schedule_past_what_it_recorded() {
    let dir = tempfile::tempdir().unwrap();
    let journal = Journal::open_in_memory().unwrap();
    let ann = member(&journal, "ANN KAMAU", "");
    crate::funds::enrol(&journal, ann, 500.0, "2025-11-01").unwrap();
    let path = workbook(dir.path(), "b.xlsx", &[&["Name", "NOV", "DEC", "JAN"], &["Ann Kamau", "500", "500", "500"]]);
    let sheet = read_sheet(&path).unwrap();
    let plan = build_plan(&journal, &sheet, &detect_mapping(&sheet.headers), Fund::Benevolent, 2025).unwrap();
    apply(&journal, &plan).unwrap();
    assert_eq!(journal.funds().enrolment(ann).unwrap().unwrap().next_due_date.as_deref(), Some("2026-02-01"));
}

#[test]
fn a_sheet_without_names_cannot_be_planned() {
    let dir = tempfile::tempdir().unwrap();
    let journal = Journal::open_in_memory().unwrap();
    let path = workbook(dir.path(), "x.xlsx", &[&["PF", "Phone"], &["1", "2"]]);
    let sheet = read_sheet(&path).unwrap();
    assert!(build_plan(&journal, &sheet, &detect_mapping(&sheet.headers), Fund::Christmas, 2026).is_err());
}
