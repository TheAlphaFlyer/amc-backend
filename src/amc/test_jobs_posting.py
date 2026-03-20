from collections import Counter
from django.test import TestCase
from amc.jobs import weighted_shuffle


class WeightedShuffleTestCase(TestCase):
    """Tests for the weighted_shuffle helper function."""

    def test_weighted_shuffle_returns_all_items(self):
        """All items should appear in the result."""
        items = ["a", "b", "c", "d"]
        result = weighted_shuffle(items, lambda x: 1.0)
        self.assertEqual(sorted(result), sorted(items))

    def test_weighted_shuffle_favors_high_weight(self):
        """Items with higher weight should appear first more often."""
        items = ["low", "high"]
        first_counts: Counter[str] = Counter()
        trials = 1000

        for _ in range(trials):
            result = weighted_shuffle(items, lambda x: 10.0 if x == "high" else 1.0)
            first_counts[result[0]] += 1

        # "high" should appear first roughly 10/11 of the time (~91%)
        self.assertGreater(
            first_counts["high"],
            trials * 0.75,
            f"Expected 'high' to be first most of the time, got {first_counts}",
        )

    def test_weighted_shuffle_with_zero_weights(self):
        """Zero-weight items should fall back to random order."""
        items = ["a", "b", "c"]
        result = weighted_shuffle(items, lambda x: 0.0)
        self.assertEqual(sorted(result), sorted(items))

    def test_weighted_shuffle_empty_list(self):
        """Empty input should return empty output."""
        result = weighted_shuffle([], lambda x: 1.0)
        self.assertEqual(result, [])

    def test_weighted_shuffle_single_item(self):
        """Single item should be returned as-is."""
        result = weighted_shuffle(["only"], lambda x: 5.0)
        self.assertEqual(result, ["only"])
