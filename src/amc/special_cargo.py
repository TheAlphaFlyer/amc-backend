"""Special cargo handler registry.

Certain cargo keys (e.g. "Money", "Ganja") trigger custom side effects beyond
the standard delivery/subsidy flow. This module provides a registry mapping
cargo keys to async handler functions and a single dispatch entry point
called from handle_cargo_arrived() in webhook.py.
"""

import asyncio
import logging
import random

from django.db.models import F
from collections import defaultdict
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

from django.core.cache import cache
from django.utils import timezone

from amc.game_server import announce
from amc.models import Confiscation, CriminalRecord, ServerCargoArrivedLog, Wanted
from amc.player_tags import refresh_player_name
from amc_finance.services import record_treasury_expense

logger = logging.getLogger("amc.special_cargo")

CRIMINAL_LEVEL_STEP = 50_000

# All cargo keys that are considered illicit and trigger a Wanted level
ILLICIT_CARGO_KEYS: set[str] = {
    "Money",
    "Ganja",
    "CocaLeavesPallet",
    "GanjaPallet",
    "Cocaine",
    "MoneyPallet",
}

# Wanted trigger probability constants
WANTED_MIN_CHANCE = 0.05  # 5% floor for small deliveries
WANTED_FULL_CHANCE_AMOUNT = 200_000  # $200k+ = 100% chance


