"""Live Web Preview session generation and bundle rendering routes."""

from __future__ import annotations

from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import jwt
import anyio

from app.auth import get_current_user, decode_access_token
from app.config import get_settings
from app.models import User, FileSystemItem
from app.b2 import get_user_b2_prefix, get_item_path, get_b2_client

router = APIRouter(prefix="/api/workspace", tags=["Workspace Live Preview"])
settings = get_settings()

PREVIEW_TOKEN_EXP_MINUTES = 60  # 1 hour preview session TTL


def create_preview_token(user_id: str, folder_id: str) -> str:
    """Generate a signed preview JWT with short TTL."""
    if not settings.JWT_SECRET_KEY:
        raise RuntimeError("JWT_SECRET_KEY is not configured in application settings.")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "folder_id": folder_id,
        "scope": "codespace_preview",
        "iat": now,
        "exp": now + timedelta(minutes=PREVIEW_TOKEN_EXP_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def verify_preview_token(token: str, folder_id: str) -> str:
    """Validate token and ensure it has not expired and matches folder."""
    if not settings.JWT_SECRET_KEY:
        raise HTTPException(status_code=500, detail="JWT secret key is not configured")
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("scope") != "codespace_preview":
            raise HTTPException(status_code=403, detail="Invalid token scope")
        if payload.get("folder_id") != folder_id:
            raise HTTPException(status_code=403, detail="Token mismatch for this workspace folder")
        return payload.get("sub", "")
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Preview session has expired. Please launch a new preview from the Codespace editor."
        )
    except (jwt.InvalidTokenError, jwt.PyJWTError):
        raise HTTPException(status_code=403, detail="Invalid preview session token.")


@router.post("/{folder_id}/preview-token", summary="Generate a time-limited Live Preview session token")
async def generate_preview_session(
    folder_id: str,
    current_user: User = Depends(get_current_user),
):
    """Generates a secure preview session token valid for 1 hour."""
    root_folder = await FileSystemItem.get(folder_id)
    if not root_folder or root_folder.is_deleted:
        raise HTTPException(status_code=404, detail="Workspace folder not found")

    token = create_preview_token(str(current_user.id), folder_id)
    return {
        "preview_token": token,
        "expires_in_seconds": PREVIEW_TOKEN_EXP_MINUTES * 60,
        "folder_id": folder_id,
    }


@router.get("/{folder_id}/preview-bundle", summary="Fetch HTML and assets for live preview with token validation")
async def get_preview_bundle(
    folder_id: str,
    token: str = Query(..., description="Preview session token"),
):
    """
    Validates preview session token and returns the entrypoint HTML content.
    If session token has expired, returns 401.
    """
    user_id = verify_preview_token(token, folder_id)

    # Fetch all files under this workspace folder
    files = await FileSystemItem.find(
        FileSystemItem.user_id == user_id,
        FileSystemItem.is_deleted == False
    ).to_list()

    # Locate index.html or first HTML file in workspace
    html_item: Optional[FileSystemItem] = None
    for item in files:
        if item.type == "file" and item.name.lower() == "index.html":
            html_item = item
            break
    
    if not html_item:
        for item in files:
            if item.type == "file" and item.name.lower().endswith((".html", ".htm")):
                html_item = item
                break

    if not html_item:
        return {
            "title": "Codespace Preview",
            "html": "<!DOCTYPE html><html><body style='font-family:sans-serif;background:#0f172a;color:white;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;'><h2>No HTML file found in this workspace. Create an index.html file to preview.</h2></body></html>",
            "entrypoint": None,
        }

    # Download HTML file content from B2
    prefix = await get_user_b2_prefix(user_id)
    path = await get_item_path(html_item, user_id)
    key = f"{prefix}/{path}"

    def download():
        client = get_b2_client()
        resp = client.get_object(Bucket=settings.B2_BUCKET, Key=key)
        return resp["Body"].read().decode("utf-8", errors="replace")

    try:
        html_content = await anyio.to_thread.run_sync(download)
    except Exception as e:
        html_content = f"<html><body><h3>Error loading {html_item.name}: {str(e)}</h3></body></html>"

    return {
        "title": html_item.name,
        "html": html_content,
        "entrypoint": html_item.name,
    }
