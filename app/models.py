"""Beanie document models for the file manager."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, List

from beanie import Document
from pydantic import Field, EmailStr, BaseModel


class User(Document):
    """Represents a registered user in the application.

    Stored as a document in the 'users' MongoDB collection.
    """

    email: EmailStr = Field(unique=True)  # type: ignore
    hashed_password: Optional[str] = Field(default=None)
    name: str = Field(max_length=255)
    google_id: Optional[str] = Field(default=None)
    avatar_url: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_admin: bool = Field(default=False)
    storage_limit_bytes: int = Field(default=15032385536)  # 15 GB
    pricing_plan: str = Field(default="free")
    user_type: str = Field(default="individual")
    billing_cycle: Optional[str] = Field(default="monthly")
    subscription_status: Optional[str] = Field(default="trial")
    trial_started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trial_expires_at: Optional[datetime] = Field(default=None)
    subscription_expires_at: Optional[datetime] = Field(default=None)
    canceled_at: Optional[datetime] = Field(default=None)
    cancellation_reason: Optional[str] = Field(default=None)
    customer_id: Optional[str] = Field(default=None)
    phone: Optional[str] = Field(default=None)

    class Settings:
        name = "users"
        indexes = [
            "email",
            "google_id",
            "customer_id",
            "created_at",
            "pricing_plan",
            "subscription_status",
        ]

    def __repr__(self) -> str:
        return f"<User(id={self.id}, email={self.email}, name={self.name})>"


class Role(Document):
    """Represents a custom or default security role in the application.

    Stored as a document in the 'roles' MongoDB collection.
    """

    name: str = Field(max_length=255)
    key: str = Field(unique=True)  # type: ignore
    is_default: bool = Field(default=False)
    description: str = Field(default="")
    permissions: List[str] = Field(default_factory=list)

    class Settings:
        name = "roles"

    def __repr__(self) -> str:
        return f"<Role(name={self.name}, key={self.key})>"



from pymongo import IndexModel, ASCENDING, DESCENDING


class ItemShare(BaseModel):
    """Represents sharing metadata for a file or folder."""
    user_id: str
    email: str
    permission: str  # "viewer" or "editor"


class FileSystemItem(Document):
    """Represents a file or folder in the file manager.

    Stored as a document in the 'file_system_items' MongoDB collection.
    """

    name: str = Field(max_length=255)
    type: str = Field(pattern="^(folder|file)$")
    parent_id: Optional[str] = Field(default=None)
    user_id: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    size: Optional[int] = Field(default=None)
    starred: bool = Field(default=False)
    is_deleted: bool = Field(default=False)
    is_locked: bool = Field(default=False)
    lock_password_hash: Optional[str] = Field(default=None)
    is_hidden: bool = Field(default=False)
    partition_id: Optional[str] = Field(default=None)
    shares: List[ItemShare] = Field(default_factory=list)

    class Settings:
        name = "file_system_items"
        indexes = [
            # Primary folder child listing: user_id + parent_id + is_deleted + partition_id
            IndexModel(
                [("user_id", ASCENDING), ("parent_id", ASCENDING), ("is_deleted", ASCENDING), ("partition_id", ASCENDING)],
                name="idx_user_parent_deleted_partition"
            ),
            # Direct child listing & sorting by name
            IndexModel(
                [("user_id", ASCENDING), ("parent_id", ASCENDING), ("is_deleted", ASCENDING), ("name", ASCENDING)],
                name="idx_user_parent_deleted_name"
            ),
            # Starred items view
            IndexModel(
                [("user_id", ASCENDING), ("is_deleted", ASCENDING), ("starred", ASCENDING)],
                name="idx_user_deleted_starred"
            ),
            # Recycle bin view
            IndexModel(
                [("user_id", ASCENDING), ("is_deleted", ASCENDING), ("parent_id", ASCENDING)],
                name="idx_user_deleted_parent"
            ),
            # Safe / Locked folder view
            IndexModel(
                [("user_id", ASCENDING), ("is_locked", ASCENDING), ("is_deleted", ASCENDING)],
                name="idx_user_locked_deleted"
            ),
            # Shared with me lookups
            IndexModel(
                [("shares.user_id", ASCENDING), ("is_deleted", ASCENDING)],
                name="idx_shares_user_deleted"
            ),
            IndexModel(
                [("shares.email", ASCENDING), ("is_deleted", ASCENDING)],
                name="idx_shares_email_deleted"
            ),
            # Storage usage aggregation
            IndexModel(
                [("user_id", ASCENDING), ("type", ASCENDING), ("size", ASCENDING)],
                name="idx_user_type_size"
            ),
            # Partition usage aggregation
            IndexModel(
                [("user_id", ASCENDING), ("partition_id", ASCENDING), ("type", ASCENDING), ("is_deleted", ASCENDING), ("size", ASCENDING)],
                name="idx_user_part_type_del_size"
            ),
            # Fallback single field lookups
            "name",
            "parent_id",
            "user_id",
        ]

    def __repr__(self) -> str:
        return f"<FileSystemItem(id={self.id}, name={self.name}, type={self.type}, user_id={self.user_id})>"


class StoragePartition(Document):
    """Represents a virtual storage partition created by a user."""
    
    user_id: str = Field(max_length=255)
    name: str = Field(max_length=255)
    allocated_size_bytes: int = Field()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_locked: bool = Field(default=False)
    lock_password_hash: Optional[str] = Field(default=None)

    class Settings:
        name = "storage_partitions"
        indexes = [
            "user_id",
        ]

    def __repr__(self) -> str:
        return f"<StoragePartition(id={self.id}, name={self.name}, user_id={self.user_id}, size={self.allocated_size_bytes})>"


class PaymentRecord(Document):
    """Represents a payment transaction / customer subscription record in Cashfree."""
    
    user_id: str = Field(..., max_length=255)
    customer_id: str = Field(..., max_length=255)
    customer_name: str = Field(default="", max_length=255)
    customer_email: str = Field(default="", max_length=255)
    customer_phone: str = Field(default="9999999999", max_length=50)
    order_id: str = Field(..., max_length=255)
    cf_order_id: Optional[str] = Field(default=None)
    cf_payment_id: Optional[str] = Field(default=None)
    payment_session_id: Optional[str] = Field(default=None)
    amount: float = Field(...)
    currency: str = Field(default="INR")
    plan_name: str = Field(..., max_length=50)
    billing_cycle: str = Field(default="monthly", max_length=20)
    status: str = Field(default="PENDING")  # "PENDING", "SUCCESS", "FAILED", "USER_DROPPED"
    payment_method: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    subscription_expires_at: Optional[datetime] = Field(default=None)
    raw_response: Optional[dict] = Field(default=None)

    class Settings:
        name = "payment_records"
        indexes = [
            "user_id",
            "order_id",
            "status",
            "customer_id",
        ]

    def __repr__(self) -> str:
        return f"<PaymentRecord(id={self.id}, user_id={self.user_id}, order_id={self.order_id}, status={self.status})>"


class CancellationRecord(Document):
    """Represents a subscription or trial cancellation record."""

    user_id: str = Field(..., max_length=255)
    customer_name: str = Field(default="", max_length=255)
    customer_email: str = Field(default="", max_length=255)
    plan_name: str = Field(default="free", max_length=50)
    billing_cycle: str = Field(default="monthly", max_length=20)
    is_trial: bool = Field(default=False)
    reason: Optional[str] = Field(default="User requested cancellation")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "cancellation_records"
        indexes = [
            "user_id",
            "customer_email",
            "plan_name",
            "created_at",
        ]

    def __repr__(self) -> str:
        return f"<CancellationRecord(id={self.id}, user_id={self.user_id}, plan={self.plan_name}, is_trial={self.is_trial})>"


# ── Billing v2 Document Models ────────────────────────────────────────────────


class Plan(Document):
    """Represents a subscription plan tier with pricing and entitlements.

    Stored as a document in the 'plans' MongoDB collection.
    """

    code: str = Field(..., max_length=100, description="Plan code, e.g. free, personal, plus, power")
    name: str = Field(..., max_length=255, description="Plan display name, e.g. Personal Plan")
    billing_interval: str = Field(..., max_length=50, description="monthly, annual, lifetime, free")
    amount_paise: int = Field(..., ge=0, description="Amount in integer paise (e.g. 11900 for ₹119.00)")
    currency: str = Field(default="INR", max_length=10)
    storage_quota_bytes: int = Field(..., gt=0, description="Storage quota limit in integer bytes")
    cashfree_plan_id: Optional[str] = Field(default=None, max_length=100, description="Cashfree recurring plan ID")
    is_active: bool = Field(default=True)
    version: int = Field(default=1, ge=1, description="Version number for preserving historical plan revisions")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "plans"
        indexes = [
            # Unique compound index for plan code, billing interval, and version
            IndexModel(
                [("code", ASCENDING), ("billing_interval", ASCENDING), ("version", ASCENDING)],
                unique=True,
                name="idx_plans_code_interval_version",
            ),
            # Active plan lookup by code and interval
            IndexModel(
                [("is_active", ASCENDING), ("code", ASCENDING), ("billing_interval", ASCENDING)],
                name="idx_plans_active_code_interval",
            ),
            # Cashfree Plan ID mapping lookup
            IndexModel(
                [("cashfree_plan_id", ASCENDING)],
                sparse=True,
                name="idx_plans_cf_plan_id",
            ),
        ]

    def __repr__(self) -> str:
        return f"<Plan(id={self.id}, code={self.code}, interval={self.billing_interval}, version={self.version})>"


class Subscription(Document):
    """Represents a recurring or lifetime subscription lifecycle for a user.

    Stored as a document in the 'subscriptions' MongoDB collection.
    """

    user_id: str = Field(..., max_length=255, description="Associated user ID")
    plan_id: str = Field(..., max_length=100, description="Reference to Plan code or ID")
    plan_snapshot: dict = Field(..., description="Immutable snapshot of plan details at subscription creation time")
    provider: str = Field(default="cashfree", max_length=50)
    cashfree_subscription_id: Optional[str] = Field(default=None, max_length=255, description="Cashfree mandate / subscription reference")
    status: str = Field(
        default="pending_authorization",
        max_length=50,
        description="pending_authorization, active, past_due, cancel_at_period_end, canceled, expired, suspended",
    )
    current_period_start: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    current_period_end: datetime = Field(..., description="End timestamp for current active billing period")
    next_billing_at: Optional[datetime] = Field(default=None, description="Next scheduled recurring charge timestamp")
    cancel_at_period_end: bool = Field(default=False, description="Flag if subscription will auto-terminate at period end")
    canceled_at: Optional[datetime] = Field(default=None)
    ended_at: Optional[datetime] = Field(default=None)
    grace_period_started_at: Optional[datetime] = Field(default=None, description="Timestamp when 14-day grace period started")
    grace_period_ends_at: Optional[datetime] = Field(default=None, description="Timestamp when 14-day grace period expires")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "subscriptions"
        indexes = [
            # Unique sparse index on Cashfree subscription ID
            IndexModel(
                [("cashfree_subscription_id", ASCENDING)],
                unique=True,
                sparse=True,
                name="idx_subs_cf_subscription_id",
            ),
            # User active subscription lookup
            IndexModel(
                [("user_id", ASCENDING), ("status", ASCENDING)],
                name="idx_subs_user_status",
            ),
            # Expiry and renewal scheduler query
            IndexModel(
                [("current_period_end", ASCENDING), ("status", ASCENDING)],
                name="idx_subs_period_end_status",
            ),
            # Grace period expiry tracking
            IndexModel(
                [("grace_period_ends_at", ASCENDING), ("status", ASCENDING)],
                sparse=True,
                name="idx_subs_grace_period_status",
            ),
        ]

    def __repr__(self) -> str:
        return f"<Subscription(id={self.id}, user_id={self.user_id}, plan={self.plan_id}, status={self.status})>"


class PaymentTransaction(Document):
    """Represents an individual payment transaction, renewal, upgrade, or refund.

    Stored as a document in the 'payment_transactions' MongoDB collection.
    """

    user_id: str = Field(..., max_length=255)
    subscription_id: Optional[str] = Field(default=None, max_length=255, description="Associated subscription reference")
    type: str = Field(
        default="initial_payment",
        max_length=50,
        description="initial_payment, renewal, upgrade, refund, adjustment, one_time_purchase",
    )
    provider: str = Field(default="cashfree", max_length=50)
    cashfree_order_id: Optional[str] = Field(default=None, max_length=255)
    cashfree_payment_id: Optional[str] = Field(default=None, max_length=255)
    amount_paise: int = Field(..., ge=0, description="Amount in integer paise")
    currency: str = Field(default="INR", max_length=10)
    status: str = Field(
        default="pending",
        max_length=50,
        description="pending, success, failed, user_dropped, refunded",
    )
    idempotency_key: str = Field(..., max_length=255, description="Unique idempotency key for preventing duplicate charges")
    billing_period_start: Optional[datetime] = Field(default=None)
    billing_period_end: Optional[datetime] = Field(default=None)
    failure_code: Optional[str] = Field(default=None, max_length=100)
    failure_message: Optional[str] = Field(default=None, max_length=1000)
    paid_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "payment_transactions"
        indexes = [
            # Strict uniqueness on idempotency key
            IndexModel(
                [("idempotency_key", ASCENDING)],
                unique=True,
                name="idx_paytx_idempotency_key",
            ),
            # Unique sparse provider + payment ID
            IndexModel(
                [("provider", ASCENDING), ("cashfree_payment_id", ASCENDING)],
                unique=True,
                partialFilterExpression={
                    "cashfree_payment_id": {
                        "$type": "string"
                    }
                },
                name="idx_paytx_provider_payment_id",
            ),
            # User transaction history lookup
            IndexModel(
                [("user_id", ASCENDING), ("created_at", DESCENDING)],
                name="idx_paytx_user_created",
            ),
            # Subscription payments lookup
            IndexModel(
                [("subscription_id", ASCENDING), ("status", ASCENDING)],
                sparse=True,
                name="idx_paytx_sub_status",
            ),
            # Cashfree Order ID lookup
            IndexModel(
                [("cashfree_order_id", ASCENDING)],
                sparse=True,
                name="idx_paytx_cf_order_id",
            ),
        ]

    def __repr__(self) -> str:
        return f"<PaymentTransaction(id={self.id}, user_id={self.user_id}, amount_paise={self.amount_paise}, status={self.status})>"


class WebhookEvent(Document):
    """Represents an incoming gateway webhook payload for idempotent asynchronous processing.

    Stored as a document in the 'webhook_events' MongoDB collection.
    """

    provider: str = Field(default="cashfree", max_length=50)
    event_id: str = Field(..., max_length=255, description="Provider-generated unique event ID or computed sha256 hash")
    event_type: str = Field(..., max_length=100, description="e.g. SUBSCRIPTION_PAYMENT_SUCCESS, SUBSCRIPTION_STATUS_CHANGE")
    cashfree_subscription_id: Optional[str] = Field(default=None, max_length=255)
    cashfree_order_id: Optional[str] = Field(default=None, max_length=255)
    payload: dict = Field(..., description="Raw JSON payload received from the gateway")
    signature_verified: bool = Field(default=False)
    status: str = Field(
        default="received",
        max_length=50,
        description="received, processing, processed, retry, failed, ignored",
    )
    attempts: int = Field(default=0, ge=0)
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    processed_at: Optional[datetime] = Field(default=None)
    last_error: Optional[str] = Field(default=None)
    next_retry_at: Optional[datetime] = Field(default=None)

    class Settings:
        name = "webhook_events"
        indexes = [
            # Unique provider + event ID to guarantee idempotent webhook delivery
            IndexModel(
                [("provider", ASCENDING), ("event_id", ASCENDING)],
                unique=True,
                name="idx_webhook_provider_event_id",
            ),
            # Retry worker lookup index
            IndexModel(
                [("status", ASCENDING), ("next_retry_at", ASCENDING)],
                name="idx_webhook_status_retry",
            ),
            # Audit listing by event type and time
            IndexModel(
                [("event_type", ASCENDING), ("received_at", DESCENDING)],
                name="idx_webhook_type_received",
            ),
            # Subscription webhook lookup
            IndexModel(
                [("cashfree_subscription_id", ASCENDING)],
                sparse=True,
                name="idx_webhook_cf_sub_id",
            ),
        ]

    def __repr__(self) -> str:
        return f"<WebhookEvent(id={self.id}, provider={self.provider}, event_id={self.event_id}, status={self.status})>"


class Entitlement(Document):
    """Represents the effective storage capacity, permissions, and quota lifecycle for a user.

    Enforces exactly one effective entitlement document per user.
    Stored as a document in the 'entitlements' MongoDB collection.
    """

    user_id: str = Field(..., max_length=255, description="Associated user ID")
    source_subscription_id: Optional[str] = Field(default=None, max_length=255, description="Originating Subscription ID")
    plan_code: str = Field(..., max_length=100, description="Active plan code, e.g. free, personal, plus, power")
    storage_quota_bytes: int = Field(..., gt=0, description="Total storage limit in integer bytes")
    billing_status: str = Field(
        default="active",
        max_length=50,
        description="active, trial, past_due, grace_period, canceled, expired, suspended",
    )
    can_upload: bool = Field(default=True, description="Enables or blocks new file uploads")
    can_download: bool = Field(default=True, description="Enables or blocks file downloads")
    can_manage_files: bool = Field(default=True, description="Enables folder creation, renaming, and organization")
    grace_period_ends_at: Optional[datetime] = Field(default=None, description="Billing grace period cutoff timestamp")
    over_quota_since: Optional[datetime] = Field(default=None, description="Timestamp when used storage first exceeded quota")
    retention_deadline: Optional[datetime] = Field(default=None, description="File retention safety deadline before cleanup actions")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "entitlements"
        indexes = [
            # Exactly one entitlement document per user
            IndexModel(
                [("user_id", ASCENDING)],
                unique=True,
                name="idx_entitlement_user_id_unique",
            ),
            # Grace-period expiry lookup
            IndexModel(
                [("billing_status", ASCENDING), ("grace_period_ends_at", ASCENDING)],
                sparse=True,
                name="idx_entitlement_status_grace",
            ),
            # Over-quota file retention deadline tracking
            IndexModel(
                [("can_upload", ASCENDING), ("retention_deadline", ASCENDING)],
                sparse=True,
                name="idx_entitlement_upload_retention",
            ),
        ]

    def __repr__(self) -> str:
        return f"<Entitlement(id={self.id}, user_id={self.user_id}, plan={self.plan_code}, quota={self.storage_quota_bytes})>"


class BillingAuditLog(Document):
    """Represents an append-only audit trail for billing, subscription, and quota changes.

    Stored as a document in the 'billing_audit_logs' MongoDB collection.
    """

    user_id: Optional[str] = Field(default=None, max_length=255)
    subscription_id: Optional[str] = Field(default=None, max_length=255)
    action: str = Field(..., max_length=100, description="e.g. PLAN_CREATED, SUBSCRIPTION_ACTIVATED, SUBSCRIPTION_CANCELLED")
    actor_type: str = Field(default="system", max_length=50, description="user, admin, system, webhook")
    actor_id: Optional[str] = Field(default=None, max_length=255)
    before: Optional[dict] = Field(default=None, description="State prior to mutation")
    after: Optional[dict] = Field(default=None, description="State following mutation")
    reason: Optional[str] = Field(default=None, max_length=1000)
    correlation_id: Optional[str] = Field(default=None, max_length=255, description="Trace ID linking related operations")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "billing_audit_logs"
        indexes = [
            # User audit history lookup
            IndexModel(
                [("user_id", ASCENDING), ("created_at", DESCENDING)],
                name="idx_audit_user_created",
            ),
            # Subscription audit history lookup
            IndexModel(
                [("subscription_id", ASCENDING), ("created_at", DESCENDING)],
                sparse=True,
                name="idx_audit_sub_created",
            ),
            # Action type tracking
            IndexModel(
                [("action", ASCENDING), ("created_at", DESCENDING)],
                name="idx_audit_action_created",
            ),
            # Correlation / Trace lookup
            IndexModel(
                [("correlation_id", ASCENDING)],
                sparse=True,
                name="idx_audit_correlation_id",
            ),
        ]

    def __repr__(self) -> str:
        return f"<BillingAuditLog(id={self.id}, user_id={self.user_id}, action={self.action}, actor={self.actor_type})>"


