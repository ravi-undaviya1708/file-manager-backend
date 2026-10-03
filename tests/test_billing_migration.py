"""Tests for Billing v2 Database Migration script (Dry-run, Apply, Idempotency, Duplicates, and Legacy Preservation)."""

import pytest
from datetime import datetime, timezone
from mongomock_motor import AsyncMongoMockClient

from app.models import (
    User,
    PaymentRecord,
    Plan,
    Subscription,
    PaymentTransaction,
    WebhookEvent,
    Entitlement,
    BillingAuditLog,
)
from app.migrations.migrate_billing_v2 import (
    run_billing_migration,
    scan_for_duplicates,
    AUTHORITATIVE_PLANS,
)


@pytest.mark.asyncio
async def test_migration_dry_run_performs_no_writes():
    """Verify that dry-run mode simulates creation but performs 0 database writes."""
    client = AsyncMongoMockClient()
    db = client["test_migration_dry_run"]

    # Initial state: no plans
    assert await db.plans.count_documents({}) == 0
    assert await db.subscriptions.count_documents({}) == 0

    res = await run_billing_migration(db=db, apply=False)

    assert res["success"] is True
    assert res["mode"] == "dry_run"
    assert res["plans_seeded"] == len(AUTHORITATIVE_PLANS)

    # Verify that database remained completely unmodified
    assert await db.plans.count_documents({}) == 0
    assert await db.subscriptions.count_documents({}) == 0
    assert await db.billing_audit_logs.count_documents({}) == 0


@pytest.mark.asyncio
async def test_migration_apply_and_idempotent_execution():
    """Verify that apply mode populates plans, and running it twice is strictly idempotent."""
    client = AsyncMongoMockClient()
    db = client["test_migration_idempotent"]

    # 1. First execution in apply mode
    res1 = await run_billing_migration(db=db, apply=True)
    assert res1["success"] is True
    assert res1["mode"] == "apply"
    assert res1["plans_seeded"] == len(AUTHORITATIVE_PLANS)
    assert res1["plans_existing"] == 0

    count_after_first = await db.plans.count_documents({})
    assert count_after_first == len(AUTHORITATIVE_PLANS)

    # Verify specific plan pricing in integer paise and quota in integer bytes
    personal_m = await db.plans.find_one({"code": "personal", "billing_interval": "monthly"})
    assert personal_m is not None
    assert personal_m["amount_paise"] == 11900  # ₹119.00
    assert personal_m["storage_quota_bytes"] == 53687091200  # 50 GiB
    assert personal_m["version"] == 1

    power_a = await db.plans.find_one({"code": "power", "billing_interval": "annual"})
    assert power_a is not None
    assert power_a["amount_paise"] == 999000  # ₹9,990.00
    assert power_a["storage_quota_bytes"] == 1099511627776  # 1 TiB

    # Verify audit log was written
    audit_count = await db.billing_audit_logs.count_documents({})
    assert audit_count == 1

    # 2. Second execution in apply mode (Idempotency test)
    res2 = await run_billing_migration(db=db, apply=True)
    assert res2["success"] is True
    assert res2["plans_seeded"] == 0
    assert res2["plans_existing"] == len(AUTHORITATIVE_PLANS)

    # Document count must not have changed
    count_after_second = await db.plans.count_documents({})
    assert count_after_second == count_after_first


@pytest.mark.asyncio
async def test_migration_preserves_existing_legacy_data():
    """Verify that existing legacy users, payment records, and storage quotas are preserved."""
    client = AsyncMongoMockClient()
    db = client["test_migration_legacy_preserved"]

    # Seed mock legacy user and payment record
    now = datetime.now(timezone.utc)
    await db.users.insert_one({
        "_id": "user_legacy_001",
        "email": "legacy_user@example.com",
        "name": "Legacy Customer",
        "pricing_plan": "plus",
        "storage_limit_bytes": 214748364800,
        "subscription_status": "active",
        "created_at": now,
    })

    await db.payment_records.insert_one({
        "_id": "pay_legacy_001",
        "user_id": "user_legacy_001",
        "order_id": "cf_ord_legacy_999",
        "amount": 299.0,
        "status": "SUCCESS",
        "created_at": now,
    })

    initial_user = await db.users.find_one({"_id": "user_legacy_001"})
    initial_payment = await db.payment_records.find_one({"_id": "pay_legacy_001"})

    # Run migration in apply mode
    res = await run_billing_migration(db=db, apply=True)
    assert res["success"] is True
    assert res["legacy_data_preserved"] is True

    # Confirm user record was not modified or overwritten
    post_user = await db.users.find_one({"_id": "user_legacy_001"})
    assert post_user == initial_user
    assert post_user["storage_limit_bytes"] == 214748364800

    # Confirm legacy payment record is untouched
    post_payment = await db.payment_records.find_one({"_id": "pay_legacy_001"})
    assert post_payment == initial_payment

    # Confirm no automatic recurring subscription was created for legacy user
    user_subs = await db.subscriptions.count_documents({"user_id": "user_legacy_001"})
    assert user_subs == 0


@pytest.mark.asyncio
async def test_duplicate_detection_aborts_migration_safely():
    """Verify that duplicate candidate keys are detected and migration halts before index corruption."""
    client = AsyncMongoMockClient()
    db = client["test_migration_duplicate_detection"]

    # Manually insert duplicate plans with the exact same (code, billing_interval, version)
    await db.plans.insert_one({
        "code": "personal",
        "billing_interval": "monthly",
        "version": 1,
        "name": "Personal Duplicate 1",
        "amount_paise": 11900,
        "storage_quota_bytes": 53687091200,
    })
    await db.plans.insert_one({
        "code": "personal",
        "billing_interval": "monthly",
        "version": 1,
        "name": "Personal Duplicate 2",
        "amount_paise": 11900,
        "storage_quota_bytes": 53687091200,
    })

    # Scan for duplicates
    dups = await scan_for_duplicates(db)
    assert len(dups) >= 1
    assert dups[0]["collection"] == "plans"
    assert dups[0]["count"] == 2

    # Run migration — should detect duplicates and fail safely
    res = await run_billing_migration(db=db, apply=True)
    assert res["success"] is False
    assert len(res["errors"]) > 0
    assert "duplicate conflict" in res["errors"][0]


@pytest.mark.asyncio
async def test_migration_error_reporting_not_silent():
    """Verify that unexpected exceptions during migration are caught and return success=False."""
    client = AsyncMongoMockClient()
    db = client["test_migration_error_handling"]

    # Pass an invalid db object or simulated failure
    class FaultyDB:
        name = "faulty_db"
        async def list_collection_names(self):
            raise RuntimeError("Simulated Database I/O Connection Breakdown")

    res = await run_billing_migration(db=FaultyDB(), apply=True)
    assert res["success"] is False
    assert len(res["errors"]) == 1
    assert "Simulated Database I/O Connection Breakdown" in res["errors"][0]
