"""Cashfree Subscriptions and Payment Gateway API client.

Handles recurring subscription creation, one-time PG orders, cancellation, and webhook signature verification.
"""

from __future__ import annotations

import hmac
import hashlib
import base64
import logging
import random
import httpx
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any

from app.config import get_settings

logger = logging.getLogger("cashfree_client")


def get_cashfree_base_url(env: str, service: Optional[str] = None) -> str:
    """Return appropriate Cashfree API base URL based on environment and service."""
    settings = get_settings()
    is_prod = env.strip().lower() == "production"
    effective_service = (service or settings.CASHFREE_SUBSCRIPTION_SERVICE or "sub").strip().lower()
    if effective_service == "sub":
        return "https://api.cashfree.com/sub/v1" if is_prod else "https://sandbox.cashfree.com/sub/v1"
    return "https://api.cashfree.com/pg" if is_prod else "https://sandbox.cashfree.com/pg"


def verify_cashfree_webhook_signature(
    raw_body: bytes,
    signature: str,
    timestamp: Optional[str] = None,
) -> bool:
    """Verify Cashfree HMAC-SHA256 signature using CASHFREE_WEBHOOK_SECRET or CASHFREE_SECRET_KEY.

    Cashfree provides HMAC-SHA256 of either (timestamp + raw_body) or raw_body.
    Supports both hex and base64 formats with timestamp replay window protection.
    Fails closed if secret or signature is missing.
    """
    settings = get_settings()
    secret = settings.CASHFREE_WEBHOOK_SECRET or settings.CASHFREE_SECRET_KEY

    # Fail closed if secret or signature is empty / unconfigured
    if not secret or not signature:
        logger.warning("Webhook signature verification failed: Missing webhook secret or signature header.")
        return False

    # Timestamp replay window validation
    if timestamp:
        try:
            ts = float(timestamp.strip())
            # Convert milliseconds to seconds if timestamp is in ms (13 digits)
            if ts > 1e11:
                ts = ts / 1000.0
            now_ts = datetime.now(timezone.utc).timestamp()
            max_skew = float(settings.CASHFREE_WEBHOOK_MAX_SKEW_SECONDS or 300)
            if abs(now_ts - ts) > max_skew:
                logger.warning(
                    f"Webhook timestamp replay validation failed: timestamp {timestamp} "
                    f"differs from server time by {abs(now_ts - ts):.1f}s (max allowed: {max_skew}s)."
                )
                return False
        except (ValueError, TypeError) as exc:
            logger.warning(f"Webhook timestamp parsing failed for value '{timestamp}': {exc}")
            return False

    secret_bytes = secret.encode("utf-8")

    # Candidate messages to hash: with timestamp prefix and without
    candidates = []
    if timestamp:
        candidates.append(timestamp.encode("utf-8") + raw_body)
    candidates.append(raw_body)

    for msg in candidates:
        # Hex comparison
        computed_hex = hmac.new(secret_bytes, msg, hashlib.sha256).hexdigest()
        if hmac.compare_digest(computed_hex.lower(), signature.lower()):
            return True

        # Base64 comparison
        computed_b64 = base64.b64encode(hmac.new(secret_bytes, msg, hashlib.sha256).digest()).decode("utf-8")
        if hmac.compare_digest(computed_b64, signature):
            return True

    return False


