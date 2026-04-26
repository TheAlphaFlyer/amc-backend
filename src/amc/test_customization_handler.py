"""Tests for the ServerSetEquipmentInventory webhook handler."""

from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase

from amc.factories import CharacterFactory, PlayerFactory
from amc.handlers import dispatch
from amc.models import CriminalRecord
from amc.webhook_context import EventContext


def _make_ctx(**kwargs):
    defaults = dict(
        http_client=None,
        http_client_mod=AsyncMock(),
        discord_client=None,
        treasury_balance=0,
        is_rp_mode=False,
        used_shortcut=False,
        active_term=None,
    )
    defaults.update(kwargs)
    return EventContext(**defaults)


def _make_event(equipped=None, unequipped=None, character_guid="A" * 32):
    return {
        "hook": "ServerSetEquipmentInventory",
        "data": {
            "UniqueID": "12345",
            "CharacterGuid": character_guid,
            "Equipped": equipped or [],
            "Unequipped": unequipped or [],
        },
    }


class CostumeEquipWithRecordTests(TestCase):
    async def test_costume_equipped_with_record_makes_suspect(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CriminalRecord.objects.acreate(character=character, reason="Test")

        event = _make_event(
            equipped=[{"Slot": 4, "ItemKey": "Costume_Police_01"}],
            character_guid=character.guid,
        )
        ctx = _make_ctx()

        with patch("amc.handlers.customization.settings.SUSPECT_COSTUMES", frozenset({"Costume_Police_01"})), \
             patch("amc.handlers.customization.make_suspect", new_callable=AsyncMock) as mock_suspect:
            await dispatch("ServerSetEquipmentInventory", event, player, character, ctx)

        await character.arefresh_from_db()
        self.assertTrue(character.wearing_costume)
        self.assertEqual(character.costume_item_key, "Costume_Police_01")
        mock_suspect.assert_called_once_with(
            ctx.http_client_mod, character.guid, duration_seconds=70,
        )

    async def test_costume_equipped_without_record_no_suspect(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        event = _make_event(
            equipped=[{"Slot": 4, "ItemKey": "Costume_Police_01"}],
            character_guid=character.guid,
        )
        ctx = _make_ctx()

        with patch("amc.handlers.customization.settings.SUSPECT_COSTUMES", frozenset({"Costume_Police_01"})), \
             patch("amc.handlers.customization.make_suspect", new_callable=AsyncMock) as mock_suspect:
            await dispatch("ServerSetEquipmentInventory", event, player, character, ctx)

        await character.arefresh_from_db()
        self.assertTrue(character.wearing_costume)
        self.assertEqual(character.costume_item_key, "Costume_Police_01")
        mock_suspect.assert_not_called()


class CostumeUnequipTests(TestCase):
    async def test_costume_unequipped_clears_fields(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, wearing_costume=True, costume_item_key="Costume_Police_01",
        )
        await character.asave(update_fields=["wearing_costume", "costume_item_key"])

        event = _make_event(
            unequipped=[{"Slot": 4, "ItemKey": "Costume_Police_01"}],
            character_guid=character.guid,
        )
        ctx = _make_ctx()

        with patch("amc.handlers.customization.make_suspect", new_callable=AsyncMock) as mock_suspect:
            await dispatch("ServerSetEquipmentInventory", event, player, character, ctx)

        await character.arefresh_from_db()
        self.assertFalse(character.wearing_costume)
        self.assertIsNone(character.costume_item_key)
        mock_suspect.assert_not_called()


class NonCostumeSlotTests(TestCase):
    async def test_hat_slot_ignored(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        event = _make_event(
            equipped=[{"Slot": 1, "ItemKey": "Hat_01"}],
            character_guid=character.guid,
        )
        ctx = _make_ctx()

        with patch("amc.handlers.customization.make_suspect", new_callable=AsyncMock) as mock_suspect:
            result = await dispatch("ServerSetEquipmentInventory", event, player, character, ctx)

        await character.arefresh_from_db()
        self.assertFalse(character.wearing_costume)
        self.assertIsNone(character.costume_item_key)
        mock_suspect.assert_not_called()
        self.assertEqual(result, (0, 0, 0, 0))


class ArrestResetTests(TestCase):
    async def test_arrest_resets_costume_fields(self):
        from amc.commands.faction import execute_arrest
        from amc.models import TeleportPoint

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            wearing_costume=True,
            costume_item_key="Costume_Police_01",
        )
        await character.asave(update_fields=["wearing_costume", "costume_item_key"])
        await CriminalRecord.objects.acreate(
            character=character, reason="Test", confiscatable_amount=0,
        )

        await sync_to_async(TeleportPoint.objects.create)(
            name="Jail",
            location="POINT (0 0 0)",
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mod.post = AsyncMock(return_value=AsyncMock(status=200))

        targets = {character.guid: (str(player.unique_id), (0, 0, 0), False)}
        target_chars = {character.guid: character}

        with patch("amc.commands.faction.refresh_player_name", new_callable=AsyncMock), \
             patch("amc.commands.faction.get_active_police_characters", new_callable=AsyncMock, return_value=[]), \
             patch("amc.commands.faction.force_exit_vehicle", new_callable=AsyncMock), \
             patch("amc.commands.faction.teleport_player", new_callable=AsyncMock), \
             patch("amc.commands.faction.show_popup", new_callable=AsyncMock):
            await execute_arrest(
                officer_character=None,
                targets=targets,
                target_chars=target_chars,
                http_client=mock_http,
                http_client_mod=mock_http_mod,
            )

        await character.arefresh_from_db()
        self.assertFalse(character.wearing_costume)
        self.assertIsNone(character.costume_item_key)
