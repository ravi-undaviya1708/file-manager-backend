"""Unit tests for Google Analytics 4 (GA4) Data API service and endpoint.

Tests strict zero-mock policy, validation logic, caching, and response schemas.
"""

import pytest
from unittest.mock import MagicMock, patch
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.models import User
from app.auth import hash_password, create_access_token
from app.google_analytics import (
    validate_property_id,
    format_duration,
    get_date_range_spec,
    fetch_realtime_metrics,
    fetch_historical_metrics,
    get_google_analytics_report_data,
    clear_analytics_cache,
)


def test_validate_property_id_valid():
    """Verify numeric property IDs are accepted and cleaned."""
    assert validate_property_id("123456789") == "123456789"
    assert validate_property_id("  987654321  ") == "987654321"
    assert validate_property_id("properties/123456789") == "123456789"


def test_validate_property_id_measurement_id_rejected():
    """Verify GA4 Measurement ID (G-XXXXXXXXXX) is explicitly rejected with clear error."""
    with pytest.raises(ValueError) as exc:
        validate_property_id("G-NV782X90LK")
    assert "Measurement ID" in str(exc.value)
    assert "numeric GA4 Property ID" in str(exc.value)

    with pytest.raises(ValueError):
        validate_property_id("g-1234567890")


def test_validate_property_id_invalid_formats():
    """Verify non-numeric or empty property IDs are rejected."""
    with pytest.raises(ValueError):
        validate_property_id(None)

    with pytest.raises(ValueError):
        validate_property_id("")

    with pytest.raises(ValueError):
        validate_property_id("my-ga4-property")


def test_format_duration():
    """Verify seconds are correctly converted to readable duration."""
    assert format_duration(0) == "0s"
    assert format_duration(45) == "45s"
    assert format_duration(135) == "2m 15s"
    assert format_duration(3600) == "60m 0s"


def test_date_range_spec():
    """Verify date ranges are properly mapped to GA4 DateRange specs."""
    assert get_date_range_spec("today").start_date == "today"
    assert get_date_range_spec("yesterday").start_date == "yesterday"
    assert get_date_range_spec("7d").start_date == "7daysAgo"
    assert get_date_range_spec("30d").start_date == "30daysAgo"
    assert get_date_range_spec("90d").start_date == "90daysAgo"


def test_realtime_metrics_parser():
    """Verify realtime API responses are accurately mapped."""
    mock_client = MagicMock()

    # Mock responses for 5 realtime requests
    # 1. Main
    res_main = MagicMock()
    row_main = MagicMock()
    val_users = MagicMock(value="14")
    val_views = MagicMock(value="42")
    val_events = MagicMock(value="89")
    val_keys = MagicMock(value="3")
    row_main.metric_values = [val_users, val_views, val_events, val_keys]
    res_main.rows = [row_main]

    # 2. Devices
    res_dev = MagicMock()
    row_d1 = MagicMock()
    row_d1.dimension_values = [MagicMock(value="desktop")]
    row_d1.metric_values = [MagicMock(value="10")]
    row_d2 = MagicMock()
    row_d2.dimension_values = [MagicMock(value="mobile")]
    row_d2.metric_values = [MagicMock(value="4")]
    res_dev.rows = [row_d1, row_d2]

    # 3. Countries
    res_geo = MagicMock()
    row_g1 = MagicMock()
    row_g1.dimension_values = [MagicMock(value="India")]
    row_g1.metric_values = [MagicMock(value="14")]
    res_geo.rows = [row_g1]

    # 4. Pages
    res_pages = MagicMock()
    row_p1 = MagicMock()
    row_p1.dimension_values = [MagicMock(value="GetFileNova Dashboard")]
    row_p1.metric_values = [MagicMock(value="28")]
    res_pages.rows = [row_p1]

    # 5. Events
    res_ev = MagicMock()
    row_e1 = MagicMock()
    row_e1.dimension_values = [MagicMock(value="login")]
    row_e1.metric_values = [MagicMock(value="12"), MagicMock(value="0")]
    res_ev.rows = [row_e1]

    mock_client.run_realtime_report.side_effect = [res_main, res_dev, res_geo, res_pages, res_ev]

    result = fetch_realtime_metrics(mock_client, "123456789")

    assert result["activeUsers"] == 14
    assert result["pageViews"] == 42
    assert result["eventCount"] == 89
    assert result["keyEvents"] == 3
    assert len(result["devices"]) == 2
    assert result["devices"][0]["device"] == "desktop"
    assert result["devices"][0]["percentage"] == 71.4
    assert len(result["countries"]) == 1
    assert result["countries"][0]["country"] == "India"
    assert len(result["topPages"]) == 1
    assert result["topPages"][0]["page"] == "GetFileNova Dashboard"
    assert result["topPages"][0]["views"] == 28


