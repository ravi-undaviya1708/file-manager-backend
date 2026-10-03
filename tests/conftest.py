"""Pytest configuration and test database fixtures."""

import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from beanie import init_beanie

from app.models import (
    User,
    FileSystemItem,
    StoragePartition,
    Role,
    PaymentRecord,
    CancellationRecord,
    Plan,
    Subscription,
    PaymentTransaction,
    WebhookEvent,
    Entitlement,
    BillingAuditLog,
)
import app.database


@pytest_asyncio.fixture(autouse=True)
async def init_test_database():
    """Set up an isolated in-memory MongoMock database for testing."""
    client = AsyncMongoMockClient()
    db = client["test_file_manager"]
    app.database.client = client
    app.database.database = db

    await init_beanie(
        database=db,
        document_models=[
            FileSystemItem,
            User,
            StoragePartition,
            Role,
            PaymentRecord,
            CancellationRecord,
            Plan,
            Subscription,
            PaymentTransaction,
            WebhookEvent,
            Entitlement,
            BillingAuditLog,
        ],
    )
    yield db
    # Teardown
    await User.find_all().delete()
    await FileSystemItem.find_all().delete()
    await StoragePartition.find_all().delete()
    await Role.find_all().delete()
    await PaymentRecord.find_all().delete()
    await CancellationRecord.find_all().delete()
    await Plan.find_all().delete()
    await Subscription.find_all().delete()
    await PaymentTransaction.find_all().delete()
    await WebhookEvent.find_all().delete()
    await Entitlement.find_all().delete()
    await BillingAuditLog.find_all().delete()
