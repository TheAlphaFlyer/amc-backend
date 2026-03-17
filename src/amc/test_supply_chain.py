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
)
from amc.models import (
    Cargo,
    DeliveryPoint,
    SupplyChainContribution,
)
from amc.supply_chain import (
    check_and_record_contribution,
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
            event=event, ceiling=100,
            cargos=[self.cargo], destination_points=[self.dest],
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
            event=event, cargos=[self.cargo], destination_points=[self.dest],
        )

        bonus = await self._contribute(cargo_key="C::Iron")

        self.assertEqual(bonus, 0)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 0)

    async def test_wrong_destination_no_match(self, mock_send):
        """Delivering to a different destination doesn't match."""
        await self.asyncSetUp()
        other_dest = await DeliveryPoint.objects.acreate(
            guid="other", name="Other", coord=Point(200, 200, 0)
        )
        event = await _make_active_event()
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, cargos=[self.cargo], destination_points=[self.dest],
        )

        bonus = await self._contribute(dest=other_dest)

        self.assertEqual(bonus, 0)
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
        bonus = await self._contribute(src=wrong_source)
        self.assertEqual(bonus, 0)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 0)

        # Deliver from correct source → match
        bonus = await self._contribute(src=self.source)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 1)

    async def test_no_cargo_filter_matches_any(self, mock_send):
        """Objective with empty cargos M2M matches any cargo key."""
        await self.asyncSetUp()
        event = await _make_active_event()
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, destination_points=[self.dest],
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
            event=event, cargos=[self.cargo],
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
            event=event, cargos=[self.cargo], destination_points=[self.dest],
        )

        bonus = await self._contribute()

        self.assertEqual(bonus, 0)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 0)

    async def test_future_event_no_match(self, mock_send):
        """Events that haven't started yet don't match deliveries."""
        await self.asyncSetUp()
        event = await _make_active_event(
            start_at=timezone.now() + timedelta(hours=2),
            end_at=timezone.now() + timedelta(hours=26),
        )
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, cargos=[self.cargo], destination_points=[self.dest],
        )

        bonus = await self._contribute()

        self.assertEqual(bonus, 0)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 0)

    # ── Ceiling enforcement ──

    async def test_ceiling_caps_rewardable_quantity(self, mock_send):
        """Contributions beyond the ceiling are capped at remaining."""
        await self.asyncSetUp()
        event = await _make_active_event()
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, ceiling=10,
            cargos=[self.cargo], destination_points=[self.dest],
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
            event=event, ceiling=5,
            cargos=[self.cargo], destination_points=[self.dest],
        )

        await self._contribute(quantity=5)  # Fill exactly
        await obj.arefresh_from_db()
        self.assertEqual(obj.quantity_fulfilled, 5)

        # Next delivery — ceiling fully met, should NOT create contribution
        bonus = await self._contribute(quantity=10)
        self.assertEqual(bonus, 0)
        self.assertEqual(await SupplyChainContribution.objects.acount(), 1)
        await obj.arefresh_from_db()
        self.assertEqual(obj.quantity_fulfilled, 5)  # Unchanged

    async def test_uncapped_objective(self, mock_send):
        """ceiling=None tracks unlimited contributions."""
        await self.asyncSetUp()
        event = await _make_active_event()
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, ceiling=None,
            cargos=[self.cargo], destination_points=[self.dest],
        )

        await self._contribute(quantity=999)
        await obj.arefresh_from_db()
        self.assertEqual(obj.quantity_fulfilled, 999)

    # ── Multiple events / objectives ──

    async def test_multiple_concurrent_events(self, mock_send):
        """Delivery matching two simultaneous events creates a contribution for each."""
        await self.asyncSetUp()
        event1 = await _make_active_event(total_prize=100_000, name="Event A")
        event2 = await _make_active_event(total_prize=200_000, name="Event B")
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event1, cargos=[self.cargo], destination_points=[self.dest],
        )
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event2, cargos=[self.cargo], destination_points=[self.dest],
        )

        await self._contribute(quantity=5)

        self.assertEqual(await SupplyChainContribution.objects.acount(), 2)

    async def test_delivery_matches_multiple_objectives_same_event(self, mock_send):
        """Delivery can match multiple objectives within the same event."""
        await self.asyncSetUp()
        event = await _make_active_event()
        # Obj1: specific cargo at specific dest
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, cargos=[self.cargo], destination_points=[self.dest],
        )
        # Obj2: any cargo at same dest (no cargo filter)
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, destination_points=[self.dest],
        )

        await self._contribute(quantity=3)

        self.assertEqual(await SupplyChainContribution.objects.acount(), 2)


