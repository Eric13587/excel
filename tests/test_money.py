import sys
import unittest

sys.path.insert(0, "/home/yhazadek/Desktop/excel")

from src.money import format_money


class TestFormatMoney(unittest.TestCase):
    def test_default_is_grouped_whole_number(self):
        self.assertEqual(format_money(1234567), "1,234,567")
        self.assertEqual(format_money(0), "0")

    def test_rounds_for_display_without_decimals(self):
        # Display rounding only; the caller's stored value is unchanged.
        self.assertEqual(format_money(1234.6), "1,235")

    def test_decimals_when_asked(self):
        self.assertEqual(format_money(1234.5, decimals=2), "1,234.50")

    def test_negative(self):
        self.assertEqual(format_money(-2500), "-2,500")

    def test_symbol_is_prefixed_only_when_given(self):
        self.assertEqual(format_money(2500, symbol="KES "), "KES 2,500")
        self.assertEqual(format_money(2500), "2,500")

    def test_none_and_junk_format_as_zero(self):
        self.assertEqual(format_money(None), "0")
        self.assertEqual(format_money(""), "0")
        self.assertEqual(format_money("abc"), "0")


if __name__ == "__main__":
    unittest.main()
