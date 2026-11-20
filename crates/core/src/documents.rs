//! One row per source record. Member-owned records include the roster identity.
use crate::reports::{Table, Value};
use crate::{Error, Journal, Result};
use rusqlite::types::ValueRef;

pub const SOURCES: &[(&str, &str)] = &[
    ("Members", "individuals"),
    ("Loans", "loans"),
    ("Loan transactions", "ledger"),
    ("Savings", "savings"),
    ("Christmas savings", "christmas_savings"),
    ("Benevolent transactions", "benevolent_ledger"),
    ("Benevolent accounts", "benevolent_accounts"),
    ("Loan suspensions", "loan_suspensions"),
    ("Savings suspensions", "savings_suspensions"),
    ("Grid participation", "grid_participation"),
    ("Imports", "import_history"),
    ("Activity log", "audit_log"),
    ("Exit payout approvals", "payout_requests"),
    ("Month-end history", "period_events"),
    ("Member financial overview", "member_overview"),
];

fn identifier(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "\"\""))
}

fn fields(j: &Journal, source: usize) -> Result<Vec<(String, String)>> {
    let (_, table) = SOURCES
        .get(source)
        .ok_or_else(|| Error::Invalid("Unknown data source".into()))?;
    let mut stmt = j
        .conn()
        .prepare("SELECT name FROM pragma_table_info(?) ORDER BY cid")?;
    let names = stmt
        .query_map([table], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut fields = Vec::new();
    if names.iter().any(|s| s == "individual_id") {
        fields.extend([
            ("Member".into(), "m.name".into()),
            ("Payroll number".into(), "m.pf_no".into()),
        ]);
    }
    for name in names {
        let mut label = name.replace('_', " ");
        label[0..1].make_ascii_uppercase();
        fields.push((label, format!("t.{}", identifier(&name))));
    }
    Ok(fields)
}

pub fn columns(j: &Journal, source: usize) -> Result<Vec<String>> {
    Ok(fields(j, source)?
        .into_iter()
        .map(|(label, _)| label)
        .collect())
}

pub fn build(
    j: &Journal,
    source: usize,
    selected: &[String],
    title: String,
    subtitle: String,
) -> Result<Table> {
    let fields = fields(j, source)?;
    if selected.is_empty() {
        return Err(Error::Invalid("Choose at least one column.".into()));
    }
    let selections = selected
        .iter()
        .map(|s| {
            fields
                .iter()
                .find(|(k, _)| k == s)
                .map(|(_, v)| v.as_str())
                .ok_or_else(|| Error::Invalid("Unknown report column".into()))
        })
        .collect::<Result<Vec<_>>>()?;
    let (label, table) = SOURCES[source];
    let join = if fields.iter().any(|(_, sql)| sql == "m.name") {
        "LEFT JOIN individuals m ON t.individual_id=m.id"
    } else {
        ""
    };
    let order = if table == "member_overview" {
        "t.id"
    } else {
        "t.rowid"
    };
    let sql = format!(
        "SELECT {} FROM {} t {join} ORDER BY {order}",
        selections.join(", "),
        identifier(table)
    );
    let mut stmt = j.conn().prepare(&sql)?;
    let rows = stmt
        .query_map([], |row| {
            (0..selected.len())
                .map(|i| {
                    Ok(match row.get_ref(i)? {
                        ValueRef::Real(n) => Value::Number(n),
                        ValueRef::Integer(n) => Value::text(n.to_string()),
                        ValueRef::Text(s) => Value::text(String::from_utf8_lossy(s)),
                        ValueRef::Null | ValueRef::Blob(_) => Value::Blank,
                    })
                })
                .collect::<rusqlite::Result<Vec<_>>>()
        })?
        .collect::<rusqlite::Result<_>>()?;
    Ok(Table {
        title,
        subtitle,
        sheet: label.into(),
        columns: selected.to_vec(),
        rows,
        total: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_source_exports_and_member_fields_follow_the_selected_order() {
        let j = Journal::open_in_memory().unwrap();
        j.conn().execute_batch("INSERT INTO individuals(name,pf_no) VALUES('Demo','001'); INSERT INTO savings(individual_id,date,transaction_type,amount) VALUES(1,'2026-01-01','Deposit',12.34);").unwrap();
        for i in 0..SOURCES.len() {
            build(
                &j,
                i,
                &columns(&j, i).unwrap(),
                String::new(),
                String::new(),
            )
            .unwrap();
        }
        let t = build(
            &j,
            3,
            &["Amount".into(), "Payroll number".into()],
            String::new(),
            String::new(),
        )
        .unwrap();
        assert_eq!(t.rows[0], [Value::Number(12.34), Value::text("001")]);
    }
}
