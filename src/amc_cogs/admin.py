from django.contrib import admin

from amc_cogs.models import TuningWorkshopSubmission


@admin.register(TuningWorkshopSubmission)
class TuningWorkshopSubmissionAdmin(admin.ModelAdmin):
    list_display = [
        "thread_id",
        "author_discord_id",
        "created_at",
        "reaction_count",
        "rewarded_reaction_count",
        "skipped",
    ]
    list_filter = ["skipped"]
    search_fields = ["thread_id", "author_discord_id"]
    readonly_fields = [
        "thread_id",
        "author_discord_id",
        "created_at",
        "reaction_count",
        "rewarded_reaction_count",
    ]
    ordering = ["-created_at"]
