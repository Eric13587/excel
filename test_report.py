from src.reports import ReportGenerator
from src.database import DatabaseManager

db = DatabaseManager('loan_master.db')
g = ReportGenerator(db)

# Q3 2025 starting July 2025? If FY starts in July?
success, msg = g.generate_quarterly_savings_report('2025-08-01', 'scratch/Quarterly_Savings_Report.csv')
print(success, msg)
