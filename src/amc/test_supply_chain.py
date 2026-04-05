from datetime import timedelta
from unittest.mock import patch, AsyncMock, MagicMock

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.contrib.gis.geos import Point
from django.utils import timezone

from amc.factories import (
    PlayerFactory,
    CharacterFactory,
    SupplyChainEventFactory,
    SupplyChainObjectiveFactory,
    SupplyChainEventTemplateFactory,
    SupplyChainObjectiveTemplateFactory,
)
from amc.models import (
    Cargo,
    DeliveryPoint,
    SupplyChainContribution,
    SupplyChainEvent,
)
from amc.supply_chain import (
    check_and_record_contribution,
    create_event_from_template,
    distribute_event_rewards,
    get_conflicting_cargo_keys,
    monitor_supply_chain_events,
)


# ── Helpers ──────────────────────────────────────────────────────────


async def _make_player_and_char(name="Tester"):
    player = await sync_to_async(PlayerFactory)()
    char = await sync_to_async(CharacterFactory)(player=player, name=name)
    return player, char


async def _make_active_event(**overrides):
    defaults = dict(
        start_at=timezone.now() - timedelta(hours=1),
        end_at=timezone.now() + timedelta(hours=23),
    )
    defaults.update(overrides)
    return await sync_to_async(SupplyChainEventFactory)(**defaults)


async def _make_ended_event(**overrides):
    defaults = dict(
        start_at=timezone.now() - timedelta(hours=25),
        end_at=timezone.now() - timedelta(hours=1),
    )
    defaults.update(overrides)
    return await sync_to_async(SupplyChainEventFactory)(**defaults)


# ── Contribution Recording Tests ─────────────────────────────────────


