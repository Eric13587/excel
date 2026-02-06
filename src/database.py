"""Database management module for LoanMaster."""
import sqlite3
import pandas as pd
from datetime import datetime
from contextlib import contextmanager

from src.exceptions import DatabaseError, TransactionError
from src.data_structures import StatementData


class DatabaseManager:
    """Handles all SQLite database operations."""
    
    def __init__(self, db_name="loan_master.db"):
        self.db_name = db_name
        self.conn = sqlite3.connect(db_name)
        self._closed = False
        self.create_tables()
    
    def close(self):
        """Close the database connection."""
        if self.conn and not self._closed:
            self.conn.close()
            self._closed = True
    
    def __del__(self):
        """Ensure connection is closed on garbage collection."""
        self.close()
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with proper cleanup."""
        self.close()
        return False
    
    def begin_transaction(self):
        """Start a manual transaction."""
        # SQLite starts implicitly, but we can ensure previous are committed or just pass.
        # For strictness we could execute "BEGIN", but Python sqlite3 context handles this.
        # We'll rely on the fact that the next execute() starts a transaction.
        pass

    def commit_transaction(self):
        """Commit the current transaction."""
        self.conn.commit()

    def rollback_transaction(self):
        """Rollback the current transaction."""
        self.conn.rollback()

    @contextmanager
    def transaction(self):
        """Context manager for database transactions with automatic rollback on failure.
        
        Usage:
            with db.transaction():
                db.add_individual(...)
                db.add_loan_record(...)
        
        If any exception occurs, the transaction is rolled back.
        """
        try:
            yield
            self.conn.commit()
        except sqlite3.Error as e:
            self.conn.rollback()
            raise TransactionError(f"Transaction failed: {str(e)}")
        except Exception as e:
            self.conn.rollback()
            raise

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS individuals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT,
                email TEXT,
                default_deduction REAL DEFAULT 0,
                created_at TEXT
            )
        """)
        # Migration for existing table
        try:
            cursor.execute("ALTER TABLE individuals ADD COLUMN default_deduction REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
            
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                individual_id INTEGER,
                date TEXT,
                event_type TEXT,
                loan_id TEXT,
                added REAL,
                deducted REAL,
                balance REAL,
                notes TEXT,
                installment_amount REAL DEFAULT 0,
                batch_id TEXT,
                interest_amount REAL DEFAULT 0,
                principal_balance REAL DEFAULT 0,
                interest_balance REAL DEFAULT 0,
                gross_balance REAL DEFAULT 0,
                principal_portion REAL DEFAULT 0,
                interest_portion REAL DEFAULT 0,
                previous_state TEXT,
                is_edited INTEGER DEFAULT 0,
                FOREIGN KEY(individual_id) REFERENCES individuals(id)
            )
        """)
        # Migrations for ledger
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN installment_amount REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass 
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN batch_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN interest_amount REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN is_edited INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                individual_id INTEGER,
                ref TEXT,
                principal REAL,
                total_amount REAL,
                balance REAL,
                installment REAL,
                start_date TEXT,
                next_due_date TEXT,
                status TEXT DEFAULT 'Active',
                monthly_interest REAL DEFAULT 0,
                unearned_interest REAL DEFAULT 0,
                interest_balance REAL DEFAULT 0,
                FOREIGN KEY(individual_id) REFERENCES individuals(id)
            )
        """)
        try:
            cursor.execute("ALTER TABLE loans ADD COLUMN monthly_interest REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        
        # Migrations for Segregated Interest Model
        try:
            cursor.execute("ALTER TABLE loans ADD COLUMN unearned_interest REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE loans ADD COLUMN interest_balance REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
            
        # Migrations for Ledger Splits
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN principal_balance REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN interest_balance REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN principal_portion REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN interest_portion REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
            
        try:
             cursor.execute("ALTER TABLE ledger ADD COLUMN gross_balance REAL DEFAULT 0")
        except sqlite3.OperationalError:
             pass

        # State Management Migration
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN previous_state TEXT")
        except sqlite3.OperationalError:
            pass

        # Settings Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

            
        self.conn.commit()

    # Individual operations
    def add_individual(self, name, phone, email):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO individuals (name, phone, email, created_at) VALUES (?, ?, ?, ?)",
                       (name, phone, email, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self.conn.commit()
        return cursor.lastrowid

    def get_individuals(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM individuals")
        return cursor.fetchall()

    def get_individual_name(self, id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM individuals WHERE id=?", (id,))
        row = cursor.fetchone()
        return row[0] if row else f"Individual {id}"

    def get_individual(self, id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM individuals WHERE id=?", (id,))
        row = cursor.fetchone()
        if row:
            cols = [description[0] for description in cursor.description]
            return dict(zip(cols, row))
        return None

    def update_individual(self, id, name, phone, email):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE individuals SET name=?, phone=?, email=? WHERE id=?", (name, phone, email, id))
        self.conn.commit()

    def update_individual_deduction(self, id, amount):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE individuals SET default_deduction=? WHERE id=?", (amount, id))
        self.conn.commit()

    def delete_individual(self, id):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM ledger WHERE individual_id=?", (id,))
        cursor.execute("DELETE FROM individuals WHERE id=?", (id,))
        self.conn.commit()

    def get_statement_data(self, individual_id, start_date=None, end_date=None) -> StatementData:
        """Fetch all data required for statement generation in one go."""
        individual = self.get_individual(individual_id)
        # Fetch FULL history for accurate running balance calculation
        ledger_df = self.get_ledger(individual_id) 
        # Savings history also needed fully if we want accurate running balances, 
        # but let's stick to ledger fix first. Actually, savings row balance works by snapshot usually? 
        # Let's fetch full savings too to be safe for running balance if needed.
        # But UI savings table shows just current balance? 
        # Verify get_savings_transactions implementation first? 
        # Let's just fetch full ledger as planned for Gross Balance.
        savings_df = self.get_savings_transactions(individual_id, start_date, end_date)
        savings_balance = self.get_savings_balance(individual_id)
        active_loans = self.get_active_loans(individual_id)
        
        return StatementData(
            individual=individual,
            ledger_df=ledger_df,
            savings_df=savings_df,
            savings_balance=savings_balance,
            active_loans=active_loans
        )

    # Ledger operations
    def get_ledger(self, individual_id, start_date=None, end_date=None):
        query = "SELECT * FROM ledger WHERE individual_id = ?"
        params = [individual_id]
        
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
            
        query += " ORDER BY date, id"
            
        return pd.read_sql_query(query, self.conn, params=tuple(params))

    def get_transaction(self, trans_id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM ledger WHERE id=?", (trans_id,))
        row = cursor.fetchone()
        if row:
            cols = [description[0] for description in cursor.description]
            return dict(zip(cols, row))
        return None

    def add_transaction(self, individual_id, date, event_type, loan_id, added, deducted, balance, notes, 
                       installment_amount=0, interest_amount=0, batch_id=None, 
                       principal_balance=0, interest_balance=0, principal_portion=0, interest_portion=0,
                       previous_state=None):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO ledger (
                individual_id, date, event_type, loan_id, added, deducted, balance, notes, 
                installment_amount, interest_amount, batch_id,
                principal_balance, interest_balance, principal_portion, interest_portion, previous_state
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (individual_id, date, event_type, loan_id, added, deducted, balance, notes, 
              installment_amount, interest_amount, batch_id,
              principal_balance, interest_balance, principal_portion, interest_portion, previous_state))
        self.conn.commit()

    def bulk_insert_transactions(self, transactions):
        """Bulk insert multiple transactions into the ledger."""
        if not transactions:
            return
            
        cursor = self.conn.cursor()
        
        vals = []
        for tx in transactions:
            vals.append((
                tx.get('individual_id'),
                tx.get('date'),
                tx.get('event_type'),
                tx.get('loan_id'),
                tx.get('added', 0),
                tx.get('deducted', 0),
                tx.get('balance', 0),
                tx.get('notes', ""),
                tx.get('installment_amount', 0),
                tx.get('interest_amount', 0),
                tx.get('batch_id'),
                tx.get('principal_balance', 0),
                tx.get('interest_balance', 0),
                tx.get('principal_portion', 0),
                tx.get('interest_portion', 0),
                tx.get('previous_state', None)
            ))
            
        cursor.executemany("""
            INSERT INTO ledger (
                individual_id, date, event_type, loan_id, added, deducted, balance, notes, 
                installment_amount, interest_amount, batch_id, 
                principal_balance, interest_balance, principal_portion, interest_portion, previous_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, vals)
        self.conn.commit()

    def update_transaction(self, id, date, added, deducted, notes, principal_portion=None, interest_portion=None, mark_edited=False, interest_amount=None):
        """Update a transaction with parameterized queries (SQL injection safe)."""
        cursor = self.conn.cursor()
        id = int(id)  # Ensure native int for SQLite compatibility
        
        # Build query dynamically using parameterized approach
        set_clauses = ["date=?", "added=?", "deducted=?", "notes=?"]
        params = [date, added, deducted, notes]
        
        # Add optional columns
        if principal_portion is not None and interest_portion is not None:
            set_clauses.extend(["principal_portion=?", "interest_portion=?"])
            params.extend([principal_portion, interest_portion])
        
        # Hysteresis Fix: Store the Target Amount in `is_edited` to preserve user intent
        if mark_edited:
            # Use the value of deducted as the "Anchor Value".
            val = deducted if deducted > 1 else 1
            set_clauses.append("is_edited=?")
            params.append(val)
        
        # Rate Storage Logic
        if interest_amount is not None:
            set_clauses.append("interest_amount=?")
            params.append(interest_amount)
        
        # Add id at the end for WHERE clause
        params.append(id)
        
        query = f"UPDATE ledger SET {', '.join(set_clauses)} WHERE id=?"
        cursor.execute(query, tuple(params))
        self.conn.commit()

    def update_balance(self, id, balance):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE ledger SET balance=? WHERE id=?", (balance, id))
        self.conn.commit()
    
    def update_ledger_balances(self, id, balance, principal_bal, interest_bal, gross_bal=0):
        """Update all three balance types for a ledger entry."""
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE ledger 
            SET balance=?, principal_balance=?, interest_balance=?, gross_balance=? 
            WHERE id=?
        """, (balance, principal_bal, interest_bal, gross_bal, id))
        self.conn.commit()

    def delete_transaction(self, id):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM ledger WHERE id=?", (id,))
        self.conn.commit()

    def delete_batch(self, batch_id):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM ledger WHERE batch_id=?", (batch_id,))
        self.conn.commit()

    # Loan operations
    def add_loan_record(self, individual_id, ref, principal, total, balance, installment, monthly_interest, start_date, next_due_date, unearned_interest=0):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO loans (
                individual_id, ref, principal, total_amount, balance, installment, 
                monthly_interest, start_date, next_due_date, unearned_interest, interest_balance
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (individual_id, ref, principal, total, balance, installment, monthly_interest, start_date, next_due_date, unearned_interest))
        self.conn.commit()

    def get_active_loans(self, individual_id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM loans WHERE individual_id=? AND status='Active'", (individual_id,))
        cols = [description[0] for description in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_loans(self, individual_id):
        """Get ALL loans (Active and Paid) for an individual."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM loans WHERE individual_id=?", (individual_id,))
        cols = [description[0] for description in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_all_active_loans(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM loans WHERE status='Active'")
        cols = [description[0] for description in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_overdue_count(self):
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM loans WHERE status='Active' AND next_due_date < ?", (today,))
        return cursor.fetchone()[0]

    def get_loan_by_ref(self, individual_id, ref):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM loans WHERE individual_id=? AND ref=?", (individual_id, ref))
        row = cursor.fetchone()
        if row:
            cols = [description[0] for description in cursor.description]
            return dict(zip(cols, row))
        return None

    def update_loan_status(self, loan_id, balance, next_due_date, status, interest_balance=None, unearned_interest=None):
        cursor = self.conn.cursor()
        
        # Build query dynamically based on provided args
        query = "UPDATE loans SET balance=?, next_due_date=?, status=?"
        params = [balance, next_due_date, status]
        
        if interest_balance is not None:
            query += ", interest_balance=?"
            params.append(interest_balance)
            
        if unearned_interest is not None:
            query += ", unearned_interest=?"
            params.append(unearned_interest)
            
        query += " WHERE id=?"
        params.append(loan_id)
        
        cursor.execute(query, tuple(params))
        self.conn.commit()

    def update_loan_recalc_state(self, loan_id, monthly_interest, unearned_interest):
        """Update loan terms derived from history replay."""
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE loans 
            SET monthly_interest = ?, unearned_interest = ?
            WHERE id = ?
        """, (monthly_interest, unearned_interest, loan_id))
        self.conn.commit()

    def unlock_future_interest(self, loan_ref, date_str):
        """Reset is_edited flag for future interest rows, allowing re-simulation."""
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE ledger 
            SET is_edited = 0 
            WHERE loan_id = ? AND event_type = 'Interest Earned' AND date > ?
        """, (loan_ref, date_str))
        self.conn.commit()

    def update_loan_details(self, loan_id, total_amount, balance, installment, monthly_interest, next_due_date, unearned_interest=None, principal_update=None, interest_balance=None):
        cursor = self.conn.cursor()
        
        query = """UPDATE loans 
                   SET total_amount=?, balance=?, installment=?, monthly_interest=?, next_due_date=?"""
        params = [total_amount, balance, installment, monthly_interest, next_due_date]
        
        if unearned_interest is not None:
            query += ", unearned_interest=?"
            params.append(unearned_interest)
            
        if principal_update is not None:
            query += ", principal=?"
            params.append(principal_update)
            
        if interest_balance is not None:
            query += ", interest_balance=?"
            params.append(interest_balance)
            
        query += " WHERE id=?"
        params.append(loan_id)
        
        # print(f"DB DEBUG: Executing UPDATE loans: {query} with {params}") 
        cursor.execute(query, tuple(params))
        self.conn.commit()

    def delete_loan(self, individual_id, loan_ref):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM ledger WHERE individual_id=? AND loan_id=?", (individual_id, loan_ref))
        cursor.execute("DELETE FROM loans WHERE individual_id=? AND ref=?", (individual_id, loan_ref))
        self.conn.commit()

    def delete_batch(self, batch_id):
        """Delete all transactions associated with a batch_id from ledger."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM ledger WHERE batch_id=?", (batch_id,))
        self.conn.commit()

    def delete_savings_batch(self, batch_id):
        """Delete all transactions associated with a batch_id from savings."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM savings WHERE batch_id=?", (batch_id,))
        self.conn.commit()

    # ========== SAVINGS OPERATIONS ==========
    
    def create_savings_table(self):
        """Create savings table if not exists."""
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS savings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                individual_id INTEGER,
                date TEXT,
                transaction_type TEXT,
                amount REAL,
                balance REAL,
                notes TEXT,
                batch_id TEXT,
                FOREIGN KEY(individual_id) REFERENCES individuals(id)
            )
        """)
        try:
            cursor.execute("ALTER TABLE savings ADD COLUMN batch_id TEXT")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()
    
    def add_savings_transaction(self, individual_id, date, transaction_type, amount, notes="", batch_id=None):
        """Add a deposit or withdrawal to savings."""
        # Ensure table exists
        self.create_savings_table()
        
        # Get current balance
        current_balance = self.get_savings_balance(individual_id)
        
        if transaction_type == "Deposit":
            new_balance = current_balance + amount
        else:  # Withdrawal
            new_balance = current_balance - amount
        
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO savings (individual_id, date, transaction_type, amount, balance, notes, batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (individual_id, date, transaction_type, amount, new_balance, notes, batch_id))
        self.conn.commit()
        return new_balance
    
    def get_savings_balance(self, individual_id):
        """Get current savings balance for an individual."""
        self.create_savings_table()
        cursor = self.conn.cursor()
        cursor.execute("SELECT balance FROM savings WHERE individual_id=? ORDER BY id DESC LIMIT 1", (individual_id,))
        row = cursor.fetchone()
        return row[0] if row else 0.0
    
    def get_savings_transactions(self, individual_id, start_date=None, end_date=None):
        """Get all savings transactions for an individual."""
        self.create_savings_table()
        query = "SELECT * FROM savings WHERE individual_id = ?"
        params = [individual_id]
        
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
            
        query += " ORDER BY id"
        return pd.read_sql_query(query, self.conn, params=tuple(params))
    
    def delete_savings_transaction(self, trans_id):
        """Delete a savings transaction and recalculate balances."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM savings WHERE id=?", (trans_id,))
        self.conn.commit()

    def update_savings_transaction(self, trans_id, new_date, new_amount, new_notes):
        """Update a savings transaction."""
        cursor = self.conn.cursor()
        cursor.execute("UPDATE savings SET date=?, amount=?, notes=? WHERE id=?", 
                       (new_date, new_amount, new_notes, trans_id))
        self.conn.commit()
    
    def recalculate_savings_balances(self, individual_id):
        """Recalculate running balances for savings."""
        self.create_savings_table()
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, transaction_type, amount FROM savings WHERE individual_id=? ORDER BY id", (individual_id,))
        rows = cursor.fetchall()
        
        running_balance = 0.0
        for row in rows:
            trans_id, trans_type, amount = row
            if trans_type == "Deposit":
                running_balance += amount
            else:
                running_balance -= amount
            cursor.execute("UPDATE savings SET balance=? WHERE id=?", (running_balance, trans_id))
        self.conn.commit()
    def get_setting(self, key, default=None):
        """Get a setting value."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
        res = cursor.fetchone()
        return res[0] if res else default

    def set_setting(self, key, value):
        """Set a setting value."""
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        self.conn.commit()

    def import_individuals_from_external_db(self, source_db_path):
        """
        Import individuals (empty shells) from another SQLite database.
        Skips individuals that already exist (by Name).
        Returns the number of imported records.
        """
        try:
            # Connect to Source
            import sqlite3
            src_conn = sqlite3.connect(source_db_path)
            src_cursor = src_conn.cursor()
            
            # Check source table exists
            try:
                src_cursor.execute("SELECT name, phone, email, default_deduction FROM individuals")
                rows = src_cursor.fetchall()
            except sqlite3.OperationalError:
                src_conn.close()
                return -1 # Error: No individuals table
            
            src_conn.close()
            
            # Get Current Individuals (to Check Duplicates)
            current_names = set()
            cursor = self.conn.cursor()
            cursor.execute("SELECT name FROM individuals")
            for r in cursor.fetchall():
                current_names.add(r[0])
            
            imported_count = 0
            
            for row in rows:
                name, phone, email, def_ded = row
                if name not in current_names:
                    # Insert
                    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    # Handle None/Null values
                    phone = phone if phone else ""
                    email = email if email else ""
                    def_ded = def_ded if def_ded else 0
                    
                    cursor.execute("""
                        INSERT INTO individuals (name, phone, email, default_deduction, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (name, phone, email, def_ded, created_at))
                    imported_count += 1
            
            self.conn.commit()
            return imported_count
            

        except Exception as e:
            print(f"Import Error: {e}")
            return 0


    def get_import_preview(self, source_db_path):
        """
        Connect to source DB and return list of individuals for preview.
        Returns: list of dicts [{'id': 1, 'name': '...', 'phone': '...', 'email': '...'}]
        """
        import sqlite3
        try:
            src_conn = sqlite3.connect(source_db_path)
            src_conn.row_factory = sqlite3.Row
            src_cur = src_conn.cursor()
            
            try:
                src_cur.execute("SELECT id, name, phone, email FROM individuals ORDER BY name")
                rows = src_cur.fetchall()
                result = [dict(row) for row in rows]
                src_conn.close()
                return result
            except sqlite3.OperationalError:
                src_conn.close()
                return None
        except Exception:
            return None

    def import_selected_data(self, source_db_path, selected_ids, options):
        """
        Import ONLY selected individuals from external database.
        selected_ids: list of source IDs to import.
        options: dict with keys 'import_loans', 'import_savings'
        """
        import sqlite3
        
        # summary stats
        stats = {"individuals": 0, "loans": 0, "ledger": 0, "savings": 0}
        
        try:
            # Connect to Source
            src_conn = sqlite3.connect(source_db_path)
            src_conn.row_factory = sqlite3.Row
            src_cur = src_conn.cursor()
            
            # 1. Map Individuals
            try:
                # Filter by selected_ids
                if not selected_ids:
                    return stats 
                    
                placeholders = ','.join(['?'] * len(selected_ids))
                query = f"SELECT * FROM individuals WHERE id IN ({placeholders})"
                src_cur.execute(query, selected_ids)
                src_inds = src_cur.fetchall()
            except sqlite3.OperationalError:
                src_conn.close()
                return -1
            
            # Map Source ID -> Dest ID
            id_map = {} 
            
            # Get current individuals to check for duplicates
            dest_cur = self.conn.cursor()
            dest_cur.execute("SELECT name, id FROM individuals")
            existing_inds = {row[0]: row[1] for row in dest_cur.fetchall()}
            
            for src_ind in src_inds:
                name = src_ind['name']
                src_id = src_ind['id']
                
                if name in existing_inds:
                    # Match existing
                    id_map[src_id] = existing_inds[name]
                else:
                    # Create new
                    phone = src_ind['phone'] if src_ind['phone'] else ""
                    email = src_ind['email'] if src_ind['email'] else ""
                    # Handle handle missing column in older DBs gracefully or use dict get
                    keys = src_ind.keys()
                    def_ded = src_ind['default_deduction'] if 'default_deduction' in keys and src_ind['default_deduction'] else 0
                    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    dest_cur.execute("""
                        INSERT INTO individuals (name, phone, email, default_deduction, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (name, phone, email, def_ded, created_at))
                    new_id = dest_cur.lastrowid
                    id_map[src_id] = new_id
                    stats["individuals"] += 1
            
            # 2. Import Loans & Ledger
            if options.get("import_loans", False):
                loan_id_map = {} # Source Loan ID -> Dest Loan ID
                
                # --- Loans ---
                try:
                    # We need to filter loans that belong to selected individuals
                    # If we filtered src_inds, id_map only contains selected keys.
                    # So we can just check if individual_id is in id_map.
                    
                    src_cur.execute("SELECT * FROM loans")
                    src_loans = src_cur.fetchall()
                    
                    for ln in src_loans:
                        old_ind_id = ln['individual_id']
                        if old_ind_id not in id_map:
                            continue 
                            
                        new_ind_id = id_map[old_ind_id]
                        src_loan_id = ln['id']
                        
                        keys = ln.keys()
                        unearned = ln['unearned_interest'] if 'unearned_interest' in keys and ln['unearned_interest'] else 0
                        int_bal = ln['interest_balance'] if 'interest_balance' in keys and ln['interest_balance'] else 0
                        mo_int = ln['monthly_interest'] if 'monthly_interest' in keys and ln['monthly_interest'] else 0
                        
                        dest_cur.execute("""
                            INSERT INTO loans (
                                individual_id, ref, principal, total_amount, balance, 
                                installment, start_date, next_due_date, status, 
                                monthly_interest, unearned_interest, interest_balance
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            new_ind_id, ln['ref'], ln['principal'], ln['total_amount'], ln['balance'],
                            ln['installment'], ln['start_date'], ln['next_due_date'], ln['status'],
                            mo_int, unearned, int_bal
                        ))
                        new_loan_id = dest_cur.lastrowid
                        loan_id_map[src_loan_id] = new_loan_id
                        stats["loans"] += 1
                        
                except sqlite3.OperationalError:
                     pass # Table might not exist in source

                # --- Ledger ---
                try:
                    src_cur.execute("SELECT * FROM ledger")
                    src_entries = src_cur.fetchall()
                    
                    for entry in src_entries:
                        old_ind_id = entry['individual_id']
                        if old_ind_id not in id_map:
                            continue
                        
                        new_ind_id = id_map[old_ind_id]
                        
                        # Handle Loan ID mapping
                        old_loan_id = entry['loan_id']
                        new_loan_id_val = None
                        
                        # Check if loan_id is an integer (standard ID) or text
                        # We try to map it if it exists in our loan_map
                        try:
                            old_lid_int = int(old_loan_id)
                            if old_lid_int in loan_id_map:
                                new_loan_id_val = str(loan_id_map[old_lid_int])
                        except (ValueError, TypeError):
                            # It might be None or a string ref that we can't map easily if it's not an ID
                            # or it's a legacy generic entry. Keep as is or None?
                            # If it's a loan payment, we need the link.
                            new_loan_id_val = old_loan_id
                        
                        keys = entry.keys()
                        # handle optional columns
                        inst_amt = entry['installment_amount'] if 'installment_amount' in keys and entry['installment_amount'] else 0
                        batch_id = entry['batch_id'] if 'batch_id' in keys else None
                        int_amt = entry['interest_amount'] if 'interest_amount' in keys and entry['interest_amount'] else 0
                        p_bal = entry['principal_balance'] if 'principal_balance' in keys and entry['principal_balance'] else 0
                        i_bal = entry['interest_balance'] if 'interest_balance' in keys and entry['interest_balance'] else 0
                        p_port = entry['principal_portion'] if 'principal_portion' in keys and entry['principal_portion'] else 0
                        i_port = entry['interest_portion'] if 'interest_portion' in keys and entry['interest_portion'] else 0
                        
                        dest_cur.execute("""
                            INSERT INTO ledger (
                                individual_id, date, event_type, loan_id, added, deducted, balance, notes,
                                installment_amount, batch_id, interest_amount,
                                principal_balance, interest_balance, principal_portion, interest_portion
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            new_ind_id, entry['date'], entry['event_type'], new_loan_id_val, 
                            entry['added'], entry['deducted'], entry['balance'], entry['notes'],
                            inst_amt, batch_id, int_amt,
                            p_bal, i_bal, p_port, i_port
                        ))
                        stats["ledger"] += 1
                        
                except sqlite3.OperationalError:
                     pass

            # 3. Import Savings
            if options.get("import_savings", False):
                self.create_savings_table() # Ensure table exists
                try:
                    # Check for savings table
                    src_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='savings'")
                    if src_cur.fetchone():
                        src_cur.execute("SELECT * FROM savings")
                        src_savings = src_cur.fetchall()
                        
                        for sav in src_savings:
                            old_ind_id = sav['individual_id']
                            if old_ind_id not in id_map:
                                continue
                            new_ind_id = id_map[old_ind_id]
                            
                            dest_cur.execute("""
                                INSERT INTO savings (individual_id, date, transaction_type, amount, balance, notes)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (new_ind_id, sav['date'], sav['transaction_type'], sav['amount'], sav['balance'], sav['notes']))
                            stats["savings"] += 1
                except sqlite3.OperationalError:
                    pass
            
            self.conn.commit()
            src_conn.close()
            
            # Return total processed count (sum of all records)
            total_ops = sum(stats.values())
            return total_ops
            
        except Exception as e:
            self.conn.rollback()
            print(f"Deep Import Error: {e}")
            if 'src_conn' in locals():
                src_conn.close()
            return -1
