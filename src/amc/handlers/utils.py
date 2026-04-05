"""Shared utilities for webhook event handlers."""

import datetime


def parse_event_timestamp(event) -> datetime.datetime:
    """Parse the UTC epoch timestamp from a game server webhook event.

    The game server mod (MTDediMod) generates timestamps via
    ``std::time(nullptr)`` which returns UTC epoch seconds.
    """
    return datetime.datetime.fromtimestamp(event["timestamp"], tz=datetime.timezone.utc)
