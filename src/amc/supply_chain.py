import asyncio
import logging

from django.db.models import F, Sum
from django.utils import timezone

from amc.game_server import announce
from amc.models import (
    Character,
    DeliveryPoint,
    SupplyChainContribution,
    SupplyChainEvent,
    SupplyChainObjective,
)
from amc_finance.services import send_fund_to_player

logger = logging.getLogger(__name__)


async def check_and_record_contribution(
    delivery,
    character,
    cargo_key: str,
    quantity: int,
    destination_point: DeliveryPoint | None,
    source_point: DeliveryPoint | None,
) -> int:
    """
    Check if a delivery matches any active supply chain event objective.
    If so, record the contribution. Returns total rewardable quantity recorded.
    """
    total_recorded = 0

    active_events = SupplyChainEvent.objects.filter_active().prefetch_related(
        "objectives__cargos",
        "objectives__destination_points",
        "objectives__source_points",
    )

    async for event in active_events:
        async for objective in event.objectives.all():
            if not await _objective_matches(
                objective, cargo_key, destination_point, source_point
            ):
                continue

            # Calculate rewardable quantity (capped at ceiling if set)
            if objective.ceiling is not None:
                remaining = max(0, objective.ceiling - objective.quantity_fulfilled)
                rewardable_qty = min(quantity, remaining)
            else:
                rewardable_qty = quantity

            if rewardable_qty <= 0:
                continue

            # Record the contribution
            await SupplyChainContribution.objects.acreate(
                objective=objective,
                character=character,
                cargo_key=cargo_key,
                quantity=rewardable_qty,
                timestamp=timezone.now(),
                delivery=delivery,
            )

            # Update fulfilled counter
            await SupplyChainObjective.objects.filter(pk=objective.pk).aupdate(
                quantity_fulfilled=F("quantity_fulfilled") + rewardable_qty
            )

            total_recorded += rewardable_qty

    return total_recorded


async def _objective_matches(
    objective: SupplyChainObjective,
    cargo_key: str,
    destination_point: DeliveryPoint | None,
    source_point: DeliveryPoint | None,
) -> bool:
    """Check if a delivery matches an objective's cargo + point filters."""
    # Check cargo match
    cargo_ids = [c.key async for c in objective.cargos.all()]
    if cargo_ids and cargo_key not in cargo_ids:
        return False

    # Check destination match
    dest_ids = [dp.pk async for dp in objective.destination_points.all()]
    if dest_ids and (destination_point is None or destination_point.pk not in dest_ids):
        return False

    # Check source match
    src_ids = [dp.pk async for dp in objective.source_points.all()]
    if src_ids and (source_point is None or source_point.pk not in src_ids):
        return False

    return True


async def distribute_event_rewards(event: SupplyChainEvent, http_client=None):
    """
    Distribute rewards for an ended event.
    Pool = reward_per_item × primary_objective.quantity_fulfilled (capped at ceiling).
    No primary objective → pool is 0.
    """
    if event.rewards_distributed:
        return

    # Find primary objective
    primary = await event.objectives.filter(is_primary=True).afirst()
    if not primary:
        # No primary objective → no payout, just mark as distributed
        event.rewards_distributed = True
        await event.asave(update_fields=["rewards_distributed"])
        return

    # Calculate pool from primary objective deliveries
    fulfilled = primary.quantity_fulfilled
    if primary.ceiling is not None:
        fulfilled = min(fulfilled, primary.ceiling)
    total_pool = int(event.reward_per_item * fulfilled)

    if total_pool <= 0:
        event.rewards_distributed = True
        await event.asave(update_fields=["rewards_distributed"])
        return

    total_weight = await event.objectives.aaggregate(total=Sum("reward_weight"))
    total_w = total_weight["total"] or 1

    all_contributor_names = []

    async for objective in event.objectives.all():
        # Objective's share of the pool
        objective_pool = int(total_pool * (objective.reward_weight / total_w))

        # Get all contributions grouped by character
        contributions = (
            SupplyChainContribution.objects.filter(objective=objective)
            .values("character_id")
            .annotate(total_qty=Sum("quantity"))
        )

        total_qty = 0
        character_contributions = {}
        async for contrib in contributions:
            char_id = contrib["character_id"]
            qty = contrib["total_qty"]
            if char_id:
                character_contributions[char_id] = qty
                total_qty += qty

        if total_qty == 0:
            continue

        # Fetch all contributing characters
        characters = {
            c.id: c
            async for c in Character.objects.filter(
                id__in=character_contributions.keys()
            )
        }

        # Distribute proportionally
        for character_id, qty in character_contributions.items():
            character_obj = characters.get(character_id)
            if not character_obj:
                continue

            reward = int(objective_pool * qty / total_qty)
            if reward <= 0:
                continue

            if character_obj.is_gov_employee:
                from amc.gov_employee import redirect_income_to_treasury

                await redirect_income_to_treasury(
                    0,
                    character_obj,
                    "Government Service – Event Bonus",
                    http_client=http_client,
                    contribution=reward,
                )
            else:
                await send_fund_to_player(
                    reward, character_obj, f"Supply Chain Event: {event.name}"
                )
            all_contributor_names.append(f"{character_obj.name} ({qty})")

    # Mark as distributed
    event.rewards_distributed = True
    await event.asave(update_fields=["rewards_distributed"])

    # Announce completion
    if all_contributor_names and http_client:
        contributors_str = ", ".join(all_contributor_names[:10])
        if len(all_contributor_names) > 10:
            contributors_str += f" and {len(all_contributor_names) - 10} more"
        message = (
            f'Supply Chain Event "{event.name}" has ended! '
            f"Rewards distributed. Thanks to: {contributors_str}"
        )
        asyncio.create_task(announce(message, http_client, color="90EE90"))


async def monitor_supply_chain_events(ctx):
    """
    Cron task: check for ended events and distribute rewards.
    """
    http_client = ctx.get("http_client")
    ended_events = SupplyChainEvent.objects.filter_ended_not_distributed()

    async for event in ended_events:
        try:
            await distribute_event_rewards(event, http_client)
            logger.info(f"Distributed rewards for supply chain event: {event.name}")
        except Exception:
            logger.exception(
                f"Failed to distribute rewards for event: {event.name}"
            )


async def get_conflicting_cargo_keys() -> set[tuple[str, int]]:
    """
    Returns set of (cargo_key, destination_point_id) pairs from active or
    future supply chain events. Used to suppress conflicting job postings.
    """
    conflicting = set()

    events = SupplyChainEvent.objects.filter_active_or_future().prefetch_related(
        "objectives__cargos",
        "objectives__destination_points",
    )

    async for event in events:
        async for objective in event.objectives.all():
            cargo_keys = [c.key async for c in objective.cargos.all()]
            dest_ids = [dp.pk async for dp in objective.destination_points.all()]

            for ck in cargo_keys:
                if dest_ids:
                    for did in dest_ids:
                        conflicting.add((ck, did))
                else:
                    # No destination filter — this cargo key conflicts everywhere
                    conflicting.add((ck, -1))

    return conflicting
