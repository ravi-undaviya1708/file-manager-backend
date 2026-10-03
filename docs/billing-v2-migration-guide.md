# GetFileNova v2.0 — Billing Database Migration Guide

**Document Version:** 1.0.0  
**Target Repository:** `file-manager-backend`  
**Migration Module:** `app.migrations.migrate_billing_v2`  
**Database Technology:** MongoDB + Motor + Beanie ODM  

---

## 1. Overview & Objectives

This migration establishes a repeatable, safe, and additive schema migration for GetFileNova v2.0 to support Cashfree recurring subscriptions.

### Core Guarantees:
- **Zero Data Loss:** Existing `users`, `payment_records`, `cancellation_records`, and `file_system_items` are preserved completely untouched.
- **Dry-Run Default:** The migration executes in dry-run simulation mode by default unless the explicit `--apply` flag is passed.
- **Duplicate Conflict Pre-scan:** Performs automated aggregation checks for candidate unique keys before creating unique indexes to prevent index build errors.
- **Idempotency:** Can be executed repeatedly in staging or production without generating duplicate plans or altering existing configurations.
- **Legacy Protection:** Legacy users with one-time payments are not enrolled in recurring billing and keep their current storage allocations.

---

## 2. Authoritative Plan Seeding Matrix

### Quota and Price Unit Standards:
- **Price Unit:** All prices are stored in **integer paise** (₹1.00 = 100 paise).
- **Storage Unit:** All storage quotas are stored in **integer bytes** following the repository's established binary prefix (`1 GiB = 1,073,741,824 bytes`).

| Plan Code | Display Name | Billing Interval | Price (INR) | Price (Paise) | Quota (GiB) | Quota (Bytes) | Version |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `free` | Free Starter | `free` | ₹0 | `0` | 15 GiB | `16,106,127,360` | 1 |
| `personal` | Personal Plan | `monthly` | ₹119 | `11,900` | 50 GiB | `53,687,091,200` | 1 |
| `personal` | Personal Plan | `annual` | ₹1,190 | `119,000` | 50 GiB | `53,687,091,200` | 1 |
| `plus` | Plus Plan | `monthly` | ₹299 | `29,900` | 200 GiB | `214,748,364,800` | 1 |
| `plus` | Plus Plan | `annual` | ₹2,990 | `299,000` | 200 GiB | `214,748,364,800` | 1 |
| `power` | Power Plan | `monthly` | ₹999 | `99,900` | 1 TiB | `1,099,511,627,776` | 1 |
| `power` | Power Plan | `annual` | ₹9,990 | `999,000` | 1 TiB | `1,099,511,627,776` | 1 |
| `power` | Power Lifetime Plan | `lifetime` | ₹19,990 | `1,999,000` | 1 TiB | `1,099,511,627,776` | 1 |

---

## 3. Execution Commands

### 3.1 Development Environment
```bash
# 1. Activate virtual environment
cd /path/to/file-manager-backend
source venv/bin/activate

# 2. Run in Dry-Run mode (Simulates all checks without writing)
python -m app.migrations.migrate_billing_v2 --dry-run

# 3. Check for candidate unique index duplicates only
python -m app.migrations.migrate_billing_v2 --check-duplicates

# 4. Apply migration (Performs live additive migration)
python -m app.migrations.migrate_billing_v2 --apply
```

### 3.2 Staging / Production Environment
```bash
# Ensure MONGODB_URL and MONGODB_DB_NAME are set in environment
export MONGODB_URL="mongodb+srv://<user>:<password>@cluster0.mongodb.net/?retryWrites=true&w=majority"
export MONGODB_DB_NAME="file_manager"

# Step 1: Pre-flight Dry-Run & Duplicate Scan
python -m app.migrations.migrate_billing_v2 --dry-run --verbose

# Step 2: Live Apply
python -m app.migrations.migrate_billing_v2 --apply --verbose
```

---

## 4. Rollback Strategy

Because all billing v2 collections and indexes are **strictly additive** and do not alter existing collection schemas:

1. **Reverting Code:** Revert model registrations in `app/database.py` and `app/seed.py`.
2. **Database Cleanliness (Optional):** The 6 new collections (`plans`, `subscriptions`, `payment_transactions`, `webhook_events`, `entitlements`, `billing_audit_logs`) can be dropped independently without affecting `users`, `file_system_items`, or legacy `payment_records`.
3. **Legacy Data Guarantee:** Legacy `payment_records` and `User` documents remain unchanged at all times.
