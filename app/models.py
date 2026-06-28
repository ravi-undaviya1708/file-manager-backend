"""Beanie document models for the file manager."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, List

from beanie import Document
from pydantic import Field, EmailStr, BaseModel


class User(Document):
    """Represents a registered user in the application.

    Stored as a document in the 'users' MongoDB collection.
    """

    email: EmailStr = Field(..., unique=True)
    hashed_password: Optional[str] = Field(default=None)
    name: str = Field(..., max_length=255)
    google_id: Optional[str] = Field(default=None)
    avatar_url: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_admin: bool = Field(default=False)
    storage_limit_bytes: int = Field(default=10200547328)  # 9.5 GB
    pricing_plan: str = Field(default="free")

    class Settings:
        name = "users"
        indexes = [
            "email",
            "google_id",
        ]

    def __repr__(self) -> str:
        return f"<User(id={self.id}, email={self.email}, name={self.name})>"


class ItemShare(BaseModel):
    """Represents sharing metadata for a file or folder."""
    user_id: str
    email: str
    permission: str  # "viewer" or "editor"


class FileSystemItem(Document):
    """Represents a file or folder in the file manager.

    Stored as a document in the 'file_system_items' MongoDB collection.
    """

    name: str = Field(..., max_length=255)
    type: str = Field(..., pattern="^(folder|file)$")
    parent_id: Optional[str] = Field(default=None)
    user_id: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    size: Optional[int] = Field(default=None)
    starred: bool = Field(default=False)
    is_deleted: bool = Field(default=False)
    is_locked: bool = Field(default=False)
    lock_password_hash: Optional[str] = Field(default=None)
    is_hidden: bool = Field(default=False)
    partition_id: Optional[str] = Field(default=None)
    shares: List[ItemShare] = Field(default_factory=list)

    class Settings:
        name = "file_system_items"
        indexes = [
            "name",
            "parent_id",
            "is_deleted",
            "starred",
            "user_id",
            "partition_id",
        ]

    def __repr__(self) -> str:
        return f"<FileSystemItem(id={self.id}, name={self.name}, type={self.type}, user_id={self.user_id})>"


class StoragePartition(Document):
    """Represents a virtual storage partition created by a user."""
    
    user_id: str = Field(..., max_length=255)
    name: str = Field(..., max_length=255)
    allocated_size_bytes: int = Field(...)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_locked: bool = Field(default=False)
    lock_password_hash: Optional[str] = Field(default=None)

    class Settings:
        name = "storage_partitions"
        indexes = [
            "user_id",
        ]

    def __repr__(self) -> str:
        return f"<StoragePartition(id={self.id}, name={self.name}, user_id={self.user_id}, size={self.allocated_size_bytes})>"
