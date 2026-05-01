import asyncio
import logging
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.db.models import Q
from django.utils.translation import gettext as _

from amc import config as amc_config
from amc.mod_server import (
    get_player_last_vehicle_parts,
    show_popup,
    transfer_money,
)
from amc.mod_detection import POLICE_DUTY_WHITELIST, detect_custom_parts
from amc.models import (
    PoliceSession,
    ServerPassengerArrivedLog,
    SubsidyRule,
    TaxRule,
)
from amc_finance.services import (
    get_treasury_fund_balance,
    register_player_deposit,
    send_fund_to_player_wallet,
)

logger = logging.getLogger(__name__)


def _format_reward(reward_type, reward_value):
    """Format a SubsidyRule/TaxRule reward field for player display."""
    if reward_type == SubsidyRule.RewardType.PERCENTAGE:
        return f"{int(float(reward_value) * 100)}%"
    return f"{int(reward_value):,} coins"


def _rule_rate_fields(rule):
    """Return (type, value) for either a SubsidyRule (reward_*) or a TaxRule (tax_*). Both share the same PERCENTAGE/FLAT semantics"""
    if hasattr(rule, "tax_value"):
        return rule.tax_type, rule.tax_value
    return rule.reward_type, rule.reward_value


def _rule_sort_key(rule):
    """Sort rules by *display magnitude*, highest first.

    Percentages are converted into a comparable scalar by treating
    `reward_value` as a fraction (so 1.5 = 150% comes before 0.10 = 10%).
    Flat amounts use their raw value. We can't mix the two units perfectly
    so we keep flat amounts in their own bucket but still ordered by size.
    """
    _, value = _rule_rate_fields(rule)
    return -float(value)


async def _format_rule_block(rule):
    """Render one rule as a block of lines (cargo + reward + areas)."""
    rtype, rvalue = _rule_rate_fields(rule)
    reward_str = _format_reward(rtype, rvalue)
    if rule.requires_on_time:
        reward_str += " (Must be on time)"

    cargos = [c async for c in rule.cargos.all()]
    if cargos:
        cargo_str = ", ".join(c.label for c in cargos)
    else:
        cargo_str = "Any Cargo"

    lines = [f"<Bold>{cargo_str}</> - <Money>{reward_str}</>"]

    source_areas = [a async for a in rule.source_areas.all()]
    source_points = [p async for p in rule.source_delivery_points.all()]
    all_sources = source_areas + source_points
    if all_sources:
        names = ", ".join(obj.name for obj in all_sources)
        lines.append(f"<Secondary>From: {names}</>")

    dest_areas = [a async for a in rule.destination_areas.all()]
    dest_points = [p async for p in rule.destination_delivery_points.all()]
    all_dests = dest_areas + dest_points
    if all_dests:
        names = ", ".join(obj.name for obj in all_dests)
        lines.append(f"<Secondary>To: {names}</>")

    return "\n".join(lines)


async def get_subsidies_text():
    """Player-facing text for `/subsidies`.

    Subsidies first, sorted by reward magnitude (highest -> lowest) with a
    blank line between each rule. ASEAN Tax rules listed underneath in a
    separate section so players can see exactly what is being collected.
    """
    parts = [_("<Title>ASEAN Server Subsidies</>")]

    subsidies = [
        rule
        async for rule in SubsidyRule.objects.filter(active=True).prefetch_related(
            "cargos",
            "source_areas",
            "source_delivery_points",
            "destination_areas",
            "destination_delivery_points",
        )
    ]
    subsidies.sort(key=_rule_sort_key)

    if subsidies:
        blocks = [await _format_rule_block(r) for r in subsidies]
        parts.append("\n\n".join(blocks))
    else:
        parts.append(_("<Secondary>No active subsidies.</>"))

    # Tow Request Subsidies (legacy, hard-coded)
    parts.append(
        _(
            "<Title>Wrecker Subsidies</>\n"
            "<Bold>Flipped Vehicle</> - <Money>2,000</> + <Money>100%</> of payment\n"
            "<Bold>Other Tow Requests</> - <Money>2,000</> + <Money>50%</> of payment\n"
            "\n"
            "<Title>Body Damage Bonus</>\n"
            "<Secondary>Tow requests include a body damage bonus up to <Money>55%</> of base payment.</>\n"
            "<Secondary>Keep the towed vehicle's body intact for maximum bonus!</>"
        )
    )

    # ASEAN Tax section
    taxes = [
        rule
        async for rule in TaxRule.objects.filter(active=True).prefetch_related(
            "cargos",
            "source_areas",
            "source_delivery_points",
            "destination_areas",
            "destination_delivery_points",
        )
    ]
    taxes.sort(key=_rule_sort_key)
    if taxes:
        parts.append(_("<Title>ASEAN Server Taxes</>"))
        parts.append(
            _(
                "<Secondary>The following taxes are deducted from the base cargo "
                "payment and credited to the Treasury. Taxes scale down as the "
                "Treasury grows.</>"
            )
        )
        blocks = [await _format_rule_block(r) for r in taxes]
        parts.append("\n\n".join(blocks))

    return "\n\n".join(parts)


