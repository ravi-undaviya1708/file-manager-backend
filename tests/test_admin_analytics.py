"""Tests for Super Admin platform analytics telemetry and KPI aggregation endpoints."""

import pytest
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone, timedelta
from app.main import app
from app.models import User, FileSystemItem, PaymentRecord, CancellationRecord
from app.auth import hash_password, create_access_token


@pytest.mark.asyncio
async def test_admin_analytics_overview_unauthorized():
    """Verify standard users without is_admin cannot access analytics."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Request without token
        res = await client.get("/api/admin/analytics/overview")
        assert res.status_code in [401, 403]

        # Request with standard individual user token
        user = await User.find_one(User.email == "test_standard_user_analytics@example.com")
        if not user:
            user = User(
                email="test_standard_user_analytics@example.com",
                name="Regular User",
                hashed_password=hash_password("password123"),
                is_admin=False,
                user_type="individual",
                pricing_plan="free"
            )
            await user.insert()

        token = create_access_token(data={"sub": str(user.id)})
        res = await client.get(
            "/api/admin/analytics/overview",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert res.status_code == 403


@pytest.mark.asyncio
async def test_admin_analytics_overview_success():
    """Verify Super Admin can fetch overview analytics with all KPIs and charts."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create or find admin user
        admin = await User.find_one(User.email == "test_analytics_admin@example.com")
        if not admin:
            admin = User(
                email="test_analytics_admin@example.com",
                name="Ravi Admin",
                hashed_password=hash_password("adminpass123"),
                is_admin=True,
                user_type="superAdmin",
                pricing_plan="power"
            )
            await admin.insert()

        token = create_access_token(data={"sub": str(admin.id)})
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Test Overview 30d (default)
        res = await client.get("/api/admin/analytics/overview?range=30d", headers=headers)
        assert res.status_code == 200
        data = res.json()

        assert "totalAccounts" in data
        assert "activeUsers" in data
        assert "newSignups" in data
        assert "activeSubscriptions" in data
        assert "trialUsers" in data
        assert "canceledUsers" in data
        assert "storageUsage" in data
        assert "userGrowth" in data
        assert "storageTrend" in data
        assert "subscriptionPlans" in data
        assert "userStatusDistribution" in data
        assert "trialMetrics" in data
        assert "cancellationMetrics" in data

        assert isinstance(data["totalAccounts"]["value"], (int, float))
        assert isinstance(data["userGrowth"], list)
        assert isinstance(data["storageTrend"], list)
        assert len(data["userGrowth"]) > 0
        assert len(data["storageTrend"]) > 0

        # 2. Test Top Storage Users
        res_top = await client.get("/api/admin/analytics/top-storage-users?limit=5", headers=headers)
        assert res_top.status_code == 200
        top_users = res_top.json()
        assert isinstance(top_users, list)

        # 3. Test Recent Signups
        res_signups = await client.get("/api/admin/analytics/recent-signups?limit=5", headers=headers)
        assert res_signups.status_code == 200
        signups = res_signups.json()
        assert isinstance(signups, list)

        # 4. Test Recent Payments
        res_payments = await client.get("/api/admin/analytics/recent-payments?limit=5", headers=headers)
        assert res_payments.status_code == 200
        payments = res_payments.json()
        assert isinstance(payments, list)

        # 5. Test Recent Cancellations
        res_cancels = await client.get("/api/admin/analytics/recent-cancellations?limit=5", headers=headers)
        assert res_cancels.status_code == 200
        cancels = res_cancels.json()
        assert isinstance(cancels, list)


@pytest.mark.asyncio
async def test_admin_analytics_date_ranges_and_cancellation():
    """Verify various date ranges and recording cancellations."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await User.find_one(User.email == "test_analytics_admin2@example.com")
        if not admin:
            admin = User(
                email="test_analytics_admin2@example.com",
                name="Ravi Admin Two",
                hashed_password=hash_password("adminpass123"),
                is_admin=True,
                user_type="superAdmin",
                pricing_plan="power"
            )
            await admin.insert()

        token = create_access_token(data={"sub": str(admin.id)})
        headers = {"Authorization": f"Bearer {token}"}

        # Test today, 7d, 90d, year
        for r in ["today", "7d", "90d", "year"]:
            res = await client.get(f"/api/admin/analytics/overview?range={r}", headers=headers)
            assert res.status_code == 200
            assert res.json()["dateRange"] == r

        # Test custom range
        now = datetime.now(timezone.utc)
        start_iso = (now - timedelta(days=14)).isoformat()
        end_iso = now.isoformat()
        res_custom = await client.get(
            f"/api/admin/analytics/overview?range=custom&startDate={start_iso}&endDate={end_iso}",
            headers=headers
        )
        assert res_custom.status_code == 200

        # Test record cancellation
        test_user = User(
            email="to_cancel_user@example.com",
            name="Cancel Target",
            pricing_plan="plus",
            subscription_status="active"
        )
        await test_user.insert()

        cancel_res = await client.post(
            "/api/admin/analytics/record-cancellation",
            json={"userId": str(test_user.id), "reason": "Switched to alternative service"},
            headers=headers
        )
        assert cancel_res.status_code == 200

        # Verify recent-cancellations includes this record
        cancels = (await client.get("/api/admin/analytics/recent-cancellations?limit=10", headers=headers)).json()
        assert any(c["userEmail"] == "to_cancel_user@example.com" for c in cancels)
