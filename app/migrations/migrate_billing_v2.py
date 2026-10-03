"""Safe, repeatable, and additive MongoDB migration for Billing v2 models.

Usage:
  # Dry-run mode (Default - no database modifications)
  python -m app.migrations.migrate_billing_v2
  python -m app.migrations.migrate_billing_v2 --dry-run

  # Apply mode (Performs actual additive migration)
  python -m app.migrations.migrate_billing_v2 --apply

  # Duplicate scan only
  python -m app.migrations.migrate_billing_v2 --check-duplicates
"""

from __future__ import annotations

import sys
import asyncio
import logging
import argparse
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import DuplicateKeyError

from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("billing_migration_v2")

# Authoritative Plan Configurations (Binary byte definition matching existing codebase)
# Storage: 1 GiB = 1,073,741,824 bytes (e.g. 50 GiB = 53,687,091,200 bytes, 1 TiB = 1,099,511,627,776 bytes)
AUTHORITATIVE_PLANS: List[Dict[str, Any]] = [
    {
        "code": "free",
        "name": "Free Starter",
        "billing_interval": "free",
        "amount_paise": 0,  # ₹0.00
        "currency": "INR",
        "storage_quota_bytes": 16106127360,  # 15 GiB
        "version": 1,
        "is_active": True,
        "cashfree_plan_id": None,
    },
    {
        "code": "personal",
        "name": "Personal Plan",
        "billing_interval": "monthly",
        "amount_paise": 11900,  # ₹119.00
        "currency": "INR",
        "storage_quota_bytes": 53687091200,  # 50 GiB
        "version": 1,
        "is_active": True,
        "cashfree_plan_id": None,
    },
    {
        "code": "personal",
        "name": "Personal Plan",
        "billing_interval": "annual",
        "amount_paise": 119000,  # ₹1,190.00
        "currency": "INR",
        "storage_quota_bytes": 53687091200,  # 50 GiB
        "version": 1,
        "is_active": True,
        "cashfree_plan_id": None,
    },
    {
        "code": "plus",
        "name": "Plus Plan",
        "billing_interval": "monthly",
        "amount_paise": 29900,  # ₹299.00
        "currency": "INR",
        "storage_quota_bytes": 214748364800,  # 200 GiB
        "version": 1,
        "is_active": True,
        "cashfree_plan_id": None,
    },
    {
        "code": "plus",
        "name": "Plus Plan",
        "billing_interval": "annual",
        "amount_paise": 299000,  # ₹2,990.00
        "currency": "INR",
        "storage_quota_bytes": 214748364800,  # 200 GiB
        "version": 1,
        "is_active": True,
        "cashfree_plan_id": None,
    },
    {
        "code": "power",
        "name": "Power Plan",
        "billing_interval": "monthly",
        "amount_paise": 99900,  # ₹999.00
        "currency": "INR",
        "storage_quota_bytes": 1099511627776,  # 1 TiB (1,024 GiB)
        "version": 1,
        "is_active": True,
        "cashfree_plan_id": None,
    },
    {
        "code": "power",
        "name": "Power Plan",
        "billing_interval": "annual",
        "amount_paise": 999000,  # ₹9,990.00
        "currency": "INR",
        "storage_quota_bytes": 1099511627776,  # 1 TiB (1,024 GiB)
        "version": 1,
        "is_active": True,
        "cashfree_plan_id": None,
    },
    {
        "code": "power",
        "name": "Power Lifetime Plan",
        "billing_interval": "lifetime",
        "amount_paise": 1999000,  # ₹19,990.00
        "currency": "INR",
        "storage_quota_bytes": 1099511627776,  # 1 TiB (1,024 GiB)
        "version": 1,
        "is_active": True,
        "cashfree_plan_id": None,
    },
]

