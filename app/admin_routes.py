"""Router for administrative management, database telemetry, and real-time analytics."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Query, status
from pydantic import BaseModel, EmailStr, Field

from app.auth import get_current_user
from app.models import User, FileSystemItem, StoragePartition, PaymentRecord, CancellationRecord, Role
import app.database

logger = logging.getLogger("app.admin_routes")

router = APIRouter(prefix="/api/admin", tags=["Super Admin"])


# ── Analytics & Telemetry Schemas ─────────────────────────────────────────────

class KPICardData(BaseModel):
    value: float | int
    previousValue: Optional[float | int] = None
    changePercent: float = 0.0
    trend: str = "neutral"  # "up", "down", "neutral"
    comparisonLabel: str = "vs last 30 days"


class StorageOverviewData(BaseModel):
    totalStorageUsedBytes: int
    totalStorageCapacityBytes: int
    allocatedCapacityBytes: int
    availableStorageBytes: int
    usagePercentage: float
    formattedUsed: str
    formattedCapacity: str
    formattedAvailable: str
    totalFilesCount: int
    totalFoldersCount: int
    totalDatabaseItems: int
    avgFilesPerUser: float


class GrowthPoint(BaseModel):
    date: str
    timestamp: int
    totalUsers: int
    newUsers: int
    activeUsers: int


class StorageTrendPoint(BaseModel):
    date: str
    timestamp: int
    totalStorageUsedBytes: int
    formattedStorage: str
    storageAddedBytes: int


class PlanDistributionItem(BaseModel):
    plan: str
    count: int
    percentage: float
    color: str


class UserStatusItem(BaseModel):
    status: str
    count: int
    percentage: float
    color: str


class TrialMetricsData(BaseModel):
    currentTrials: int
    newTrialsInPeriod: int
    convertedToPaid: int
    expiredTrials: int
    canceledTrials: int
    conversionRate: float
    cancellationRate: float


class CancellationMetricsData(BaseModel):
    totalCancellations: int
    paidCancellations: int
    trialCancellations: int
    cancellationRate: float


class AnalyticsOverviewResponse(BaseModel):
    dateRange: str
    startDate: str
    endDate: str
    totalAccounts: KPICardData
    activeUsers: KPICardData
    newSignups: KPICardData
    activeSubscriptions: KPICardData
    trialUsers: KPICardData
    canceledUsers: KPICardData
    storageUsage: StorageOverviewData
    userGrowth: List[GrowthPoint]
    storageTrend: List[StorageTrendPoint]
    subscriptionPlans: List[PlanDistributionItem]
    userStatusDistribution: List[UserStatusItem]
    trialMetrics: TrialMetricsData
    cancellationMetrics: CancellationMetricsData
    lastUpdated: str


class TopStorageUserItem(BaseModel):
    id: str
    name: str
    email: str
    avatarUrl: Optional[str] = None
    spaceUsed: int
    formattedSpaceUsed: str
    storageLimitBytes: int
    pricingPlan: str
    userType: str
    totalFiles: int


class RecentSignupItem(BaseModel):
    id: str
    name: str
    email: str
    avatarUrl: Optional[str] = None
    pricingPlan: str
    userType: str
    subscriptionStatus: str
    createdAt: str
    formattedJoinedAt: str


class RecentPaymentItem(BaseModel):
    id: str
    orderId: str
    customerName: str
    customerEmail: str
    amount: float
    currency: str
    formattedAmount: str
    planName: str
    billingCycle: str
    status: str
    paymentMethod: Optional[str] = None
    createdAt: str
    formattedDate: str


class RecentCancellationItem(BaseModel):
    id: str
    userId: str
    userName: str
    userEmail: str
    planName: str
    billingCycle: str
    isTrial: bool
    reason: str
    canceledAt: str
    formattedCanceledAt: str


class RecordCancellationRequest(BaseModel):
    userId: str
    reason: Optional[str] = "User requested cancellation"


class SubscriptionUserItem(BaseModel):
    id: str
    name: str
    email: str
    avatarUrl: Optional[str] = None
    pricingPlan: str
    billingCycle: str
    subscriptionStatus: str
    createdAt: str
    formattedJoinedAt: str
    trialExpiresAt: Optional[str] = None
    daysRemainingInTrial: Optional[int] = None
    spaceUsed: int
    formattedSpaceUsed: str
    storageLimitBytes: int


class SubscriptionsOverviewResponse(BaseModel):
    totalSubscribers: int
    activePaid: int
    onTrial: int
    canceled: int
    users: List[SubscriptionUserItem]


class PaymentSummaryResponse(BaseModel):
    totalCollected: float
    formattedTotalCollected: str
    totalTransactions: int
    averageTransactionValue: float
    formattedAverageTransactionValue: str
    currency: str
    payments: List[RecentPaymentItem]


class TrafficSourceItem(BaseModel):
    source: str
    sessions: int
    percentage: float
    color: str


class TopPageItem(BaseModel):
    path: str
    title: str
    pageviews: int
    uniquePageviews: int
    avgTime: str


class DeviceItem(BaseModel):
    device: str
    percentage: float
    sessions: int
    color: str


class CountryItem(BaseModel):
    country: str
    code: str
    users: int
    percentage: float


class EventAnalyticsItem(BaseModel):
    eventName: str
    eventCount: int
    keyEvents: int = 0


class GoogleAnalyticsResponse(BaseModel):
    propertyId: str
    status: str
    isRealtimeActive: bool = True
    dateRange: str = "30d"
    updatedAt: Optional[str] = None
    realtime: Optional[Dict[str, Any]] = None
    realtimeActiveUsers: int
    totalUsers: int
    newUsers: int
    sessions: int
    pageviews: int
    avgSessionDuration: str
    avgSessionDurationSeconds: Optional[float] = None
    bounceRate: float
    trafficSources: List[TrafficSourceItem]
    topPages: List[TopPageItem]
    deviceDistribution: List[DeviceItem]
    countryDistribution: List[CountryItem]
    events: List[EventAnalyticsItem] = []


# ── Req/Res Schemas ──────────────────────────────────────────────────────────

class AdminUserResponse(BaseModel):
    id: str
    name: str
    email: str
    isAdmin: bool
    storageLimitBytes: int
    pricingPlan: str
    createdAt: str
    totalFiles: int
    spaceUsed: int
    userType: str


class EditLimitRequest(BaseModel):
    limitBytes: int


class EditRoleRequest(BaseModel):
    userType: str


class MessageResponse(BaseModel):
    message: str


class RoleResponse(BaseModel):
    name: str
    key: str
    isDefault: bool
    description: str
    permissions: List[str]


class CreateRoleRequest(BaseModel):
    name: str
    key: str
    description: str = ""
    permissions: List[str] = []


class UpdateRoleRequest(BaseModel):
    name: str
    description: str = ""
    permissions: List[str] = []



# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime object is timezone-aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def format_size_human(bytes_val: int | float) -> str:
    """Format bytes to readable string like '4.37 GB', '23.61 MB', '0 Bytes'."""
    if not bytes_val or bytes_val <= 0:
        return "0 Bytes"
    k = 1024
    sizes = ["Bytes", "KB", "MB", "GB", "TB", "PB"]
    i = int(math.floor(math.log(max(1, bytes_val)) / math.log(k)))
    i = min(i, len(sizes) - 1)
    val = round(bytes_val / (k ** i), 2)
    return f"{val} {sizes[i]}"


def _calculate_pct_change(current_val: float | int, prev_val: Optional[float | int]) -> tuple[float, str]:
    """Calculate percentage change and trend direction."""
    if prev_val is None or prev_val == 0:
        if current_val > 0:
            return 100.0, "up"
        return 0.0, "neutral"
    
    change = ((current_val - prev_val) / prev_val) * 100.0
    change_rounded = round(change, 1)
    if change_rounded > 0:
        return change_rounded, "up"
    elif change_rounded < 0:
        return change_rounded, "down"
    return 0.0, "neutral"


def _parse_date_range(
    range_str: str,
    start_date_str: Optional[str] = None,
    end_date_str: Optional[str] = None
) -> tuple[datetime, datetime, datetime, datetime, str, int]:
    """Parse date range into UTC start/end and previous period window."""
    now = datetime.now(timezone.utc)
    range_clean = (range_str or "30d").lower().strip()

    if range_clean == "today":
        start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_time = now
        duration = end_time - start_time
        if duration.total_seconds() < 3600:
            duration = timedelta(hours=24)
        prev_end_time = start_time
        prev_start_time = prev_end_time - duration
        comparison_label = "vs yesterday"
        intervals = 6
    elif range_clean in ["7d", "7days", "last 7 days", "last7days"]:
        end_time = now
        start_time = now - timedelta(days=7)
        prev_end_time = start_time
        prev_start_time = start_time - timedelta(days=7)
        comparison_label = "vs last 7 days"
        intervals = 7
    elif range_clean in ["90d", "90days", "last 90 days", "last90days"]:
        end_time = now
        start_time = now - timedelta(days=90)
        prev_end_time = start_time
        prev_start_time = start_time - timedelta(days=90)
        comparison_label = "vs last 90 days"
        intervals = 6
    elif range_clean in ["year", "1y", "365d", "this year", "thisyear"]:
        end_time = now
        start_time = now - timedelta(days=365)
        prev_end_time = start_time
        prev_start_time = start_time - timedelta(days=365)
        comparison_label = "vs last year"
        intervals = 6
    elif range_clean == "custom" and start_date_str:
        try:
            start_time = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
            start_time = _ensure_utc(start_time)
            if end_date_str:
                end_time = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                end_time = _ensure_utc(end_time)
            else:
                end_time = now
            duration = end_time - start_time
            prev_end_time = start_time
            prev_start_time = start_time - duration
            comparison_label = "vs previous period"
            intervals = 6
        except Exception:
            end_time = now
            start_time = now - timedelta(days=30)
            prev_end_time = start_time
            prev_start_time = start_time - timedelta(days=30)
            comparison_label = "vs last 30 days"
            intervals = 6
    else:  # default 30d
        end_time = now
        start_time = now - timedelta(days=30)
        prev_end_time = start_time
        prev_start_time = start_time - timedelta(days=30)
        comparison_label = "vs last 30 days"
        intervals = 6

    return start_time, end_time, prev_start_time, prev_end_time, comparison_label, intervals


# ── Dependency ────────────────────────────────────────────────────────────────

async def admin_required(current_user: User = Depends(get_current_user)) -> User:
    """Dependency to enforce admin access controls."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Administrative privileges required."}
        )
    return current_user



