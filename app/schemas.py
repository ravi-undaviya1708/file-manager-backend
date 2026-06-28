"""Pydantic schemas for request/response validation."""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ItemType(str, Enum):
    folder = "folder"
    file = "file"


# ── Response Schemas ──────────────────────────────────────────────────────────


class ItemShareResponse(BaseModel):
    """Schema for sharing metadata of an item."""
    userId: str
    email: str
    permission: str  # "viewer" or "editor"

    model_config = {"from_attributes": True}


class FileSystemItemResponse(BaseModel):
    """Schema returned to the frontend — matches the TypeScript FileSystemItem interface."""

    id: str
    name: str
    type: ItemType
    parentId: Optional[str] = None
    createdAt: str
    size: Optional[int] = None
    starred: bool = False
    isDeleted: bool = False
    isLocked: bool = False
    isHidden: bool = False
    shares: Optional[List[ItemShareResponse]] = None
    ownerId: Optional[str] = None
    ownerEmail: Optional[str] = None
    partitionId: Optional[str] = None

    model_config = {"from_attributes": True}


# ── Request Schemas ───────────────────────────────────────────────────────────


class ShareItemRequest(BaseModel):
    """Request to share a folder or file."""
    email: str
    permission: str = Field(..., pattern="^(viewer|editor)$")


class CreateFolderRequest(BaseModel):
    """Create a new folder."""

    name: str = Field(..., min_length=1, max_length=255, description="Folder name")
    parentId: Optional[str] = Field(None, description="Parent folder ID, null for root")
    type: ItemType = Field(ItemType.folder, description="Must be 'folder'")
    partitionId: Optional[str] = Field(None, description="Optional partition ID")


class LockFolderRequest(BaseModel):
    """Request schema to lock a folder."""

    password: str = Field(..., min_length=4, description="Folder password")


class RenameItemRequest(BaseModel):
    """Rename a file or folder."""

    name: str = Field(..., min_length=1, max_length=255, description="New name")


class MoveItemRequest(BaseModel):
    """Move item to a different parent folder."""

    targetParentId: Optional[str] = Field(
        None, description="Target parent folder ID, null for root"
    )


class DuplicateItemResponse(BaseModel):
    """Response for duplicate operation."""

    id: str
    name: str
    type: ItemType
    parentId: Optional[str] = None
    createdAt: str
    size: Optional[int] = None
    starred: bool = False
    isDeleted: bool = False

    model_config = {"from_attributes": True}


# ── Upload Schema ─────────────────────────────────────────────────────────────


class UploadFileResponse(BaseModel):
    """Response after uploading a file."""

    id: str
    name: str
    type: ItemType = ItemType.file
    parentId: Optional[str] = None
    createdAt: str
    size: Optional[int] = None
    starred: bool = False
    isDeleted: bool = False

    model_config = {"from_attributes": True}


# ── Generic ───────────────────────────────────────────────────────────────────


class MessageResponse(BaseModel):
    """Generic message response."""

    message: str


class ErrorResponse(BaseModel):
    """Error detail response."""

    error: str


# ── Auth Schemas ──────────────────────────────────────────────────────────────


from pydantic import EmailStr


class UserRegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Full Name")
    email: EmailStr = Field(..., description="User Email Address")
    password: str = Field(..., min_length=6, description="Password (min 6 characters)")


class UserLoginRequest(BaseModel):
    email: EmailStr = Field(..., description="User Email Address")
    password: str = Field(..., description="User Password")


class GoogleLoginRequest(BaseModel):
    credential: str = Field(..., description="Google ID Token JWT credential")


class UserResponse(BaseModel):
    id: str
    name: str
    email: str
    avatarUrl: Optional[str] = None
    createdAt: str
    isAdmin: bool = False
    storageLimitBytes: int = 10200547328
    pricingPlan: str = "free"

    model_config = {"from_attributes": True}


class AuthTokenResponse(BaseModel):
    token: str
    user: UserResponse


class PartitionResponse(BaseModel):
    id: str
    name: str
    allocatedSizeBytes: int
    usedSizeBytes: int
    createdAt: str
    isLocked: bool = False

    model_config = {"from_attributes": True}


class CreatePartitionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    allocatedSizeBytes: int = Field(..., gt=0)


class ResizePartitionRequest(BaseModel):
    name: Optional[str] = None
    allocatedSizeBytes: Optional[int] = Field(None, gt=0)