SUBSIDIES_TEXT = "Use await get_subsidies_text()"


# ---------------------------------------------------------------------------
# Player savings (auto-deposit a portion of profit into bank)
# ---------------------------------------------------------------------------


cargo_names = {
    "MeatBox": "Meat Box",
    "BottlePallete": "Water Bottle Pallete",
    "Burger_01_Signature": "Signature Burger",
    "Pizza_01_Premium": "Premium Pizza",
    "GiftBox_01": "Gift Box",
    "LiveFish_01": "Live Fish",
    "Log_Oak_12ft": "12ft Oak Log",
}


DEFAULT_SAVING_RATE = 1


async def set_aside_player_savings(character, payment, session):
    try:
        if character.saving_rate is not None:
            saving_rate = character.saving_rate
        else:
            saving_rate = Decimal(DEFAULT_SAVING_RATE)
        if saving_rate == Decimal(0):
            return 0

        saving = Decimal(saving_rate) * Decimal(payment)
        if saving > 0:
            # Phrased as a transfer rather than a payment so the in-game chat
            # entry reads as a routing of funds, not a charge.
            # NOTE: the leading "-$" in chat is rendered by the game server
            # itself based on the sign of the amount and cannot be suppressed
            # without a mod-server change.
            message = "Auto-Save to Bank"
            if character.saving_rate is None:
                message = "Auto-Save to Bank (use /bank, /set_saving_rate)"

            await transfer_money(
                session,
                int(-saving),
                message,
                str(character.player.unique_id),
            )
            await register_player_deposit(
                saving, character, character.player, "Earnings Deposit"
            )
            return int(saving)
    except Exception as e:
        asyncio.create_task(
            show_popup(
                session,
                f"Failed to deposit earnings:\n{e}",
                character_guid=character.guid,
            )
        )
        raise e


# ---------------------------------------------------------------------------
# Subsidy lookup
# ---------------------------------------------------------------------------


async def get_subsidy_for_cargos(cargos, treasury_balance=None):
    total = 0
    for cargo in cargos:
        result = await get_subsidy_for_cargo(cargo, treasury_balance)
        total += result[0]
    return total


async def get_subsidy_for_cargo(cargo, treasury_balance=None):
    rules = SubsidyRule.objects.filter(active=True).order_by("-priority")

    # 1. Cargo Key Filter
    rules = rules.filter(Q(cargos__isnull=True) | Q(cargos__key=cargo.cargo_key))

    # 2. Source Area Filter
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

    # 3. Destination Area Filter
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

    subsidy_factor = 0.0
    subsidy_amount = 0

    if best_rule:
        factor = float(best_rule.reward_value)
        if best_rule.reward_type == SubsidyRule.RewardType.PERCENTAGE:
            subsidy_factor = factor
            subsidy_amount = int(int(cargo.payment) * subsidy_factor)
        else:
            subsidy_amount = int(factor)
            if cargo.payment > 0:
                subsidy_factor = subsidy_amount / cargo.payment

    # Treasury cap: subsidies scale down when treasury is low (preserve floor;
    # below FLOOR -> zero, above CEILING -> full, linear in between).
    if treasury_balance is not None and subsidy_amount > 0:
        floor = amc_config.TREASURY_SUBSIDY_FLOOR
        ceiling = amc_config.TREASURY_SUBSIDY_CEILING
        bal = float(treasury_balance)
        if bal <= floor:
            scale = 0.0
        elif bal >= ceiling:
            scale = 1.0
        else:
            scale = (bal - floor) / (ceiling - floor)
        subsidy_amount = int(subsidy_amount * scale)
        subsidy_amount = min(subsidy_amount, int(treasury_balance))
        if cargo.payment > 0:
            subsidy_factor = subsidy_amount / cargo.payment

    return subsidy_amount, subsidy_factor, best_rule


def get_passenger_subsidy(passenger):
    match passenger.passenger_type:
        case ServerPassengerArrivedLog.PassengerType.Taxi:
            return 2_000 + passenger.payment * 0.5
        case ServerPassengerArrivedLog.PassengerType.Ambulance:
            return 2_000 + passenger.payment * 0.5
        case _:
            return 0


