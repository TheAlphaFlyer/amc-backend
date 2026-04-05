"""Tests for illicit cargo: Wanted trigger, Delivery↔Wanted link, contraband handler."""

import time
from datetime import timedelta
from unittest.mock import patch, AsyncMock

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import (
    CriminalRecord,
    CharacterLocation,
    Delivery,
    DeliveryPoint,
    Wanted,
)
from amc.special_cargo import ILLICIT_CARGO_KEYS
from amc.webhook import process_event


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class IllicitCargoWantedTests(TestCase):
    """All illicit cargo keys should trigger Wanted status and link Delivery."""

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )
        return player, character

    def _cargo_event(self, character, cargo_key, payment=5000):
        return {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": cargo_key,
                        "Net_Payment": payment,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

    # ------------------------------------------------------------------
    # Wanted creation
    # ------------------------------------------------------------------

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_ganja_creates_wanted(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)
        self.assertEqual(wanted.wanted_remaining, Wanted.INITIAL_WANTED_SECONDS)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_cocaine_creates_wanted(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Cocaine")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_coca_leaves_pallet_creates_wanted(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "CocaLeavesPallet")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_ganja_pallet_creates_wanted(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "GanjaPallet")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_money_pallet_creates_wanted(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "MoneyPallet")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)

    # ------------------------------------------------------------------
    # Wanted refresh (existing wanted gets timer reset)
    # ------------------------------------------------------------------

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_contraband_refreshes_existing_wanted(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        """If already wanted, a new contraband delivery resets the countdown."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        # Pre-existing wanted with 60 seconds remaining
        await Wanted.objects.acreate(
            character=character,
            wanted_remaining=60,
        )

        event = self._cargo_event(character, "Ganja")
        await process_event(event, player, character)

        wanted_records = [
            w
            async for w in Wanted.objects.filter(
                character=character, expired_at__isnull=True
            )
        ]
        self.assertEqual(len(wanted_records), 1, "Should not create a second wanted")
        self.assertEqual(
            wanted_records[0].wanted_remaining, Wanted.INITIAL_WANTED_SECONDS
        )

    # ------------------------------------------------------------------
    # Delivery → Wanted FK link
    # ------------------------------------------------------------------

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_delivery_linked_to_wanted_for_ganja(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja", payment=10_000)
        await process_event(event, player, character)

        delivery = await Delivery.objects.filter(
            character=character, cargo_key="Ganja"
        ).afirst()
        self.assertIsNotNone(delivery)
        self.assertIsNotNone(delivery.wanted_id)

        wanted = await Wanted.objects.aget(pk=delivery.wanted_id)
        self.assertEqual(wanted.character_id, character.id)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_delivery_linked_to_wanted_for_money(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Money", payment=10_000)
        await process_event(event, player, character)

        delivery = await Delivery.objects.filter(
            character=character, cargo_key="Money"
        ).afirst()
        self.assertIsNotNone(delivery)
        self.assertIsNotNone(delivery.wanted_id)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_non_illicit_delivery_has_no_wanted(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        """Non-illicit cargo should NOT create Wanted or link delivery."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Coal", payment=5_000)
        await process_event(event, player, character)

        self.assertEqual(
            await Wanted.objects.filter(character=character).acount(), 0
        )
        delivery = await Delivery.objects.filter(
            character=character, cargo_key="Coal"
        ).afirst()
        if delivery:
            self.assertIsNone(delivery.wanted_id)


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class ContrabandCriminalRecordTests(TestCase):
    """Contraband deliveries should create criminal records with cargo-specific reasons."""

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )
        return player, character

    def _cargo_event(self, character, cargo_key, payment=5000):
        return {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": cargo_key,
                        "Net_Payment": payment,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_ganja_creates_criminal_record(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja")
        await process_event(event, player, character)

        record = await CriminalRecord.objects.filter(character=character).afirst()
        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "Ganja delivery")
        self.assertAlmostEqual(
            record.expires_at.timestamp(),
            (timezone.now() + timedelta(days=7)).timestamp(),
            delta=5,
        )

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_cocaine_creates_criminal_record(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Cocaine")
        await process_event(event, player, character)

        record = await CriminalRecord.objects.filter(character=character).afirst()
        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "Cocaine delivery")

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_contraband_resets_existing_criminal_record(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        """Repeat contraband delivery should reset the expiry, not create a duplicate."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        # Pre-existing record expiring in 3 days
        await CriminalRecord.objects.acreate(
            character=character,
            reason="Ganja delivery",
            expires_at=timezone.now() + timedelta(days=3),
        )

        event = self._cargo_event(character, "Cocaine")
        await process_event(event, player, character)

        records = [r async for r in CriminalRecord.objects.filter(character=character)]
        self.assertEqual(len(records), 1, "Should not create a second record")
        # Expiry reset to 7 days from now
        self.assertAlmostEqual(
            records[0].expires_at.timestamp(),
            (timezone.now() + timedelta(days=7)).timestamp(),
            delta=5,
        )

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_contraband_does_not_accumulate_laundered_total(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        """Contraband (non-Money) should NOT increment criminal_laundered_total."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja", payment=50_000)
        await process_event(event, player, character)

        await character.arefresh_from_db()
        self.assertEqual(character.criminal_laundered_total, 0)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_contraband_no_treasury_expense(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        """Contraband should NOT incur the 20% money laundering treasury cost."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja", payment=50_000)
        await process_event(event, player, character)

        mock_treasury.assert_not_called()

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_player_tag_refreshed_for_contraband(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        """Player name tag should be refreshed for contraband (via create_or_refresh_wanted)."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Cocaine")
        await process_event(event, player, character)

        mock_refresh.assert_called_once_with(character, None)


class IllicitCargoKeysRegistryTests(TestCase):
    """Verify that the ILLICIT_CARGO_KEYS set and handler registry are consistent."""

    def test_illicit_cargo_keys_contains_all_expected(self):
        expected = {"Money", "Ganja", "CocaLeavesPallet", "GanjaPallet", "Cocaine", "MoneyPallet"}
        self.assertEqual(ILLICIT_CARGO_KEYS, expected)

    def test_all_illicit_keys_have_handlers(self):
        from amc.special_cargo import SPECIAL_CARGO_HANDLERS

        for key in ILLICIT_CARGO_KEYS:
            self.assertIn(key, SPECIAL_CARGO_HANDLERS, f"{key} missing from handler registry")

    def test_no_non_illicit_keys_in_handlers(self):
        from amc.special_cargo import SPECIAL_CARGO_HANDLERS

        for key in SPECIAL_CARGO_HANDLERS:
            self.assertIn(key, ILLICIT_CARGO_KEYS, f"{key} in handlers but not in ILLICIT_CARGO_KEYS")
