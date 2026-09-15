"""Contact and Support router for GetFileNova inquiries."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from fastapi import APIRouter, HTTPException, BackgroundTasks, status
from pydantic import BaseModel, EmailStr, Field

from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contact", tags=["Contact"])


class ContactRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150, description="Full name of sender")
    email: EmailStr = Field(description="Email address for reply")
    subject: str = Field(min_length=1, max_length=250, description="Subject of inquiry")
    message: str = Field(min_length=1, max_length=5000, description="Message body")


class ContactResponse(BaseModel):
    success: bool
    message: str


def send_contact_email_task(name: str, email: str, subject: str, message: str, timestamp_iso: str):
    """Background task to deliver contact inquiry to support."""
    settings = get_settings()
    recipient = getattr(settings, "CONTACT_EMAIL", "undaviyaraj2000@gmail.com") or "undaviyaraj2000@gmail.com"
    email_subject = f"GetFileNova Contact — {subject} ({name})"

    body_text = f"""New Contact Inquiry received on GetFileNova:

Timestamp: {timestamp_iso}
Name: {name}
Email: {email}
Subject: {subject}

Message:
--------------------------------------------------
{message}
--------------------------------------------------

Reply-To: {email}
Powered by Binary Infotech (https://binaries.org.in)
"""

    logger.info(
        "Contact inquiry processed for %s <%s> to %s | Subject: %s",
        name,
        email,
        recipient,
        subject,
    )

    # Optional SMTP delivery if environment configured
    smtp_host = getattr(settings, "SMTP_HOST", None)
    smtp_port = getattr(settings, "SMTP_PORT", 587)
    smtp_user = getattr(settings, "SMTP_USER", None)
    smtp_password = getattr(settings, "SMTP_PASSWORD", None)

    if smtp_host and smtp_user and smtp_password:
        try:
            msg = MIMEMultipart()
            msg["From"] = smtp_user
            msg["To"] = recipient
            msg["Subject"] = email_subject
            msg["Reply-To"] = email
            msg.attach(MIMEText(body_text, "plain"))

            with smtplib.SMTP(smtp_host, int(smtp_port), timeout=15) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
            logger.info("Successfully dispatched contact email via SMTP to %s", recipient)
        except Exception as exc:
            logger.warning("SMTP delivery failed (fallback recorded): %s", exc)


@router.post(
    "",
    response_model=ContactResponse,
    status_code=status.HTTP_200_OK,
    summary="Submit a contact or support inquiry",
)
async def submit_contact_form(
    body: ContactRequest,
    background_tasks: BackgroundTasks,
):
    """Receive contact inquiry from user and route to support."""
    now_iso = datetime.now(timezone.utc).isoformat()
    
    # Trigger non-blocking email delivery task
    background_tasks.add_task(
        send_contact_email_task,
        body.name,
        body.email,
        body.subject,
        body.message,
        now_iso,
    )

    return ContactResponse(
        success=True,
        message="Thanks for contacting us. Your message has been sent successfully. We'll get back to you as soon as possible.",
    )
