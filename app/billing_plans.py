import calendar
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional

# Storage limits in bytes
PLAN_LIMITS: Dict[str, int] = {
    "free": 16106127360,       # 15 GB
    "personal": 53687091200,   # 50 GB
    "plus": 214748364800,     # 200 GB
    "power": 1099511627776,   # 1 TB
}

# Plan prices in INR (standard/display)
PLAN_PRICES: Dict[str, Dict[str, float]] = {
    "personal": {
        "monthly": 119.0,
        "annual": 1190.0,
    },
    "plus": {
        "monthly": 299.0,
        "annual": 2990.0,
    },
    "power": {
        "monthly": 999.0,
        "annual": 9990.0,
    }
}

PLAN_PRICING = PLAN_PRICES


def get_storage_limit(plan_name: str) -> int:
    """Get storage limit in bytes for a given plan name."""
    plan_name = plan_name.lower().strip()
    return PLAN_LIMITS.get(plan_name, PLAN_LIMITS["free"])

def get_plan_price(plan_name: str, billing_cycle: str) -> float:
    """Get price in INR for a given plan and billing cycle."""
    plan_name = plan_name.lower().strip()
    billing_cycle = billing_cycle.lower().strip()
    
    if plan_name not in PLAN_PRICES:
        return 0.0
        
    return PLAN_PRICES[plan_name].get(billing_cycle, 0.0)


def add_calendar_interval(base_dt: datetime, interval: str) -> datetime:
    """Add exactly one calendar interval (monthly or annual) to a UTC datetime.

    Handles month-end preservation and leap years deterministically:
    - Monthly:
      - If base_dt is the last day of its month, the target date is the last day of the target month
        (e.g., Jan 31 -> Feb 28 -> Mar 31 -> Apr 30 -> May 31; Jan 31 -> Feb 29 -> Mar 31 on leap years).
      - Otherwise, preserves the exact numeric day where possible, clipping only when the target month has fewer days
        (e.g., Oct 3 -> Nov 3 -> Dec 3; Apr 15 -> May 15).
    - Annual:
      - Advances year by 1, preserving month and day.
      - If base_dt is Feb 29 on a leap year, it maps to Feb 28 on non-leap years.
    """
    if base_dt.tzinfo is None:
        dt = base_dt.replace(tzinfo=timezone.utc)
    else:
        dt = base_dt.astimezone(timezone.utc)

    clean_interval = (interval or "monthly").lower().strip()

    if clean_interval == "annual":
        target_year = dt.year + 1
        target_month = dt.month
        # Handle Feb 29 on leap year transitioning to non-leap year
        max_days = calendar.monthrange(target_year, target_month)[1]
        target_day = min(dt.day, max_days)
        return dt.replace(year=target_year, month=target_month, day=target_day)
    else:  # monthly (default)
        target_year = dt.year + (1 if dt.month == 12 else 0)
        target_month = 1 if dt.month == 12 else dt.month + 1

        base_max_days = calendar.monthrange(dt.year, dt.month)[1]
        is_last_day_of_month = (dt.day == base_max_days)

        target_max_days = calendar.monthrange(target_year, target_month)[1]

        if is_last_day_of_month:
            target_day = target_max_days
        else:
            target_day = min(dt.day, target_max_days)

        return dt.replace(year=target_year, month=target_month, day=target_day)


def compute_subscription_expiry(billing_cycle: str, start_dt: Optional[datetime] = None) -> datetime:
    """Compute subscription expiry date based on calendar billing cycle (monthly vs annual)."""
    base = start_dt or datetime.now(timezone.utc)
    return add_calendar_interval(base, billing_cycle)
