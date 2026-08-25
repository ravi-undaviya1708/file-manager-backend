"""Centralized billing plans configuration and helpers."""

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

def compute_subscription_expiry(billing_cycle: str) -> datetime:
    """Compute subscription expiry date based on billing cycle (monthly vs annual) from now."""
    now = datetime.now(timezone.utc)
    if billing_cycle.lower().strip() == "annual":
        return now + timedelta(days=365)
    else:
        return now + timedelta(days=30)
