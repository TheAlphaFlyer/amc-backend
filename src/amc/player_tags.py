import re
import logging
from amc.mod_server import set_character_name

logger = logging.getLogger(__name__)

# Compile regexes for stripping prefix tags
TAG_PATTERNS = [
    re.compile(r"\[MOD\]\s*", re.IGNORECASE),
    re.compile(r"\[GOV\d*\]\s*", re.IGNORECASE),
    re.compile(r"\[DOT\]\s*", re.IGNORECASE),
]


def strip_all_tags(name: str) -> str:
    """Remove all known tag prefixes from a name."""
    clean_name = name
    for pattern in TAG_PATTERNS:
        clean_name = pattern.sub("", clean_name)
    return clean_name.strip()


def build_display_name(
    base_name: str, *, has_custom_parts: bool = False, gov_level: int = 0
) -> str:
    """Build the definitive display name with all applicable tags.

    Tag order: [MOD] [GOV#] BaseName

    Args:
        base_name: The player's original name (stripped of any existing tags)
        has_custom_parts: Whether the player's current vehicle has custom/modded parts
        gov_level: Government employee level (0 = not a gov employee)
    """
    clean_name = strip_all_tags(base_name)
    tags = []

    if has_custom_parts:
        tags.append("[MOD]")

    if gov_level > 0:
        tags.append(f"[GOV{gov_level}]")

    if tags:
        return f"{' '.join(tags)} {clean_name}"
    return clean_name


async def refresh_player_name(
    character, session, *, has_custom_parts: bool | None = None
):
    """Recompute and apply the correct display name for a character.

    This is the ONLY function that should call set_character_name.
    Reads character state (gov_employee, etc.) and computes the definitive name.

    Args:
        character: Character model instance (with player relation loaded)
        session: HTTP client for mod server
        has_custom_parts: If provided, use this value. If None, preserve the
            character's current MOD tag state (from custom_name).
    """
    if not character:
        return

    # Determine MOD state
    if has_custom_parts is None:
        # Preserve existing state if not explicitly specified
        current_name = character.custom_name or character.name
        has_custom_parts = bool(re.search(r"\[MOD\]", current_name, re.IGNORECASE))

    # Determine GOV state
    gov_level = 0
    if character.is_gov_employee:
        from amc.gov_employee import calculate_gov_level

        gov_level = calculate_gov_level(character.gov_employee_contributions)

    # Reconstruct name
    new_name = build_display_name(
        character.name, has_custom_parts=has_custom_parts, gov_level=gov_level
    )

    # Save to DB if changed
    if new_name != character.custom_name:
        # If it matches the original character name exactly, we set custom_name to None
        if new_name == character.name:
            character.custom_name = None
        else:
            character.custom_name = new_name
            
        await character.asave(update_fields=["custom_name"])

    # Push to game server if GUID exists
    if session and character.guid:
        try:
            await set_character_name(session, character.guid, new_name)
        except Exception as e:
            logger.exception(f"Failed to set character name for {character.name}: {e}")
