from django.contrib import admin

from amc_cogs.models import TuningWorkshopSubmission


@admin.register(TuningWorkshopSubmission)
class TuningWorkshopSubmissionAdmin(admin.ModelAdmin):
    list_display = [
        "thread_id",
        "author_discord_id",
        "created_at",
        "reward_at",
        "reaction_count",
        "processed",
        "skipped",
        "voucher",
    ]
    list_filter = ["processed", "skipped"]
    search_fields = ["thread_id", "author_discord_id"]
    readonly_fields = [
        "thread_id",
        "author_discord_id",
        "created_at",
        "reward_at",
        "reaction_count",
        "voucher",
    ]
    ordering = ["-created_at"]
