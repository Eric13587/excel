# SQL statement inventory

Every SQL statement in the project, grouped by the table it touches. Generated
by AST-walking every `.py` file for string literals and f-strings that begin
with a SQL verb, then resolving the tables each one references.

| Scope | Statements |
|---|---|
| Production code (`src/`, `loan.py`) | 302 |
| Tests and `scratch/` | 134 |
| **Total** | **436** |

A statement that joins two tables is listed under each. Statements built by
string concatenation at runtime (`get_ledger`'s optional date filters,
`update_transaction`'s dynamic SET list, `update_loan_status`'s optional
columns) appear once, as the base fragment.

Counts by file:

| File | Statements |
|---|---|
| [src/database.py](../../src/database.py) | 226 |
| [src/services/gl_service.py](../../src/services/gl_service.py) | 30 |
| [src/services/loan_service.py](../../src/services/loan_service.py) | 11 |
| [src/engine.py](../../src/engine.py) | 9 |
| [src/services/undo_manager.py](../../src/services/undo_manager.py) | 7 |
| [src/services/balance_calculator.py](../../src/services/balance_calculator.py) | 6 |
| [src/views/ledger.py](../../src/views/ledger.py) | 4 |
| [src/services/savings_service.py](../../src/services/savings_service.py) | 2 |
| [src/services/transaction_manager.py](../../src/services/transaction_manager.py) | 2 |
| [src/reports.py](../../src/reports.py) | 1 |
| [src/services/benevolent_service.py](../../src/services/benevolent_service.py) | 1 |
| [src/services/christmas_service.py](../../src/services/christmas_service.py) | 1 |
| [src/views/dashboard.py](../../src/views/dashboard.py) | 1 |
| [src/views/treasury.py](../../src/views/treasury.py) | 1 |

---

## By table

### `individuals` — 53 statements

*SELECT 26 · DELETE 3 · CREATE 7 · ALTER 7 · INSERT 3 · UPDATE 7*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:160 | SELECT | `SELECT o.individual_id, COUNT(*), <fund> FROM <fund> o LEFT JOIN individuals i ON i.id = o.individual_id WHERE i.id IS NULL GROUP BY o.individual_i…` |
| src/database.py:184 | SELECT | `SELECT o.* FROM <fund> o LEFT JOIN individuals i ON i.id = o.individual_id WHERE i.id IS NULL` |
| src/database.py:206 | DELETE | `DELETE FROM <fund> WHERE individual_id IS NULL OR individual_id NOT IN (SELECT id FROM individuals)` |
| src/database.py:279 | CREATE | `CREATE TABLE IF NOT EXISTS individuals ( id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT, email TEXT, default_deduction REAL D…` |
| src/database.py:292 | ALTER | `ALTER TABLE individuals ADD COLUMN default_deduction REAL DEFAULT 0` |
| src/database.py:296 | ALTER | `ALTER TABLE individuals ADD COLUMN import_id INTEGER` |
| src/database.py:301 | ALTER | `ALTER TABLE individuals ADD COLUMN is_retired INTEGER DEFAULT 0` |
| src/database.py:305 | ALTER | `ALTER TABLE individuals ADD COLUMN retired_date TEXT` |
| src/database.py:310 | ALTER | `ALTER TABLE individuals ADD COLUMN employment_status TEXT DEFAULT 'Active'` |
| src/database.py:314 | ALTER | `ALTER TABLE individuals ADD COLUMN pf_no TEXT` |
| src/database.py:318 | ALTER | `ALTER TABLE individuals ADD COLUMN id_no TEXT` |
| src/database.py:324 | CREATE | `CREATE UNIQUE INDEX IF NOT EXISTS idx_individuals_pf_no ON individuals(pf_no) WHERE pf_no IS NOT NULL AND pf_no != ''` |
| src/database.py:329 | CREATE | `CREATE UNIQUE INDEX IF NOT EXISTS idx_individuals_id_no ON individuals(id_no) WHERE id_no IS NOT NULL AND id_no != ''` |
| src/database.py:701 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_individuals_updated AFTER UPDATE ON individuals BEGIN UPDATE individuals SET updated_at=datetime('now') WHERE id=N…` |
| src/database.py:706 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_audit_ind_ins AFTER INSERT ON individuals BEGIN INSERT INTO audit_log(ts,operation,entity,entity_id,summary) VALUE…` |
| src/database.py:710 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_audit_ind_upd AFTER UPDATE ON individuals WHEN NEW.name IS NOT OLD.name OR NEW.phone IS NOT OLD.phone OR NEW.email…` |
| src/database.py:718 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_audit_ind_del AFTER DELETE ON individuals BEGIN INSERT INTO audit_log(ts,operation,entity,entity_id,summary) VALUE…` |
| src/database.py:768 | INSERT | `INSERT INTO individuals (name, phone, email, default_deduction, created_at, employment_status, pf_no, id_no, is_retired, retired_date) VALUES (?, ?…` |
| src/database.py:780 | SELECT | `SELECT COUNT(*) FROM individuals WHERE LOWER(name) = LOWER(?)` |
| src/database.py:792 | SELECT | `SELECT name FROM individuals WHERE pf_no=? AND id<>?` |
| src/database.py:795 | SELECT | `SELECT name FROM individuals WHERE pf_no=?` |
| src/database.py:808 | SELECT | `SELECT name FROM individuals WHERE id_no=? AND id<>?` |
| src/database.py:811 | SELECT | `SELECT name FROM individuals WHERE id_no=?` |
| src/database.py:817 | SELECT | `SELECT * FROM individuals` |
| src/database.py:822 | SELECT | `SELECT name FROM individuals WHERE id=?` |
| src/database.py:828 | SELECT | `SELECT * FROM individuals WHERE id=?` |
| src/database.py:838 | UPDATE | `UPDATE individuals SET name=?, phone=?, email=? WHERE id=?` |
| src/database.py:845 | SELECT | `SELECT retired_date FROM individuals WHERE id=?` |
| src/database.py:849 | UPDATE | `UPDATE individuals SET employment_status=?, is_retired=?, retired_date=? WHERE id=?` |
| src/database.py:852 | UPDATE | `UPDATE individuals SET pf_no=? WHERE id=?` |
| src/database.py:854 | UPDATE | `UPDATE individuals SET id_no=? WHERE id=?` |
| src/database.py:859 | UPDATE | `UPDATE individuals SET default_deduction=? WHERE id=?` |
| src/database.py:865 | DELETE | `DELETE FROM individuals WHERE id=?` |
| src/database.py:1341 | UPDATE | `UPDATE individuals SET is_retired=1, retired_date=?, employment_status='Retired' WHERE id=?` |
| src/database.py:1348 | UPDATE | `UPDATE individuals SET is_retired=0, retired_date=NULL, employment_status='Active' WHERE id=?` |
| src/database.py:1640 | SELECT | `SELECT name, phone, email, default_deduction FROM individuals` |
| src/database.py:1651 | SELECT | `SELECT name FROM individuals` |
| src/database.py:1668 | INSERT | `INSERT INTO individuals (name, phone, email, default_deduction, created_at) VALUES (?, ?, ?, ?, ?)` |
| src/database.py:1695 | SELECT | `SELECT id, name, phone, email FROM individuals ORDER BY name` |
| src/database.py:1734 | SELECT | `SELECT id, name, phone, email FROM individuals WHERE id IN (<fund>)` |
| src/database.py:1734 | SELECT | `SELECT id, name, phone, email FROM individuals WHERE id IN (` |
| src/database.py:1753 | SELECT | `SELECT id, name, phone, email FROM individuals WHERE name LIKE ?` |
| src/database.py:1765 | SELECT | `SELECT id, name, phone, email FROM individuals WHERE phone = ?` |
| src/database.py:1782 | SELECT | `SELECT id, name, phone, email FROM individuals WHERE email LIKE ?` |
| src/database.py:1872 | SELECT | `SELECT id, name FROM individuals WHERE id IN (<fund>)` |
| src/database.py:1872 | SELECT | `SELECT id, name FROM individuals WHERE id IN (` |
| src/database.py:2042 | SELECT | `SELECT * FROM individuals WHERE id IN (<fund>)` |
| src/database.py:2042 | SELECT | `SELECT * FROM individuals WHERE id IN (` |
| src/database.py:2068 | SELECT | `SELECT name, id FROM individuals` |
| src/database.py:2074 | SELECT | `SELECT pf_no FROM individuals WHERE pf_no IS NOT NULL AND pf_no != ''` |
| src/database.py:2076 | SELECT | `SELECT id_no FROM individuals WHERE id_no IS NOT NULL AND id_no != ''` |
| src/database.py:2152 | INSERT | `INSERT INTO individuals (name, phone, email, default_deduction, created_at, employment_status, pf_no, id_no, is_retired, retired_date, import_id) V…` |
| src/database.py:2494 | DELETE | `DELETE FROM individuals WHERE import_id = ?` |

