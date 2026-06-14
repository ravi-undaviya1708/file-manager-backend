"""Beanie document models for the file manager."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from beanie import Document
from pydantic import Field


class FileSystemItem(Document):
    """Represents a file or folder in the file manager.

    Stored as a document in the 'file_system_items' MongoDB collection.
    """

    name: str = Field(..., max_length=255)
    type: str = Field(..., pattern="^(folder|file)$")
    parent_id: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    size: Optional[int] = Field(default=None)
    starred: bool = Field(default=False)
    is_deleted: bool = Field(default=False)

    class Settings:
        name = "file_system_items"
        indexes = [
            "name",
            "parent_id",
            "is_deleted",
            "starred",
        ]

    def __repr__(self) -> str:
        return f"<FileSystemItem(id={self.id}, name={self.name}, type={self.type})>"
