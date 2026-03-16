from django.test import TestCase
from amc.cargo import get_cargo_bonus


class GetCargoBonusTests(TestCase):
    def test_oak_log_zero_damage(self):
        # 0% damage → full bonus (100% of payment)
        self.assertEqual(get_cargo_bonus("Log_Oak_12ft", 8641, 0.0), 8641)

    def test_oak_log_full_damage(self):
        # 100% damage → no bonus
        self.assertEqual(get_cargo_bonus("Log_Oak_12ft", 8641, 1.0), 0)

    def test_oak_log_partial_damage(self):
        # 50% damage → 50% bonus
        self.assertEqual(get_cargo_bonus("Log_Oak_12ft", 10000, 0.5), 5000)

    def test_oak_log_quarter_damage(self):
        # 25% damage → 75% bonus
        self.assertEqual(get_cargo_bonus("Log_Oak_12ft", 10000, 0.25), 7500)

    def test_unknown_cargo_no_bonus(self):
        self.assertEqual(get_cargo_bonus("oranges", 10000, 0.0), 0)

    def test_unknown_cargo_with_damage(self):
        self.assertEqual(get_cargo_bonus("apples", 10000, 0.5), 0)
