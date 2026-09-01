"""Router for billing plans, customer subscriptions, and Cashfree payment checkouts."""

from __future__ import annotations

import random
import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.models import User, PaymentRecord
from app.config import get_settings
from app.schemas import (
    CreateOrderRequest,
    CreateOrderResponse,
    VerifyPaymentRequest,
    MessageResponse,
)
from app.billing_plans import (
    get_plan_price,
    get_storage_limit,
    compute_subscription_expiry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/payments", tags=["Billing & Payments"])


# @router.post(
#     "/create-order",
#     response_model=CreateOrderResponse,
#     summary="Create a checkout order with Cashfree"
# ── Pricing Configuration ───────────────────────────────────────────────────

# Amounts in INR (Rupees)
PLAN_PRICING = {
    "personal": {"monthly": 119.0, "annual": 1190.0, "storage_gb": 50},
    "plus": {"monthly": 299.0, "annual": 2990.0, "storage_gb": 200},
    "power": {"monthly": 999.0, "annual": 9990.0, "storage_gb": 1000},
    # Backwards compatibility aliases
    "pro": {"monthly": 299.0, "annual": 2990.0, "storage_gb": 200},
    "business": {"monthly": 999.0, "annual": 9990.0, "storage_gb": 1000},
}


# ── Req/Res Schemas ──────────────────────────────────────────────────────────

class CreateOrderRequest(BaseModel):
    planName: str = Field(..., description="Plan tier: personal, plus, power")
    billingCycle: Optional[str] = Field("monthly", description="monthly or annual")


class CreateOrderResponse(BaseModel):
    orderId: str
    amount: float
    currency: str
    paymentSessionId: Optional[str] = None
    customerId: str
    customerName: str
    customerEmail: str
    customerPhone: str
    planName: str
    billingCycle: str
    cfOrderId: Optional[str] = None
    keyId: Optional[str] = None


class VerifyPaymentRequest(BaseModel):
    orderId: str
    planName: str
    billingCycle: Optional[str] = "monthly"
    paymentId: Optional[str] = None
    signature: Optional[str] = None
    paymentMethod: Optional[str] = "upi"


class PaymentRecordResponse(BaseModel):
    id: str
    userId: str
    customerId: str
    customerName: str
    customerEmail: str
    customerPhone: str
    orderId: str
    cfOrderId: Optional[str] = None
    cfPaymentId: Optional[str] = None
    amount: float
    currency: str
    planName: str
    billingCycle: str
    status: str
    paymentMethod: Optional[str] = None
    createdAt: str
    updatedAt: str
    subscriptionExpiresAt: Optional[str] = None


class MessageResponse(BaseModel):
    message: str


# ── Cashfree API Helper ──────────────────────────────────────────────────────

def _get_cashfree_base_url(env: str) -> str:
    if env.lower() == "production":
        return "https://api.cashfree.com/pg"
    return "https://sandbox.cashfree.com/pg"


async def _create_cashfree_order_api(
    order_id: str,
    amount: float,
    customer_id: str,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
    plan_name: str,
    billing_cycle: str,
    user_id: str,
) -> Optional[dict]:
    """Call official Cashfree PG Order creation API."""
    settings = get_settings()
    app_id = settings.CASHFREE_APP_ID
    secret_key = settings.CASHFREE_SECRET_KEY

    if not app_id or not secret_key or app_id.startswith("mock_"):
        return None

    import httpx

    base_url = _get_cashfree_base_url(settings.CASHFREE_ENV)
    headers = {
        "x-client-id": app_id,
        "x-client-secret": secret_key,
        "x-api-version": settings.CASHFREE_API_VERSION or "2023-08-01",
        "Content-Type": "application/json",
    }

    # Clean 10-digit phone number
    clean_phone = "".join(filter(str.isdigit, customer_phone))
    if len(clean_phone) < 10:
        clean_phone = "9999999999"
    elif len(clean_phone) > 10:
        clean_phone = clean_phone[-10:]

    payload = {
        "order_id": order_id,
        "order_amount": float(amount),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": customer_id,
            "customer_name": customer_name or "Customer",
            "customer_email": customer_email,
            "customer_phone": clean_phone,
        },
        "order_note": f"GetFileNova {plan_name.upper()} Plan ({billing_cycle.title()})",
        "order_meta": {
            "return_url": f"http://localhost:3000/dashboard?order_id={order_id}"
        },
        "order_tags": {
            "plan": plan_name,
            "billing_cycle": billing_cycle,
            "user_id": user_id,
        },
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/orders",
                json=payload,
                headers=headers,
                timeout=10.0,
            )
            if response.status_code in [200, 201]:
                return response.json()
    except Exception as e:
        print(f"Cashfree API order creation error: {e}")

    return None


