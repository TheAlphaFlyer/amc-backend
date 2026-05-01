"""ASEAN Tax engine.

Mirrors the structure of `amc.subsidies.get_subsidy_for_cargo` /
`subsidise_player`, but flows funds INTO the treasury instead of out.

Tax is computed on the *raw* base cargo payment (post-fraud-clawback,
pre-subsidy, pre-supply-chain-bonus). Treasury cap is *inverted* so tax
decreases as the treasury fills, supporting the design goal of keeping
net flow positive into the treasury while protecting players from
runaway taxation when the government is already rich.
"""

from __future__ import annotations

import logging

from django.contrib.gis.geos import Point
from django.db.models import F, Q

from amc import config
from amc.models import TaxRule
from amc.mod_server import transfer_money
from amc_finance.services import record_treasury_tax_collection

logger = logging.getLogger(__name__)


async def get_tax_for_cargo(cargo, treasury_balance=None):
    """Find the highest-priority TaxRule that matches this cargo log.

    Returns (tax_amount, tax_factor, best_rule).

    Mirrors `amc.subsidies.get_subsidy_for_cargo` matching logic exactly.
    Treasury cap is inverted: high treasury → tax scales down to 0.
    """
    rules = TaxRule.objects.filter(active=True).order_by("-priority")

    # 1. Cargo Key Filter
    rules = rules.filter(Q(cargos__isnull=True) | Q(cargos__key=cargo.cargo_key))

    # 2. Source Filter
    if cargo.sender_point and cargo.sender_point.coord:
        rules = rules.filter(
            Q(source_areas__isnull=True, source_delivery_points__isnull=True)
            | Q(source_areas__polygon__contains=cargo.sender_point.coord)
            | Q(source_delivery_points__coord__dwithin=(cargo.sender_point.coord, 1.0))
        )
    else:
        rules = rules.filter(
            source_areas__isnull=True, source_delivery_points__isnull=True
        )

    # 3. Destination Filter
    destination_coord = None
    if cargo.destination_point and cargo.destination_point.coord:
        destination_coord = cargo.destination_point.coord
    elif destination_location := cargo.data.get("Net_DestinationLocation"):
        destination_coord = Point(
            destination_location["X"],
            destination_location["Y"],
            destination_location["Z"],
            srid=3857,
        )

    if destination_coord:
        rules = rules.filter(
            Q(destination_areas__isnull=True, destination_delivery_points__isnull=True)
            | Q(destination_areas__polygon__contains=destination_coord)
            | Q(destination_delivery_points__coord__dwithin=(destination_coord, 1.0))
        )
    else:
        rules = rules.filter(
            destination_areas__isnull=True, destination_delivery_points__isnull=True
        )

    # 4. On Time Check
    is_on_time = cargo.data.get("Net_TimeLeftSeconds", 0) > 0
    if not is_on_time:
        rules = rules.exclude(requires_on_time=True)

    best_rule = await rules.distinct().afirst()

    tax_factor = 0.0
    tax_amount = 0
    if best_rule:
        factor = float(best_rule.tax_value)
        if best_rule.tax_type == TaxRule.TaxType.PERCENTAGE:
            tax_factor = factor
            tax_amount = int(int(cargo.payment) * tax_factor)
        else:
            tax_amount = int(factor)
            if cargo.payment > 0:
                tax_factor = tax_amount / cargo.payment

    # Inverted, non-linear treasury cap.
    # Tax scale = (1 - t) ** TAX_CURVE_EXPONENT, where t is the normalised position of the treasury between FLOOR (t=0, full tax) and CEILING (t=1, no tax). With EXPONENT < 1 the curve is convex: tax stays HIGH
    # For most of the range and only collapses sharply as the treasury approaches the ceiling.Poor treasury = more tax. Rich treasury = less tax.
    if treasury_balance is not None and tax_amount > 0:
        floor = config.TREASURY_SUBSIDY_FLOOR
        ceiling = config.TREASURY_SUBSIDY_CEILING
        bal = float(treasury_balance)
        if bal >= ceiling:
            scale = 0.0
        elif bal <= floor:
            scale = 1.0
        else:
            t = (bal - floor) / (ceiling - floor)
            exponent = max(0.0001, float(config.TAX_CURVE_EXPONENT))
            scale = (1.0 - t) ** exponent
        tax_amount = int(tax_amount * scale)
        if cargo.payment > 0:
            tax_factor = tax_amount / cargo.payment

    return tax_amount, tax_factor, best_rule


async def tax_player(amount, character, session, message="ASEAN Tax"):
    """Deduct `amount` from the player's in-game wallet and credit the
    Treasury Fund via a proper double-entry ledger entry.

    Safe for `amount <= 0` (no-op). Safe to call without ledger context
    (the ledger entry handles its own transaction).
    """
    if amount <= 0 or not character or not session:
        return
    try:
        await transfer_money(
            session,
            int(-amount),
            message,
            str(character.player.unique_id),
        )
    except Exception as e:
        # Player wallet may not have the funds. Don't credit the treasury
        # if we couldn't actually take the money from the player.
        logger.warning(
            "Failed to deduct ASEAN Tax from %s (%s): %s",
            character.name,
            character.player.unique_id,
            e,
        )
        return
    try:
        await record_treasury_tax_collection(amount, character, message)
    except Exception:
        # Wallet was already debited; log but do not raise so we don't
        # break delivery processing.
        logger.exception(
            "ASEAN Tax wallet deducted from %s but treasury credit failed",
            character.name,
        )


async def record_tax_rule_collection(rule, amount):
    """Increment a TaxRule's lifetime collected counter."""
    if rule and amount > 0:
        await TaxRule.objects.filter(pk=rule.pk).aupdate(
            collected=F("collected") + amount
        )