# ── Analytics Routes ──────────────────────────────────────────────────────────

@router.get(
    "/analytics/overview",
    response_model=AnalyticsOverviewResponse,
    summary="Get comprehensive platform analytics telemetry & KPIs"
)
async def get_analytics_overview(
    time_range: str = Query("30d", alias="range", description="Time window: today, 7d, 30d, 90d, year, custom"),
    startDate: Optional[str] = Query(None, description="Custom start date ISO string"),
    endDate: Optional[str] = Query(None, description="Custom end date ISO string"),
    admin: User = Depends(admin_required)
):
    """Retrieve full platform telemetry KPIs, charts, distributions, and storage analytics."""
    from app.crud import _run_filesystem_aggregation

    now = datetime.now(timezone.utc)
    start_time, end_time, prev_start_time, prev_end_time, comparison_label, num_intervals = _parse_date_range(
        time_range, startDate, endDate
    )

    # 1. Fetch all users
    all_users = await User.find_all().to_list()
    total_users_count = len(all_users)

    # Calculate Total Accounts KPI
    users_before_start = sum(
        1 for u in all_users
        if u.created_at and _ensure_utc(u.created_at) < start_time
    )
    total_accounts_change, total_accounts_trend = _calculate_pct_change(
        total_users_count, users_before_start
    )

    # Calculate New Signups KPI
    new_signups_current = sum(
        1 for u in all_users
        if u.created_at and start_time <= _ensure_utc(u.created_at) <= end_time
    )
    new_signups_prev = sum(
        1 for u in all_users
        if u.created_at and prev_start_time <= _ensure_utc(u.created_at) <= prev_end_time
    )
    new_signups_change, new_signups_trend = _calculate_pct_change(
        new_signups_current, new_signups_prev
    )

    # Active Users (Status not canceled or inactive)
    active_users_list = [
        u for u in all_users if (getattr(u, "subscription_status", "active") or "active") not in ["canceled", "inactive"]
    ]
    active_users_count = len(active_users_list)
    active_users_prev = sum(
        1 for u in all_users
        if (getattr(u, "subscription_status", "active") or "active") not in ["canceled", "inactive"]
        and u.created_at and _ensure_utc(u.created_at) < start_time
    )
    active_users_change, active_users_trend = _calculate_pct_change(
        active_users_count, active_users_prev
    )

    # Active Subscriptions (Paid tiers)
    paid_plans = ["personal", "plus", "power", "pro", "business"]
    paid_users = [
        u for u in all_users
        if (u.pricing_plan or "").lower() in paid_plans
        and (getattr(u, "subscription_status", "active") or "active") == "active"
    ]
    active_subs_count = len(paid_users)
    active_subs_prev = sum(
        1 for u in all_users
        if (u.pricing_plan or "").lower() in paid_plans
        and (getattr(u, "subscription_status", "active") or "active") == "active"
        and u.created_at and _ensure_utc(u.created_at) < start_time
    )
    active_subs_change, active_subs_trend = _calculate_pct_change(
        active_subs_count, active_subs_prev
    )

    # Trial Users
    trial_users_list = [
        u for u in all_users
        if (u.pricing_plan or "").lower() == "free" or (getattr(u, "subscription_status", None) == "trial")
    ]
    trial_users_count = len(trial_users_list)
    trial_users_prev = sum(
        1 for u in all_users
        if ((u.pricing_plan or "").lower() == "free" or (getattr(u, "subscription_status", None) == "trial"))
        and u.created_at and _ensure_utc(u.created_at) < start_time
    )
    trial_users_change, trial_users_trend = _calculate_pct_change(
        trial_users_count, trial_users_prev
    )

    # Cancellations in period
    cancellations_in_period = await CancellationRecord.find({
        "created_at": {"$gte": start_time, "$lte": end_time}
    }).count()

    # Also count any user with canceled_at in range
    users_canceled_in_period = sum(
        1 for u in all_users
        if getattr(u, "canceled_at", None) and start_time <= _ensure_utc(u.canceled_at) <= end_time
    )
    canceled_current = max(cancellations_in_period, users_canceled_in_period)

    cancellations_prev = await CancellationRecord.find({
        "created_at": {"$gte": prev_start_time, "$lte": prev_end_time}
    }).count()
    users_canceled_prev = sum(
        1 for u in all_users
        if getattr(u, "canceled_at", None) and prev_start_time <= _ensure_utc(u.canceled_at) <= prev_end_time
    )
    canceled_prev = max(cancellations_prev, users_canceled_prev)
    canceled_change, canceled_trend = _calculate_pct_change(
        canceled_current, canceled_prev
    )

    # 2. Storage metrics & DB aggregations
    storage_pipeline = [
        {"$match": {"type": "file", "is_deleted": False}},
        {"$group": {
            "_id": None,
            "total_size": {"$sum": "$size"},
            "file_count": {"$sum": 1}
        }}
    ]
    storage_res = await _run_filesystem_aggregation(storage_pipeline, length=1)
    total_storage_used = storage_res[0]["total_size"] if storage_res else 0
    total_files_count = storage_res[0]["file_count"] if storage_res else 0

    total_folders_count = await FileSystemItem.find(
        FileSystemItem.type == "folder",
        FileSystemItem.is_deleted == False
    ).count()

    total_db_items = await FileSystemItem.find(
        FileSystemItem.is_deleted == False
    ).count()

    allocated_capacity = sum(
        getattr(u, "storage_limit_bytes", 16106127360) for u in all_users
    )
    system_capacity_baseline = 200 * 1024 * 1024 * 1024
    total_capacity = max(system_capacity_baseline, allocated_capacity if allocated_capacity > 0 else system_capacity_baseline)
    available_storage = max(0, total_capacity - total_storage_used)
    usage_pct = round((total_storage_used / total_capacity) * 100.0, 2) if total_capacity > 0 else 0.0
    avg_files = round(total_files_count / max(1, total_users_count), 1)

    # 3. Time-Series: User Growth Chart
    user_growth_points: List[GrowthPoint] = []
    total_duration_sec = (end_time - start_time).total_seconds()
    step_sec = total_duration_sec / max(1, num_intervals)

    for i in range(num_intervals + 1):
        bucket_time = start_time + timedelta(seconds=i * step_sec)
        if bucket_time > end_time:
            bucket_time = end_time

        cum_users = sum(
            1 for u in all_users
            if u.created_at and _ensure_utc(u.created_at) <= bucket_time
        )
        slice_start = bucket_time - timedelta(seconds=step_sec)
        new_in_bucket = sum(
            1 for u in all_users
            if u.created_at and slice_start < _ensure_utc(u.created_at) <= bucket_time
        )
        active_in_bucket = sum(
            1 for u in all_users
            if u.created_at and _ensure_utc(u.created_at) <= bucket_time
            and (getattr(u, "subscription_status", "active") or "active") not in ["canceled", "inactive"]
        )

        date_label = bucket_time.strftime("%b %d") if total_duration_sec > 86400 else bucket_time.strftime("%H:%M")
        user_growth_points.append(
            GrowthPoint(
                date=date_label,
                timestamp=int(bucket_time.timestamp() * 1000),
                totalUsers=cum_users,
                newUsers=new_in_bucket,
                activeUsers=active_in_bucket
            )
        )

    # 4. Time-Series: Storage Usage Trend Chart
    storage_trend_points: List[StorageTrendPoint] = []
    all_files_pipeline = [
        {"$match": {"type": "file", "is_deleted": False}},
        {"$project": {"size": 1, "created_at": 1}}
    ]
    file_items_raw = await _run_filesystem_aggregation(all_files_pipeline, length=100000)

    for i in range(num_intervals + 1):
        bucket_time = start_time + timedelta(seconds=i * step_sec)
        if bucket_time > end_time:
            bucket_time = end_time

        cum_storage_bytes = sum(
            f.get("size", 0) for f in file_items_raw
            if f.get("created_at") and _ensure_utc(f["created_at"]) <= bucket_time
        )
        slice_start = bucket_time - timedelta(seconds=step_sec)
        added_in_slice = sum(
            f.get("size", 0) for f in file_items_raw
            if f.get("created_at") and slice_start < _ensure_utc(f["created_at"]) <= bucket_time
        )

        date_label = bucket_time.strftime("%b %d") if total_duration_sec > 86400 else bucket_time.strftime("%H:%M")
        storage_trend_points.append(
            StorageTrendPoint(
                date=date_label,
                timestamp=int(bucket_time.timestamp() * 1000),
                totalStorageUsedBytes=cum_storage_bytes,
                formattedStorage=format_size_human(cum_storage_bytes),
                storageAddedBytes=added_in_slice
            )
        )

    # 5. Subscription Plan Distribution
    plan_colors = {
        "free": "#818cf8",        # Soft Indigo
        "plus": "#a855f7",        # Purple
        "power": "#3b82f6",       # Blue
        "personal": "#06b6d4",    # Cyan
        "pro": "#6366f1",         # Deep Indigo
        "enterprise": "#ec4899",  # Pink
        "trial": "#34d399",       # Mint Green
    }

    plan_counts: Dict[str, int] = {
        "Free": 0,
        "Plus": 0,
        "Power": 0,
        "Personal": 0,
        "Pro": 0,
        "Enterprise": 0,
        "Trial": 0
    }

    for u in all_users:
        plan_raw = (u.pricing_plan or "free").lower().strip()
        status_raw = (getattr(u, "subscription_status", "trial") or "trial").lower().strip()

        if status_raw == "trial" or (plan_raw == "free" and getattr(u, "daysRemainingInTrial", 10) and getattr(u, "daysRemainingInTrial", 10) > 0):
            plan_counts["Trial"] += 1
        elif plan_raw == "free":
            plan_counts["Free"] += 1
        elif plan_raw == "plus":
            plan_counts["Plus"] += 1
        elif plan_raw == "power":
            plan_counts["Power"] += 1
        elif plan_raw == "personal":
            plan_counts["Personal"] += 1
        elif plan_raw == "pro":
            plan_counts["Pro"] += 1
        elif plan_raw == "enterprise":
            plan_counts["Enterprise"] += 1
        else:
            plan_counts["Free"] += 1

    subscription_plans_list: List[PlanDistributionItem] = []
    denom = max(1, total_users_count)
    for p_name, count in plan_counts.items():
        pct = round((count / denom) * 100.0, 1)
        subscription_plans_list.append(
            PlanDistributionItem(
                plan=p_name,
                count=count,
                percentage=pct,
                color=plan_colors.get(p_name.lower(), "#94a3b8")
            )
        )

    # 6. User Status Distribution (Active, On Trial, Canceled, Inactive)
    status_counts = {
        "Active": 0,
        "On Trial": 0,
        "Canceled": 0,
        "Inactive": 0
    }

    for u in all_users:
        st = (getattr(u, "subscription_status", "active") or "active").lower().strip()
        plan_raw = (u.pricing_plan or "free").lower().strip()

        if st == "canceled":
            status_counts["Canceled"] += 1
        elif st == "inactive" or st == "expired":
            status_counts["Inactive"] += 1
        elif st == "trial" or plan_raw == "free":
            status_counts["On Trial"] += 1
        else:
            status_counts["Active"] += 1

    status_colors = {
        "Active": "#10b981",    # Emerald green
        "On Trial": "#06b6d4",  # Bright cyan
        "Canceled": "#ef4444",  # Rose red
        "Inactive": "#94a3b8",  # Muted slate
    }

    user_status_list: List[UserStatusItem] = [
        UserStatusItem(
            status=s_name,
            count=count,
            percentage=round((count / denom) * 100.0, 1),
            color=status_colors.get(s_name, "#64748b")
        )
        for s_name, count in status_counts.items()
    ]

    # 7. Trial Metrics & Cancellation Metrics
    converted_paid = sum(
        1 for u in all_users
        if (u.pricing_plan or "").lower() in paid_plans
    )
    expired_trials = sum(
        1 for u in all_users
        if (u.pricing_plan or "").lower() == "free" and (getattr(u, "daysRemainingInTrial", None) == 0 or getattr(u, "subscription_status", None) == "expired")
    )
    total_cancellations_all_time = await CancellationRecord.count() + sum(
        1 for u in all_users if getattr(u, "subscription_status", None) == "canceled"
    )
    trial_cancellations_count = await CancellationRecord.find({"is_trial": True}).count()
    paid_cancellations_count = max(0, total_cancellations_all_time - trial_cancellations_count)

    trial_conversion_rate = round((converted_paid / max(1, total_users_count)) * 100.0, 1)
    trial_cancellation_rate = round((trial_cancellations_count / max(1, trial_users_count)) * 100.0, 1)
    overall_cancellation_rate = round((total_cancellations_all_time / max(1, total_users_count)) * 100.0, 1)

    return AnalyticsOverviewResponse(
        dateRange=time_range,
        startDate=start_time.isoformat(),
        endDate=end_time.isoformat(),
        totalAccounts=KPICardData(
            value=total_users_count,
            previousValue=users_before_start,
            changePercent=total_accounts_change,
            trend=total_accounts_trend,
            comparisonLabel=comparison_label
        ),
        activeUsers=KPICardData(
            value=active_users_count,
            previousValue=active_users_prev,
            changePercent=active_users_change,
            trend=active_users_trend,
            comparisonLabel=comparison_label
        ),
        newSignups=KPICardData(
            value=new_signups_current,
            previousValue=new_signups_prev,
            changePercent=new_signups_change,
            trend=new_signups_trend,
            comparisonLabel=comparison_label
        ),
        activeSubscriptions=KPICardData(
            value=active_subs_count,
            previousValue=active_subs_prev,
            changePercent=active_subs_change,
            trend=active_subs_trend,
            comparisonLabel=comparison_label
        ),
        trialUsers=KPICardData(
            value=trial_users_count,
            previousValue=trial_users_prev,
            changePercent=trial_users_change,
            trend=trial_users_trend,
            comparisonLabel=comparison_label
        ),
        canceledUsers=KPICardData(
            value=canceled_current,
            previousValue=canceled_prev,
            changePercent=canceled_change,
            trend=canceled_trend,
            comparisonLabel=comparison_label
        ),
        storageUsage=StorageOverviewData(
            totalStorageUsedBytes=total_storage_used,
            totalStorageCapacityBytes=total_capacity,
            allocatedCapacityBytes=allocated_capacity,
            availableStorageBytes=available_storage,
            usagePercentage=usage_pct,
            formattedUsed=format_size_human(total_storage_used),
            formattedCapacity=format_size_human(total_capacity),
            formattedAvailable=format_size_human(available_storage),
            totalFilesCount=total_files_count,
            totalFoldersCount=total_folders_count,
            totalDatabaseItems=total_db_items,
            avgFilesPerUser=avg_files
        ),
        userGrowth=user_growth_points,
        storageTrend=storage_trend_points,
        subscriptionPlans=subscription_plans_list,
        userStatusDistribution=user_status_list,
        trialMetrics=TrialMetricsData(
            currentTrials=trial_users_count,
            newTrialsInPeriod=new_signups_current,
            convertedToPaid=converted_paid,
            expiredTrials=expired_trials,
            canceledTrials=trial_cancellations_count,
            conversionRate=trial_conversion_rate,
            cancellationRate=trial_cancellation_rate
        ),
        cancellationMetrics=CancellationMetricsData(
            totalCancellations=total_cancellations_all_time,
            paidCancellations=paid_cancellations_count,
            trialCancellations=trial_cancellations_count,
            cancellationRate=overall_cancellation_rate
        ),
        lastUpdated=now.strftime("%b %d, %Y %H:%M:%S UTC")
    )


