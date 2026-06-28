"""MongoDB connection and Beanie ODM initialization."""

from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie

from app.config import get_settings

settings = get_settings()

# Motor client (created once, reused across the app)
client = AsyncIOMotorClient(settings.MONGODB_URL)
database = client[settings.MONGODB_DB_NAME]


async def init_db() -> None:
    """Initialize Beanie ODM with document models."""
    from app.models import FileSystemItem, User, StoragePartition

    await init_beanie(
        database=database,
        document_models=[FileSystemItem, User, StoragePartition],
    )


async def close_db() -> None:
    """Close the MongoDB connection."""
    client.close()