def test_historical_metrics_parser():
    """Verify historical API responses are accurately mapped."""
    mock_client = MagicMock()

    # 1. Main KPIs
    res_main = MagicMock()
    row_main = MagicMock()
    row_main.metric_values = [
        MagicMock(value="1250"),  # totalUsers
        MagicMock(value="1180"),  # activeUsers
        MagicMock(value="420"),   # newUsers
        MagicMock(value="1890"),  # sessions
        MagicMock(value="5420"),  # pageviews
        MagicMock(value="195.4"), # avgSessionDuration
        MagicMock(value="0.284"), # bounceRate (ratio)
    ]
    res_main.rows = [row_main]

    # 2. Traffic
    res_traffic = MagicMock()
    row_t1 = MagicMock()
    row_t1.dimension_values = [MagicMock(value="Organic Search")]
    row_t1.metric_values = [MagicMock(value="800"), MagicMock(value="600")]
    res_traffic.rows = [row_t1]

    # 3. Devices
    res_dev = MagicMock()
    row_d1 = MagicMock()
    row_d1.dimension_values = [MagicMock(value="desktop")]
    row_d1.metric_values = [MagicMock(value="1200"), MagicMock(value="800")]
    res_dev.rows = [row_d1]

    # 4. Pages
    res_pages = MagicMock()
    row_p1 = MagicMock()
    row_p1.dimension_values = [MagicMock(value="/dashboard"), MagicMock(value="GetFileNova Drive")]
    row_p1.metric_values = [MagicMock(value="2400"), MagicMock(value="800"), MagicMock(value="7200.0")]
    res_pages.rows = [row_p1]

    # 5. Countries
    res_geo = MagicMock()
    row_g1 = MagicMock()
    row_g1.dimension_values = [MagicMock(value="India"), MagicMock(value="IN")]
    row_g1.metric_values = [MagicMock(value="750"), MagicMock(value="1100")]
    res_geo.rows = [row_g1]

    # 6. Events
    res_ev = MagicMock()
    row_e1 = MagicMock()
    row_e1.dimension_values = [MagicMock(value="signup_completed")]
    row_e1.metric_values = [MagicMock(value="150"), MagicMock(value="150")]
    res_ev.rows = [row_e1]

    mock_client.run_report.side_effect = [res_main, res_traffic, res_dev, res_pages, res_geo, res_ev]

    result = fetch_historical_metrics(mock_client, "123456789", "30d")

    assert result["totalUsers"] == 1250
    assert result["activeUsers"] == 1180
    assert result["newUsers"] == 420
    assert result["sessions"] == 1890
    assert result["pageviews"] == 5420
    assert result["avgSessionDuration"] == "3m 15s"
    assert result["bounceRate"] == 28.4
    assert len(result["trafficSources"]) == 1
    assert result["trafficSources"][0]["source"] == "Organic Search"
    assert result["trafficSources"][0]["percentage"] == 100.0
    assert len(result["deviceDistribution"]) == 1
    assert "Desktop" in result["deviceDistribution"][0]["device"]
    assert len(result["topPages"]) == 1
    assert result["topPages"][0]["path"] == "/dashboard"
    assert result["topPages"][0]["avgTime"] == "3s"
    assert len(result["countryDistribution"]) == 1
    assert result["countryDistribution"][0]["country"] == "India"
    assert result["countryDistribution"][0]["code"] == "IN"
    assert len(result["events"]) == 1
    assert result["events"][0]["eventName"] == "signup_completed"


@pytest.mark.asyncio
async def test_admin_google_analytics_endpoint_unconfigured_error():
    """Verify endpoint returns 400 with actionable error when GA4 credentials are missing."""
    clear_analytics_cache()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await User.find_one(User.email == "test_ga_admin@example.com")
        if not admin:
            admin = User(
                email="test_ga_admin@example.com",
                name="GA Admin",
                hashed_password=hash_password("adminpass123"),
                is_admin=True,
                user_type="superAdmin",
                pricing_plan="power"
            )
            await admin.insert()

        token = create_access_token(data={"sub": str(admin.id)})
        headers = {"Authorization": f"Bearer {token}"}

        with patch("app.google_analytics.get_settings") as mock_settings:
            mock_settings.return_value.GA4_PROPERTY_ID = None
            mock_settings.return_value.GOOGLE_SERVICE_ACCOUNT_EMAIL = None
            mock_settings.return_value.GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY = None
            mock_settings.return_value.GOOGLE_APPLICATION_CREDENTIALS = None

            res = await client.get("/api/admin/analytics/google-analytics", headers=headers)
            assert res.status_code == 400
            data = res.json()
            assert "detail" in data
            assert "GA4_PROPERTY_ID is not configured" in str(data["detail"])