@router.get(
    "/analytics/top-storage-users",
    response_model=List[TopStorageUserItem],
    summary="Get top storage-consuming users"
)
async def get_top_storage_users(
    limit: int = Query(10, ge=1, le=100),
    admin: User = Depends(admin_required)
):
    """Retrieve top users sorted descending by physical storage space occupied."""
    from app.crud import _run_filesystem_aggregation

    # MongoDB aggregation pipeline to sum size and count files per user
    pipeline = [
        {"$match": {"type": "file", "is_deleted": False, "user_id": {"$ne": None}}},
        {"$group": {
            "_id": "$user_id",
            "spaceUsed": {"$sum": "$size"},
            "totalFiles": {"$sum": 1}
        }},
        {"$sort": {"spaceUsed": -1}},
        {"$limit": limit}
    ]

    stats_list = await _run_filesystem_aggregation(pipeline, length=limit)
    stats_map = {str(s["_id"]): s for s in stats_list if s.get("_id")}

    # Also fetch all users to populate profile information
    users = await User.find_all().to_list()
    users_map = {str(u.id): u for u in users}

    result: List[TopStorageUserItem] = []

    for item in stats_list:
        uid = str(item["_id"])
        u = users_map.get(uid)
        if not u:
            continue
        space_used = item.get("spaceUsed", 0)
        result.append(
            TopStorageUserItem(
                id=uid,
                name=u.name,
                email=u.email,
                avatarUrl=u.avatar_url,
                spaceUsed=space_used,
                formattedSpaceUsed=format_size_human(space_used),
                storageLimitBytes=u.storage_limit_bytes,
                pricingPlan=u.pricing_plan,
                userType=u.user_type,
                totalFiles=item.get("totalFiles", 0)
            )
        )

    # If fewer items than limit, append users with 0 storage
    if len(result) < limit:
        sorted_remaining = [u for u in users if str(u.id) not in stats_map]
        for u in sorted_remaining[:limit - len(result)]:
            result.append(
                TopStorageUserItem(
                    id=str(u.id),
                    name=u.name,
                    email=u.email,
                    avatarUrl=u.avatar_url,
                    spaceUsed=0,
                    formattedSpaceUsed="0 Bytes",
                    storageLimitBytes=u.storage_limit_bytes,
                    pricingPlan=u.pricing_plan,
                    userType=u.user_type,
                    totalFiles=0
                )
            )

    return result


