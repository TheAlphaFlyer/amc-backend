import logging

from django.db.models import Count

from amc.models import Character, CharacterLocation, CharacterLocationStats

logger = logging.getLogger(__name__)


async def refresh_vehicle_stats(character: Character, since=None):
    """
    Compute or incrementally update vehicle stats for a single character.

    If `since` is provided, only processes rows after that timestamp and merges
    with existing stats. Otherwise does a full recompute.
    """
    filters = {"character": character, "vehicle_key__isnull": False}
    if since:
        filters["timestamp__gt"] = since

    # Per-character GROUP BY vehicle_key — uses (character_id, timestamp DESC) index
    qs = (
        CharacterLocation.objects.filter(**filters)
        .values("vehicle_key")
        .annotate(cnt=Count("id"))
    )

    delta: dict[str, int] = {}
    async for row in qs:
        delta[row["vehicle_key"]] = row["cnt"]

    if not delta and not since:
        # No data at all, nothing to store
        return

    # Get or create the stats row
    stats, _ = await CharacterLocationStats.objects.aget_or_create(character=character)

    if since and stats.vehicle_stats:
        # Incremental: merge delta into existing stats
        merged = dict(stats.vehicle_stats)  # copy
        for key, count in delta.items():
            merged[key] = merged.get(key, 0) + count
    else:
        # Full recompute
        merged = delta

    # Determine favourite
    if merged:
        favourite = max(merged, key=lambda k: merged[k])
    else:
        favourite = None

    total = sum(merged.values()) if merged else 0

    # Get the latest timestamp we've now processed
    latest_row = (
        await CharacterLocation.objects.filter(character=character)
        .order_by("-timestamp")
        .values("timestamp")
        .afirst()
    )
    last_ts = latest_row["timestamp"] if latest_row else None

    stats.favourite_vehicle = favourite
    stats.vehicle_stats = merged
    stats.total_location_records = total
    stats.last_computed_at = last_ts
    await stats.asave(
        update_fields=[
            "favourite_vehicle",
            "vehicle_stats",
            "total_location_records",
            "last_computed_at",
        ]
    )


async def refresh_all_vehicle_stats(ctx=None):
    """
    Refresh vehicle stats for all characters that have new location data.
    Called by the hourly arq cron.

    For characters with existing stats, does incremental update (only new rows).
    For characters without stats, does full recompute.
    """
    characters_with_locations = (
        Character.objects.filter(last_online__isnull=False)
        .select_related("location_stats")
        .order_by("id")
    )

    count = 0
    async for character in characters_with_locations:
        try:
            existing_stats = character.location_stats
            since = existing_stats.last_computed_at
        except CharacterLocationStats.DoesNotExist:
            since = None

        try:
            await refresh_vehicle_stats(character, since=since)
            count += 1
        except Exception:
            logger.exception(
                "Failed to refresh vehicle stats for character %s", character.id
            )

    logger.info("Refreshed vehicle stats for %d characters", count)
