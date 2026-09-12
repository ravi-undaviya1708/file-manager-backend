"""Automated Integration & Unit Tests for Authentication Lifecycle, Security, and Edge Cases."""

import pytest
from datetime import datetime, timedelta, timezone
from fastapi import status, HTTPException, BackgroundTasks

from app.models import User, FileSystemItem
from app.auth import (
    hash_password,
    verify_password,
    create_access_token,
    decode_access_token,
)
from app.auth_routes import register, login
from app.schemas import UserRegisterRequest, UserLoginRequest


class TestAuthLifecycle:
    """Tests covering Phase 2: Complete Authentication Lifecycle."""

    async def test_valid_registration_and_password_hashing(self):
        """Verify valid user registration stores bcrypt hash and generates JWT."""
        req = UserRegisterRequest(
            name="Alice Wonder",
            email="Alice.Wonder@Example.COM",
            password="SecurePassword123!",
        )
        bg = BackgroundTasks()
        res = await register(req, bg)

        assert res.token is not None
        assert len(res.token) > 20
        assert res.user.email == "alice.wonder@example.com"
        assert res.user.name == "Alice Wonder"
        assert res.user.pricingPlan == "free"
        assert res.user.storageLimitBytes == 16106127360

        # Verify DB document
        db_user = await User.find_one({"email": "alice.wonder@example.com"})
        assert db_user is not None
        assert db_user.hashed_password != "SecurePassword123!"
        assert verify_password("SecurePassword123!", db_user.hashed_password)

    async def test_duplicate_email_registration_rejected(self):
        """Verify registering with existing email returns 409 Conflict."""
        req = UserRegisterRequest(
            name="Alice 1",
            email="duplicate@example.com",
            password="Password123!",
        )
        await register(req, BackgroundTasks())

        # Second registration with same email (different case)
        req_dup = UserRegisterRequest(
            name="Alice 2",
            email="DUPLICATE@example.com",
            password="DifferentPassword456!",
        )
        with pytest.raises(HTTPException) as exc_info:
            await register(req_dup, BackgroundTasks())

        assert exc_info.value.status_code == status.HTTP_409_CONFLICT

    async def test_login_success_and_casing_normalization(self):
        """Verify login handles case normalization and whitespace."""
        await register(
            UserRegisterRequest(
                name="Bob Builder",
                email="bob.builder@company.com",
                password="MyStrongPassword99!",
            ),
            BackgroundTasks(),
        )

        login_req = UserLoginRequest(
            email="BOB.BUILDER@COMPANY.COM",
            password="MyStrongPassword99!",
        )
        login_res = await login(login_req, BackgroundTasks())

        assert login_res.token is not None
        assert login_res.user.email == "bob.builder@company.com"

    async def test_login_invalid_password_rejected(self):
        """Verify login with wrong password returns 401 Unauthorized."""
        await register(
            UserRegisterRequest(
                name="Charlie Brown",
                email="charlie@peanuts.com",
                password="CorrectPassword123!",
            ),
            BackgroundTasks(),
        )

        login_req = UserLoginRequest(
            email="charlie@peanuts.com",
            password="WrongPassword999!",
        )
        with pytest.raises(HTTPException) as exc_info:
            await login(login_req, BackgroundTasks())

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_login_unknown_email_rejected(self):
        """Verify login with non-existent user returns 401 Unauthorized."""
        login_req = UserLoginRequest(
            email="nonexistent.user@example.com",
            password="AnyPassword123!",
        )
        with pytest.raises(HTTPException) as exc_info:
            await login(login_req, BackgroundTasks())

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_jwt_tampering_and_expiration(self):
        """Verify JWT decoding enforces signature validity, sub claim, and expiry."""
        user_id = "65f1a2b3c4d5e6f7a8b9c0d1"

        # 1. Valid token
        token = create_access_token(data={"sub": user_id})
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["sub"] == user_id

        # 2. Expired token
        expired_token = create_access_token(
            data={"sub": user_id},
            expires_delta=timedelta(seconds=-60),
        )
        assert decode_access_token(expired_token) is None

        # 3. Tampered signature
        tampered = token[:-4] + "abcd"
        assert decode_access_token(tampered) is None

        # 4. Malformed string
        assert decode_access_token("not.a.jwt.token") is None
        assert decode_access_token("") is None
