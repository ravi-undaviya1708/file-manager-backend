"""Comprehensive tests for Phase 4 Recurring Billing Core with Cashfree Subscriptions.

Tests cover:
- Cashfree recurring subscription client & creation
- Webhook signature verification (HMAC-SHA256 hex & base64, timestamped)
- Webhook event idempotency via WebhookEvent
- Initial payment activation and recurring renewals
- Renewal failure & 14-day grace period entry (preserving uploads)
- Grace period recovery on payment success
- User cancellation at billing period end
- Background expiry worker (grace expiration -> block upload, preserve download)
- Entitlement synchronization & quota enforcement
- Legacy one-time payment backwards compatibility
"""

import hmac
import hashlib
import base64
import json
import pytest
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import (
    User,
    Plan,
    Subscription,
    PaymentTransaction,
    WebhookEvent,
    Entitlement,
    BillingAuditLog,
    PaymentRecord,
    CancellationRecord,
)
from app.cashfree_client import (
    verify_cashfree_webhook_signature,
    create_cashfree_recurring_subscription,
    cancel_cashfree_subscription,
)
from app.billing_service import (
    sync_user_entitlement,
    create_user_subscription,
    cancel_user_subscription,
    process_webhook_payload,
    process_expired_subscriptions,
)
from app.billing_plans import PLAN_LIMITS, PLAN_PRICING
from app.auth import create_access_token


def _to_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@pytest.fixture
def test_secret():
    return "test_cf_webhook_secret_key_12345"



