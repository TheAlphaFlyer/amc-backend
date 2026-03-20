from django.db import models
from typing import override, final, ClassVar, TYPE_CHECKING


@final
class TuningWorkshopSubmission(models.Model):
    """Tracks a #tuning-workshop forum post for on-demand reward claiming."""

    thread_id = models.PositiveBigIntegerField(unique=True)
    author_discord_id = models.PositiveBigIntegerField()
    created_at = models.DateTimeField()
    skipped = models.BooleanField(default=False)  # True if weekly limit exceeded
    reaction_count = models.PositiveIntegerField(default=0)
    rewarded_reaction_count = models.PositiveIntegerField(default=0)

    if TYPE_CHECKING:
        objects: ClassVar[models.Manager["TuningWorkshopSubmission"]]

    @override
    def __str__(self):
        if self.skipped:
            return f"Workshop #{self.thread_id} (skipped)"
        return f"Workshop #{self.thread_id} ({self.rewarded_reaction_count}/{self.reaction_count} rewarded)"