@patch("amc.supply_chain.send_fund_to_player", new_callable=AsyncMock)
class ContributionRecordingTests(TestCase):
    """Tests for check_and_record_contribution — matching, capping, recording."""

    async def asyncSetUp(self):
        _, self.character = await _make_player_and_char()
        self.source = await DeliveryPoint.objects.acreate(
            guid="src1", name="Mine", coord=Point(0, 0, 0)
        )
        self.dest = await DeliveryPoint.objects.acreate(
            guid="dst1", name="Factory", coord=Point(100, 100, 0)
        )
        self.cargo = await Cargo.objects.acreate(key="C::Coal", label="Coal")

    async def _contribute(self, cargo_key="C::Coal", quantity=5, dest=None, src=None):
        return await check_and_record_contribution(
            delivery=None,
            character=self.character,
            cargo_key=cargo_key,
            quantity=quantity,
            destination_point=dest or self.dest,
            source_point=src or self.source,
        )

    # ── Basic matching ──

    async def test_matching_cargo_and_dest(self, mock_send):
        """Contribution created when cargo + destination match an active objective."""
        await self.asyncSetUp()
        event = await _make_active_event()
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            ceiling=100,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )

        await self._contribute(quantity=5)

        self.assertEqual(await SupplyChainContribution.objects.acount(), 1)
        contrib = await SupplyChainContribution.objects.afirst()
        self.assertEqual(contrib.quantity, 5)
        self.assertEqual(contrib.character_id, self.character.id)
        await obj.arefresh_from_db()
        self.assertEqual(obj.quantity_fulfilled, 5)

    async def test_wrong_cargo_no_match(self, mock_send):
        """Delivering a cargo type not in the objective does NOT match."""
        await self.asyncSetUp()
        event = await _make_active_event()
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )

        result = await self._contribute(cargo_key="C::Iron")

        self.assertEqual(result, 0)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 0)

    async def test_wrong_destination_no_match(self, mock_send):
        """Delivering to a different destination doesn't match."""
        await self.asyncSetUp()
        other_dest = await DeliveryPoint.objects.acreate(
            guid="other", name="Other", coord=Point(200, 200, 0)
        )
        event = await _make_active_event()
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )

        result = await self._contribute(dest=other_dest)

        self.assertEqual(result, 0)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 0)

    async def test_source_point_filter(self, mock_send):
        """Objective with source_points only matches deliveries from those sources."""
        await self.asyncSetUp()
        wrong_source = await DeliveryPoint.objects.acreate(
            guid="wrong_src", name="Wrong Mine", coord=Point(300, 300, 0)
        )
        event = await _make_active_event()
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            cargos=[self.cargo],
            destination_points=[self.dest],
            source_points=[self.source],  # Only from THIS source
        )

        # Deliver from wrong source → no match
        result = await self._contribute(src=wrong_source)
        self.assertEqual(result, 0)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 0)

        # Deliver from correct source → match
        result = await self._contribute(src=self.source)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 1)

    async def test_no_cargo_filter_matches_any(self, mock_send):
        """Objective with empty cargos M2M matches any cargo key."""
        await self.asyncSetUp()
        event = await _make_active_event()
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            destination_points=[self.dest],
            # No cargos → matches any
        )

        await self._contribute(cargo_key="C::Anything")

        self.assertEqual(await SupplyChainContribution.objects.acount(), 1)

    async def test_no_destination_filter_matches_any(self, mock_send):
        """Objective with empty destination_points M2M matches any destination."""
        await self.asyncSetUp()
        random_dest = await DeliveryPoint.objects.acreate(
            guid="random", name="Random", coord=Point(500, 500, 0)
        )
        event = await _make_active_event()
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            cargos=[self.cargo],
            # No destination_points → matches anywhere
        )

        await self._contribute(dest=random_dest)

        self.assertEqual(await SupplyChainContribution.objects.acount(), 1)

    # ── Event lifecycle ──

    async def test_expired_event_no_match(self, mock_send):
        """Expired events are not active and don't match deliveries."""
        await self.asyncSetUp()
        event = await _make_ended_event()
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )

        result = await self._contribute()

        self.assertEqual(result, 0)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 0)

    async def test_future_event_no_match(self, mock_send):
        """Events that haven't started yet don't match deliveries."""
        await self.asyncSetUp()
        event = await _make_active_event(
            start_at=timezone.now() + timedelta(hours=2),
            end_at=timezone.now() + timedelta(hours=26),
        )
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )

        result = await self._contribute()

        self.assertEqual(result, 0)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 0)

    # ── Ceiling enforcement ──

    async def test_ceiling_caps_rewardable_quantity(self, mock_send):
        """Contributions beyond the ceiling are capped at remaining."""
        await self.asyncSetUp()
        event = await _make_active_event()
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            ceiling=10,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )

        await self._contribute(quantity=8)
        await obj.arefresh_from_db()
        self.assertEqual(obj.quantity_fulfilled, 8)

        # Second delivery: 5 units, but only 2 remaining
        await self._contribute(quantity=5)
        await obj.arefresh_from_db()
        self.assertEqual(obj.quantity_fulfilled, 10)

        contribs = [c async for c in SupplyChainContribution.objects.order_by("id")]
        self.assertEqual(len(contribs), 2)
        self.assertEqual(contribs[0].quantity, 8)
        self.assertEqual(contribs[1].quantity, 2)

    async def test_ceiling_fully_met_skips_delivery(self, mock_send):
        """When ceiling is fully met, further deliveries are silently ignored."""
        await self.asyncSetUp()
        event = await _make_active_event()
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            ceiling=5,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )

        await self._contribute(quantity=5)  # Fill exactly
        await obj.arefresh_from_db()
        self.assertEqual(obj.quantity_fulfilled, 5)

        # Next delivery — ceiling fully met, should NOT create contribution
        result = await self._contribute(quantity=10)
        self.assertEqual(result, 0)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 1)
        await obj.arefresh_from_db()
        self.assertEqual(obj.quantity_fulfilled, 5)  # Unchanged

    async def test_uncapped_objective(self, mock_send):
        """ceiling=None tracks unlimited contributions."""
        await self.asyncSetUp()
        event = await _make_active_event()
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            ceiling=None,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )

        await self._contribute(quantity=999)
        await obj.arefresh_from_db()
        self.assertEqual(obj.quantity_fulfilled, 999)

    # ── Multiple events / objectives ──

    async def test_multiple_concurrent_events(self, mock_send):
        """Delivery matching two simultaneous events creates a contribution for each."""
        await self.asyncSetUp()
        event1 = await _make_active_event(reward_per_item=100_000, name="Event A")
        event2 = await _make_active_event(reward_per_item=200_000, name="Event B")
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event1,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event2,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )

        await self._contribute(quantity=5)

        self.assertEqual(await SupplyChainContribution.objects.acount(), 2)

    async def test_delivery_matches_multiple_objectives_same_event(self, mock_send):
        """Delivery can match multiple objectives within the same event."""
        await self.asyncSetUp()
        event = await _make_active_event()
        # Obj1: specific cargo at specific dest
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        # Obj2: any cargo at same dest (no cargo filter)
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            destination_points=[self.dest],
        )

        await self._contribute(quantity=3)

        self.assertEqual(await SupplyChainContribution.objects.acount(), 2)

    async def test_no_immediate_payment(self, mock_send):
        """Contributions do NOT trigger any immediate payment."""
        await self.asyncSetUp()
        event = await _make_active_event(reward_per_item=100_000)
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            ceiling=10,
            is_primary=True,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )

        await self._contribute(quantity=5)

        mock_send.assert_not_called()