@pytest.mark.asyncio
async def test_admin_google_analytics_endpoint_measurement_id_error():
    """Verify endpoint returns 400 when Measurement ID G-XXXXXXXXXX is provided instead of Property ID."""
    clear_analytics_cache()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await User.find_one(User.email == "test_ga_admin@example.com")
        if not admin:
            admin = User(
                email="test_ga_admin@example.com",
                name="GA Admin",
                hashed_password=hash_password("adminpass123"),
                is_admin=True,
                user_type="superAdmin",
                pricing_plan="power"
            )
            await admin.insert()
        token = create_access_token(data={"sub": str(admin.id)})
        headers = {"Authorization": f"Bearer {token}"}

        with patch("app.google_analytics.get_settings") as mock_settings:
            mock_settings.return_value.GA4_PROPERTY_ID = "G-NV782X90LK"
            mock_settings.return_value.GOOGLE_SERVICE_ACCOUNT_EMAIL = "sa@test.iam.gserviceaccount.com"
            mock_settings.return_value.formatted_google_private_key = "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----"
            mock_settings.return_value.GOOGLE_APPLICATION_CREDENTIALS = None

            res = await client.get("/api/admin/analytics/google-analytics", headers=headers)
            assert res.status_code == 400
            data = res.json()
            assert "Measurement ID" in str(data["detail"])


@pytest.mark.asyncio
async def test_admin_google_analytics_endpoint_success():
    """Verify endpoint successfully returns structured GA4 telemetry when Data API responds."""
    clear_analytics_cache()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await User.find_one(User.email == "test_ga_admin@example.com")
        if not admin:
            admin = User(
                email="test_ga_admin@example.com",
                name="GA Admin",
                hashed_password=hash_password("adminpass123"),
                is_admin=True,
                user_type="superAdmin",
                pricing_plan="power"
            )
            await admin.insert()
        token = create_access_token(data={"sub": str(admin.id)})
        headers = {"Authorization": f"Bearer {token}"}

        mock_report_payload = {
            "propertyId": "987654321",
            "status": "CONNECTED_ACTIVE",
            "isRealtimeActive": True,
            "dateRange": "30d",
            "updatedAt": "2026-09-18T00:00:00Z",
            "realtimeActiveUsers": 12,
            "totalUsers": 2400,
            "newUsers": 950,
            "sessions": 3800,
            "pageviews": 10500,
            "avgSessionDuration": "2m 45s",
            "avgSessionDurationSeconds": 165.0,
            "bounceRate": 24.5,
            "trafficSources": [
                {"source": "Organic Search", "sessions": 1900, "users": 1200, "percentage": 50.0, "color": "#4f46e5"}
            ],
            "deviceDistribution": [
                {"device": "Desktop", "sessions": 2600, "users": 1700, "percentage": 68.4, "color": "#4f46e5"}
            ],
            "topPages": [
                {"path": "/", "title": "Home", "pageviews": 5000, "uniquePageviews": 1800, "avgTime": "1m 30s"}
            ],
            "countryDistribution": [
                {"country": "India", "code": "IN", "users": 1500, "percentage": 62.5}
            ],
            "events": [
                {"eventName": "login", "eventCount": 420, "keyEvents": 0}
            ],
        }

        with patch("app.google_analytics.get_google_analytics_report_data", return_value=mock_report_payload):
            res = await client.get("/api/admin/analytics/google-analytics?range=30d", headers=headers)
            assert res.status_code == 200
            data = res.json()

            assert data["propertyId"] == "987654321"
            assert data["status"] == "CONNECTED_ACTIVE"
            assert data["realtimeActiveUsers"] == 12
            assert data["totalUsers"] == 2400
            assert data["newUsers"] == 950
            assert data["sessions"] == 3800
            assert data["pageviews"] == 10500
            assert data["avgSessionDuration"] == "2m 45s"
            assert data["bounceRate"] == 24.5
            assert len(data["trafficSources"]) == 1
            assert len(data["deviceDistribution"]) == 1
            assert len(data["topPages"]) == 1
            assert len(data["countryDistribution"]) == 1
            assert len(data["events"]) == 1
