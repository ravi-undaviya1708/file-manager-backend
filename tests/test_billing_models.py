"""Tests for MongoDB Billing v2 models, schema validation, constraints, and indexes."""

import pytest
from datetime import datetime, timezone, timedelta
from pymongo.errors import DuplicateKeyError
from pydantic import ValidationError

from app.models import (
    Plan,
    Subscription,
    PaymentTransaction,
    WebhookEvent,
    Entitlement,
    BillingAuditLog,
)
from app.seed import seed_billing_plans


@pytest.mark.asyncio
async def test_plan_model_crud_and_validation():
    """Verify Plan model creation, integer paise pricing, and versioning."""
    plan = Plan(
        code="personal",
        name="Personal Plan",
        billing_interval="monthly",
        amount_paise=11900,  # ₹119.00
        currency="INR",
        storage_quota_bytes=53687091200,  # 50 GB
        cashfree_plan_id="cf_plan_personal_m_01",
        is_active=True,
        version=1,
    )
    await plan.insert()

    fetched = await Plan.find_one(Plan.code == "personal", Plan.billing_interval == "monthly")
    assert fetched is not None
    assert fetched.amount_paise == 11900
    assert fetched.storage_quota_bytes == 53687091200
    assert fetched.version == 1
    assert fetched.is_active is True

    # Validate amount cannot be negative
    with pytest.raises(ValidationError):
        Plan(
            code="invalid",
            name="Invalid Plan",
            billing_interval="monthly",
            amount_paise=-500,
            storage_quota_bytes=1000,
        )

    # Validate storage quota must be greater than 0
    with pytest.raises(ValidationError):
        Plan(
            code="invalid",
            name="Invalid Plan",
            billing_interval="monthly",
            amount_paise=1000,
            storage_quota_bytes=0,
        )


@pytest.mark.asyncio
async def test_subscription_model_and_lifecycle():
    """Verify Subscription model, plan snapshots, and grace periods."""
    now = datetime.now(timezone.utc)
    period_end = now + timedelta(days=30)
    grace_end = period_end + timedelta(days=14)

    plan_snapshot = {
        "code": "plus",
        "name": "Plus Plan",
        "billing_interval": "monthly",
        "amount_paise": 29900,
        "storage_quota_bytes": 214748364800,
        "version": 1,
    }

    sub = Subscription(
        user_id="user_sub_test_01",
        plan_id="plus",
        plan_snapshot=plan_snapshot,
        provider="cashfree",
        cashfree_subscription_id="cf_sub_mandate_987654",
        status="active",
        current_period_start=now,
        current_period_end=period_end,
        next_billing_at=period_end,
        cancel_at_period_end=False,
        grace_period_started_at=None,
        grace_period_ends_at=grace_end,
    )
    await sub.insert()

    fetched = await Subscription.find_one(Subscription.user_id == "user_sub_test_01")
    assert fetched is not None
    assert fetched.status == "active"
    assert fetched.plan_snapshot["amount_paise"] == 29900
    assert fetched.cashfree_subscription_id == "cf_sub_mandate_987654"

    # Status transition to cancel_at_period_end
    fetched.cancel_at_period_end = True
    fetched.canceled_at = datetime.now(timezone.utc)
    fetched.status = "cancel_at_period_end"
    await fetched.save()

    updated = await Subscription.get(fetched.id)
    assert updated.status == "cancel_at_period_end"
    assert updated.cancel_at_period_end is True


@pytest.mark.asyncio
async def test_payment_transaction_model():
    """Verify PaymentTransaction model, amounts in paise, and transaction types."""
    tx = PaymentTransaction(
        user_id="user_tx_test_01",
        subscription_id="sub_ref_123",
        type="initial_payment",
        provider="cashfree",
        cashfree_order_id="cf_ord_001122",
        cashfree_payment_id="cf_pay_998877",
        amount_paise=29900,
        currency="INR",
        status="success",
        idempotency_key="idemp_key_unique_001",
        paid_at=datetime.now(timezone.utc),
    )
    await tx.insert()

    fetched = await PaymentTransaction.find_one(PaymentTransaction.idempotency_key == "idemp_key_unique_001")
    assert fetched is not None
    assert fetched.amount_paise == 29900
    assert fetched.status == "success"
    assert fetched.type == "initial_payment"


