from django.test import TestCase
from django.contrib.gis.geos import Point
from django.utils import timezone
from datetime import timedelta

from amc.models import Player, Character, CharacterLocation, CharacterLocationStats
from amc.characterlocation_stats import refresh_vehicle_stats, refresh_all_vehicle_stats


class VehicleStatsTestCase(TestCase):
    async def test_basic_favourite_vehicle(self):
        """Character with multiple vehicle types gets correct favourite."""
        player = await Player.objects.acreate(unique_id=1001)
        char = await Character.objects.acreate(player=player, name="Driver1")
        now = timezone.now()

        # 10 rows as Titan, 5 as Kuda, 2 as Fox
        for i in range(10):
            await CharacterLocation.objects.acreate(
                character=char,
                location=Point(i * 100, 0, 0),
                vehicle_key="Titan",
                timestamp=now - timedelta(seconds=100 - i),
            )
        for i in range(5):
            await CharacterLocation.objects.acreate(
                character=char,
                location=Point(i * 100, 100, 0),
                vehicle_key="Kuda",
                timestamp=now - timedelta(seconds=200 - i),
            )
        for i in range(2):
            await CharacterLocation.objects.acreate(
                character=char,
                location=Point(i * 100, 200, 0),
                vehicle_key="Fox",
                timestamp=now - timedelta(seconds=300 - i),
            )

        await refresh_vehicle_stats(char)

        stats = await CharacterLocationStats.objects.aget(character=char)
        self.assertEqual(stats.favourite_vehicle, "Titan")
        self.assertEqual(stats.vehicle_stats["Titan"], 10)
        self.assertEqual(stats.vehicle_stats["Kuda"], 5)
        self.assertEqual(stats.vehicle_stats["Fox"], 2)
        self.assertEqual(stats.total_location_records, 17)
        self.assertIsNotNone(stats.last_computed_at)

    async def test_incremental_update(self):
        """Incremental refresh merges new data correctly."""
        player = await Player.objects.acreate(unique_id=1002)
        char = await Character.objects.acreate(player=player, name="Driver2")
        now = timezone.now()

        # Initial: 5 Titan rows
        for i in range(5):
            await CharacterLocation.objects.acreate(
                character=char,
                location=Point(i * 100, 0, 0),
                vehicle_key="Titan",
                timestamp=now - timedelta(hours=2, seconds=-i),
            )

        await refresh_vehicle_stats(char)
        stats = await CharacterLocationStats.objects.aget(character=char)
        self.assertEqual(stats.vehicle_stats["Titan"], 5)
        since = stats.last_computed_at

        # Add 3 more Kuda rows after the since timestamp
        for i in range(3):
            await CharacterLocation.objects.acreate(
                character=char,
                location=Point(i * 100, 100, 0),
                vehicle_key="Kuda",
                timestamp=now - timedelta(seconds=10 - i),
            )

        await refresh_vehicle_stats(char, since=since)
        stats = await CharacterLocationStats.objects.aget(character=char)
        # Titan should still be 5, Kuda should be 3
        self.assertEqual(stats.vehicle_stats["Titan"], 5)
        self.assertEqual(stats.vehicle_stats["Kuda"], 3)
        self.assertEqual(stats.total_location_records, 8)
        # Favourite is still Titan
        self.assertEqual(stats.favourite_vehicle, "Titan")

    async def test_null_vehicle_key_excluded(self):
        """Rows with vehicle_key=NULL are not counted."""
        player = await Player.objects.acreate(unique_id=1003)
        char = await Character.objects.acreate(player=player, name="Driver3")
        now = timezone.now()

        # 3 with vehicle, 5 without
        for i in range(3):
            await CharacterLocation.objects.acreate(
                character=char,
                location=Point(i * 100, 0, 0),
                vehicle_key="Fox",
                timestamp=now - timedelta(seconds=100 - i),
            )
        for i in range(5):
            await CharacterLocation.objects.acreate(
                character=char,
                location=Point(i * 100, 100, 0),
                vehicle_key=None,
                timestamp=now - timedelta(seconds=200 - i),
            )

        await refresh_vehicle_stats(char)
        stats = await CharacterLocationStats.objects.aget(character=char)
        self.assertEqual(stats.favourite_vehicle, "Fox")
        self.assertEqual(stats.total_location_records, 3)
        self.assertNotIn(None, stats.vehicle_stats)

    async def test_no_location_data(self):
        """Character with no location data creates no stats row."""
        player = await Player.objects.acreate(unique_id=1004)
        char = await Character.objects.acreate(player=player, name="NoData")

        await refresh_vehicle_stats(char)
        self.assertFalse(
            await CharacterLocationStats.objects.filter(character=char).aexists()
        )

    async def test_refresh_all(self):
        """refresh_all_vehicle_stats processes active characters."""
        now = timezone.now()
        player = await Player.objects.acreate(unique_id=1005)
        char = await Character.objects.acreate(
            player=player, name="Active", last_online=now
        )

        for i in range(3):
            await CharacterLocation.objects.acreate(
                character=char,
                location=Point(i * 100, 0, 0),
                vehicle_key="Titan",
                timestamp=now - timedelta(seconds=10 - i),
            )

        await refresh_all_vehicle_stats()

        stats = await CharacterLocationStats.objects.aget(character=char)
        self.assertEqual(stats.favourite_vehicle, "Titan")
        self.assertEqual(stats.total_location_records, 3)
