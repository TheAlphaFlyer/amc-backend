import re
import logging
from django.core.cache import cache
from amc.mod_server import set_character_name

logger = logging.getLogger(__name__)

PUSHED_NAME_TTL = 3600  # 1 hour

# Regexes for stripping tags — covers both new compact and legacy formats
TAG_PATTERNS = [
    # New compact format: must start with C/M/G/P or *, optionally followed by more letters/digits/stars
    # e.g. [C], [M], [G3], [CM], [MG3], [CMG12], [*****], [C***], [MP], [MPG3], [MC1***G3]
    re.compile(r"\[(?=[CMGP*])[CMGP\d*]+\]\s*"),
    # Legacy formats with W-prefix or Unicode stars (for players who logged in before star refactor)
    re.compile(r"\[(?=[CMGPW\u2605])[CMGPW\d\u2605]+\]\s*"),
    # Legacy compact format with Unicode subscript digits (e.g. [G₃], [MG₂₃])
    re.compile(r"\[(?=[CMGP])[CMGP₀₁₂₃₄₅₆₇₈₉]+\]\s*"),
    # Legacy formats (for players who logged in before the refactor)
    re.compile(r"\[CRIM\]\s*", re.IGNORECASE),
    re.compile(r"\[MODS\]\s*", re.IGNORECASE),
    re.compile(r"\[MOD\]\s*", re.IGNORECASE),
    re.compile(r"\[GOV\d*\]\s*", re.IGNORECASE),
    # NOTE: [DOT] is intentionally NOT stripped — it is a permanent player tag.
]


def strip_all_tags(name: str) -> str:
    """Remove all known tag prefixes from a name."""
    clean_name = name
    for pattern in TAG_PATTERNS:
        clean_name = pattern.sub("", clean_name)
    return clean_name.strip()


def name_has_mod_tag(name: str) -> bool:
    """Return True if the name currently carries a mod-parts tag."""
    return bool(
        re.search(r"\[MODS?\]", name, re.IGNORECASE)
        or re.search(r"\[[CGPW0-9₀-₉]*M[CGPW0-9₀-₉]*\]", name)
    )


def build_display_name(
    base_name: str,
    *,
    criminal_level: int = 0,
    has_custom_parts: bool = False,
    police_level: int = 0,
    gov_level: int = 0,
    wanted_stars: int = 0,
) -> str:
    """Build the definitive display name with a single compact tag.

    Tag format: [MP1*****C1G3] BaseName  (order: M, P, stars, C, G)
      M = Modded vehicle parts
      P1 = Police level (active session)
      ***** = Wanted level (1–5 stars, based on wanted_remaining heat)
      C1 = Criminal level (suppressed when police is active)
      G3 = Government employee level

    Args:
        base_name: The player's original name (stripped of any existing tags)
        criminal_level: Criminal level (0 = no active criminal record)
        has_custom_parts: Whether the player's current vehicle has custom/modded parts
        police_level: Police level (0 = not on duty)
        gov_level: Government employee level (0 = not a gov employee)
        wanted_stars: Wanted level (0–5, 0 = not wanted)
    """
    clean_name = strip_all_tags(base_name)
    tag = ""

    if has_custom_parts:
        tag += "M"

    if police_level > 0:
        tag += f"P{police_level}"

    if wanted_stars > 0:
        tag += "*" * wanted_stars

    # C is suppressed when police is active
    if criminal_level > 0 and police_level == 0:
        tag += f"C{criminal_level}"

    if gov_level > 0:
        tag += f"G{gov_level}"

    if tag:
        return f"[{tag}] {clean_name}"
    return clean_name


async def refresh_player_name(
    character, session, *, has_custom_parts: bool | None = None
):
    """Recompute and apply the correct display name for a character.

    This is the ONLY function that should call set_character_name.
    Reads character state (gov_employee, criminal_records, etc.) and computes the definitive name.

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
        # Preserve existing state — check for both legacy [MODS]/[MOD] and new [M]
        current_name = character.custom_name or character.name
        has_custom_parts = name_has_mod_tag(current_name)

    # Determine GOV state
    gov_level = 0
    if character.is_gov_employee:
        from amc.gov_employee import calculate_gov_level

        gov_level = calculate_gov_level(character.gov_employee_contributions)

    # Determine CRIM state
    from amc.models import CriminalRecord

    has_criminal_record = await CriminalRecord.objects.filter(
        character=character, cleared_at__isnull=True
    ).aexists()

    # Compute criminal level from cumulative laundered total
    criminal_level = 0
    if has_criminal_record:
        from amc.special_cargo import calculate_criminal_level

        criminal_level = calculate_criminal_level(character.criminal_laundered_total)

    # Determine WANTED state (W-level based on wanted_remaining heat)
    from amc.models import Wanted
    from amc.criminals import _compute_stars

    wanted_stars = 0
    try:
        wanted = await Wanted.objects.filter(
            character=character,
            expired_at__isnull=True,
        ).afirst()
        if wanted and wanted.wanted_remaining > 0:
            wanted_stars = _compute_stars(wanted.wanted_remaining)
    except Exception:
        pass

    # Determine POLICE state
    from amc.police import is_police as check_police, calculate_police_level

    police_level = 0
    if await check_police(character):
        police_level = calculate_police_level(character.police_confiscated_total)

    # Reconstruct name
    new_name = build_display_name(
        character.name,
        criminal_level=criminal_level,
        has_custom_parts=has_custom_parts,
        police_level=police_level,
        gov_level=gov_level,
        wanted_stars=wanted_stars,
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
        cache_key = f"pushed_name:{character.guid}"
        last_pushed = await cache.aget(cache_key)
        if last_pushed == new_name:
            return  # already correct on game server

        try:
            await set_character_name(session, character.guid, new_name)
            await cache.aset(cache_key, new_name, timeout=PUSHED_NAME_TTL)
        except Exception as e:
            logger.warning(f"Failed to set character name for {character.name}: {e}")