# ── Per-Delivery Bonus Tests ─────────────────────────────────────────


@patch("amc.supply_chain.send_fund_to_player", new_callable=AsyncMock)
class PerDeliveryBonusTests(TestCase):
    """Tests for per-delivery bonus calculation and payment."""

    async def asyncSetUp(self):
        _, self.character = await _make_player_and_char()
        self.source = await DeliveryPoint.objects.acreate(
            guid="src1", name="Mine", coord=Point(0, 0, 0)
        )
        self.dest = await DeliveryPoint.objects.acreate(
            guid="dst1", name="Factory", coord=Point(100, 100, 0)
        )
        self.cargo = await Cargo.objects.acreate(key="C::Coal", label="Coal")

    async def test_bonus_calculation(self, mock_send):
        """Per-delivery bonus = quantity * (pool/ceiling) * multiplier."""
        await self.asyncSetUp()
        event = await _make_active_event(total_prize=1_000_000, per_delivery_bonus_pct=0.20)
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, ceiling=100, reward_weight=10,
            per_delivery_bonus_multiplier=1.0,
            cargos=[self.cargo], destination_points=[self.dest],
        )

        bonus = await check_and_record_contribution(
            delivery=None, character=self.character,
            cargo_key="C::Coal", quantity=10,
            destination_point=self.dest, source_point=self.source,
        )

        # Pool = 1M * 0.20 * (10/10) = 200K. Per-unit = 200K/100 = 2K. 10*2K = 20K
        self.assertEqual(bonus, 20_000)
        mock_send.assert_called_once_with(
            20_000, self.character, f"Supply Chain Event: {event.name}"
        )

    async def test_zero_multiplier_no_bonus(self, mock_send):
        """multiplier=0 means no bonus, no payment."""
        await self.asyncSetUp()
        event = await _make_active_event(total_prize=1_000_000, per_delivery_bonus_pct=0.20)
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, ceiling=100, per_delivery_bonus_multiplier=0.0,
            cargos=[self.cargo], destination_points=[self.dest],
        )

        bonus = await check_and_record_contribution(
            delivery=None, character=self.character,
            cargo_key="C::Coal", quantity=10,
            destination_point=self.dest, source_point=self.source,
        )

        self.assertEqual(bonus, 0)
        mock_send.assert_not_called()

    async def test_bonus_with_partial_weight(self, mock_send):
        """Bonus pool is proportional to reward_weight across objectives."""
        await self.asyncSetUp()
        cargo2 = await Cargo.objects.acreate(key="C::Iron", label="Iron")
        event = await _make_active_event(total_prize=1_000_000, per_delivery_bonus_pct=0.20)
        # This objective gets 40% of the bonus pool
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, ceiling=100, reward_weight=40,
            per_delivery_bonus_multiplier=1.0,
            cargos=[self.cargo], destination_points=[self.dest],
        )
        # Other objective gets 60%
        dest2 = await DeliveryPoint.objects.acreate(
            guid="dst2", name="Other", coord=Point(200, 200, 0)
        )
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, ceiling=100, reward_weight=60,
            cargos=[cargo2], destination_points=[dest2],
        )

        bonus = await check_and_record_contribution(
            delivery=None, character=self.character,
            cargo_key="C::Coal", quantity=10,
            destination_point=self.dest, source_point=self.source,
        )

        # Pool = 1M * 0.20 * (40/100) = 80K. Per-unit = 80K/100 = 800. 10*800 = 8000
        self.assertEqual(bonus, 8_000)

    @patch("amc.gov_employee.redirect_income_to_treasury", new_callable=AsyncMock)
    async def test_gov_employee_bonus_redirected(self, mock_redirect, mock_send):
        """Gov employee per-delivery bonus is redirected to treasury."""
        await self.asyncSetUp()
        self.character.gov_employee_until = timezone.now() + timedelta(days=30)
        await self.character.asave(update_fields=["gov_employee_until"])

        event = await _make_active_event(total_prize=1_000_000, per_delivery_bonus_pct=0.20)
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, ceiling=100, reward_weight=10,
            per_delivery_bonus_multiplier=1.0,
            cargos=[self.cargo], destination_points=[self.dest],
        )

        bonus = await check_and_record_contribution(
            delivery=None, character=self.character,
            cargo_key="C::Coal", quantity=10,
            destination_point=self.dest, source_point=self.source,
        )

        self.assertEqual(bonus, 20_000)
        mock_send.assert_not_called()  # NOT sent directly to player


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
        """Completion pool distributed proportionally to contribution quantities."""
        await self._setup()
        event = await _make_ended_event(total_prize=100_000, per_delivery_bonus_pct=0.20)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, reward_weight=10, ceiling=100,
            cargos=[self.cargo], destination_points=[self.dest],
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj, character=self.c1,
            cargo_key="C::Steel", quantity=70, timestamp=timezone.now(),
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj, character=self.c2,
            cargo_key="C::Steel", quantity=30, timestamp=timezone.now(),
        )

        await distribute_event_rewards(event)

        await event.arefresh_from_db()
        self.assertTrue(event.rewards_distributed)
        # Pool = 100K * 0.80 = 80K. Alice: 70/100*80K = 56K, Bob: 30/100*80K = 24K
        rewards = {call[0][1].id: call[0][0] for call in mock_send.call_args_list}
        self.assertEqual(rewards[self.c1.id], 56_000)
        self.assertEqual(rewards[self.c2.id], 24_000)

    async def test_multi_objective_weighted_distribution(self, mock_send):
        """Reward pool split by objective weight, then by contribution within each."""
        await self._setup()
        cargo2 = await Cargo.objects.acreate(key="C::Coal", label="Coal")
        dest2 = await DeliveryPoint.objects.acreate(
            guid="d2", name="Mine", coord=Point(100, 100, 0)
        )
        event = await _make_ended_event(total_prize=100_000, per_delivery_bonus_pct=0.0)
        obj1 = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, reward_weight=60,
            cargos=[self.cargo], destination_points=[self.dest],
        )
        obj2 = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, reward_weight=40,
            cargos=[cargo2], destination_points=[dest2],
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj1, character=self.c1,
            cargo_key="C::Steel", quantity=50, timestamp=timezone.now(),
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj2, character=self.c2,
            cargo_key="C::Coal", quantity=100, timestamp=timezone.now(),
        )

        await distribute_event_rewards(event)

        rewards = {call[0][1].id: call[0][0] for call in mock_send.call_args_list}
        self.assertEqual(rewards[self.c1.id], 60_000)
        self.assertEqual(rewards[self.c2.id], 40_000)

    async def test_idempotent_distribution(self, mock_send):
        """Calling distribute twice does NOT double-pay."""
        await self._setup()
        event = await _make_ended_event(total_prize=50_000, per_delivery_bonus_pct=0.0)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, reward_weight=10,
            cargos=[self.cargo], destination_points=[self.dest],
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj, character=self.c1,
            cargo_key="C::Steel", quantity=10, timestamp=timezone.now(),
        )

        await distribute_event_rewards(event)
        await distribute_event_rewards(event)  # Second call — should no-op

        self.assertEqual(mock_send.call_count, 1)

    async def test_no_contributions_no_payout(self, mock_send):
        """Event with no contributions still marks as distributed but pays nobody."""
        await self._setup()
        event = await _make_ended_event(total_prize=50_000, per_delivery_bonus_pct=0.0)
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, reward_weight=10,
            cargos=[self.cargo], destination_points=[self.dest],
        )

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
        event = await _make_ended_event(total_prize=100_000, per_delivery_bonus_pct=0.0)
        obj1 = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, reward_weight=50,
            cargos=[self.cargo], destination_points=[self.dest],
        )
        obj2 = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, reward_weight=50,
            cargos=[cargo2], destination_points=[dest2],
        )

        # Same player contributes to both objectives
        await SupplyChainContribution.objects.acreate(
            objective=obj1, character=self.c1,
            cargo_key="C::Steel", quantity=10, timestamp=timezone.now(),
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj2, character=self.c1,
            cargo_key="C::Coal", quantity=20, timestamp=timezone.now(),
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

        event = await _make_ended_event(total_prize=100_000, per_delivery_bonus_pct=0.0)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, reward_weight=10,
            cargos=[self.cargo], destination_points=[self.dest],
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj, character=self.c1,
            cargo_key="C::Steel", quantity=50, timestamp=timezone.now(),
        )

        await distribute_event_rewards(event)

        mock_send.assert_not_called()  # NOT paid to player
        mock_redirect.assert_called_once()  # Redirected to treasury

    @patch("amc.supply_chain.announce", new_callable=AsyncMock)
    async def test_monitor_distributes_ended_events(self, mock_announce, mock_send):
        """Monitor cron picks up ended events and distributes their rewards."""
        await self._setup()
        event = await _make_ended_event(total_prize=50_000, per_delivery_bonus_pct=0.0)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, reward_weight=10,
            cargos=[self.cargo], destination_points=[self.dest],
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj, character=self.c1,
            cargo_key="C::Steel", quantity=10, timestamp=timezone.now(),
        )

        await monitor_supply_chain_events({"http_client": MagicMock()})

        await event.arefresh_from_db()
        self.assertTrue(event.rewards_distributed)
        mock_send.assert_called_once()

    @patch("amc.supply_chain.announce", new_callable=AsyncMock)
    async def test_monitor_skips_already_distributed(self, mock_announce, mock_send):
        """Monitor does NOT re-distribute already-distributed events."""
        await self._setup()
        event = await _make_ended_event(
            total_prize=50_000, per_delivery_bonus_pct=0.0,
            rewards_distributed=True,
        )
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, reward_weight=10,
            cargos=[self.cargo], destination_points=[self.dest],
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj, character=self.c1,
            cargo_key="C::Steel", quantity=10, timestamp=timezone.now(),
        )

        await monitor_supply_chain_events({"http_client": MagicMock()})

        mock_send.assert_not_called()

    @patch("amc.supply_chain.announce", new_callable=AsyncMock)
    async def test_monitor_skips_active_events(self, mock_announce, mock_send):
        """Monitor does NOT distribute events that are still active."""
        await self._setup()
        event = await _make_active_event(total_prize=50_000, per_delivery_bonus_pct=0.0)
        obj = await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, reward_weight=10,
            cargos=[self.cargo], destination_points=[self.dest],
        )
        await SupplyChainContribution.objects.acreate(
            objective=obj, character=self.c1,
            cargo_key="C::Steel", quantity=10, timestamp=timezone.now(),
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
            event=event, cargos=[cargo], destination_points=[dest],
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
            event=event, cargos=[cargo], destination_points=[dest],
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
            event=event, cargos=[cargo], destination_points=[dest],
        )

        conflicts = await get_conflicting_cargo_keys()
        self.assertNotIn(("C::Stone", dest.pk), conflicts)

    async def test_wildcard_conflict_no_destination_filter(self):
        """Objective with no destination filter creates wildcard (-1) conflict."""
        cargo = await Cargo.objects.acreate(key="C::Fuel", label="Fuel")
        event = await _make_active_event()
        await sync_to_async(SupplyChainObjectiveFactory)(
            event=event, cargos=[cargo],
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
            event=event, cargos=[cargo1, cargo2], destination_points=[dest],
        )

        conflicts = await get_conflicting_cargo_keys()
        self.assertIn(("C::Coal", dest.pk), conflicts)
        self.assertIn(("C::Iron", dest.pk), conflicts)

    async def test_no_events_no_conflicts(self):
        """When no events exist, conflict set is empty."""
        conflicts = await get_conflicting_cargo_keys()
        self.assertEqual(len(conflicts), 0)
