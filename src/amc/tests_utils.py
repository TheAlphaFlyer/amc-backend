from django.test import SimpleTestCase
from amc.utils import generate_verification_code, fuzzy_find_player


class UtilsTestCase(SimpleTestCase):
    def test_generate_verification_code(self):
        code = generate_verification_code("test_input")
        self.assertEqual(len(code), 4)
        for char in code:
            self.assertIn(char, "ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
            self.assertNotIn(char, "0O1Il")

    def test_determinism(self):
        c1 = generate_verification_code("input1")
        c2 = generate_verification_code("input1")
        self.assertEqual(c1, c2)

        c3 = generate_verification_code("input2")
        self.assertNotEqual(c1, c3)  # Highly likely to be different


class FuzzyFindPlayerTestCase(SimpleTestCase):
    """Tests for fuzzy_find_player with tag-stripping support."""

    def _make_players(self, *names):
        """Helper to build player list from names."""
        return [(f"pid-{i}", {"name": n}) for i, n in enumerate(names)]

    def test_exact_match_no_tags(self):
        players = self._make_players("Alice", "Bob")
        self.assertEqual(fuzzy_find_player(players, "Alice"), "pid-0")

    def test_exact_match_with_tagged_name(self):
        """Typing the full tagged name still works."""
        players = self._make_players("[MODS] Alice", "Bob")
        self.assertEqual(fuzzy_find_player(players, "[MODS] Alice"), "pid-0")

    def test_exact_match_base_name_strips_mods_tag(self):
        """Typing just 'Alice' matches '[MODS] Alice'."""
        players = self._make_players("[MODS] Alice", "Bob")
        self.assertEqual(fuzzy_find_player(players, "Alice"), "pid-0")

    def test_exact_match_base_name_strips_gov_tag(self):
        """Typing just 'Alice' matches '[GOV2] Alice'."""
        players = self._make_players("[GOV2] Alice", "Bob")
        self.assertEqual(fuzzy_find_player(players, "Alice"), "pid-0")

    def test_exact_match_base_name_strips_multiple_tags(self):
        """Typing just 'Alice' matches '[MODS] [GOV2] Alice'."""
        players = self._make_players("[MODS] [GOV2] Alice", "Bob")
        self.assertEqual(fuzzy_find_player(players, "Alice"), "pid-0")

    def test_fuzzy_match_strips_tags(self):
        """Fuzzy match works against the base name when tags are present."""
        players = self._make_players("[MODS] TargetPlayer", "OtherPerson")
        # "TargetP" should fuzzy-match "[MODS] TargetPlayer" via stripped name
        self.assertEqual(fuzzy_find_player(players, "TargetP"), "pid-0")

    def test_no_match_returns_none(self):
        players = self._make_players("Alice", "Bob")
        self.assertIsNone(fuzzy_find_player(players, "Zzzzz"))

    def test_empty_query_returns_none(self):
        players = self._make_players("Alice")
        self.assertIsNone(fuzzy_find_player(players, ""))

    def test_case_insensitive(self):
        players = self._make_players("[MODS] Alice")
        self.assertEqual(fuzzy_find_player(players, "alice"), "pid-0")
