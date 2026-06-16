"""Backblaze B2 S3-compatible cloud storage operations and MongoDB synchronization."""

from __future__ import annotations

import logging
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from typing import Optional

from app.config import get_settings
from app.models import FileSystemItem

logger = logging.getLogger("b2")
settings = get_settings()


def get_b2_client():
    """Create S3 client configured for Backblaze B2 S3-compatible API."""
    return boto3.client(
        "s3",
        endpoint_url=settings.B2_ENDPOINT,
        aws_access_key_id=settings.B2_KEY_ID,
        aws_secret_access_key=settings.B2_APPLICATION_KEY,
        config=Config(signature_version="s3v4"),
    )


async def get_item_path(item: FileSystemItem, user_id: str) -> str:
    """Recursively resolve the full directory path for a database item."""
    parts = []
    current = item
    visited = set()
    
    while current:
        parts.insert(0, current.name)
        parent_id = current.parent_id
        if not parent_id or parent_id in visited:
            break
        visited.add(parent_id)
        
        # Look up parent folder
        from beanie import PydanticObjectId
        parent = None
        try:
            parent = await FileSystemItem.find_one({"_id": PydanticObjectId(parent_id), "user_id": user_id})
        except Exception:
            parent = await FileSystemItem.find_one({"_id": parent_id, "user_id": user_id})
        
        current = parent
        
    return "/".join(parts)


def create_b2_object(key: str, body: bytes = b"") -> bool:
    """Upload or create a raw object/placeholder in Backblaze B2."""
    try:
        client = get_b2_client()
        client.put_object(
            Bucket=settings.B2_BUCKET,
            Key=key,
            Body=body,
        )
        logger.info(f"B2: Created object '{key}' in bucket '{settings.B2_BUCKET}'")
        return True
    except Exception as e:
        logger.error(f"B2 Error: Failed to create object '{key}': {e}")
        return False


def create_b2_folder(folder_path: str) -> bool:
    """Create a folder placeholder in B2 by placing a .keep file."""
    key = f"{folder_path}/.keep"
    return create_b2_object(key, b"")


def upload_b2_file(file_path: str, content: bytes) -> bool:
    """Upload a file to B2."""
    return create_b2_object(file_path, content)



def delete_b2_object(key: str) -> bool:
    """Delete an object from Backblaze B2."""
    try:
        client = get_b2_client()
        client.delete_object(
            Bucket=settings.B2_BUCKET,
            Key=key,
        )
        logger.info(f"B2: Deleted object '{key}'")
        return True
    except Exception as e:
        logger.error(f"B2 Error: Failed to delete object '{key}': {e}")
        return False


def copy_b2_object(old_key: str, new_key: str) -> bool:
    """Copy an object inside the Backblaze B2 bucket."""
    try:
        client = get_b2_client()
        client.copy_object(
            Bucket=settings.B2_BUCKET,
            CopySource={"Bucket": settings.B2_BUCKET, "Key": old_key},
            Key=new_key,
        )
        logger.info(f"B2: Copied '{old_key}' to '{new_key}'")
        return True
    except Exception as e:
        logger.error(f"B2 Error: Failed to copy '{old_key}' to '{new_key}': {e}")
        return False


def rename_b2_object(old_key: str, new_key: str) -> bool:
    """Rename (Copy + Delete) an object in B2."""
    if copy_b2_object(old_key, new_key):
        return delete_b2_object(old_key)
    return False


def move_b2_prefix(old_prefix: str, new_prefix: str) -> bool:
    """Recursively move all files under a prefix (directory rename/move) in B2."""
    try:
        client = get_b2_client()
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=settings.B2_BUCKET, Prefix=old_prefix)
        success = True
        
        for page in pages:
            for obj in page.get("Contents", []):
                old_key = obj["Key"]
                # Substitute the old prefix with new prefix
                new_key = old_key.replace(old_prefix, new_prefix, 1)
                if not rename_b2_object(old_key, new_key):
                    success = False
        return success
    except Exception as e:
        logger.error(f"B2 Error: Failed to move prefix '{old_prefix}' to '{new_prefix}': {e}")
        return False


