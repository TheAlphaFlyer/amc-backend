from datetime import timedelta
from django.test import TestCase  # pyrefly: ignore
from django.utils import timezone  # pyrefly: ignore
from django.db.models import F  # pyrefly: ignore
from django.db.models.functions import Least, Greatest  # pyrefly: ignore
from asgiref.sync import sync_to_async  # pyrefly: ignore
from amc.factories import DeliveryJobFactory, DeliveryJobTemplateFactory
from amc.jobs import _decay_template_score
from amc.models import DeliveryJobTemplate


class TemplateSuccessScoreDecayTestCase(TestCase):
    """Tests for success_score decay on job expiry."""

    async def test_success_score_decreases_on_expiry(self):
        """Expiring a job should decay the template's success_score by 30%."""
        template = await sync_to_async(DeliveryJobTemplateFactory)(success_score=1.0)
        job = await sync_to_async(DeliveryJobFactory)(
            created_from=template,
            fulfilled_at=None,
            expired_at=timezone.now() - timedelta(hours=1),
        )

        await _decay_template_score(job)

        await template.arefresh_from_db()
        self.assertAlmostEqual(template.success_score, 0.70, places=2)
        self.assertEqual(template.lifetime_expirations, 1)

    async def test_success_score_floored_at_min(self):
        """Score should not drop below 0.1."""
        template = await sync_to_async(DeliveryJobTemplateFactory)(success_score=0.12)
        job = await sync_to_async(DeliveryJobFactory)(
            created_from=template,
            fulfilled_at=None,
            expired_at=timezone.now() - timedelta(hours=1),
        )

        await _decay_template_score(job)

        await template.arefresh_from_db()
        self.assertAlmostEqual(template.success_score, 0.1, places=2)

    async def test_no_error_without_template(self):
        """Jobs without created_from should not error."""
        job = await sync_to_async(DeliveryJobFactory)(
            created_from=None,
            fulfilled_at=None,
            expired_at=timezone.now() - timedelta(hours=1),
        )
        # Should not raise
        await _decay_template_score(job)


class TemplateSuccessScoreBoostTestCase(TestCase):
    """Tests for success_score boost on job completion."""

    async def test_success_score_increases_on_completion(self):
        """Completing a job should boost the template's success_score by 15%."""
        template = await sync_to_async(DeliveryJobTemplateFactory)(success_score=1.0)

        # Simulate what on_delivery_job_fulfilled does
        await DeliveryJobTemplate.objects.filter(pk=template.pk).aupdate(
            success_score=Least(2.0, F("success_score") * 1.15),
            lifetime_completions=F("lifetime_completions") + 1,
        )

        await template.arefresh_from_db()
        self.assertAlmostEqual(template.success_score, 1.15, places=2)
        self.assertEqual(template.lifetime_completions, 1)

    async def test_success_score_capped_at_max(self):
        """Score should not exceed 2.0."""
        template = await sync_to_async(DeliveryJobTemplateFactory)(success_score=1.9)

        await DeliveryJobTemplate.objects.filter(pk=template.pk).aupdate(
            success_score=Least(2.0, F("success_score") * 1.15),
            lifetime_completions=F("lifetime_completions") + 1,
        )

        await template.arefresh_from_db()
        # 1.9 * 1.15 = 2.185 → capped at 2.0
        self.assertAlmostEqual(template.success_score, 2.0, places=2)


class TemplateSuccessScoreEquilibriumTestCase(TestCase):
    """Tests for the equilibrium behavior over multiple events."""

    async def test_recovery_from_failures(self):
        """After consecutive failures, consecutive completions should recover score."""
        template = await sync_to_async(DeliveryJobTemplateFactory)(success_score=1.0)

        # 3 failures: 1.0 → 0.70 → 0.49 → 0.343
        for _ in range(3):
            await DeliveryJobTemplate.objects.filter(pk=template.pk).aupdate(
                success_score=Greatest(0.1, F("success_score") * 0.70),
            )

        await template.arefresh_from_db()
        self.assertAlmostEqual(template.success_score, 0.343, places=2)

        # 5 completions: 0.343 → 0.394 → 0.454 → 0.522 → 0.600 → 0.690
        for _ in range(5):
            await DeliveryJobTemplate.objects.filter(pk=template.pk).aupdate(
                success_score=Least(2.0, F("success_score") * 1.15),
            )

        await template.arefresh_from_db()
        # Should be recovering but still below 1.0
        self.assertGreater(template.success_score, 0.6)
        self.assertLess(template.success_score, 0.75)

    async def test_lifetime_counters_increment(self):
        """Both lifetime counters should track independently."""
        template = await sync_to_async(DeliveryJobTemplateFactory)(
            success_score=1.0,
            lifetime_completions=0,
            lifetime_expirations=0,
        )

        # 2 completions
        for _ in range(2):
            await DeliveryJobTemplate.objects.filter(pk=template.pk).aupdate(
                success_score=Least(2.0, F("success_score") * 1.15),
                lifetime_completions=F("lifetime_completions") + 1,
            )

        # 1 expiry
        await DeliveryJobTemplate.objects.filter(pk=template.pk).aupdate(
            success_score=Greatest(0.1, F("success_score") * 0.70),
            lifetime_expirations=F("lifetime_expirations") + 1,
        )

        await template.arefresh_from_db()
        self.assertEqual(template.lifetime_completions, 2)
        self.assertEqual(template.lifetime_expirations, 1)
