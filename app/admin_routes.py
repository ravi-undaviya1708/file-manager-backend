"""Router for administrative management and database telemetry."""

from __future__ import annotations

from typing import List
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, EmailStr

from app.auth import get_current_user
from app.models import User, FileSystemItem, StoragePartition
import app.database

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
    userType: str


class EditLimitRequest(BaseModel):
    limitBytes: int


class EditRoleRequest(BaseModel):
    userType: str


class MessageResponse(BaseModel):
    message: str


class RoleResponse(BaseModel):
    name: str
    key: str
    isDefault: bool
    description: str
    permissions: List[str]


class CreateRoleRequest(BaseModel):
    name: str
    key: str
    description: str = ""
    permissions: List[str] = []


class UpdateRoleRequest(BaseModel):
    name: str
    description: str = ""
    permissions: List[str] = []



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
                spaceUsed=space_used,
                userType=u.user_type
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
        
    user.storage_limit_bytes = body.limitBytes
    await user.save()
    return MessageResponse(
        message=f"Storage limit for user {user.email} updated to {body.limitBytes} bytes."
    )


@router.put(
    "/users/{user_id}/role",
    response_model=MessageResponse,
    summary="Change user role/type status"
)
async def edit_user_role(
    user_id: str,
    body: EditRoleRequest,
    admin: User = Depends(admin_required)
):
    """Change the security role/user type for a user."""
    from beanie import PydanticObjectId
    from app.models import Role
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
            detail={"error": "You cannot change your own administrator role privileges."}
        )
        
    # Check if role exists
    role_exists = await Role.find_one(Role.key == body.userType)
    if not role_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"Role '{body.userType}' does not exist in the database."}
        )

    # Validate that non-superAdmins cannot assign the superAdmin role
    if body.userType == "superAdmin" and admin.user_type != "superAdmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Only Super Admins can assign the Super Admin role."}
        )

    user.user_type = body.userType
    user.is_admin = body.userType in ["superAdmin", "admin"]
    await user.save()
    
    return MessageResponse(
        message=f"Role status for user {user.email} updated to {role_exists.name}."
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
        stats = await app.database.database.command("dbStats")
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


# ── Role Management Routes ──────────────────────────────────────────────────

@router.get(
    "/roles",
    response_model=List[RoleResponse],
    summary="List all security roles"
)
async def list_roles(admin: User = Depends(admin_required)):
    """Retrieve details of all roles. Filter out superAdmin for standard Admins."""
    from app.models import Role
    roles = await Role.find_all().to_list()
    
    # Filter out superAdmin if the logged-in user is not superAdmin
    if admin.user_type != "superAdmin":
        roles = [r for r in roles if r.key != "superAdmin"]
        
    return [
        RoleResponse(
            name=r.name,
            key=r.key,
            isDefault=r.is_default,
            description=r.description,
            permissions=r.permissions
        ) for r in roles
    ]


@router.post(
    "/roles",
    response_model=RoleResponse,
    summary="Create a custom role"
)
async def create_role(
    body: CreateRoleRequest,
    admin: User = Depends(admin_required)
):
    """Create a new custom user role."""
    from app.models import Role
    
    existing = await Role.find_one(Role.key == body.key)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": f"Role key '{body.key}' already exists."}
        )
        
    if body.key in ["superAdmin", "admin", "individual"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Cannot create a custom role with a default system key name."}
        )

    # Validate that standard Admins cannot create roles with manage_roles privileges
    if admin.user_type != "superAdmin" and "manage_roles" in body.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Standard Admins cannot create roles with manage_roles permissions."}
        )

    new_role = Role(
        name=body.name,
        key=body.key,
        is_default=False,
        description=body.description,
        permissions=body.permissions
    )
    await new_role.insert()
    
    return RoleResponse(
        name=new_role.name,
        key=new_role.key,
        isDefault=new_role.is_default,
        description=new_role.description,
        permissions=new_role.permissions
    )


@router.put(
    "/roles/{key}",
    response_model=RoleResponse,
    summary="Update a role"
)
async def update_role(
    key: str,
    body: UpdateRoleRequest,
    admin: User = Depends(admin_required)
):
    """Update custom or default role details."""
    from app.models import Role
    
    role = await Role.find_one(Role.key == key)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Role not found."}
        )
        
    # Standard admins cannot modify superAdmin role
    if key == "superAdmin" and admin.user_type != "superAdmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Only Super Admins can modify the Super Admin role."}
        )

    # Admins cannot modify default Admin role permissions.
    if key == "admin" and admin.user_type != "superAdmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Admins cannot change permissions of the default system Admin role."}
        )

    # Standard Admins cannot escalate a role's permissions to "manage_roles"
    if admin.user_type != "superAdmin" and "manage_roles" in body.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Standard Admins cannot assign manage_roles permission."}
        )

    # Update role properties
    role.name = body.name
    role.description = body.description
    
    # Default roles keep their permissions or can only be edited by Super Admin
    if not role.is_default or admin.user_type == "superAdmin":
        role.permissions = body.permissions
        
    await role.save()
    
    return RoleResponse(
        name=role.name,
        key=role.key,
        isDefault=role.is_default,
        description=role.description,
        permissions=role.permissions
    )


@router.delete(
    "/roles/{key}",
    response_model=MessageResponse,
    summary="Delete a custom role"
)
async def delete_role(
    key: str,
    admin: User = Depends(admin_required)
):
    """Delete a custom role. Default system roles cannot be deleted."""
    from app.models import Role, User
    
    role = await Role.find_one(Role.key == key)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Role not found."}
        )
        
    if role.is_default:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Default system roles cannot be deleted."}
        )
        
    # Prevent standard Admins from deleting roles that require superAdmin permissions
    if admin.user_type != "superAdmin" and "manage_roles" in role.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Standard Admins cannot delete roles containing manage_roles permissions."}
        )

    # Before deleting, check if any user is currently assigned this role
    assigned_users_count = await User.find(User.user_type == key).count()
    if assigned_users_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": f"Cannot delete role. There are {assigned_users_count} user(s) currently assigned to this role."}
        )

    await role.delete()
    return MessageResponse(message=f"Custom role '{role.name}' has been deleted.")