@router.get(
    "/analytics/recent-signups",
    response_model=List[RecentSignupItem],
    summary="Get recent user registrations"
)
async def get_recent_signups(
    limit: int = Query(5, ge=1, le=50),
    admin: User = Depends(admin_required)
):
    """Retrieve most recently registered users."""
    users = await User.find_all().sort("-created_at").limit(limit).to_list()

    return [
        RecentSignupItem(
            id=str(u.id),
            name=u.name,
            email=u.email,
            avatarUrl=u.avatar_url,
            pricingPlan=u.pricing_plan,
            userType=u.user_type,
            subscriptionStatus=getattr(u, "subscription_status", "active") or "active",
            createdAt=u.created_at.isoformat() if u.created_at else "",
            formattedJoinedAt=u.created_at.strftime("%b %d, %Y") if u.created_at else "Recent"
        )
        for u in users
    ]


@router.get(
    "/analytics/recent-payments",
    response_model=List[RecentPaymentItem],
    summary="Get recent customer subscription payments"
)
async def get_recent_payments(
    limit: int = Query(5, ge=1, le=50),
    admin: User = Depends(admin_required)
):
    """Retrieve latest payment records."""
    payments = await PaymentRecord.find_all().sort("-created_at").limit(limit).to_list()

    return [
        RecentPaymentItem(
            id=str(p.id),
            orderId=p.order_id,
            customerName=p.customer_name or "Customer",
            customerEmail=p.customer_email or "",
            amount=p.amount,
            currency=p.currency,
            formattedAmount=f"₹{p.amount:,.2f}" if p.currency == "INR" else f"${p.amount:,.2f}",
            planName=p.plan_name.title(),
            billingCycle=p.billing_cycle.title(),
            status=p.status.title(),
            paymentMethod=p.payment_method,
            createdAt=p.created_at.isoformat() if p.created_at else "",
            formattedDate=p.created_at.strftime("%b %d, %Y") if p.created_at else "Recent"
        )
        for p in payments
    ]