# ── Reward Distribution Tests ────────────────────────────────────────


@patch("amc.supply_chain.send_fund_to_player", new_callable=AsyncMock)
class DistributeRewardsTests(TestCase):
    """Tests for distribute_event_rewards — end-of-event pool distribution."""

    async def _setup(self):
        _, self.c1 = await _make_player_and_char("Alice")
        _, self.c2 = await _make_player_and_char("Bob")
        self.cargo = await Cargo.objects.acreate(key="C::Steel", label="Steel Coil")
        self.dest = await DeliveryPoint.objects.acreate(
            guid="d1", name="Factory", coord=Point(0, 0, 0)
        )

    async def test_proportional_distribution(self, mock_send):
        """Pool = reward_per_item × primary fulfilled, distributed proportionally."""
        await self._setup()
        event = await _make_ended_event(reward_per_item=10_000)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=10,
            ceiling=100,
            is_primary=True,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        # Simulate 100 units delivered total (70 Alice, 30 Bob)
        obj.quantity_fulfilled = 100
        await obj.asave(update_fields=["quantity_fulfilled"])

        await SupplyChainContribution.objects.acreate(
            objective=obj,
            character=self.c1,
            cargo_key="C::Steel",
            quantity=70,
            timestamp=timezone.now(),
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj,
            character=self.c2,
            cargo_key="C::Steel",
            quantity=30,
            timestamp=timezone.now(),
        )

        await distribute_event_rewards(event)

        await event.arefresh_from_db()
        self.assertTrue(event.rewards_distributed)
        # Pool = 10K × 100 = 1M. Alice: 70/100 × 1M = 700K, Bob: 30/100 × 1M = 300K
        rewards = {call[0][1].id: call[0][0] for call in mock_send.call_args_list}
        self.assertEqual(rewards[self.c1.id], 700_000)
        self.assertEqual(rewards[self.c2.id], 300_000)

    async def test_pool_capped_at_primary_ceiling(self, mock_send):
        """Pool is capped at ceiling even if quantity_fulfilled exceeds it."""
        await self._setup()
        event = await _make_ended_event(reward_per_item=100_000)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=10,
            ceiling=10,
            is_primary=True,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        # Simulate overshoot: 15 delivered but ceiling is 10
        obj.quantity_fulfilled = 15
        await obj.asave(update_fields=["quantity_fulfilled"])

        await SupplyChainContribution.objects.acreate(
            objective=obj,
            character=self.c1,
            cargo_key="C::Steel",
            quantity=15,
            timestamp=timezone.now(),
        )

        await distribute_event_rewards(event)

        # Pool = 100K × min(15, 10) = 100K × 10 = 1M
        rewards = {call[0][1].id: call[0][0] for call in mock_send.call_args_list}
        self.assertEqual(rewards[self.c1.id], 1_000_000)

    async def test_multi_objective_weighted_distribution(self, mock_send):
        """Reward pool split by objective weight, then by contribution within each."""
        await self._setup()
        cargo2 = await Cargo.objects.acreate(key="C::Coal", label="Coal")
        dest2 = await DeliveryPoint.objects.acreate(
            guid="d2", name="Mine", coord=Point(100, 100, 0)
        )
        event = await _make_ended_event(reward_per_item=10_000)
        # Primary objective: 60% weight
        obj1 = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=60,
            is_primary=True,
            ceiling=100,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        # Secondary objective: 40% weight
        obj2 = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=40,
            cargos=[cargo2],
            destination_points=[dest2],
        )
        # Primary delivered 10 items → pool = 10K × 10 = 100K
        obj1.quantity_fulfilled = 10
        await obj1.asave(update_fields=["quantity_fulfilled"])

        await SupplyChainContribution.objects.acreate(
            objective=obj1,
            character=self.c1,
            cargo_key="C::Steel",
            quantity=10,
            timestamp=timezone.now(),
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj2,
            character=self.c2,
            cargo_key="C::Coal",
            quantity=100,
            timestamp=timezone.now(),
        )

        await distribute_event_rewards(event)

        rewards = {call[0][1].id: call[0][0] for call in mock_send.call_args_list}
        # Pool = 100K. obj1 = 60K, obj2 = 40K
        self.assertEqual(rewards[self.c1.id], 60_000)
        self.assertEqual(rewards[self.c2.id], 40_000)

    async def test_no_primary_no_payout(self, mock_send):
        """No primary objective → pool is 0, no payouts."""
        await self._setup()
        event = await _make_ended_event(reward_per_item=100_000)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=10,
            is_primary=False,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj,
            character=self.c1,
            cargo_key="C::Steel",
            quantity=50,
            timestamp=timezone.now(),
        )

        await distribute_event_rewards(event)

        await event.arefresh_from_db()
        self.assertTrue(event.rewards_distributed)
        mock_send.assert_not_called()

    async def test_idempotent_distribution(self, mock_send):
        """Calling distribute twice does NOT double-pay."""
        await self._setup()
        event = await _make_ended_event(reward_per_item=10_000)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=10,
            is_primary=True,
            ceiling=100,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        obj.quantity_fulfilled = 5
        await obj.asave(update_fields=["quantity_fulfilled"])
        await SupplyChainContribution.objects.acreate(
            objective=obj,
            character=self.c1,
            cargo_key="C::Steel",
            quantity=5,
            timestamp=timezone.now(),
        )

        await distribute_event_rewards(event)
        await distribute_event_rewards(event)  # Second call — should no-op

        self.assertEqual(mock_send.call_count, 1)

    async def test_no_contributions_no_payout(self, mock_send):
        """Event with no contributions still marks as distributed but pays nobody."""
        await self._setup()
        event = await _make_ended_event(reward_per_item=10_000)
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=10,
            is_primary=True,
            ceiling=100,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        # quantity_fulfilled=0, so pool=0

        await distribute_event_rewards(event)

        await event.arefresh_from_db()
        self.assertTrue(event.rewards_distributed)
        mock_send.assert_not_called()

    async def test_same_player_multiple_objectives(self, mock_send):
        """Player contributing to multiple objectives gets separate payouts per objective."""
        await self._setup()
        cargo2 = await Cargo.objects.acreate(key="C::Coal", label="Coal")
        dest2 = await DeliveryPoint.objects.acreate(
            guid="d2", name="Mine", coord=Point(100, 100, 0)
        )
        event = await _make_ended_event(reward_per_item=10_000)
        obj1 = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=50,
            is_primary=True,
            ceiling=10,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        obj2 = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=50,
            cargos=[cargo2],
            destination_points=[dest2],
        )

        # Primary delivered 10 → pool = 10K × 10 = 100K
        obj1.quantity_fulfilled = 10
        await obj1.asave(update_fields=["quantity_fulfilled"])

        # Same player contributes to both objectives
        await SupplyChainContribution.objects.acreate(
            objective=obj1,
            character=self.c1,
            cargo_key="C::Steel",
            quantity=10,
            timestamp=timezone.now(),
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj2,
            character=self.c1,
            cargo_key="C::Coal",
            quantity=20,
            timestamp=timezone.now(),
        )

        await distribute_event_rewards(event)

        # Alice gets 50K from obj1 + 50K from obj2 = two payments
        self.assertEqual(mock_send.call_count, 2)
        total = sum(call[0][0] for call in mock_send.call_args_list)
        self.assertEqual(total, 100_000)

    @patch("amc.gov_employee.redirect_income_to_treasury", new_callable=AsyncMock)
    async def test_gov_employee_rewards_redirected(self, mock_redirect, mock_send):
        """Gov employee completion rewards go to treasury, not player wallet."""
        await self._setup()
        self.c1.gov_employee_until = timezone.now() + timedelta(days=30)
        await self.c1.asave(update_fields=["gov_employee_until"])

        event = await _make_ended_event(reward_per_item=10_000)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=10,
            is_primary=True,
            ceiling=100,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        obj.quantity_fulfilled = 10
        await obj.asave(update_fields=["quantity_fulfilled"])
        await SupplyChainContribution.objects.acreate(
            objective=obj,
            character=self.c1,
            cargo_key="C::Steel",
            quantity=10,
            timestamp=timezone.now(),
        )

        await distribute_event_rewards(event)

        mock_send.assert_not_called()  # NOT paid to player
        mock_redirect.assert_called_once()  # Redirected to treasury

    @patch("amc.supply_chain.announce", new_callable=AsyncMock)
    async def test_monitor_distributes_ended_events(self, mock_announce, mock_send):
        """Monitor cron picks up ended events and distributes their rewards."""
        await self._setup()
        event = await _make_ended_event(reward_per_item=10_000)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=10,
            is_primary=True,
            ceiling=100,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        obj.quantity_fulfilled = 5
        await obj.asave(update_fields=["quantity_fulfilled"])
        await SupplyChainContribution.objects.acreate(
            objective=obj,
            character=self.c1,
            cargo_key="C::Steel",
            quantity=5,
            timestamp=timezone.now(),
        )

        await monitor_supply_chain_events({"http_client": MagicMock()})

        await event.arefresh_from_db()
        self.assertTrue(event.rewards_distributed)
        mock_send.assert_called_once()

    @patch("amc.supply_chain.announce", new_callable=AsyncMock)
    async def test_monitor_skips_already_distributed(self, mock_announce, mock_send):
        """Monitor does NOT re-distribute already-distributed events."""
        await self._setup()
        event = await _make_ended_event(rewards_distributed=True)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=10,
            is_primary=True,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj,
            character=self.c1,
            cargo_key="C::Steel",
            quantity=10,
            timestamp=timezone.now(),
        )

        await monitor_supply_chain_events({"http_client": MagicMock()})

        mock_send.assert_not_called()

    @patch("amc.supply_chain.announce", new_callable=AsyncMock)
    async def test_monitor_skips_active_events(self, mock_announce, mock_send):
        """Monitor does NOT distribute events that are still active."""
        await self._setup()
        event = await _make_active_event(reward_per_item=10_000)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            reward_weight=10,
            is_primary=True,
            cargos=[self.cargo],
            destination_points=[self.dest],
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj,
            character=self.c1,
            cargo_key="C::Steel",
            quantity=10,
            timestamp=timezone.now(),
        )

        await monitor_supply_chain_events({"http_client": MagicMock()})

        mock_send.assert_not_called()


