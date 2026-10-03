"""Router for recurring subscription lifecycle, entitlement status, and user cancellation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.models import User, Subscription, Entitlement, PaymentTransaction
from app.billing_service import (
    create_user_subscription,
    resume_user_pending_subscription,
    cancel_user_subscription,
    sync_user_entitlement,
)
from app.billing_plans import PLAN_LIMITS

logger = logging.getLogger("subscription_routes")

router = APIRouter(prefix="/api/subscriptions", tags=["Subscriptions"])


# ── Request / Response Schemas ────────────────────────────────────────────────

class CreateSubscriptionRequest(BaseModel):
    planName: str = Field(..., description="Plan code: personal, plus, power")
    billingCycle: Optional[str] = Field("monthly", description="monthly or annual")
    returnUrl: Optional[str] = Field("http://localhost:3000/dashboard", description="Client return URL after authorization")


class CancelSubscriptionRequest(BaseModel):
    reason: Optional[str] = Field("User requested cancellation", description="Optional cancellation reason")


class SubscriptionResponse(BaseModel):
    subscriptionId: str
    planCode: str
    planName: str
    billingInterval: str
    status: str
    currentPeriodStart: datetime
    currentPeriodEnd: datetime
    nextBillingAt: Optional[datetime] = None
    cancelAtPeriodEnd: bool
    canceledAt: Optional[datetime] = None
    gracePeriodStartedAt: Optional[datetime] = None
    gracePeriodEndsAt: Optional[datetime] = None
    storageQuotaBytes: int
    canUpload: bool
    canDownload: bool


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get(
    "/pending-session",
    summary="Resume pending subscription authorization and get active session ID",
)
async def get_pending_subscription_session(
    current_user: User = Depends(get_current_user),
):
    """Fetch existing subscription session for a pending_authorization subscription without creating a duplicate."""
    res, err = await resume_user_pending_subscription(user_id=str(current_user.id))
    if err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": err},
        )
    return res


@router.post(
    "/create",
    summary="Create a recurring subscription mandate via Cashfree",
)
async def create_subscription(
    body: CreateSubscriptionRequest,
    current_user: User = Depends(get_current_user),
):
    """Initiate a recurring subscription mandate with Cashfree Subscriptions."""
    plan_code = body.planName.lower().strip()
    billing_interval = (body.billingCycle or "monthly").lower().strip()

    if billing_interval not in ["monthly", "annual"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Invalid billing interval. Choose 'monthly' or 'annual'."},
        )

    res, err = await create_user_subscription(
        user=current_user,
        plan_code=plan_code,
        billing_interval=billing_interval,
        return_url=body.returnUrl or "http://localhost:3000/dashboard",
    )

    if err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": err},
        )

    return res


@router.get(
    "/current",
    summary="Get current user subscription and entitlement status",
)
async def get_current_subscription(
    current_user: User = Depends(get_current_user),
):
    """Fetch current active subscription, period dates, grace period info, and storage entitlement."""
    user_id_str = str(current_user.id)

    # 1. Fetch Subscription document
    sub = await Subscription.find_one(
        Subscription.user_id == user_id_str,
        {"status": {"$in": ["active", "past_due", "cancel_at_period_end", "pending_authorization"]}},
    )

    # 2. Fetch or initialize Entitlement document
    entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
    if not entitlement:
        free_quota = PLAN_LIMITS.get(current_user.pricing_plan, PLAN_LIMITS["free"])
        entitlement = await sync_user_entitlement(
            user_id=user_id_str,
            plan_code=current_user.pricing_plan or "free",
            storage_quota_bytes=current_user.storage_limit_bytes or free_quota,
            billing_status=current_user.subscription_status or "active",
            can_upload=True,
            can_download=True,
        )

    if sub:
        plan_snapshot = sub.plan_snapshot or {}
        return SubscriptionResponse(
            subscriptionId=str(sub.id),
            planCode=sub.plan_id,
            planName=plan_snapshot.get("name", sub.plan_id.title()),
            billingInterval=plan_snapshot.get("billing_interval", "monthly"),
            status=sub.status,
            currentPeriodStart=sub.current_period_start,
            currentPeriodEnd=sub.current_period_end,
            nextBillingAt=sub.next_billing_at,
            cancelAtPeriodEnd=sub.cancel_at_period_end,
            canceledAt=sub.canceled_at,
            gracePeriodStartedAt=sub.grace_period_started_at,
            gracePeriodEndsAt=sub.grace_period_ends_at,
            storageQuotaBytes=entitlement.storage_quota_bytes,
            canUpload=entitlement.can_upload,
            canDownload=entitlement.can_download,
        )
    else:
        # Free / Lifetime / Legacy plan state
        return {
            "subscriptionId": None,
            "planCode": entitlement.plan_code,
            "planName": f"{entitlement.plan_code.title()} Plan",
            "billingInterval": current_user.billing_cycle or "free",
            "status": entitlement.billing_status,
            "currentPeriodStart": current_user.created_at,
            "currentPeriodEnd": current_user.subscription_expires_at or current_user.trial_expires_at or datetime.now(timezone.utc),
            "nextBillingAt": None,
            "cancelAtPeriodEnd": False,
            "canceledAt": current_user.canceled_at,
            "gracePeriodStartedAt": None,
            "gracePeriodEndsAt": entitlement.grace_period_ends_at,
            "storageQuotaBytes": entitlement.storage_quota_bytes,
            "canUpload": entitlement.can_upload,
            "canDownload": entitlement.can_download,
        }


@router.post(
    "/cancel",
    summary="Cancel active subscription at current billing period end",
)
async def cancel_subscription(
    body: Optional[CancelSubscriptionRequest] = None,
    current_user: User = Depends(get_current_user),
):
    """Cancel subscription. Access continues until the end of the paid billing period."""
    reason = body.reason if body else "User requested cancellation"
    success, err = await cancel_user_subscription(
        user_id=str(current_user.id),
        reason=reason,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": err or "Failed to cancel subscription."},
        )

    return {
        "success": True,
        "message": "Subscription scheduled for cancellation at the end of the current billing period. Full access remains active until then.",
    }


@router.get(
    "/transactions",
    summary="Get user's recurring billing transactions",
)
async def get_subscription_transactions(
    current_user: User = Depends(get_current_user),
):
    """Fetch payment transactions associated with recurring subscriptions."""
    transactions = await PaymentTransaction.find(
        PaymentTransaction.user_id == str(current_user.id)
    ).sort("-created_at").to_list()

    return [
        {
            "id": str(tx.id),
            "subscriptionId": tx.subscription_id,
            "type": tx.type,
            "amountPaise": tx.amount_paise,
            "amountInr": tx.amount_paise / 100.0,
            "currency": tx.currency,
            "status": tx.status,
            "failureCode": tx.failure_code,
            "failureMessage": tx.failure_message,
            "paidAt": tx.paid_at.isoformat() if tx.paid_at else None,
            "createdAt": tx.created_at.isoformat(),
        }
        for tx in transactions
    ]
