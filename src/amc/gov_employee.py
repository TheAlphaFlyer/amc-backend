import re
from datetime import timedelta
from django.db.models import F
from django.utils import timezone
from amc.mod_server import set_character_name
from amc_finance.services import player_donation

GOV_LEVEL_STEP = 500_000
GOV_ROLE_DURATION = timedelta(hours=24)
GOV_TAG_PATTERN = re.compile(r"\[GOV\d*\]\s*", re.IGNORECASE)


def calculate_gov_level(contributions: int) -> int:
    """Calculate government employee level from cumulative contributions.
    Level scales infinitely: floor(contributions / step) + 1"""
    return (contributions // GOV_LEVEL_STEP) + 1


def make_gov_name(base_name: str, level: int) -> str:
    """Create the [GOV#] prefixed name."""
    clean = GOV_TAG_PATTERN.sub("", base_name).strip()
    return f"[GOV{level}] {clean}"


def strip_gov_name(name: str) -> str:
    """Remove the [GOV#] prefix from a name."""
    return GOV_TAG_PATTERN.sub("", name).strip()


async def activate_gov_role(character, session):
    """Activate the government employee role for 24 hours."""
    level = calculate_gov_level(character.gov_employee_contributions)
    character.gov_employee_until = timezone.now() + GOV_ROLE_DURATION
    character.gov_employee_level = level
    await character.asave(update_fields=["gov_employee_until", "gov_employee_level"])

    gov_name = make_gov_name(character.name, level)
    character.custom_name = gov_name
    await character.asave(update_fields=["custom_name"])

    if session and character.guid:
        await set_character_name(session, character.guid, gov_name)


async def deactivate_gov_role(character, session):
    """Deactivate the government employee role and restore name."""
    character.gov_employee_until = None
    original_name = strip_gov_name(character.custom_name or character.name)
    # If the stripped name matches the original name, clear custom_name
    if original_name == character.name:
        character.custom_name = None
    else:
        character.custom_name = original_name
    await character.asave(update_fields=["gov_employee_until", "custom_name"])

    if session and character.guid:
        await set_character_name(
            session, character.guid, original_name or character.name
        )


async def redirect_income_to_treasury(
    amount, character, description, http_client=None, session=None, contribution=None
):
    """Record a government employee's income as a treasury contribution.

    Args:
        amount: Real money confiscated from wallet → treasury ledger.
        contribution: Total economic value for gov level progression.
            Defaults to amount if not specified. May include subsidy
            credit that was never in the wallet.
    """
    if contribution is None:
        contribution = amount

    # Ledger: only record real money that was confiscated
    await player_donation(int(amount), character, description=description)

    # Contributions: track full economic value (including subsidy) for levels
    character.gov_employee_contributions = F("gov_employee_contributions") + int(contribution)
    await character.asave(update_fields=["gov_employee_contributions"])

    # Refresh to get actual DB value, then recalculate level
    await character.arefresh_from_db(fields=["gov_employee_contributions"])
    new_level = calculate_gov_level(character.gov_employee_contributions)
    if new_level != character.gov_employee_level:
        character.gov_employee_level = new_level
        await character.asave(update_fields=["gov_employee_level"])

        # Level up logic
        gov_name = make_gov_name(character.name, new_level)
        character.custom_name = gov_name
        await character.asave(update_fields=["custom_name"])

        if session and character.guid:
            await set_character_name(session, character.guid, gov_name)

        if http_client:
            from amc.game_server import announce
            import asyncio

            asyncio.create_task(
                announce(
                    f"🎉 {character.name} has been promoted to Government Employee Level {new_level}!",
                    http_client,
                    color="90EE90",
                )
            )


async def expire_gov_employees(ctx):
    """Cron task: deactivate expired gov employee roles for online players."""
    from amc.models import Character

    expired = Character.objects.filter(
        gov_employee_until__isnull=False,
        gov_employee_until__lt=timezone.now(),
    ).select_related("player")
    http_client_mod = ctx.get("http_client_mod")
    async for character in expired:
        try:
            await deactivate_gov_role(character, http_client_mod)
        except Exception as e:
            import logging

            logging.getLogger(__name__).exception(
                f"Error expiring gov role for {character}: {e}"
            )
