"""
Custom/modded vehicle parts detection.

Compares a vehicle's parts keys against the stock parts catalogue in the game
sqlite database to identify non-stock (custom/modded) parts.
"""

import os
import sqlite3
import logging
from typing import Optional

from amc.enums import VehiclePartSlot

log = logging.getLogger(__name__)

GAME_DB_PATH = os.environ.get("GAME_DB_PATH", "/var/lib/motortown/gamedata.db")

# Module-level cache — loaded once on first call
_stock_part_keys: Optional[set[str]] = None


def get_stock_part_keys() -> set[str]:
    """
    Load all stock part keys from the game database.
    Results are cached in-memory after first call.
    """
    global _stock_part_keys
    if _stock_part_keys is not None:
        return _stock_part_keys

    try:
        conn = sqlite3.connect(
            f"file:{GAME_DB_PATH}?mode=ro",
            uri=True,
            timeout=5,
        )
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM vehicle_parts")
        _stock_part_keys = {row[0] for row in cursor.fetchall()}
        conn.close()
        log.info(f"Loaded {len(_stock_part_keys)} stock part keys from game database")
    except Exception as e:
        log.error(f"Failed to load stock part keys: {e}")
        _stock_part_keys = set()

    return _stock_part_keys


def _slot_name(slot_value: int) -> str:
    """Get human-readable slot name from slot enum value."""
    try:
        return VehiclePartSlot(slot_value).name
    except ValueError:
        return f"Unknown({slot_value})"


def detect_custom_parts(parts: list[dict]) -> list[dict]:
    """
    Detect custom/modded parts in a vehicle's parts list.

    Args:
        parts: List of part dicts with at least 'Key' and 'Slot' fields.

    Returns:
        List of dicts for each custom part found:
        [{'key': str, 'slot': str, 'slot_value': int}]
    """
    stock_keys = get_stock_part_keys()
    if not stock_keys:
        log.warning("Stock parts cache is empty — skipping detection")
        return []

    custom = []
    for part in parts:
        key = part.get("Key", "")
        slot_value = part.get("Slot", 0)
        if key and key not in stock_keys:
            custom.append({
                "key": key,
                "slot": _slot_name(slot_value),
                "slot_value": slot_value,
            })

    return custom


def format_custom_parts(custom_parts: list[dict]) -> str:
    """Format custom parts list for display."""
    if not custom_parts:
        return "✅ All stock parts"
    lines = [f"**{p['slot']}**: {p['key']}" for p in custom_parts]
    return "\n".join(lines)


def format_custom_parts_plain(custom_parts: list[dict]) -> str:
    """Format custom parts list for plain text (management command)."""
    if not custom_parts:
        return "All stock parts"
    lines = [f"  {p['slot']}: {p['key']}" for p in custom_parts]
    return "\n".join(lines)


def format_custom_parts_game(custom_parts: list[dict]) -> str:
    """Format custom parts list for in-game popup (Motor Town markup)."""
    if not custom_parts:
        return "All stock parts"
    lines = [f"<Bold>{p['slot']}</>  {p['key']}" for p in custom_parts]
    return "\n".join(lines)

