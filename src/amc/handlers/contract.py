"""Contract event handlers.

Handles: ServerSignContract, ServerContractCargoDelivered
"""

from __future__ import annotations

import logging
from typing import Any, cast

from django.db.models import F

from amc.handlers import register
from amc.models import ServerSignContractLog

logger = logging.getLogger("amc.webhook.handlers.contract")


@register("ServerSignContract")
async def handle_contract_signed(event, player, character, ctx):
    timestamp = _parse_timestamp(event)
    contract = event["data"].get("Contract")
    if not contract:
        raise ValueError(f"Missing contract data in event: {event}")

    await ServerSignContractLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        guid=event["data"].get("ContractGuid"),
        cargo_key=contract.get("Item", ""),
        amount=contract.get("Amount", 0),
        payment=contract.get("CompletionPayment", {}).get("BaseValue", 0),
        cost=contract.get("Cost", {}).get("BaseValue", 0),
    )
    return 0, 0, 0, 0


@register("ServerContractCargoDelivered")
async def handle_contract_delivered(event, player, character, ctx):
    timestamp = _parse_timestamp(event)
    data = event.get("data", {})
    guid = data.get("ContractGuid")
    if not guid:
        raise ValueError("Missing ContractGuid")

    if "Item" in data:
        log, _ = await ServerSignContractLog.objects.aget_or_create(
            guid=guid,
            defaults={
                "timestamp": timestamp,
                "player": player,
                "cargo_key": data["Item"],
                "amount": data["Amount"],
                "payment": data["CompletionPayment"],
                "cost": data.get("Cost", 0),
                "data": data,
            },
        )
    else:
        try:
            log = await ServerSignContractLog.objects.aget(guid=guid)
        except ServerSignContractLog.DoesNotExist:
            return 0, 0, 0, 0

    log.finished_amount = cast(Any, F("finished_amount") + 1)
    await log.asave(update_fields=["finished_amount"])
    await log.arefresh_from_db()

    payment = 0
    if log.finished_amount == log.amount and not log.delivered:
        payment = log.payment
        log.delivered = True
        await log.asave(update_fields=["delivered"])

    return 0, 0, payment, 0


def _parse_timestamp(event):
    from amc.handlers.utils import parse_event_timestamp

    return parse_event_timestamp(event)
