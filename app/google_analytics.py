"""Centralized Google Analytics 4 (GA4) Service.

Communicates with the official Google Analytics Data API (v1beta) using
server-side Google Service Account credentials.
Strictly adheres to Zero Mock Policy: Never returns fabricated, estimated, or mock data.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    OrderBy,
    RunRealtimeReportRequest,
    RunReportRequest,
)
from google.auth.exceptions import GoogleAuthError
from google.api_core.exceptions import GoogleAPIError, PermissionDenied, Unauthenticated, InvalidArgument
from google.oauth2 import service_account

from app.config import get_settings

logger = logging.getLogger("app.google_analytics")

# ── Color Palette for Dynamic Charts ──────────────────────────────────────────
CHANNEL_COLORS: Dict[str, str] = {
    "organic search": "#4f46e5",
    "direct": "#3b82f6",
    "referral": "#10b981",
    "organic social": "#a855f7",
    "social": "#a855f7",
    "email": "#f59e0b",
    "paid search": "#ec4899",
    "display": "#06b6d4",
    "unassigned": "#64748b",
}

DEVICE_COLORS: Dict[str, str] = {
    "desktop": "#4f46e5",
    "mobile": "#06b6d4",
    "tablet": "#a855f7",
    "smart tv": "#f59e0b",
}

FALLBACK_COLORS = ["#4f46e5", "#3b82f6", "#10b981", "#a855f7", "#f59e0b", "#06b6d4", "#ec4899", "#64748b"]

# ── In-Memory Cache ───────────────────────────────────────────────────────────
_CACHE_STORE: Dict[str, Tuple[float, Any]] = {}
REALTIME_CACHE_TTL_SECONDS = 30
HISTORICAL_CACHE_TTL_SECONDS = 300


def clear_analytics_cache() -> None:
    """Clear in-memory analytics cache."""
    global _CACHE_STORE
    _CACHE_STORE.clear()


def validate_property_id(property_id: Optional[str]) -> str:
    """Validate that the GA4 Property ID is a valid numeric ID and not a Measurement ID.
    
    Raises ValueError with actionable instructions if invalid.
    """
    if not property_id or not property_id.strip():
        raise ValueError(
            "GA4_PROPERTY_ID is not configured. Please set GA4_PROPERTY_ID in your environment variables."
        )

    clean_id = property_id.strip()

    # Detect Measurement ID (e.g. G-XXXXXXXXXX)
    if clean_id.upper().startswith("G-"):
        raise ValueError(
            f"Invalid GA4_PROPERTY_ID '{clean_id}': You provided a GA4 Measurement ID ('G-XXXXXXXXXX') "
            "instead of a numeric GA4 Property ID. The Google Analytics Data API requires the numeric Property ID "
            "(e.g., '123456789') found in Google Analytics -> Admin -> Property Details."
        )

    # Strip optional 'properties/' prefix if provided
    if clean_id.startswith("properties/"):
        clean_id = clean_id.replace("properties/", "")

    if not clean_id.isdigit():
        raise ValueError(
            f"Invalid GA4_PROPERTY_ID '{clean_id}': GA4 Property ID must be a numeric string (e.g., '123456789'). "
            "Do not use tracking IDs, measurement IDs, or alphanumeric property names."
        )

    return clean_id


def get_analytics_client() -> Tuple[BetaAnalyticsDataClient, str]:
    """Initialize and return the Google Analytics Data API client and validated Property ID.
    
    Raises ValueError if configuration is missing or invalid.
    """
    settings = get_settings()
    prop_id = validate_property_id(settings.GA4_PROPERTY_ID)

    if settings.GOOGLE_APPLICATION_CREDENTIALS:
        try:
            client = BetaAnalyticsDataClient.from_service_account_file(settings.GOOGLE_APPLICATION_CREDENTIALS)
            return client, prop_id
        except Exception as exc:
            logger.error("Failed to initialize GA4 client from GOOGLE_APPLICATION_CREDENTIALS file: %s", exc)
            raise ValueError(f"Failed to load Google credentials file: {exc}")

    if settings.GOOGLE_SERVICE_ACCOUNT_EMAIL and settings.formatted_google_private_key:
        try:
            credentials_info = {
                "type": "service_account",
                "client_email": settings.GOOGLE_SERVICE_ACCOUNT_EMAIL.strip(),
                "private_key": settings.formatted_google_private_key,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
            credentials = service_account.Credentials.from_service_account_info(
                credentials_info,
                scopes=["https://www.googleapis.com/auth/analytics.readonly"]
            )
            client = BetaAnalyticsDataClient(credentials=credentials)
            return client, prop_id
        except Exception as exc:
            logger.error("Failed to initialize GA4 client from service account credentials: %s", exc)
            raise ValueError(f"Failed to initialize Google Service Account credentials: {exc}")

    raise ValueError(
        "Google Analytics authorization is not configured. Please set GOOGLE_SERVICE_ACCOUNT_EMAIL and "
        "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY (or GOOGLE_APPLICATION_CREDENTIALS) in your environment variables."
    )


def format_duration(seconds: float) -> str:
    """Format duration in seconds into 'Xm Ys' or 'Xs'."""
    sec = int(round(seconds))
    if sec <= 0:
        return "0s"
    minutes = sec // 60
    remaining_sec = sec % 60
    if minutes > 0:
        return f"{minutes}m {remaining_sec}s"
    return f"{remaining_sec}s"


def get_date_range_spec(range_str: str) -> DateRange:
    """Map dashboard range string to Google Analytics DateRange."""
    r = range_str.lower().strip()
    if r == "today":
        return DateRange(start_date="today", end_date="today")
    elif r == "yesterday":
        return DateRange(start_date="yesterday", end_date="yesterday")
    elif r in ["7d", "last 7 days", "7days"]:
        return DateRange(start_date="7daysAgo", end_date="today")
    elif r in ["90d", "last 90 days", "90days"]:
        return DateRange(start_date="90daysAgo", end_date="today")
    # Default 30 days
    return DateRange(start_date="30daysAgo", end_date="today")


# ── Realtime Telemetry ────────────────────────────────────────────────────────

def fetch_realtime_metrics(client: BetaAnalyticsDataClient, prop_id: str) -> Dict[str, Any]:
    """Fetch realtime telemetry using runRealtimeReport."""
    property_name = f"properties/{prop_id}"

    # 1. Realtime Active Users, Views, Events
    req_main = RunRealtimeReportRequest(
        property=property_name,
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="screenPageViews"),
            Metric(name="eventCount"),
            Metric(name="keyEvents"),
        ]
    )

    # 2. Realtime Devices
    req_devices = RunRealtimeReportRequest(
        property=property_name,
        dimensions=[Dimension(name="deviceCategory")],
        metrics=[Metric(name="activeUsers")],
    )

    # 3. Realtime Countries
    req_countries = RunRealtimeReportRequest(
        property=property_name,
        dimensions=[Dimension(name="country")],
        metrics=[Metric(name="activeUsers")],
        limit=10,
    )

    # 4. Realtime Top Pages
    req_pages = RunRealtimeReportRequest(
        property=property_name,
        dimensions=[Dimension(name="unifiedScreenName")],
        metrics=[Metric(name="screenPageViews")],
        limit=10,
    )

    # 5. Realtime Events
    req_events = RunRealtimeReportRequest(
        property=property_name,
        dimensions=[Dimension(name="eventName")],
        metrics=[Metric(name="eventCount"), Metric(name="keyEvents")],
        limit=15,
    )

    res_main = client.run_realtime_report(req_main)
    res_devices = client.run_realtime_report(req_devices)
    res_countries = client.run_realtime_report(req_countries)
    res_pages = client.run_realtime_report(req_pages)
    res_events = client.run_realtime_report(req_events)

    # Parse main metrics
    active_users = 0
    page_views = 0
    event_count = 0
    key_events = 0

    if res_main.rows:
        row = res_main.rows[0]
        active_users = int(row.metric_values[0].value) if len(row.metric_values) > 0 else 0
        page_views = int(row.metric_values[1].value) if len(row.metric_values) > 1 else 0
        event_count = int(row.metric_values[2].value) if len(row.metric_values) > 2 else 0
        key_events = int(row.metric_values[3].value) if len(row.metric_values) > 3 else 0

    # Parse Realtime Devices
    realtime_devices: List[Dict[str, Any]] = []
    total_device_users = 0
    for r in res_devices.rows:
        dev_name = r.dimension_values[0].value or "unknown"
        dev_users = int(r.metric_values[0].value) if r.metric_values else 0
        total_device_users += dev_users
        realtime_devices.append({"device": dev_name, "users": dev_users})

    for d in realtime_devices:
        pct = round((d["users"] / total_device_users * 100), 1) if total_device_users > 0 else 0.0
        d["percentage"] = pct
        d["color"] = DEVICE_COLORS.get(d["device"].lower(), "#64748b")

    # Parse Realtime Countries
    realtime_countries: List[Dict[str, Any]] = []
    total_country_users = 0
    for r in res_countries.rows:
        c_name = r.dimension_values[0].value or "Unknown"
        c_users = int(r.metric_values[0].value) if r.metric_values else 0
        total_country_users += c_users
        realtime_countries.append({"country": c_name, "users": c_users})

    for c in realtime_countries:
        pct = round((c["users"] / total_country_users * 100), 1) if total_country_users > 0 else 0.0
        c["percentage"] = pct

    # Parse Realtime Top Pages
    realtime_pages: List[Dict[str, Any]] = []
    for r in res_pages.rows:
        p_name = r.dimension_values[0].value or "(not set)"
        p_views = int(r.metric_values[0].value) if r.metric_values else 0
        realtime_pages.append({"page": p_name, "views": p_views})

    # Parse Realtime Events
    realtime_events_list: List[Dict[str, Any]] = []
    for r in res_events.rows:
        e_name = r.dimension_values[0].value or "unknown"
        e_count = int(r.metric_values[0].value) if len(r.metric_values) > 0 else 0
        e_key = int(r.metric_values[1].value) if len(r.metric_values) > 1 else 0
        realtime_events_list.append({"eventName": e_name, "eventCount": e_count, "keyEvents": e_key})

    return {
        "activeUsers": active_users,
        "pageViews": page_views,
        "eventCount": event_count,
        "keyEvents": key_events,
        "devices": realtime_devices,
        "countries": realtime_countries,
        "topPages": realtime_pages,
        "events": realtime_events_list,
    }


# ── Historical Telemetry ──────────────────────────────────────────────────────

def fetch_historical_metrics(client: BetaAnalyticsDataClient, prop_id: str, date_range_str: str) -> Dict[str, Any]:
    """Fetch historical telemetry using runReport."""
    property_name = f"properties/{prop_id}"
    date_range = get_date_range_spec(date_range_str)

    # 1. Main Overview KPI metrics
    req_main = RunReportRequest(
        property=property_name,
        date_ranges=[date_range],
        metrics=[
            Metric(name="totalUsers"),
            Metric(name="activeUsers"),
            Metric(name="newUsers"),
            Metric(name="sessions"),
            Metric(name="screenPageViews"),
            Metric(name="averageSessionDuration"),
            Metric(name="bounceRate"),
        ],
    )

    # 2. Traffic Acquisition Channels (sessionDefaultChannelGroup)
    req_traffic = RunReportRequest(
        property=property_name,
        date_ranges=[date_range],
        dimensions=[Dimension(name="sessionDefaultChannelGroup")],
        metrics=[Metric(name="sessions"), Metric(name="activeUsers")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        limit=10,
    )

    # 3. Device Category Distribution
    req_devices = RunReportRequest(
        property=property_name,
        date_ranges=[date_range],
        dimensions=[Dimension(name="deviceCategory")],
        metrics=[Metric(name="sessions"), Metric(name="activeUsers")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
    )

    # 4. Top Viewed Pages & Screen Views
    req_pages = RunReportRequest(
        property=property_name,
        date_ranges=[date_range],
        dimensions=[Dimension(name="pagePath"), Dimension(name="pageTitle")],
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="activeUsers"),
            Metric(name="userEngagementDuration"),
        ],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
        limit=15,
    )

    # 5. Geographic Audience Distribution
    req_countries = RunReportRequest(
        property=property_name,
        date_ranges=[date_range],
        dimensions=[Dimension(name="country"), Dimension(name="countryId")],
        metrics=[Metric(name="activeUsers"), Metric(name="sessions")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="activeUsers"), desc=True)],
        limit=10,
    )

    # 6. Event Analytics
    req_events = RunReportRequest(
        property=property_name,
        date_ranges=[date_range],
        dimensions=[Dimension(name="eventName")],
        metrics=[Metric(name="eventCount"), Metric(name="keyEvents")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="eventCount"), desc=True)],
        limit=20,
    )

    res_main = client.run_report(req_main)
    res_traffic = client.run_report(req_traffic)
    res_devices = client.run_report(req_devices)
    res_pages = client.run_report(req_pages)
    res_countries = client.run_report(req_countries)
    res_events = client.run_report(req_events)

    # Parse main KPIs
    total_users = 0
    active_users = 0
    new_users = 0
    sessions = 0
    pageviews = 0
    avg_session_duration_sec = 0.0
    bounce_rate = 0.0

    if res_main.rows:
        row = res_main.rows[0]
        vals = row.metric_values
        total_users = int(vals[0].value) if len(vals) > 0 and vals[0].value else 0
        active_users = int(vals[1].value) if len(vals) > 1 and vals[1].value else 0
        new_users = int(vals[2].value) if len(vals) > 2 and vals[2].value else 0
        sessions = int(vals[3].value) if len(vals) > 3 and vals[3].value else 0
        pageviews = int(vals[4].value) if len(vals) > 4 and vals[4].value else 0
        avg_session_duration_sec = float(vals[5].value) if len(vals) > 5 and vals[5].value else 0.0
        # bounceRate from GA4 is a ratio between 0.0 and 1.0; convert to percentage 0-100%
        raw_bounce = float(vals[6].value) if len(vals) > 6 and vals[6].value else 0.0
        bounce_rate = round(raw_bounce * 100.0, 1) if raw_bounce <= 1.0 else round(raw_bounce, 1)

    # Parse Traffic Sources
    traffic_sources: List[Dict[str, Any]] = []
    total_channel_sessions = 0
    for idx, r in enumerate(res_traffic.rows):
        src_name = r.dimension_values[0].value or "Unassigned"
        src_sessions = int(r.metric_values[0].value) if r.metric_values else 0
        src_users = int(r.metric_values[1].value) if len(r.metric_values) > 1 else 0
        total_channel_sessions += src_sessions
        color = CHANNEL_COLORS.get(src_name.lower(), FALLBACK_COLORS[idx % len(FALLBACK_COLORS)])
        traffic_sources.append({
            "source": src_name,
            "sessions": src_sessions,
            "users": src_users,
            "color": color,
        })

    for s in traffic_sources:
        pct = round((s["sessions"] / total_channel_sessions * 100), 1) if total_channel_sessions > 0 else 0.0
        s["percentage"] = pct

    # Parse Device Distribution
    device_dist: List[Dict[str, Any]] = []
    total_dev_sessions = 0
    for idx, r in enumerate(res_devices.rows):
        d_name = r.dimension_values[0].value or "Unknown"
        d_sessions = int(r.metric_values[0].value) if r.metric_values else 0
        d_users = int(r.metric_values[1].value) if len(r.metric_values) > 1 else 0
        total_dev_sessions += d_sessions
        color = DEVICE_COLORS.get(d_name.lower(), FALLBACK_COLORS[idx % len(FALLBACK_COLORS)])
        formatted_name = d_name.capitalize()
        if formatted_name.lower() == "desktop":
            formatted_name = "Desktop (macOS / Windows / Linux)"
        elif formatted_name.lower() == "mobile":
            formatted_name = "Mobile (iOS / Android)"
        elif formatted_name.lower() == "tablet":
            formatted_name = "Tablet (iPad / Android Tablet)"

        device_dist.append({
            "device": formatted_name,
            "sessions": d_sessions,
            "users": d_users,
            "color": color,
        })

    for d in device_dist:
        pct = round((d["sessions"] / total_dev_sessions * 100), 1) if total_dev_sessions > 0 else 0.0
        d["percentage"] = pct

    # Parse Top Pages
    top_pages: List[Dict[str, Any]] = []
    for r in res_pages.rows:
        path = r.dimension_values[0].value or "/"
        title = r.dimension_values[1].value or path
        views = int(r.metric_values[0].value) if len(r.metric_values) > 0 else 0
        uniq_users = int(r.metric_values[1].value) if len(r.metric_values) > 1 else 0
        engagement_sec = float(r.metric_values[2].value) if len(r.metric_values) > 2 else 0.0
        avg_time_sec = (engagement_sec / views) if views > 0 else 0.0
        top_pages.append({
            "path": path,
            "title": title,
            "pageviews": views,
            "uniquePageviews": uniq_users,
            "avgTime": format_duration(avg_time_sec),
        })

    # Parse Country Distribution
    country_dist: List[Dict[str, Any]] = []
    total_geo_users = 0
    for r in res_countries.rows:
        c_name = r.dimension_values[0].value or "Unknown"
        c_code = r.dimension_values[1].value if len(r.dimension_values) > 1 else ""
        c_users = int(r.metric_values[0].value) if len(r.metric_values) > 0 else 0
        total_geo_users += c_users
        country_dist.append({
            "country": c_name,
            "code": c_code.upper() if c_code else "GL",
            "users": c_users,
        })

    for c in country_dist:
        pct = round((c["users"] / total_geo_users * 100), 1) if total_geo_users > 0 else 0.0
        c["percentage"] = pct

    # Parse Events
    events_list: List[Dict[str, Any]] = []
    for r in res_events.rows:
        ev_name = r.dimension_values[0].value or "unknown"
        ev_count = int(r.metric_values[0].value) if len(r.metric_values) > 0 else 0
        ev_key = int(r.metric_values[1].value) if len(r.metric_values) > 1 else 0
        events_list.append({
            "eventName": ev_name,
            "eventCount": ev_count,
            "keyEvents": ev_key,
        })

    return {
        "totalUsers": total_users,
        "activeUsers": active_users,
        "newUsers": new_users,
        "sessions": sessions,
        "pageviews": pageviews,
        "avgSessionDuration": format_duration(avg_session_duration_sec),
        "avgSessionDurationSeconds": avg_session_duration_sec,
        "bounceRate": bounce_rate,
        "trafficSources": traffic_sources,
        "deviceDistribution": device_dist,
        "topPages": top_pages,
        "countryDistribution": country_dist,
        "events": events_list,
    }


# ── Unified Analytics Report Generator ────────────────────────────────────────

def get_google_analytics_report_data(date_range: str = "30d", force_refresh: bool = False) -> Dict[str, Any]:
    """Retrieve combined realtime and historical telemetry from GA4 Data API.
    
    Adheres strictly to Zero Mock Policy: Never returns mock data on error.
    """
    client, prop_id = get_analytics_client()
    now_ts = time.time()
    cache_key = f"{prop_id}:{date_range.lower()}"

    # Check cache if not forcing refresh
    if not force_refresh and cache_key in _CACHE_STORE:
        cached_time, cached_data = _CACHE_STORE[cache_key]
        if now_ts - cached_time < REALTIME_CACHE_TTL_SECONDS:
            return cached_data

    try:
        realtime_data = fetch_realtime_metrics(client, prop_id)
        historical_data = fetch_historical_metrics(client, prop_id, date_range)

        # Build combined response
        response_payload = {
            "propertyId": prop_id,
            "status": "CONNECTED_ACTIVE",
            "isRealtimeActive": True,
            "dateRange": date_range,
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
            # Realtime snapshot
            "realtime": realtime_data,
            "realtimeActiveUsers": realtime_data["activeUsers"],
            # Historical KPIs & Breakdowns
            "totalUsers": historical_data["totalUsers"],
            "activeUsers": historical_data["activeUsers"],
            "newUsers": historical_data["newUsers"],
            "sessions": historical_data["sessions"],
            "pageviews": historical_data["pageviews"],
            "avgSessionDuration": historical_data["avgSessionDuration"],
            "avgSessionDurationSeconds": historical_data["avgSessionDurationSeconds"],
            "bounceRate": historical_data["bounceRate"],
            "trafficSources": historical_data["trafficSources"],
            "deviceDistribution": historical_data["deviceDistribution"],
            "topPages": historical_data["topPages"],
            "countryDistribution": historical_data["countryDistribution"],
            "events": historical_data["events"],
        }

        # Store in cache
        _CACHE_STORE[cache_key] = (now_ts, response_payload)
        return response_payload

    except (PermissionDenied, Unauthenticated) as exc:
        logger.error("GA4 API Authorization Error: %s", exc)
        raise ValueError(
            f"Google Analytics Authorization Error: The service account ({get_settings().GOOGLE_SERVICE_ACCOUNT_EMAIL}) "
            f"does not have permission to access GA4 property '{prop_id}'. Please grant 'Viewer' role to the service account "
            "in Google Analytics Admin -> Property Access Management."
        )
    except InvalidArgument as exc:
        logger.error("GA4 API Invalid Argument: %s", exc)
        raise ValueError(f"Google Analytics Data API Invalid Request: {exc.message}")
    except GoogleAPIError as exc:
        logger.error("Google Analytics Data API Error: %s", exc)
        raise ValueError(f"Google Analytics Data API request failed: {exc.message}")
    except Exception as exc:
        logger.error("Unexpected error fetching Google Analytics report: %s", exc)
        raise ValueError(f"Google Analytics integration error: {str(exc)}")