@pytest.mark.asyncio
async def test_webhook_event_model():
    """Verify WebhookEvent model for asynchronous idempotent event processing."""
    payload = {
        "event_time": "2026-09-30T01:30:00Z",
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "data": {
            "subscription": {"subscription_id": "cf_sub_123"},
            "payment": {"payment_id": "cf_pay_456", "payment_amount": 299.0},
        },
    }

    event = WebhookEvent(
        provider="cashfree",
        event_id="evt_cf_test_999999",
        event_type="SUBSCRIPTION_PAYMENT_SUCCESS",
        cashfree_subscription_id="cf_sub_123",
        cashfree_order_id=None,
        payload=payload,
        signature_verified=True,
        status="received",
        attempts=1,
    )
    await event.insert()

    fetched = await WebhookEvent.find_one(WebhookEvent.event_id == "evt_cf_test_999999")
    assert fetched is not None
    assert fetched.signature_verified is True
    assert fetched.status == "received"
    assert fetched.payload["type"] == "SUBSCRIPTION_PAYMENT_SUCCESS"


@pytest.mark.asyncio
async def test_entitlement_model():
    """Verify Entitlement model, quota enforcement flags, and retention fields."""
    now = datetime.now(timezone.utc)
    retention_cutoff = now + timedelta(days=90)

    entitlement = Entitlement(
        user_id="user_ent_test_01",
        source_subscription_id="sub_test_01",
        plan_code="plus",
        storage_quota_bytes=214748364800,  # 200 GB
        billing_status="active",
        can_upload=True,
        can_download=True,
        can_manage_files=True,
        retention_deadline=retention_cutoff,
    )
    await entitlement.insert()

    fetched = await Entitlement.find_one(Entitlement.user_id == "user_ent_test_01")
    assert fetched is not None
    assert fetched.plan_code == "plus"
    assert fetched.storage_quota_bytes == 214748364800
    assert fetched.can_upload is True
    assert fetched.can_download is True

    # Test downgrade to read-only state on over-quota / suspension
    fetched.can_upload = False
    fetched.billing_status = "past_due"
    fetched.over_quota_since = now
    await fetched.save()

    updated = await Entitlement.get(fetched.id)
    assert updated.can_upload is False
    assert updated.can_download is True
    assert updated.billing_status == "past_due"


@pytest.mark.asyncio
async def test_billing_audit_log_model():
    """Verify append-only BillingAuditLog model."""
    audit = BillingAuditLog(
        user_id="user_audit_01",
        subscription_id="sub_audit_01",
        action="SUBSCRIPTION_ACTIVATED",
        actor_type="webhook",
        actor_id="cashfree_gateway",
        before={"status": "pending_authorization"},
        after={"status": "active"},
        reason="Payment confirmed by Cashfree webhook",
        correlation_id="corr_trace_123456",
    )
    await audit.insert()

    fetched = await BillingAuditLog.find_one(BillingAuditLog.correlation_id == "corr_trace_123456")
    assert fetched is not None
    assert fetched.action == "SUBSCRIPTION_ACTIVATED"
    assert fetched.actor_type == "webhook"
    assert fetched.before["status"] == "pending_authorization"
    assert fetched.after["status"] == "active"


@pytest.mark.asyncio
async def test_seed_billing_plans():
    """Verify that seed_billing_plans initializes all required standard tiers."""
    await seed_billing_plans()

    plans = await Plan.find_all().to_list()
    assert len(plans) >= 8

    # Verify specific plan entries
    free_plan = await Plan.find_one(Plan.code == "free", Plan.billing_interval == "free")
    assert free_plan is not None
    assert free_plan.amount_paise == 0
    assert free_plan.storage_quota_bytes == 16106127360

    personal_m = await Plan.find_one(Plan.code == "personal", Plan.billing_interval == "monthly")
    assert personal_m is not None
    assert personal_m.amount_paise == 11900
    assert personal_m.storage_quota_bytes == 53687091200

    power_life = await Plan.find_one(Plan.code == "power", Plan.billing_interval == "lifetime")
    assert power_life is not None
    assert power_life.amount_paise == 1999000
    assert power_life.storage_quota_bytes == 1099511627776

    # Calling seed again should be idempotent
    await seed_billing_plans()
    plans_after = await Plan.find_all().to_list()
    assert len(plans_after) == len(plans)
