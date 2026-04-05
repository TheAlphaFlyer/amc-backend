from datetime import date

from ninja import Router

from amc_finance.treasury_summary import (
    get_treasury_summary,
    get_treasury_trend,
    get_snapshot_or_live,
    get_snapshot_archive,
)

treasury_router = Router()


def _decimal_safe(obj: object) -> object:  # pyrefly: ignore
    """Recursively convert Decimal values to float for JSON serialization."""
    from decimal import Decimal

    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_safe(i) for i in obj]
    return obj


@treasury_router.get("/summary/")
def treasury_summary(
    request, days: int = 1, target_date: str | None = None
):  # pyrefly: ignore
    """Daily treasury income/expense summary with category breakdowns.

    Uses persisted snapshots for historical dates, live calculation otherwise.

    Args:
        days: Number of days to aggregate (default: 1 = yesterday only).
        target_date: Optional ISO date string (YYYY-MM-DD) to query.
    """
    parsed_date = date.fromisoformat(target_date) if target_date else None

    if parsed_date and days == 1:
        # For single-day queries, prefer snapshot if available
        data, is_snapshot = get_snapshot_or_live(target_date=parsed_date)
        result = _decimal_safe(data)
        result["is_snapshot"] = is_snapshot
        return result

    data = get_treasury_summary(target_date=parsed_date, days=days)
    result = _decimal_safe(data)
    result["is_snapshot"] = False
    return result


@treasury_router.get("/trend/")
def treasury_trend(request, days: int = 7):
    """Time-series treasury data for charting (daily income, expenses, balances).

    Args:
        days: Number of days of trend data (default: 7).
    """
    data = get_treasury_trend(days=days)
    return _decimal_safe(data)


@treasury_router.get("/archive/")
def treasury_archive(request, limit: int = 30):
    """List of available daily treasury snapshots for archive browsing.

    Args:
        limit: Maximum number of snapshots to return (default: 30).
    """
    return get_snapshot_archive(limit=limit)
