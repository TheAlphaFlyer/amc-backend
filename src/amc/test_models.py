from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from django.contrib.gis.geos import Point
from asgiref.sync import sync_to_async
from amc.factories import CharacterFactory, ChampionshipFactory, ChampionshipPointFactory
from amc.models import CharacterLocation, Character

class CharacterLocationTestCase(TestCase):
  async def test_activity(self):
    character = await sync_to_async(CharacterFactory)()
    for n in range(0, 1000): 
      await CharacterLocation.objects.acreate(
        timestamp=timezone.now() - timedelta(hours=1) + timedelta(seconds=n),
        character=character,
        location=Point(10000 + n * 100, 10000 + n * 100, 0),
      )
    is_online, is_active = await CharacterLocation.get_character_activity(
      character,
      timezone.now() - timedelta(hours=1),
      timezone.now() - timedelta(seconds=0),
    )
    self.assertTrue(is_online)
    self.assertTrue(is_active)

  async def test_activity_offline(self):
    character = await sync_to_async(CharacterFactory)()
    is_online, is_active = await CharacterLocation.get_character_activity(
      character,
      timezone.now() - timedelta(hours=1),
      timezone.now() - timedelta(seconds=0),
    )
    self.assertFalse(is_online)
    self.assertFalse(is_active)

  async def test_activity_afk(self):
    character = await sync_to_async(CharacterFactory)()
    for n in range(0, 1000): 
      await CharacterLocation.objects.acreate(
        timestamp=timezone.now() - timedelta(hours=1) + timedelta(seconds=n),
        character=character,
        location=Point(10000 + n * 0.1, 10000 + n * 0.1, 0),
      )
    is_online, is_active = await CharacterLocation.get_character_activity(
      character,
      timezone.now() - timedelta(hours=1),
      timezone.now() - timedelta(seconds=0),
    )
    self.assertTrue(is_online)
    self.assertFalse(is_active)

  async def test_batch_character_activity(self):
    """Batch version should produce same results as individual calls."""
    active_char = await sync_to_async(CharacterFactory)()
    afk_char = await sync_to_async(CharacterFactory)()
    offline_char = await sync_to_async(CharacterFactory)()

    # Active character: large movement
    for n in range(0, 100):
      await CharacterLocation.objects.acreate(
        timestamp=timezone.now() - timedelta(hours=1) + timedelta(seconds=n),
        character=active_char,
        location=Point(10000 + n * 100, 10000 + n * 100, 0),
      )

    # AFK character: tiny movement
    for n in range(0, 100):
      await CharacterLocation.objects.acreate(
        timestamp=timezone.now() - timedelta(hours=1) + timedelta(seconds=n),
        character=afk_char,
        location=Point(10000 + n * 0.1, 10000 + n * 0.1, 0),
      )

    # Offline character: no locations at all

    start = timezone.now() - timedelta(hours=1)
    end = timezone.now()

    result = await CharacterLocation.batch_get_character_activity(
      [active_char, afk_char, offline_char],
      start, end,
    )

    # Active character
    self.assertEqual(result[active_char.id], (True, True))
    # AFK character (online but not active)
    self.assertEqual(result[afk_char.id], (True, False))
    # Offline character
    self.assertEqual(result[offline_char.id], (False, False))

class ChampionshipTestCase(TestCase):
  async def test_award_personal_prizes(self):
    championship = await sync_to_async(ChampionshipFactory)()
    await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
    )
    await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
    )
    prizes = await championship.calculate_personal_prizes()
    print(prizes)

  async def test_award_team_prizes(self):
    championship = await sync_to_async(ChampionshipFactory)()
    p1 = await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
    )
    await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
      team=p1.team
    )
    await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
    )
    prizes = await championship.calculate_team_prizes()
    print(prizes)

class CharacterMangerTestCase(TestCase):
  async def test_change_name(self):
    character1, *_ = await Character.objects.aget_or_create_character_player('test', 123, character_guid=234)
    character2, *_ = await Character.objects.aget_or_create_character_player('test2', 123, character_guid=234)
    self.assertEqual(character1.id, character2.id)

  async def test_add_guid(self):
    character1, *_ = await Character.objects.aget_or_create_character_player('test', 123)
    character2, *_ = await Character.objects.aget_or_create_character_player('test', 123, character_guid=234)
    self.assertEqual(character1.id, character2.id)

  async def test_missing_guid(self):
    character1, *_ = await Character.objects.aget_or_create_character_player('test', 123, character_guid=234)
    character2, *_ = await Character.objects.aget_or_create_character_player('test', 123)
    self.assertEqual(character1.id, character2.id)

  async def test_new_alt(self):
    character1, *_ = await Character.objects.aget_or_create_character_player('test', 123, character_guid=234)
    character2, *_ = await Character.objects.aget_or_create_character_player('test', 123, character_guid=345)
    self.assertNotEqual(character1.id, character2.id)

