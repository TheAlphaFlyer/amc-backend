"""Passenger event handler.

Handles: ServerPassengerArrived
"""

from __future__ import annotations

import asyncio
import logging

from amc.handlers import register
from amc.models import ServerPassengerArrivedLog
from amc.mod_server import show_popup, transfer_money
from amc.fraud_detection import validate_passenger_payment
from amc.subsidies import get_passenger_subsidy

logger = logging.getLogger("amc.webhook.handlers.passenger")


@register("ServerPassengerArrived")
async def handle_passenger_arrived(event, player, character, ctx):
    timestamp = _parse_timestamp(event)
    passenger = event["data"].get("Passenger")
    passenger_data = passenger or {}
    base_payment = passenger_data.get("Net_Payment", 0)
    flag = passenger_data.get("Net_PassengerFlags", 0)

    if base_payment < 0:
        raise ValueError(f"Negative payment for passenger: {passenger_data}")

    # Exploit detection: passengers picked up on a modded server have
    # Net_StartLocation at the world origin (0,0,0).
    start_loc = passenger_data.get("Net_StartLocation", {})
    is_exploit = (
        start_loc.get("X", 1) == 0
        and start_loc.get("Y", 1) == 0
        and start_loc.get("Z", 1) == 0
    )

    log = ServerPassengerArrivedLog(
        timestamp=timestamp,
        player=player,
        passenger_type=passenger_data.get("Net_PassengerType", ""),
        distance=passenger_data.get("Net_Distance"),
        payment=base_payment,
        arrived=passenger_data.get("Net_bArrived", True),
        comfort=bool(flag & 1),
        urgent=bool(flag & 2),
        limo=bool(flag & 4),
        offroad=bool(flag & 8),
        comfort_rating=passenger_data.get("Net_LCComfortSatisfaction"),
        urgent_rating=passenger_data.get("Net_TimeLimitPoint"),
        data=passenger_data,
    )

    if is_exploit:
        log.payment = 0
        await log.asave()
        if base_payment > 0 and character and ctx.http_client_mod:
            await transfer_money(
                ctx.http_client_mod,
                int(-base_payment),
                "Invalid Passenger",
                str(character.player.unique_id),
            )
            asyncio.create_task(
                show_popup(
                    ctx.http_client_mod,
                    "Passenger delivery rejected: invalid origin.",
                    character_guid=character.guid,
                    player_id=str(character.player.unique_id),
                )
            )
        logger.warning(
            "Exploit detected: passenger with zero start location for player %s (payment=%s)",
            player.unique_id,
            base_payment,
        )
        return 0, 0, 0, 0

    # Fraud detection: validate payment against type ceiling
    passenger_type_int = int(log.passenger_type) if log.passenger_type else 0
    fraud_excess = validate_passenger_payment(passenger_type_int, base_payment)
    if fraud_excess > 0:
        original = base_payment
        base_payment = max(0, base_payment - fraud_excess)
        log.payment = base_payment
        logger.warning(
            "Fraud detected (passenger): player=%s type=%s original=%d reduced=%d excess=%d",
            player.unique_id,
            passenger_type_int,
            original,
            base_payment,
            fraud_excess,
        )

    # Taxi bonuses
    if log.passenger_type == ServerPassengerArrivedLog.PassengerType.Taxi:
        if log.comfort:
            bonus_per_star = 0.2
            if log.limo:
                bonus_per_star = bonus_per_star * 1.3
            log.payment += base_payment * log.comfort_rating * bonus_per_star
        if log.urgent:
            log.payment += base_payment * log.urgent_rating * 0.3

    # Ambulance bonus
    if log.passenger_type == ServerPassengerArrivedLog.PassengerType.Ambulance:
        radius_ratio = passenger_data.get("Net_SearchAndRescueRadiusRatio")
        if radius_ratio is not None:
            bonus_multiplier = 1 - radius_ratio
            log.payment += int(base_payment * bonus_multiplier)

    await log.asave()
    subsidy = get_passenger_subsidy(log)
    return log.payment, subsidy, 0, 0


def _parse_timestamp(event):
    from django.utils import timezone as _tz
    current_tz = _tz.get_current_timezone()
    return _tz.datetime.fromtimestamp(event["timestamp"], tz=current_tz)