# Required Billing Collections and Index Specifications
BILLING_COLLECTIONS_AND_INDEXES: Dict[str, List[IndexModel]] = {
    "plans": [
        IndexModel(
            [("code", ASCENDING), ("billing_interval", ASCENDING), ("version", ASCENDING)],
            unique=True,
            name="idx_plans_code_interval_version",
        ),
        IndexModel(
            [("is_active", ASCENDING), ("code", ASCENDING), ("billing_interval", ASCENDING)],
            name="idx_plans_active_code_interval",
        ),
        IndexModel(
            [("cashfree_plan_id", ASCENDING)],
            sparse=True,
            name="idx_plans_cf_plan_id",
        ),
    ],
    "subscriptions": [
        IndexModel(
            [("cashfree_subscription_id", ASCENDING)],
            unique=True,
            sparse=True,
            name="idx_subs_cf_subscription_id",
        ),
        IndexModel(
            [("user_id", ASCENDING), ("status", ASCENDING)],
            name="idx_subs_user_status",
        ),
        IndexModel(
            [("current_period_end", ASCENDING), ("status", ASCENDING)],
            name="idx_subs_period_end_status",
        ),
        IndexModel(
            [("grace_period_ends_at", ASCENDING), ("status", ASCENDING)],
            sparse=True,
            name="idx_subs_grace_period_status",
        ),
    ],
    "payment_transactions": [
        IndexModel(
            [("idempotency_key", ASCENDING)],
            unique=True,
            name="idx_paytx_idempotency_key",
        ),
        IndexModel(
            [("provider", ASCENDING), ("cashfree_payment_id", ASCENDING)],
            unique=True,
            sparse=True,
            name="idx_paytx_provider_payment_id",
        ),
        IndexModel(
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            name="idx_paytx_user_created",
        ),
        IndexModel(
            [("subscription_id", ASCENDING), ("status", ASCENDING)],
            sparse=True,
            name="idx_paytx_sub_status",
        ),
        IndexModel(
            [("cashfree_order_id", ASCENDING)],
            sparse=True,
            name="idx_paytx_cf_order_id",
        ),
    ],
    "webhook_events": [
        IndexModel(
            [("provider", ASCENDING), ("event_id", ASCENDING)],
            unique=True,
            name="idx_webhook_provider_event_id",
        ),
        IndexModel(
            [("status", ASCENDING), ("next_retry_at", ASCENDING)],
            name="idx_webhook_status_retry",
        ),
        IndexModel(
            [("event_type", ASCENDING), ("received_at", DESCENDING)],
            name="idx_webhook_type_received",
        ),
        IndexModel(
            [("cashfree_subscription_id", ASCENDING)],
            sparse=True,
            name="idx_webhook_cf_sub_id",
        ),
    ],
    "entitlements": [
        IndexModel(
            [("user_id", ASCENDING)],
            unique=True,
            name="idx_entitlement_user_id_unique",
        ),
        IndexModel(
            [("billing_status", ASCENDING), ("grace_period_ends_at", ASCENDING)],
            sparse=True,
            name="idx_entitlement_status_grace",
        ),
        IndexModel(
            [("can_upload", ASCENDING), ("retention_deadline", ASCENDING)],
            sparse=True,
            name="idx_entitlement_upload_retention",
        ),
    ],
    "billing_audit_logs": [
        IndexModel(
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            name="idx_audit_user_created",
        ),
        IndexModel(
            [("subscription_id", ASCENDING), ("created_at", DESCENDING)],
            sparse=True,
            name="idx_audit_sub_created",
        ),
        IndexModel(
            [("action", ASCENDING), ("created_at", DESCENDING)],
            name="idx_audit_action_created",
        ),
        IndexModel(
            [("correlation_id", ASCENDING)],
            sparse=True,
            name="idx_audit_correlation_id",
        ),
    ],
}