@router.get(
    "/analytics/recent-cancellations",
    response_model=List[RecentCancellationItem],
    summary="Get recent subscription / trial cancellations"
)
async def get_recent_cancellations(
    limit: int = Query(5, ge=1, le=50),
    admin: User = Depends(admin_required)
):
    """Retrieve recent cancellation records."""
    cancellations = await CancellationRecord.find_all().sort("-created_at").limit(limit).to_list()

    # If few records in CancellationRecord, also check users with canceled status
    results: List[RecentCancellationItem] = [
        RecentCancellationItem(
            id=str(c.id),
            userId=c.user_id,
            userName=c.customer_name or "Customer",
            userEmail=c.customer_email,
            planName=c.plan_name.title(),
            billingCycle=c.billing_cycle.title(),
            isTrial=c.is_trial,
            reason=c.reason or "Subscription canceled",
            canceledAt=c.created_at.isoformat() if c.created_at else "",
            formattedCanceledAt=c.created_at.strftime("%b %d, %Y") if c.created_at else "Recent"
        )
        for c in cancellations
    ]

    if len(results) < limit:
        canceled_users = await User.find(
            User.subscription_status == "canceled"
        ).sort("-created_at").limit(limit - len(results)).to_list()

        for u in canceled_users:
            if any(r.userId == str(u.id) for r in results):
                continue
            canceled_time = getattr(u, "canceled_at", None) or u.created_at
            results.append(
                RecentCancellationItem(
                    id=str(u.id),
                    userId=str(u.id),
                    userName=u.name,
                    userEmail=u.email,
                    planName=u.pricing_plan.title(),
                    billingCycle=getattr(u, "billing_cycle", "monthly").title(),
                    isTrial=u.pricing_plan == "free",
                    reason=getattr(u, "cancellation_reason", "Account subscription canceled"),
                    canceledAt=canceled_time.isoformat() if canceled_time else "",
                    formattedCanceledAt=canceled_time.strftime("%b %d, %Y") if canceled_time else "Recent"
                )
            )

    return results


