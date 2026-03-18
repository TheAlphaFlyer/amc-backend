"""
Custom/modded vehicle parts detection.

Compares a vehicle's parts keys against the stock parts catalogue in the game
sqlite database to identify non-stock (custom/modded) parts.

Player part keys encode tuning parameters inline:
  - Damper200_200  = stock Damper200 with Rebound=2.0
  - BrakePower_200 = stock BrakePower with Multiplier=2.0
  - WheelSpacer_50 = stock WheelSpacer with Space=5.0
  - SmallRadiator_100 = stock SmallRadiator with CoolingPower=1.0

The stock key set includes both base RowNames and all valid tuned variants.
"""

import os
import sqlite3
import logging
from collections import defaultdict
from typing import Optional

from amc.enums import VehiclePartSlot

log = logging.getLogger(__name__)

GAME_DB_PATH = os.environ.get("GAME_DB_PATH", "/var/lib/motortown/gamedata.db")

# Module-level cache — loaded once on first call
_stock_part_keys: Optional[set[str]] = None


# Suffix encoding rules per part_type.
# Each rule: (struct_type, field_name, multiplier)
# Generates: f"{base_row_name}_{int(value * multiplier)}" for each unique value
SUFFIX_RULES: dict[str, list[tuple[str, str, int]]] = {
    "Suspension_Damper": [
        ("SuspensionDamper", "ReboundDampingRateMultiplier", 100),
    ],
    "BrakePower": [
        ("BrakePower", "BrakePowerMultiplier", 100),
    ],
    "CoolantRadiator": [
        ("CoolantRadiator", "CoolingPower", 100),
    ],
    "WheelSpacer": [
        ("WheelSpacer", "Space", 10),
    ],
    "AntiRollBar": [
        ("AntiRollBar", "AntiRollBarRateMultiplier", 100),
    ],
    "AngleKit": [
        ("AngleKit", "AngleIncreaseInDegree", 1),
    ],
}


def get_stock_part_keys() -> set[str]:
    """
    Load all stock part keys from the game database, including
    all valid tuned-name variants.

    For suffix-encoded parts (Damper, BrakePower, etc.), generates
    valid keys by combining each base RowName with ALL known tuning
    values of that part type.

    For tire/LSD parts, generates valid keys by combining each base
    RowName with blueprint variant suffixes from the game pak.

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

        # Load base part IDs and types
        parts_by_type: dict[str, set[str]] = defaultdict(set)
        all_part_ids = set()
        for row in cursor.execute("SELECT id, part_type FROM vehicle_parts"):
            part_id, part_type = row[0], row[1] or ""
            all_part_ids.add(part_id)
            parts_by_type[part_type].add(part_id)

        # Collect all unique tuning values per (part_type, struct_type, field_name)
        # Uses part_type_values table which preserves all values from duplicate RowNames
        values_by_type: dict[str, dict[tuple[str, str], set[float]]] = defaultdict(
            lambda: defaultdict(set)
        )
        try:
            for row in cursor.execute(
                "SELECT part_type, struct_type, field_name, field_value FROM part_type_values"
            ):
                part_type, struct_type, field_name, value = row
                values_by_type[part_type][(struct_type, field_name)].add(value)
        except sqlite3.OperationalError:
            # Fallback for old DB without part_type_values table
            pass

        # Load blueprint variant suffixes (tire/LSD pak asset variants)
        # Maps (asset_type, base_name) -> set of variant_name
        bp_variants: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        try:
            for row in cursor.execute(
                "SELECT base_name, variant_name, asset_type FROM blueprint_variants"
            ):
                base_name, variant_name, asset_type = row
                bp_variants[asset_type][base_name].add(variant_name)
        except sqlite3.OperationalError:
            pass

        conn.close()

        # Build valid keys
        _stock_part_keys = set(all_part_ids)

        # Suffix rules for tuning-encoded parts (Damper, BrakePower, etc.)
        for part_type, rules in SUFFIX_RULES.items():
            base_ids = parts_by_type.get(part_type, set())
            type_values = values_by_type.get(part_type, {})
            for base_id in base_ids:
                for struct_type, field_name, multiplier in rules:
                    values = type_values.get((struct_type, field_name), set())
                    for value in values:
                        suffix_val = int(value * multiplier)
                        _stock_part_keys.add(f"{base_id}_{suffix_val}")

        # Tire blueprint variant suffixes
        # Extract unique suffix values from TirePhysics variants only
        tire_suffix_values: set[int] = set()
        for base_name, variants in bp_variants.get("TirePhysics", {}).items():
            for variant_name in variants:
                suffix = variant_name[len(base_name):]
                for part in suffix.lstrip("_").split("_"):
                    if part.isdigit():
                        tire_suffix_values.add(int(part))

        for part_id in parts_by_type.get("Tire", set()):
            for val in tire_suffix_values:
                _stock_part_keys.add(f"{part_id}_{val}")

        # LSD blueprint variant suffixes
        # Extract unique suffix values from LSD variants only
        lsd_suffix_values: set[int] = set()
        for base_name, variants in bp_variants.get("LSD", {}).items():
            for variant_name in variants:
                suffix = variant_name[len(base_name):]
                for part in suffix.split("_"):
                    if part.isdigit():
                        lsd_suffix_values.add(int(part))

        for part_id in parts_by_type.get("LSD", set()):
            for val in lsd_suffix_values:
                _stock_part_keys.add(f"{part_id}_{val}")
                # Double suffix for 1.5-way (accel + brake)
                for val2 in lsd_suffix_values:
                    _stock_part_keys.add(f"{part_id}_{val}_{val2}")

        tuned_count = len(_stock_part_keys) - len(all_part_ids)
        log.info(
            "Loaded %d stock part keys (%d base + %d tuned variants) from game database",
            len(_stock_part_keys),
            len(all_part_ids),
            tuned_count,
        )
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
    lines = [f"<Bold>{p['slot']}</> {p['key']}" for p in custom_parts]
    return "\n".join(lines)
