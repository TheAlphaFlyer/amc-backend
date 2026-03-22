"""Tests for source storage reservation logic in monitor_jobs.

When multiple jobs are posted in a single tick, they must not double-count
the same source storage. The reserved_source dict tracks how much each
(cargo_id, delivery_point_id) pair has been claimed.
"""

from decimal import Decimal
from unittest.mock import patch, AsyncMock

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.factories import DeliveryJobTemplateFactory, DeliveryPointFactory
from amc.models import (
    Cargo,
    DeliveryJob,
    DeliveryPointStorage,
    JobPostingConfig,
)
from amc.jobs import monitor_jobs


def _make_template(cargo, source, destination, quantity, name=None):
    """Helper: create a DeliveryJobTemplate wired to the given cargo/points."""
    return DeliveryJobTemplateFactory(
        name=name or f"Job for {cargo.key}",
        default_quantity=quantity,
        bonus_multiplier=1.0,
        completion_bonus=10_000,
        duration_hours=5,
        job_posting_probability=1.0,
        success_score=1.0,
        cargos=[cargo],
        source_points=[source],
        destination_points=[destination],
    )


# Patch targets for external calls in monitor_jobs
_PATCHES = {
    "get_players": "amc.jobs.get_players",
    "announce": "amc.jobs.announce",
    "treasury": "amc.jobs.get_treasury_fund_balance",
    "escrow": "amc.jobs.escrow_ministry_funds",
    "sc_conflicts": "amc.supply_chain.get_conflicting_cargo_keys",
}


