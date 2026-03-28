"""Special cargo handler registry.

Certain cargo keys (e.g. "Money") trigger custom side effects beyond the
standard delivery/subsidy flow. This module provides a registry mapping
cargo keys to async handler functions and a single dispatch entry point
called from handle_cargo_arrived() in webhook.py.
"""

import asyncio
import logging

from django.db.models import F
from collections import defaultdict
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

from django.core.cache import cache
from django.utils import timezone

from amc.game_server import announce
from amc.models import CriminalRecord, ServerCargoArrivedLog
from amc.player_tags import refresh_player_name
from amc_finance.services import record_treasury_expense

logger = logging.getLogger("amc.special_cargo")

CRIMINAL_LEVEL_STEP = 100_000


def calculate_criminal_level(laundered_total: int) -> int:
    """Calculate criminal level from cumulative laundered amount.
    Level scales infinitely: floor(total / step) + 1"""
    return (laundered_total // CRIMINAL_LEVEL_STEP) + 1

# Handler signature: (logs, character, http_client, http_client_mod) -> None
SpecialCargoHandler = Callable[
    [list[ServerCargoArrivedLog], Any, Any, Any],
    Coroutine[Any, Any, None],
]


async def _announce_laundered_after_delay(character_guid, http_client, delay=30):
    """Wait for the debounce window, then announce the accumulated total."""
    await asyncio.sleep(delay)
    cache_key = f"money_laundered:{character_guid}"
    total = await cache.aget(cache_key, 0)
    await cache.adelete(cache_key)
    if total > 0:
        await announce(
            f"${total:,} has been laundered",
            http_client,
            color="FFA500",
        )


async def handle_money_cargo(
    logs: list[ServerCargoArrivedLog],
    character,
    http_client,
    http_client_mod,
) -> None:
    """Side effects for Money deliveries.

    - Create or reset criminal record (7 days from now)
    - Refresh player name tag ([C])
    - Debounced laundering announcement (30s window)
    - Record 20% treasury cost
    """
    # --- Accumulate laundered total for criminal level ---
    money_payment = sum(log.payment for log in logs)
    if money_payment > 0:
        character.criminal_laundered_total = F("criminal_laundered_total") + money_payment
        await character.asave(update_fields=["criminal_laundered_total"])
        await character.arefresh_from_db(fields=["criminal_laundered_total"])

    # --- Criminal record ---
    active_record = await (
        CriminalRecord.objects.filter(
            character=character, expires_at__gt=timezone.now()
        ).afirst()
    )
    if active_record:
        active_record.expires_at = timezone.now() + timedelta(days=7)
        await active_record.asave(update_fields=["expires_at"])
    else:
        await CriminalRecord.objects.acreate(
            character=character,
            reason="Money delivery",
            expires_at=timezone.now() + timedelta(days=7),
        )

    # --- Player tag refresh ---
    await refresh_player_name(character, http_client_mod)

    # --- Debounced announcement + treasury cost ---
    if money_payment > 0:
        if http_client:
            cache_key = f"money_laundered:{character.guid}"
            prev_total = await cache.aget(cache_key, 0)
            if prev_total == 0:
                await cache.aset(cache_key, money_payment, timeout=60)
                asyncio.create_task(
                    _announce_laundered_after_delay(
                        character.guid, http_client, delay=30
                    )
                )
            else:
                await cache.aset(
                    cache_key, prev_total + money_payment, timeout=60
                )
        laundering_cost = int(money_payment * 0.20)
        if laundering_cost > 0:
            await record_treasury_expense(laundering_cost, "Money Laundering Cost")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SPECIAL_CARGO_HANDLERS: dict[str, SpecialCargoHandler] = {
    "Money": handle_money_cargo,
}


async def run_special_cargo_handlers(
    logs: list[ServerCargoArrivedLog],
    character,
    http_client,
    http_client_mod,
) -> None:
    """Dispatch special-cargo handlers for all cargo keys present in *logs*."""
    if not character:
        return
    logs_by_key: dict[str, list[ServerCargoArrivedLog]] = defaultdict(list)
    for log in logs:
        if log.cargo_key in SPECIAL_CARGO_HANDLERS:
            logs_by_key[log.cargo_key].append(log)
    for key, matching_logs in logs_by_key.items():
        await SPECIAL_CARGO_HANDLERS[key](matching_logs, character, http_client, http_client_mod)