async def scan_for_duplicates(db: AsyncIOMotorDatabase) -> List[Dict[str, Any]]:
    """Inspect collections for any records that would violate unique index constraints."""
    duplicate_findings = []

    # 1. Check plans: (code, billing_interval, version)
    if "plans" in await db.list_collection_names():
        pipeline = [
            {"$group": {
                "_id": {"code": "$code", "interval": "$billing_interval", "version": "$version"},
                "count": {"$sum": 1},
                "ids": {"$push": "$_id"}
            }},
            {"$match": {"count": {"$gt": 1}}}
        ]
        dups = await db.plans.aggregate(pipeline).to_list(None)
        for d in dups:
            duplicate_findings.append({
                "collection": "plans",
                "index": "idx_plans_code_interval_version",
                "conflict_key": d["_id"],
                "count": d["count"],
                "document_ids": [str(i) for i in d["ids"]],
            })

    # 2. Check subscriptions: cashfree_subscription_id (when non-null)
    if "subscriptions" in await db.list_collection_names():
        pipeline = [
            {"$match": {"cashfree_subscription_id": {"$ne": None}}},
            {"$group": {
                "_id": "$cashfree_subscription_id",
                "count": {"$sum": 1},
                "ids": {"$push": "$_id"}
            }},
            {"$match": {"count": {"$gt": 1}}}
        ]
        dups = await db.subscriptions.aggregate(pipeline).to_list(None)
        for d in dups:
            duplicate_findings.append({
                "collection": "subscriptions",
                "index": "idx_subs_cf_subscription_id",
                "conflict_key": d["_id"],
                "count": d["count"],
                "document_ids": [str(i) for i in d["ids"]],
            })

    # 3. Check payment_transactions: idempotency_key
    if "payment_transactions" in await db.list_collection_names():
        pipeline = [
            {"$match": {"idempotency_key": {"$ne": None}}},
            {"$group": {
                "_id": "$idempotency_key",
                "count": {"$sum": 1},
                "ids": {"$push": "$_id"}
            }},
            {"$match": {"count": {"$gt": 1}}}
        ]
        dups = await db.payment_transactions.aggregate(pipeline).to_list(None)
        for d in dups:
            duplicate_findings.append({
                "collection": "payment_transactions",
                "index": "idx_paytx_idempotency_key",
                "conflict_key": d["_id"],
                "count": d["count"],
                "document_ids": [str(i) for i in d["ids"]],
            })

    # 4. Check entitlements: user_id
    if "entitlements" in await db.list_collection_names():
        pipeline = [
            {"$group": {
                "_id": "$user_id",
                "count": {"$sum": 1},
                "ids": {"$push": "$_id"}
            }},
            {"$match": {"count": {"$gt": 1}}}
        ]
        dups = await db.entitlements.aggregate(pipeline).to_list(None)
        for d in dups:
            duplicate_findings.append({
                "collection": "entitlements",
                "index": "idx_entitlement_user_id_unique",
                "conflict_key": d["_id"],
                "count": d["count"],
                "document_ids": [str(i) for i in d["ids"]],
            })

    # 5. Check webhook_events: (provider, event_id)
    if "webhook_events" in await db.list_collection_names():
        pipeline = [
            {"$group": {
                "_id": {"provider": "$provider", "event_id": "$event_id"},
                "count": {"$sum": 1},
                "ids": {"$push": "$_id"}
            }},
            {"$match": {"count": {"$gt": 1}}}
        ]
        dups = await db.webhook_events.aggregate(pipeline).to_list(None)
        for d in dups:
            duplicate_findings.append({
                "collection": "webhook_events",
                "index": "idx_webhook_provider_event_id",
                "conflict_key": d["_id"],
                "count": d["count"],
                "document_ids": [str(i) for i in d["ids"]],
            })

    return duplicate_findings


