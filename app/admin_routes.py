"""Router for administrative management and database telemetry."""

from __future__ import annotations

from typing import List
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, EmailStr

from app.auth import get_current_user
from app.models import User, FileSystemItem, StoragePartition
from app.database import database

router = APIRouter(prefix="/api/admin", tags=["Super Admin"])


# ── Req/Res Schemas ──────────────────────────────────────────────────────────

class AdminUserResponse(BaseModel):
    id: str
    name: str
    email: str
    isAdmin: bool
    storageLimitBytes: int
    pricingPlan: str
    createdAt: str
    totalFiles: int
    spaceUsed: int


class EditLimitRequest(BaseModel):
    limitBytes: int


class EditRoleRequest(BaseModel):
    isAdmin: bool


class MessageResponse(BaseModel):
    message: str


# ── Dependency ────────────────────────────────────────────────────────────────

async def admin_required(current_user: User = Depends(get_current_user)) -> User:
    """Dependency to enforce admin access controls."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Administrative privileges required."}
        )
    return current_user


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get(
    "/users",
    response_model=List[AdminUserResponse],
    summary="List all users and their usage statistics"
)
async def list_users(admin: User = Depends(admin_required)):
    """Retrieve details of all registered users with space usage statistics."""
    users = await User.find_all().to_list()
    response = []
    
    for u in users:
        user_id_str = str(u.id)
        # Count all files belonging to this user
        files = await FileSystemItem.find(
            FileSystemItem.user_id == user_id_str,
            FileSystemItem.type == "file",
            FileSystemItem.is_deleted == False
        ).to_list()
        
        total_files = len(files)
        space_used = sum(f.size or 0 for f in files)
        
        response.append(
            AdminUserResponse(
                id=user_id_str,
                name=u.name,
                email=u.email,
                isAdmin=u.is_admin,
                storageLimitBytes=u.storage_limit_bytes,
                pricingPlan=u.pricing_plan,
                createdAt=u.created_at.isoformat() if u.created_at else "",
                totalFiles=total_files,
                spaceUsed=space_used
            )
        )
        
    return response


@router.put(
    "/users/{user_id}/limit",
    response_model=MessageResponse,
    summary="Change a user's storage limit"
)
async def edit_user_limit(
    user_id: str,
    body: EditLimitRequest,
    admin: User = Depends(admin_required)
):
    """Change the total storage limit (in bytes) for a specific user."""
    from beanie import PydanticObjectId
    try:
        user = await User.get(PydanticObjectId(user_id))
    except Exception:
        user = None
        
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "User not found."}
        )
        
    if body.limitBytes < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Storage limit cannot be negative."}
        )
        
    await user.update({"$set": {"storage_limit_bytes": body.limitBytes}})
    return MessageResponse(
        message=f"Storage limit for user {user.email} updated to {body.limitBytes} bytes."
    )


@router.put(
    "/users/{user_id}/role",
    response_model=MessageResponse,
    summary="Toggle user admin role status"
)
async def edit_user_role(
    user_id: str,
    body: EditRoleRequest,
    admin: User = Depends(admin_required)
):
    """Grant or revoke administrative privileges for a user."""
    from beanie import PydanticObjectId
    try:
        user = await User.get(PydanticObjectId(user_id))
    except Exception:
        user = None
        
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "User not found."}
        )
        
    if str(user.id) == str(admin.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "You cannot revoke your own administrator privileges."}
        )
        
    await user.update({"$set": {"is_admin": body.isAdmin}})
    role_str = "administrator" if body.isAdmin else "standard user"
    return MessageResponse(
        message=f"Role status for user {user.email} updated to {role_str}."
    )


@router.delete(
    "/users/{user_id}",
    response_model=MessageResponse,
    summary="Delete user account and all owned items"
)
async def delete_user(
    user_id: str,
    admin: User = Depends(admin_required)
):
    """Deactivate user, purge B2 files and delete all database documents."""
    from beanie import PydanticObjectId
    try:
        user = await User.get(PydanticObjectId(user_id))
    except Exception:
        user = None
        
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "User not found."}
        )
        
    if str(user.id) == str(admin.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "You cannot delete your own administrator account."}
        )
        
    # 1. Fetch user items
    items = await FileSystemItem.find(FileSystemItem.user_id == user_id).to_list()
    
    # 2. Delete B2 files
    from app.b2 import handle_b2_delete
    for item in items:
        if item.type == "file":
            try:
                await handle_b2_delete(item, user_id)
            except Exception:
                pass  # Ignore missing B2 files
                
    # 3. Purge DB items, partitions, and user document
    await FileSystemItem.find(FileSystemItem.user_id == user_id).delete()
    await StoragePartition.find(StoragePartition.user_id == user_id).delete()
    await user.delete()
    
    return MessageResponse(
        message=f"User {user.email} and all owned storage contents have been deleted."
    )


@router.get(
    "/db-stats",
    summary="Fetch MongoDB cluster stats telemetry"
)
async def get_db_stats(admin: User = Depends(admin_required)):
    """Fetch raw database level statistics directly from the MongoDB engine."""
    try:
        stats = await database.command("dbStats")
        # Extract/return key indicators safely
        return {
            "db": stats.get("db", ""),
            "collections": stats.get("collections", 0),
            "objects": stats.get("objects", 0),
            "avgObjSize": stats.get("avgObjSize", 0.0),
            "dataSize": stats.get("dataSize", 0),
            "storageSize": stats.get("storageSize", 0),
            "indexes": stats.get("indexes", 0),
            "indexSize": stats.get("indexSize", 0),
            "ok": stats.get("ok", 1.0)
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": f"Failed to execute dbStats query: {str(e)}"}
        )