@router.post(
    "/analytics/record-cancellation",
    response_model=MessageResponse,
    summary="Record a user cancellation"
)
async def record_user_cancellation(
    body: RecordCancellationRequest,
    admin: User = Depends(admin_required)
):
    """Log a cancellation record and mark user status."""
    from beanie import PydanticObjectId
    try:
        target_user = await User.get(PydanticObjectId(body.userId))
    except Exception:
        target_user = None

    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "User not found."}
        )

    now = datetime.now(timezone.utc)
    is_trial = (target_user.pricing_plan == "free" or getattr(target_user, "subscription_status", None) == "trial")

    cancellation = CancellationRecord(
        user_id=str(target_user.id),
        customer_name=target_user.name,
        customer_email=target_user.email,
        plan_name=target_user.pricing_plan,
        billing_cycle=getattr(target_user, "billing_cycle", "monthly") or "monthly",
        is_trial=is_trial,
        reason=body.reason or "User canceled subscription",
        created_at=now
    )
    await cancellation.insert()

    target_user.subscription_status = "canceled"
    target_user.canceled_at = now
    target_user.cancellation_reason = body.reason
    await target_user.save()

    return MessageResponse(message=f"Cancellation recorded for {target_user.email}.")


@router.get(
    "/subscriptions/overview",
    response_model=SubscriptionsOverviewResponse,
    summary="Get detailed subscription user list and metrics"
)
async def get_subscriptions_overview(admin: User = Depends(admin_required)):
    """Retrieve all users with subscription plan details, status, and joined dates."""
    from app.crud import _run_filesystem_aggregation

    users = await User.find_all().sort("-created_at").to_list()
    
    # Aggregation for space used
    pipeline = [
        {"$match": {"type": "file", "is_deleted": False, "user_id": {"$ne": None}}},
        {"$group": {"_id": "$user_id", "total_size": {"$sum": "$size"}}}
    ]
    usage_list = await _run_filesystem_aggregation(pipeline, length=100000)
    usage_map = {str(u["_id"]): u.get("total_size", 0) for u in usage_list if u.get("_id")}

    paid_plans = ["personal", "plus", "power", "pro", "business"]
    active_paid = 0
    on_trial = 0
    canceled = 0
    items: List[SubscriptionUserItem] = []

    for u in users:
        plan = (u.pricing_plan or "free").lower().strip()
        status_val = (getattr(u, "subscription_status", "active") or "active").strip()
        
        if status_val.lower() == "canceled":
            current_status = "Canceled"
            canceled += 1
        elif status_val.lower() == "trial" or plan == "free":
            current_status = "On Trial"
            on_trial += 1
        elif status_val.lower() == "inactive" or status_val.lower() == "expired":
            current_status = "Inactive"
        else:
            current_status = "Active"
            if plan in paid_plans:
                active_paid += 1

        space = usage_map.get(str(u.id), 0)
        items.append(
            SubscriptionUserItem(
                id=str(u.id),
                name=u.name,
                email=u.email,
                avatarUrl=u.avatar_url,
                pricingPlan=u.pricing_plan.title() if u.pricing_plan else "Free",
                billingCycle=getattr(u, "billing_cycle", "monthly").title() if getattr(u, "billing_cycle", None) else "Monthly",
                subscriptionStatus=current_status,
                createdAt=u.created_at.isoformat() if u.created_at else "",
                formattedJoinedAt=u.created_at.strftime("%b %d, %Y") if u.created_at else "Recent",
                trialExpiresAt=u.trial_expires_at.isoformat() if getattr(u, "trial_expires_at", None) else None,
                daysRemainingInTrial=getattr(u, "days_remaining_in_trial", 10),
                spaceUsed=space,
                formattedSpaceUsed=format_size_human(space),
                storageLimitBytes=u.storage_limit_bytes
            )
        )

    return SubscriptionsOverviewResponse(
        totalSubscribers=len(users),
        activePaid=active_paid,
        onTrial=on_trial,
        canceled=canceled,
        users=items
    )