async def run_billing_migration(
    db: Optional[AsyncIOMotorDatabase] = None,
    apply: bool = False,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Execute or dry-run the billing v2 database migration.

    Parameters:
      db: Motor database instance. If None, connects using settings.MONGODB_URL.
      apply: If True, writes changes to MongoDB. If False, performs safe read-only simulation.
      verbose: Enable verbose diagnostic logging.

    Returns:
      Dictionary containing migration summary metrics, status, and audit records.
    """
    settings = get_settings()
    client: Optional[AsyncIOMotorClient] = None
    should_close_client = False

    if db is None:
        client = AsyncIOMotorClient(settings.MONGODB_URL)
        db = client[settings.MONGODB_DB_NAME]
        should_close_client = True

    mode_str = "APPLY (LIVE WRITE)" if apply else "DRY-RUN (SIMULATION ONLY)"
    logger.info("================================================================================")
    logger.info("GetFileNova v2.0 Billing Database Migration")
    logger.info(f"Execution Mode: {mode_str}")
    logger.info(f"Target Database: {db.name}")
    logger.info("================================================================================")

    result: Dict[str, Any] = {
        "success": False,
        "mode": "apply" if apply else "dry_run",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database_name": db.name,
        "collections_created": [],
        "indexes_verified": [],
        "plans_seeded": 0,
        "plans_existing": 0,
        "duplicates_detected": [],
        "legacy_data_preserved": True,
        "errors": [],
    }

    try:
        # Step 1: Pre-flight Inspection of Existing Legacy Data
        existing_collections = await db.list_collection_names()
        users_count = await db.users.count_documents({}) if "users" in existing_collections else 0
        payments_count = await db.payment_records.count_documents({}) if "payment_records" in existing_collections else 0
        files_count = await db.file_system_items.count_documents({}) if "file_system_items" in existing_collections else 0

        logger.info(
            f"Pre-flight Database Status: users={users_count}, payment_records={payments_count}, file_system_items={files_count}"
        )

        # Step 2: Scan for Potential Unique Constraint Violations
        logger.info("Scanning for duplicate records across unique index candidate fields...")
        duplicate_conflicts = await scan_for_duplicates(db)
        result["duplicates_detected"] = duplicate_conflicts

        if duplicate_conflicts:
            msg = f"Found {len(duplicate_conflicts)} duplicate conflict(s) that would violate unique index constraints!"
            logger.error(msg)
            for c in duplicate_conflicts:
                logger.error(f"  Conflict: collection={c['collection']}, index={c['index']}, key={c['conflict_key']}, docs={c['document_ids']}")
            result["errors"].append(msg)
            result["success"] = False
            return result

        logger.info("✓ Zero duplicate key conflicts detected. Unique index integrity confirmed.")

        # Step 3: Verify and Create Collections and Approved Indexes
        for col_name, index_models in BILLING_COLLECTIONS_AND_INDEXES.items():
            col_exists = col_name in existing_collections
            if not col_exists:
                if apply:
                    # In MongoDB Motor, creating a collection or an index creates the collection
                    logger.info(f"Creating collection '{col_name}'...")
                    await db.create_collection(col_name)
                    result["collections_created"].append(col_name)
                else:
                    logger.info(f"[Dry-Run] Proposed creation of collection '{col_name}'")
                    result["collections_created"].append(col_name)

            collection = db[col_name]
            existing_indexes = []
            if col_exists or apply:
                try:
                    cursor = collection.list_indexes()
                    existing_indexes = [idx["name"] async for idx in cursor]
                except Exception:
                    existing_indexes = []

            for model in index_models:
                idx_name = model.document.get("name") or str(model.document.get("key"))
                idx_keys = model.document.get("key")
                is_unique = bool(model.document.get("unique"))

                if apply:
                    if idx_name not in existing_indexes:
                        logger.info(f"Creating index '{idx_name}' on collection '{col_name}' (unique={is_unique})...")
                        await collection.create_indexes([model])
                        result["indexes_verified"].append({"collection": col_name, "index": idx_name, "status": "created"})
                    else:
                        logger.info(f"Index '{idx_name}' on collection '{col_name}' already exists.")
                        result["indexes_verified"].append({"collection": col_name, "index": idx_name, "status": "already_exists"})
                else:
                    status_desc = "would_create" if idx_name not in existing_indexes else "already_exists"
                    logger.info(f"[Dry-Run] Index '{idx_name}' on '{col_name}': {status_desc} (unique={is_unique})")
                    result["indexes_verified"].append({"collection": col_name, "index": idx_name, "status": status_desc})

        # Step 4: Seed Authoritative Plan Configurations
        logger.info("Evaluating authoritative billing plans for seeding...")
        plans_col = db.plans

        for plan_spec in AUTHORITATIVE_PLANS:
            query = {
                "code": plan_spec["code"],
                "billing_interval": plan_spec["billing_interval"],
                "version": plan_spec["version"],
            }
            existing_plan = await plans_col.find_one(query)

            if existing_plan is None:
                if apply:
                    now_utc = datetime.now(timezone.utc)
                    doc_to_insert = plan_spec.copy()
                    doc_to_insert["created_at"] = now_utc
                    doc_to_insert["updated_at"] = now_utc
                    await plans_col.insert_one(doc_to_insert)
                    logger.info(
                        f"Inserted plan '{plan_spec['code']}' ({plan_spec['billing_interval']}) — Amount: {plan_spec['amount_paise']} paise, Quota: {plan_spec['storage_quota_bytes']} bytes"
                    )
                    result["plans_seeded"] += 1
                else:
                    logger.info(
                        f"[Dry-Run] Would insert plan '{plan_spec['code']}' ({plan_spec['billing_interval']}) — Amount: {plan_spec['amount_paise']} paise, Quota: {plan_spec['storage_quota_bytes']} bytes"
                    )
                    result["plans_seeded"] += 1
            else:
                logger.info(
                    f"Plan '{plan_spec['code']}' ({plan_spec['billing_interval']} v{plan_spec['version']}) already exists. Preserving historical configuration."
                )
                result["plans_existing"] += 1

        # Step 5: Post-flight Legacy Data Integrity Confirmation
        post_users_count = await db.users.count_documents({}) if "users" in existing_collections else 0
        post_payments_count = await db.payment_records.count_documents({}) if "payment_records" in existing_collections else 0

        if post_users_count != users_count or post_payments_count != payments_count:
            msg = "CRITICAL: Legacy record count mismatch detected during migration!"
            logger.error(msg)
            result["errors"].append(msg)
            result["legacy_data_preserved"] = False
            result["success"] = False
            return result

        logger.info(
            f"✓ Legacy Integrity Verified: users ({users_count} unchanged), payment_records ({payments_count} unchanged)."
        )

        # Step 6: Log Audit Trail Entry (in apply mode)
        if apply:
            now_utc = datetime.now(timezone.utc)
            audit_entry = {
                "user_id": None,
                "subscription_id": None,
                "action": "MIGRATION_V2_APPLIED",
                "actor_type": "system",
                "actor_id": "migration_script",
                "before": {"plans_count": result["plans_existing"]},
                "after": {
                    "plans_count": result["plans_existing"] + result["plans_seeded"],
                    "collections_created": result["collections_created"],
                },
                "reason": "Additive billing v2 schema migration executed successfully",
                "correlation_id": f"mig_{int(now_utc.timestamp())}",
                "created_at": now_utc,
            }
            await db.billing_audit_logs.insert_one(audit_entry)
            logger.info("✓ Recorded audit trail log in 'billing_audit_logs'.")

        result["success"] = True
        logger.info("================================================================================")
        logger.info(f"Migration Completed Successfully. Mode: {mode_str}")
        logger.info(f"Summary: Plans Seeded={result['plans_seeded']}, Existing={result['plans_existing']}, Indexes Processed={len(result['indexes_verified'])}")
        logger.info("================================================================================")

    except Exception as exc:
        err_msg = f"Migration execution failed with exception: {exc}"
        logger.exception(err_msg)
        result["errors"].append(err_msg)
        result["success"] = False
    finally:
        if should_close_client and client:
            client.close()

    return result


def main():
    """CLI entry point for migration execution."""
    parser = argparse.ArgumentParser(
        description="GetFileNova v2.0 Billing Database Migration Tool"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute live database migration writes (Default is dry-run mode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate migration without writing to database (Default)",
    )
    parser.add_argument(
        "--check-duplicates",
        action="store_true",
        default=False,
        help="Scan and report duplicate keys on candidate unique index fields only",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if args.check_duplicates:
        async def run_check():
            settings = get_settings()
            client = AsyncIOMotorClient(settings.MONGODB_URL)
            db = client[settings.MONGODB_DB_NAME]
            try:
                dups = await scan_for_duplicates(db)
                if dups:
                    print(f"FAILED: Found {len(dups)} duplicate conflict(s):")
                    for d in dups:
                        print(f"  {d}")
                    sys.exit(1)
                else:
                    print("SUCCESS: Zero duplicate conflicts detected.")
                    sys.exit(0)
            finally:
                client.close()

        asyncio.run(run_check())
        return

    # Determine execution mode: apply only if --apply explicitly passed and --dry-run not set
    apply_mode = args.apply and not args.dry_run

    res = asyncio.run(run_billing_migration(apply=apply_mode, verbose=args.verbose))

    if not res["success"]:
        logger.error(f"Migration Failed with Errors: {res['errors']}")
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
