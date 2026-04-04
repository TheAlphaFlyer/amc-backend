"""Webhook handler registry.

Maps hook names to async handler functions.  Each handler module
registers its handlers via the @register decorator.  The dispatch
loop in process_event() looks up handlers from REGISTRY instead
of using a monolithic match statement.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger("amc.webhook.handlers")

# Return type for handlers that affect payments: (base_payment, subsidy, contract_payment, clawback)
PaymentResult = tuple[int, int, int, int]

# Handler signature: (event, player, character, ctx) -> PaymentResult
EventHandler = Callable[..., Awaitable[PaymentResult]]

REGISTRY: dict[str, EventHandler] = {}


def register(hook_name: str):
    """Decorator: register a handler for the given webhook hook name.

    Usage::

        @register("ServerCargoArrived")
        async def handle_cargo_arrived(event, player, character, ctx):
            ...
    """
    def decorator(fn: EventHandler) -> EventHandler:
        if hook_name in REGISTRY:
            logger.warning("Overwriting handler for hook %s", hook_name)
        REGISTRY[hook_name] = fn
        return fn
    return decorator


async def dispatch(
    hook_name: str,
    event: dict,
    player: Any,
    character: Any,
    ctx: Any,
) -> PaymentResult:
    """Dispatch an event to its registered handler.

    Returns (0, 0, 0, 0) for unrecognised hooks.
    """
    handler = REGISTRY.get(hook_name)
    if handler is None:
        return 0, 0, 0, 0
    return await handler(event, player, character, ctx)