class SourceReservationTestCase(TestCase):
    """Verify that multiple jobs in a single tick don't deplete the same source."""

    def setUp(self):
        # Shared delivery points
        self.source = DeliveryPointFactory(name="Source Farm")
        self.dest_a = DeliveryPointFactory(name="Destination A")
        self.dest_b = DeliveryPointFactory(name="Destination B")

        # Cargo
        self.wheat = Cargo.objects.create(key="C::Wheat", label="Wheat")

        # Two templates sharing the SAME source, different destinations
        self.template_a = _make_template(
            self.wheat, self.source, self.dest_a, quantity=50, name="Wheat to A"
        )
        self.template_b = _make_template(
            self.wheat, self.source, self.dest_b, quantity=50, name="Wheat to B"
        )

        # Source has 60 units — enough for one job (50) but NOT two (100)
        DeliveryPointStorage.objects.create(
            delivery_point=self.source,
            cargo=self.wheat,
            cargo_key="C::Wheat",
            kind=DeliveryPointStorage.Kind.OUTPUT,
            amount=60,
            capacity=100,
        )

        # Both destinations are empty (capacity > 0, amount = 0)
        DeliveryPointStorage.objects.create(
            delivery_point=self.dest_a,
            cargo=self.wheat,
            cargo_key="C::Wheat",
            kind=DeliveryPointStorage.Kind.INPUT,
            amount=0,
            capacity=100,
        )
        DeliveryPointStorage.objects.create(
            delivery_point=self.dest_b,
            cargo=self.wheat,
            cargo_key="C::Wheat",
            kind=DeliveryPointStorage.Kind.INPUT,
            amount=0,
            capacity=100,
        )

        # Allow plenty of job slots
        JobPostingConfig.objects.update_or_create(
            pk=1,
            defaults={
                "min_base_jobs": 10,
                "max_posts_per_tick": 10,
                "target_success_rate": 0.5,
                "min_multiplier": 1.0,
                "max_multiplier": 2.0,
                "treasury_equilibrium": 50_000_000,
                "treasury_sensitivity": 0.5,
            },
        )

    @patch(_PATCHES["sc_conflicts"], new_callable=AsyncMock, return_value=set())
    @patch(_PATCHES["escrow"], new_callable=AsyncMock, return_value=True)
    @patch(_PATCHES["treasury"], new_callable=AsyncMock, return_value=Decimal("50000000"))
    @patch(_PATCHES["announce"], new_callable=AsyncMock)
    @patch(_PATCHES["get_players"], new_callable=AsyncMock, return_value=[(1, {"name": "Player1"})] * 5)
    async def test_second_job_skipped_when_source_insufficient(
        self, mock_players, mock_announce, mock_treasury, mock_escrow, mock_conflicts
    ):
        """Only 1 job should be posted when source can't supply both."""
        ctx = {"http_client": AsyncMock()}
        await monitor_jobs(ctx)

        job_count = await DeliveryJob.objects.filter(
            fulfilled_at__isnull=True,
            expired_at__gte=timezone.now(),
        ).acount()
        self.assertEqual(job_count, 1, "Should post only 1 job when source has 60 but both want 50")

    @patch(_PATCHES["sc_conflicts"], new_callable=AsyncMock, return_value=set())
    @patch(_PATCHES["escrow"], new_callable=AsyncMock, return_value=True)
    @patch(_PATCHES["treasury"], new_callable=AsyncMock, return_value=Decimal("50000000"))
    @patch(_PATCHES["announce"], new_callable=AsyncMock)
    @patch(_PATCHES["get_players"], new_callable=AsyncMock, return_value=[(1, {"name": "Player1"})] * 5)
    async def test_both_jobs_posted_when_source_sufficient(
        self, mock_players, mock_announce, mock_treasury, mock_escrow, mock_conflicts
    ):
        """Both jobs should be posted when source can supply both."""
        # Increase source to 120 — enough for two 50-qty jobs
        await DeliveryPointStorage.objects.filter(
            delivery_point=self.source,
            cargo=self.wheat,
        ).aupdate(amount=120)

        ctx = {"http_client": AsyncMock()}
        await monitor_jobs(ctx)

        job_count = await DeliveryJob.objects.filter(
            fulfilled_at__isnull=True,
            expired_at__gte=timezone.now(),
        ).acount()
        self.assertEqual(job_count, 2, "Should post both jobs when source has 120 and each needs 50")

    @patch(_PATCHES["sc_conflicts"], new_callable=AsyncMock, return_value=set())
    @patch(_PATCHES["escrow"], new_callable=AsyncMock, return_value=True)
    @patch(_PATCHES["treasury"], new_callable=AsyncMock, return_value=Decimal("50000000"))
    @patch(_PATCHES["announce"], new_callable=AsyncMock)
    @patch(_PATCHES["get_players"], new_callable=AsyncMock, return_value=[(1, {"name": "Player1"})] * 5)
    async def test_exactly_enough_source_for_both(
        self, mock_players, mock_announce, mock_treasury, mock_escrow, mock_conflicts
    ):
        """Edge case: source has exactly 100, each job wants 50 → both should post."""
        await DeliveryPointStorage.objects.filter(
            delivery_point=self.source,
            cargo=self.wheat,
        ).aupdate(amount=100)

        ctx = {"http_client": AsyncMock()}
        await monitor_jobs(ctx)

        job_count = await DeliveryJob.objects.filter(
            fulfilled_at__isnull=True,
            expired_at__gte=timezone.now(),
        ).acount()
        self.assertEqual(job_count, 2, "Should post both when source exactly matches combined demand")

    @patch(_PATCHES["sc_conflicts"], new_callable=AsyncMock, return_value=set())
    @patch(_PATCHES["escrow"], new_callable=AsyncMock, return_value=True)
    @patch(_PATCHES["treasury"], new_callable=AsyncMock, return_value=Decimal("50000000"))
    @patch(_PATCHES["announce"], new_callable=AsyncMock)
    @patch(_PATCHES["get_players"], new_callable=AsyncMock, return_value=[(1, {"name": "Player1"})] * 5)
    async def test_independent_sources_not_affected(
        self, mock_players, mock_announce, mock_treasury, mock_escrow, mock_conflicts
    ):
        """Jobs with different sources should not affect each other's reservation."""
        # Give template_b its own separate source with plenty of stock
        source_b = await sync_to_async(DeliveryPointFactory)(name="Source B")
        await self.template_b.source_points.aclear()
        await self.template_b.source_points.aadd(source_b)
        await DeliveryPointStorage.objects.acreate(
            delivery_point=source_b,
            cargo=self.wheat,
            cargo_key="C::Wheat",
            kind=DeliveryPointStorage.Kind.OUTPUT,
            amount=60,
            capacity=100,
        )

        ctx = {"http_client": AsyncMock()}
        await monitor_jobs(ctx)

        job_count = await DeliveryJob.objects.filter(
            fulfilled_at__isnull=True,
            expired_at__gte=timezone.now(),
        ).acount()
        self.assertEqual(job_count, 2, "Jobs with independent sources should both post")
