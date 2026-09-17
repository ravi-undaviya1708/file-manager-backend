"""Email service for administrative notifications and transactional emails."""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import Optional

from app.config import get_settings
from app.models import User

logger = logging.getLogger(__name__)

PORTAL_URL = "https://file-manager.binaries.org.in/"


def build_registration_notification_email(
    user_name: str,
    user_email: str,
    registered_at_str: str,
    registration_method: str = "Email/Password",
) -> tuple[str, str]:
    """Build sanitized plain-text and HTML email bodies for new user registration alert.

    Guarantees no sensitive credentials, password hashes, or auth tokens are included.
    """
    safe_name = user_name.strip() if user_name else "Not provided"
    safe_email = user_email.strip()
    safe_time = registered_at_str
    safe_method = registration_method

    # Plain text fallback
    plain_text = f"""New user registered on GetFileNova.

Name: {safe_name}
Email: {safe_email}
Registration time: {safe_time}
Registration method: {safe_method}

GetFileNova:
{PORTAL_URL}
"""

    # Escaped values for safe HTML rendering
    escaped_name = html.escape(safe_name)
    escaped_email = html.escape(safe_email)
    escaped_time = html.escape(safe_time)
    escaped_method = html.escape(safe_method)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>New User Registration</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      background-color: #f4f6f9;
      margin: 0;
      padding: 24px;
      color: #1e293b;
    }}
    .container {{
      max-width: 580px;
      margin: 0 auto;
      background: #ffffff;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
      border: 1px solid #e2e8f0;
    }}
    .header {{
      background: linear-gradient(135deg, #2563eb, #1d4ed8);
      padding: 28px 32px;
      color: #ffffff;
    }}
    .header h1 {{
      margin: 0;
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }}
    .header p {{
      margin: 6px 0 0;
      font-size: 13px;
      opacity: 0.9;
    }}
    .content {{
      padding: 32px;
    }}
    .intro {{
      font-size: 15px;
      line-height: 1.5;
      margin-bottom: 24px;
      color: #334155;
    }}
    .details-table {{
      width: 100%;
      border-collapse: collapse;
      margin-bottom: 28px;
    }}
    .details-table td {{
      padding: 12px 16px;
      font-size: 14px;
      border-bottom: 1px solid #f1f5f9;
    }}
    .details-table td.label {{
      font-weight: 600;
      color: #64748b;
      width: 35%;
      background-color: #f8fafc;
    }}
    .details-table td.value {{
      color: #0f172a;
      font-weight: 500;
    }}
    .cta-container {{
      text-align: center;
      margin: 28px 0 16px;
    }}
    .cta-button {{
      display: inline-block;
      background-color: #2563eb;
      color: #ffffff !important;
      text-decoration: none;
      padding: 12px 28px;
      border-radius: 8px;
      font-weight: 600;
      font-size: 14px;
      box-shadow: 0 2px 6px rgba(37, 99, 235, 0.2);
    }}
    .footer {{
      padding: 20px 32px;
      background-color: #f8fafc;
      border-top: 1px solid #e2e8f0;
      text-align: center;
      font-size: 12px;
      color: #94a3b8;
    }}
    .footer a {{
      color: #64748b;
      text-decoration: none;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>GetFileNova Admin Notification</h1>
      <p>New User Account Created</p>
    </div>
    <div class="content">
      <p class="intro">A new user has successfully registered on <strong>GetFileNova</strong>.</p>
      
      <table class="details-table">
        <tr>
          <td class="label">Name</td>
          <td class="value">{escaped_name}</td>
        </tr>
        <tr>
          <td class="label">Email</td>
          <td class="value"><strong>{escaped_email}</strong></td>
        </tr>
        <tr>
          <td class="label">Registration Time</td>
          <td class="value">{escaped_time}</td>
        </tr>
        <tr>
          <td class="label">Registration Method</td>
          <td class="value">{escaped_method}</td>
        </tr>
      </table>

      <div class="cta-container">
        <a href="{PORTAL_URL}" class="cta-button" target="_blank" rel="noopener noreferrer">Open GetFileNova</a>
      </div>
    </div>
    <div class="footer">
      <p>This is an automated administrative notification sent to the account owner.</p>
      <p><a href="{PORTAL_URL}">{PORTAL_URL}</a></p>
    </div>
  </div>
</body>
</html>
"""

    return plain_text, html_content


def send_smtp_email(
    to_email: str,
    subject: str,
    plain_text: str,
    html_content: Optional[str] = None,
) -> bool:
    """Send an email using configured SMTP credentials with SSL/TLS auto-detection.

    Guarantees that errors are caught cleanly, secrets are never logged, and the caller
    is never blocked or crashed.
    """
    settings = get_settings()

    smtp_host = settings.SMTP_HOST
    smtp_port = int(settings.SMTP_PORT or 465)
    smtp_user = settings.effective_smtp_user
    smtp_password = settings.SMTP_PASSWORD
    from_email = settings.effective_from_email or smtp_user or "no-reply@getfilenova.com"
    from_name = settings.SMTP_FROM_NAME or "GetFileNova"

    if not smtp_host or not smtp_user or not smtp_password:
        logger.info(
            "SMTP configuration incomplete (host=%s, user=%s). Skipping email dispatch to %s.",
            bool(smtp_host),
            bool(smtp_user),
            to_email,
        )
        return False

    try:
        # Build MIME multipart message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((from_name, from_email))
        msg["To"] = to_email
        msg["Date"] = formatdate(localtime=False, usegmt=True)

        # Attach plain text and HTML alternatives
        msg.attach(MIMEText(plain_text, "plain", "utf-8"))
        if html_content:
            msg.attach(MIMEText(html_content, "html", "utf-8"))

        # Connect via SSL or TLS based on port
        ssl_context = ssl.create_default_context()

        if smtp_port == 465:
            # SMTPS (Implicit SSL)
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10, context=ssl_context) as server:
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
        else:
            # STARTTLS (Port 587 or standard SMTP)
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.ehlo()
                if server.has_extn("STARTTLS"):
                    server.starttls(context=ssl_context)
                    server.ehlo()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)

        logger.info("Successfully delivered admin notification email to %s via SMTP (%s:%d)", to_email, smtp_host, smtp_port)
        return True

    except Exception as exc:
        # Never log password or full connection strings with auth details
        logger.error(
            "Failed to deliver admin notification email to %s via SMTP (%s:%d): %s",
            to_email,
            smtp_host,
            smtp_port,
            exc,
            exc_info=False,
        )
        return False


def send_new_registration_notification(
    user: User,
    registration_method: str = "Email/Password",
) -> bool:
    """Send admin notification email for a newly registered user account.

    Safe and non-blocking: never raises exceptions to the caller.
    """
    settings = get_settings()

    if not getattr(settings, "ADMIN_NOTIFICATIONS_ENABLED", True):
        logger.debug("Admin notifications are disabled. Skipping registration notification.")
        return False

    recipient = getattr(settings, "ADMIN_NOTIFICATION_EMAIL", None)
    if not recipient:
        logger.info("ADMIN_NOTIFICATION_EMAIL is not set. Skipping registration notification for %s.", user.email)
        return False

    # Format timestamp in clean UTC format
    dt = user.created_at or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    formatted_time = dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    subject = "New GetFileNova User Registration"
    plain_text, html_body = build_registration_notification_email(
        user_name=user.name,
        user_email=user.email,
        registered_at_str=formatted_time,
        registration_method=registration_method,
    )
    try:
        return send_smtp_email(
            to_email=recipient,
            subject=subject,
            plain_text=plain_text,
            html_content=html_body,
        )
    except Exception as exc:
        logger.warning("Failed to send new user registration notification email to %s: %s", recipient, exc)
        return False