# ── Job Conflict Detection Tests ─────────────────────────────────────


class ConflictingCargoKeysTests(TestCase):
    """Tests for get_conflicting_cargo_keys — used by jobs.py suppression."""

    async def test_active_event_conflicts(self):
        """Active event objectives generate conflict entries."""
        cargo = await Cargo.objects.acreate(key="C::Coal", label="Coal")
        dest = await DeliveryPoint.objects.acreate(
            guid="d1", name="Factory", coord=Point(0, 0, 0)
        )
        event = await _make_active_event()
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            cargos=[cargo],
            destination_points=[dest],
        )

        conflicts = await get_conflicting_cargo_keys()
        self.assertIn(("C::Coal", dest.pk), conflicts)

    async def test_future_event_conflicts(self):
        """Future events also block job posting."""
        cargo = await Cargo.objects.acreate(key="C::Iron", label="Iron Ore")
        dest = await DeliveryPoint.objects.acreate(
            guid="d2", name="Smelter", coord=Point(50, 50, 0)
        )
        event = await sync_to_async(SupplyChainEventFactory)(
            start_at=timezone.now() + timedelta(hours=2),
            end_at=timezone.now() + timedelta(hours=26),
        )
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            cargos=[cargo],
            destination_points=[dest],
        )

        conflicts = await get_conflicting_cargo_keys()
        self.assertIn(("C::Iron", dest.pk), conflicts)

    async def test_expired_event_not_conflicting(self):
        """Expired + distributed events are NOT in the conflict set."""
        cargo = await Cargo.objects.acreate(key="C::Stone", label="Stone")
        dest = await DeliveryPoint.objects.acreate(
            guid="d3", name="Quarry", coord=Point(75, 75, 0)
        )
        event = await _make_ended_event(rewards_distributed=True)
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            cargos=[cargo],
            destination_points=[dest],
        )

        conflicts = await get_conflicting_cargo_keys()
        self.assertNotIn(("C::Stone", dest.pk), conflicts)

    async def test_wildcard_conflict_no_destination_filter(self):
        """Objective with no destination filter creates wildcard (-1) conflict."""
        cargo = await Cargo.objects.acreate(key="C::Fuel", label="Fuel")
        event = await _make_active_event()
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            cargos=[cargo],
            # No destination_points
        )

        conflicts = await get_conflicting_cargo_keys()
        self.assertIn(("C::Fuel", -1), conflicts)

    async def test_multiple_cargos_per_objective(self):
        """Each cargo in a multi-cargo objective generates a separate conflict pair."""
        cargo1 = await Cargo.objects.acreate(key="C::Coal", label="Coal")
        cargo2 = await Cargo.objects.acreate(key="C::Iron", label="Iron")
        dest = await DeliveryPoint.objects.acreate(
            guid="d4", name="Port", coord=Point(0, 0, 0)
        )
        event = await _make_active_event()
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event,
            cargos=[cargo1, cargo2],
            destination_points=[dest],
        )

        conflicts = await get_conflicting_cargo_keys()
        self.assertIn(("C::Coal", dest.pk), conflicts)
        self.assertIn(("C::Iron", dest.pk), conflicts)

    async def test_no_events_no_conflicts(self):
        """When no events exist, conflict set is empty."""
        conflicts = await get_conflicting_cargo_keys()
        self.assertEqual(len(conflicts), 0)