def calculate_criminal_level(laundered_total: int) -> int:
    """Calculate criminal level from cumulative laundered amount.
    Level scales infinitely: floor(total / step) + 1"""
    return (laundered_total // CRIMINAL_LEVEL_STEP) + 1


def should_trigger_wanted(payment: int) -> bool:
    """Determine whether an illicit delivery should trigger a Wanted level.

    Probability scales linearly with payment:
    - Under $10k: 5% chance (floor)
    - $100k: 50% chance
    - $200k+: 100% chance
    """
    chance = max(WANTED_MIN_CHANCE, min(1.0, payment / WANTED_FULL_CHANCE_AMOUNT))
    return random.random() < chance


# Handler signature: (logs, character, http_client, http_client_mod) -> None
SpecialCargoHandler = Callable[
    [list[ServerCargoArrivedLog], Any, Any, Any],
    Coroutine[Any, Any, None],
]


async def _announce_laundered_after_delay(character_guid, http_client, delay=15):
    """Wait for the debounce window, then announce the accumulated total."""
    await asyncio.sleep(delay)
    cache_key = f"money_laundered:{character_guid}"
    data = await cache.aget(cache_key)
    await cache.adelete(cache_key)
    if data and data.get("total", 0) > 0:
        total = data["total"]
        name = data.get("name", "Unknown")
        await announce(
            f"${total:,} has been laundered by {name}",
            http_client,
            color="FFA500",
        )


async def announce_money_secured(character_guid: str, http_client) -> None:
    """Announce that laundered money is safe, if applicable.

    Called from tick_wanted_countdown when a wanted status expires.
    Checks the money_secured cache (accumulated by handle_money_cargo)
    and announces if no confiscation happened.
    """
    cache_key = f"money_secured:{character_guid}"
    data = await cache.aget(cache_key)
    if not data:
        logger.debug("announce_money_secured(%s): no cache data", character_guid)
        return
    await cache.adelete(cache_key)

    # Check if any confiscation happened during the wanted period
    # Use a generous window since wanted is now permanent until cleared
    window_start = timezone.now() - timedelta(days=7)
    was_confiscated = await Confiscation.objects.filter(
        character__guid=character_guid,
        created_at__gte=window_start,
    ).aexists()
    if was_confiscated:
        logger.info(
            "announce_money_secured(%s): suppressed — confiscation found",
            character_guid,
        )
        return

    total = data.get("total", 0)
    name = data.get("name", "Unknown")
    if total > 0 and http_client:
        logger.info(
            "announce_money_secured(%s): announcing $%s safe for %s",
            character_guid,
            total,
            name,
        )
        await announce(
            f"{name}'s ${total:,} is now safe from police",
            http_client,
            color="43B581",
        )
    else:
        logger.debug(
            "announce_money_secured(%s): skipped — total=%s http_client=%s",
            character_guid,
            total,
            bool(http_client),
        )


# ---------------------------------------------------------------------------
# Wanted status management (shared by all illicit cargo)
# ---------------------------------------------------------------------------


async def create_or_refresh_wanted(
    character, http_client_mod, *, amount: int = 0
) -> tuple[Wanted, bool]:
    """Create or refresh a Wanted record for the given character.

    Returns a tuple of (active Wanted instance, created) where *created*
    is True when a brand-new record was inserted.
    Called by cargo handlers for all illicit cargo types.

    Args:
        character: The Character model instance.
        http_client_mod: Mod server HTTP client.
        amount: The delivery payment amount to accumulate on the Wanted record.
    """
    from amc.mod_server import send_system_message

    created = False
    active_wanted = await Wanted.objects.filter(
        character=character,
        expired_at__isnull=True,
    ).afirst()
    if active_wanted:
        active_wanted.wanted_remaining = Wanted.INITIAL_WANTED_LEVEL
        active_wanted.amount = F("amount") + amount
        await active_wanted.asave(update_fields=["wanted_remaining", "amount"])
        await active_wanted.arefresh_from_db(fields=["amount"])
    else:
        active_wanted = await Wanted.objects.acreate(
            character=character,
            wanted_remaining=Wanted.INITIAL_WANTED_LEVEL,
            amount=amount,
        )
        created = True

    await refresh_player_name(character, http_client_mod)
    asyncio.create_task(
        send_system_message(
            http_client_mod,
            "You are wanted. Police are closing in!",
            character_guid=character.guid,
        )
    )
    return active_wanted, created


async def link_delivery_to_wanted(character, wanted, cargo_key, timestamp) -> None:
    """Associate the Delivery record created for this cargo with the Wanted record."""
    from amc.models import Delivery

    delivery = await Delivery.objects.filter(
        character=character, cargo_key=cargo_key, timestamp=timestamp
    ).afirst()
    if delivery and not delivery.wanted_id:
        delivery.wanted = wanted
        await delivery.asave(update_fields=["wanted"])


# ---------------------------------------------------------------------------
# Per-cargo-type handlers
# ---------------------------------------------------------------------------


async def handle_money_cargo(
    logs: list[ServerCargoArrivedLog],
    character,
    http_client,
    http_client_mod,
) -> None:
    """Side effects for Money deliveries.

    - Create or reset criminal record (7 days from now)
    - Refresh player name tag ([C])
    - Debounced laundering announcement (15s window)
    - Record 20% treasury cost
    """
    # --- Accumulate laundered total for criminal level ---
    money_payment = sum(log.payment for log in logs)
    if money_payment > 0:
        character.criminal_laundered_total = (
            F("criminal_laundered_total") + money_payment
        )
        await character.asave(update_fields=["criminal_laundered_total"])
        await character.arefresh_from_db(fields=["criminal_laundered_total"])

    # --- Criminal record ---
    active_record = await CriminalRecord.objects.filter(
        character=character, expires_at__gt=timezone.now()
    ).afirst()
    if active_record:
        active_record.expires_at = timezone.now() + timedelta(days=7)
        await active_record.asave(update_fields=["expires_at"])
    else:
        await CriminalRecord.objects.acreate(
            character=character,
            reason="Money delivery",
            expires_at=timezone.now() + timedelta(days=7),
        )

    # --- Treasury cost ---
    if money_payment > 0:
        laundering_cost = int(money_payment * 0.20)
        if laundering_cost > 0:
            await record_treasury_expense(laundering_cost, "Money Laundering Cost")

    # --- Accumulate "money secured" total (announced when wanted expires) ---
    if money_payment > 0:
        secured_cache_key = f"money_secured:{character.guid}"
        prev_secured = await cache.aget(secured_cache_key)
        secured_total = (
            (prev_secured.get("total", 0) + money_payment)
            if prev_secured
            else money_payment
        )
        secured_data = {
            "total": secured_total,
            "name": character.name,
        }
        # Use a long timeout since wanted is now permanent until cleared
        await cache.aset(
            secured_cache_key,
            secured_data,
            timeout=7 * 24 * 3600,  # 7 days
        )


async def handle_contraband_cargo(
    logs: list[ServerCargoArrivedLog],
    character,
    http_client,
    http_client_mod,
) -> None:
    """Side effects for contraband deliveries (Ganja, Cocaine, etc.).

    - Create or reset criminal record (7 days from now)
    - Refresh player name tag ([C])
    """
    # --- Criminal record ---
    active_record = await CriminalRecord.objects.filter(
        character=character, expires_at__gt=timezone.now()
    ).afirst()
    if active_record:
        active_record.expires_at = timezone.now() + timedelta(days=7)
        await active_record.asave(update_fields=["expires_at"])
    else:
        cargo_key = logs[0].cargo_key if logs else "Contraband"
        await CriminalRecord.objects.acreate(
            character=character,
            reason=f"{cargo_key} delivery",
            expires_at=timezone.now() + timedelta(days=7),
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SPECIAL_CARGO_HANDLERS: dict[str, SpecialCargoHandler] = {
    "Money": handle_money_cargo,
    "Ganja": handle_contraband_cargo,
    "CocaLeavesPallet": handle_contraband_cargo,
    "GanjaPallet": handle_contraband_cargo,
    "Cocaine": handle_contraband_cargo,
    "MoneyPallet": handle_contraband_cargo,
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
        await SPECIAL_CARGO_HANDLERS[key](
            matching_logs, character, http_client, http_client_mod
        )
