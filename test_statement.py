from src.database import DatabaseManager
from src.statement_generator import StatementGenerator

db = DatabaseManager('loan_master.db')
g = StatementGenerator(db)

import os
if not os.path.exists('scratch'):
    os.makedirs('scratch')

# Generating a historical statement for Francis Mwanza (let's assume ID 5 or something, we can use ID 6, since we know Ind 6 has data)
res_html = g.generate_pdf_statement(6, "Test user", "scratch", from_date="2025-08-01", to_date="2025-09-15")

print(f"Generated successfully: {res_html}")
