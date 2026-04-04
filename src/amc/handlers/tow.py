"""Tow request event handler.

Handles: ServerTowRequestArrived
"""

from __future__ import annotations

import logging

from amc.handlers import register
from amc.models import ServerTowRequestArrivedLog
from amc.fraud_detection import validate_tow_payment

logger = logging.getLogger("amc.webhook.handlers.tow")


@register("ServerTowRequestArrived")
async def handle_tow_request(event, player, character, ctx):
    timestamp = _parse_timestamp(event)
    tow_request = event["data"].get("TowRequest")
    tow_data = tow_request or {}
    payment = tow_data.get("Net_Payment", 0)

    # Body damage bonus
    body_damage = tow_data.get("BodyDamage", 1.0)
    payment += int(payment * 0.55 * (1 - body_damage))

    # Fraud detection
    fraud_excess = validate_tow_payment(payment)
    if fraud_excess > 0:
        logger.warning(
            "Fraud detected (tow): player=%s original=%d reduced=%d excess=%d",
            player.unique_id,
            payment,
            max(0, payment - fraud_excess),
            fraud_excess,
        )
        payment = max(0, payment - fraud_excess)

    await ServerTowRequestArrivedLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        payment=payment,
        data=tow_data,
    )

    match tow_data.get("Net_TowRequestFlags", 0):
        case 1:  # Flipped
            subsidy = 2_000 + payment * 1.0
        case _:
            subsidy = 2_000 + payment * 0.5

    return payment, subsidy, 0, 0


def _parse_timestamp(event):
    from django.utils import timezone as _tz
    current_tz = _tz.get_current_timezone()
    return _tz.datetime.fromtimestamp(event["timestamp"], tz=current_tz)