# ── Template Instantiation Tests ─────────────────────────────────────


class TemplateInstantiationTests(TestCase):
    """Tests for creating supply chain events from templates."""

    async def test_basic_template_creates_event(self):
        """Template instantiation creates a SupplyChainEvent with correct fields."""
        tmpl = await sync_to_async(SupplyChainEventTemplateFactory)(
            name="Steel Rush",
            description="Export steel",
            reward_per_item=10_000,
            duration_hours=48.0,
        )

        event = await create_event_from_template(tmpl)

        self.assertEqual(event.name, "Steel Rush")
        self.assertEqual(event.description, "Export steel")
        self.assertEqual(event.reward_per_item, 10_000)
        self.assertFalse(event.rewards_distributed)

        # Check duration (approximately 48 hours)
        duration = (event.end_at - event.start_at).total_seconds() / 3600
        self.assertAlmostEqual(duration, 48.0, delta=0.1)

    async def test_objectives_created_from_template(self):
        """Event objectives mirror the template objectives."""
        cargo = await Cargo.objects.acreate(key="C::Steel", label="Steel")
        tmpl = await sync_to_async(SupplyChainEventTemplateFactory)(name="Test")
        await sync_to_async(SupplyChainObjectiveTemplateFactory)(
            template=tmpl,
            cargos=[cargo],
            ceiling=200,
            reward_weight=60,
            is_primary=True,
        )

        event = await create_event_from_template(tmpl)

        objectives = [o async for o in event.objectives.all()]
        self.assertEqual(len(objectives), 1)

        obj = objectives[0]
        self.assertEqual(obj.ceiling, 200)
        self.assertEqual(obj.reward_weight, 60)
        self.assertTrue(obj.is_primary)
        self.assertEqual(obj.quantity_fulfilled, 0)

        cargo_keys = [c.key async for c in obj.cargos.all()]
        self.assertIn("C::Steel", cargo_keys)

    async def test_multiple_objectives(self):
        """Template with multiple objectives creates matching event objectives."""
        cargo1 = await Cargo.objects.acreate(key="C::Coal", label="Coal")
        cargo2 = await Cargo.objects.acreate(key="C::Iron", label="Iron Ore")
        tmpl = await sync_to_async(SupplyChainEventTemplateFactory)(name="Multi")
        await sync_to_async(SupplyChainObjectiveTemplateFactory)(
            template=tmpl,
            cargos=[cargo1],
            ceiling=100,
            reward_weight=40,
            is_primary=True,
        )
        await sync_to_async(SupplyChainObjectiveTemplateFactory)(
            template=tmpl,
            cargos=[cargo2],
            ceiling=300,
            reward_weight=60,
            is_primary=False,
        )

        event = await create_event_from_template(tmpl)

        objectives = [o async for o in event.objectives.all()]
        self.assertEqual(len(objectives), 2)

        primaries = [o for o in objectives if o.is_primary]
        self.assertEqual(len(primaries), 1)

    async def test_destination_points_copied(self):
        """DeliveryPoint M2M relations are copied from template to event objective."""
        dest = await DeliveryPoint.objects.acreate(
            guid="dp-test-1", name="Test Point", coord=Point(0, 0, 0)
        )
        tmpl = await sync_to_async(SupplyChainEventTemplateFactory)(name="Dest Test")
        await sync_to_async(SupplyChainObjectiveTemplateFactory)(
            template=tmpl,
            destination_points=[dest],
            is_primary=True,
        )

        event = await create_event_from_template(tmpl)
        obj = await event.objectives.afirst()
        dest_pks = [dp.pk async for dp in obj.destination_points.all()]
        self.assertIn(dest.pk, dest_pks)

    async def test_source_points_copied(self):
        """Source point M2M relations are copied from template to event objective."""
        src = await DeliveryPoint.objects.acreate(
            guid="dp-src-1", name="Source Point", coord=Point(0, 0, 0)
        )
        tmpl = await sync_to_async(SupplyChainEventTemplateFactory)(name="Src Test")
        await sync_to_async(SupplyChainObjectiveTemplateFactory)(
            template=tmpl,
            source_points=[src],
            is_primary=True,
        )

        event = await create_event_from_template(tmpl)
        obj = await event.objectives.afirst()
        src_pks = [dp.pk async for dp in obj.source_points.all()]
        self.assertIn(src.pk, src_pks)

    async def test_duration_override(self):
        """Duration override replaces template's default duration."""
        tmpl = await sync_to_async(SupplyChainEventTemplateFactory)(
            duration_hours=24.0,
        )

        event = await create_event_from_template(tmpl, duration_hours=12.0)

        duration = (event.end_at - event.start_at).total_seconds() / 3600
        self.assertAlmostEqual(duration, 12.0, delta=0.1)

    async def test_template_with_no_objectives(self):
        """Template with no objectives creates event with zero objectives."""
        tmpl = await sync_to_async(SupplyChainEventTemplateFactory)(name="Empty")

        event = await create_event_from_template(tmpl)

        count = await event.objectives.acount()
        self.assertEqual(count, 0)

    async def test_created_event_is_active(self):
        """Newly created event from template should be active."""
        tmpl = await sync_to_async(SupplyChainEventTemplateFactory)()

        event = await create_event_from_template(tmpl)

        self.assertTrue(event.is_active)
        active_events = SupplyChainEvent.objects.filter_active()
        self.assertTrue(await active_events.filter(pk=event.pk).aexists())


