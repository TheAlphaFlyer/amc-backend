"""Anti-cheat reporting API endpoint.

PoC phase: unauthenticated, observe-only. Receives physics integrity reports
from the client mod, logs them, and compares against hardcoded reference values
if available. No enforcement actions are taken.

Route: POST /api/ac/report
"""

import logging
from ninja import Router, Schema

from amc.anticheat import find_reference, compare_values

logger = logging.getLogger("amc.anticheat")

router = Router(tags=["anticheat"])


class PhysicsReportSchema(Schema):
    guid: str
    timestamp: int
    mod_version: str = ""
    values: dict


@router.post("/report")
async def report_integrity(request, payload: PhysicsReportSchema):
    """Receive physics integrity report from client mod.

    Logs all reports. Compares against VEHICLE_REFERENCE if the vehicle class
    is known. Returns {"status": "ok"} always — PoC is observe-only.
    """
    vehicle_class = payload.values.get("VehicleClass", "unknown")
    num_wheels = len(payload.values.get("Wheels", []))
    guid_short = payload.guid[:8] if len(payload.guid) >= 8 else payload.guid

    logger.info(
        "AC report: guid=%s vehicle=%s air_drag=%s brake_mult=%s wheels=%d mod=%s",
        guid_short,
        vehicle_class,
        payload.values.get("AirDragCoeff"),
        payload.values.get("BrakeTorqueMultiplier"),
        num_wheels,
        payload.mod_version,
    )

    # Log wheel 0 values for easy reference baseline building
    wheels = payload.values.get("Wheels", [])
    if wheels:
        w0 = wheels[0]
        logger.info(
            "AC wheel[0]: StaticMu=%s SlidingMu=%s RollingResist=%s Offroad=%s MaxKg=%s WearRate=%s",
            w0.get("StaticMu"),
            w0.get("SlidingMu"),
            w0.get("RollingResistanceCoeff"),
            w0.get("OffroadFriction"),
            w0.get("MaxWeightKg"),
            w0.get("WearRate"),
        )

    # Compare against reference if known
    reference = find_reference(vehicle_class)
    if reference:
        mismatches = compare_values(payload.values, reference)
        if mismatches:
            logger.warning(
                "AC MISMATCH: guid=%s vehicle=%s count=%d mismatches=%s",
                guid_short,
                vehicle_class,
                len(mismatches),
                mismatches,
            )
            return {"status": "flagged", "mismatches": len(mismatches)}
        else:
            logger.info("AC OK: guid=%s vehicle=%s", guid_short, vehicle_class)

    # Always return 200 OK — PoC never rejects
    return {"status": "ok"}


class PakScanSchema(Schema):
    timestamp: int
    mod_version: str = ""
    paks: list[dict]


@router.post("/paks")
async def report_paks(request, payload: PakScanSchema):
    """Receive pak file scan from client mod.

    Logs all .pak files found in MotorTown/Content/Paks at mod load time.
    PoC: observe-only. No enforcement.
    """
    pak_names = [p.get("name", "?") for p in payload.paks]

    logger.info(
        "AC paks: count=%d mod=%s paks=%s",
        len(payload.paks),
        payload.mod_version,
        pak_names,
    )

    return {"status": "ok"}