@pytest.mark.asyncio
async def test_webhook_signature_verification(monkeypatch, test_secret):
    """Test HMAC-SHA256 signature verification for Cashfree webhooks, including replay window checks and fail-closed security."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)
    monkeypatch.setattr(settings, "CASHFREE_SECRET_KEY", test_secret)

    raw_body = json.dumps({"type": "SUBSCRIPTION_PAYMENT_SUCCESS", "data": {"subscription": {"subscription_id": "sub_123"}}}).encode("utf-8")
    now_ts = int(datetime.now(timezone.utc).timestamp())
    current_timestamp = str(now_ts)

    # 1. Valid Hex Signature with Current Timestamp
    msg_with_ts = current_timestamp.encode("utf-8") + raw_body
    valid_hex_sig = hmac.new(test_secret.encode("utf-8"), msg_with_ts, hashlib.sha256).hexdigest()
    assert verify_cashfree_webhook_signature(raw_body, valid_hex_sig, timestamp=current_timestamp) is True

    # 2. Valid Base64 Signature with Current Timestamp
    valid_b64_sig = base64.b64encode(hmac.new(test_secret.encode("utf-8"), msg_with_ts, hashlib.sha256).digest()).decode("utf-8")
    assert verify_cashfree_webhook_signature(raw_body, valid_b64_sig, timestamp=current_timestamp) is True

    # 3. Valid Signature without Timestamp (raw_body only)
    valid_raw_sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    assert verify_cashfree_webhook_signature(raw_body, valid_raw_sig, timestamp=None) is True

    # 4. Invalid Signature
    assert verify_cashfree_webhook_signature(raw_body, "invalid_tampered_signature", timestamp=current_timestamp) is False

    # 5. Expired Timestamp (> 300s old) Replay Attempt
    expired_timestamp = str(now_ts - 600)  # 10 minutes ago
    expired_msg = expired_timestamp.encode("utf-8") + raw_body
    expired_sig = hmac.new(test_secret.encode("utf-8"), expired_msg, hashlib.sha256).hexdigest()
    assert verify_cashfree_webhook_signature(raw_body, expired_sig, timestamp=expired_timestamp) is False

    # 6. Future Timestamp (> 300s into future)
    future_timestamp = str(now_ts + 600)
    future_msg = future_timestamp.encode("utf-8") + raw_body
    future_sig = hmac.new(test_secret.encode("utf-8"), future_msg, hashlib.sha256).hexdigest()
    assert verify_cashfree_webhook_signature(raw_body, future_sig, timestamp=future_timestamp) is False

    # 7. Fail closed if secret is missing or empty
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", "")
    monkeypatch.setattr(settings, "CASHFREE_SECRET_KEY", "")
    assert verify_cashfree_webhook_signature(raw_body, valid_hex_sig, timestamp=current_timestamp) is False
    assert verify_cashfree_webhook_signature(raw_body, "mock_signature", timestamp=current_timestamp) is False


@pytest.mark.asyncio
async def test_subscription_creation_flow():
    """Verify subscription creation, document persistence, and audit logging."""
    # Seed plan
    plan = Plan(
        code="personal",
        name="Personal Plan",
        billing_interval="monthly",
        amount_paise=11900,
        currency="INR",
        storage_quota_bytes=53687091200,
        is_active=True,
    )
    await plan.insert()

    user = User(
        email="subscriber@example.com",
        name="Subscriber Test",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()

    res, err = await create_user_subscription(
        user=user,
        plan_code="personal",
        billing_interval="monthly",
        return_url="http://localhost:3000/dashboard",
    )

    assert err is None
    assert res is not None
    assert res["plan_code"] == "personal"
    assert res["billing_interval"] == "monthly"
    assert res["amount"] == 119.0
    assert res["status"] == "pending_authorization"

    # Verify Subscription document created
    sub = await Subscription.find_one(Subscription.user_id == str(user.id))
    assert sub is not None
    assert sub.status == "pending_authorization"
    assert sub.plan_id == "personal"
    assert sub.plan_snapshot["amount_paise"] == 11900
    assert sub.plan_snapshot["storage_quota_bytes"] == 53687091200

    # Verify initial PaymentTransaction created
    tx = await PaymentTransaction.find_one(PaymentTransaction.user_id == str(user.id))
    assert tx is not None
    assert tx.type == "initial_payment"
    assert tx.status == "pending"
    assert tx.amount_paise == 11900

    # Verify BillingAuditLog recorded
    audit = await BillingAuditLog.find_one(BillingAuditLog.user_id == str(user.id), BillingAuditLog.action == "SUBSCRIPTION_CREATED")
    assert audit is not None


@pytest.mark.asyncio
async def test_webhook_idempotency(monkeypatch, test_secret):
    """Verify webhook deduplication using WebhookEvent collection."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    payload = {
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "event_id": "evt_unique_1001",
        "data": {
            "subscription": {"subscription_id": "cf_sub_test_001"},
            "payment": {"payment_id": "cf_pay_test_001", "payment_status": "SUCCESS"},
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    # 1. First webhook delivery -> processed
    res1, code1 = await process_webhook_payload(raw_body, headers)
    assert code1 == 200
    assert res1["status"] == "success"

    # Verify WebhookEvent stored
    evt = await WebhookEvent.find_one(WebhookEvent.event_id == "evt_unique_1001")
    assert evt is not None
    assert evt.status == "processed"

    # 2. Duplicate webhook delivery -> skipped idempotently
    res2, code2 = await process_webhook_payload(raw_body, headers)
    assert code2 == 200
    assert res2["status"] == "already_processed"


@pytest.mark.asyncio
async def test_subscription_activation_and_entitlement_sync(monkeypatch, test_secret):
    """Verify successful payment webhook activates subscription and syncs storage entitlement."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="activate@example.com",
        name="Activate User",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)

    # Create pending subscription
    now_utc = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user_id_str,
        plan_id="plus",
        plan_snapshot={
            "code": "plus",
            "name": "Plus Plan",
            "billing_interval": "monthly",
            "amount_paise": 29900,
            "storage_quota_bytes": 214748364800,  # 200 GB
        },
        provider="cashfree",
        cashfree_subscription_id="cf_sub_activate_123",
        status="pending_authorization",
        current_period_start=now_utc,
        current_period_end=now_utc + timedelta(days=30),
    )
    await sub.insert()

    # Incoming webhook payload
    payload = {
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "event_id": "evt_act_999",
        "data": {
            "subscription": {"subscription_id": "cf_sub_activate_123"},
            "payment": {"payment_id": "cf_pay_999", "payment_status": "SUCCESS"},
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    res, code = await process_webhook_payload(raw_body, headers)
    assert code == 200

    # Verify Subscription updated to active
    updated_sub = await Subscription.get(sub.id)
    assert updated_sub.status == "active"
    assert updated_sub.grace_period_ends_at is None

    # Verify Entitlement synchronized
    entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
    assert entitlement is not None
    assert entitlement.plan_code == "plus"
    assert entitlement.storage_quota_bytes == 214748364800
    assert entitlement.billing_status == "active"
    assert entitlement.can_upload is True
    assert entitlement.can_download is True

    # Verify User dual-written
    updated_user = await User.get(user.id)
    assert updated_user.storage_limit_bytes == 214748364800
    assert updated_user.pricing_plan == "plus"
    assert updated_user.subscription_status == "active"


@pytest.mark.asyncio
async def test_renewal_failure_enters_14_day_grace_period(monkeypatch, test_secret):
    """Verify renewal payment failure enters 14-day grace period while keeping can_upload=True."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="grace@example.com",
        name="Grace User",
        pricing_plan="power",
        storage_limit_bytes=1099511627776,  # 1 TB
    )
    await user.insert()
    user_id_str = str(user.id)

    now_utc = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user_id_str,
        plan_id="power",
        plan_snapshot={
            "code": "power",
            "name": "Power Plan",
            "billing_interval": "monthly",
            "amount_paise": 99900,
            "storage_quota_bytes": 1099511627776,
        },
        provider="cashfree",
        cashfree_subscription_id="cf_sub_power_grace",
        status="active",
        current_period_start=now_utc - timedelta(days=30),
        current_period_end=now_utc,
    )
    await sub.insert()

    # Failed renewal webhook
    payload = {
        "type": "SUBSCRIPTION_PAYMENT_FAILED",
        "event_id": "evt_fail_111",
        "data": {
            "subscription": {"subscription_id": "cf_sub_power_grace"},
            "payment": {"payment_id": "cf_pay_failed_111"},
            "error_details": {"error_code": "INSUFFICIENT_FUNDS", "error_description": "Card declined"},
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    res, code = await process_webhook_payload(raw_body, headers)
    assert code == 200

    # Verify subscription is now past_due with 14-day grace period
    updated_sub = await Subscription.get(sub.id)
    assert updated_sub.status == "past_due"
    assert updated_sub.grace_period_started_at is not None
    assert updated_sub.grace_period_ends_at is not None
    # Check that grace period is ~14 days from now
    diff_days = (_to_utc(updated_sub.grace_period_ends_at) - now_utc).total_seconds() / 86400
    assert 13.9 <= diff_days <= 14.1

    # Verify Entitlement preserves upload and download access during grace
    entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
    assert entitlement.billing_status == "past_due"
    assert entitlement.can_upload is True   # Uploads preserved during 14-day grace!
    assert entitlement.can_download is True

    # Verify failed transaction recorded
    tx = await PaymentTransaction.find_one(PaymentTransaction.idempotency_key == "fail_evt_fail_111")
    assert tx is not None
    assert tx.status == "failed"
    assert tx.failure_code == "INSUFFICIENT_FUNDS"


@pytest.mark.asyncio
async def test_grace_period_recovery(monkeypatch, test_secret):
    """Verify successful payment recovers subscription from past_due state and clears grace period."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="recovery@example.com",
        name="Recovery User",
        pricing_plan="personal",
        storage_limit_bytes=53687091200,
    )
    await user.insert()
    user_id_str = str(user.id)

    now_utc = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user_id_str,
        plan_id="personal",
        plan_snapshot={
            "code": "personal",
            "name": "Personal Plan",
            "billing_interval": "monthly",
            "amount_paise": 11900,
            "storage_quota_bytes": 53687091200,
        },
        provider="cashfree",
        cashfree_subscription_id="cf_sub_recover",
        status="past_due",
        grace_period_started_at=now_utc - timedelta(days=2),
        grace_period_ends_at=now_utc + timedelta(days=12),
        current_period_start=now_utc - timedelta(days=32),
        current_period_end=now_utc - timedelta(days=2),
    )
    await sub.insert()

    # Recovery webhook
    payload = {
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "event_id": "evt_recover_222",
        "data": {
            "subscription": {"subscription_id": "cf_sub_recover"},
            "payment": {"payment_id": "cf_pay_recovered_222", "payment_status": "SUCCESS"},
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    res, code = await process_webhook_payload(raw_body, headers)
    assert code == 200

    # Verify recovery: status back to active, grace period cleared
    updated_sub = await Subscription.get(sub.id)
    assert updated_sub.status == "active"
    assert updated_sub.grace_period_started_at is None
    assert updated_sub.grace_period_ends_at is None

    # Entitlement restored to active
    entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
    assert entitlement.billing_status == "active"
    assert entitlement.can_upload is True


@pytest.mark.asyncio
async def test_user_cancellation_at_period_end():
    """Verify cancellation marks cancel_at_period_end without immediately revoking access."""
    user = User(
        email="cancel@example.com",
        name="Cancel User",
        pricing_plan="plus",
        storage_limit_bytes=214748364800,
    )
    await user.insert()
    user_id_str = str(user.id)

    now_utc = datetime.now(timezone.utc)
    future_end = now_utc + timedelta(days=18)

    sub = Subscription(
        user_id=user_id_str,
        plan_id="plus",
        plan_snapshot={"code": "plus", "storage_quota_bytes": 214748364800, "billing_interval": "monthly"},
        provider="cashfree",
        cashfree_subscription_id="cf_sub_cancel_me",
        status="active",
        current_period_start=now_utc - timedelta(days=12),
        current_period_end=future_end,
    )
    await sub.insert()

    success, err = await cancel_user_subscription(user_id_str, reason="Too expensive")
    assert success is True
    assert err is None

    updated_sub = await Subscription.get(sub.id)
    assert updated_sub.cancel_at_period_end is True
    assert updated_sub.status == "cancel_at_period_end"
    assert updated_sub.canceled_at is not None
    assert _to_utc(updated_sub.current_period_end).replace(microsecond=0) == future_end.replace(microsecond=0)  # Paid period end unchanged!

    # Cancellation record logged
    cancel_rec = await CancellationRecord.find_one(CancellationRecord.user_id == user_id_str)
    assert cancel_rec is not None
    assert cancel_rec.reason == "Too expensive"


@pytest.mark.asyncio
async def test_expiry_worker_processes_expired_grace_period():
    """Verify background expiry worker downgrades expired grace periods: blocks upload, keeps download."""
    user = User(
        email="expired@example.com",
        name="Expired User",
        pricing_plan="personal",
        storage_limit_bytes=53687091200,
    )
    await user.insert()
    user_id_str = str(user.id)

    # Initial Entitlement
    await sync_user_entitlement(
        user_id=user_id_str,
        plan_code="personal",
        storage_quota_bytes=53687091200,
        billing_status="past_due",
        can_upload=True,
    )

    now_utc = datetime.now(timezone.utc)
    # Grace period expired 1 hour ago
    sub = Subscription(
        user_id=user_id_str,
        plan_id="personal",
        plan_snapshot={"code": "personal", "storage_quota_bytes": 53687091200},
        provider="cashfree",
        cashfree_subscription_id="cf_sub_grace_expired",
        status="past_due",
        grace_period_started_at=now_utc - timedelta(days=15),
        grace_period_ends_at=now_utc - timedelta(hours=1),
        current_period_start=now_utc - timedelta(days=45),
        current_period_end=now_utc - timedelta(days=15),
    )
    await sub.insert()

    # Run expiry worker
    stats = await process_expired_subscriptions()
    assert stats["expired_grace_count"] >= 1

    # Verify subscription is now expired
    updated_sub = await Subscription.get(sub.id)
    assert updated_sub.status == "expired"
    assert updated_sub.ended_at is not None

    # Verify Entitlement: can_upload is False, can_download is True, quota downgraded to 15 GB
    entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
    assert entitlement.billing_status == "expired"
    assert entitlement.can_upload is False   # Uploads blocked!
    assert entitlement.can_download is True  # Downloads preserved!
    assert entitlement.storage_quota_bytes == PLAN_LIMITS["free"]

    # Verify User dual-write
    updated_user = await User.get(user.id)
    assert updated_user.storage_limit_bytes == PLAN_LIMITS["free"]
    assert updated_user.subscription_status == "expired"


@pytest.mark.asyncio
async def test_expiry_worker_processes_period_end_cancellation():
    """Verify background worker terminates cancel_at_period_end subscriptions once period_end is reached."""
    user = User(
        email="period_end@example.com",
        name="Period End User",
        pricing_plan="plus",
        storage_limit_bytes=214748364800,
    )
    await user.insert()
    user_id_str = str(user.id)

    now_utc = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user_id_str,
        plan_id="plus",
        plan_snapshot={"code": "plus", "storage_quota_bytes": 214748364800},
        provider="cashfree",
        cashfree_subscription_id="cf_sub_period_cancel",
        status="cancel_at_period_end",
        cancel_at_period_end=True,
        canceled_at=now_utc - timedelta(days=10),
        current_period_start=now_utc - timedelta(days=40),
        current_period_end=now_utc - timedelta(hours=2),  # Period ended 2 hours ago
    )
    await sub.insert()

    stats = await process_expired_subscriptions()
    assert stats["canceled_period_count"] >= 1

    updated_sub = await Subscription.get(sub.id)
    assert updated_sub.status == "canceled"
    assert updated_sub.ended_at is not None

    entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
    assert entitlement.billing_status == "canceled"
    assert entitlement.storage_quota_bytes == PLAN_LIMITS["free"]


@pytest.mark.asyncio
async def test_subscription_api_endpoints():
    """Test HTTP API routes: create, current status, cancel, and admin trigger."""
    user = User(
        email="api_sub@example.com",
        name="API Sub User",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
        is_admin=True,
    )
    await user.insert()
    token = create_access_token({"sub": str(user.id)})
    auth_headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Create recurring subscription
        create_res = await client.post(
            "/api/subscriptions/create",
            json={"planName": "personal", "billingCycle": "monthly"},
            headers=auth_headers,
        )
        assert create_res.status_code == 200
        create_data = create_res.json()
        assert create_data["plan_code"] == "personal"
        assert create_data["status"] == "pending_authorization"

        # 2. Get current subscription status
        curr_res = await client.get("/api/subscriptions/current", headers=auth_headers)
        assert curr_res.status_code == 200
        curr_data = curr_res.json()
        assert curr_data["planCode"] == "personal"
        assert curr_data["canUpload"] is True

        # 3. Cancel subscription
        cancel_res = await client.post(
            "/api/subscriptions/cancel",
            json={"reason": "Testing cancellation"},
            headers=auth_headers,
        )
        assert cancel_res.status_code == 200
        assert cancel_res.json()["success"] is True

        # 4. Trigger admin expiry worker
        admin_res = await client.post(
            "/api/admin/billing/process-expirations",
            headers=auth_headers,
        )
        assert admin_res.status_code == 200
        assert admin_res.json()["success"] is True
        assert "results" in admin_res.json()


@pytest.mark.asyncio
async def test_webhook_endpoint_invalid_signature(monkeypatch, test_secret):
    """Test HTTP POST /api/payments/webhook returns 401 on invalid signature."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/api/payments/webhook",
            content=b'{"type": "SUBSCRIPTION_PAYMENT_SUCCESS"}',
            headers={"x-webhook-signature": "bogus_signature_123"},
        )
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_webhook_subscription_cancelled_event(monkeypatch, test_secret):
    """Test handling SUBSCRIPTION_CANCELLED webhook event."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="cancelled_sub@example.com",
        name="Cancelled Sub User",
        pricing_plan="personal",
        storage_limit_bytes=53687091200,
    )
    await user.insert()
    user_id_str = str(user.id)

    now_utc = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user_id_str,
        plan_id="personal",
        plan_snapshot={"code": "personal", "storage_quota_bytes": 53687091200},
        provider="cashfree",
        cashfree_subscription_id="cf_sub_to_terminate",
        status="active",
        current_period_start=now_utc - timedelta(days=10),
        current_period_end=now_utc + timedelta(days=20),
    )
    await sub.insert()

    payload = {
        "type": "SUBSCRIPTION_CANCELLED",
        "event_id": "evt_term_1234",
        "data": {"subscription": {"subscription_id": "cf_sub_to_terminate"}},
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    res, code = await process_webhook_payload(raw_body, headers)
    assert code == 200

    updated_sub = await Subscription.get(sub.id)
    assert updated_sub.status == "canceled"
    assert updated_sub.ended_at is not None

    entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
    assert entitlement.billing_status == "canceled"


@pytest.mark.asyncio
async def test_upload_quota_enforcement_with_entitlement(monkeypatch):
    """Test upload route blocks uploads when Entitlement.can_upload is False."""
    from unittest.mock import MagicMock
    import app.b2 as b2_module
    monkeypatch.setattr(b2_module, "upload_b2_file_from_path", MagicMock(return_value=True))

    user = User(
        email="upload_tester@example.com",
        name="Upload Tester",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    token = create_access_token({"sub": user_id_str})
    auth_headers = {"Authorization": f"Bearer {token}"}

    # 1. When Entitlement blocks upload (grace period expired)
    await sync_user_entitlement(
        user_id=user_id_str,
        plan_code="free",
        storage_quota_bytes=16106127360,
        billing_status="expired",
        can_upload=False,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        files = {"file": ("test.txt", b"Hello World", "text/plain")}
        res = await client.post("/api/files/upload", files=files, headers=auth_headers)
        assert res.status_code == 403
        assert "grace period has expired" in res.json().get("detail", {}).get("error", "")

    # 2. When Entitlement allows upload
    await sync_user_entitlement(
        user_id=user_id_str,
        plan_code="free",
        storage_quota_bytes=16106127360,
        billing_status="active",
        can_upload=True,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        files = {"file": ("test2.txt", b"Hello World 2", "text/plain")}
        res = await client.post("/api/files/upload", files=files, headers=auth_headers)
        assert res.status_code == 201
        assert res.json()["name"] == "test2.txt"


@pytest.mark.asyncio
async def test_legacy_one_time_payment_compatibility():
    """Verify legacy one-time PG orders create PaymentRecord, upgrade user, and sync Entitlement without recurring mandates."""
    user = User(
        email="legacy_onetime@example.com",
        name="Legacy Customer",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    token = create_access_token({"sub": user_id_str})
    auth_headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Create order
        order_res = await client.post(
            "/api/payments/create-order",
            json={"planName": "personal", "billingCycle": "annual"},
            headers=auth_headers,
        )
        assert order_res.status_code == 200
        order_data = order_res.json()
        order_id = order_data["orderId"]

        # 2. Verify payment
        verify_res = await client.post(
            "/api/payments/verify-payment",
            json={
                "orderId": order_id,
                "planName": "personal",
                "billingCycle": "annual",
                "paymentId": "cf_pay_legacy_001",
            },
            headers=auth_headers,
        )
        assert verify_res.status_code == 200
        verify_data = verify_res.json()
        assert verify_data["success"] is True
        assert verify_data["storageLimitBytes"] == 50 * 1024 * 1024 * 1024

        # 3. Check legacy PaymentRecord
        record = await PaymentRecord.find_one(PaymentRecord.order_id == order_id)
        assert record is not None
        assert record.status == "SUCCESS"
        assert record.amount == 1190.0

        # 4. Check Entitlement
        entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
        assert entitlement is not None
        assert entitlement.plan_code == "personal"
        assert entitlement.billing_status == "active"
        assert entitlement.storage_quota_bytes == 53687091200


@pytest.mark.asyncio
async def test_concurrent_duplicate_webhook_delivery(monkeypatch, test_secret):
    """Verify concurrent duplicate webhook deliveries do not trigger 500 DuplicateKeyError and return 200 idempotently."""
    import asyncio
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    now_ts = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "event_id": "evt_concurrent_race_999",
        "data": {
            "subscription": {"subscription_id": "cf_sub_concurrent_001"},
            "payment": {"payment_id": "cf_pay_concurrent_001", "payment_status": "SUCCESS"},
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    msg_with_ts = str(now_ts).encode("utf-8") + raw_body
    sig = hmac.new(test_secret.encode("utf-8"), msg_with_ts, hashlib.sha256).hexdigest()
    headers = {
        "x-webhook-signature": sig,
        "x-webhook-timestamp": str(now_ts),
    }

    # Dispatch 5 simultaneous concurrent requests with the exact same event_id
    results = await asyncio.gather(
        process_webhook_payload(raw_body, headers),
        process_webhook_payload(raw_body, headers),
        process_webhook_payload(raw_body, headers),
        process_webhook_payload(raw_body, headers),
        process_webhook_payload(raw_body, headers),
    )

    # Every concurrent request must receive HTTP 200 (either "success" or "already_processed")
    for res_dict, status_code in results:
        assert status_code == 200
        assert res_dict.get("status") in ["success", "already_processed"]

    # Verify only 1 WebhookEvent document was inserted into MongoDB
    event_count = await WebhookEvent.find(WebhookEvent.event_id == "evt_concurrent_race_999").count()
    assert event_count == 1


@pytest.mark.asyncio
async def test_workspace_file_creation_blocked_when_entitlement_upload_disabled(monkeypatch):
    """Verify workspace file creation respects Entitlement.can_upload and storage quotas."""
    from unittest.mock import AsyncMock
    import app.workspace_routes as ws_mod
    monkeypatch.setattr(ws_mod, "upload_b2_file_async", AsyncMock(return_value=None))

    from app.models import FileSystemItem

    user = User(
        email="workspace_quota@example.com",
        name="Workspace Quota Tester",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    token = create_access_token({"sub": user_id_str})
    auth_headers = {"Authorization": f"Bearer {token}"}

    # Create root folder
    root_folder = FileSystemItem(
        name="ProjectRoot",
        type="folder",
        user_id=user_id_str,
        parent_id=None,
    )
    await root_folder.insert()
    folder_id = str(root_folder.id)

    # 1. When can_upload is False (expired grace period), workspace file creation must fail with 403
    await sync_user_entitlement(
        user_id=user_id_str,
        plan_code="free",
        storage_quota_bytes=16106127360,
        billing_status="expired",
        can_upload=False,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            f"/api/workspace/{folder_id}/file",
            json={"name": "main.py", "content": "print('hello')", "parentId": folder_id},
            headers=auth_headers,
        )
        assert res.status_code == 403
        assert "grace period has expired" in res.json().get("detail", {}).get("error", "")

        # 2. When can_upload is True, workspace file creation succeeds
        await sync_user_entitlement(
            user_id=user_id_str,
            plan_code="free",
            storage_quota_bytes=16106127360,
            billing_status="active",
            can_upload=True,
        )
        res_ok = await client.post(
            f"/api/workspace/{folder_id}/file",
            json={"name": "main.py", "content": "print('hello')", "parentId": folder_id},
            headers=auth_headers,
        )
        assert res_ok.status_code == 200
        assert res_ok.json()["name"] == "main.py"


@pytest.mark.asyncio
async def test_early_chunk_upload_rejection_on_first_chunk():
    """Verify chunked uploads fail fast on chunkIndex == 0 when subscription is expired or quota is exceeded."""
    user = User(
        email="chunk_early@example.com",
        name="Chunk Early Tester",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    token = create_access_token({"sub": user_id_str})
    auth_headers = {"Authorization": f"Bearer {token}"}

    # Set Entitlement to blocked uploads
    await sync_user_entitlement(
        user_id=user_id_str,
        plan_code="free",
        storage_quota_bytes=16106127360,
        billing_status="expired",
        can_upload=False,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Rejection on first chunk (chunkIndex=0)
        data = {
            "uploadId": "upload_test_early_123",
            "chunkIndex": 0,
            "totalChunks": 10,
            "filename": "huge_video.mp4",
        }
        files = {"file": ("chunk_0", b"First chunk bytes", "application/octet-stream")}
        res = await client.post("/api/files/upload/chunk", data=data, files=files, headers=auth_headers)
        assert res.status_code == 403
        assert "grace period has expired" in res.json().get("detail", {}).get("error", "")

        # Invalid chunk bounds check
        bad_data = {
            "uploadId": "upload_test_early_123",
            "chunkIndex": 12,  # > totalChunks
            "totalChunks": 10,
            "filename": "huge_video.mp4",
        }
        bad_res = await client.post("/api/files/upload/chunk", data=bad_data, files=files, headers=auth_headers)
        assert bad_res.status_code == 400


@pytest.mark.asyncio
async def test_subscription_upgrade_supersedes_prior_active_subscription(monkeypatch, test_secret):
    """Verify upgrading a subscription marks the prior active subscription as superseded without downtime."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    now_utc = datetime.now(timezone.utc)
    user = User(
        email="upgrader@example.com",
        name="Upgrade Customer",
        pricing_plan="personal",
        storage_limit_bytes=53687091200,
    )
    await user.insert()
    user_id_str = str(user.id)

    # 1. Existing Active "Personal" Subscription
    old_sub = Subscription(
        user_id=user_id_str,
        plan_id="personal",
        plan_snapshot={
            "code": "personal",
            "name": "Personal Plan",
            "billing_interval": "monthly",
            "amount_paise": 11900,
            "storage_quota_bytes": 53687091200,
        },
        provider="cashfree",
        cashfree_subscription_id="cf_sub_old_personal_111",
        status="active",
        current_period_start=now_utc - timedelta(days=10),
        current_period_end=now_utc + timedelta(days=20),
    )
    await old_sub.insert()

    # 2. User initiates upgrade to "Power" plan (pending authorization)
    new_sub = Subscription(
        user_id=user_id_str,
        plan_id="power",
        plan_snapshot={
            "code": "power",
            "name": "Power Plan",
            "billing_interval": "monthly",
            "amount_paise": 99900,
            "storage_quota_bytes": 1099511627776,
        },
        provider="cashfree",
        cashfree_subscription_id="cf_sub_new_power_222",
        status="pending_authorization",
        current_period_start=now_utc,
        current_period_end=now_utc + timedelta(days=30),
    )
    await new_sub.insert()

    # Prior subscription is still active while new subscription is pending
    fresh_old = await Subscription.get(old_sub.id)
    assert fresh_old.status == "active"

    # 3. New subscription payment success webhook arrives
    now_ts = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "event_id": "evt_upgrade_confirm_888",
        "data": {
            "subscription": {"subscription_id": "cf_sub_new_power_222"},
            "payment": {"payment_id": "cf_pay_power_888", "payment_status": "SUCCESS"},
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    msg_with_ts = str(now_ts).encode("utf-8") + raw_body
    sig = hmac.new(test_secret.encode("utf-8"), msg_with_ts, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig, "x-webhook-timestamp": str(now_ts)}

    res, code = await process_webhook_payload(raw_body, headers)
    assert code == 200

    # 4. Verify new subscription is active
    fresh_new = await Subscription.get(new_sub.id)
    assert fresh_new.status == "active"

    # 5. Verify old subscription is marked superseded and terminated
    fresh_old = await Subscription.get(old_sub.id)
    assert fresh_old.status == "superseded"
    assert fresh_old.ended_at is not None

    # 6. Verify Entitlement updated to Power plan (1 TB)
    entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
    assert entitlement.plan_code == "power"
    assert entitlement.storage_quota_bytes == 1099511627776
    assert entitlement.billing_status == "active"

    # 7. Verify BillingAuditLog recorded SUBSCRIPTION_SUPERSEDED
    audit = await BillingAuditLog.find_one(
        BillingAuditLog.user_id == user_id_str,
        BillingAuditLog.action == "SUBSCRIPTION_SUPERSEDED",
    )
    assert audit is not None


@pytest.mark.asyncio
async def test_production_mode_does_not_simulate_fake_cashfree_payloads(monkeypatch):
    """Verify that in production mode, missing/invalid Cashfree credentials fail safely without mock simulation."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_ENV", "production")
    monkeypatch.setattr(settings, "CASHFREE_APP_ID", "")
    monkeypatch.setattr(settings, "CASHFREE_SECRET_KEY", "")

    data, err = await create_cashfree_recurring_subscription(
        user_id="user_prod_test",
        customer_id="cust_prod_test",
        customer_name="Prod Customer",
        customer_email="prod@example.com",
        customer_phone="9999999999",
        plan_code="personal",
        plan_name="Personal",
        billing_interval="monthly",
        amount_inr=119.0,
        return_url="https://example.com",
    )
    assert data is None
    assert err is not None
    assert "Production Cashfree credentials" in err


@pytest.mark.asyncio
async def test_cashfree_base_url_service_resolution(monkeypatch):
    """Verify get_cashfree_base_url correctly resolves PG vs Sub endpoints in sandbox and production."""
    from app.cashfree_client import get_cashfree_base_url
    from app.config import get_settings
    settings = get_settings()

    # Default service=pg
    monkeypatch.setattr(settings, "CASHFREE_SUBSCRIPTION_SERVICE", "pg")
    assert get_cashfree_base_url("SANDBOX") == "https://sandbox.cashfree.com/pg"
    assert get_cashfree_base_url("production") == "https://api.cashfree.com/pg"

    # Explicit service=sub
    assert get_cashfree_base_url("SANDBOX", service="sub") == "https://sandbox.cashfree.com/sub/v1"
    assert get_cashfree_base_url("production", service="sub") == "https://api.cashfree.com/sub/v1"


@pytest.mark.asyncio
async def test_cashfree_pg_subscription_endpoint_and_snake_case_payload(monkeypatch):
    """Verify create_cashfree_recurring_subscription targets /pg/subscriptions and sends proper PG snake_case schema."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_ENV", "SANDBOX")
    monkeypatch.setattr(settings, "CASHFREE_SUBSCRIPTION_SERVICE", "pg")
    monkeypatch.setattr(settings, "CASHFREE_API_VERSION", "2025-01-01")
    monkeypatch.setattr(settings, "CASHFREE_APP_ID", "TEST_VALID_APP_ID")
    monkeypatch.setattr(settings, "CASHFREE_SECRET_KEY", "cfsk_ma_test_real_key_123")
    monkeypatch.setattr(settings, "CASHFREE_MODE", "")

    captured_request = {}

    class MockResponse:
        status_code = 200
        def json(self):
            return {
                "subscription_id": "cf_sub_mock_123",
                "cf_subscription_id": "cf_mandate_9999",
                "subscription_session_id": "sub_sess_abc123xyz",
                "subscription_status": "INITIALIZED",
                "auth_link": "https://sandbox.cashfree.com/pg/view/sub/cf_sub_mock_123",
            }
        @property
        def text(self):
            return ""

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        async def post(self, url, json=None, headers=None):
            captured_request["url"] = url
            captured_request["json"] = json
            captured_request["headers"] = headers
            return MockResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

    data, err = await create_cashfree_recurring_subscription(
        user_id="usr_pg_test_99",
        customer_id="cust_pg_test_99",
        customer_name="PG Test Customer",
        customer_email="pg_test@example.com",
        customer_phone="9876543210",
        plan_code="plus",
        plan_name="Plus",
        billing_interval="monthly",
        amount_inr=299.0,
        return_url="http://localhost:3000/dashboard",
    )

    assert err is None
    assert data is not None

    # Verify Endpoint URL and Headers
    assert captured_request["url"] == "https://sandbox.cashfree.com/pg/subscriptions"
    assert captured_request["headers"]["x-api-version"] == "2025-01-01"
    assert captured_request["headers"]["x-client-id"] == "TEST_VALID_APP_ID"

    # Verify snake_case Schema and inline plan_details
    payload = captured_request["json"]
    assert "subscription_id" in payload
    assert "plan_details" in payload
    assert "customer_details" in payload
    assert "subscription_meta" in payload
    assert "subscription_tags" in payload

    plan = payload["plan_details"]
    assert plan["plan_name"] == "GetFileNova-Plus-monthly"
    assert plan["plan_type"] == "PERIODIC"
    assert plan["plan_amount"] == 299.0
    assert plan["plan_max_amount"] == 299.0
    assert plan["plan_currency"] == "INR"
    assert plan["plan_interval_type"] == "MONTH"
    assert plan["plan_intervals"] == 1
    assert plan["plan_max_cycles"] == 120

    cust = payload["customer_details"]
    assert cust["customer_id"] == "cust_pg_test_99"
    assert cust["customer_name"] == "PG Test Customer"
    assert cust["customer_email"] == "pg_test@example.com"
    assert cust["customer_phone"] == "9876543210"

    assert payload["subscription_meta"]["return_url"] == "http://localhost:3000/dashboard"

    # Verify Response normalization
    assert data["subscription_session_id"] == "sub_sess_abc123xyz"
    assert data["payment_session_id"] == "sub_sess_abc123xyz"
    assert data["cf_subscription_id"] == "cf_mandate_9999"
    assert data["auth_link"] == "https://sandbox.cashfree.com/pg/view/sub/cf_sub_mock_123"


@pytest.mark.asyncio
async def test_cashfree_pg_webhook_subscription_details_format(monkeypatch, test_secret):
    """Verify webhook handler extracts subscription_id from PG Subscriptions subscription_details structure."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="pg_webhook_test@example.com",
        name="PG Webhook User",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)

    now_utc = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user_id_str,
        plan_id="power",
        plan_snapshot={
            "code": "power",
            "name": "Power Plan",
            "billing_interval": "monthly",
            "amount_paise": 99900,
            "storage_quota_bytes": 1099511627776,
        },
        provider="cashfree",
        cashfree_subscription_id="cf_sub_pg_webhook_777",
        status="pending_authorization",
        current_period_start=now_utc,
        current_period_end=now_utc + timedelta(days=30),
    )
    await sub.insert()

    now_ts = int(datetime.now(timezone.utc).timestamp())
    # PG Subscriptions payload with subscription_details and SUBSCRIPTION_STATUS_CHANGE (ACTIVE)
    payload = {
        "type": "SUBSCRIPTION_STATUS_CHANGE",
        "event_id": "evt_pg_status_change_777",
        "data": {
            "subscription_details": {
                "subscription_id": "cf_sub_pg_webhook_777",
                "cf_subscription_id": "cf_mandate_pg_777",
                "subscription_status": "ACTIVE",
            },
            "payment_details": {
                "payment_id": "cf_pay_pg_777",
                "payment_status": "SUCCESS",
            },
            "customer_details": {
                "customer_id": f"cust_{user_id_str}",
                "customer_email": user.email,
            },
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    msg_with_ts = str(now_ts).encode("utf-8") + raw_body
    sig = hmac.new(test_secret.encode("utf-8"), msg_with_ts, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig, "x-webhook-timestamp": str(now_ts)}

    res, code = await process_webhook_payload(raw_body, headers)
    assert code == 200

    # Verify subscription activated
    updated_sub = await Subscription.get(sub.id)
    assert updated_sub.status == "active"

    # Verify Entitlement synchronized to Power plan (1 TB)
    entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
    assert entitlement.plan_code == "power"
    assert entitlement.storage_quota_bytes == 1099511627776
    assert entitlement.billing_status == "active"


@pytest.mark.asyncio
async def test_resume_pending_subscription_retrieves_existing_session(monkeypatch):
    """Verify GET /api/subscriptions/pending-session returns the session for an authenticated user's pending subscription."""
    user = User(
        name="Test User",
        email="resume_pending_user@example.com",
        password_hash="hashed_pw",
        storage_used=0,
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    token = create_access_token({"sub": user_id_str})
    auth_headers = {"Authorization": f"Bearer {token}"}

    # Insert a pending_authorization subscription
    now = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user_id_str,
        plan_id="plus",
        plan_snapshot={"code": "plus", "name": "Plus Plan", "billing_interval": "monthly", "amount_paise": 29900, "storage_quota_bytes": 214748364800, "currency": "INR"},
        provider="cashfree",
        cashfree_subscription_id="cf_sub_resume_123",
        status="pending_authorization",
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        next_billing_at=now + timedelta(days=30),
        cancel_at_period_end=False,
    )
    await sub.insert()

    # Mock get_cashfree_subscription
    from app import billing_service
    async def mock_get_sub(cf_sub_id):
        assert cf_sub_id == "cf_sub_resume_123"
        return {
            "subscription_id": "cf_sub_resume_123",
            "cf_subscription_id": "cf_sub_resume_123",
            "subscription_session_id": "sub_sess_resume_active_999",
            "payment_session_id": "sub_sess_resume_active_999",
            "status": "INITIALIZED",
            "subscription_status": "INITIALIZED",
            "auth_link": "https://sandbox.cashfree.com/pg/view/sub/cf_sub_resume_123",
        }, None

    monkeypatch.setattr(billing_service, "get_cashfree_subscription", mock_get_sub)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Test unauthenticated request fails
        unauth_resp = await client.get("/api/subscriptions/pending-session")
        assert unauth_resp.status_code in (401, 403)

        # Test authenticated request succeeds
        resp = await client.get("/api/subscriptions/pending-session", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["subscription_id"] == str(sub.id)
        assert data["cashfree_subscription_id"] == "cf_sub_resume_123"
        assert data["subscription_session_id"] == "sub_sess_resume_active_999"
        assert data["status"] == "pending_authorization"
        assert data["plan_code"] == "plus"


@pytest.mark.asyncio
async def test_create_subscription_resumes_pending_without_duplicate(monkeypatch):
    """Verify POST /api/subscriptions/create reuses existing pending subscription instead of creating a duplicate."""
    user = User(
        name="Test User",
        email="no_dup_sub_user@example.com",
        password_hash="hashed_pw",
        storage_used=0,
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    token = create_access_token({"sub": user_id_str})
    auth_headers = {"Authorization": f"Bearer {token}"}

    # Insert a pending_authorization subscription
    now = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user_id_str,
        plan_id="plus",
        plan_snapshot={"code": "plus", "name": "Plus Plan", "billing_interval": "monthly", "amount_paise": 29900, "storage_quota_bytes": 214748364800, "currency": "INR"},
        provider="cashfree",
        cashfree_subscription_id="cf_sub_existing_777",
        status="pending_authorization",
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        next_billing_at=now + timedelta(days=30),
        cancel_at_period_end=False,
    )
    await sub.insert()

    # Mock get_cashfree_subscription and spy create_cashfree_recurring_subscription
    from app import billing_service
    create_called = False

    async def mock_create(*args, **kwargs):
        nonlocal create_called
        create_called = True
        return None, "Should not be called"

    async def mock_get_sub(cf_sub_id):
        return {
            "subscription_id": "cf_sub_existing_777",
            "subscription_session_id": "sub_sess_existing_777",
            "status": "INITIALIZED",
        }, None

    monkeypatch.setattr(billing_service, "create_cashfree_recurring_subscription", mock_create)
    monkeypatch.setattr(billing_service, "get_cashfree_subscription", mock_get_sub)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/subscriptions/create",
            json={"planName": "plus", "billingCycle": "monthly"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert create_called is False
        assert data["subscription_id"] == str(sub.id)
        assert data["subscription_session_id"] == "sub_sess_existing_777"
        assert data["cashfree_subscription_id"] == "cf_sub_existing_777"


@pytest.mark.asyncio
async def test_resume_pending_subscription_cashfree_active_syncs_entitlement(monkeypatch):
    """Verify that if Cashfree reports the subscription is already ACTIVE, local state and entitlements are synced."""
    user = User(
        name="Test User",
        email="already_active_user@example.com",
        password_hash="hashed_pw",
        storage_used=0,
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    token = create_access_token({"sub": user_id_str})
    auth_headers = {"Authorization": f"Bearer {token}"}

    now = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user_id_str,
        plan_id="power",
        plan_snapshot={"code": "power", "name": "Power Plan", "billing_interval": "monthly", "amount_paise": 99900, "storage_quota_bytes": 1099511627776, "currency": "INR"},
        provider="cashfree",
        cashfree_subscription_id="cf_sub_already_active_888",
        status="pending_authorization",
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        next_billing_at=now + timedelta(days=30),
        cancel_at_period_end=False,
    )
    await sub.insert()

    from app import billing_service
    async def mock_get_sub(cf_sub_id):
        return {
            "subscription_id": "cf_sub_already_active_888",
            "status": "ACTIVE",
            "subscription_status": "ACTIVE",
        }, None

    monkeypatch.setattr(billing_service, "get_cashfree_subscription", mock_get_sub)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/subscriptions/pending-session", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        assert data["plan_code"] == "power"

        # Verify DB state was updated
        updated_sub = await Subscription.get(sub.id)
        assert updated_sub.status == "active"

        entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
        assert entitlement.plan_code == "power"
        assert entitlement.billing_status == "active"


@pytest.mark.asyncio
async def test_resume_pending_subscription_cashfree_failure_handles_gracefully(monkeypatch):
    """Verify that if Cashfree reports the subscription is CANCELLED/FAILED, appropriate status is returned."""
    user = User(
        name="Test User",
        email="failed_sub_user@example.com",
        password_hash="hashed_pw",
        storage_used=0,
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    token = create_access_token({"sub": user_id_str})
    auth_headers = {"Authorization": f"Bearer {token}"}

    now = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user_id_str,
        plan_id="plus",
        plan_snapshot={"code": "plus", "name": "Plus Plan", "billing_interval": "monthly", "amount_paise": 29900, "storage_quota_bytes": 214748364800, "currency": "INR"},
        provider="cashfree",
        cashfree_subscription_id="cf_sub_failed_999",
        status="pending_authorization",
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        next_billing_at=now + timedelta(days=30),
        cancel_at_period_end=False,
    )
    await sub.insert()

    from app import billing_service
    async def mock_get_sub(cf_sub_id):
        return {
            "subscription_id": "cf_sub_failed_999",
            "status": "CANCELLED",
            "subscription_status": "CANCELLED",
        }, None

    monkeypatch.setattr(billing_service, "get_cashfree_subscription", mock_get_sub)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/subscriptions/pending-session", headers=auth_headers)
        assert resp.status_code == 400
        data = resp.json()
        assert "cancelled" in data["detail"]["error"].lower()

        # Verify DB sub status updated
        updated_sub = await Subscription.get(sub.id)
        assert updated_sub.status == "cancelled"


@pytest.mark.asyncio
async def test_initial_subscription_payment_success_reconciles_pending_transaction(monkeypatch, test_secret):
    """Verify that SUBSCRIPTION_PAYMENT_SUCCESS reconciles the pre-created pending initial_payment transaction."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="init_reconcile@example.com",
        name="Init Reconcile",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    now_utc = datetime.now(timezone.utc)
    cf_sub_id = "cf_sub_reconcile_001"

    # Setup pending subscription and pre-created initial_payment transaction
    sub = Subscription(
        user_id=user_id_str,
        plan_id="plus",
        plan_snapshot={
            "code": "plus",
            "name": "Plus Plan",
            "billing_interval": "monthly",
            "amount_paise": 29900,
            "storage_quota_bytes": 214748364800,
        },
        provider="cashfree",
        cashfree_subscription_id=cf_sub_id,
        status="pending_authorization",
        current_period_start=now_utc,
        current_period_end=now_utc + timedelta(days=30),
    )
    await sub.insert()
    sub_id_str = str(sub.id)

    init_tx = PaymentTransaction(
        user_id=user_id_str,
        subscription_id=sub_id_str,
        type="initial_payment",
        provider="cashfree",
        cashfree_order_id=cf_sub_id,
        cashfree_payment_id=None,
        amount_paise=29900,
        currency="INR",
        status="pending",
        idempotency_key=f"init_sub_{cf_sub_id}",
        billing_period_start=now_utc,
        billing_period_end=now_utc + timedelta(days=30),
        paid_at=None,
        created_at=now_utc,
        updated_at=now_utc,
    )
    await init_tx.insert()
    init_tx_id = init_tx.id

    payload = {
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "event_id": "evt_reconcile_001",
        "data": {
            "subscription_details": {"subscription_id": cf_sub_id},
            "payment_details": {
                "payment_id": "cf_pay_rec_001",
                "payment_status": "SUCCESS",
                "payment_time": "2026-09-30T17:00:00Z",
            },
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    res, code = await process_webhook_payload(raw_body, headers)
    assert code == 200
    assert res["status"] == "success"

    # Assert SAME transaction document was updated
    all_txs = await PaymentTransaction.find(PaymentTransaction.subscription_id == sub_id_str).to_list()
    assert len(all_txs) == 1
    reconciled_tx = all_txs[0]
    assert reconciled_tx.id == init_tx_id
    assert reconciled_tx.type == "initial_payment"
    assert reconciled_tx.status == "success"
    assert reconciled_tx.cashfree_payment_id == "cf_pay_rec_001"
    assert reconciled_tx.paid_at is not None
    assert reconciled_tx.paid_at.year == 2026

    # Subscription and entitlement active
    updated_sub = await Subscription.get(sub.id)
    assert updated_sub.status == "active"
    entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
    assert entitlement.billing_status == "active"
    assert entitlement.can_upload is True


@pytest.mark.asyncio
async def test_duplicate_subscription_payment_success_is_idempotent(monkeypatch, test_secret):
    """Process the same SUCCESS webhook twice and assert exactly one transaction and no duplicate renewal."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="dup_reconcile@example.com",
        name="Dup Reconcile",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    now_utc = datetime.now(timezone.utc)
    cf_sub_id = "cf_sub_dup_002"

    sub = Subscription(
        user_id=user_id_str,
        plan_id="plus",
        plan_snapshot={
            "code": "plus",
            "name": "Plus Plan",
            "billing_interval": "monthly",
            "amount_paise": 29900,
            "storage_quota_bytes": 214748364800,
        },
        provider="cashfree",
        cashfree_subscription_id=cf_sub_id,
        status="pending_authorization",
        current_period_start=now_utc,
        current_period_end=now_utc + timedelta(days=30),
    )
    await sub.insert()
    sub_id_str = str(sub.id)

    init_tx = PaymentTransaction(
        user_id=user_id_str,
        subscription_id=sub_id_str,
        type="initial_payment",
        provider="cashfree",
        cashfree_order_id=cf_sub_id,
        cashfree_payment_id=None,
        amount_paise=29900,
        currency="INR",
        status="pending",
        idempotency_key=f"init_sub_{cf_sub_id}",
        billing_period_start=now_utc,
        billing_period_end=now_utc + timedelta(days=30),
        paid_at=None,
        created_at=now_utc,
        updated_at=now_utc,
    )
    await init_tx.insert()

    payload = {
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "event_id": "evt_dup_002",
        "data": {
            "subscription_details": {"subscription_id": cf_sub_id},
            "payment_details": {
                "payment_id": "cf_pay_dup_002",
                "payment_status": "SUCCESS",
            },
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    # First delivery
    res1, code1 = await process_webhook_payload(raw_body, headers)
    assert code1 == 200
    assert res1["status"] == "success"

    # Second delivery
    res2, code2 = await process_webhook_payload(raw_body, headers)
    assert code2 == 200
    assert res2["status"] == "already_processed"

    # Verify exactly one transaction exists
    all_txs = await PaymentTransaction.find(PaymentTransaction.subscription_id == sub_id_str).to_list()
    assert len(all_txs) == 1
    assert all_txs[0].status == "success"
    assert all_txs[0].cashfree_payment_id == "cf_pay_dup_002"


@pytest.mark.asyncio
async def test_initial_subscription_payment_failure_reconciles_pending_transaction(monkeypatch, test_secret):
    """Verify that initial payment failure webhook reconciles pending transaction to failed with error codes."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="fail_reconcile@example.com",
        name="Fail Reconcile",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    now_utc = datetime.now(timezone.utc)
    cf_sub_id = "cf_sub_fail_003"

    sub = Subscription(
        user_id=user_id_str,
        plan_id="personal",
        plan_snapshot={
            "code": "personal",
            "name": "Personal Plan",
            "billing_interval": "monthly",
            "amount_paise": 11900,
            "storage_quota_bytes": 53687091200,
        },
        provider="cashfree",
        cashfree_subscription_id=cf_sub_id,
        status="pending_authorization",
        current_period_start=now_utc,
        current_period_end=now_utc + timedelta(days=30),
    )
    await sub.insert()
    sub_id_str = str(sub.id)

    init_tx = PaymentTransaction(
        user_id=user_id_str,
        subscription_id=sub_id_str,
        type="initial_payment",
        provider="cashfree",
        cashfree_order_id=cf_sub_id,
        cashfree_payment_id=None,
        amount_paise=11900,
        currency="INR",
        status="pending",
        idempotency_key=f"init_sub_{cf_sub_id}",
        billing_period_start=now_utc,
        billing_period_end=now_utc + timedelta(days=30),
        paid_at=None,
        created_at=now_utc,
        updated_at=now_utc,
    )
    await init_tx.insert()
    init_tx_id = init_tx.id

    payload = {
        "type": "SUBSCRIPTION_PAYMENT_FAILED",
        "event_id": "evt_fail_003",
        "data": {
            "subscription_details": {"subscription_id": cf_sub_id},
            "payment_details": {
                "payment_id": "cf_pay_fail_003",
                "payment_status": "FAILED",
            },
            "error_details": {
                "error_code": "AUTH_FAILED",
                "error_description": "User cancelled authentication on bank page",
            },
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    res, code = await process_webhook_payload(raw_body, headers)
    assert code == 200

    all_txs = await PaymentTransaction.find(PaymentTransaction.subscription_id == sub_id_str).to_list()
    assert len(all_txs) == 1
    failed_tx = all_txs[0]
    assert failed_tx.id == init_tx_id
    assert failed_tx.status == "failed"
    assert failed_tx.failure_code == "AUTH_FAILED"
    assert "User cancelled" in (failed_tx.failure_message or "")
    assert failed_tx.cashfree_payment_id == "cf_pay_fail_003"


@pytest.mark.asyncio
async def test_recurring_payment_success_creates_renewal(monkeypatch, test_secret):
    """Use a subscription with no pending initial_payment, assert a type='renewal' transaction is created."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="renewal_user@example.com",
        name="Renewal User",
        pricing_plan="plus",
        storage_limit_bytes=214748364800,
    )
    await user.insert()
    user_id_str = str(user.id)
    now_utc = datetime.now(timezone.utc)
    cf_sub_id = "cf_sub_renew_004"

    sub = Subscription(
        user_id=user_id_str,
        plan_id="plus",
        plan_snapshot={
            "code": "plus",
            "name": "Plus Plan",
            "billing_interval": "monthly",
            "amount_paise": 29900,
            "storage_quota_bytes": 214748364800,
        },
        provider="cashfree",
        cashfree_subscription_id=cf_sub_id,
        status="active",
        current_period_start=now_utc - timedelta(days=30),
        current_period_end=now_utc,
    )
    await sub.insert()
    sub_id_str = str(sub.id)

    # Pre-existing initial payment that is already SUCCESS
    initial_tx = PaymentTransaction(
        user_id=user_id_str,
        subscription_id=sub_id_str,
        type="initial_payment",
        provider="cashfree",
        cashfree_order_id=cf_sub_id,
        cashfree_payment_id="cf_pay_init_004",
        amount_paise=29900,
        currency="INR",
        status="success",
        idempotency_key=f"init_sub_{cf_sub_id}",
        paid_at=now_utc - timedelta(days=30),
        created_at=now_utc - timedelta(days=30),
        updated_at=now_utc - timedelta(days=30),
    )
    await initial_tx.insert()

    # Incoming recurring renewal webhook
    payload = {
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "event_id": "evt_renew_004",
        "data": {
            "subscription_details": {"subscription_id": cf_sub_id},
            "payment_details": {
                "payment_id": "cf_pay_renewal_004",
                "payment_status": "SUCCESS",
            },
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    res, code = await process_webhook_payload(raw_body, headers)
    assert code == 200

    all_txs = await PaymentTransaction.find(PaymentTransaction.subscription_id == sub_id_str).to_list()
    assert len(all_txs) == 2
    renewal_tx = next(t for t in all_txs if t.type == "renewal")
    assert renewal_tx.status == "success"
    assert renewal_tx.cashfree_payment_id == "cf_pay_renewal_004"
    assert renewal_tx.amount_paise == 29900


@pytest.mark.asyncio
async def test_recurring_payment_duplicate_does_not_create_second_transaction(monkeypatch, test_secret):
    """Send the same renewal SUCCESS webhook twice and assert exactly one renewal transaction."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="renewal_dup@example.com",
        name="Renewal Dup",
        pricing_plan="plus",
        storage_limit_bytes=214748364800,
    )
    await user.insert()
    user_id_str = str(user.id)
    now_utc = datetime.now(timezone.utc)
    cf_sub_id = "cf_sub_renew_dup_005"

    sub = Subscription(
        user_id=user_id_str,
        plan_id="plus",
        plan_snapshot={
            "code": "plus",
            "name": "Plus Plan",
            "billing_interval": "monthly",
            "amount_paise": 29900,
            "storage_quota_bytes": 214748364800,
        },
        provider="cashfree",
        cashfree_subscription_id=cf_sub_id,
        status="active",
        current_period_start=now_utc - timedelta(days=30),
        current_period_end=now_utc,
    )
    await sub.insert()
    sub_id_str = str(sub.id)

    # Initial payment already successful
    initial_tx = PaymentTransaction(
        user_id=user_id_str,
        subscription_id=sub_id_str,
        type="initial_payment",
        provider="cashfree",
        cashfree_order_id=cf_sub_id,
        cashfree_payment_id="cf_pay_init_005",
        amount_paise=29900,
        currency="INR",
        status="success",
        idempotency_key=f"init_sub_{cf_sub_id}",
        paid_at=now_utc - timedelta(days=30),
    )
    await initial_tx.insert()

    payload = {
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "event_id": "evt_renew_dup_005",
        "data": {
            "subscription_details": {"subscription_id": cf_sub_id},
            "payment_details": {
                "payment_id": "cf_pay_renew_dup_005",
                "payment_status": "SUCCESS",
            },
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    res1, code1 = await process_webhook_payload(raw_body, headers)
    assert code1 == 200

    res2, code2 = await process_webhook_payload(raw_body, headers)
    assert code2 == 200
    assert res2["status"] == "already_processed"

    renewals = await PaymentTransaction.find(
        PaymentTransaction.subscription_id == sub_id_str,
        PaymentTransaction.type == "renewal",
    ).to_list()
    assert len(renewals) == 1


@pytest.mark.asyncio
async def test_numeric_cashfree_payment_id_is_normalized(monkeypatch, test_secret):
    """Verify that numeric payment IDs in Cashfree webhook payloads are normalized to strings."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="numeric_payid@example.com",
        name="Numeric PayID",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    now_utc = datetime.now(timezone.utc)
    cf_sub_id = "cf_sub_numeric_006"

    sub = Subscription(
        user_id=user_id_str,
        plan_id="power",
        plan_snapshot={
            "code": "power",
            "name": "Power Plan",
            "billing_interval": "monthly",
            "amount_paise": 99900,
            "storage_quota_bytes": 1099511627776,
        },
        provider="cashfree",
        cashfree_subscription_id=cf_sub_id,
        status="pending_authorization",
        current_period_start=now_utc,
        current_period_end=now_utc + timedelta(days=30),
    )
    await sub.insert()
    sub_id_str = str(sub.id)

    init_tx = PaymentTransaction(
        user_id=user_id_str,
        subscription_id=sub_id_str,
        type="initial_payment",
        provider="cashfree",
        cashfree_order_id=cf_sub_id,
        cashfree_payment_id=None,
        amount_paise=99900,
        currency="INR",
        status="pending",
        idempotency_key=f"init_sub_{cf_sub_id}",
    )
    await init_tx.insert()

    # Numeric integer payment_id in webhook payload
    payload = {
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "event_id": "evt_numeric_006",
        "data": {
            "subscription_details": {"subscription_id": cf_sub_id},
            "payment_details": {
                "payment_id": 987654321,  # Integer!
                "payment_status": "SUCCESS",
            },
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    res, code = await process_webhook_payload(raw_body, headers)
    assert code == 200

    reconciled_tx = await PaymentTransaction.get(init_tx.id)
    assert reconciled_tx.status == "success"
    assert reconciled_tx.cashfree_payment_id == "987654321"
    assert isinstance(reconciled_tx.cashfree_payment_id, str)


@pytest.mark.asyncio
async def test_missing_payment_id_does_not_create_fake_payment_id(monkeypatch, test_secret):
    """Verify that missing/null payment ID does not store fake string like 'None'."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="missing_payid@example.com",
        name="Missing PayID",
        pricing_plan="free",
        storage_limit_bytes=16106127360,
    )
    await user.insert()
    user_id_str = str(user.id)
    now_utc = datetime.now(timezone.utc)
    cf_sub_id = "cf_sub_missing_007"

    sub = Subscription(
        user_id=user_id_str,
        plan_id="personal",
        plan_snapshot={
            "code": "personal",
            "name": "Personal Plan",
            "billing_interval": "monthly",
            "amount_paise": 11900,
            "storage_quota_bytes": 53687091200,
        },
        provider="cashfree",
        cashfree_subscription_id=cf_sub_id,
        status="pending_authorization",
        current_period_start=now_utc,
        current_period_end=now_utc + timedelta(days=30),
    )
    await sub.insert()
    sub_id_str = str(sub.id)

    init_tx = PaymentTransaction(
        user_id=user_id_str,
        subscription_id=sub_id_str,
        type="initial_payment",
        provider="cashfree",
        cashfree_order_id=cf_sub_id,
        cashfree_payment_id=None,
        amount_paise=11900,
        currency="INR",
        status="pending",
        idempotency_key=f"init_sub_{cf_sub_id}",
    )
    await init_tx.insert()

    # Payload with no payment_id or payment_id is None
    payload = {
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "event_id": "evt_missing_007",
        "data": {
            "subscription_details": {"subscription_id": cf_sub_id},
            "payment_details": {
                "payment_id": None,
                "payment_status": "SUCCESS",
            },
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    res, code = await process_webhook_payload(raw_body, headers)
    assert code == 200

    reconciled_tx = await PaymentTransaction.get(init_tx.id)
    assert reconciled_tx.status == "success"
    assert reconciled_tx.cashfree_payment_id is None


# ── Focused Calendar Arithmetic and Renewal Hardening Tests ───────────────────

def test_calendar_interval_monthly_arithmetic():
    """Verify monthly calendar addition handles normal months, month-end clipping, and leap years."""
    from app.billing_plans import add_calendar_interval

    # 1. Normal month (31 days): Oct 3 -> Nov 3 -> Dec 3 -> Jan 3
    base_oct = datetime(2026, 10, 3, 14, 30, 0, tzinfo=timezone.utc)
    next_nov = add_calendar_interval(base_oct, "monthly")
    assert next_nov == datetime(2026, 11, 3, 14, 30, 0, tzinfo=timezone.utc)
    next_dec = add_calendar_interval(next_nov, "monthly")
    assert next_dec == datetime(2026, 12, 3, 14, 30, 0, tzinfo=timezone.utc)
    next_jan = add_calendar_interval(next_dec, "monthly")
    assert next_jan == datetime(2027, 1, 3, 14, 30, 0, tzinfo=timezone.utc)

    # 2. 30-day month: Apr 15 -> May 15
    apr_15 = datetime(2026, 4, 15, 10, 0, 0, tzinfo=timezone.utc)
    assert add_calendar_interval(apr_15, "monthly") == datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)

    # 3. Month-end 31-day multi-month sequence (Jan 31 non-leap -> Feb 28 -> Mar 31 -> Apr 30 -> May 31)
    jan_31_non_leap = datetime(2026, 1, 31, 10, 0, 0, tzinfo=timezone.utc)
    feb_28 = add_calendar_interval(jan_31_non_leap, "monthly")
    assert feb_28 == datetime(2026, 2, 28, 10, 0, 0, tzinfo=timezone.utc)
    mar_31 = add_calendar_interval(feb_28, "monthly")
    assert mar_31 == datetime(2026, 3, 31, 10, 0, 0, tzinfo=timezone.utc)
    apr_30 = add_calendar_interval(mar_31, "monthly")
    assert apr_30 == datetime(2026, 4, 30, 10, 0, 0, tzinfo=timezone.utc)
    may_31 = add_calendar_interval(apr_30, "monthly")
    assert may_31 == datetime(2026, 5, 31, 10, 0, 0, tzinfo=timezone.utc)

    # 4. Month-end leap year sequence (Jan 31 2028 -> Feb 29 2028 -> Mar 31 2028)
    jan_31_leap = datetime(2028, 1, 31, 10, 0, 0, tzinfo=timezone.utc)
    feb_29 = add_calendar_interval(jan_31_leap, "monthly")
    assert feb_29 == datetime(2028, 2, 29, 10, 0, 0, tzinfo=timezone.utc)
    mar_31_leap = add_calendar_interval(feb_29, "monthly")
    assert mar_31_leap == datetime(2028, 3, 31, 10, 0, 0, tzinfo=timezone.utc)

    # 5. Month-end sequence (Mar 31 -> Apr 30 -> May 31 -> Jun 30 -> Jul 31 -> Aug 31 -> Sep 30 -> Oct 31 -> Nov 30 -> Dec 31 -> Jan 31)
    d = datetime(2026, 3, 31, 12, 0, 0, tzinfo=timezone.utc)
    expected_days = [
        (2026, 4, 30),
        (2026, 5, 31),
        (2026, 6, 30),
        (2026, 7, 31),
        (2026, 8, 31),
        (2026, 9, 30),
        (2026, 10, 31),
        (2026, 11, 30),
        (2026, 12, 31),
        (2027, 1, 31),
        (2027, 2, 28),
        (2027, 3, 31),
    ]
    for exp_y, exp_m, exp_d in expected_days:
        d = add_calendar_interval(d, "monthly")
        assert (d.year, d.month, d.day) == (exp_y, exp_m, exp_d)

    # 6. Timezone-aware guarantee
    naive_dt = datetime(2026, 7, 15, 12, 0, 0)
    aware_result = add_calendar_interval(naive_dt, "monthly")
    assert aware_result.tzinfo is not None
    assert aware_result == datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_calendar_interval_annual_arithmetic():
    """Verify annual calendar addition handles standard years, leap-year transitions, and multi-year renewals."""
    from app.billing_plans import add_calendar_interval

    # 1. Normal annual: Oct 3, 2026 -> Oct 3, 2027 -> Oct 3, 2028
    base_annual = datetime(2026, 10, 3, 15, 0, 0, tzinfo=timezone.utc)
    next_annual = add_calendar_interval(base_annual, "annual")
    assert next_annual == datetime(2027, 10, 3, 15, 0, 0, tzinfo=timezone.utc)
    next_annual_2 = add_calendar_interval(next_annual, "annual")
    assert next_annual_2 == datetime(2028, 10, 3, 15, 0, 0, tzinfo=timezone.utc)

    # 2. Leap year Feb 29 -> Non-leap year Feb 28 -> Non-leap year Feb 28
    leap_feb_29 = datetime(2028, 2, 29, 12, 0, 0, tzinfo=timezone.utc)
    non_leap_feb_28 = add_calendar_interval(leap_feb_29, "annual")
    assert non_leap_feb_28 == datetime(2029, 2, 28, 12, 0, 0, tzinfo=timezone.utc)
    non_leap_feb_28_2 = add_calendar_interval(non_leap_feb_28, "annual")
    assert non_leap_feb_28_2 == datetime(2030, 2, 28, 12, 0, 0, tzinfo=timezone.utc)

    # 3. Non-leap year Feb 28 -> Leap year Feb 28
    non_leap_feb_28_base = datetime(2027, 2, 28, 12, 0, 0, tzinfo=timezone.utc)
    leap_feb_28 = add_calendar_interval(non_leap_feb_28_base, "annual")
    assert leap_feb_28 == datetime(2028, 2, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_successful_renewal_advances_period_with_calendar_precision(monkeypatch, test_secret):
    """Verify that a successful renewal advances current_period_end with exact calendar arithmetic."""
    from app.config import get_settings
    from app.billing_plans import add_calendar_interval

    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="power_calendar_renew@example.com",
        name="Power Calendar User",
        pricing_plan="power",
        storage_limit_bytes=1099511627776,
    )
    await user.insert()
    user_id_str = str(user.id)
    cf_sub_id = "cf_sub_calendar_power_008"

    init_start = datetime(2026, 10, 3, 10, 30, 0, tzinfo=timezone.utc)
    init_end = add_calendar_interval(init_start, "monthly")

    sub = Subscription(
        user_id=user_id_str,
        plan_id="power",
        plan_snapshot={
            "code": "power",
            "name": "Power Plan",
            "billing_interval": "monthly",
            "amount_paise": 99900,
            "storage_quota_bytes": 1099511627776,
        },
        provider="cashfree",
        cashfree_subscription_id=cf_sub_id,
        status="active",
        current_period_start=init_start,
        current_period_end=init_end,
        next_billing_at=init_end,
    )
    await sub.insert()
    sub_id_str = str(sub.id)

    # Initial payment was already completed
    init_tx = PaymentTransaction(
        user_id=user_id_str,
        subscription_id=sub_id_str,
        type="initial_payment",
        provider="cashfree",
        cashfree_order_id=cf_sub_id,
        cashfree_payment_id="cf_pay_init_008",
        amount_paise=99900,
        currency="INR",
        status="success",
        idempotency_key=f"init_sub_{cf_sub_id}",
        paid_at=init_start,
    )
    await init_tx.insert()

    # Renewal webhook arrives on renewal date
    renewal_payment_time = datetime(2026, 11, 3, 10, 30, 0, tzinfo=timezone.utc)
    payload = {
        "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
        "event_id": "evt_power_renew_008",
        "data": {
            "subscription_details": {"subscription_id": cf_sub_id},
            "payment_details": {
                "payment_id": "cf_pay_power_renew_008",
                "payment_status": "SUCCESS",
                "payment_time": renewal_payment_time.isoformat(),
            },
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    res, code = await process_webhook_payload(raw_body, headers)
    assert code == 200

    # Verify subscription period advanced to exactly Dec 3
    from app.billing_service import _to_utc
    updated_sub = await Subscription.get(sub.id)
    assert updated_sub.status == "active"
    assert _to_utc(updated_sub.current_period_start) == renewal_payment_time
    expected_period_end = add_calendar_interval(renewal_payment_time, "monthly")
    assert _to_utc(updated_sub.current_period_end) == expected_period_end
    assert _to_utc(updated_sub.next_billing_at) == expected_period_end
    assert _to_utc(updated_sub.current_period_end) == datetime(2026, 12, 3, 10, 30, 0, tzinfo=timezone.utc)

    # Verify renewal transaction created with correct dates and amounts
    txs = await PaymentTransaction.find(PaymentTransaction.subscription_id == sub_id_str).to_list()
    assert len(txs) == 2
    renew_tx = next(t for t in txs if t.type == "renewal")
    assert renew_tx.status == "success"
    assert renew_tx.cashfree_payment_id == "cf_pay_power_renew_008"
    assert renew_tx.amount_paise == 99900
    assert _to_utc(renew_tx.paid_at) == renewal_payment_time
    assert _to_utc(renew_tx.billing_period_start) == renewal_payment_time
    assert _to_utc(renew_tx.billing_period_end) == expected_period_end

    # Entitlement remains active
    entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
    assert entitlement.billing_status == "active"
    assert entitlement.can_upload is True
    assert entitlement.can_download is True


@pytest.mark.asyncio
async def test_failed_renewal_computes_grace_from_failure_timestamp(monkeypatch, test_secret):
    """Verify failed renewal parses failure timestamp and enters 14-day grace without duplicate resetting."""
    from app.config import get_settings
    from app.billing_service import _to_utc
    settings = get_settings()
    monkeypatch.setattr(settings, "CASHFREE_WEBHOOK_SECRET", test_secret)

    user = User(
        email="fail_grace_test@example.com",
        name="Fail Grace User",
        pricing_plan="plus",
        storage_limit_bytes=214748364800,
    )
    await user.insert()
    user_id_str = str(user.id)
    cf_sub_id = "cf_sub_fail_grace_009"

    failure_time = datetime(2026, 11, 3, 11, 0, 0, tzinfo=timezone.utc)

    sub = Subscription(
        user_id=user_id_str,
        plan_id="plus",
        plan_snapshot={
            "code": "plus",
            "name": "Plus Plan",
            "billing_interval": "monthly",
            "amount_paise": 29900,
            "storage_quota_bytes": 214748364800,
        },
        provider="cashfree",
        cashfree_subscription_id=cf_sub_id,
        status="active",
        current_period_start=datetime(2026, 10, 3, 11, 0, 0, tzinfo=timezone.utc),
        current_period_end=failure_time,
    )
    await sub.insert()
    sub_id_str = str(sub.id)

    # Initial payment succeeded previously
    init_tx = PaymentTransaction(
        user_id=user_id_str,
        subscription_id=sub_id_str,
        type="initial_payment",
        provider="cashfree",
        cashfree_order_id=cf_sub_id,
        cashfree_payment_id="cf_pay_init_009",
        amount_paise=29900,
        currency="INR",
        status="success",
        idempotency_key=f"init_sub_{cf_sub_id}",
    )
    await init_tx.insert()

    # Send failed renewal webhook
    payload = {
        "type": "SUBSCRIPTION_PAYMENT_FAILED",
        "event_id": "evt_fail_renew_009",
        "data": {
            "subscription_details": {"subscription_id": cf_sub_id},
            "payment_details": {
                "payment_id": "cf_pay_fail_009",
                "payment_status": "FAILED",
                "payment_time": failure_time.isoformat(),
            },
            "error_details": {
                "error_code": "INSUFFICIENT_FUNDS",
                "error_description": "Card balance insufficient",
            },
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {"x-webhook-signature": sig}

    res1, code1 = await process_webhook_payload(raw_body, headers)
    assert code1 == 200

    updated_sub = await Subscription.get(sub.id)
    assert updated_sub.status == "past_due"
    assert _to_utc(updated_sub.grace_period_started_at) == failure_time
    assert _to_utc(updated_sub.grace_period_ends_at) == failure_time + timedelta(days=14)

    # Entitlement: uploads & downloads remain active during grace
    entitlement = await Entitlement.find_one(Entitlement.user_id == user_id_str)
    assert entitlement.billing_status == "past_due"
    assert entitlement.can_upload is True
    assert entitlement.can_download is True
    assert _to_utc(entitlement.grace_period_ends_at) == failure_time + timedelta(days=14)

    # Duplicate failed renewal webhook must be idempotent
    res2, code2 = await process_webhook_payload(raw_body, headers)
    assert code2 == 200
    assert res2["status"] == "already_processed"

    # Verify no duplicate failed transaction was inserted
    failed_txs = await PaymentTransaction.find(
        PaymentTransaction.subscription_id == sub_id_str,
        PaymentTransaction.type == "renewal",
    ).to_list()
    assert len(failed_txs) == 1
    assert failed_txs[0].status == "failed"
    assert failed_txs[0].failure_code == "INSUFFICIENT_FUNDS"

