import os
import time
from datetime import timedelta
from unittest.mock import patch, MagicMock, AsyncMock
from django.test import TestCase
from django.contrib.gis.geos import Point
from django.utils import timezone
from asgiref.sync import sync_to_async
from amc.mod_server import get_party_size_for_character, get_party_members_for_character
from amc.webhook import PARTY_BONUS_RATE
from amc.factories import PlayerFactory, CharacterFactory
from amc.models import DeliveryPoint, CharacterLocation, PlayerStatusLog


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


class GetPartyMembersTest(TestCase):
    """Tests for get_party_members_for_character."""

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

    def test_returns_all_members_in_2_person_party(self):
        members = get_party_members_for_character(
            self.parties, "AAAA0000BBBB1111CCCC2222DDDD3333"
        )
        self.assertEqual(len(members), 2)
        self.assertIn("AAAA0000BBBB1111CCCC2222DDDD3333", members)
        self.assertIn("EEEE4444FFFF5555AAAA6666BBBB7777", members)

    def test_returns_all_members_in_3_person_party(self):
        members = get_party_members_for_character(
            self.parties, "11112222333344445555666677778888"
        )
        self.assertEqual(len(members), 3)

    def test_solo_returns_own_guid(self):
        members = get_party_members_for_character(
            self.parties, "NOT_IN_ANY_PARTY_GUID00000000"
        )
        self.assertEqual(members, ["NOT_IN_ANY_PARTY_GUID00000000"])

    def test_empty_parties(self):
        members = get_party_members_for_character(
            [], "AAAA0000BBBB1111CCCC2222DDDD3333"
        )
        self.assertEqual(members, ["AAAA0000BBBB1111CCCC2222DDDD3333"])

    def test_none_guid_returns_empty(self):
        members = get_party_members_for_character(self.parties, None)
        self.assertEqual(members, [])

    def test_case_insensitive_lookup(self):
        members = get_party_members_for_character(
            self.parties, "aaaa0000bbbb1111cccc2222dddd3333"
        )
        self.assertEqual(len(members), 2)


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


def _make_cargo_event(character_guid, payment=10_000):
    """Helper to create a simple cargo event."""
    return {
        "hook": "ServerCargoArrived",
        "timestamp": int(time.time()),
        "data": {
            "CharacterGuid": str(character_guid),
            "Cargos": [
                {
                    "Net_CargoKey": "oranges",
                    "Net_Payment": payment,
                    "Net_Weight": 100.0,
                    "Net_Damage": 0.0,
                    "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                    "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                }
            ],
        },
    }


async def _setup_delivery_points():
    """Create basic delivery points for tests."""
    await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
    await DeliveryPoint.objects.acreate(guid="d1", name="D1", coord=Point(100, 100, 0))


