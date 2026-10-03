"""Core business logic for recurring subscriptions, entitlements, webhooks, and expiry processing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple
from bson import ObjectId
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.models import (
    User,
    Plan,
    Subscription,
    PaymentTransaction,
    WebhookEvent,
    Entitlement,
    BillingAuditLog,
    CancellationRecord,
    PaymentRecord,
)
from app.billing_plans import PLAN_LIMITS, PLAN_PRICING, add_calendar_interval
from app.cashfree_client import (
    verify_cashfree_webhook_signature,
    create_cashfree_recurring_subscription,
    cancel_cashfree_subscription,
    get_cashfree_subscription,
)

logger = logging.getLogger("billing_service")


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime object is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_payment_id(payment_id: Any) -> Optional[str]:
    """Normalize payment ID to non-empty string, preventing fabrication of fake IDs."""
    if payment_id is None:
        return None
    val = str(payment_id).strip()
    if not val or val.lower() in ["none", "null"]:
        return None
    return val


def _parse_timestamp(ts: Any) -> Optional[datetime]:
    """Safely parse various datetime/timestamp representations to UTC datetime."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return _to_utc(ts)
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(ts, str):
        ts_clean = ts.strip()
        if not ts_clean:
            return None
        try:
            val = float(ts_clean)
            return datetime.fromtimestamp(val, tz=timezone.utc)
        except (ValueError, OSError):
            pass
        try:
            dt = datetime.fromisoformat(ts_clean.replace("Z", "+00:00"))
            return _to_utc(dt)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(ts_clean, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
    return None


# ── Entitlement Synchronization ───────────────────────────────────────────────

async def sync_user_entitlement(
    user_id: str,
    plan_code: str,
    storage_quota_bytes: int,
    billing_status: str,
    source_subscription_id: Optional[str] = None,
    grace_period_ends_at: Optional[datetime] = None,
    can_upload: bool = True,
    can_download: bool = True,
    actor_type: str = "system",
    actor_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> Entitlement:
    """Synchronize user Entitlement record and dual-write to User.storage_limit_bytes."""
    now_utc = datetime.now(timezone.utc)
    clean_user_id = str(user_id)

    # 1. Fetch or create unique Entitlement document
    entitlement = await Entitlement.find_one(Entitlement.user_id == clean_user_id)
    before_state = entitlement.model_dump() if entitlement else None

    if entitlement:
        entitlement.plan_code = plan_code
        entitlement.storage_quota_bytes = storage_quota_bytes
        entitlement.billing_status = billing_status
        entitlement.source_subscription_id = source_subscription_id or entitlement.source_subscription_id
        entitlement.grace_period_ends_at = grace_period_ends_at
        entitlement.can_upload = can_upload
        entitlement.can_download = can_download
        entitlement.updated_at = now_utc
        await entitlement.save()
    else:
        entitlement = Entitlement(
            user_id=clean_user_id,
            source_subscription_id=source_subscription_id,
            plan_code=plan_code,
            storage_quota_bytes=storage_quota_bytes,
            billing_status=billing_status,
            can_upload=can_upload,
            can_download=can_download,
            can_manage_files=True,
            grace_period_ends_at=grace_period_ends_at,
            updated_at=now_utc,
        )
        await entitlement.insert()

    after_state = entitlement.model_dump()

    # 2. Dual-write to User model to preserve existing quota guards and user fields
    try:
        user_doc = None
        if ObjectId.is_valid(clean_user_id):
            user_doc = await User.get(ObjectId(clean_user_id))
        if not user_doc:
            user_doc = await User.find_one(User.id == clean_user_id)  # type: ignore

        if user_doc:
            user_doc.storage_limit_bytes = storage_quota_bytes
            user_doc.pricing_plan = plan_code
            user_doc.subscription_status = billing_status
            if grace_period_ends_at:
                user_doc.subscription_expires_at = grace_period_ends_at
            await user_doc.save()
    except Exception as exc:
        logger.error(f"Failed to dual-write entitlement to User {clean_user_id}: {exc}")

    # 3. Write BillingAuditLog
    audit_log = BillingAuditLog(
        user_id=clean_user_id,
        subscription_id=source_subscription_id,
        action="ENTITLEMENT_SYNCED",
        actor_type=actor_type,
        actor_id=actor_id,
        before=before_state,
        after=after_state,
        reason=f"Entitlement synchronized to plan '{plan_code}' with status '{billing_status}'",
        correlation_id=correlation_id,
        created_at=now_utc,
    )
    await audit_log.insert()

    return entitlement


# ── Subscription Lifecycle Management ──────────────────────────────────────────

async def resume_user_pending_subscription(
    user_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fetch active authorization session for a user's pending subscription without creating a duplicate."""
    user_id_str = str(user_id)
    now_utc = datetime.now(timezone.utc)

    # 1. Look up pending subscription document
    sub = await Subscription.find_one(
        Subscription.user_id == user_id_str,
        Subscription.status == "pending_authorization",
    )
    if not sub or not sub.cashfree_subscription_id:
        return None, "No pending authorization subscription found."

    # 2. Query Cashfree for the latest state of this subscription
    cf_data, cf_err = await get_cashfree_subscription(sub.cashfree_subscription_id)
    if cf_err or not cf_data:
        return None, cf_err or "Failed to retrieve subscription details from gateway."

    cf_status = (cf_data.get("status") or cf_data.get("subscription_status") or "").upper()

    if cf_status == "ACTIVE":
        # Synchronize local subscription and entitlements
        sub.status = "active"
        sub.updated_at = now_utc
        await sub.save()
        plan_snapshot = sub.plan_snapshot or {}
        await sync_user_entitlement(
            user_id=user_id_str,
            plan_code=sub.plan_id,
            storage_quota_bytes=plan_snapshot.get("storage_quota_bytes", 15 * 1024 * 1024 * 1024),
            billing_status="active",
            source_subscription_id=str(sub.id),
        )
        return {
            "subscription_id": str(sub.id),
            "cashfree_subscription_id": sub.cashfree_subscription_id,
            "status": "active",
            "message": "Subscription is already active.",
            "plan_code": sub.plan_id,
        }, None

    if cf_status in ["CANCELLED", "EXPIRED", "FAILED"]:
        sub.status = "cancelled"
        sub.updated_at = now_utc
        await sub.save()
        return None, f"Subscription mandate is {cf_status.lower()}."

    sub_session = cf_data.get("subscription_session_id") or cf_data.get("payment_session_id")
    auth_link = cf_data.get("auth_link") or cf_data.get("sub_link")
    plan_snapshot = sub.plan_snapshot or {}

    return {
        "subscription_id": str(sub.id),
        "cashfree_subscription_id": sub.cashfree_subscription_id,
        "subscription_session_id": sub_session,
        "payment_session_id": sub_session,
        "auth_link": auth_link,
        "sub_link": auth_link,
        "plan_code": sub.plan_id,
        "billing_interval": plan_snapshot.get("billing_interval", "monthly"),
        "amount": plan_snapshot.get("amount_paise", 0) / 100.0,
        "currency": plan_snapshot.get("currency", "INR"),
        "status": "pending_authorization",
    }, None


async def create_user_subscription(
    user: User,
    plan_code: str,
    billing_interval: str = "monthly",
    return_url: str = "http://localhost:3000/dashboard",
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Initiate a recurring subscription mandate with Cashfree and persist pending subscription."""
    clean_plan_code = plan_code.lower().strip()
    clean_interval = billing_interval.lower().strip()
    now_utc = datetime.now(timezone.utc)
    user_id_str = str(user.id)

    # 1. Fetch authoritative Plan from DB, fallback to configuration
    plan_doc = await Plan.find_one(
        Plan.code == clean_plan_code,
        Plan.billing_interval == clean_interval,
        Plan.is_active == True,
    )

    if plan_doc:
        amount_paise = plan_doc.amount_paise
        amount_inr = amount_paise / 100.0
        storage_quota_bytes = plan_doc.storage_quota_bytes
        plan_name = plan_doc.name
    else:
        if clean_plan_code not in PLAN_PRICING:
            return None, f"Plan '{clean_plan_code}' does not exist"
        amount_inr = float(PLAN_PRICING[clean_plan_code].get(clean_interval, 0.0))
        amount_paise = int(amount_inr * 100)
        storage_quota_bytes = PLAN_LIMITS.get(clean_plan_code, PLAN_LIMITS["free"])
        plan_name = f"{clean_plan_code.title()} Plan"

    # 2. Check if user already has an existing pending_authorization subscription
    existing_sub = await Subscription.find_one(
        Subscription.user_id == user_id_str,
        Subscription.status == "pending_authorization",
    )
    if existing_sub and existing_sub.cashfree_subscription_id:
        cf_status_data, _ = await get_cashfree_subscription(existing_sub.cashfree_subscription_id)
        if cf_status_data:
            cf_status = (cf_status_data.get("status") or cf_status_data.get("subscription_status") or "").upper()
            if cf_status == "ACTIVE":
                existing_sub.status = "active"
                existing_sub.updated_at = now_utc
                await existing_sub.save()
                await sync_user_entitlement(
                    user_id=user_id_str,
                    plan_code=existing_sub.plan_id,
                    storage_quota_bytes=existing_sub.plan_snapshot.get("storage_quota_bytes", storage_quota_bytes),
                    billing_status="active",
                    source_subscription_id=str(existing_sub.id),
                )
                return {
                    "subscription_id": str(existing_sub.id),
                    "cashfree_subscription_id": existing_sub.cashfree_subscription_id,
                    "plan_code": existing_sub.plan_id,
                    "status": "active",
                    "message": "Subscription is already active",
                }, None
            elif cf_status in ["INITIALIZED", "BANK_APPROVAL_PENDING", "PENDING"]:
                if existing_sub.plan_id == clean_plan_code and existing_sub.plan_snapshot.get("billing_interval") == clean_interval:
                    sub_session = cf_status_data.get("subscription_session_id") or cf_status_data.get("payment_session_id")
                    auth_link = cf_status_data.get("auth_link") or cf_status_data.get("sub_link")
                    return {
                        "subscription_id": str(existing_sub.id),
                        "cashfree_subscription_id": existing_sub.cashfree_subscription_id,
                        "subscription_session_id": sub_session,
                        "payment_session_id": sub_session,
                        "auth_link": auth_link,
                        "sub_link": auth_link,
                        "plan_code": clean_plan_code,
                        "billing_interval": clean_interval,
                        "amount": amount_inr,
                        "currency": "INR",
                        "status": "pending_authorization",
                    }, None
                else:
                    existing_sub.status = "cancelled"
                    existing_sub.updated_at = now_utc
                    await existing_sub.save()
            elif cf_status in ["CANCELLED", "EXPIRED", "FAILED"]:
                existing_sub.status = "cancelled"
                existing_sub.updated_at = now_utc
                await existing_sub.save()

    customer_id = getattr(user, "customer_id", None) or f"cust_{user_id_str}"
    customer_name = user.name or "GetFileNova Customer"
    customer_email = user.email
    customer_phone = getattr(user, "phone", None) or "9999999999"

    # 3. Call Cashfree Recurring Subscriptions API
    cf_data, cf_err = await create_cashfree_recurring_subscription(
        user_id=user_id_str,
        customer_id=customer_id,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        plan_code=clean_plan_code,
        plan_name=plan_name,
        billing_interval=clean_interval,
        amount_inr=amount_inr,
        return_url=return_url,
    )

    if cf_err:
        return None, cf_err

    cf_sub_id = cf_data.get("subscription_id") if cf_data else None
    period_end = add_calendar_interval(now_utc, clean_interval)

    plan_snapshot = {
        "code": clean_plan_code,
        "name": plan_name,
        "billing_interval": clean_interval,
        "amount_paise": amount_paise,
        "storage_quota_bytes": storage_quota_bytes,
        "currency": "INR",
    }

    # 4. Create Subscription document in pending_authorization state
    subscription = Subscription(
        user_id=user_id_str,
        plan_id=clean_plan_code,
        plan_snapshot=plan_snapshot,
        provider="cashfree",
        cashfree_subscription_id=cf_sub_id,
        status="pending_authorization",
        current_period_start=now_utc,
        current_period_end=period_end,
        next_billing_at=period_end,
        cancel_at_period_end=False,
        created_at=now_utc,
        updated_at=now_utc,
    )
    await subscription.insert()
    sub_id_str = str(subscription.id)

    # 4. Create initial pending PaymentTransaction
    init_tx = PaymentTransaction(
        user_id=user_id_str,
        subscription_id=sub_id_str,
        type="initial_payment",
        provider="cashfree",
        cashfree_order_id=cf_sub_id,
        amount_paise=amount_paise,
        currency="INR",
        status="pending",
        idempotency_key=f"init_sub_{cf_sub_id or sub_id_str}",
        billing_period_start=now_utc,
        billing_period_end=period_end,
        created_at=now_utc,
        updated_at=now_utc,
    )
    await init_tx.insert()

    # 5. Also write legacy PaymentRecord for backward compatibility with existing dashboard & tests
    legacy_rec = PaymentRecord(
        user_id=user_id_str,
        customer_id=customer_id,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        order_id=cf_sub_id or f"cf_sub_{sub_id_str}",
        payment_session_id=cf_data.get("payment_session_id") if cf_data else None,
        amount=amount_inr,
        currency="INR",
        plan_name=clean_plan_code,
        billing_cycle=clean_interval,
        status="PENDING",
        created_at=now_utc,
        updated_at=now_utc,
    )
    await legacy_rec.insert()

    # 6. Write Audit Log
    audit = BillingAuditLog(
        user_id=user_id_str,
        subscription_id=sub_id_str,
        action="SUBSCRIPTION_CREATED",
        actor_type="user",
        actor_id=user_id_str,
        after=subscription.model_dump(),
        reason=f"Initiated recurring subscription for {plan_name} ({clean_interval})",
        created_at=now_utc,
    )
    await audit.insert()

    # Ensure user has customer_id set
    if not user.customer_id:
        user.customer_id = customer_id
        await user.save()

    response_payload = {
        "subscription_id": sub_id_str,
        "cashfree_subscription_id": cf_sub_id,
        "subscription_session_id": cf_data.get("subscription_session_id") or cf_data.get("payment_session_id") if cf_data else None,
        "payment_session_id": cf_data.get("subscription_session_id") or cf_data.get("payment_session_id") if cf_data else None,
        "auth_link": cf_data.get("auth_link") or cf_data.get("sub_link") if cf_data else None,
        "sub_link": cf_data.get("sub_link") or cf_data.get("auth_link") if cf_data else None,
        "plan_code": clean_plan_code,
        "billing_interval": clean_interval,
        "amount": amount_inr,
        "currency": "INR",
        "status": "pending_authorization",
    }
    return response_payload, None


async def cancel_user_subscription(
    user_id: str,
    reason: Optional[str] = "User requested cancellation",
) -> Tuple[bool, Optional[str]]:
    """Cancel subscription effective at the end of the current billing period."""
    clean_user_id = str(user_id)
    now_utc = datetime.now(timezone.utc)

    # Find active, pending_authorization, or past_due subscription
    sub = await Subscription.find_one(
        Subscription.user_id == clean_user_id,
        {"status": {"$in": ["active", "past_due", "pending_authorization"]}},
    )

    if not sub:
        return False, "No active subscription found to cancel."

    before_state = sub.model_dump()

    # Mark cancellation at period end (preserves access until current_period_end)
    sub.cancel_at_period_end = True
    sub.status = "cancel_at_period_end"
    sub.canceled_at = now_utc
    sub.updated_at = now_utc
    await sub.save()

    # Optional: inform Cashfree provider if subscription ID is present
    if sub.cashfree_subscription_id:
        try:
            await cancel_cashfree_subscription(sub.cashfree_subscription_id)
        except Exception as exc:
            logger.warning(f"Could not cancel Cashfree mandate {sub.cashfree_subscription_id}: {exc}")

    # Record cancellation record for reporting
    user = None
    if ObjectId.is_valid(clean_user_id):
        user = await User.get(ObjectId(clean_user_id))
    if not user:
        user = await User.find_one(User.id == clean_user_id)  # type: ignore

    cancellation = CancellationRecord(
        user_id=clean_user_id,
        customer_name=user.name if user else "",
        customer_email=user.email if user else "",
        plan_name=sub.plan_id,
        billing_cycle=sub.plan_snapshot.get("billing_interval", "monthly"),
        is_trial=False,
        reason=reason,
        created_at=now_utc,
    )
    await cancellation.insert()

    # Audit log
    audit = BillingAuditLog(
        user_id=clean_user_id,
        subscription_id=str(sub.id),
        action="SUBSCRIPTION_CANCEL_SCHEDULED",
        actor_type="user",
        actor_id=clean_user_id,
        before=before_state,
        after=sub.model_dump(),
        reason=reason,
        created_at=now_utc,
    )
    await audit.insert()

    return True, None


# ── Webhook Processing ────────────────────────────────────────────────────────

async def process_webhook_payload(
    raw_body: bytes,
    headers: Dict[str, str],
) -> Tuple[Dict[str, Any], int]:
    """Verify and process incoming Cashfree webhook payloads idempotently."""
    signature = (
        headers.get("x-webhook-signature")
        or headers.get("x-cashfree-signature")
        or headers.get("signature")
        or ""
    )
    timestamp = (
        headers.get("x-webhook-timestamp")
        or headers.get("x-cashfree-timestamp")
        or None
    )

    # 1. Verify signature
    is_valid = verify_cashfree_webhook_signature(raw_body, signature, timestamp)
    if not is_valid:
        logger.warning(f"Invalid Cashfree webhook signature received. Signature: {signature}")
        return {"error": "Invalid webhook signature"}, 401

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        return {"error": f"Invalid JSON body: {exc}"}, 400

    now_utc = datetime.now(timezone.utc)

    # Extract event type and identifiers from Cashfree payload structure
    event_type = (
        payload.get("type")
        or payload.get("event")
        or payload.get("event_type")
        or "UNKNOWN_EVENT"
    ).upper()

    data = payload.get("data", {})
    sub_data = data.get("subscription_details") or data.get("subscription") or {}
    payment_data = data.get("payment_details") or data.get("payment") or {}
    order_data = data.get("order_details") or data.get("order") or {}

    cf_sub_id = (
        sub_data.get("subscription_id")
        or sub_data.get("cf_subscription_id")
        or sub_data.get("sub_id")
        or payload.get("subscription_id")
        or data.get("subscription_id")
    )
    cf_order_id = (
        order_data.get("order_id")
        or payment_data.get("order_id")
        or payload.get("order_id")
        or data.get("order_id")
    )
    cf_payment_id = (
        payment_data.get("payment_id")
        or payment_data.get("cf_payment_id")
        or payload.get("payment_id")
    )

    # 2. Derive unique event_id for idempotency
    event_id = (
        payload.get("event_id")
        or payload.get("eventId")
        or (f"{event_type}_{cf_sub_id or cf_order_id}_{cf_payment_id or ''}" if (cf_sub_id or cf_order_id) else None)
        or hashlib.sha256(raw_body).hexdigest()
    )

    # 3. Check WebhookEvent for duplicate delivery
    existing_event = await WebhookEvent.find_one(
        WebhookEvent.provider == "cashfree",
        WebhookEvent.event_id == event_id,
    )

    if existing_event:
        if existing_event.status in ["processed", "processing"]:
            logger.info(f"Duplicate webhook event {event_id} already {existing_event.status}. Skipping.")
            return {"status": "already_processed", "event_id": event_id}, 200

    webhook_doc = existing_event or WebhookEvent(
        provider="cashfree",
        event_id=event_id,
        event_type=event_type,
        cashfree_subscription_id=cf_sub_id,
        cashfree_order_id=cf_order_id,
        payload=payload,
        signature_verified=True,
        status="processing",
        received_at=now_utc,
    )
    if not existing_event:
        try:
            await webhook_doc.insert()
        except (DuplicateKeyError, PyMongoError) as exc:
            logger.info(f"Concurrent duplicate webhook event {event_id} captured during insert: {exc}. Returning idempotent success.")
            return {"status": "already_processed", "event_id": event_id}, 200
    else:
        webhook_doc.status = "processing"
        webhook_doc.attempts += 1
        await webhook_doc.save()

    # 4. Route and handle event based on event_type
    try:
        sub_status = (sub_data.get("subscription_status") or "").upper()
        if event_type in [
            "SUBSCRIPTION_PAYMENT_SUCCESS",
            "PAYMENT_SUCCESS_WEBHOOK",
            "SUBSCRIPTION_ACTIVATED",
            "SUBSCRIPTION_AUTH_SUCCESS",
        ] or (event_type == "SUBSCRIPTION_STATUS_CHANGE" and sub_status in ["ACTIVE", "INITIALIZED"]):
            await _handle_payment_success_webhook(
                cf_sub_id=cf_sub_id,
                cf_order_id=cf_order_id,
                cf_payment_id=cf_payment_id,
                data=data,
                event_type=event_type,
                event_id=event_id,
                raw_payload=payload,
            )
        elif event_type in [
            "SUBSCRIPTION_PAYMENT_FAILED",
            "SUBSCRIPTION_PAYMENT_DECLINED",
            "PAYMENT_FAILED_WEBHOOK",
        ]:
            await _handle_payment_failed_webhook(
                cf_sub_id=cf_sub_id,
                cf_order_id=cf_order_id,
                data=data,
                event_type=event_type,
                event_id=event_id,
                cf_payment_id=cf_payment_id,
                raw_payload=payload,
            )
        elif event_type in [
            "SUBSCRIPTION_CANCELLED",
            "SUBSCRIPTION_TERMINATED",
            "CUSTOMER_CANCELLED",
        ] or (event_type == "SUBSCRIPTION_STATUS_CHANGE" and sub_status in ["CANCELLED", "CUSTOMER_CANCELLED", "TERMINATED", "EXPIRED"]):
            await _handle_subscription_cancelled_webhook(
                cf_sub_id=cf_sub_id,
                data=data,
                event_type=event_type,
                event_id=event_id,
            )
        else:
            logger.info(f"Unhandled Cashfree webhook event type: {event_type}")

        webhook_doc.status = "processed"
        webhook_doc.processed_at = datetime.now(timezone.utc)
        await webhook_doc.save()

        return {"status": "success", "event_id": event_id, "event_type": event_type}, 200

    except Exception as exc:
        logger.error(f"Error processing webhook event {event_id}: {exc}", exc_info=True)
        webhook_doc.status = "failed"
        webhook_doc.last_error = str(exc)
        await webhook_doc.save()
        return {"error": f"Webhook processing error: {exc}"}, 500


async def _handle_payment_success_webhook(
    cf_sub_id: Optional[str],
    cf_order_id: Optional[str],
    cf_payment_id: Optional[Any],
    data: dict,
    event_type: str,
    event_id: str,
    raw_payload: Optional[dict] = None,
):
    """Handle successful initial subscription payments and recurring renewals."""
    now_utc = datetime.now(timezone.utc)
    sub = None

    if cf_sub_id:
        sub = await Subscription.find_one(Subscription.cashfree_subscription_id == str(cf_sub_id))
    if not sub and cf_order_id:
        sub = await Subscription.find_one(Subscription.cashfree_subscription_id == str(cf_order_id))

    normalized_payment_id = _normalize_payment_id(cf_payment_id)
    payment_data = data.get("payment_details") or data.get("payment") or {}
    raw_ts = (
        payment_data.get("payment_time")
        or payment_data.get("payment_completion_time")
        or data.get("payment_time")
        or data.get("payment_completion_time")
        or (raw_payload.get("payment_time") if raw_payload else None)
    )
    payment_time = _parse_timestamp(raw_ts) or now_utc

    if sub:
        plan_code = sub.plan_id
        interval = sub.plan_snapshot.get("billing_interval", "monthly")
        quota_bytes = sub.plan_snapshot.get("storage_quota_bytes", PLAN_LIMITS.get(plan_code, PLAN_LIMITS["free"]))
        amount_paise = sub.plan_snapshot.get("amount_paise", 0)

        # Advance billing period and activate using exact calendar arithmetic
        sub.status = "active"
        sub.current_period_start = payment_time
        sub.current_period_end = add_calendar_interval(payment_time, interval)
        sub.next_billing_at = sub.current_period_end
        # Clear any grace period flags if recovering
        sub.grace_period_started_at = None
        sub.grace_period_ends_at = None
        sub.updated_at = now_utc
        await sub.save()

        # 1. Reconcile pre-created pending initial_payment transaction if present
        pending_init_tx = await PaymentTransaction.find_one(
            PaymentTransaction.subscription_id == str(sub.id),
            PaymentTransaction.type == "initial_payment",
            PaymentTransaction.status == "pending",
        )

        if pending_init_tx:
            pending_init_tx.status = "success"
            if normalized_payment_id:
                pending_init_tx.cashfree_payment_id = normalized_payment_id
            pending_init_tx.paid_at = payment_time
            pending_init_tx.updated_at = now_utc
            await pending_init_tx.save()
            logger.info(
                f"Reconciled pending initial_payment transaction {pending_init_tx.id} for subscription {sub.id} (payment_id={normalized_payment_id})"
            )
        else:
            # Check if this transaction has already been recorded/processed (deduplication)
            existing_tx = None
            if normalized_payment_id:
                existing_tx = await PaymentTransaction.find_one(
                    PaymentTransaction.subscription_id == str(sub.id),
                    PaymentTransaction.cashfree_payment_id == normalized_payment_id,
                )
            if not existing_tx:
                idempotency_key = f"pay_{normalized_payment_id or event_id}"
                existing_tx = await PaymentTransaction.find_one(
                    PaymentTransaction.idempotency_key == idempotency_key
                )

            if existing_tx:
                logger.info(
                    f"PaymentTransaction {existing_tx.id} already exists for event {event_id} / payment {normalized_payment_id}. Skipping insert."
                )
            else:
                # Genuinely new recurring renewal
                idempotency_key = f"pay_{normalized_payment_id or event_id}"
                tx = PaymentTransaction(
                    user_id=sub.user_id,
                    subscription_id=str(sub.id),
                    type="renewal",
                    provider="cashfree",
                    cashfree_order_id=cf_order_id or cf_sub_id,
                    cashfree_payment_id=normalized_payment_id,
                    amount_paise=amount_paise,
                    currency="INR",
                    status="success",
                    idempotency_key=idempotency_key,
                    billing_period_start=sub.current_period_start,
                    billing_period_end=sub.current_period_end,
                    paid_at=payment_time,
                    created_at=now_utc,
                    updated_at=now_utc,
                )
                try:
                    await tx.insert()
                except (DuplicateKeyError, PyMongoError) as exc:
                    logger.info(f"Duplicate payment transaction insert ignored: {exc}")

        # Synchronize Entitlement & User storage quota
        await sync_user_entitlement(
            user_id=sub.user_id,
            plan_code=plan_code,
            storage_quota_bytes=quota_bytes,
            billing_status="active",
            source_subscription_id=str(sub.id),
            can_upload=True,
            can_download=True,
            actor_type="webhook",
            correlation_id=event_id,
        )

        # Supersede and terminate any prior active subscriptions for this user to prevent duplicate active subscriptions/charges
        prior_subs = await Subscription.find(
            Subscription.user_id == sub.user_id,
            Subscription.id != sub.id,
            {"status": {"$in": ["active", "past_due", "cancel_at_period_end", "pending_authorization"]}},
        ).to_list()

        for old_sub in prior_subs:
            old_before = old_sub.model_dump()
            old_sub.status = "superseded"
            old_sub.ended_at = now_utc
            old_sub.updated_at = now_utc
            await old_sub.save()

            if old_sub.cashfree_subscription_id and old_sub.cashfree_subscription_id != sub.cashfree_subscription_id:
                try:
                    await cancel_cashfree_subscription(old_sub.cashfree_subscription_id)
                except Exception as exc:
                    logger.warning(f"Could not cancel superseded Cashfree subscription {old_sub.cashfree_subscription_id}: {exc}")

            supersede_audit = BillingAuditLog(
                user_id=sub.user_id,
                subscription_id=str(old_sub.id),
                action="SUBSCRIPTION_SUPERSEDED",
                actor_type="system",
                before=old_before,
                after=old_sub.model_dump(),
                reason=f"Superseded by newly activated subscription {str(sub.id)} (Plan: {plan_code})",
                correlation_id=event_id,
                created_at=now_utc,
            )
            await supersede_audit.insert()

        # Also update legacy PaymentRecord if exists
        if cf_sub_id or cf_order_id:
            legacy = await PaymentRecord.find_one(
                {"order_id": {"$in": [cf_sub_id, cf_order_id]}}
            )
            if legacy:
                legacy.status = "SUCCESS"
                legacy.cf_payment_id = normalized_payment_id
                legacy.updated_at = now_utc
                legacy.subscription_expires_at = sub.current_period_end
                await legacy.save()

    else:
        # Check if this is a one-time PG order payment (legacy / lifetime checkout)
        if cf_order_id:
            legacy = await PaymentRecord.find_one(PaymentRecord.order_id == cf_order_id)
            if legacy:
                plan_name = legacy.plan_name.lower().strip()
                legacy.status = "SUCCESS"
                legacy.cf_payment_id = normalized_payment_id
                legacy.updated_at = now_utc
                legacy.subscription_expires_at = add_calendar_interval(payment_time, legacy.billing_cycle)
                await legacy.save()

                quota_bytes = PLAN_LIMITS.get(plan_name, PLAN_LIMITS["free"])
                await sync_user_entitlement(
                    user_id=legacy.user_id,
                    plan_code=plan_name,
                    storage_quota_bytes=quota_bytes,
                    billing_status="active",
                    can_upload=True,
                    can_download=True,
                    actor_type="webhook",
                    correlation_id=event_id,
                )


async def _handle_payment_failed_webhook(
    cf_sub_id: Optional[str],
    cf_order_id: Optional[str],
    data: dict,
    event_type: str,
    event_id: str,
    cf_payment_id: Optional[Any] = None,
    raw_payload: Optional[dict] = None,
):
    """Handle recurring renewal failure or initial payment failure."""
    now_utc = datetime.now(timezone.utc)
    sub = None

    if cf_sub_id:
        sub = await Subscription.find_one(Subscription.cashfree_subscription_id == str(cf_sub_id))
    if not sub and cf_order_id:
        sub = await Subscription.find_one(Subscription.cashfree_subscription_id == str(cf_order_id))

    normalized_payment_id = _normalize_payment_id(cf_payment_id)
    payment_data = data.get("payment_details") or data.get("payment") or {}
    error_data = data.get("error_details", {}) or {}
    failure_code = (
        error_data.get("error_code")
        or error_data.get("failure_reason")
        or error_data.get("payment_status")
        or payment_data.get("payment_status")
        or "PAYMENT_FAILED"
    )
    failure_message = (
        error_data.get("error_description")
        or error_data.get("failure_description")
        or error_data.get("payment_message")
        or payment_data.get("payment_message")
        or "Payment failed"
    )

    raw_ts = (
        payment_data.get("payment_time")
        or payment_data.get("payment_completion_time")
        or error_data.get("payment_time")
        or error_data.get("payment_completion_time")
        or data.get("payment_time")
        or data.get("payment_completion_time")
        or (raw_payload.get("payment_time") if raw_payload else None)
    )
    failure_time = _parse_timestamp(raw_ts) or now_utc

    if sub:
        # Enter 14-day grace period if not already in grace
        if not sub.grace_period_ends_at or sub.status != "past_due":
            sub.status = "past_due"
            sub.grace_period_started_at = failure_time
            sub.grace_period_ends_at = failure_time + timedelta(days=14)
            sub.updated_at = now_utc
            await sub.save()

        # 1. Reconcile pre-created pending initial_payment transaction if present
        pending_init_tx = await PaymentTransaction.find_one(
            PaymentTransaction.subscription_id == str(sub.id),
            PaymentTransaction.type == "initial_payment",
            PaymentTransaction.status == "pending",
        )

        if pending_init_tx:
            pending_init_tx.status = "failed"
            pending_init_tx.failure_code = str(failure_code) if failure_code else None
            pending_init_tx.failure_message = str(failure_message) if failure_message else None
            if normalized_payment_id:
                pending_init_tx.cashfree_payment_id = normalized_payment_id
            pending_init_tx.updated_at = now_utc
            await pending_init_tx.save()
            logger.info(
                f"Reconciled pending initial_payment transaction {pending_init_tx.id} to failed for subscription {sub.id}"
            )
        else:
            idempotency_key = f"fail_{event_id}"
            existing_tx = await PaymentTransaction.find_one(PaymentTransaction.idempotency_key == idempotency_key)
            if not existing_tx and normalized_payment_id:
                existing_tx = await PaymentTransaction.find_one(
                    PaymentTransaction.subscription_id == str(sub.id),
                    PaymentTransaction.cashfree_payment_id == normalized_payment_id,
                )

            if not existing_tx:
                tx = PaymentTransaction(
                    user_id=sub.user_id,
                    subscription_id=str(sub.id),
                    type="renewal",
                    provider="cashfree",
                    cashfree_order_id=cf_order_id or cf_sub_id,
                    cashfree_payment_id=normalized_payment_id,
                    amount_paise=sub.plan_snapshot.get("amount_paise", 0),
                    currency="INR",
                    status="failed",
                    failure_code=str(failure_code) if failure_code else None,
                    failure_message=str(failure_message) if failure_message else None,
                    idempotency_key=idempotency_key,
                    created_at=now_utc,
                    updated_at=now_utc,
                )
                try:
                    await tx.insert()
                except (DuplicateKeyError, PyMongoError) as exc:
                    logger.info(f"Duplicate failed payment transaction insert ignored: {exc}")

        # During 14-day grace period, can_upload remains True!
        quota_bytes = sub.plan_snapshot.get("storage_quota_bytes", PLAN_LIMITS.get(sub.plan_id, PLAN_LIMITS["free"]))
        await sync_user_entitlement(
            user_id=sub.user_id,
            plan_code=sub.plan_id,
            storage_quota_bytes=quota_bytes,
            billing_status="past_due",
            source_subscription_id=str(sub.id),
            grace_period_ends_at=sub.grace_period_ends_at,
            can_upload=True,
            can_download=True,
            actor_type="webhook",
            correlation_id=event_id,
        )


async def _handle_subscription_cancelled_webhook(
    cf_sub_id: Optional[str],
    data: dict,
    event_type: str,
    event_id: str,
):
    """Handle subscription termination notification from provider."""
    now_utc = datetime.now(timezone.utc)
    if not cf_sub_id:
        return

    sub = await Subscription.find_one(Subscription.cashfree_subscription_id == cf_sub_id)
    if sub:
        sub.status = "canceled"
        sub.ended_at = now_utc
        sub.updated_at = now_utc
        await sub.save()

        # Synchronize entitlement status
        await sync_user_entitlement(
            user_id=sub.user_id,
            plan_code=sub.plan_id,
            storage_quota_bytes=sub.plan_snapshot.get("storage_quota_bytes", PLAN_LIMITS["free"]),
            billing_status="canceled",
            source_subscription_id=str(sub.id),
            can_upload=True,
            can_download=True,
            actor_type="webhook",
            correlation_id=event_id,
        )


# ── Expiry Worker ─────────────────────────────────────────────────────────────

_expiry_lock = asyncio.Lock()


async def process_expired_subscriptions() -> Dict[str, int]:
    """Safe, idempotent background worker for expired grace periods and period-end cancellations.

    - Guaranteed not to run multiple overlapping jobs simultaneously using an asyncio.Lock.
    - If grace period expired: blocks new uploads (can_upload=False), preserves read/download (can_download=True).
    - Downgrades storage quota to free tier (15 GB) without deleting any user files.
    """
    async with _expiry_lock:
        now_utc = datetime.now(timezone.utc)
        expired_grace_count = 0
        canceled_period_count = 0

        # 1. Process subscriptions where grace period has expired
        past_due_subs = await Subscription.find(
            Subscription.status == "past_due",
            Subscription.grace_period_ends_at <= now_utc,
        ).to_list()

        for sub in past_due_subs:
            before_state = sub.model_dump()
            sub.status = "expired"
            sub.ended_at = now_utc
            sub.updated_at = now_utc
            await sub.save()

            # Downgrade entitlement: Block uploads, preserve downloads, set quota to free tier
            free_quota = PLAN_LIMITS["free"]
            await sync_user_entitlement(
                user_id=sub.user_id,
                plan_code="free",
                storage_quota_bytes=free_quota,
                billing_status="expired",
                source_subscription_id=str(sub.id),
                can_upload=False,   # Block uploads after grace expires!
                can_download=True,  # Preserve read/download!
                actor_type="expiry_worker",
                correlation_id=f"worker_grace_expire_{str(sub.id)}",
            )

            audit = BillingAuditLog(
                user_id=sub.user_id,
                subscription_id=str(sub.id),
                action="GRACE_PERIOD_EXPIRED_DOWNGRADED",
                actor_type="system",
                before=before_state,
                after=sub.model_dump(),
                reason="14-day renewal grace period expired. Uploads blocked, file downloads preserved.",
                created_at=now_utc,
            )
            await audit.insert()
            expired_grace_count += 1

        # 2. Process subscriptions scheduled for cancellation at period end that reached period_end
        canceled_subs = await Subscription.find(
            Subscription.status == "cancel_at_period_end",
            Subscription.current_period_end <= now_utc,
        ).to_list()

        for sub in canceled_subs:
            before_state = sub.model_dump()
            sub.status = "canceled"
            sub.ended_at = now_utc
            sub.updated_at = now_utc
            await sub.save()

            # Downgrade entitlement to free plan
            free_quota = PLAN_LIMITS["free"]
            await sync_user_entitlement(
                user_id=sub.user_id,
                plan_code="free",
                storage_quota_bytes=free_quota,
                billing_status="canceled",
                source_subscription_id=str(sub.id),
                can_upload=True,
                can_download=True,
                actor_type="expiry_worker",
                correlation_id=f"worker_period_end_cancel_{str(sub.id)}",
            )

            audit = BillingAuditLog(
                user_id=sub.user_id,
                subscription_id=str(sub.id),
                action="PERIOD_END_CANCELLATION_COMPLETED",
                actor_type="system",
                before=before_state,
                after=sub.model_dump(),
                reason="Subscription reached current_period_end following user cancellation request.",
                created_at=now_utc,
            )
            await audit.insert()
            canceled_period_count += 1

        logger.info(
            f"Expiry worker completed: {expired_grace_count} grace period expirations, "
            f"{canceled_period_count} period-end cancellations processed."
        )

        return {
            "expired_grace_count": expired_grace_count,
            "canceled_period_count": canceled_period_count,
            "total_processed": expired_grace_count + canceled_period_count,
        }