@router.get(
    "/payments/overview",
    response_model=PaymentSummaryResponse,
    summary="Get revenue collections and all paid transactions"
)
async def get_payments_overview(admin: User = Depends(admin_required)):
    """Retrieve total revenue collected excluding free trials, with transaction list."""
    # Find all payments, sorted descending by creation time
    payments_raw = await PaymentRecord.find_all().sort("-created_at").to_list()

    # Filter out free trial ($0.00 / free plan) records to accurately compute collected revenue
    paid_records = [
        p for p in payments_raw
        if p.amount > 0 and (p.plan_name or "").lower() != "free"
    ]

    total_collected = sum(p.amount for p in paid_records if (p.status or "").upper() in ["SUCCESS", "COMPLETED", "PAID"])
    # If no paid records found yet in dev, total is 0
    total_tx_count = len([p for p in paid_records if (p.status or "").upper() in ["SUCCESS", "COMPLETED", "PAID"]])
    avg_tx_val = round(total_collected / max(1, total_tx_count), 2) if total_tx_count > 0 else 0.0

    # Currency format
    currency = "USD"
    if paid_records and any(p.currency == "INR" for p in paid_records):
        currency = "INR"

    formatted_total = f"₹{total_collected:,.2f}" if currency == "INR" else f"${total_collected:,.2f}"
    formatted_avg = f"₹{avg_tx_val:,.2f}" if currency == "INR" else f"${avg_tx_val:,.2f}"

    payments_list: List[RecentPaymentItem] = []
    for p in payments_raw:
        # Include all payments in table, with explicit status
        amt_str = f"₹{p.amount:,.2f}" if p.currency == "INR" else f"${p.amount:,.2f}"
        payments_list.append(
            RecentPaymentItem(
                id=str(p.id),
                orderId=p.order_id,
                customerName=p.customer_name or "Customer",
                customerEmail=p.customer_email or "",
                amount=p.amount,
                currency=p.currency,
                formattedAmount=amt_str,
                planName=p.plan_name.title(),
                billingCycle=p.billing_cycle.title(),
                status=p.status.title(),
                paymentMethod=p.payment_method,
                createdAt=p.created_at.isoformat() if p.created_at else "",
                formattedDate=p.created_at.strftime("%b %d, %Y") if p.created_at else "Recent"
            )
        )

    return PaymentSummaryResponse(
        totalCollected=total_collected,
        formattedTotalCollected=formatted_total,
        totalTransactions=total_tx_count,
        averageTransactionValue=avg_tx_val,
        formattedAverageTransactionValue=formatted_avg,
        currency=currency,
        payments=payments_list
    )


@router.get(
    "/analytics/google-analytics",
    response_model=GoogleAnalyticsResponse,
    summary="Get Google Analytics 4 report telemetry"
)
async def get_google_analytics_report(
    range: str = Query("30d", description="Date range: today, yesterday, 7d, 30d, 90d"),
    refresh: bool = Query(False, description="Force refresh and bypass cache"),
    admin: User = Depends(admin_required)
):
    """Retrieve real Google Analytics 4 performance metrics, traffic sources, device statistics, and events.
    Strictly adheres to Zero Mock Policy: returns real Data API telemetry or actionable error details."""
    from app.google_analytics import get_google_analytics_report_data

    try:
        data = get_google_analytics_report_data(date_range=range, force_refresh=refresh)
        return GoogleAnalyticsResponse(**data)
    except ValueError as exc:
        logger.error("Google Analytics configuration/validation error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": str(exc), "code": "GA4_CONFIG_ERROR"}
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Google Analytics API request error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": f"Google Analytics service unavailable: {str(exc)}", "code": "GA4_API_ERROR"}
        )


# ── Management Routes ─────────────────────────────────────────────────────────

@router.get(
    "/users",
    response_model=List[AdminUserResponse],
    summary="List all users and their usage statistics"
)
async def list_users(admin: User = Depends(admin_required)):
    """Retrieve details of all registered users with space usage statistics."""
    users = await User.find_all().to_list()
    if not users:
        return []

    # Single aggregation query to fetch file counts and space used for all users in O(1)
    pipeline = [
        {"$match": {"type": "file", "is_deleted": False, "user_id": {"$ne": None}}},
        {"$group": {
            "_id": "$user_id",
            "total_files": {"$sum": 1},
            "space_used": {"$sum": "$size"}
        }}
    ]
    from app.crud import _run_filesystem_aggregation
    stats_list = await _run_filesystem_aggregation(pipeline, length=10000)
    stats_map = {str(s["_id"]): s for s in stats_list if s.get("_id")}

    response = []
    for u in users:
        user_id_str = str(u.id)
        user_stats = stats_map.get(user_id_str, {})
        total_files = user_stats.get("total_files", 0)
        space_used = user_stats.get("space_used", 0)
        
        response.append(
            AdminUserResponse(
                id=user_id_str,
                name=u.name,
                email=u.email,
                isAdmin=u.is_admin,
                storageLimitBytes=u.storage_limit_bytes,
                pricingPlan=u.pricing_plan,
                createdAt=u.created_at.isoformat() if u.created_at else "",
                totalFiles=total_files,
                spaceUsed=space_used,
                userType=u.user_type
            )
        )
        
    return response


@router.put(
    "/users/{user_id}/limit",
    response_model=MessageResponse,
    summary="Change a user's storage limit"
)
async def edit_user_limit(
    user_id: str,
    body: EditLimitRequest,
    admin: User = Depends(admin_required)
):
    """Change the total storage limit (in bytes) for a specific user."""
    from beanie import PydanticObjectId
    try:
        user = await User.get(PydanticObjectId(user_id))
    except Exception:
        user = None
        
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "User not found."}
        )
        
    if body.limitBytes < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Storage limit cannot be negative."}
        )
        
    user.storage_limit_bytes = body.limitBytes
    await user.save()
    return MessageResponse(
        message=f"Storage limit for user {user.email} updated to {body.limitBytes} bytes."
    )


@router.put(
    "/users/{user_id}/role",
    response_model=MessageResponse,
    summary="Change user role/type status"
)
async def edit_user_role(
    user_id: str,
    body: EditRoleRequest,
    admin: User = Depends(admin_required)
):
    """Change the security role/user type for a user."""
    from beanie import PydanticObjectId
    from app.models import Role
    try:
        user = await User.get(PydanticObjectId(user_id))
    except Exception:
        user = None
        
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "User not found."}
        )
        
    if str(user.id) == str(admin.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "You cannot change your own administrator role privileges."}
        )
        
    # Check if role exists
    role_exists = await Role.find_one(Role.key == body.userType)
    if not role_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"Role '{body.userType}' does not exist in the database."}
        )

    # Validate that non-superAdmins cannot assign the superAdmin role
    if body.userType == "superAdmin" and admin.user_type != "superAdmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Only Super Admins can assign the Super Admin role."}
        )

    user.user_type = body.userType
    user.is_admin = body.userType in ["superAdmin", "admin"]
    await user.save()
    
    return MessageResponse(
        message=f"Role status for user {user.email} updated to {role_exists.name}."
    )



