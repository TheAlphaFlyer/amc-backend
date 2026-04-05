"""Cargo event handlers.

Handles: ServerCargoArrived, ServerCargoDumped
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from operator import attrgetter

from django.contrib.gis.geos import Point
from django.db.models import F

from amc.handlers import register
from amc.models import (
    Delivery,
    DeliveryJob,
    DeliveryPoint,
    PoliceSession,
    ServerCargoArrivedLog,
    SubsidyRule,
)
from amc.special_cargo import (
    ILLICIT_CARGO_KEYS,
    create_or_refresh_wanted,
    link_delivery_to_wanted,
)
from amc.mod_detection import detect_custom_parts, POLICE_DUTY_WHITELIST
from amc.mod_server import (
    list_player_vehicles,
    show_popup,
    transfer_money,
)
from amc.fraud_detection import validate_cargo_payment
from amc.pipeline.discord import post_discord_delivery_embed
from amc.pipeline.delivery import atomic_process_delivery
from amc.subsidies import get_subsidy_for_cargo, subsidise_player
from amc_finance.services import record_ministry_subsidy_spend
from asgiref.sync import sync_to_async

logger = logging.getLogger("amc.webhook.handlers.cargo")


# ---------------------------------------------------------------------------
# ServerCargoDumped
# ---------------------------------------------------------------------------


@register("ServerCargoDumped")
async def handle_cargo_dumped(event, player, character, ctx):
    cargo = event["data"]["Cargo"]
    if cargo["Net_Payment"] < 0:
        raise ValueError(f"Negative payment for dumped cargo: {cargo}")

    cargo_data = cargo or {}
    log = await ServerCargoArrivedLog.objects.acreate(
        timestamp=_parse_timestamp(event),
        player=player,
        cargo_key=cargo_data.get("Net_CargoKey", ""),
        payment=cargo_data.get("Net_Payment", 0),
        weight=cargo_data.get("Net_Weight", 0),
        damage=cargo_data.get("Net_Damage", 0),
        data=event.get("data"),
    )
    subsidy, _, rule = await get_subsidy_for_cargo(log)
    if rule and subsidy > 0:
        await SubsidyRule.objects.filter(pk=rule.pk).aupdate(spent=F("spent") + subsidy)
    return log.payment, subsidy, 0, 0


# ---------------------------------------------------------------------------
# ServerCargoArrived
# ---------------------------------------------------------------------------


@register("ServerCargoArrived")
async def handle_cargo_arrived(event, player, character, ctx):
    from amc.cargo import get_cargo_bonus
    from amc.special_cargo import run_special_cargo_handlers
    from amc.supply_chain import check_and_record_contribution
    from amc.police import (
        get_active_police_count,
        SECURITY_BONUS_RATE,
        SECURITY_BONUS_MAX,
    )

    timestamp = _parse_timestamp(event)

    # --- 1. Parse cargos, detect clawback for zero-delivery ---
    valid_cargos, clawback = _parse_cargos(event)

    # --- 2. Build logs (parallel) ---
    logs = await asyncio.gather(
        *[
            process_cargo_log(cargo, player, character, timestamp)
            for cargo in valid_cargos
        ]
    )

    # --- 3. Apply game-level bonuses (damage bonus etc.) ---
    for log in logs:
        log.payment += get_cargo_bonus(log.cargo_key, log.payment, log.damage or 0)

    # --- 4. Fraud detection ---
    for log in logs:
        excess = await validate_cargo_payment(
            cargo_key=log.cargo_key,
            payment=log.payment,
            quantity=1,
            sender_point=log.sender_point,
            destination_point=log.destination_point,
        )
        if excess > 0:
            log.payment = max(0, log.payment - excess)
            logger.warning(
                "Fraud detected (cargo): player=%s cargo=%s original=%d reduced=%d excess=%d",
                character.player.unique_id,
                log.cargo_key,
                log.payment + excess,
                log.payment,
                excess,
            )

    await ServerCargoArrivedLog.objects.abulk_create(logs)

    # --- 5. Special cargo side effects (e.g. Money -> criminal record) ---
    await run_special_cargo_handlers(
        logs, character, ctx.http_client, ctx.http_client_mod
    )

    # --- 6. Per-cargo-group: subsidy, delivery, job, supply chain ---
    total_subsidy = 0
    total_payment = sum(log.payment for log in logs)
    vehicle_key = character.last_vehicle_key or "" if character else ""

    key_by_cargo = attrgetter("cargo_key")
    logs.sort(key=key_by_cargo)

    for cargo_key, group in itertools.groupby(logs, key=key_by_cargo):
        group_list = list(group)
        quantity = len(group_list)
        payment = group_list[0].payment
        delivery_source = group_list[0].sender_point
        delivery_destination = group_list[0].destination_point

        cargo_subsidy_res = await get_subsidy_for_cargo(
            group_list[0], treasury_balance=ctx.treasury_balance
        )
        cargo_subsidy = cargo_subsidy_res[0] * quantity
        rule = cargo_subsidy_res[2]
        if rule and cargo_subsidy > 0:
            await SubsidyRule.objects.filter(pk=rule.pk).aupdate(
                spent=F("spent") + cargo_subsidy
            )
            if ctx.active_term:
                await record_ministry_subsidy_spend(cargo_subsidy, ctx.active_term.id)

        cargo_name = group_list[0].get_cargo_key_display()

        # Modded vehicle penalty for illicit cargo
        if cargo_key in ILLICIT_CARGO_KEYS and ctx.http_client_mod:
            await _apply_modded_vehicle_penalty(
                character, payment, quantity, ctx.http_client_mod
            )

        # Find matching delivery job
        job = await (
            DeliveryJob.objects.filter_active().filter_by_delivery(
                delivery_source, delivery_destination, cargo_key
            )
        ).afirst()
        if job is not None and job.rp_mode and not ctx.is_rp_mode:
            job = None

        # Build delivery data
        delivery_data = _build_delivery_data(
            timestamp,
            character,
            cargo_key,
            quantity,
            payment,
            cargo_subsidy,
            delivery_source,
            delivery_destination,
            ctx.is_rp_mode,
        )

        job_id = job.id if job and not ctx.used_shortcut else None
        job = await sync_to_async(atomic_process_delivery)(
            job_id, quantity, delivery_data
        )

        # Job fulfillment
        if job and job.quantity_fulfilled >= job.quantity_requested:
            from amc.jobs import on_delivery_job_fulfilled

            rows_updated = await DeliveryJob.objects.filter(
                pk=job.id, fulfilled_at__isnull=True
            ).aupdate(fulfilled_at=timestamp)
            if rows_updated > 0:
                await job.arefresh_from_db()
                await on_delivery_job_fulfilled(job, ctx.http_client)

        # Supply chain contribution
        delivery_obj = await Delivery.objects.filter(
            character=character, cargo_key=cargo_key, timestamp=timestamp
        ).afirst()
        sc_bonus = await check_and_record_contribution(
            delivery=delivery_obj,
            character=character,
            cargo_key=cargo_key,
            quantity=quantity,
            destination_point=delivery_destination,
            source_point=delivery_source,
        )
        delivery_subsidy = delivery_data["subsidy"] + sc_bonus

        # Security bonus for Money + Wanted status
        security_bonus = 0
        if cargo_key == "Money":
            police_count = await get_active_police_count()
            bonus_rate = min(police_count * SECURITY_BONUS_RATE, SECURITY_BONUS_MAX)
            security_bonus = int(payment * quantity * bonus_rate)
            if security_bonus > 0 and character:
                await subsidise_player(
                    security_bonus,
                    character,
                    ctx.http_client_mod,
                    message="Risk Premium",
                )

        # Wanted status for all illicit cargo
        if cargo_key in ILLICIT_CARGO_KEYS and character:
            wanted = await create_or_refresh_wanted(
                character, ctx.http_client_mod
            )
            # Link delivery to the wanted record
            if delivery_obj:
                await link_delivery_to_wanted(
                    character, wanted, cargo_key, timestamp
                )

        # Discord notification
        if ctx.discord_client:
            asyncio.create_task(
                post_discord_delivery_embed(
                    ctx.discord_client,
                    character,
                    cargo_name,
                    quantity,
                    delivery_source,
                    delivery_destination,
                    payment * quantity,
                    delivery_subsidy + security_bonus,
                    vehicle_key,
                    job=job,
                    delivery_id=delivery_obj.id if delivery_obj else None,
                )
            )

        total_subsidy += delivery_subsidy

    return total_payment, total_subsidy, 0, clawback


# ---------------------------------------------------------------------------
# Sub-helpers for handle_cargo_arrived
# ---------------------------------------------------------------------------


def _parse_cargos(event):
    """Extract valid cargos and compute clawback for zero-delivery items."""
    valid_cargos = []
    clawback = 0
    for cargo in event["data"]["Cargos"]:
        if cargo["Net_Payment"] < 0:
            raise ValueError(f"Negative payment for cargo: {cargo}")
        if "Net_DeliveryId" in cargo and cargo["Net_DeliveryId"] == 0:
            clawback += cargo["Net_Payment"]
        valid_cargos.append(cargo)
    return valid_cargos, clawback


async def _apply_modded_vehicle_penalty(character, payment, quantity, http_client_mod):
    """Check for modded parts on Money delivery; confiscate profits if found."""
    try:
        vehicles = await list_player_vehicles(
            http_client_mod, str(character.player.unique_id), active=True, complete=True
        )
        if not vehicles:
            return
        main_vehicle = next(
            (
                v
                for v in vehicles.values()
                if v.get("isLastVehicle") and v.get("index", -1) == 0
            ),
            None,
        )
        if not main_vehicle:
            return

        whitelist = None
        is_on_duty = await PoliceSession.objects.filter(
            character=character, ended_at__isnull=True
        ).aexists()
        if is_on_duty:
            whitelist = POLICE_DUTY_WHITELIST
        custom_parts = detect_custom_parts(
            main_vehicle.get("parts", []), whitelist=whitelist
        )
        if custom_parts:
            penalty = payment * quantity
            await transfer_money(
                http_client_mod,
                int(-penalty),
                "Modded Vehicle Penalty",
                str(character.player.unique_id),
            )
            asyncio.create_task(
                show_popup(
                    http_client_mod,
                    "Your criminal profits were zeroed out for using a modified vehicle.",
                    character_guid=character.guid,
                    player_id=str(character.player.unique_id),
                )
            )
    except Exception as e:
        logger.warning(f"Failed to check custom parts for money delivery penalty: {e}")


def _build_delivery_data(
    timestamp,
    character,
    cargo_key,
    quantity,
    payment,
    subsidy,
    delivery_source,
    delivery_destination,
    is_rp_mode,
):
    """Construct the delivery_data dict used for Delivery creation."""
    delivery_data = {
        "timestamp": timestamp,
        "character": character,
        "cargo_key": cargo_key,
        "quantity": quantity,
        "payment": payment * quantity,
        "subsidy": subsidy,
        "sender_point": delivery_source,
        "destination_point": delivery_destination,
        "rp_mode": is_rp_mode,
    }
    if is_rp_mode:
        delivery_data["subsidy"] = int((subsidy * 1.5) + (payment * quantity * 0.5))
    return delivery_data


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def process_cargo_log(cargo, player, character, timestamp):
    """Create a ServerCargoArrivedLog from raw cargo event data."""
    sender_coord_raw = cargo["Net_SenderAbsoluteLocation"]
    sender_coord = Point(
        sender_coord_raw["X"],
        sender_coord_raw["Y"],
        sender_coord_raw["Z"],
    ).buffer(1)
    destination_coord_raw = cargo["Net_DestinationLocation"]
    destination_coord = Point(
        destination_coord_raw["X"],
        destination_coord_raw["Y"],
        destination_coord_raw["Z"],
    ).buffer(1)
    sender = await DeliveryPoint.objects.filter(coord__coveredby=sender_coord).afirst()
    destination = await DeliveryPoint.objects.filter(
        coord__coveredby=destination_coord
    ).afirst()
    return ServerCargoArrivedLog(
        timestamp=timestamp,
        player=player,
        character=character,
        cargo_key=cargo["Net_CargoKey"],
        payment=cargo["Net_Payment"],
        weight=cargo.get("Net_Weight", 0),
        damage=cargo["Net_Damage"],
        sender_point=sender,
        destination_point=destination,
        data=cargo,
    )


def _parse_timestamp(event):
    from amc.handlers.utils import parse_event_timestamp

    return parse_event_timestamp(event)