# ---------------------------------------------------------------------------
# Modded vehicle detection (delivery-time, no popup, no zero-out)
# ---------------------------------------------------------------------------


async def is_player_in_modded_vehicle(character, http_client_mod) -> bool:
    """Return True if the character is currently in a vehicle whose parts
    fail the same custom-parts check used to drive the [M] tag.

    Does NOT depend on the [M] tag being present yet (the tag is updated
    asynchronously). Returns False on any error so a transient mod-server
    hiccup never punishes a player.
    """
    if not http_client_mod or not character:
        return False
    try:
        parts_data = await get_player_last_vehicle_parts(
            http_client_mod, str(character.guid), complete=False
        )
        whitelist = None
        is_on_duty = await PoliceSession.objects.filter(
            character=character, ended_at__isnull=True
        ).aexists()
        if is_on_duty:
            whitelist = POLICE_DUTY_WHITELIST
        custom_parts = detect_custom_parts(
            parts_data.get("parts", []), whitelist=whitelist
        )
        return bool(custom_parts)
    except Exception as e:
        logger.warning("is_player_in_modded_vehicle check failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Subsidy player-side cuts (gov skip, rich players, modded vehicle)
# ---------------------------------------------------------------------------


async def apply_subsidy_player_cuts(subsidy, character, http_client_mod=None):
    """Apply player-specific reductions to a positive subsidy amount.

    - Gov employees: skipped
    - Rich players: subsidy scales down with curve, until reaches ceiling
    - Modded current vehicle: scaled by MODDED_SUBSIDY_MULTIPLIER.

    Cuts compose multiplicatively. Returns the adjusted integer amount.
    """
    if subsidy <= 0 or character is None:
        return 0

    if getattr(character, "is_gov_employee", False):
        return 0

    # Rich player check - bank balance only (wallet money is not exposed by
    # the mod server; bank balance is the persistent-wealth proxy).
    #
    # The cut scales smoothly between WEALTH_RICH_THRESHOLD and WEALTH_RICH_CEILING using a curve 
    # Poorer player more support, rich player less support
    try:
        from amc_finance.loans import get_player_bank_balance

        bank_balance = int(await get_player_bank_balance(character) or 0)
        threshold = amc_config.WEALTH_RICH_THRESHOLD
        ceiling = amc_config.WEALTH_RICH_CEILING
        if bank_balance >= threshold and ceiling > threshold:
            t = min(1.0, (bank_balance - threshold) / (ceiling - threshold))
            # Slow at first then accelerating with curve
            full_cut = 1.0 - amc_config.WEALTH_RICH_SUBSIDY_MULTIPLIER
            cut_now = full_cut * (t * t)
            subsidy = int(subsidy * (1.0 - cut_now))
        elif bank_balance >= threshold:
            # if ceiling <= threshold apply full cut to subsidy. subsidy become 0
            subsidy = int(subsidy * amc_config.WEALTH_RICH_SUBSIDY_MULTIPLIER)
    except Exception as e:
        logger.warning("Wealth-cut bank lookup failed for %s: %s", character.name, e)

    if subsidy <= 0:
        return 0

    if http_client_mod and await is_player_in_modded_vehicle(
        character, http_client_mod
    ):
        subsidy = int(subsidy * amc_config.MODDED_SUBSIDY_MULTIPLIER)

    return max(0, subsidy)


# ---------------------------------------------------------------------------
# Subsidy payout (positive amounts only)
# ---------------------------------------------------------------------------


async def subsidise_player(subsidy, character, session, message=None):
    """Pay a subsidy from the Treasury into the player's in-game wallet
    AND credit their bank ledger.

    This function is positive-only; tax flow lives in `amc.tax.tax_player`.
    Treasury floor scaling is preserved (low treasury -> zero subsidy).
    """
    if subsidy <= 0:
        return

    treasury_balance = await get_treasury_fund_balance()
    if treasury_balance <= amc_config.TREASURY_SUBSIDY_FLOOR:
        return
    scale = min(
        1.0,
        (float(treasury_balance) - amc_config.TREASURY_SUBSIDY_FLOOR)
        / (
            amc_config.TREASURY_SUBSIDY_CEILING
            - amc_config.TREASURY_SUBSIDY_FLOOR
        ),
    )
    subsidy = int(subsidy * scale)
    if subsidy <= 0:
        return

    if message is None:
        message = "ASEAN Subsidy"
    await transfer_money(
        session,
        int(subsidy),
        message,
        character.player.unique_id,
    )
    await send_fund_to_player_wallet(subsidy, character, message)
