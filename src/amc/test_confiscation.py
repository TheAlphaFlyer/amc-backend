import time
from unittest.mock import patch, AsyncMock

from asgiref.sync import sync_to_async
from django.test import TestCase

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import (
    Confiscation,
    FactionChoice,
    FactionMembership,
)
from amc.webhook import handle_pickup_cargo


def _pickup_event(character_guid, payment=5000, previous_owner_guid=None, cargo_key="Money"):
    return {
        "hook": "ServerPickupCargo",
        "timestamp": int(time.time()),
        "data": {
            "CharacterGuid": str(character_guid),
            "Cargo": {
                "Net_CargoKey": cargo_key,
                "Net_Payment": payment,
                "PreviousOwnerCharacterGuid": previous_owner_guid,
            },
        },
    }


@patch("amc.webhook.record_treasury_confiscation_income", new_callable=AsyncMock)
@patch("amc.webhook.despawn_player_cargo", new_callable=AsyncMock)
@patch("amc.webhook.transfer_money", new_callable=AsyncMock)
@patch("amc.webhook.announce", new_callable=AsyncMock)
class ConfiscationHandlerTests(TestCase):
    """Tests for handle_pickup_cargo — police Money confiscation."""

    async def _setup_police_and_criminal(self):
        """Create a police officer and a non-police player."""
        officer_player = await sync_to_async(PlayerFactory)()
        officer = await sync_to_async(CharacterFactory)(player=officer_player)
        await FactionMembership.objects.acreate(
            player=officer_player, faction=FactionChoice.COP
        )

        criminal_player = await sync_to_async(PlayerFactory)()
        criminal = await sync_to_async(CharacterFactory)(player=criminal_player)
        return officer, criminal

    async def test_police_confiscates_money(
        self, mock_announce, mock_transfer, mock_despawn, mock_treasury,
    ):
        """Police picking up Money from non-police triggers full confiscation."""
        officer, criminal = await self._setup_police_and_criminal()

        event = _pickup_event(
            officer.guid, payment=10_000, previous_owner_guid=criminal.guid,
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        await handle_pickup_cargo(event, officer, mock_http, mock_http_mod)

        # Confiscation record created
        self.assertEqual(await Confiscation.objects.acount(), 1)
        conf = await Confiscation.objects.afirst()
        self.assertEqual(conf.character_id, criminal.id)
        self.assertEqual(conf.officer_id, officer.id)
        self.assertEqual(conf.amount, 10_000)

        # Previous owner charged
        mock_transfer.assert_called_once_with(
            mock_http_mod, -10_000, "Money Confiscated",
            str(criminal.player.unique_id),
        )

        # Treasury credited
        mock_treasury.assert_called_once_with(10_000, "Police Confiscation")

        # Cargo despawned
        mock_despawn.assert_called_once_with(mock_http_mod, str(officer.guid))

    async def test_non_police_no_confiscation(
        self, mock_announce, mock_transfer, mock_despawn, mock_treasury,
    ):
        """Non-police picking up Money should not trigger confiscation."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        other_player = await sync_to_async(PlayerFactory)()
        other_char = await sync_to_async(CharacterFactory)(player=other_player)

        event = _pickup_event(
            character.guid, payment=5000, previous_owner_guid=other_char.guid,
        )
        await handle_pickup_cargo(event, character, AsyncMock(), AsyncMock())

        self.assertEqual(await Confiscation.objects.acount(), 0)
        mock_transfer.assert_not_called()

    async def test_non_money_cargo_no_confiscation(
        self, mock_announce, mock_transfer, mock_despawn, mock_treasury,
    ):
        """Police picking up non-Money cargo should not trigger confiscation."""
        officer, criminal = await self._setup_police_and_criminal()

        event = _pickup_event(
            officer.guid, payment=5000, previous_owner_guid=criminal.guid,
            cargo_key="oranges",
        )
        await handle_pickup_cargo(event, officer, AsyncMock(), AsyncMock())

        self.assertEqual(await Confiscation.objects.acount(), 0)
        mock_transfer.assert_not_called()

    async def test_self_confiscation_blocked(
        self, mock_announce, mock_transfer, mock_despawn, mock_treasury,
    ):
        """Police picking up their own Money should not trigger confiscation."""
        officer, _ = await self._setup_police_and_criminal()

        event = _pickup_event(
            officer.guid, payment=5000, previous_owner_guid=officer.guid,
        )
        await handle_pickup_cargo(event, officer, AsyncMock(), AsyncMock())

        self.assertEqual(await Confiscation.objects.acount(), 0)
        mock_transfer.assert_not_called()

    async def test_police_on_police_blocked(
        self, mock_announce, mock_transfer, mock_despawn, mock_treasury,
    ):
        """Police picking up Money from another police officer should not trigger confiscation."""
        officer1_player = await sync_to_async(PlayerFactory)()
        officer1 = await sync_to_async(CharacterFactory)(player=officer1_player)
        await FactionMembership.objects.acreate(
            player=officer1_player, faction=FactionChoice.COP,
        )

        officer2_player = await sync_to_async(PlayerFactory)()
        officer2 = await sync_to_async(CharacterFactory)(player=officer2_player)
        await FactionMembership.objects.acreate(
            player=officer2_player, faction=FactionChoice.COP,
        )

        event = _pickup_event(
            officer1.guid, payment=5000, previous_owner_guid=officer2.guid,
        )
        await handle_pickup_cargo(event, officer1, AsyncMock(), AsyncMock())

        self.assertEqual(await Confiscation.objects.acount(), 0)
        mock_transfer.assert_not_called()

    async def test_missing_previous_owner_guid(
        self, mock_announce, mock_transfer, mock_despawn, mock_treasury,
    ):
        """Missing PreviousOwnerCharacterGuid should not trigger confiscation."""
        officer, _ = await self._setup_police_and_criminal()

        event = _pickup_event(officer.guid, payment=5000, previous_owner_guid=None)
        await handle_pickup_cargo(event, officer, AsyncMock(), AsyncMock())

        self.assertEqual(await Confiscation.objects.acount(), 0)
        mock_transfer.assert_not_called()

    async def test_zero_payment_no_confiscation(
        self, mock_announce, mock_transfer, mock_despawn, mock_treasury,
    ):
        """Zero-payment cargo should not trigger confiscation."""
        officer, criminal = await self._setup_police_and_criminal()

        event = _pickup_event(
            officer.guid, payment=0, previous_owner_guid=criminal.guid,
        )
        await handle_pickup_cargo(event, officer, AsyncMock(), AsyncMock())

        self.assertEqual(await Confiscation.objects.acount(), 0)
        mock_transfer.assert_not_called()