async def create_cashfree_recurring_subscription(
    user_id: str,
    customer_id: str,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
    plan_code: str,
    plan_name: str,
    billing_interval: str,
    amount_inr: float,
    return_url: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Initiate a recurring subscription mandate with Cashfree.

    Returns:
      Tuple of (subscription_response_dict, error_message).
    """
    settings = get_settings()
    app_id = settings.CASHFREE_APP_ID
    secret_key = settings.CASHFREE_SECRET_KEY
    is_prod = settings.CASHFREE_ENV.strip().lower() == "production"

    clean_phone = "".join(filter(str.isdigit, customer_phone))
    if len(clean_phone) < 10:
        clean_phone = "9999999999"
    elif len(clean_phone) > 10:
        clean_phone = clean_phone[-10:]

    timestamp = int(datetime.now(timezone.utc).timestamp())
    rand_id = random.randint(1000, 9999)
    sub_ref_id = f"cf_sub_{user_id[-6:]}_{timestamp}_{rand_id}"

    # In Production, never bypass or simulate gateway calls
    if is_prod:
        if not app_id or not secret_key:
            return None, "Production Cashfree credentials (CASHFREE_APP_ID / CASHFREE_SECRET_KEY) are missing or invalid."
    else:
        # Non-production: allow test mode simulation only when explicitly configured or credentials are mock
        if (
            not app_id
            or not secret_key
            or settings.CASHFREE_MODE == "test"
            or app_id.startswith("mock_")
            or app_id.startswith("test_")
            or app_id == "cf_sandbox_app_id"
        ):
            simulated_data = {
                "subscription_id": sub_ref_id,
                "cf_subscription_id": f"cf_mandate_{sub_ref_id}",
                "subscription_session_id": f"sub_sess_{sub_ref_id}",
                "payment_session_id": f"sub_sess_{sub_ref_id}",
                "auth_link": f"https://sandbox.cashfree.com/pg/view/sub/{sub_ref_id}",
                "sub_link": f"https://sandbox.cashfree.com/pg/view/sub/{sub_ref_id}",
                "subscription_status": "INITIALIZED",
                "status": "INITIALIZED",
                "customer_id": customer_id,
                "plan_code": plan_code,
                "billing_interval": billing_interval,
                "amount": amount_inr,
            }
            return simulated_data, None

    base_url = get_cashfree_base_url(settings.CASHFREE_ENV, service=settings.CASHFREE_SUBSCRIPTION_SERVICE)
    headers = {
        "x-client-id": app_id,
        "x-client-secret": secret_key,
        "x-api-version": settings.CASHFREE_API_VERSION or "2025-01-01",
        "Content-Type": "application/json",
    }

    interval_type = "MONTH" if billing_interval.lower() == "monthly" else "YEAR"
    payload = {
        "subscription_id": sub_ref_id,
        "plan_details": {
            "plan_name": f"GetFileNova-{plan_name}-{billing_interval.lower()}",
            "plan_type": "PERIODIC",
            "plan_amount": amount_inr,
            "plan_max_amount": amount_inr,
            "plan_currency": "INR",
            "plan_interval_type": interval_type,
            "plan_intervals": 1,
            "plan_max_cycles": 120,
        },
        "customer_details": {
            "customer_id": customer_id,
            "customer_name": customer_name or "GetFileNova Customer",
            "customer_email": customer_email,
            "customer_phone": clean_phone,
        },
        "subscription_meta": {
            "return_url": return_url,
        },
        "subscription_tags": {
            "user_id": user_id,
            "plan_code": plan_code,
            "billing_interval": billing_interval,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.post(f"{base_url}/subscriptions", json=payload, headers=headers)
            if response.status_code in [200, 201]:
                data = response.json()
                sub_session = (
                    data.get("subscription_session_id")
                    or data.get("payment_session_id")
                    or data.get("paymentSessionId")
                    or f"sub_sess_{sub_ref_id}"
                )
                auth_link = data.get("auth_link") or data.get("sub_link")
                data["subscription_id"] = data.get("subscription_id") or sub_ref_id
                data["cf_subscription_id"] = data.get("cf_subscription_id") or data["subscription_id"]
                data["subscription_session_id"] = sub_session
                data["payment_session_id"] = sub_session
                data["auth_link"] = auth_link
                data["sub_link"] = auth_link
                data["status"] = data.get("subscription_status") or data.get("status") or "INITIALIZED"
                return data, None
            else:
                err_text = response.text
                logger.error(
                    f"Cashfree Subscriptions API Error "
                    f"[{response.status_code}]: {err_text}"
                )
                return None, f"[{response.status_code}] {err_text}"
    except Exception as exc:
        logger.error(f"Cashfree Subscriptions HTTP Exception: {exc}")
        return None, str(exc)


async def cancel_cashfree_subscription(
    cf_subscription_id: str,
) -> Tuple[bool, Optional[str]]:
    """Cancel a recurring subscription mandate on Cashfree."""
    settings = get_settings()
    app_id = settings.CASHFREE_APP_ID
    secret_key = settings.CASHFREE_SECRET_KEY
    is_prod = settings.CASHFREE_ENV.strip().lower() == "production"

    if not is_prod:
        if not app_id or not secret_key or app_id.startswith("mock_") or app_id.startswith("test_") or settings.CASHFREE_MODE == "test":
            return True, None

    if not app_id or not secret_key:
        return False, "Missing Cashfree credentials"

    base_url = get_cashfree_base_url(settings.CASHFREE_ENV, service=settings.CASHFREE_SUBSCRIPTION_SERVICE)
    headers = {
        "x-client-id": app_id,
        "x-client-secret": secret_key,
        "x-api-version": settings.CASHFREE_API_VERSION or "2025-01-01",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{base_url}/subscriptions/{cf_subscription_id}/cancel",
                headers=headers,
            )
            if response.status_code in [200, 201, 204]:
                return True, None
            else:
                return False, f"[{response.status_code}] {response.text}"
    except Exception as exc:
        return False, str(exc)


async def get_cashfree_subscription(
    cf_subscription_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fetch subscription details from Cashfree PG Subscriptions API."""
    settings = get_settings()
    app_id = settings.CASHFREE_APP_ID
    secret_key = settings.CASHFREE_SECRET_KEY
    is_prod = settings.CASHFREE_ENV.strip().lower() == "production"

    if is_prod and (not app_id or not secret_key):
        return None, "Production Cashfree credentials are missing or invalid."

    if not is_prod:
        if not app_id or not secret_key or settings.CASHFREE_MODE == "test" or app_id.startswith("mock_"):
            return {
                "subscription_id": cf_subscription_id,
                "cf_subscription_id": cf_subscription_id,
                "subscription_session_id": f"sub_sess_{cf_subscription_id}",
                "payment_session_id": f"sub_sess_{cf_subscription_id}",
                "subscription_status": "INITIALIZED",
                "status": "INITIALIZED",
                "auth_link": f"https://sandbox.cashfree.com/pg/view/sub/{cf_subscription_id}",
                "sub_link": f"https://sandbox.cashfree.com/pg/view/sub/{cf_subscription_id}",
            }, None

    base_url = get_cashfree_base_url(settings.CASHFREE_ENV, service=settings.CASHFREE_SUBSCRIPTION_SERVICE)
    headers = {
        "x-client-id": app_id,
        "x-client-secret": secret_key,
        "x-api-version": settings.CASHFREE_API_VERSION or "2025-01-01",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{base_url}/subscriptions/{cf_subscription_id}",
                headers=headers,
            )
            if response.status_code == 200:
                data = response.json()
                sub_session = (
                    data.get("subscription_session_id")
                    or data.get("payment_session_id")
                    or data.get("paymentSessionId")
                    or f"sub_sess_{cf_subscription_id}"
                )
                auth_link = data.get("auth_link") or data.get("sub_link")
                data["subscription_id"] = data.get("subscription_id") or cf_subscription_id
                data["cf_subscription_id"] = data.get("cf_subscription_id") or cf_subscription_id
                data["subscription_session_id"] = sub_session
                data["payment_session_id"] = sub_session
                data["auth_link"] = auth_link
                data["sub_link"] = auth_link
                data["status"] = data.get("subscription_status") or data.get("status") or "INITIALIZED"
                return data, None
            else:
                err_text = response.text
                logger.error(f"Cashfree GET Subscription Error [{response.status_code}]: {err_text}")
                return None, f"[{response.status_code}] {err_text}"
    except Exception as exc:
        logger.error(f"Cashfree GET Subscription HTTP Exception: {exc}")
        return None, str(exc)

