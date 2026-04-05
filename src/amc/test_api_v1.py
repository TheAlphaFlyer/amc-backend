"""Tests for V1 Public API endpoints."""

from decimal import Decimal
from typing import cast, Any

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone
from ninja.testing import TestAsyncClient

from amc.api.v1.routes import (
    economy_router,
    storage_router,
    characters_router,
    vehicles_router,
    supply_chain_router,
    server_router,
    police_router,
    rescue_router,
)
from amc.factories import (
    CharacterFactory,
    PlayerFactory,
    CargoFactory,
    DeliveryFactory,
    DeliveryPointFactory,
    SupplyChainEventFactory,
    SupplyChainObjectiveFactory,
    SupplyChainContributionFactory,
)
from amc.models import (
    CharacterVehicle,
    DeliveryPointStorage,
    ServerStatus,
    PolicePatrolLog,
    PolicePenaltyLog,
    PoliceShiftLog,
    RescueRequest,
)
from amc_finance.models import Account


# ═══════════════════════════════════════════════════════════════
# Phase 4: Economy
# ═══════════════════════════════════════════════════════════════


class EconomyOverviewAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(economy_router)

    async def test_overview_empty(self):
        """Economy overview returns zeroes when no data exists."""
        response = await cast(Any, self.api_client.get("/overview/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["treasury_balance"], 0.0)
        self.assertEqual(data["total_donations_all_time"], 0.0)
        self.assertEqual(data["total_subsidy_spend_all_time"], 0.0)
        self.assertEqual(data["active_loan_count"], 0)
        self.assertEqual(data["npl_count"], 0)

    async def test_overview_with_treasury(self):
        """Economy overview reflects treasury balance."""
        await Account.objects.acreate(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.GOVERNMENT,
            character=None,
            name="Treasury Fund",
            balance=Decimal("5000000"),
        )
        response = await cast(Any, self.api_client.get("/overview/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["treasury_balance"], 5000000.0)

    async def test_overview_privacy(self):
        """Economy overview does not expose per-character data."""
        response = await cast(Any, self.api_client.get("/overview/"))
        data = response.json()
        # Should only have aggregate fields
        self.assertNotIn("accounts", data)
        self.assertNotIn("characters", data)
        self.assertNotIn("player", data)


class NPLLoansAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(economy_router)

    async def test_npl_empty(self):
        """NPL list is empty when no loans exist."""
        response = await cast(Any, self.api_client.get("/npl/"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    async def test_npl_with_overdue_loan(self):
        """NPL list includes characters with overdue loans."""
        character = await sync_to_async(CharacterFactory)()
        await Account.objects.acreate(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.BANK,
            character=character,
            name=f"Loan #{character.id}",
            balance=Decimal("1000000"),  # Above NPL_MIN_BALANCE (500k)
        )
        response = await cast(Any, self.api_client.get("/npl/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(len(data), 1)
        npl = data[0]
        self.assertEqual(npl["character_name"], character.name)
        self.assertGreater(npl["loan_balance"], 0)
        # Privacy check: no discord ID, no money
        self.assertNotIn("discord_user_id", npl)
        self.assertNotIn("money", npl)


class DonationsLeaderboardAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(economy_router)

    async def test_empty_leaderboard(self):
        response = await cast(Any, self.api_client.get("/donations/leaderboard/"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    async def test_leaderboard_with_donors(self):
        await sync_to_async(CharacterFactory)(total_donations=100000)
        await sync_to_async(CharacterFactory)(total_donations=50000)
        response = await cast(Any, self.api_client.get("/donations/leaderboard/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(len(data), 2)


# ═══════════════════════════════════════════════════════════════
# Phase 4: Storage
# ═══════════════════════════════════════════════════════════════


class DeliveryPointStorageAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(storage_router)

    async def test_single_point_storage(self):
        dp = await sync_to_async(DeliveryPointFactory)()
        cargo = await sync_to_async(CargoFactory)(key="C::Stone", label="Stone")
        await DeliveryPointStorage.objects.acreate(
            delivery_point=dp,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="C::Stone",
            cargo=cargo,
            amount=50,
            capacity=100,
        )
        response = await cast(Any, self.api_client.get(f"/{dp.guid}/storage/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["cargo_key"], "C::Stone")
        self.assertEqual(data[0]["amount"], 50)
        self.assertEqual(data[0]["capacity"], 100)

    async def test_bulk_storage(self):
        dp = await sync_to_async(DeliveryPointFactory)()
        cargo = await sync_to_async(CargoFactory)(key="C::Wood", label="Wood")
        await DeliveryPointStorage.objects.acreate(
            delivery_point=dp,
            kind=DeliveryPointStorage.Kind.OUTPUT,
            cargo_key="C::Wood",
            cargo=cargo,
            amount=30,
        )
        response = await cast(Any, self.api_client.get("/storage/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(len(data), 1)


# ═══════════════════════════════════════════════════════════════
# Phase 4: Characters
# ═══════════════════════════════════════════════════════════════


class CharacterProfileAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(characters_router)

    async def test_profile(self):
        character = await sync_to_async(CharacterFactory)()
        response = await cast(Any, self.api_client.get(f"/{character.id}/profile/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], character.id)
        self.assertEqual(data["name"], character.name)
        self.assertIn("credit_score_tier", data)
        self.assertIn("is_government_employee", data)
        self.assertIn("total_donations", data)

    async def test_profile_privacy(self):
        """Profile must NOT expose private fields."""
        character = await sync_to_async(CharacterFactory)()
        response = await cast(Any, self.api_client.get(f"/{character.id}/profile/"))
        data = response.json()
        self.assertNotIn("money", data)
        self.assertNotIn("social_score", data)
        self.assertNotIn("discord_user_id", data)
        self.assertNotIn("credit_score", data)  # raw score hidden, tier exposed
        self.assertNotIn("bank_balance", data)


class CharacterVehiclesAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(characters_router)

    async def test_vehicles_empty(self):
        character = await sync_to_async(CharacterFactory)()
        response = await cast(Any, self.api_client.get(f"/{character.id}/vehicles/"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    async def test_vehicles_with_data(self):
        character = await sync_to_async(CharacterFactory)()
        await CharacterVehicle.objects.acreate(
            character=character,
            vehicle_id=1,
            alias="My Tuscan",
            config={"VehicleName": "Tuscan"},
            for_sale=True,
            rental=False,
        )
        response = await cast(Any, self.api_client.get(f"/{character.id}/vehicles/"))
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["alias"], "My Tuscan")
        self.assertEqual(data[0]["vehicle_name"], "Tuscan")
        self.assertTrue(data[0]["for_sale"])


class CharacterDeliveriesAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(characters_router)

    async def test_deliveries(self):
        character = await sync_to_async(CharacterFactory)()
        await sync_to_async(DeliveryFactory)(
            character=character,
            timestamp=timezone.now(),
            cargo_key="SmallBox",
            quantity=10,
            payment=5000,
        )
        response = await cast(Any, self.api_client.get(f"/{character.id}/deliveries/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["cargo_key"], "SmallBox")
        # Privacy: payment should NOT be in response
        self.assertNotIn("payment", data[0])
        self.assertNotIn("subsidy", data[0])


# ═══════════════════════════════════════════════════════════════
# Phase 4: Vehicles
# ═══════════════════════════════════════════════════════════════


class VehicleCatalogAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(vehicles_router)

    async def test_catalog(self):
        response = await cast(Any, self.api_client.get("/catalog/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreater(len(data), 10)
        first = data[0]
        self.assertIn("key", first)
        self.assertIn("label", first)
        self.assertIn("cost", first)

    async def test_enums(self):
        response = await cast(Any, self.api_client.get("/enums/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("vehicles", data)
        self.assertIn("cargos", data)
        self.assertGreater(len(data["vehicles"]), 10)
        self.assertGreater(len(data["cargos"]), 10)


# ═══════════════════════════════════════════════════════════════
# Phase 5: Supply Chain Events
# ═══════════════════════════════════════════════════════════════


class SupplyChainEventsAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(supply_chain_router)

    async def test_list_events(self):
        event = await sync_to_async(SupplyChainEventFactory)()
        response = await cast(Any, self.api_client.get("/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(len(data), 1)
        self.assertEqual(data[0]["name"], event.name)

    async def test_event_detail(self):
        event = await sync_to_async(SupplyChainEventFactory)()
        cargo = await sync_to_async(CargoFactory)(key="C::Test", label="Test Cargo")
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, is_primary=True
        )
        await obj.cargos.aadd(cargo)

        response = await cast(Any, self.api_client.get(f"/{event.id}/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], event.name)
        self.assertEqual(len(data["objectives"]), 1)
        self.assertTrue(data["objectives"][0]["is_primary"])

    async def test_leaderboard(self):
        event = await sync_to_async(SupplyChainEventFactory)()
        obj = await sync_to_async(SupplyChainObjectiveFactory)(event=event)
        await sync_to_async(SupplyChainContributionFactory)(objective=obj, quantity=100)
        response = await cast(Any, self.api_client.get(f"/{event.id}/leaderboard/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["total_quantity"], 100)


# ═══════════════════════════════════════════════════════════════
# Phase 6: Server Status
# ═══════════════════════════════════════════════════════════════


class ServerStatusAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(server_router)

    async def test_status_empty(self):
        """Returns 204 when no status data exists."""
        response = await cast(Any, self.api_client.get("/status/"))
        self.assertEqual(response.status_code, 204)

    async def test_status_latest(self):
        await ServerStatus.objects.acreate(
            num_players=15, fps=60, used_memory=2_000_000_000
        )
        response = await cast(Any, self.api_client.get("/status/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["num_players"], 15)
        self.assertEqual(data["fps"], 60)

    async def test_status_history(self):
        for i in range(3):
            await ServerStatus.objects.acreate(
                num_players=10 + i, fps=60, used_memory=1_000_000_000
            )
        response = await cast(Any, self.api_client.get("/status/history/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 3)


# ═══════════════════════════════════════════════════════════════
# Phase 6: Police & Rescue
# ═══════════════════════════════════════════════════════════════


class PoliceStatsAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(police_router)

    async def test_stats_empty(self):
        response = await cast(Any, self.api_client.get("/stats/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_patrols"], 0)
        self.assertEqual(data["total_penalties"], 0)
        self.assertEqual(data["active_shifts"], 0)

    async def test_stats_with_data(self):
        player = await sync_to_async(PlayerFactory)()
        now = timezone.now()
        await PolicePatrolLog.objects.acreate(
            timestamp=now, player=player, patrol_point_id=1
        )
        await PolicePenaltyLog.objects.acreate(
            timestamp=now, player=player, warning_only=False
        )
        await PoliceShiftLog.objects.acreate(
            timestamp=now, player=player, action=PoliceShiftLog.Action.START
        )
        response = await cast(Any, self.api_client.get("/stats/"))
        data = response.json()
        self.assertEqual(data["total_patrols"], 1)
        self.assertEqual(data["total_penalties"], 1)
        self.assertEqual(data["active_shifts"], 1)

    async def test_stats_privacy(self):
        """Police stats should not expose player names."""
        response = await cast(Any, self.api_client.get("/stats/"))
        data = response.json()
        self.assertNotIn("player", data)
        self.assertNotIn("player_name", data)


class RescueAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(rescue_router)

    async def test_recent_empty(self):
        response = await cast(Any, self.api_client.get("/recent/"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    async def test_recent_with_data(self):
        character = await sync_to_async(CharacterFactory)()
        await RescueRequest.objects.acreate(
            character=character,
            message="Need help!",
            location=Point(100, 200, 300),
        )
        response = await cast(Any, self.api_client.get("/recent/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["message"], "Need help!")
        self.assertEqual(data[0]["responder_count"], 0)
        self.assertIsNotNone(data[0]["location"])
        # Privacy: no character or player names
        self.assertNotIn("character_name", data[0])
        self.assertNotIn("player", data[0])
