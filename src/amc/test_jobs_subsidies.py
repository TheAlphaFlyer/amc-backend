from django.test import TestCase
from django.utils import timezone
from amc.models import DeliveryJob, Cargo, Player, Character, DeliveryPoint
from amc.webhook import atomic_process_delivery


class SubsidyBugTest(TestCase):
    def setUp(self):
        self.player = Player.objects.create(unique_id=123)
        self.character = Character.objects.create(player=self.player, name="TestChar")
        self.cargo, _ = Cargo.objects.get_or_create(
            key="SunflowerSeed", defaults={"label": "Sunflower Seed"}
        )
        self.point, _ = DeliveryPoint.objects.get_or_create(
            guid="dasa", defaults={"name": "Dasa", "coord": "POINT(0 0 0)"}
        )

    def _make_job(self, bonus_multiplier=2.0):
        return DeliveryJob.objects.create(
            name="Sunflower for Dasa",
            cargo_key="SunflowerSeed",
            quantity_requested=100,
            quantity_fulfilled=0,
            bonus_multiplier=bonus_multiplier,
            expired_at=timezone.now() + timezone.timedelta(days=1),
        )

    def _make_delivery_data(self, quantity=10, payment=10000, subsidy=0, rp_mode=False):
        return {
            "timestamp": timezone.now(),
            "character": self.character,
            "cargo_key": "SunflowerSeed",
            "quantity": quantity,
            "payment": payment,
            "subsidy": subsidy,
            "rp_mode": rp_mode,
        }

    def test_bonus_with_zero_subsidy(self):
        """Job bonus is added when there's no existing subsidy."""
        job = self._make_job(bonus_multiplier=2.0)
        delivery_data = self._make_delivery_data(subsidy=0)

        atomic_process_delivery(job.id, 10, delivery_data)

        # bonus = 10000 * (2.0 - 1) = 10000
        self.assertEqual(delivery_data["subsidy"], 10000)

    def test_bonus_adds_to_existing_subsidy(self):
        """Job bonus is ADDED to existing cargo subsidy, not replaced."""
        job = self._make_job(bonus_multiplier=1.5)
        cargo_subsidy = 5000
        delivery_data = self._make_delivery_data(subsidy=cargo_subsidy)

        atomic_process_delivery(job.id, 10, delivery_data)

        # bonus = 10000 * (1.5 - 1) = 5000
        # total = 5000 (cargo) + 5000 (job) = 10000
        self.assertEqual(delivery_data["subsidy"], 10000)

    def test_bonus_adds_to_rp_subsidy(self):
        """Job bonus stacks on top of RP-mode subsidy."""
        job = self._make_job(bonus_multiplier=1.2)
        rp_subsidy = 12500  # Pre-calculated RP subsidy
        delivery_data = self._make_delivery_data(subsidy=rp_subsidy, rp_mode=True)

        atomic_process_delivery(job.id, 10, delivery_data)

        # bonus = 10000 * (1.2 - 1) = 2000
        # total = 12500 (RP) + 2000 (job) = 14500
        self.assertEqual(delivery_data["subsidy"], 14500)

    def test_high_bonus_adds_to_subsidy(self):
        """High bonus multiplier still adds to existing subsidy."""
        job = self._make_job(bonus_multiplier=5.0)
        cargo_subsidy = 5000
        delivery_data = self._make_delivery_data(subsidy=cargo_subsidy)

        atomic_process_delivery(job.id, 10, delivery_data)

        # bonus = 10000 * (5.0 - 1) = 40000
        # total = 5000 (cargo) + 40000 (job) = 45000
        self.assertEqual(delivery_data["subsidy"], 45000)

    def test_no_bonus_when_multiplier_is_one(self):
        """A 1.0x multiplier means no bonus added."""
        job = self._make_job(bonus_multiplier=1.0)
        cargo_subsidy = 3000
        delivery_data = self._make_delivery_data(subsidy=cargo_subsidy)

        atomic_process_delivery(job.id, 10, delivery_data)

        # bonus = 10000 * (1.0 - 1) = 0
        # total = 3000 (cargo) + 0 = 3000
        self.assertEqual(delivery_data["subsidy"], 3000)

    def test_no_bonus_without_job(self):
        """Without a job, subsidy stays unchanged."""
        delivery_data = self._make_delivery_data(subsidy=8000)

        atomic_process_delivery(None, 10, delivery_data)

        self.assertEqual(delivery_data["subsidy"], 8000)

    def test_partial_fulfillment_bonus(self):
        """Bonus scales proportionally when only part of delivery counts."""
        job = self._make_job(bonus_multiplier=2.0)
        job.quantity_fulfilled = 95
        job.save()

        # Deliver 10, but only 5 can count toward the job (100 - 95 = 5)
        delivery_data = self._make_delivery_data(quantity=10, payment=10000, subsidy=2000)

        atomic_process_delivery(job.id, 10, delivery_data)

        # bonus = 10000 * (5/10) * (2.0 - 1) = 5000
        # total = 2000 (cargo) + 5000 (job) = 7000
        self.assertEqual(delivery_data["subsidy"], 7000)
