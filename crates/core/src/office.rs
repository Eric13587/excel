//! Payroll comparisons use integer cents and preserve each signed-off revision.
use crate::{Error, Journal, Result};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Line {
    pub pf: String,
    pub expected: i64,
    pub payroll: i64,
    pub posted: i64,
}

fn invalid(s: &str) -> Error {
    Error::Invalid(s.into())
}
pub fn cents(s: &str) -> Result<i64> {
    let s = s.trim();
    let (w, f) = s.split_once('.').unwrap_or((s, ""));
    if w.is_empty()
        || !w.bytes().all(|c| c.is_ascii_digit())
        || f.len() > 2
        || !f.bytes().all(|c| c.is_ascii_digit())
    {
        return Err(invalid(
            "Enter a positive amount or zero, with up to two decimal places.",
        ));
    }
    let fraction = format!("{f:0<2}")
        .parse::<i64>()
        .map_err(|_| invalid("Invalid amount"))?;
    w.parse::<i64>()
        .ok()
        .and_then(|n| n.checked_mul(100))
        .and_then(|n| n.checked_add(fraction))
        .ok_or_else(|| invalid("Amount is too large"))
}
fn month_key(month: &str) -> Result<()> {
    if month.len() != 7 || crate::dates::parse(&format!("{month}-01")).is_none() {
        return Err(invalid("Use YYYY-MM for the payroll month."));
    }
    Ok(())
}
pub fn compare(j: &Journal, month: &str, input: &str) -> Result<Vec<Line>> {
    month_key(month)?;
    let mut people = BTreeMap::new();
    let mut statement=j.conn().prepare("WITH postings(member,cents) AS (
      SELECT l.individual_id,c.deducted FROM ledger l JOIN ledger_cents c ON c.posting_id=l.id WHERE substr(l.date,1,7)=?1 AND l.event_type='Repayment'
      UNION ALL SELECT l.individual_id,c.amount FROM savings l JOIN savings_cents c ON c.posting_id=l.id WHERE substr(l.date,1,7)=?1 AND l.transaction_type='Deposit'
      UNION ALL SELECT l.individual_id,c.amount FROM christmas_savings l JOIN christmas_savings_cents c ON c.posting_id=l.id WHERE substr(l.date,1,7)=?1 AND l.transaction_type='Deposit'
      UNION ALL SELECT l.individual_id,c.amount FROM benevolent_ledger l JOIN benevolent_ledger_cents c ON c.posting_id=l.id WHERE substr(l.date,1,7)=?1 AND l.transaction_type='Contribution')
      SELECT trim(i.pf_no), COALESCE(sum(p.cents),0) FROM individuals i LEFT JOIN postings p ON p.member=i.id GROUP BY i.id")?;
    for row in statement.query_map([month], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))
    })? {
        let (pf, posted) = row?;
        if pf.is_empty() && posted == 0 {
            continue;
        }
        if pf.is_empty() || people.insert(pf, posted).is_some() {
            return Err(invalid(
                "Each member used in payroll needs a unique PF number.",
            ));
        }
    }
    let mut seen = std::collections::BTreeSet::new();
    let mut out = Vec::new();
    for line in input.lines().filter(|l| !l.trim().is_empty()) {
        let fields = line.split('\t').map(str::trim).collect::<Vec<_>>();
        let [pf, expected, actual] = fields.as_slice() else {
            return Err(invalid(
                "Paste three tab-separated columns from payroll: PF, expected and deducted.",
            ));
        };
        if !seen.insert(*pf) {
            return Err(Error::Invalid(format!("Duplicate payroll row for {pf}")));
        }
        let posted = people
            .remove(*pf)
            .ok_or_else(|| Error::Invalid(format!("Unknown payroll number {pf}")))?;
        out.push(Line {
            pf: pf.to_string(),
            expected: cents(expected)?,
            payroll: cents(actual)?,
            posted,
        });
    }
    if out.is_empty() {
        return Err(invalid("Paste the payroll rows before comparing."));
    }
    for (pf, posted) in people {
        if posted != 0 {
            out.push(Line {
                pf,
                expected: 0,
                payroll: 0,
                posted,
            });
        }
    }
    Ok(out)
}
pub fn principal() -> Result<String> {
    let name = std::process::Command::new(if cfg!(windows) {
        "whoami.exe"
    } else {
        "/usr/bin/whoami"
    })
    .output()?;
    let value = String::from_utf8_lossy(&name.stdout).trim().to_lowercase();
    if !name.status.success() || value.is_empty() {
        return Err(invalid(
            "Could not identify the signed-in operating system user.",
        ));
    }
    Ok(value)
}
pub fn role(j: &Journal, user: &str) -> Result<String> {
    let count: i64 = j
        .conn()
        .query_row("SELECT count(*) FROM operators", [], |r| r.get(0))?;
    if count == 0 {
        return Ok("unconfigured".into());
    }
    Ok(j.conn()
        .query_row(
            "SELECT role FROM operators WHERE principal=?",
            [user],
            |r| r.get(0),
        )
        .optional()?
        .unwrap_or_else(|| "reviewer".into()))
}
pub fn set_role(j: &Journal, user: &str, target: &str, new_role: &str) -> Result<()> {
    let target = target.trim().to_lowercase();
    j.transaction(||{
        let current=role(j,user)?;
        if target.is_empty() || !matches!(new_role,"administrator"|"officer"|"reviewer") || !(current=="administrator" || current=="unconfigured" && target==user && new_role=="administrator") {return Err(invalid("An administrator must configure this account."));}
        if target==user && new_role!="administrator"{return Err(invalid("Keep your own administrator access."));}
        j.conn().execute("INSERT INTO operators VALUES (?,?) ON CONFLICT(principal) DO UPDATE SET role=excluded.role",params![target,new_role])?;
        Ok(())
    })
}
pub fn session(j: &Journal, user: &str) -> Result<()> {
    j.conn().execute_batch("CREATE TEMP TABLE IF NOT EXISTS session_operator(principal TEXT); DELETE FROM session_operator;")?;
    j.conn()
        .execute("INSERT INTO session_operator VALUES(?)", [user])?;
    j.conn().execute_batch("CREATE TEMP TRIGGER IF NOT EXISTS audit_operator AFTER INSERT ON main.audit_log BEGIN UPDATE audit_log SET operator=(SELECT principal FROM session_operator) WHERE id=NEW.id; END;")?;
    // Recheck permissions at write time so a demotion also applies to open windows.
    for table in [
        "individuals",
        "loans",
        "ledger",
        "savings",
        "christmas_savings",
        "benevolent_ledger",
        "benevolent_accounts",
        "loan_suspensions",
        "savings_suspensions",
        "grid_participation",
        "settings",
        "import_history",
        "period_events",
        "payout_requests",
        "operators",
    ] {
        let allowed = if table == "operators" {
            "'administrator'"
        } else {
            "'administrator','officer'"
        };
        for event in ["INSERT", "UPDATE", "DELETE"] {
            j.conn().execute_batch(&format!("CREATE TEMP TRIGGER IF NOT EXISTS gate_{table}_{event} BEFORE {event} ON main.{table}
                WHEN EXISTS(SELECT 1 FROM operators) AND NOT EXISTS(SELECT 1 FROM operators WHERE principal=(SELECT principal FROM session_operator) AND role IN ({allowed}))
                BEGIN SELECT RAISE(ABORT,'This account does not have permission to make this change.'); END;"))?;
        }
    }
    if role(j, user)? == "reviewer" {
        j.conn().pragma_update(None, "query_only", true)?;
    }
    Ok(())
}
pub fn close(
    j: &Journal,
    user: &str,
    month: &str,
    input: &str,
    bank: &str,
    reason: &str,
) -> Result<()> {
    j.transaction(||{
        if !matches!(role(j,user)?.as_str(),"administrator"|"officer"){return Err(invalid("Set up a named officer or administrator first."));}
        if month > j.today().format("%Y-%m").to_string().as_str(){return Err(invalid("A future payroll month cannot be closed."));}
        if latest(j,month)?.is_some_and(|(event,_)|event!="reopen"){return Err(invalid("This month is already closed."));}
        let rows=compare(j,month,input)?;
        let received=cents(bank)?;
        let total=rows.iter().try_fold(0_i64,|sum,l|sum.checked_add(l.payroll)).ok_or_else(||invalid("Payroll total is too large"))?;
        if received!=total{return Err(invalid("The amount received must match total payroll deductions."));}
        if rows.iter().any(|l|l.posted!=l.payroll){return Err(invalid("Resolve differences between posted and deducted amounts before closing."));}
        if rows.iter().any(|l|l.expected!=l.payroll) && reason.trim().is_empty(){return Err(invalid("Explain the difference between expected and deducted amounts."));}
        j.conn().execute("INSERT INTO period_events(month,event,operator,reason,bank_cents,details) VALUES (?,'close',?,?,?,?)",params![month,user,reason,received,serde_json::to_string(&rows).map_err(|_|invalid("Could not save payroll details"))?])?;
        Ok(())
    })
}
fn latest(j: &Journal, month: &str) -> Result<Option<(String, String)>> {
    Ok(j.conn()
        .query_row(
            "SELECT event,operator FROM period_events WHERE month=? ORDER BY id DESC LIMIT 1",
            [month],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?)
}
pub fn reopen(j: &Journal, user: &str, month: &str, reason: &str) -> Result<()> {
    j.transaction(||{
        if reason.trim().is_empty() || !matches!(role(j,user)?.as_str(),"administrator"|"officer"){return Err(invalid("A named officer and a reason are required."));}
        let event=match latest(j,month)? {
            Some((e,_)) if e=="close"=>"request",
            Some((e,requester)) if e=="request" && requester!=user=>"reopen",
            _=>return Err(invalid("Reopening requires a closed month and a second officer to approve the request.")),
        };
        j.conn().execute("INSERT INTO period_events(month,event,operator,reason,details) VALUES(?,?,?,?,'')",params![month,event,user,reason])?;
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn signed_period_is_locked_until_another_officer_approves() {
        let j = Journal::open_in_memory().unwrap();
        set_role(&j, "ann", "ann", "administrator").unwrap();
        set_role(&j, "ann", "bob", "officer").unwrap();
        session(&j, "ann").unwrap();
        j.conn().execute_batch("INSERT INTO individuals(name,pf_no) VALUES('Demo','001'); INSERT INTO savings(individual_id,date,transaction_type,amount) VALUES(1,'2020-01-02','Deposit',12.34);").unwrap();
        let payroll = "001\t12.34\t12.34";
        assert!(close(&j, "ann", "2020-01", payroll, "11", "").is_err());
        close(&j, "ann", "2020-01", payroll, "12.34", "").unwrap();
        assert!(j.conn().execute("DELETE FROM savings", []).is_err());
        reopen(&j, "ann", "2020-01", "Correction").unwrap();
        assert!(reopen(&j, "ann", "2020-01", "Approve").is_err());
        reopen(&j, "bob", "2020-01", "Approved").unwrap();
        j.conn().execute("DELETE FROM savings", []).unwrap();
    }
}

/// Print a specific signed revision, independently of later changes to the journal.
pub fn close_report(j: &Journal, month: &str) -> Result<crate::reports::Table> {
    use crate::reports::{Table, Value};
    let (revision,operator,when,bank,reason,data):(i64,String,String,i64,String,String)=j.conn().query_row(
        "SELECT id,operator,recorded_at,bank_cents,reason,details FROM period_events WHERE month=? AND event='close' ORDER BY id DESC LIMIT 1",[month],
        |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?.ok_or_else(||invalid("This month has no signed close."))?;
    let rows: Vec<Line> = serde_json::from_str(&data)
        .map_err(|_| invalid("The saved close details could not be read."))?;
    let amount = |n: i64| Value::Number(n as f64 / 100.0);
    Ok(Table {
        title: format!("Payroll reconciliation · {month}"),
        subtitle: format!(
            "Revision {revision} · {operator} · {when} UTC · Received KSh {}.{:02} · {reason}",
            bank / 100,
            bank % 100
        ),
        sheet: "Payroll reconciliation".into(),
        columns: ["PF number", "Expected", "Payroll", "Posted"]
            .map(String::from)
            .to_vec(),
        rows: rows
            .into_iter()
            .map(|l| {
                vec![
                    Value::text(l.pf),
                    amount(l.expected),
                    amount(l.payroll),
                    amount(l.posted),
                ]
            })
            .collect(),
        total: None,
    })
}

fn holdings(j: &Journal, id: i64) -> Result<(i64, i64)> {
    use crate::db::model::Fund;
    let amount = |fund| cents(&format!("{:.2}", j.funds().balance(fund, id)?));
    Ok((amount(Fund::Savings)?, amount(Fund::Christmas)?))
}

pub fn request_payout(j: &Journal, id: i64, status: &str, date: &str) -> Result<()> {
    let user = principal()?;
    j.transaction(||{
        if !matches!(role(j,&user)?.as_str(),"administrator"|"officer"){return Err(invalid("Configure named operators before requesting a second approval."));}
        let (savings,christmas)=holdings(j,id)?;
        j.conn().execute("INSERT INTO payout_requests(member_id,status,leaving_date,savings_cents,christmas_cents,requested_by) VALUES(?,?,?,?,?,?)
        ON CONFLICT(member_id) WHERE approved_by IS NULL DO UPDATE SET status=excluded.status,leaving_date=excluded.leaving_date,savings_cents=excluded.savings_cents,christmas_cents=excluded.christmas_cents,requested_by=excluded.requested_by,requested_at=CURRENT_TIMESTAMP",params![id,status,date,savings,christmas,user])?;
        Ok(())
    })
}

/// Runs inside the same transaction that pays the member and changes their status.
pub(crate) fn approve_payout(j: &Journal, id: i64, status: &str, date: &str) -> Result<()> {
    let configured: bool =
        j.conn()
            .query_row("SELECT EXISTS(SELECT 1 FROM operators)", [], |r| r.get(0))?;
    if !configured {
        return Ok(());
    }
    let (savings, christmas) = holdings(j, id)?;
    if savings == 0 && christmas == 0 {
        return Ok(());
    }
    let user = principal()?;
    if matches!(role(j,&user)?.as_str(),"administrator"|"officer") &&
        j.conn().execute("UPDATE payout_requests SET approved_by=?1, approved_at=CURRENT_TIMESTAMP WHERE member_id=?2 AND status=?3 AND leaving_date=?4 AND savings_cents=?5 AND christmas_cents=?6 AND requested_by<>?1 AND approved_by IS NULL",
            params![user,id,status,date,savings,christmas])?==1 {return Ok(());}
    Err(invalid(
        "Use Retire or Leave to request the payout. A different officer must confirm the same status, date and amounts.",
    ))
}
