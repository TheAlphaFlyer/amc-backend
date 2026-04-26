import logging

from django.conf import settings

from amc.handlers import register
from amc.mod_server import clear_suspect, make_suspect
from amc.models import CriminalRecord, Wanted

logger = logging.getLogger("amc.webhook.handlers.customization")

COSTUME_SLOT = 4
CRIMINAL_SUSPECT_DURATION = 70  # seconds — slightly > tick interval (60s) for continuous overlap


@register("ServerSetEquipmentInventory")
async def handle_set_equipment_inventory(event, player, character, ctx):
    data = event.get("data", {}) or {}
    equipped   = data.get("Equipped",   []) or []
    unequipped = data.get("Unequipped", []) or []

    costume_equipped   = next((e for e in equipped   if e.get("Slot") == COSTUME_SLOT), None)
    costume_unequipped = next((e for e in unequipped if e.get("Slot") == COSTUME_SLOT), None)

    if not costume_equipped and not costume_unequipped:
        return 0, 0, 0, 0  # nothing to do for hat/glasses/beard

    was_wearing_costume = character.wearing_costume

    if costume_equipped:
        new_key = costume_equipped.get("ItemKey") or None
        character.wearing_costume  = new_key in settings.SUSPECT_COSTUMES
        character.costume_item_key = new_key
    else:
        character.wearing_costume  = False
        character.costume_item_key = None

    await character.asave(update_fields=["wearing_costume", "costume_item_key"])

    # Immediate suspect poke so the wearer lights up on cops' HUD without
    # waiting for the 10 s refresh tick. Gated on an active CriminalRecord.
    if character.wearing_costume and ctx.http_client_mod and character.guid:
        has_record = await CriminalRecord.objects.filter(
            character=character, cleared_at__isnull=True
        ).aexists()
        if has_record:
            try:
                await make_suspect(
                    ctx.http_client_mod,
                    character.guid,
                    duration_seconds=CRIMINAL_SUSPECT_DURATION,
                )
            except Exception:
                logger.warning("make_suspect (costume-equip) failed for %s",
                               character.name, exc_info=True)

    # Immediate suspect GE removal when the player stops wearing a suspect
    # costume — clears the blue overlay / Net_Suspects entry without waiting
    # up to 10 s for the refresh_suspect_tags transition-out pass.
    if was_wearing_costume and not character.wearing_costume and ctx.http_client_mod and character.guid:
        is_wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True, wanted_remaining__gt=0
        ).aexists()
        if not is_wanted:
            try:
                await clear_suspect(ctx.http_client_mod, character.guid)
            except Exception:
                logger.warning("clear_suspect (costume-change) failed for %s",
                               character.name, exc_info=True)

    return 0, 0, 0, 0
