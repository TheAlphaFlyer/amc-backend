from amc.command_framework import registry, CommandContext
from amc.game_server import get_players
from amc.models import CriminalRecord, PoliceSession, Wanted
from amc.special_cargo import calculate_criminal_level
from amc.criminals import _compute_stars
from django.utils import timezone
from django.utils.translation import gettext_lazy
from django.db.models import OuterRef, Subquery



def _stars(n: int) -> str:
    """Return n filled stars + (5-n) empty stars."""
    return "★" * n + "☆" * (5 - n)


@registry.register(
    "/wanted",
    description=gettext_lazy("List wanted criminals"),
    category="Faction",
)
async def cmd_wanted(ctx: CommandContext):
    timezone.now()

    # --- Online player GUIDs ---
    online_guids: set[str] = set()
    players = await get_players(ctx.http_client)
    if players:
        for _uid, pdata in players:
            guid = pdata.get("character_guid")
            if guid:
                online_guids.add(guid)

    # --- Active cops (excluded from both lists) ---
    active_cop_ids: set[int] = set()
    async for session in PoliceSession.objects.filter(ended_at__isnull=True):
        active_cop_ids.add(session.character_id)

    # --- Section 1: Active Wanted records (have a live bounty) ---
    # Annotate with confiscatable_amount from the linked CriminalRecord
    confiscatable_sq = (
        CriminalRecord.objects.filter(
            character=OuterRef("character"), cleared_at__isnull=True
        )
        .order_by("-confiscatable_amount")
        .values("confiscatable_amount")[:1]
    )

    active_bounties: list[dict] = []
    active_character_ids: set[int] = set()
    async for wanted in (
        Wanted.objects.filter(expired_at__isnull=True, wanted_remaining__gt=0)
        .select_related("character")
        .annotate(confiscatable_amount=Subquery(confiscatable_sq))
    ):
        if wanted.character_id in active_cop_ids:
            continue
        active_character_ids.add(wanted.character_id)
        stars = _compute_stars(wanted.wanted_remaining)
        is_online = wanted.character.guid in online_guids
        confiscatable = wanted.confiscatable_amount or 0
        active_bounties.append(
            {
                "name": wanted.character.name,
                "stars": stars,
                "confiscatable_amount": confiscatable,
                "online": is_online,
            }
        )

    # Sort active bounties: online first, then by confiscatable_amount desc within each group
    active_bounties.sort(key=lambda e: (not e["online"], -e["confiscatable_amount"]))

    # --- Section 2: Criminal records without an active Wanted ---
    other_records = [
        r
        async for r in CriminalRecord.objects.filter(cleared_at__isnull=True)
        .order_by("-confiscatable_amount")
        .exclude(character_id__in=active_character_ids)
        .exclude(character_id__in=active_cop_ids)
        .select_related("character")
    ]

    if not active_bounties and not other_records:
        await ctx.reply("No wanted criminals")
        return

    # Sort other records by criminal level desc
    other_entries = []
    for record in other_records:
        laundered = record.character.criminal_laundered_total
        level = calculate_criminal_level(laundered)
        guid = record.character.guid
        other_entries.append(
            {
                "name": record.character.name,
                "guid": guid,
                "level": level,
                "laundered": laundered,
                "confiscatable_amount": record.confiscatable_amount,
                "online": guid in online_guids,
            }
        )
    other_online = sorted(
        [e for e in other_entries if e["online"]],
        key=lambda e: e["confiscatable_amount"],
        reverse=True,
    )
    other_offline = sorted(
        [e for e in other_entries if not e["online"]],
        key=lambda e: e["confiscatable_amount"],
        reverse=True,
    )

    # --- Build message ---
    msg = "<Title>Wanted List</>\n\n"

    def _row_bounty(e: dict) -> str:
        confiscatable = e["confiscatable_amount"]
        amount_str = f"${confiscatable:,}" if confiscatable > 0 else "no bounty"
        return f"{_stars(e['stars'])} {e['name']} <Secondary>{amount_str}</>\n"

    def _row_record(e: dict) -> str:
        confiscatable = e["confiscatable_amount"]
        amount_str = f"${confiscatable:,}" if confiscatable > 0 else "no bounty"
        return (
            f"<Highlight>C{e['level']}</> {e['name']}"
            f" <Secondary>{amount_str}</>\n"
        )

    if active_bounties:
        bounties_online = [e for e in active_bounties if e["online"]]
        bounties_offline = [e for e in active_bounties if not e["online"]]
        msg += "<Title>Active Bounties</>\n"
        if bounties_online:
            msg += "<EffectGood>Online</>\n"
            for e in bounties_online:
                msg += _row_bounty(e)
        if bounties_offline:
            msg += "<Warning>Offline</>\n"
            for e in bounties_offline:
                msg += _row_bounty(e)
        msg += "\n"
    # (active_bounties already sorted: online first, then confiscatable_amount desc)

    if other_online or other_offline:
        msg += "<Title>Criminal Record</>\n"
        if other_online:
            msg += "<EffectGood>Online</>\n"
            for e in other_online:
                msg += _row_record(e)
        if other_offline:
            msg += "<Warning>Offline</>\n"
            for e in other_offline:
                msg += _row_record(e)

    await ctx.reply(msg.rstrip())
