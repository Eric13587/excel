import sys
import unittest

sys.path.insert(0, "/home/yhazadek/Desktop/excel")

from src.database import DatabaseManager


class TestPortfolioAggregates(unittest.TestCase):
    """The cheap, whole-journal queries behind the dashboard KPI band."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.a = self.db.add_individual("Alice", "1", "a@x.com")
        self.b = self.db.add_individual("Bob", "2", "b@x.com")

    def tearDown(self):
        self.db.close()

    def test_total_savings_sums_every_member(self):
        self.db.add_savings_transaction(self.a, "2026-01-10", "Deposit", 2500)
        self.db.add_savings_transaction(self.b, "2026-01-10", "Deposit", 4000)
        self.db.add_savings_transaction(self.a, "2026-02-10", "Withdrawal", 500)
        # 2500 - 500 + 4000
        self.assertEqual(self.db.get_total_savings(), 6000)

    def test_total_savings_is_zero_on_empty_journal(self):
        self.assertEqual(self.db.get_total_savings(), 0)

    def test_total_savings_matches_sum_of_per_member_balances(self):
        self.db.add_savings_transaction(self.a, "2026-01-10", "Deposit", 1234)
        self.db.add_savings_transaction(self.b, "2026-01-10", "Deposit", 8766)
        per_member = (self.db.get_savings_balance(self.a)
                      + self.db.get_savings_balance(self.b))
        self.assertEqual(self.db.get_total_savings(), per_member)

    def test_outstanding_and_active_count_from_active_loans(self):
        self.db.add_loan_record(self.a, "L-001", 1000, 1150, 1150, 100, 0,
                                "2026-01-01", "2026-02-01")
        self.db.add_loan_record(self.b, "L-001", 2000, 2300, 800, 200, 0,
                                "2026-01-01", "2026-02-01")
        active = self.db.get_all_active_loans()
        outstanding = sum(float(loan.get("balance") or 0) for loan in active)
        self.assertEqual(len(active), 2)
        self.assertEqual(outstanding, 1950)  # 1150 + 800


if __name__ == "__main__":
    unittest.main()
