"""Authentication helpers, JWT utilities, and OAuth2 security dependencies."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import httpx
import jwt
from beanie import PydanticObjectId
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import get_settings
from app.models import User

settings = get_settings()
security = HTTPBearer()


def hash_password(password: str) -> str:
    """Hash a plain text password using bcrypt."""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain text password against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8")
        )
    except Exception:
        return False


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Generate a JWT access token containing the specified claims."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(
        to_encode,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM
    )
    return encoded_jwt


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT access token."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM]
        )
        return payload
    except jwt.PyJWTError:
        return None


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> User:
    """FastAPI dependency to retrieve the currently authenticated user from DB."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization credentials missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials / Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload: missing sub claim",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user = await User.get(PydanticObjectId(user_id))
    except Exception:
        # User ID might not be a valid PydanticObjectId in some test cases
        user = None

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def verify_google_token(id_token: str) -> dict:
    """Verify a Google credential (ID Token) and return user profile details.

    Supports mock tokens for local testing.
    """
    # Fallback to mock profile if token starts with a mock prefix
    if id_token.startswith("mock_google_token_"):
        parts = id_token.split("_")
        # Format: mock_google_token_email_name
        email = f"{parts[3]}" if len(parts) > 3 else "google.user@example.com"
        name = parts[4].replace("-", " ") if len(parts) > 4 else "Google User"
        return {
            "sub": f"mock_google_id_{email}",
            "email": email,
            "name": name,
            "picture": f"https://api.dicebear.com/7.x/adventurer/svg?seed={email}"
        }

    # Fetch token metadata from Google API
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": id_token},
                timeout=5.0
            )
            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid Google OAuth token"
                )
            data = response.json()

            # Verify client ID (audience) if customized in settings
            if (
                settings.GOOGLE_CLIENT_ID
                and settings.GOOGLE_CLIENT_ID != "mock-client-id-for-testing.apps.googleusercontent.com"
            ):
                if data.get("aud") != settings.GOOGLE_CLIENT_ID:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Google token audience mismatch"
                    )

            return data
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to connect to Google OAuth service: {str(e)}"
            )
