from django.db import models
from typing import override, final, ClassVar, TYPE_CHECKING


@final
class TuningWorkshopSubmission(models.Model):
    """Tracks a #tuning-workshop forum post for scheduled reward processing."""

    thread_id = models.PositiveBigIntegerField(unique=True)
    author_discord_id = models.PositiveBigIntegerField()
    created_at = models.DateTimeField()
    reward_at = models.DateTimeField()  # created_at + 7 days
    processed = models.BooleanField(default=False)
    skipped = models.BooleanField(default=False)  # True if weekly limit exceeded
    reaction_count = models.PositiveIntegerField(default=0)
    voucher = models.OneToOneField(
        "amc.Voucher", on_delete=models.SET_NULL, null=True, blank=True
    )

    if TYPE_CHECKING:
        objects: ClassVar[models.Manager["TuningWorkshopSubmission"]]

    @override
    def __str__(self):
        status = "skipped" if self.skipped else ("processed" if self.processed else "pending")
        return f"Workshop #{self.thread_id} ({status})"
