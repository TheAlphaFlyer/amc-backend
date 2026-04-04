"""Deduplication logic for webhook events.

Handles epoch-based reset and seq/timestamp deduplication.
Extracted from process_events() in webhook.py.
"""

from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger("amc.webhook.dedup")

LAST_SEQ_CACHE_KEY = "webhook:last_processed_seq"
LAST_TS_CACHE_KEY = "webhook:last_processed_ts"
LAST_EPOCH_CACHE_KEY = "webhook:last_epoch"


def deduplicate_events(events: list[dict]) -> tuple[list[dict], int, int]:
    """Apply epoch and seq-based deduplication to raw webhook events.

    Returns (new_events, max_seq, last_processed).
    last_processed may be 0 after an epoch reset.
    """
    # --- Epoch-based reset ---
    last_processed = cache.get(LAST_SEQ_CACHE_KEY, 0)
    cached_epoch = cache.get(LAST_EPOCH_CACHE_KEY)
    for event in events:
        event_epoch = event.get("_epoch")
        if event_epoch is not None:
            if cached_epoch is not None and event_epoch != cached_epoch:
                logger.warning(
                    "Epoch changed: %s -> %s (server restarted), resetting seq high-water mark",
                    cached_epoch,
                    event_epoch,
                )
                last_processed = 0
                cache.set(LAST_SEQ_CACHE_KEY, 0, timeout=None)
            elif cached_epoch is None:
                last_processed = 0
                cache.set(LAST_SEQ_CACHE_KEY, 0, timeout=None)
            if event_epoch != cached_epoch:
                cached_epoch = event_epoch
                cache.set(LAST_EPOCH_CACHE_KEY, cached_epoch, timeout=None)
            break

    # --- Seq-based deduplication ---
    new_events = []
    max_seq = last_processed
    for event in events:
        seq = event.get("_seq")
        if seq is not None:
            if seq <= last_processed:
                continue
            max_seq = max(max_seq, seq)
        new_events.append(event)

    # --- Timestamp-floor deduplication (pre-sequence hotfix) ---
    last_processed_ts = cache.get(LAST_TS_CACHE_KEY, 0)
    if last_processed_ts:
        new_events = [
            e
            for e in new_events
            if e.get("_seq") is not None or e["timestamp"] > last_processed_ts
        ]

    return new_events, max_seq, last_processed


def persist_watermarks(max_seq: int, last_processed: int, events: list[dict]):
    """Persist high-water marks after successful processing."""
    last_processed_ts = cache.get(LAST_TS_CACHE_KEY, 0)
    if max_seq > last_processed:
        cache.set(LAST_SEQ_CACHE_KEY, max_seq, timeout=None)
    max_ts = max((e["timestamp"] for e in events if e.get("_seq") is None), default=0)
    if max_ts and max_ts > last_processed_ts:
        cache.set(LAST_TS_CACHE_KEY, max_ts, timeout=None)