# ── End-to-End Lifecycle Tests ───────────────────────────────────────


@patch("amc.supply_chain.send_fund_to_player", new_callable=AsyncMock)
class EndToEndLifecycleTests(TestCase):
    """Full lifecycle: template → create event → deliver → distribute rewards."""

    async def _setup_steel_rush(self):
        """Set up a Steel Rush-style template with two objectives."""
        self.coal = await Cargo.objects.acreate(key="C::Coal", label="Coal")
        self.steel = await Cargo.objects.acreate(key="C::Steel", label="Steel Coil")
        self.mine = await DeliveryPoint.objects.acreate(
            guid="mine-1", name="Coal Mine", coord=Point(0, 0, 0)
        )
        self.mill = await DeliveryPoint.objects.acreate(
            guid="mill-1", name="Steel Mill", coord=Point(100, 100, 0)
        )
        self.harbor = await DeliveryPoint.objects.acreate(
            guid="harbor-1", name="Harbor", coord=Point(200, 200, 0)
        )

        self.tmpl = await sync_to_async(SupplyChainEventTemplateFactory)(
            name="Steel Rush",
            reward_per_item=10_000,
            duration_hours=48.0,
        )
        # Primary: Steel Coil → Harbor (60% weight)
        await sync_to_async(SupplyChainObjectiveTemplateFactory)(
            template=self.tmpl,
            cargos=[self.steel],
            destination_points=[self.harbor],
            ceiling=100,
            reward_weight=60,
            is_primary=True,
        )
        # Secondary: Coal → Steel Mill (40% weight)
        await sync_to_async(SupplyChainObjectiveTemplateFactory)(
            template=self.tmpl,
            cargos=[self.coal],
            destination_points=[self.mill],
            ceiling=500,
            reward_weight=40,
            is_primary=False,
        )

    async def test_template_to_contribution_lifecycle(self, mock_send):
        """Event from template correctly matches deliveries through check_and_record_contribution."""
        await self._setup_steel_rush()
        _, char = await _make_player_and_char("Trucker")

        # Instantiate event from template
        event = await create_event_from_template(self.tmpl)
        self.assertEqual(await event.objectives.acount(), 2)

        # Deliver steel coils to harbor — should match primary objective
        recorded = await check_and_record_contribution(
            delivery=None,
            character=char,
            cargo_key="C::Steel",
            quantity=10,
            destination_point=self.harbor,
            source_point=self.mill,
        )
        self.assertEqual(recorded, 10)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 1)

        # Deliver coal to steel mill — should match secondary objective
        recorded = await check_and_record_contribution(
            delivery=None,
            character=char,
            cargo_key="C::Coal",
            quantity=20,
            destination_point=self.mill,
            source_point=self.mine,
        )
        self.assertEqual(recorded, 20)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 2)

    async def test_wrong_destination_no_match_from_template(self, mock_send):
        """Delivery to wrong destination doesn't match template-created objectives."""
        await self._setup_steel_rush()
        _, char = await _make_player_and_char("Trucker")
        _event = await create_event_from_template(self.tmpl)

        # Deliver steel coil to the MILL (not the harbor) — shouldn't match primary
        recorded = await check_and_record_contribution(
            delivery=None,
            character=char,
            cargo_key="C::Steel",
            quantity=10,
            destination_point=self.mill,
            source_point=self.mine,
        )
        self.assertEqual(recorded, 0)

    async def test_full_lifecycle_template_to_payout(self, mock_send):
        """Full lifecycle: template → event → deliveries → end → distribute rewards."""
        await self._setup_steel_rush()
        _, alice = await _make_player_and_char("Alice")
        _, bob = await _make_player_and_char("Bob")

        # Instantiate event
        event = await create_event_from_template(self.tmpl)

        # Alice delivers 60 steel coils to harbor (primary)
        await check_and_record_contribution(
            delivery=None,
            character=alice,
            cargo_key="C::Steel",
            quantity=60,
            destination_point=self.harbor,
            source_point=self.mill,
        )
        # Bob delivers 40 steel coils to harbor (primary)
        await check_and_record_contribution(
            delivery=None,
            character=bob,
            cargo_key="C::Steel",
            quantity=40,
            destination_point=self.harbor,
            source_point=self.mill,
        )
        # Alice delivers 100 coal to mill (secondary)
        await check_and_record_contribution(
            delivery=None,
            character=alice,
            cargo_key="C::Coal",
            quantity=100,
            destination_point=self.mill,
            source_point=self.mine,
        )

        # Fast-forward event to ended
        event.start_at = timezone.now() - timedelta(hours=49)
        event.end_at = timezone.now() - timedelta(hours=1)
        await event.asave(update_fields=["start_at", "end_at"])

        # Distribute
        await distribute_event_rewards(event)

        await event.arefresh_from_db()
        self.assertTrue(event.rewards_distributed)

        # Primary pool = 10K × 100 (60+40 capped at ceiling=100) = 1M
        # Total weight = 60 + 40 = 100
        # Obj1 (primary, 60%): 600K → Alice 60/100 = 360K, Bob 40/100 = 240K
        # Obj2 (secondary, 40%): 400K → Alice gets all = 400K
        rewards = {}
        for call in mock_send.call_args_list:
            amount, char_obj = call[0][0], call[0][1]
            rewards[char_obj.name] = rewards.get(char_obj.name, 0) + amount

        self.assertEqual(rewards["Alice"], 360_000 + 400_000)  # 760K
        self.assertEqual(rewards["Bob"], 240_000)

    async def test_ceiling_enforcement_through_lifecycle(self, mock_send):
        """Primary ceiling caps contributions even through the full lifecycle."""
        await self._setup_steel_rush()
        _, trucker = await _make_player_and_char("Trucker")

        event = await create_event_from_template(self.tmpl)

        # Primary ceiling is 100 — deliver 120 steel coils
        await check_and_record_contribution(
            delivery=None,
            character=trucker,
            cargo_key="C::Steel",
            quantity=80,
            destination_point=self.harbor,
            source_point=self.mill,
        )
        await check_and_record_contribution(
            delivery=None,
            character=trucker,
            cargo_key="C::Steel",
            quantity=40,
            destination_point=self.harbor,
            source_point=self.mill,
        )

        # Only 100 should be recorded (80 + 20, not 80 + 40)
        total_contributed = 0
        async for c in SupplyChainContribution.objects.filter(
            objective__event=event, objective__is_primary=True
        ):
            total_contributed += c.quantity
        self.assertEqual(total_contributed, 100)

        # Verify objective quantity_fulfilled
        primary = await event.objectives.filter(is_primary=True).afirst()
        await primary.arefresh_from_db()
        self.assertEqual(primary.quantity_fulfilled, 100)

    async def test_concurrent_events_from_templates(self, mock_send):
        """Two template-created events can run concurrently without interference."""
        await self._setup_steel_rush()
        _, char = await _make_player_and_char("Trucker")

        # Create two events from the same template
        _event1 = await create_event_from_template(self.tmpl)
        _event2 = await create_event_from_template(self.tmpl)

        # Deliver steel coils — should match BOTH events' primary objectives
        recorded = await check_and_record_contribution(
            delivery=None,
            character=char,
            cargo_key="C::Steel",
            quantity=10,
            destination_point=self.harbor,
            source_point=self.mill,
        )

        # Should create 2 contributions (one per event)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 2)
        self.assertGreater(recorded, 0)
