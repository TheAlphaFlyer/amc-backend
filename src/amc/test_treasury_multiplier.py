from django.test import SimpleTestCase
from amc.jobs import calculate_treasury_multiplier


class TreasuryMultiplierTestCase(SimpleTestCase):
    """Tests for the sigmoid-based treasury multiplier."""

    def test_at_equilibrium_returns_approximately_one(self):
        """At equilibrium balance, multiplier should be ~1.0."""
        result = calculate_treasury_multiplier(50_000_000, equilibrium=50_000_000)
        self.assertAlmostEqual(result, 1.0, places=5)

    def test_below_equilibrium_returns_less_than_one(self):
        """Below equilibrium, multiplier should be < 1.0."""
        result = calculate_treasury_multiplier(25_000_000, equilibrium=50_000_000)
        self.assertLess(result, 1.0)

    def test_above_equilibrium_returns_greater_than_one(self):
        """Above equilibrium, multiplier should be > 1.0."""
        result = calculate_treasury_multiplier(75_000_000, equilibrium=50_000_000)
        self.assertGreater(result, 1.0)

    def test_zero_balance_does_not_crash(self):
        """Zero balance should return a small positive value, not crash."""
        result = calculate_treasury_multiplier(0, equilibrium=50_000_000)
        self.assertGreater(result, 0)
        self.assertLess(result, 1.0)

    def test_zero_equilibrium_does_not_crash(self):
        """Zero equilibrium should not cause division by zero."""
        result = calculate_treasury_multiplier(50_000_000, equilibrium=0)
        self.assertGreater(result, 0)

    def test_max_bounded_at_two(self):
        """Even with extreme balance, multiplier should approach but not exceed 2.0."""
        result = calculate_treasury_multiplier(1_000_000_000, equilibrium=50_000_000)
        self.assertLessEqual(result, 2.0)

    def test_always_positive(self):
        """Multiplier should always be positive regardless of inputs."""
        test_cases = [0, 1, 1_000, 1_000_000, 50_000_000, 500_000_000]
        for balance in test_cases:
            with self.subTest(balance=balance):
                result = calculate_treasury_multiplier(balance, equilibrium=50_000_000)
                self.assertGreater(result, 0)

    def test_monotonically_increasing(self):
        """Higher balance should always give higher or equal multiplier."""
        balances = [0, 10_000_000, 25_000_000, 50_000_000, 75_000_000, 100_000_000]
        results = [
            calculate_treasury_multiplier(b, equilibrium=50_000_000) for b in balances
        ]
        for i in range(1, len(results)):
            self.assertGreaterEqual(
                results[i],
                results[i - 1],
                f"Multiplier should increase: balance {balances[i]} gave {results[i]} "
                f"but balance {balances[i - 1]} gave {results[i - 1]}",
            )

    def test_higher_sensitivity_gives_steeper_curve(self):
        """Higher sensitivity should make the curve steeper (more extreme values)."""
        low_sens = calculate_treasury_multiplier(
            25_000_000, equilibrium=50_000_000, sensitivity=0.3
        )
        high_sens = calculate_treasury_multiplier(
            25_000_000, equilibrium=50_000_000, sensitivity=2.0
        )
        # Below equilibrium: higher sensitivity should give a LOWER multiplier
        self.assertLess(high_sens, low_sens)

    def test_representative_values(self):
        """Verify multiplier at key treasury balances with default params."""
        # Near-broke: should reduce spending
        at_zero = calculate_treasury_multiplier(
            0, equilibrium=50_000_000, sensitivity=0.5
        )
        self.assertLess(at_zero, 1.0)

        # Half: should moderately reduce spending
        at_half = calculate_treasury_multiplier(
            25_000_000, equilibrium=50_000_000, sensitivity=0.5
        )
        self.assertGreater(at_half, 0.5)
        self.assertLess(at_half, 1.0)

        # Double: should increase spending
        at_double = calculate_treasury_multiplier(
            100_000_000, equilibrium=50_000_000, sensitivity=0.5
        )
        self.assertGreater(at_double, 1.2)
        self.assertLess(at_double, 2.0)

    def test_clamped_scaling_factor_floor(self):
        """When treasury is near-broke, combined factor should not go below 0.5."""
        # Near-zero balance with high sensitivity → very low multiplier
        treasury_mult = calculate_treasury_multiplier(
            0, equilibrium=100_000_000, sensitivity=2.0
        )
        # Worst-case random roll (0.7)
        combined = treasury_mult * 0.7
        self.assertLess(combined, 0.5, "Pre-clamp value should be below 0.5")
        clamped = max(0.5, min(2.0, combined))
        self.assertEqual(clamped, 0.5)

    def test_clamped_scaling_factor_ceiling(self):
        """When treasury is very rich, combined factor should not exceed 2.0."""
        # Very high balance → treasury_mult approaches 2.0
        treasury_mult = calculate_treasury_multiplier(
            1_000_000_000, equilibrium=100_000_000, sensitivity=0.5
        )
        # Best-case random roll (1.3)
        combined = treasury_mult * 1.3
        clamped = max(0.5, min(2.0, combined))
        self.assertEqual(clamped, 2.0)

    def test_bonus_within_expected_range(self):
        """Final completion bonus should always be within [base * 0.5, base * 2.0]."""
        import random as rng

        base_bonus = 100_000
        rng.seed(42)  # Deterministic for reproducibility

        for _ in range(1000):
            # Random treasury balance between 0 and 500M
            balance = rng.randint(0, 500_000_000)
            treasury_mult = calculate_treasury_multiplier(
                balance, equilibrium=100_000_000, sensitivity=0.5
            )
            scaling_factor = max(0.5, min(2.0, treasury_mult * rng.uniform(0.7, 1.3)))
            completion_bonus = int(base_bonus * scaling_factor)
            self.assertGreaterEqual(
                completion_bonus,
                int(base_bonus * 0.5),
                f"Bonus {completion_bonus} below floor at balance {balance}",
            )
            self.assertLessEqual(
                completion_bonus,
                int(base_bonus * 2.0),
                f"Bonus {completion_bonus} above ceiling at balance {balance}",
            )