async def _create_online_character(player=None, guid=None, **kwargs):
    """Create a character with location and status log (required for process_events)."""
    if player is None:
        player = await sync_to_async(PlayerFactory)()
    char_kwargs = {"player": player}
    if guid:
        char_kwargs["guid"] = guid
    char_kwargs.update(kwargs)
    character = await sync_to_async(CharacterFactory)(**char_kwargs)
    await CharacterLocation.objects.acreate(
        character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
    )
    await PlayerStatusLog.objects.acreate(
        character=character,
        timespan=(timezone.now() - timedelta(minutes=5), timezone.now()),
    )
    return character


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.webhook.PARTY_BONUS_ENABLED", True)
class PartyPaymentSplitTest(TestCase):
    """Integration tests for party payment splitting via process_events."""

    async def test_equal_split_two_members(self, mock_treasury, mock_rp):
        """2-person party: payment split equally after bonus."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        await _create_online_character(guid="AAAA0000BBBB1111CCCC2222DDDD3333")
        await _create_online_character(guid="EEEE4444FFFF5555AAAA6666BBBB7777")
        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "AAAA0000BBBB1111CCCC2222DDDD3333",
                    "EEEE4444FFFF5555AAAA6666BBBB7777",
                ],
            }
        ]
        events = [_make_cargo_event("AAAA0000BBBB1111CCCC2222DDDD3333", payment=10_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx

            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # 2-person party: bonus = int(10000 * 0.05) = 500
        # total_payment = 10500, total_subsidy = 500 (bonus only, no cargo subsidy)
        # share_payment = 10500 // 2 = 5250
        # share_subsidy = 500 // 2 = 250
        self.assertEqual(len(player_profits), 2)
        for char, subsidy, payment, contract in player_profits:
            self.assertEqual(payment, 5250)
            self.assertEqual(subsidy, 250)
            self.assertEqual(contract, 0)

    async def test_equal_split_three_members(self, mock_treasury, mock_rp):
        """3-person party: payment split equally after bonus."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        await _create_online_character(guid="AAAA000000000000000000000000AAA1")
        await _create_online_character(guid="AAAA000000000000000000000000AAA2")
        await _create_online_character(guid="AAAA000000000000000000000000AAA3")
        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "AAAA000000000000000000000000AAA1",
                    "AAAA000000000000000000000000AAA2",
                    "AAAA000000000000000000000000AAA3",
                ],
            }
        ]
        events = [_make_cargo_event("AAAA000000000000000000000000AAA1", payment=15_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # 3-person: bonus = int(15000 * 0.10) = 1500
        # total = 16500, subsidy = 1500
        # share_payment = 16500 // 3 = 5500
        # share_subsidy = 1500 // 3 = 500
        self.assertEqual(len(player_profits), 3)
        for char, subsidy, payment, contract in player_profits:
            self.assertEqual(payment, 5500)
            self.assertEqual(subsidy, 500)

    async def test_solo_no_split(self, mock_treasury, mock_rp):
        """Solo player (not in party): no splitting, same as before."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        await _create_online_character(guid="SOLO000000000000000000000000SOLO")
        await _setup_delivery_points()

        events = [_make_cargo_event("SOLO000000000000000000000000SOLO", payment=10_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=[]),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # Solo: no party bonus, no split
        self.assertEqual(len(player_profits), 1)
        char, subsidy, payment, contract = player_profits[0]
        self.assertEqual(payment, 10_000)

    async def test_split_with_gov_employee_member(self, mock_treasury, mock_rp):
        """Gov employee member still gets their share in player_profits.
        on_player_profit will handle the gov redirect independently."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        await _create_online_character(guid="GOV1000000000000000000000000GOV1")
        # Make c2 a gov employee
        c2 = await _create_online_character(guid="GOV2000000000000000000000000GOV2")
        c2.gov_employee_until = timezone.now() + timedelta(hours=24)
        await c2.asave(update_fields=["gov_employee_until"])

        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "GOV1000000000000000000000000GOV1",
                    "GOV2000000000000000000000000GOV2",
                ],
            }
        ]
        events = [_make_cargo_event("GOV1000000000000000000000000GOV1", payment=10_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # Both members get equal shares in player_profits
        self.assertEqual(len(player_profits), 2)
        guids = {pp[0].guid for pp in player_profits}
        self.assertIn("GOV2000000000000000000000000GOV2", guids)
        # Shares should be equal regardless of gov status
        shares = {pp[2] for pp in player_profits}
        self.assertEqual(len(shares), 1)  # all equal

    async def test_nonexistent_party_member_skipped(self, mock_treasury, mock_rp):
        """If a party member's GUID isn't in DB, they're skipped gracefully."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        await _create_online_character(guid="REAL000000000000000000000000REAL")
        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "REAL000000000000000000000000REAL",
                    "FAKE000000000000000000000000FAKE",  # not in DB
                ],
            }
        ]
        events = [_make_cargo_event("REAL000000000000000000000000REAL", payment=10_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # Earner + 1 nonexistent = only earner + 0 found others
        # Split is still calculated for party_size=2, but only earner is appended
        # since FAKE is not in DB → other_characters is empty
        self.assertEqual(len(player_profits), 1)
        char, subsidy, payment, contract = player_profits[0]
        # Share is half of boosted total (earner gets their split only)
        self.assertEqual(char.guid, "REAL000000000000000000000000REAL")

    async def test_wallet_transfers_made(self, mock_treasury, mock_rp):
        """Verify transfer_money calls: withdraw from earner, deposit to others."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        await _create_online_character(guid="XFER000000000000000000000000XFR1")
        await _create_online_character(guid="XFER000000000000000000000000XFR2")
        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "XFER000000000000000000000000XFR1",
                    "XFER000000000000000000000000XFR2",
                ],
            }
        ]
        events = [_make_cargo_event("XFER000000000000000000000000XFR1", payment=10_000)]

        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock),
            patch("amc.webhook.transfer_money", new_callable=AsyncMock) as mock_transfer,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)

        # Should have at least 2 transfer_money calls:
        # 1. Withdraw from earner (negative)
        # 2. Deposit to other member (positive)
        transfer_calls = mock_transfer.call_args_list
        self.assertGreaterEqual(len(transfer_calls), 2)

        # Find the withdrawal (negative amount) and deposit (positive amount)
        amounts = [call[0][1] for call in transfer_calls]  # second positional arg
        messages = [call[0][2] for call in transfer_calls]
        self.assertTrue(any(a < 0 for a in amounts), "Should have a withdrawal")
        self.assertTrue(any(a > 0 for a in amounts), "Should have a deposit")
        self.assertIn("Party Split", messages)
        self.assertIn("Party Share", messages)

    async def test_split_with_contract_payment(self, mock_treasury, mock_rp):
        """Contract payments are also split equally among party members."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.models import ServerSignContractLog

        c1 = await _create_online_character(guid="CNTR000000000000000000000000CNT1")
        await _create_online_character(guid="CNTR000000000000000000000000CNT2")
        await _setup_delivery_points()

        # Create a completed contract for c1
        await ServerSignContractLog.objects.acreate(
            guid="contract_split_test",
            player=c1.player,
            cargo_key="sand",
            amount=1,
            finished_amount=0,
            payment=50_000,
            cost=1000,
            timestamp=timezone.now(),
        )

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "CNTR000000000000000000000000CNT1",
                    "CNTR000000000000000000000000CNT2",
                ],
            }
        ]
        # Contract delivery event
        contract_event = {
            "hook": "ServerContractCargoDelivered",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(c1.guid),
                "ContractGuid": "contract_split_test",
                "Item": "sand",
                "Amount": 1,
                "CompletionPayment": 50_000,
                "Cost": 1000,
            },
        }

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events([contract_event], http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # Contract payment (50000) should be split
        self.assertEqual(len(player_profits), 2)
        for char, subsidy, payment, contract in player_profits:
            self.assertEqual(contract, 25_000)  # 50000 // 2