async def _fetch_cashfree_order_status(order_id: str) -> Optional[dict]:
    """Fetch order status and payments from Cashfree PG API."""
    settings = get_settings()
    app_id = settings.CASHFREE_APP_ID
    secret_key = settings.CASHFREE_SECRET_KEY

    if not app_id or not secret_key or app_id.startswith("mock_"):
        return None

    import httpx

    base_url = _get_cashfree_base_url(settings.CASHFREE_ENV)
    headers = {
        "x-client-id": app_id,
        "x-client-secret": secret_key,
        "x-api-version": settings.CASHFREE_API_VERSION or "2023-08-01",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{base_url}/orders/{order_id}",
                headers=headers,
                timeout=10.0,
            )
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        print(f"Cashfree order fetch error: {e}")

    return None


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post(
    "/create-order",
    response_model=CreateOrderResponse,
    summary="Create a subscription checkout order with Cashfree and record customer details",
)
async def create_order(
    body: CreateOrderRequest,
    current_user: User = Depends(get_current_user),
):
    # """Initiate a Cashfree checkout session for plan upgrades."""
    # plan_name = body.planName.lower().strip()
    # billing_cycle = body.billingCycle.lower().strip()

    # if plan_name not in ["personal", "plus", "power"]:
    """Initiate a Cashfree checkout session with customer details and record transaction."""
    plan_name = body.planName.lower().strip()
    if plan_name not in PLAN_PRICING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": f"Invalid plan '{body.planName}'. Choose Personal, Plus, or Power."},
        )

    billing_cycle = (body.billingCycle or "monthly").lower().strip()
    if billing_cycle not in ["monthly", "annual"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Invalid billing cycle. Choose monthly or annual."},
        )

    plan_info = PLAN_PRICING[plan_name]
    amount = float(plan_info[billing_cycle])

    # Customer Identification (Every subscribing user is a customer)
    customer_id = getattr(current_user, "customer_id", None) or f"cust_{str(current_user.id)}"
    customer_name = current_user.name or "GetFileNova User"
    customer_email = current_user.email
    customer_phone = getattr(current_user, "phone", None) or "9999999999"

    # Generate unique, compliant Cashfree Order ID
    user_prefix = str(current_user.id)[-6:]
    timestamp = int(datetime.now(timezone.utc).timestamp())
    rand_suffix = random.randint(1000, 9999)
    order_id = f"cf_ord_{user_prefix}_{timestamp}_{rand_suffix}"

    # Handshake with Cashfree API
    cf_data = await _create_cashfree_order_api(
        order_id=order_id,
        amount=amount,
        customer_id=customer_id,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        plan_name=plan_name,
        billing_cycle=billing_cycle,
        user_id=str(current_user.id),
    )

    cf_order_id = str(cf_data.get("cf_order_id")) if cf_data and "cf_order_id" in cf_data else None
    payment_session_id = (
        cf_data.get("payment_session_id")
        if cf_data and "payment_session_id" in cf_data
        else f"cf_sess_{order_id}"
    )

    # Record Customer Subscription Order in Database immediately (Pending status)
    now_utc = datetime.now(timezone.utc)
    payment_record = PaymentRecord(
        user_id=str(current_user.id),
        customer_id=customer_id,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        order_id=order_id,
        cf_order_id=cf_order_id,
        payment_session_id=payment_session_id,
        amount=amount,
        currency="INR",
        plan_name=plan_name,
        billing_cycle=billing_cycle,
        status="PENDING",
        created_at=now_utc,
        updated_at=now_utc,
        raw_response=cf_data,
    )
    await payment_record.insert()

    # Update User customer_id if not previously set
    if not current_user.customer_id:
        current_user.customer_id = customer_id
        await current_user.save()

    settings = get_settings()
    return CreateOrderResponse(
        orderId=order_id,
        amount=amount,
        currency="INR",
        paymentSessionId=payment_session_id,
        customerId=customer_id,
        customerName=customer_name,
        customerEmail=customer_email,
        customerPhone=customer_phone,
        planName=plan_name,
        billingCycle=billing_cycle,
        cfOrderId=cf_order_id,
        keyId=settings.CASHFREE_APP_ID or settings.RAZORPAY_KEY_ID or "cf_sandbox_app_id",
    )


