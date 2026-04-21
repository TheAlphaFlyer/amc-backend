from django.test import SimpleTestCase
from amc.jobs import calculate_treasury_multiplier


class TreasuryMultiplierTestCase(SimpleTestCase):
    """Tests for the asymmetric treasury multiplier."""

    def test_at_equilibrium_returns_one(self):
        result = calculate_treasury_multiplier(100_000_000, equilibrium=100_000_000)
        self.assertEqual(result, 1.0)

    def test_below_equilibrium_returns_less_than_one(self):
        result = calculate_treasury_multiplier(50_000_000, equilibrium=100_000_000)
        self.assertLess(result, 1.0)

    def test_above_equilibrium_returns_greater_than_one(self):
        result = calculate_treasury_multiplier(150_000_000, equilibrium=100_000_000)
        self.assertGreater(result, 1.0)

    def test_zero_balance_returns_zero(self):
        result = calculate_treasury_multiplier(0, equilibrium=100_000_000)
        self.assertEqual(result, 0.0)

    def test_negative_balance_returns_zero(self):
        result = calculate_treasury_multiplier(-1_000_000, equilibrium=100_000_000)
        self.assertEqual(result, 0.0)

    def test_zero_equilibrium_does_not_crash(self):
        result = calculate_treasury_multiplier(100_000_000, equilibrium=0)
        self.assertGreaterEqual(result, 0.0)

    def test_always_positive_or_zero(self):
        test_cases = [0, 1, 1_000, 1_000_000, 100_000_000, 1_000_000_000]
        for balance in test_cases:
            with self.subTest(balance=balance):
                result = calculate_treasury_multiplier(balance, equilibrium=100_000_000)
                self.assertGreaterEqual(result, 0.0)

    def test_monotonically_increasing(self):
        balances = [
            0, 10_000_000, 25_000_000, 50_000_000,
            75_000_000, 100_000_000, 200_000_000,
        ]
        results = [
            calculate_treasury_multiplier(b, equilibrium=100_000_000)
            for b in balances
        ]
        for i in range(1, len(results)):
            self.assertGreaterEqual(
                results[i],
                results[i - 1],
                f"Multiplier should increase: balance {balances[i]} gave {results[i]} "
                f"but balance {balances[i - 1]} gave {results[i - 1]}",
            )

    def test_higher_sensitivity_gives_steeper_curve_below_equilibrium(self):
        low_sens = calculate_treasury_multiplier(
            50_000_000, equilibrium=100_000_000, sensitivity=1.0
        )
        high_sens = calculate_treasury_multiplier(
            50_000_000, equilibrium=100_000_000, sensitivity=3.0
        )
        self.assertLess(high_sens, low_sens)

    def test_below_equilibrium_uses_power_curve(self):
        result = calculate_treasury_multiplier(
            50_000_000, equilibrium=100_000_000, sensitivity=1.5
        )
        expected = (50_000_000 / 100_000_000) ** 1.5
        self.assertAlmostEqual(result, expected, places=5)

    def test_above_equilibrium_uses_log_curve(self):
        import math

        result = calculate_treasury_multiplier(
            200_000_000, equilibrium=100_000_000, sensitivity=1.5, cap_ratio=4.0
        )
        expected = 1.0 + math.log(2.0) / math.log(4.0)
        self.assertAlmostEqual(result, expected, places=5)

    def test_at_cap_ratio_returns_two(self):
        result = calculate_treasury_multiplier(
            400_000_000, equilibrium=100_000_000, sensitivity=1.5, cap_ratio=4.0
        )
        self.assertAlmostEqual(result, 2.0, places=5)

    def test_above_cap_ratio_exceeds_two(self):
        result = calculate_treasury_multiplier(
            1_000_000_000, equilibrium=100_000_000, sensitivity=1.5, cap_ratio=4.0
        )
        self.assertGreater(result, 2.0)

    def test_higher_cap_ratio_gives_slower_growth_above_equilibrium(self):
        low_cap = calculate_treasury_multiplier(
            400_000_000, equilibrium=100_000_000, cap_ratio=4.0
        )
        high_cap = calculate_treasury_multiplier(
            400_000_000, equilibrium=100_000_000, cap_ratio=8.0
        )
        self.assertLess(high_cap, low_cap)

    def test_clamped_scaling_factor_floor(self):
        treasury_mult = calculate_treasury_multiplier(
            0, equilibrium=100_000_000, sensitivity=1.5
        )
        combined = treasury_mult * 0.7
        self.assertEqual(combined, 0.0)
        clamped = max(treasury_mult * 0.5, min(2.0, combined))
        self.assertEqual(clamped, 0.0)

    def test_clamped_scaling_factor_ceiling(self):
        treasury_mult = calculate_treasury_multiplier(
            1_000_000_000, equilibrium=100_000_000, sensitivity=1.5, cap_ratio=4.0
        )
        combined = treasury_mult * 1.3
        clamped = max(treasury_mult * 0.5, min(2.0, combined))
        self.assertEqual(clamped, 2.0)

    def test_bonus_within_expected_range(self):
        import random as rng

        base_bonus = 100_000
        rng.seed(42)

        for _ in range(1000):
            balance = rng.randint(0, 1_000_000_000)
            treasury_mult = calculate_treasury_multiplier(
                balance, equilibrium=100_000_000, sensitivity=1.5
            )
            scaling_factor = max(
                treasury_mult * 0.5,
                min(2.0, treasury_mult * rng.uniform(0.7, 1.3)),
            )
            completion_bonus = int(base_bonus * scaling_factor)
            self.assertGreaterEqual(
                completion_bonus,
                0,
                f"Bonus {completion_bonus} below floor at balance {balance}",
            )
            self.assertLessEqual(
                completion_bonus,
                int(base_bonus * 2.0),
                f"Bonus {completion_bonus} above ceiling at balance {balance}",
            )
