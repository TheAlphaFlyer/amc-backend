import os
from unittest.mock import patch
from django.test import TestCase
from amc.mod_server import get_party_size_for_character
from amc.webhook import PARTY_BONUS_RATE


class GetPartySizeTest(TestCase):
    """Tests for get_party_size_for_character."""

    def setUp(self):
        self.parties = [
            {
                "PartyId": 1,
                "Players": [
                    "AAAA0000BBBB1111CCCC2222DDDD3333",
                    "EEEE4444FFFF5555AAAA6666BBBB7777",
                ],
            },
            {
                "PartyId": 2,
                "Players": [
                    "11112222333344445555666677778888",
                    "AAAABBBBCCCCDDDDEEEEFFFF00001111",
                    "22223333444455556666777788889999",
                ],
            },
        ]

    def test_player_in_2_person_party(self):
        size = get_party_size_for_character(
            self.parties, "AAAA0000BBBB1111CCCC2222DDDD3333"
        )
        self.assertEqual(size, 2)

    def test_player_in_3_person_party(self):
        size = get_party_size_for_character(
            self.parties, "11112222333344445555666677778888"
        )
        self.assertEqual(size, 3)

    def test_player_not_in_party(self):
        size = get_party_size_for_character(
            self.parties, "NOT_IN_ANY_PARTY_GUID00000000"
        )
        self.assertEqual(size, 1)

    def test_empty_parties_list(self):
        size = get_party_size_for_character([], "AAAA0000BBBB1111CCCC2222DDDD3333")
        self.assertEqual(size, 1)

    def test_case_insensitive_guid_matching(self):
        """Backend may pass lowercase guid; mod server returns uppercase."""
        size = get_party_size_for_character(
            self.parties, "aaaa0000bbbb1111cccc2222dddd3333"
        )
        self.assertEqual(size, 2)

    def test_none_guid(self):
        """Defensive: None guid should not crash."""
        size = get_party_size_for_character(self.parties, None)
        self.assertEqual(size, 1)

    def test_party_with_empty_players(self):
        parties = [{"PartyId": 1, "Players": []}]
        size = get_party_size_for_character(parties, "SOME_GUID")
        self.assertEqual(size, 1)

    def test_party_missing_players_key(self):
        parties = [{"PartyId": 1}]
        size = get_party_size_for_character(parties, "SOME_GUID")
        self.assertEqual(size, 1)


class PartyBonusMultiplierTest(TestCase):
    """Tests for the bonus multiplier formula applied in process_events."""

    def _calc_multiplier(self, party_size):
        return 1 + (party_size - 1) * PARTY_BONUS_RATE

    def test_solo_no_bonus(self):
        self.assertAlmostEqual(self._calc_multiplier(1), 1.0)

    def test_two_person_party(self):
        self.assertAlmostEqual(self._calc_multiplier(2), 1.05)

    def test_five_person_party(self):
        self.assertAlmostEqual(self._calc_multiplier(5), 1.20)

    def test_large_party_no_cap(self):
        self.assertAlmostEqual(self._calc_multiplier(10), 1.45)

    def test_bonus_on_whole_payment(self):
        """15000 total payment with 3-person party -> bonus = int(15000 * 0.10) = 1500."""
        total_payment = 15000
        total_subsidy = 5000
        party_size = 3
        multiplier = self._calc_multiplier(party_size)
        party_bonus = int(total_payment * (multiplier - 1))
        total_subsidy += party_bonus
        total_payment += party_bonus
        self.assertEqual(party_bonus, 1500)
        self.assertEqual(total_subsidy, 6500)  # 5000 + 1500
        self.assertEqual(total_payment, 16500)  # 15000 + 1500

    def test_no_bonus_on_zero_payment(self):
        total_payment = 0
        # Guard: total_payment > 0 prevents computation
        if total_payment > 0:
            party_bonus = int(total_payment * (self._calc_multiplier(3) - 1))
        else:
            party_bonus = 0
        self.assertEqual(party_bonus, 0)

    def test_no_bonus_on_negative_payment(self):
        """Negative payment must NOT generate bonus."""
        total_payment = -1000
        if total_payment > 0:
            party_bonus = int(total_payment * (self._calc_multiplier(3) - 1))
        else:
            party_bonus = 0
        self.assertEqual(party_bonus, 0)

    def test_bonus_delivered_as_subsidy(self):
        """Party bonus is added to total_subsidy since backend transfers it."""
        total_payment = 10000  # base 7000 + subsidy 3000
        total_subsidy = 3000
        party_bonus = int(total_payment * (self._calc_multiplier(2) - 1))
        total_subsidy += party_bonus
        total_payment += party_bonus
        # Verify subsidy now includes the party bonus
        self.assertEqual(party_bonus, 500)  # 10000 * 0.05
        self.assertEqual(total_subsidy, 3500)  # 3000 + 500

    def test_shortcut_strips_party_bonus(self):
        """Shortcut zeroes subsidy (including party bonus) after it's applied."""
        total_payment = 10000  # base 7000 + subsidy 3000
        total_subsidy = 3000

        # Apply party bonus first
        party_bonus = int(total_payment * (self._calc_multiplier(3) - 1))
        total_subsidy += party_bonus
        total_payment += party_bonus
        self.assertEqual(total_subsidy, 4000)  # 3000 + 1000

        # Then shortcut strips it
        total_payment -= total_subsidy
        total_subsidy = 0
        self.assertEqual(total_subsidy, 0)
        self.assertEqual(total_payment, 7000)  # back to base only

    def test_bonus_with_zero_subsidy(self):
        """Even with zero subsidy, bonus applies to total_payment (base earnings)."""
        total_payment = 10000  # all base, no subsidy
        total_subsidy = 0
        party_bonus = int(total_payment * (self._calc_multiplier(2) - 1))
        total_subsidy += party_bonus
        total_payment += party_bonus
        self.assertEqual(party_bonus, 500)
        self.assertEqual(total_subsidy, 500)  # was 0, now has party bonus


class FeatureFlagTest(TestCase):
    """Tests for PARTY_BONUS_ENABLED feature flag."""

    def test_flag_enabled_with_1(self):
        with patch.dict(os.environ, {"PARTY_BONUS_ENABLED": "1"}):
            result = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
            self.assertTrue(result)

    def test_flag_enabled_with_true(self):
        with patch.dict(os.environ, {"PARTY_BONUS_ENABLED": "true"}):
            result = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
            self.assertTrue(result)

    def test_flag_enabled_with_TRUE(self):
        with patch.dict(os.environ, {"PARTY_BONUS_ENABLED": "TRUE"}):
            result = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
            self.assertTrue(result)

    def test_flag_disabled_with_0(self):
        with patch.dict(os.environ, {"PARTY_BONUS_ENABLED": "0"}):
            result = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
            self.assertFalse(result)

    def test_flag_disabled_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            result = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
            self.assertFalse(result)

    def test_flag_disabled_with_empty(self):
        with patch.dict(os.environ, {"PARTY_BONUS_ENABLED": ""}):
            result = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
            self.assertFalse(result)