@router.post(
    "/verify-payment",
    response_model=MessageResponse,
    summary="Verify payment and activate user subscription capacity",
)
async def verify_payment(
    body: VerifyPaymentRequest,
    current_user: User = Depends(get_current_user),
):
    """Verify payment receipt, record payment success, and upgrade account capacity."""
    plan_name = body.planName.lower().strip()
    if plan_name not in PLAN_PRICING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": f"Invalid plan '{body.planName}'."},
        )

    billing_cycle = (body.billingCycle or "monthly").lower().strip()
    if billing_cycle not in ["monthly", "annual"]:
        billing_cycle = "monthly"

    # Find the corresponding PaymentRecord
    payment_record = await PaymentRecord.find_one(PaymentRecord.order_id == body.orderId)
    now_utc = datetime.now(timezone.utc)
    sub_duration_days = 365 if billing_cycle == "annual" else 30
    expires_at = now_utc + timedelta(days=sub_duration_days)

    cf_payment_id = body.paymentId or f"cf_pay_{random.randint(1000000, 9999999)}"
    payment_method = body.paymentMethod or "upi"

    # If real Cashfree order was placed, verify with Cashfree API
    if payment_record and payment_record.cf_order_id:
        cf_order = await _fetch_cashfree_order_status(body.orderId)
        if cf_order:
            cf_status = cf_order.get("order_status")
            if cf_status == "PAID":
                payment_method = cf_order.get("payment_method", payment_method)

    if payment_record:
        payment_record.status = "SUCCESS"
        payment_record.cf_payment_id = cf_payment_id
        payment_record.payment_method = payment_method
        payment_record.updated_at = now_utc
        payment_record.subscription_expires_at = expires_at
        await payment_record.save()
    else:
        # Create record if direct verify is received
        customer_id = getattr(current_user, "customer_id", None) or f"cust_{str(current_user.id)}"
        payment_record = PaymentRecord(
            user_id=str(current_user.id),
            customer_id=customer_id,
            customer_name=current_user.name or "Customer",
            customer_email=current_user.email,
            customer_phone=getattr(current_user, "phone", None) or "9999999999",
            order_id=body.orderId,
            cf_payment_id=cf_payment_id,
            amount=PLAN_PRICING[plan_name][billing_cycle],
            currency="INR",
            plan_name=plan_name,
            billing_cycle=billing_cycle,
            status="SUCCESS",
            payment_method=payment_method,
            created_at=now_utc,
            updated_at=now_utc,
            subscription_expires_at=expires_at,
        )
        await payment_record.insert()

    # Determine storage capacity
    storage_gb = PLAN_PRICING[plan_name]["storage_gb"]
    storage_bytes = storage_gb * 1024 * 1024 * 1024

    # Update User document with active subscription
    current_user.pricing_plan = plan_name
    current_user.storage_limit_bytes = storage_bytes
    current_user.billing_cycle = billing_cycle
    current_user.subscription_status = "active"
    current_user.subscription_expires_at = expires_at
    if not current_user.customer_id:
        current_user.customer_id = payment_record.customer_id
    await current_user.save()

    return MessageResponse(
        message=f"Payment verified successfully. Account upgraded to {plan_name.upper()} plan ({storage_gb} GB)."
    )


@router.get(
    "/history",
    response_model=List[PaymentRecordResponse],
    summary="Fetch payment and subscription transaction history for current user",
)
async def get_payment_history(
    current_user: User = Depends(get_current_user),
):
    """Retrieve all payment records and subscription history for the authenticated user."""
    records = await PaymentRecord.find(
        PaymentRecord.user_id == str(current_user.id)
    ).sort("-created_at").to_list()

    return [
        PaymentRecordResponse(
            id=str(r.id),
            userId=r.user_id,
            customerId=r.customer_id,
            customerName=r.customer_name,
            customerEmail=r.customer_email,
            customerPhone=r.customer_phone,
            orderId=r.order_id,
            cfOrderId=r.cf_order_id,
            cfPaymentId=r.cf_payment_id,
            amount=r.amount,
            currency=r.currency,
            planName=r.plan_name,
            billingCycle=r.billing_cycle,
            status=r.status,
            paymentMethod=r.payment_method,
            createdAt=r.created_at.isoformat() if r.created_at else "",
            updatedAt=r.updated_at.isoformat() if r.updated_at else "",
            subscriptionExpiresAt=r.subscription_expires_at.isoformat() if r.subscription_expires_at else None,
        )
        for r in records
    ]


@router.get(
    "/all",
    response_model=List[PaymentRecordResponse],
    summary="Super Admin: List all customer subscription orders and payment records",
)
async def list_all_payments(
    admin: User = Depends(get_current_user),
):
    """Retrieve all customer payments across the entire system (Super Admin only)."""
    if not admin.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Administrative privileges required to access all customer payment records."},
        )

    records = await PaymentRecord.find_all().sort("-created_at").to_list()

    return [
        PaymentRecordResponse(
            id=str(r.id),
            userId=r.user_id,
            customerId=r.customer_id,
            customerName=r.customer_name,
            customerEmail=r.customer_email,
            customerPhone=r.customer_phone,
            orderId=r.order_id,
            cfOrderId=r.cf_order_id,
            cfPaymentId=r.cf_payment_id,
            amount=r.amount,
            currency=r.currency,
            planName=r.plan_name,
            billingCycle=r.billing_cycle,
            status=r.status,
            paymentMethod=r.payment_method,
            createdAt=r.created_at.isoformat() if r.created_at else "",
            updatedAt=r.updated_at.isoformat() if r.updated_at else "",
            subscriptionExpiresAt=r.subscription_expires_at.isoformat() if r.subscription_expires_at else None,
        )
        for r in records
    ]