@router.delete(
    "/users/{user_id}",
    response_model=MessageResponse,
    summary="Delete user account and all owned items"
)
async def delete_user(
    user_id: str,
    admin: User = Depends(admin_required)
):
    """Deactivate user, purge B2 files and delete all database documents."""
    from beanie import PydanticObjectId
    try:
        user = await User.get(PydanticObjectId(user_id))
    except Exception:
        user = None
        
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "User not found."}
        )
        
    if str(user.id) == str(admin.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "You cannot delete your own administrator account."}
        )
        
    # 1. Fetch user items
    items = await FileSystemItem.find(FileSystemItem.user_id == user_id).to_list()
    
    # 2. Delete B2 files
    from app.b2 import handle_b2_delete
    for item in items:
        if item.type == "file":
            try:
                await handle_b2_delete(item, user_id)
            except Exception:
                pass  # Ignore missing B2 files
                
    # 3. Purge DB items, partitions, and user document
    await FileSystemItem.find(FileSystemItem.user_id == user_id).delete()
    await StoragePartition.find(StoragePartition.user_id == user_id).delete()
    await user.delete()
    
    return MessageResponse(
        message=f"User {user.email} and all owned storage contents have been deleted."
    )


@router.get(
    "/db-stats",
    summary="Fetch MongoDB cluster stats telemetry"
)
async def get_db_stats(admin: User = Depends(admin_required)):
    """Fetch raw database level statistics directly from the MongoDB engine."""
    try:
        stats = await app.database.database.command("dbStats")
        # Extract/return key indicators safely
        return {
            "db": stats.get("db", ""),
            "collections": stats.get("collections", 0),
            "objects": stats.get("objects", 0),
            "avgObjSize": stats.get("avgObjSize", 0.0),
            "dataSize": stats.get("dataSize", 0),
            "storageSize": stats.get("storageSize", 0),
            "indexes": stats.get("indexes", 0),
            "indexSize": stats.get("indexSize", 0),
            "ok": stats.get("ok", 1.0)
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": f"Failed to execute dbStats query: {str(e)}"}
        )


# ── Role Management Routes ──────────────────────────────────────────────────

@router.get(
    "/roles",
    response_model=List[RoleResponse],
    summary="List all security roles"
)
async def list_roles(admin: User = Depends(admin_required)):
    """Retrieve details of all roles. Filter out superAdmin for standard Admins."""
    from app.models import Role
    roles = await Role.find_all().to_list()
    
    # Filter out superAdmin if the logged-in user is not superAdmin
    if admin.user_type != "superAdmin":
        roles = [r for r in roles if r.key != "superAdmin"]
        
    return [
        RoleResponse(
            name=r.name,
            key=r.key,
            isDefault=r.is_default,
            description=r.description,
            permissions=r.permissions
        ) for r in roles
    ]


@router.post(
    "/roles",
    response_model=RoleResponse,
    summary="Create a custom role"
)
async def create_role(
    body: CreateRoleRequest,
    admin: User = Depends(admin_required)
):
    """Create a new custom user role."""
    from app.models import Role
    
    existing = await Role.find_one(Role.key == body.key)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": f"Role key '{body.key}' already exists."}
        )
        
    if body.key in ["superAdmin", "admin", "individual"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Cannot create a custom role with a default system key name."}
        )

    # Validate that standard Admins cannot create roles with manage_roles privileges
    if admin.user_type != "superAdmin" and "manage_roles" in body.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Standard Admins cannot create roles with manage_roles permissions."}
        )

    new_role = Role(
        name=body.name,
        key=body.key,
        is_default=False,
        description=body.description,
        permissions=body.permissions
    )
    await new_role.insert()
    
    return RoleResponse(
        name=new_role.name,
        key=new_role.key,
        isDefault=new_role.is_default,
        description=new_role.description,
        permissions=new_role.permissions
    )


@router.put(
    "/roles/{key}",
    response_model=RoleResponse,
    summary="Update a role"
)
async def update_role(
    key: str,
    body: UpdateRoleRequest,
    admin: User = Depends(admin_required)
):
    """Update custom or default role details."""
    from app.models import Role
    
    role = await Role.find_one(Role.key == key)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Role not found."}
        )
        
    # Standard admins cannot modify superAdmin role
    if key == "superAdmin" and admin.user_type != "superAdmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Only Super Admins can modify the Super Admin role."}
        )

    # Admins cannot modify default Admin role permissions.
    if key == "admin" and admin.user_type != "superAdmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Admins cannot change permissions of the default system Admin role."}
        )

    # Standard Admins cannot escalate a role's permissions to "manage_roles"
    if admin.user_type != "superAdmin" and "manage_roles" in body.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Standard Admins cannot assign manage_roles permission."}
        )

    # Update role properties
    role.name = body.name
    role.description = body.description
    
    # Default roles keep their permissions or can only be edited by Super Admin
    if not role.is_default or admin.user_type == "superAdmin":
        role.permissions = body.permissions
        
    await role.save()
    
    return RoleResponse(
        name=role.name,
        key=role.key,
        isDefault=role.is_default,
        description=role.description,
        permissions=role.permissions
    )


@router.delete(
    "/roles/{key}",
    response_model=MessageResponse,
    summary="Delete a custom role"
)
async def delete_role(
    key: str,
    admin: User = Depends(admin_required)
):
    """Delete a custom role. Default system roles cannot be deleted."""
    from app.models import Role, User
    
    role = await Role.find_one(Role.key == key)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Role not found."}
        )
        
    if role.is_default:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Default system roles cannot be deleted."}
        )
        
    # Prevent standard Admins from deleting roles that require superAdmin permissions
    if admin.user_type != "superAdmin" and "manage_roles" in role.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Standard Admins cannot delete roles containing manage_roles permissions."}
        )

    # Before deleting, check if any user is currently assigned this role
    assigned_users_count = await User.find(User.user_type == key).count()
    if assigned_users_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": f"Cannot delete role. There are {assigned_users_count} user(s) currently assigned to this role."}
        )

    await role.delete()
    return MessageResponse(message=f"Custom role '{role.name}' has been deleted.")


@router.post(
    "/billing/process-expirations",
    summary="Super Admin: Trigger expiry and grace period worker",
)
async def trigger_billing_expiry_worker(
    admin: User = Depends(get_current_user),
):
    """Run the expiry worker on demand to process expired grace periods and period-end cancellations."""
    if not admin.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Administrative privileges required."},
        )

    from app.billing_service import process_expired_subscriptions
    results = await process_expired_subscriptions()
    return {
        "success": True,
        "message": "Billing expiry worker executed successfully.",
        "results": results,
    }

