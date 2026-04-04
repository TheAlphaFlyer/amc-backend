"""EventContext — frozen dataclass threaded through all webhook handlers.

Analogous to Phoenix's Plug.Conn: a single value carried through the
pipeline, replacing the 10+ parameter signatures that previously
threaded through every function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EventContext:
    """Immutable request context for webhook event processing."""

    http_client: Any = None
    http_client_mod: Any = None
    discord_client: Any = None
    treasury_balance: int | None = 0
    is_rp_mode: bool = False
    used_shortcut: bool = False
    active_term: Any = None
    parties: list = field(default_factory=list)