### `loans` — 46 statements

*CREATE 5 · ALTER 6 · INSERT 3 · SELECT 17 · UPDATE 13 · DELETE 2*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:383 | CREATE | `CREATE TABLE IF NOT EXISTS loans ( id INTEGER PRIMARY KEY AUTOINCREMENT, individual_id INTEGER, ref TEXT, principal REAL, total_amount REAL, balanc…` |
| src/database.py:404 | ALTER | `ALTER TABLE loans ADD COLUMN monthly_interest REAL DEFAULT 0` |
| src/database.py:408 | ALTER | `ALTER TABLE loans ADD COLUMN unearned_interest REAL DEFAULT 0` |
| src/database.py:412 | ALTER | `ALTER TABLE loans ADD COLUMN interest_balance REAL DEFAULT 0` |
| src/database.py:416 | ALTER | `ALTER TABLE loans ADD COLUMN import_id INTEGER` |
| src/database.py:421 | ALTER | `ALTER TABLE loans ADD COLUMN is_suspended INTEGER DEFAULT 0` |
| src/database.py:425 | ALTER | `ALTER TABLE loans ADD COLUMN suspend_until TEXT` |
| src/database.py:453 | INSERT | `INSERT INTO loan_suspensions (loan_id, individual_id, loan_ref, start_date, suspend_until, resumed_date, status, created_at) SELECT l.id, l.individ…` |
| src/database.py:688 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_loans_created AFTER INSERT ON loans WHEN NEW.created_at IS NULL BEGIN UPDATE loans SET created_at=datetime('now') …` |
| src/database.py:703 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_loans_updated AFTER UPDATE ON loans BEGIN UPDATE loans SET updated_at=datetime('now') WHERE id=NEW.id; END` |
| src/database.py:722 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_audit_loan_ins AFTER INSERT ON loans BEGIN INSERT INTO audit_log(ts,operation,entity,entity_id,summary) VALUES (da…` |
| src/database.py:725 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_audit_loan_del AFTER DELETE ON loans BEGIN INSERT INTO audit_log(ts,operation,entity,entity_id,summary) VALUES (da…` |
| src/database.py:1075 | INSERT | `INSERT INTO loans ( individual_id, ref, principal, total_amount, balance, installment, monthly_interest, start_date, next_due_date, unearned_intere…` |
| src/database.py:1086 | SELECT | `SELECT * FROM loans WHERE individual_id=? AND status='Active'` |
| src/database.py:1093 | SELECT | `SELECT * FROM loans WHERE individual_id=?` |
| src/database.py:1099 | SELECT | `SELECT * FROM loans WHERE status='Active'` |
| src/database.py:1106 | SELECT | `SELECT COUNT(*) FROM loans WHERE status='Active' AND next_due_date < ?` |
| src/database.py:1111 | SELECT | `SELECT * FROM loans WHERE individual_id=? AND ref=?` |
| src/database.py:1122 | UPDATE | `UPDATE loans SET balance=?, next_due_date=?, status=?` |
| src/database.py:1142 | UPDATE | `UPDATE loans SET monthly_interest = ?, unearned_interest = ? WHERE id = ?` |
| src/database.py:1165 | UPDATE | `UPDATE loans SET total_amount=?, balance=?, installment=?, monthly_interest=?, next_due_date=?` |
| src/database.py:1191 | DELETE | `DELETE FROM loans WHERE individual_id=? AND ref=?` |
| src/database.py:1206 | UPDATE | `UPDATE loans SET is_suspended=1, suspend_until=? WHERE id=?` |
| src/database.py:1212 | SELECT | `SELECT ref, individual_id FROM loans WHERE id=?` |
| src/database.py:1252 | UPDATE | `UPDATE loans SET is_suspended=0, suspend_until=NULL WHERE id=?` |
| src/database.py:1298 | SELECT | `SELECT ref, individual_id FROM loans WHERE id=?` |
| src/database.py:1355 | SELECT | `SELECT COUNT(*) FROM loans WHERE individual_id=? AND status='Active'` |
| src/database.py:1861 | SELECT | `SELECT ref FROM loans` |
| src/database.py:1882 | SELECT | `SELECT ref FROM loans WHERE individual_id=?` |
| src/database.py:1987 | SELECT | `SELECT id FROM loans WHERE individual_id=? AND ref=?` |
| src/database.py:2088 | SELECT | `SELECT COUNT(*) FROM loans` |
| src/database.py:2180 | SELECT | `SELECT ref FROM loans` |
| src/database.py:2187 | SELECT | `SELECT * FROM loans WHERE individual_id IN (<fund>)` |
| src/database.py:2187 | SELECT | `SELECT * FROM loans WHERE individual_id IN (` |
| src/database.py:2227 | INSERT | `INSERT INTO loans ( individual_id, ref, principal, total_amount, balance, installment, start_date, next_due_date, status, monthly_interest, unearne…` |
| src/database.py:2488 | DELETE | `DELETE FROM loans WHERE import_id = ?` |
| src/engine.py:135 | SELECT | `SELECT id, total_amount, installment, monthly_interest FROM loans WHERE individual_id=? AND ref=?` |
| src/engine.py:201 | SELECT | `SELECT individual_id, ref FROM loans WHERE status='Active' ORDER BY individual_id, ref` |
| src/engine.py:224 | UPDATE | `UPDATE loans SET total_amount=?, installment=?, monthly_interest=?, unearned_interest=? WHERE id=?` |
| src/engine.py:755 | UPDATE | `UPDATE loans SET installment = ?, monthly_interest = ? WHERE ref = ? AND individual_id = ?` |
| src/services/loan_service.py:197 | UPDATE | `UPDATE loans SET balance=?, interest_balance=0, unearned_interest=?, next_due_date=?, status='Active' WHERE id=?` |
| src/services/loan_service.py:250 | UPDATE | `UPDATE loans SET next_due_date=? WHERE id=?` |
| src/services/loan_service.py:417 | UPDATE | `UPDATE loans SET balance=?, interest_balance=?, unearned_interest=?, next_due_date=?, status=? WHERE id=? AND individual_id=?` |
| src/services/loan_service.py:441 | UPDATE | `UPDATE loans SET next_due_date=? WHERE id=? AND individual_id=?` |
| src/services/loan_service.py:575 | UPDATE | `UPDATE loans SET next_due_date=? WHERE id=?` |
| src/services/undo_manager.py:241 | UPDATE | `UPDATE loans SET principal = ?, total_amount = ?, balance = ?, installment = ?, interest_balance = ?, unearned_interest = ?, monthly_interest = ?, …` |

### `loan_suspensions` — 10 statements

*CREATE 1 · INSERT 4 · SELECT 3 · UPDATE 2*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:433 | CREATE | `CREATE TABLE IF NOT EXISTS loan_suspensions ( id INTEGER PRIMARY KEY AUTOINCREMENT, loan_id INTEGER, individual_id INTEGER, loan_ref TEXT, start_da…` |
| src/database.py:453 | INSERT | `INSERT INTO loan_suspensions (loan_id, individual_id, loan_ref, start_date, suspend_until, resumed_date, status, created_at) SELECT l.id, l.individ…` |
| src/database.py:1222 | SELECT | `SELECT id FROM loan_suspensions WHERE loan_id=? AND resumed_date IS NULL AND status='active' ORDER BY id DESC LIMIT 1` |
| src/database.py:1228 | UPDATE | `UPDATE loan_suspensions SET suspend_until=? WHERE id=?` |
| src/database.py:1233 | INSERT | `INSERT INTO loan_suspensions (loan_id, individual_id, loan_ref, start_date, suspend_until, resumed_date, status, created_at) VALUES (?, ?, ?, ?, ?,…` |
| src/database.py:1259 | UPDATE | `UPDATE loan_suspensions SET resumed_date=?, status='resumed' WHERE id = (SELECT id FROM loan_suspensions WHERE loan_id=? AND resumed_date IS NULL O…` |
| src/database.py:1275 | SELECT | `SELECT id, loan_id, individual_id, loan_ref, start_date, suspend_until, resumed_date, status FROM loan_suspensions WHERE individual_id=? ORDER BY s…` |
| src/database.py:1303 | INSERT | `INSERT INTO loan_suspensions (loan_id, individual_id, loan_ref, start_date, suspend_until, resumed_date, status, created_at) VALUES (?, ?, ?, ?, ?,…` |
| src/database.py:1976 | SELECT | `SELECT * FROM loan_suspensions` |
| src/database.py:1991 | INSERT | `INSERT INTO loan_suspensions (loan_id, individual_id, loan_ref, start_date, suspend_until, resumed_date, status, created_at) VALUES (?, ?, ?, ?, ?,…` |

### `ledger` — 67 statements

*CREATE 1 · ALTER 11 · UPDATE 16 · DELETE 10 · SELECT 24 · INSERT 5*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:334 | CREATE | `CREATE TABLE IF NOT EXISTS ledger ( id INTEGER PRIMARY KEY AUTOINCREMENT, individual_id INTEGER, date TEXT, event_type TEXT, loan_id TEXT, added RE…` |
| src/database.py:361 | ALTER | `ALTER TABLE ledger ADD COLUMN installment_amount REAL DEFAULT 0` |
| src/database.py:365 | ALTER | `ALTER TABLE ledger ADD COLUMN batch_id TEXT` |
| src/database.py:369 | ALTER | `ALTER TABLE ledger ADD COLUMN interest_amount REAL DEFAULT 0` |
| src/database.py:374 | ALTER | `ALTER TABLE ledger ADD COLUMN is_edited INTEGER DEFAULT 0` |
| src/database.py:379 | ALTER | `ALTER TABLE ledger ADD COLUMN import_id INTEGER` |
| src/database.py:531 | ALTER | `ALTER TABLE ledger ADD COLUMN principal_balance REAL DEFAULT 0` |
| src/database.py:535 | ALTER | `ALTER TABLE ledger ADD COLUMN interest_balance REAL DEFAULT 0` |
| src/database.py:539 | ALTER | `ALTER TABLE ledger ADD COLUMN principal_portion REAL DEFAULT 0` |
| src/database.py:543 | ALTER | `ALTER TABLE ledger ADD COLUMN interest_portion REAL DEFAULT 0` |
| src/database.py:548 | ALTER | `ALTER TABLE ledger ADD COLUMN gross_balance REAL DEFAULT 0` |
| src/database.py:554 | ALTER | `ALTER TABLE ledger ADD COLUMN previous_state TEXT` |
| src/database.py:640 | UPDATE | `UPDATE ledger SET is_edited=1 WHERE is_edited NOT IN (0, 1)` |
| src/database.py:864 | DELETE | `DELETE FROM ledger WHERE individual_id=?` |
| src/database.py:896 | SELECT | `SELECT MIN(date) FROM ( SELECT MIN(date) AS date FROM ledger WHERE individual_id = ? UNION ALL SELECT MIN(date) AS date FROM savings WHERE individu…` |
| src/database.py:915 | SELECT | `SELECT MIN(date) FROM ( SELECT MIN(date) AS date FROM ledger WHERE individual_id IN (<fund>) UNION ALL SELECT MIN(date) AS date FROM savings WHERE …` |
| src/database.py:915 | SELECT | `SELECT MIN(date) FROM ( SELECT MIN(date) AS date FROM ledger WHERE individual_id IN (` |
| src/database.py:927 | SELECT | `SELECT * FROM ledger WHERE individual_id = ?` |
| src/database.py:943 | SELECT | `SELECT * FROM ledger WHERE id=?` |
| src/database.py:955 | INSERT | `INSERT INTO ledger ( individual_id, date, event_type, loan_id, added, deducted, balance, notes, installment_amount, interest_amount, batch_id, prin…` |
| src/database.py:1002 | SELECT | `SELECT COALESCE(MAX(id), 0) FROM ledger` |
| src/database.py:1005 | INSERT | `INSERT INTO ledger ( individual_id, date, event_type, loan_id, added, deducted, balance, notes, installment_amount, interest_amount, batch_id, prin…` |
| src/database.py:1015 | SELECT | `SELECT id FROM ledger WHERE id > ? ORDER BY id` |
| src/database.py:1048 | UPDATE | `UPDATE ledger SET <fund> WHERE id=?` |
| src/database.py:1048 | UPDATE | `UPDATE ledger SET` |
| src/database.py:1054 | UPDATE | `UPDATE ledger SET balance=? WHERE id=?` |
| src/database.py:1060 | UPDATE | `UPDATE ledger SET balance=?, principal_balance=?, interest_balance=?, gross_balance=? WHERE id=?` |
| src/database.py:1069 | DELETE | `DELETE FROM ledger WHERE id=?` |
| src/database.py:1155 | UPDATE | `UPDATE ledger SET is_edited = 0 WHERE individual_id = ? AND loan_id = ? AND event_type = 'Interest Earned' AND date > ?` |
| src/database.py:1190 | DELETE | `DELETE FROM ledger WHERE individual_id=? AND loan_id=?` |
| src/database.py:1324 | SELECT | `SELECT COUNT(*) FROM ledger WHERE individual_id=? AND loan_id=? AND event_type IN ('Repayment', 'Loan Buyoff') AND date >= ? AND date <= ?` |
| src/database.py:1361 | DELETE | `DELETE FROM ledger WHERE batch_id=?` |
| src/database.py:1892 | SELECT | `SELECT count(*) FROM ledger WHERE individual_id=?` |
| src/database.py:2090 | SELECT | `SELECT COUNT(*) FROM ledger` |
| src/database.py:2253 | SELECT | `SELECT * FROM ledger WHERE individual_id IN (<fund>)<fund>` |
| src/database.py:2253 | SELECT | `SELECT * FROM ledger WHERE individual_id IN (` |
| src/database.py:2294 | INSERT | `INSERT INTO ledger ( individual_id, date, event_type, loan_id, added, deducted, balance, notes, installment_amount, batch_id, interest_amount, prin…` |
| src/database.py:2485 | DELETE | `DELETE FROM ledger WHERE import_id = ?` |
| src/engine.py:142 | SELECT | `SELECT COUNT(*) FROM ledger WHERE individual_id=? AND loan_id=? AND event_type IN ('Loan Top-Up','Loan Buyoff')` |
| src/engine.py:148 | SELECT | `SELECT COUNT(*) FROM ledger WHERE individual_id=? AND loan_id=? AND event_type='Repayment' AND is_edited<>0` |
| src/engine.py:153 | SELECT | `SELECT notes FROM ledger WHERE individual_id=? AND loan_id=? AND event_type='Loan Issued' ORDER BY date, id LIMIT 1` |
| src/engine.py:178 | SELECT | `SELECT COUNT(*) FROM ledger WHERE individual_id=? AND loan_id=? AND event_type='Interest Earned' AND is_edited=1` |
| src/engine.py:230 | UPDATE | `UPDATE ledger SET is_edited=0 WHERE individual_id=? AND loan_id=? AND event_type='Interest Earned'` |
| src/reports.py:69 | SELECT | `SELECT MIN(date) FROM (SELECT date FROM ledger UNION ALL SELECT date FROM savings)` |
| src/services/balance_calculator.py:476 | UPDATE | `UPDATE ledger SET added = ?, interest_amount = ? WHERE id = ?` |
| src/services/balance_calculator.py:510 | UPDATE | `UPDATE ledger SET deducted = ? WHERE id = ?` |
| src/services/balance_calculator.py:523 | UPDATE | `UPDATE ledger SET deducted = ? WHERE id = ?` |
| src/services/balance_calculator.py:547 | UPDATE | `UPDATE ledger SET added = ?, interest_amount = ? WHERE id = ?` |
| src/services/balance_calculator.py:577 | UPDATE | `UPDATE ledger SET principal_portion = ?, interest_portion = ? WHERE id = ?` |
| src/services/balance_calculator.py:590 | UPDATE | `UPDATE ledger SET principal_balance = ?, interest_balance = ? WHERE id = ?` |
| src/services/gl_service.py:632 | SELECT | `SELECT id, date, event_type, added, deducted, principal_portion, interest_portion, interest_amount FROM ledger ORDER BY date, id` |
| src/services/gl_service.py:684 | SELECT | `SELECT date, event_type, added, deducted, principal_portion, interest_portion, interest_amount FROM ledger WHERE id=?` |
| src/services/loan_service.py:152 | SELECT | `SELECT COUNT(*) FROM ledger WHERE individual_id=? AND loan_id=? AND event_type IN ('Loan Top-Up', 'Loan Buyoff')` |
| src/services/loan_service.py:176 | SELECT | `SELECT MAX(date) FROM ledger WHERE individual_id=? AND loan_id=? AND event_type='Repayment'` |
| src/services/loan_service.py:182 | DELETE | `DELETE FROM ledger WHERE individual_id=? AND loan_id=? AND event_type IN ('Repayment', 'Interest Earned')` |
| src/services/loan_service.py:189 | SELECT | `SELECT MIN(date) FROM ledger WHERE individual_id=? AND loan_id=? AND event_type='Loan Issued'` |
| src/services/loan_service.py:502 | SELECT | `SELECT DISTINCT individual_id FROM ledger WHERE batch_id=?` |
| src/services/loan_service.py:506 | SELECT | `SELECT DISTINCT loan_id, individual_id FROM ledger WHERE batch_id=?` |
| src/services/transaction_manager.py:362 | UPDATE | `UPDATE ledger SET added = ?, is_edited = 1 WHERE individual_id = ? AND loan_id = ? AND event_type = 'Interest Earned' AND date >= ?` |
| src/services/transaction_manager.py:369 | UPDATE | `UPDATE ledger SET interest_amount = ? WHERE id = ?` |
| src/services/undo_manager.py:219 | DELETE | `DELETE FROM ledger WHERE id = ?` |
| src/services/undo_manager.py:220 | DELETE | `DELETE FROM ledger WHERE id = ?` |
| src/services/undo_manager.py:265 | INSERT | `INSERT INTO ledger ( id, individual_id, date, event_type, added, deducted, balance, loan_id, notes, interest_amount, principal_balance, interest_ba…` |
| src/services/undo_manager.py:319 | DELETE | `DELETE FROM ledger WHERE id = ?` |
| src/services/undo_manager.py:378 | DELETE | `DELETE FROM ledger WHERE id = ?` |
| src/services/undo_manager.py:394 | INSERT | `INSERT INTO ledger (id, individual_id, date, event_type, added, deducted, balance, loan_id, notes, interest_amount, principal_amount, linked_trans_…` |
| src/views/ledger.py:1348 | UPDATE | `UPDATE ledger SET date = ? WHERE id = ?` |

### `savings` — 32 statements

*CREATE 3 · ALTER 2 · SELECT 15 · DELETE 4 · INSERT 2 · UPDATE 6*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:467 | CREATE | `CREATE TABLE IF NOT EXISTS savings ( id INTEGER PRIMARY KEY AUTOINCREMENT, individual_id INTEGER, date TEXT, transaction_type TEXT, -- Deposit, Wit…` |
| src/database.py:482 | ALTER | `ALTER TABLE savings ADD COLUMN import_id INTEGER` |
| src/database.py:691 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_savings_created AFTER INSERT ON savings WHEN NEW.created_at IS NULL BEGIN UPDATE savings SET created_at=datetime('…` |
| src/database.py:896 | SELECT | `SELECT MIN(date) FROM ( SELECT MIN(date) AS date FROM ledger WHERE individual_id = ? UNION ALL SELECT MIN(date) AS date FROM savings WHERE individu…` |
| src/database.py:915 | SELECT | `SELECT MIN(date) FROM ( SELECT MIN(date) AS date FROM ledger WHERE individual_id IN (<fund>) UNION ALL SELECT MIN(date) AS date FROM savings WHERE …` |
| src/database.py:1367 | DELETE | `DELETE FROM savings WHERE batch_id=?` |
| src/database.py:1375 | CREATE | `CREATE TABLE IF NOT EXISTS savings ( id INTEGER PRIMARY KEY AUTOINCREMENT, individual_id INTEGER, date TEXT, transaction_type TEXT, amount REAL, ba…` |
| src/database.py:1389 | ALTER | `ALTER TABLE savings ADD COLUMN batch_id TEXT` |
| src/database.py:1408 | INSERT | `INSERT INTO savings (individual_id, date, transaction_type, amount, balance, notes, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?)` |
| src/database.py:1421 | SELECT | `SELECT balance FROM savings WHERE individual_id=? ORDER BY date DESC, id DESC LIMIT 1` |
| src/database.py:1435 | SELECT | `SELECT id, transaction_type, amount FROM savings WHERE individual_id=? ORDER BY date ASC, id ASC` |
| src/database.py:1451 | UPDATE | `UPDATE savings SET balance=? WHERE id=?` |
| src/database.py:1459 | SELECT | `SELECT * FROM savings WHERE individual_id = ?` |
| src/database.py:1475 | DELETE | `DELETE FROM savings WHERE id=?` |
| src/database.py:1481 | UPDATE | `UPDATE savings SET date=?, amount=?, notes=? WHERE id=?` |
| src/database.py:1489 | SELECT | `SELECT id, transaction_type, amount FROM savings WHERE individual_id=? ORDER BY id` |
| src/database.py:1499 | UPDATE | `UPDATE savings SET balance=? WHERE id=?` |
| src/database.py:1897 | SELECT | `SELECT count(*) FROM savings WHERE individual_id=?` |
| src/database.py:2096 | SELECT | `SELECT COUNT(*) FROM savings` |
| src/database.py:2335 | SELECT | `SELECT * FROM savings LIMIT 0` |
| src/database.py:2345 | SELECT | `SELECT * FROM savings<fund>` |
| src/database.py:2345 | SELECT | `SELECT * FROM savings` |
| src/database.py:2361 | INSERT | `INSERT INTO savings (individual_id, date, transaction_type, amount, balance, notes, import_id) VALUES (?, ?, ?, ?, ?, ?, ?)` |
| src/database.py:2482 | DELETE | `DELETE FROM savings WHERE import_id = ?` |
| src/reports.py:69 | SELECT | `SELECT MIN(date) FROM (SELECT date FROM ledger UNION ALL SELECT date FROM savings)` |
| src/services/gl_service.py:636 | SELECT | `SELECT id, date, transaction_type, amount FROM savings ORDER BY date, id` |
| src/services/gl_service.py:697 | SELECT | `SELECT date, transaction_type, amount FROM savings WHERE id=?` |
| src/services/savings_service.py:147 | UPDATE | `UPDATE savings SET balance=? WHERE id=?` |
| src/services/savings_service.py:279 | SELECT | `SELECT DISTINCT individual_id FROM savings WHERE batch_id=?` |
| src/views/ledger.py:2349 | UPDATE | `UPDATE savings SET date=?, amount=?, notes=? WHERE id=?` |
| src/views/ledger.py:2362 | UPDATE | `UPDATE savings SET date=?, amount=?, notes=? WHERE id=?` |
| src/views/ledger.py:2445 | DELETE | `DELETE FROM savings WHERE individual_id=?` |

### `christmas_savings` — 2 statements

*CREATE 1 · SELECT 1*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:694 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_xmas_created AFTER INSERT ON christmas_savings WHEN NEW.created_at IS NULL BEGIN UPDATE christmas_savings SET crea…` |
| src/services/gl_service.py:638 | SELECT | `SELECT id, date, transaction_type, amount FROM christmas_savings ORDER BY date, id` |

### `benevolent_ledger` — 2 statements

*CREATE 1 · SELECT 1*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:697 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_benev_created AFTER INSERT ON benevolent_ledger WHEN NEW.created_at IS NULL BEGIN UPDATE benevolent_ledger SET cre…` |
| src/services/gl_service.py:640 | SELECT | `SELECT id, date, transaction_type, amount FROM benevolent_ledger ORDER BY date, id` |

### christmas_savings / benevolent_ledger (generic `fund_*` API — table name interpolated from a whitelist) — 21 statements

*SELECT 10 · DELETE 4 · CREATE 1 · ALTER 1 · INSERT 2 · UPDATE 3*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:160 | SELECT | `SELECT o.individual_id, COUNT(*), <fund> FROM <fund> o LEFT JOIN individuals i ON i.id = o.individual_id WHERE i.id IS NULL GROUP BY o.individual_i…` |
| src/database.py:184 | SELECT | `SELECT o.* FROM <fund> o LEFT JOIN individuals i ON i.id = o.individual_id WHERE i.id IS NULL` |
| src/database.py:206 | DELETE | `DELETE FROM <fund> WHERE individual_id IS NULL OR individual_id NOT IN (SELECT id FROM individuals)` |
| src/database.py:491 | CREATE | `CREATE TABLE IF NOT EXISTS <fund> ( id INTEGER PRIMARY KEY AUTOINCREMENT, individual_id INTEGER, date TEXT, transaction_type TEXT, -- Deposit \| Wi…` |
| src/database.py:668 | ALTER | `ALTER TABLE <fund> ADD COLUMN <fund> TEXT` |
| src/database.py:1515 | SELECT | `SELECT balance FROM <fund> WHERE individual_id=? ORDER BY date DESC, id DESC LIMIT 1` |
| src/database.py:1530 | INSERT | `INSERT INTO <fund> (individual_id, date, transaction_type, amount, balance, notes, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?)` |
| src/database.py:1539 | SELECT | `SELECT * FROM <fund> WHERE individual_id=? ORDER BY date, id` |
| src/database.py:1545 | SELECT | `SELECT id, transaction_type, amount FROM <fund> WHERE individual_id=? ORDER BY date ASC, id ASC` |
| src/database.py:1550 | UPDATE | `UPDATE <fund> SET balance=? WHERE id=?` |
| src/database.py:1555 | DELETE | `DELETE FROM <fund> WHERE id=?` |
| src/database.py:1560 | DELETE | `DELETE FROM <fund> WHERE batch_id=?` |
| src/database.py:1565 | DELETE | `DELETE FROM <fund> WHERE individual_id=?` |
| src/database.py:1571 | SELECT | `SELECT id, individual_id, date, transaction_type, amount, notes FROM <fund> WHERE id=?` |
| src/database.py:1581 | UPDATE | `UPDATE <fund> SET date=?, amount=?, notes=? WHERE id=?` |
| src/database.py:1932 | SELECT | `SELECT id, transaction_type, amount FROM <fund> WHERE individual_id=? ORDER BY date, id` |
| src/database.py:1935 | UPDATE | `UPDATE <fund> SET balance=? WHERE id=?` |
| src/database.py:1941 | SELECT | `SELECT * FROM <fund>` |
| src/database.py:1950 | INSERT | `INSERT INTO <fund> (individual_id, date, transaction_type, amount, balance, notes, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?)` |
| src/services/benevolent_service.py:141 | SELECT | `SELECT individual_id, MIN(date) FROM <fund> WHERE batch_id=? GROUP BY individual_id` |
| src/services/christmas_service.py:151 | SELECT | `SELECT DISTINCT individual_id FROM <fund> WHERE batch_id=?` |

### `benevolent_accounts` — 7 statements

*CREATE 1 · SELECT 2 · UPDATE 2 · INSERT 2*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:505 | CREATE | `CREATE TABLE IF NOT EXISTS benevolent_accounts ( id INTEGER PRIMARY KEY AUTOINCREMENT, individual_id INTEGER UNIQUE, monthly_amount REAL DEFAULT 0,…` |
| src/database.py:1589 | SELECT | `SELECT id, individual_id, monthly_amount, start_date, next_due_date, active FROM benevolent_accounts WHERE individual_id=?` |
| src/database.py:1599 | UPDATE | `UPDATE benevolent_accounts SET monthly_amount=?, start_date=?, next_due_date=?, active=1 WHERE individual_id=?` |
| src/database.py:1603 | INSERT | `INSERT INTO benevolent_accounts (individual_id, monthly_amount, start_date, next_due_date, active) VALUES (?, ?, ?, ?, 1)` |
| src/database.py:1609 | UPDATE | `UPDATE benevolent_accounts SET next_due_date=? WHERE individual_id=?` |
| src/database.py:1960 | SELECT | `SELECT * FROM benevolent_accounts` |
| src/database.py:1968 | INSERT | `INSERT OR REPLACE INTO benevolent_accounts (individual_id, monthly_amount, start_date, next_due_date, active) VALUES (?, ?, ?, ?, ?)` |

### `chart_of_accounts` — 9 statements

*CREATE 1 · INSERT 1 · SELECT 7*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:587 | CREATE | `CREATE TABLE IF NOT EXISTS chart_of_accounts ( id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, type TEXT NOT N…` |
| src/database.py:631 | INSERT | `INSERT OR IGNORE INTO chart_of_accounts (code, name, type, normal_balance) VALUES (?, ?, ?, ?)` |
| src/services/gl_service.py:68 | SELECT | `SELECT code FROM chart_of_accounts` |
| src/services/gl_service.py:75 | SELECT | `SELECT code, name, type, normal_balance, is_active, description FROM chart_of_accounts` |
| src/services/gl_service.py:215 | SELECT | `SELECT normal_balance FROM chart_of_accounts WHERE code=?` |
| src/services/gl_service.py:230 | SELECT | `SELECT a.code, a.name, a.type, COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) AS net FROM chart_of_accounts a LEFT JOIN journal_lines l ON l.…` |
| src/services/gl_service.py:271 | SELECT | `SELECT a.code, a.name, COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) AS net FROM chart_of_accounts a LEFT JOIN journal_lines l ON l.account_…` |
| src/services/gl_service.py:381 | SELECT | `SELECT l.account_code, a.name, l.debit, l.credit FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id JOIN chart_of_accounts a ON a.cod…` |
| src/services/gl_service.py:439 | SELECT | `SELECT normal_balance FROM chart_of_accounts WHERE code=?` |

### `journal_entries` — 19 statements

*CREATE 2 · INSERT 1 · SELECT 10 · UPDATE 2 · DELETE 4*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:598 | CREATE | `CREATE TABLE IF NOT EXISTS journal_entries ( id INTEGER PRIMARY KEY AUTOINCREMENT, entry_date TEXT NOT NULL, memo TEXT, source TEXT, -- manual\|loa…` |
| src/database.py:626 | CREATE | `CREATE INDEX IF NOT EXISTS idx_journal_entries_source ON journal_entries(source, source_ref)` |
| src/services/gl_service.py:139 | INSERT | `INSERT INTO journal_entries (entry_date, memo, source, source_ref, status, created_at, created_by) VALUES (?, ?, ?, ?, 'posted', ?, ?)` |
| src/services/gl_service.py:160 | SELECT | `SELECT entry_date, memo FROM journal_entries WHERE id=?` |
| src/services/gl_service.py:175 | UPDATE | `UPDATE journal_entries SET status='reversed' WHERE id=?` |
| src/services/gl_service.py:176 | UPDATE | `UPDATE journal_entries SET status='reversal', reversal_of=? WHERE id=?` |
| src/services/gl_service.py:184 | SELECT | `SELECT id FROM journal_entries WHERE source=? AND source_ref=? LIMIT 1` |
| src/services/gl_service.py:192 | SELECT | `SELECT COUNT(*) FROM journal_entries` |
| src/services/gl_service.py:205 | SELECT | `SELECT COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id WHERE l.account_code = ?` |
| src/services/gl_service.py:230 | SELECT | `SELECT a.code, a.name, a.type, COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) AS net FROM chart_of_accounts a LEFT JOIN journal_lines l ON l.…` |
| src/services/gl_service.py:271 | SELECT | `SELECT a.code, a.name, COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) AS net FROM chart_of_accounts a LEFT JOIN journal_lines l ON l.account_…` |
| src/services/gl_service.py:373 | SELECT | `SELECT COALESCE(SUM(l.debit - l.credit),0) FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id WHERE l.account_code = ? AND e.entry_da…` |
| src/services/gl_service.py:381 | SELECT | `SELECT l.account_code, a.name, l.debit, l.credit FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id JOIN chart_of_accounts a ON a.cod…` |
| src/services/gl_service.py:427 | SELECT | `SELECT e.id, e.entry_date, e.memo, e.source, e.status, COALESCE(SUM(l.debit), 0) AS amount FROM journal_entries e LEFT JOIN journal_lines l ON l.en…` |
| src/services/gl_service.py:443 | SELECT | `SELECT e.id, e.entry_date, e.memo, e.source, l.debit, l.credit FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id WHERE l.account_cod…` |
| src/services/gl_service.py:597 | DELETE | `DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE source IN (<fund>))` |
| src/services/gl_service.py:597 | DELETE | `DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE source IN (` |
| src/services/gl_service.py:602 | DELETE | `DELETE FROM journal_entries WHERE source IN (<fund>)` |
| src/services/gl_service.py:602 | DELETE | `DELETE FROM journal_entries WHERE source IN (` |

### `journal_lines` — 14 statements

*CREATE 3 · INSERT 1 · SELECT 8 · DELETE 2*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:611 | CREATE | `CREATE TABLE IF NOT EXISTS journal_lines ( id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id INTEGER NOT NULL, account_code TEXT NOT NULL, debit REAL …` |
| src/database.py:624 | CREATE | `CREATE INDEX IF NOT EXISTS idx_journal_lines_entry ON journal_lines(entry_id)` |
| src/database.py:625 | CREATE | `CREATE INDEX IF NOT EXISTS idx_journal_lines_account ON journal_lines(account_code)` |
| src/services/gl_service.py:147 | INSERT | `INSERT INTO journal_lines (entry_id, account_code, debit, credit, line_memo) VALUES (?, ?, ?, ?, ?)` |
| src/services/gl_service.py:164 | SELECT | `SELECT account_code, debit, credit FROM journal_lines WHERE entry_id=?` |
| src/services/gl_service.py:205 | SELECT | `SELECT COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id WHERE l.account_code = ?` |
| src/services/gl_service.py:230 | SELECT | `SELECT a.code, a.name, a.type, COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) AS net FROM chart_of_accounts a LEFT JOIN journal_lines l ON l.…` |
| src/services/gl_service.py:271 | SELECT | `SELECT a.code, a.name, COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) AS net FROM chart_of_accounts a LEFT JOIN journal_lines l ON l.account_…` |
| src/services/gl_service.py:373 | SELECT | `SELECT COALESCE(SUM(l.debit - l.credit),0) FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id WHERE l.account_code = ? AND e.entry_da…` |
| src/services/gl_service.py:381 | SELECT | `SELECT l.account_code, a.name, l.debit, l.credit FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id JOIN chart_of_accounts a ON a.cod…` |
| src/services/gl_service.py:427 | SELECT | `SELECT e.id, e.entry_date, e.memo, e.source, e.status, COALESCE(SUM(l.debit), 0) AS amount FROM journal_entries e LEFT JOIN journal_lines l ON l.en…` |
| src/services/gl_service.py:443 | SELECT | `SELECT e.id, e.entry_date, e.memo, e.source, l.debit, l.credit FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id WHERE l.account_cod…` |
| src/services/gl_service.py:597 | DELETE | `DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE source IN (<fund>))` |
| src/services/gl_service.py:597 | DELETE | `DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE source IN (` |

### `general_ledger` — 2 statements

*CREATE 1 · SELECT 1*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:570 | CREATE | `CREATE TABLE IF NOT EXISTS general_ledger ( id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, category TEXT, type TEXT, amount REAL, notes TEXT, bat…` |
| src/services/gl_service.py:767 | SELECT | `SELECT id, date, category, type, amount, notes FROM general_ledger ORDER BY date, id` |

### `settings` — 3 statements

*CREATE 1 · SELECT 1 · INSERT 1*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:559 | CREATE | `CREATE TABLE IF NOT EXISTS settings ( key TEXT PRIMARY KEY, value TEXT )` |
| src/database.py:1616 | SELECT | `SELECT value FROM settings WHERE key=?` |
| src/database.py:1623 | INSERT | `INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)` |

### `import_history` — 5 statements

*CREATE 1 · INSERT 1 · UPDATE 1 · SELECT 1 · DELETE 1*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:518 | CREATE | `CREATE TABLE IF NOT EXISTS import_history ( id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, source_file TEXT, details TEXT, item_count INTEGER )` |
| src/database.py:2059 | INSERT | `INSERT INTO import_history (timestamp, source_file, details, item_count) VALUES (?, ?, ?, ?)` |
| src/database.py:2429 | UPDATE | `UPDATE import_history SET details = ?, item_count = ? WHERE id = ?` |
| src/database.py:2459 | SELECT | `SELECT id, timestamp, source_file, details, item_count FROM import_history ORDER BY id DESC` |
| src/database.py:2497 | DELETE | `DELETE FROM import_history WHERE id = ?` |

### `audit_log` — 8 statements

*CREATE 7 · SELECT 1*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:673 | CREATE | `CREATE TABLE IF NOT EXISTS audit_log ( id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, operation TEXT, -- INSERT \| UPDATE \| DELETE entity TEXT, --…` |
| src/database.py:683 | CREATE | `CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id)` |
| src/database.py:706 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_audit_ind_ins AFTER INSERT ON individuals BEGIN INSERT INTO audit_log(ts,operation,entity,entity_id,summary) VALUE…` |
| src/database.py:710 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_audit_ind_upd AFTER UPDATE ON individuals WHEN NEW.name IS NOT OLD.name OR NEW.phone IS NOT OLD.phone OR NEW.email…` |
| src/database.py:718 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_audit_ind_del AFTER DELETE ON individuals BEGIN INSERT INTO audit_log(ts,operation,entity,entity_id,summary) VALUE…` |
| src/database.py:722 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_audit_loan_ins AFTER INSERT ON loans BEGIN INSERT INTO audit_log(ts,operation,entity,entity_id,summary) VALUES (da…` |
| src/database.py:725 | CREATE | `CREATE TRIGGER IF NOT EXISTS trg_audit_loan_del AFTER DELETE ON loans BEGIN INSERT INTO audit_log(ts,operation,entity,entity_id,summary) VALUES (da…` |
| src/database.py:738 | SELECT | `SELECT ts, operation, entity, entity_id, summary FROM audit_log` |

### sqlite_master (schema introspection) — 5 statements

*SELECT 5*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:133 | SELECT | `SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name` |
| src/database.py:1926 | SELECT | `SELECT name FROM sqlite_master WHERE type='table' AND name=?` |
| src/database.py:2332 | SELECT | `SELECT name FROM sqlite_master WHERE type='table' AND name='savings'` |
| src/database.py:2527 | SELECT | `SELECT name FROM sqlite_master LIMIT 1` |
| src/database.py:2532 | SELECT | `SELECT name FROM sqlite_master WHERE type='table' AND name='individuals'` |

### No table — connection/transaction control — 21 statements

*PRAGMA 10 · BEGIN 2 · SAVEPOINT 5 · ROLLBACK 2 · RELEASE 2*

| File:line | Verb | Statement |
|---|---|---|
| src/database.py:59 | PRAGMA | `PRAGMA busy_timeout = 5000` |
| src/database.py:61 | PRAGMA | `PRAGMA foreign_key_check` |
| src/database.py:72 | PRAGMA | `PRAGMA foreign_keys = ON` |
| src/database.py:82 | PRAGMA | `PRAGMA quick_check` |
| src/database.py:139 | PRAGMA | `PRAGMA table_info(<fund>)` |
| src/database.py:139 | PRAGMA | `PRAGMA table_info(` |
| src/database.py:157 | PRAGMA | `PRAGMA table_info(<fund>)` |
| src/database.py:157 | PRAGMA | `PRAGMA table_info(` |
| src/database.py:2054 | BEGIN | `BEGIN` |
| src/database.py:2055 | SAVEPOINT | `SAVEPOINT import_start` |
| src/database.py:2169 | SAVEPOINT | `SAVEPOINT individuals_imported` |
| src/database.py:2172 | ROLLBACK | `ROLLBACK TO import_start` |
| src/database.py:2314 | SAVEPOINT | `SAVEPOINT loans_imported` |
| src/database.py:2318 | ROLLBACK | `ROLLBACK TO individuals_imported` |
| src/database.py:2384 | SAVEPOINT | `SAVEPOINT savings_imported` |
| src/database.py:2421 | SAVEPOINT | `SAVEPOINT aux_imported` |
| src/database.py:2433 | RELEASE | `RELEASE import_start` |
| src/database.py:2477 | BEGIN | `BEGIN` |
| src/database.py:2537 | PRAGMA | `PRAGMA table_info(individuals)` |
| src/views/dashboard.py:2301 | PRAGMA | `PRAGMA foreign_keys` |
| src/views/treasury.py:510 | RELEASE | `release` |