# ─── Event-Based Synchronization ───

async def handle_b2_rename(item: FileSystemItem, user_id: str, old_name: str):
    """Update object keys in B2 when a file or folder is renamed in MongoDB."""
    old_item = FileSystemItem(**item.model_dump())
    old_item.name = old_name
    
    old_path = await get_item_path(old_item, user_id)
    new_path = await get_item_path(item, user_id)
    
    old_key = f"{user_id}/{old_path}"
    new_key = f"{user_id}/{new_path}"
    
    if item.type == "folder":
        move_b2_prefix(f"{old_key}/", f"{new_key}/")
    else:
        rename_b2_object(old_key, new_key)


async def handle_b2_move(item: FileSystemItem, user_id: str, old_parent_id: Optional[str]):
    """Update object keys in B2 when a file or folder is moved in MongoDB."""
    old_item = FileSystemItem(**item.model_dump())
    old_item.parent_id = old_parent_id
    
    old_path = await get_item_path(old_item, user_id)
    new_path = await get_item_path(item, user_id)
    
    old_key = f"{user_id}/{old_path}"
    new_key = f"{user_id}/{new_path}"
    
    if item.type == "folder":
        move_b2_prefix(f"{old_key}/", f"{new_key}/")
    else:
        rename_b2_object(old_key, new_key)


async def handle_b2_delete(item: FileSystemItem, user_id: str):
    """Recursively delete B2 objects when a file or folder is permanently deleted."""
    path = await get_item_path(item, user_id)
    key = f"{user_id}/{path}"
    
    if item.type == "folder":
        prefix = f"{key}/"
        try:
            client = get_b2_client()
            paginator = client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=settings.B2_BUCKET, Prefix=prefix)
            
            for page in pages:
                for obj in page.get("Contents", []):
                    delete_b2_object(obj["Key"])
            # Delete folder placeholder keep file
            delete_b2_object(f"{key}/.keep")
        except Exception as e:
            logger.error(f"B2 Error: Failed to list/delete items under prefix '{prefix}': {e}")
    else:
        delete_b2_object(key)


# ─── Retroactive Sync on Login ───

async def check_and_sync_user(user_id: str):
    """Verify if user's root folder exists in B2, if not run a full retroactive sync from DB."""
    client = get_b2_client()
    root_key = f"{user_id}/.keep"
    
    try:
        client.head_object(Bucket=settings.B2_BUCKET, Key=root_key)
        # Root folder exists, sync already completed
        logger.info(f"B2: Root folder for user '{user_id}' already exists. Sync skipped.")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "404":
            logger.error(f"B2 Error checking root key '{root_key}': {e}")
            return
            
    # Key doesn't exist (404), run full sync
    logger.info(f"B2: Starting retroactive sync for user '{user_id}'...")
    
    # 1. Create root folder keep file
    create_b2_object(root_key, b"")
    
    # 2. Fetch all MongoDB items for this user
    items = await FileSystemItem.find(FileSystemItem.user_id == user_id).to_list()
    
    # 3. Create respective placeholders inside B2
    for item in items:
        try:
            path = await get_item_path(item, user_id)
            key = f"{user_id}/{path}"
            
            if item.type == "folder":
                # Create folder keep file
                create_b2_object(f"{key}/.keep", b"")
            else:
                # Create mock file placeholder content detailing size/origin
                meta = (
                    f"Placeholder for existing file synced during B2 integration.\n"
                    f"Name: {item.name}\n"
                    f"Original Size: {item.size or 0} bytes\n"
                    f"Created: {item.created_at.isoformat() if item.created_at else ''}\n"
                ).encode("utf-8")
                create_b2_object(key, meta)
        except Exception as err:
            logger.error(f"B2 Error: Sync failed for item '{item.name}' ({item.id}): {err}")
            
    logger.info(f"B2: Retroactive sync complete for user '{user_id}'.")
