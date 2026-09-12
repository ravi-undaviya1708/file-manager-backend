"""Automated Unit Tests for JWT Authentication and Password Security."""

import os
import unittest
from datetime import datetime, timedelta, timezone
import jwt
from fastapi import HTTPException

from app.auth import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.preview_routes import create_preview_token, verify_preview_token
from app.config import get_settings

settings = get_settings()


class TestJwtModuleIntegrity(unittest.TestCase):
    """Verify the Python environment has the correct PyJWT library loaded."""

    def test_jwt_module_has_required_attributes(self):
        """Catch 'AttributeError: module jwt has no attribute encode/decode'."""
        self.assertTrue(hasattr(jwt, "encode"), "module 'jwt' must have attribute 'encode' (PyJWT)")
        self.assertTrue(hasattr(jwt, "decode"), "module 'jwt' must have attribute 'decode' (PyJWT)")
        self.assertTrue(hasattr(jwt, "PyJWTError"), "module 'jwt' must expose PyJWTError")
        self.assertTrue(hasattr(jwt, "ExpiredSignatureError"), "module 'jwt' must expose ExpiredSignatureError")
        self.assertTrue(hasattr(jwt, "InvalidTokenError"), "module 'jwt' must expose InvalidTokenError")
        # Ensure it is from PyJWT
        self.assertTrue(callable(jwt.encode))
        self.assertTrue(callable(jwt.decode))


class TestJwtAccessTokens(unittest.TestCase):
    """Test access token lifecycle: creation, claims, validation, expiration."""

    def test_create_and_decode_access_token(self):
        user_id = "65f1a2b3c4d5e6f7a8b9c0d1"
        token = create_access_token(data={"sub": user_id})

        self.assertIsInstance(token, str)
        self.assertGreater(len(token), 20)

        payload = decode_access_token(token)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["sub"], user_id)
        self.assertIn("exp", payload)
        self.assertIn("iat", payload)

    def test_token_expiration_enforced(self):
        user_id = "65f1a2b3c4d5e6f7a8b9c0d2"
        # Create token already expired by 10 minutes
        expired_delta = timedelta(minutes=-10)
        token = create_access_token(data={"sub": user_id}, expires_delta=expired_delta)

        payload = decode_access_token(token)
        self.assertIsNone(payload, "Expired token must not decode successfully")

    def test_invalid_signature_rejected(self):
        user_id = "65f1a2b3c4d5e6f7a8b9c0d3"
        # Encode with an attacker secret key
        payload = {
            "sub": user_id,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            "iat": datetime.now(timezone.utc),
        }
        tampered_token = jwt.encode(payload, "wrong_attacker_secret_key_12345", algorithm="HS256")

        decoded = decode_access_token(tampered_token)
        self.assertIsNone(decoded, "Token with invalid signature must be rejected")

    def test_malformed_token_rejected(self):
        self.assertIsNone(decode_access_token("this.is.not.a.valid.jwt"))
        self.assertIsNone(decode_access_token(""))
        self.assertIsNone(decode_access_token("random_garbage_string"))

    def test_token_missing_sub_rejected(self):
        # Create a token without 'sub' claim
        payload = {
            "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            "iat": datetime.now(timezone.utc),
        }
        token_without_sub = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

        decoded = decode_access_token(token_without_sub)
        self.assertIsNone(decoded, "Token without 'sub' claim must fail decode requirement")


class TestPasswordSecurity(unittest.TestCase):
    """Test bcrypt hashing and verification."""

    def test_hash_and_verify_password(self):
        plain = "CorrectPassword123!#"
        hashed = hash_password(plain)

        self.assertNotEqual(hashed, plain)
        self.assertTrue(hashed.startswith("$2b$") or hashed.startswith("$2a$"))
        self.assertTrue(verify_password(plain, hashed))
        self.assertFalse(verify_password("WrongPassword", hashed))

    def test_verify_password_invalid_hash(self):
        self.assertFalse(verify_password("Password123", "not_a_valid_hash"))
        self.assertFalse(verify_password("Password123", ""))


class TestPreviewJwtTokens(unittest.TestCase):
    """Test Codespace Live Preview scoped token generation and verification."""

    def test_preview_token_lifecycle(self):
        user_id = "user_12345"
        folder_id = "folder_abcde"
        token = create_preview_token(user_id, folder_id)

        self.assertIsInstance(token, str)
        verified_user_id = verify_preview_token(token, folder_id)
        self.assertEqual(verified_user_id, user_id)

    def test_preview_token_folder_mismatch(self):
        user_id = "user_12345"
        folder_id = "folder_abcde"
        token = create_preview_token(user_id, folder_id)

        with self.assertRaises(HTTPException) as ctx:
            verify_preview_token(token, "different_folder_id")
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
