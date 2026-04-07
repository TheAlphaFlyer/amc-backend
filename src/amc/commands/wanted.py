from amc.command_framework import registry, CommandContext
from amc.game_server import get_players
from amc.models import CriminalRecord, PoliceSession, Wanted
from amc.special_cargo import calculate_criminal_level
from amc.criminals import _compute_stars
from django.utils import timezone
from django.utils.translation import gettext_lazy



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
    active_bounties = []
    async for wanted in (
        Wanted.objects.filter(expired_at__isnull=True, wanted_remaining__gt=0)
        .select_related("character")
        .order_by("-amount")
    ):
        if wanted.character_id in active_cop_ids:
            continue
        stars = _compute_stars(wanted.wanted_remaining)
        is_online = wanted.character.guid in online_guids
        active_bounties.append(
            {
                "name": wanted.character.name,
                "stars": stars,
                "amount": wanted.amount,
                "online": is_online,
            }
        )

    # --- Section 2: Criminal records without an active Wanted ---
    # Collect character IDs already shown in active bounties
    active_character_ids = set()
    async for wanted in Wanted.objects.filter(
        expired_at__isnull=True, wanted_remaining__gt=0
    ):
        active_character_ids.add(wanted.character_id)

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

    if active_bounties:
        msg += "<Title>⚠ Active Bounties</>\n<Secondary></>\n"
        for e in active_bounties:
            status = "🟢" if e["online"] else "🔴"
            bounty_str = f"${e['amount']:,}" if e["amount"] > 0 else "no bounty"
            msg += (
                f"{_stars(e['stars'])} {status} {e['name']}"
                f" <Secondary>{bounty_str}</>\n"
            )
        msg += "\n"

    if other_online or other_offline:
        msg += "<Title>Criminal Record</>\n<Secondary></>\n"
        for e in other_online + other_offline:
            status = "🟢" if e["online"] else "🔴"
            confiscatable = e["confiscatable_amount"]
            amount_str = f" ${confiscatable:,}" if confiscatable > 0 else ""
            msg += (
                f"<Highlight>C{e['level']}</> {status} {e['name']}"
                f" <Secondary>${e['laundered']:,}{amount_str}</>\n"
            )

    await ctx.reply(msg.rstrip())
