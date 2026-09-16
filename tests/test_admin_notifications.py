"""Automated unit and integration tests for Admin Registration Email Notifications."""

from __future__ import annotations

import smtplib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException, status
from pydantic import ValidationError

from app.auth import verify_password
from app.auth_routes import register
from app.config import get_settings
from app.email_service import (
    PORTAL_URL,
    build_registration_notification_email,
    send_new_registration_notification,
    send_smtp_email,
)
from app.models import User
from app.schemas import UserRegisterRequest


class TestAdminRegistrationNotifications:
    """Test suite covering admin email notification behavior and security guarantees."""

    @pytest.mark.asyncio
    async def test_successful_registration_triggers_notification_task_once(self):
        """Verify that a successful registration enqueues the admin notification exactly once."""
        req = UserRegisterRequest(
            name="John Doe",
            email="john.doe@example.com",
            password="SecurePassword123!",
        )

        bg = BackgroundTasks()

        with patch("app.email_service.send_new_registration_notification") as mock_notify:
            res = await register(req, bg)

            # Registration response is valid
            assert res.token is not None
            assert res.user.email == "john.doe@example.com"
            assert res.user.name == "John Doe"

            # Execute background tasks manually to simulate background runner
            await bg()

            # Verify notification was called exactly once with the created user
            assert mock_notify.call_count == 1
            call_args = mock_notify.call_args[0]
            assert isinstance(call_args[0], User)
            assert call_args[0].email == "john.doe@example.com"
            assert call_args[0].name == "John Doe"
            assert call_args[1] == "Email/Password"

    @pytest.mark.asyncio
    async def test_duplicate_registration_does_not_trigger_notification(self):
        """Verify that duplicate registration attempts (409 Conflict) do NOT trigger any notification."""
        req = UserRegisterRequest(
            name="Existing User",
            email="existing@example.com",
            password="Password123!",
        )
        bg = BackgroundTasks()
        await register(req, bg)

        # Attempt duplicate registration with same email
        req_duplicate = UserRegisterRequest(
            name="Existing User 2",
            email="EXISTING@example.com",
            password="Password456!",
        )

        bg_dup = BackgroundTasks()
        with patch("app.email_service.send_new_registration_notification") as mock_notify:
            with pytest.raises(HTTPException) as exc_info:
                await register(req_duplicate, bg_dup)

            assert exc_info.value.status_code == status.HTTP_409_CONFLICT
            await bg_dup()
            mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_registration_validation_fails_without_notification(self):
        """Verify that schema validation failures prevent notification dispatch."""
        with patch("app.email_service.send_new_registration_notification") as mock_notify:
            # Short password failure
            with pytest.raises(ValidationError):
                UserRegisterRequest(
                    name="Invalid User",
                    email="valid@example.com",
                    password="123",  # min_length is 6
                )

            # Invalid email format failure
            with pytest.raises(ValidationError):
                UserRegisterRequest(
                    name="Invalid User",
                    email="not-an-email",
                    password="ValidPassword123!",
                )

            mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_email_failure_does_not_block_user_registration(self):
        """Verify that if SMTP or email service throws an error, registration still succeeds completely."""
        req = UserRegisterRequest(
            name="Resilient User",
            email="resilient.user@example.com",
            password="SecurePassword999!",
        )

        bg = BackgroundTasks()

        # Simulate SMTP error during notification
        with patch("app.email_service.send_smtp_email", side_effect=Exception("SMTP Connection Timeout")):
            res = await register(req, bg)

            # User registration must still succeed
            assert res.token is not None
            assert res.user.email == "resilient.user@example.com"

            # Execute background tasks (which includes the email call)
            # Should not raise an unhandled exception
            await bg()

            # Verify user exists in database
            db_user = await User.find_one({"email": "resilient.user@example.com"})
            assert db_user is not None
            assert verify_password("SecurePassword999!", db_user.hashed_password)

    def test_email_payload_security_and_content_isolation(self):
        """Verify that the generated email payload contains ONLY safe fields and NO secrets/tokens."""
        name = "Jane Smith"
        email = "jane.smith@example.com"
        reg_time = "2026-09-17 01:45:00 UTC"
        raw_password = "SuperSecretPassword123!"
        hashed_password = "$2b$12$e8YnZ7QzVjK1..."
        jwt_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0..."

        plain_text, html_content = build_registration_notification_email(
            user_name=name,
            user_email=email,
            registered_at_str=reg_time,
            registration_method="Email/Password",
        )

        # 1. Verify required information is present
        assert name in plain_text
        assert email in plain_text
        assert reg_time in plain_text
        assert "Email/Password" in plain_text
        assert PORTAL_URL in plain_text

        assert name in html_content
        assert email in html_content
        assert reg_time in html_content
        assert "Email/Password" in html_content
        assert PORTAL_URL in html_content

        # 2. Strict Security Check: Verify sensitive secrets are NOT present
        assert raw_password not in plain_text
        assert raw_password not in html_content
        assert hashed_password not in plain_text
        assert hashed_password not in html_content
        assert jwt_token not in plain_text
        assert jwt_token not in html_content
        assert "smtp" not in plain_text.lower()
        assert "password" not in plain_text.lower().replace("email/password", "")

    def test_send_smtp_email_ssl_port_465_dispatch(self):
        """Verify SMTP dispatch on port 465 uses SMTP_SSL with secure login."""
        settings = get_settings()
        settings.SMTP_HOST = "smtp.gmail.com"
        settings.SMTP_PORT = 465
        settings.SMTP_USERNAME = "sender@gmail.com"
        settings.SMTP_PASSWORD = "app-password-token"
        settings.SMTP_FROM_EMAIL = "sender@gmail.com"
        settings.SMTP_FROM_NAME = "GetFileNova"

        with patch("smtplib.SMTP_SSL") as mock_smtp_ssl:
            mock_server = MagicMock()
            mock_smtp_ssl.return_value.__enter__.return_value = mock_server

            success = send_smtp_email(
                to_email="admin@gmail.com",
                subject="Test Subject",
                plain_text="Plain Text Content",
                html_content="<p>HTML Content</p>",
            )

            assert success is True
            mock_smtp_ssl.assert_called_once()
            mock_server.login.assert_called_once_with("sender@gmail.com", "app-password-token")
            mock_server.send_message.assert_called_once()

    def test_send_smtp_email_tls_port_587_dispatch(self):
        """Verify SMTP dispatch on port 587 uses standard SMTP with STARTTLS."""
        settings = get_settings()
        settings.SMTP_HOST = "smtp.gmail.com"
        settings.SMTP_PORT = 587
        settings.SMTP_USERNAME = "sender@gmail.com"
        settings.SMTP_PASSWORD = "app-password-token"
        settings.SMTP_FROM_EMAIL = "sender@gmail.com"

        with patch("smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_server.has_extn.return_value = True
            mock_smtp.return_value.__enter__.return_value = mock_server

            success = send_smtp_email(
                to_email="admin@gmail.com",
                subject="Test Subject",
                plain_text="Plain Text Content",
            )

            assert success is True
            mock_smtp.assert_called_once()
            mock_server.starttls.assert_called_once()
            mock_server.login.assert_called_once_with("sender@gmail.com", "app-password-token")
            mock_server.send_message.assert_called_once()

    def test_send_smtp_email_handles_exceptions_gracefully(self):
        """Verify that SMTP network/auth errors are cleanly caught and return False."""
        settings = get_settings()
        settings.SMTP_HOST = "smtp.gmail.com"
        settings.SMTP_PORT = 465
        settings.SMTP_USERNAME = "sender@gmail.com"
        settings.SMTP_PASSWORD = "wrong-password"

        with patch("smtplib.SMTP_SSL", side_effect=smtplib.SMTPAuthenticationError(535, b"Authentication failed")):
            success = send_smtp_email(
                to_email="admin@gmail.com",
                subject="Test",
                plain_text="Hello",
            )
            assert success is False

    def test_send_new_registration_notification_disabled_or_missing_config(self):
        """Verify notification skips cleanly when email or feature flag is disabled."""
        settings = get_settings()
        user = User(
            name="Test User",
            email="test@example.com",
            created_at=datetime.now(timezone.utc),
        )

        # 1. Feature disabled
        settings.ADMIN_NOTIFICATIONS_ENABLED = False
        settings.ADMIN_NOTIFICATION_EMAIL = "admin@gmail.com"
        assert send_new_registration_notification(user) is False

        # 2. Missing admin email
        settings.ADMIN_NOTIFICATIONS_ENABLED = True
        settings.ADMIN_NOTIFICATION_EMAIL = None
        assert send_new_registration_notification(user) is False
