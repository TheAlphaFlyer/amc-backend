"""Anti-cheat physics integrity monitoring.

PoC phase: observe-only. Logs all client reports. No enforcement, no banning.

Usage flow:
  1. Client mod sends POST /api/ac/report with physics values on vehicle entry.
  2. Django logs the report and compares against VEHICLE_REFERENCE if available.
  3. Mismatches are logged as warnings — no action taken.

Reference values are populated by observing clean client reports initially,
then hardcoded here once a baseline is established.
"""

import logging

logger = logging.getLogger("amc.anticheat")

# Float comparison tolerance — accounts for IEEE 754 rounding across platforms
FLOAT_EPSILON = 1e-3

# ──────────────────────────────────────────────────────────────────────────────
# Hardcoded reference values per vehicle class.
#
# Populated after PoC data collection phase. Leave empty initially.
# Key   = substring of the vehicle's full UObject class name
# Value = expected physics params
#
# Example (uncomment and adjust after PoC data collection):
# ──────────────────────────────────────────────────────────────────────────────
VEHICLE_REFERENCE: dict[str, dict] = {
    # "Volvo_FH16_750_C": {
    #     "AirDragCoeff": 0.35,
    #     "BrakeTorqueMultiplier": 1.0,
    #     "FuelTankCapacityInLiter": 400.0,
    #     "MaxSteeringAngleDegree": 35.0,
    #     "Wheels": [
    #         {
    #             "StaticMu": 1.2,
    #             "SlidingMu": 0.8,
    #             "RollingResistanceCoeff": 0.015,
    #             "OffroadFriction": 0.6,
    #             "MaxWeightKg": 3000.0,
    #             "WearRate": 0.001,
    #         },
    #     ],
    # },
}


def find_reference(vehicle_class: str) -> dict | None:
    """Find reference values matching the given vehicle class string."""
    for key, ref in VEHICLE_REFERENCE.items():
        if key in vehicle_class:
            return ref
    return None


def compare_values(client_values: dict, reference: dict) -> list[dict]:
    """Compare client-reported physics values against reference baseline.

    Returns a list of mismatch dicts. Empty list means all values match.
    """
    mismatches = []

    # Vehicle-level params
    for key in [
        "AirDragCoeff",
        "BrakeTorqueMultiplier",
        "FuelTankCapacityInLiter",
        "MaxSteeringAngleDegree",
    ]:
        cv = client_values.get(key)
        rv = reference.get(key)
        if cv is not None and rv is not None:
            if abs(cv - rv) > FLOAT_EPSILON:
                mismatches.append(
                    {
                        "property": key,
                        "client": cv,
                        "expected": rv,
                        "deviation": round(abs(cv - rv), 6),
                    }
                )

    # Per-wheel tire params
    client_wheels = client_values.get("Wheels", [])
    ref_wheels = reference.get("Wheels", [])
    if not ref_wheels:
        return mismatches

    for cw in client_wheels:
        idx = cw.get("Index", 0)
        # Use per-index ref if available, fall back to first wheel (most vehicles
        # share identical params across all wheels for a given axle type)
        rw = ref_wheels[idx] if idx < len(ref_wheels) else ref_wheels[0]
        for key in [
            "StaticMu",
            "SlidingMu",
            "RollingResistanceCoeff",
            "OffroadFriction",
            "MaxWeightKg",
            "WearRate",
        ]:
            cv = cw.get(key)
            rv = rw.get(key)
            if cv is not None and rv is not None:
                if abs(cv - rv) > FLOAT_EPSILON:
                    mismatches.append(
                        {
                            "property": key,
                            "wheel_index": idx,
                            "client": cv,
                            "expected": rv,
                            "deviation": round(abs(cv - rv), 6),
                        }
                    )

    return mismatches
