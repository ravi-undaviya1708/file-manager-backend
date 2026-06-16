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


def upload_b2_file_from_path(file_path: str, b2_key: str) -> bool:
    """Upload a file to B2 from a disk path using optimized boto3 upload_file."""
    try:
        client = get_b2_client()
        client.upload_file(
            Filename=file_path,
            Bucket=settings.B2_BUCKET,
            Key=b2_key,
        )
        logger.info(f"B2: Uploaded file from '{file_path}' to key '{b2_key}'")
        return True
    except Exception as e:
        logger.error(f"B2 Error: Failed to upload file from '{file_path}' to key '{b2_key}': {e}")
        return False



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
    """Rename (Copy + Version-Purge Delete) an object in B2.
    
    Uses delete_b2_prefix_versions to purge all historical versions of the old
    key so that versioning-enabled B2 buckets don't leave residual 'ghost'
    virtual directory entries in the B2 console after renames.
    """
    if copy_b2_object(old_key, new_key):
        return delete_b2_prefix_versions(old_key)
    return False


def move_b2_prefix(old_prefix: str, new_prefix: str) -> bool:
    """Recursively move all files under a prefix (directory rename/move) in B2.
    
    After copying each file to its new key, all versions of the old key are
    purged so that the B2 console does not show residual 'FolderName*'
    virtual directory placeholders on versioned buckets.
    """
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
                if copy_b2_object(old_key, new_key):
                    # Purge all versions of old key, not just add a delete marker
                    if not delete_b2_prefix_versions(old_key):
                        success = False
                else:
                    success = False
        
        # Also purge the old prefix's .keep folder placeholder versions
        delete_b2_prefix_versions(f"{old_prefix}.keep")
        return success
    except Exception as e:
        logger.error(f"B2 Error: Failed to move prefix '{old_prefix}' to '{new_prefix}': {e}")
        return False


# ─── Event-Based Synchronization ───

async def get_user_b2_prefix(user_id: str) -> str:
    """Get custom B2 user prefix: {User first name}-{user-id-last-4-digits}."""
    from app.models import User
    from beanie import PydanticObjectId
    
    try:
        user = await User.get(PydanticObjectId(user_id))
    except Exception:
        user = await User.get(user_id)
        
    if user and user.name:
        first_name = user.name.strip().split()[0]
        last_4 = str(user.id)[-4:]
        return f"{first_name}-{last_4}"
        
    return user_id


async def handle_b2_rename(item: FileSystemItem, user_id: str, old_name: str):
    """Update object keys in B2 when a file or folder is renamed in MongoDB."""
    old_item = FileSystemItem(**item.model_dump())
    old_item.name = old_name
    
    old_path = await get_item_path(old_item, user_id)
    new_path = await get_item_path(item, user_id)
    
    prefix = await get_user_b2_prefix(user_id)
    old_key = f"{prefix}/{old_path}"
    new_key = f"{prefix}/{new_path}"
    
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
    
    prefix = await get_user_b2_prefix(user_id)
    old_key = f"{prefix}/{old_path}"
    new_key = f"{prefix}/{new_path}"
    
    if item.type == "folder":
        move_b2_prefix(f"{old_key}/", f"{new_key}/")
    else:
        rename_b2_object(old_key, new_key)


def delete_b2_prefix_versions(prefix: str) -> bool:
    """Permanently delete all versions and delete markers of all objects under a prefix.
    
    This is required for buckets with versioning enabled to prevent 'Test Folder*' indicators
    and delete markers from leaving residual folder placeholders in the B2 console.
    """
    try:
        client = get_b2_client()
        paginator = client.get_paginator("list_object_versions")
        pages = paginator.paginate(Bucket=settings.B2_BUCKET, Prefix=prefix)
        
        objects_to_delete = []
        for page in pages:
            for version in page.get("Versions", []):
                objects_to_delete.append({
                    "Key": version["Key"],
                    "VersionId": version["VersionId"]
                })
            for marker in page.get("DeleteMarkers", []):
                objects_to_delete.append({
                    "Key": marker["Key"],
                    "VersionId": marker["VersionId"]
                })
                
        if not objects_to_delete:
            logger.info(f"B2: No versions found to delete under prefix '{prefix}'")
            return True
            
        # Delete objects in batches of up to 1000 (S3 API limit)
        for i in range(0, len(objects_to_delete), 1000):
            batch = objects_to_delete[i:i+1000]
            client.delete_objects(
                Bucket=settings.B2_BUCKET,
                Delete={"Objects": batch, "Quiet": True}
            )
            
        logger.info(f"B2: Permanently deleted all versions and delete markers under prefix '{prefix}'")
        return True
    except Exception as e:
        logger.error(f"B2 Error: Failed to delete all versions under prefix '{prefix}': {e}")
        return False


async def handle_b2_delete(item: FileSystemItem, user_id: str):
    """Recursively delete B2 objects (by putting delete markers) when a file or folder is permanently deleted.
    
    This hides files from standard listings while preserving historical versions in B2 
    for 30 days to align with the bucket's lifecycle retention policy.
    """
    path = await get_item_path(item, user_id)
    prefix = await get_user_b2_prefix(user_id)
    key = f"{prefix}/{path}"
    
    if item.type == "folder":
        folder_prefix = f"{key}/"
        try:
            client = get_b2_client()
            paginator = client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=settings.B2_BUCKET, Prefix=folder_prefix)
            
            for page in pages:
                for obj in page.get("Contents", []):
                    delete_b2_object(obj["Key"])
            # Delete folder placeholder keep file
            delete_b2_object(f"{key}/.keep")
        except Exception as e:
            logger.error(f"B2 Error: Failed to list/delete items under prefix '{folder_prefix}': {e}")
    else:
        delete_b2_object(key)


# ─── Retroactive Sync on Login ───

async def check_and_sync_user(user_id: str):
    """Verify if user's root folder exists in B2, if not run a full retroactive sync from DB."""
    client = get_b2_client()
    prefix = await get_user_b2_prefix(user_id)
    root_key = f"{prefix}/.keep"
    
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
            key = f"{prefix}/{path}"
            
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


# ─── 2-Way Synchronization on Refresh ───

async def sync_b2_to_mongodb(user_id: str):
    """Synchronize Backblaze B2 state with MongoDB.
    
    - List all objects under the user's custom prefix.
    - Parse relative file paths and folder structures.
    - Create folders/files in MongoDB if they exist in B2 but not in MongoDB.
    - Update changed file sizes in MongoDB.
    - Remove folders/files from MongoDB if they are absent in B2 (excluding soft-deleted items).
    """
    logger.info(f"B2: Starting 2-way sync for user '{user_id}'...")
    prefix = await get_user_b2_prefix(user_id)
    client = get_b2_client()
    
    try:
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=settings.B2_BUCKET, Prefix=f"{prefix}/")
        
        b2_folders = set()
        b2_files = {}
        
        for page in pages:
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel_path = key[len(prefix):].lstrip("/")
                if not rel_path:
                    continue
                
                if rel_path.endswith("/.keep") or rel_path == ".keep":
                    folder_path = rel_path[:-6] if rel_path.endswith("/.keep") else ""
                    if folder_path:
                        b2_folders.add(folder_path)
                else:
                    b2_files[rel_path] = {
                        "size": obj.get("Size", 0),
                        "last_modified": obj.get("LastModified"),
                    }
        
        # Infer implied folders from files
        for file_path in list(b2_files.keys()):
            parts = file_path.split("/")
            for i in range(1, len(parts)):
                implied_folder = "/".join(parts[:i])
                b2_folders.add(implied_folder)
                
    except Exception as e:
        logger.error(f"B2 Error listing objects for user '{user_id}': {e}")
        return

    # Fetch all MongoDB items for this user
    db_items = await FileSystemItem.find(FileSystemItem.user_id == user_id).to_list()
    
    # Map item IDs to items for quick lookup
    db_items_by_id = {str(item.id): item for item in db_items}
    
    # Resolve relative path for every DB item
    db_item_paths = {}
    
    async def resolve_path(item: FileSystemItem) -> str:
        parts = []
        current = item
        visited = set()
        while current:
            parts.insert(0, current.name)
            p_id = current.parent_id
            if not p_id or p_id in visited:
                break
            visited.add(p_id)
            current = db_items_by_id.get(p_id)
        return "/".join(parts)

    for item in db_items:
        path = await resolve_path(item)
        db_item_paths[path] = item

    # 1. Create missing folders in MongoDB
    # Sort folders by depth so parent folders exist in the mapping before children
    sorted_folders = sorted(list(b2_folders), key=lambda p: p.count("/"))
    
    for folder_path in sorted_folders:
        if folder_path not in db_item_paths:
            parts = folder_path.split("/")
            folder_name = parts[-1]
            parent_path = "/".join(parts[:-1])
            parent_id = None
            if parent_path:
                parent_folder = db_item_paths.get(parent_path)
                if parent_folder:
                    parent_id = str(parent_folder.id)
            
            new_folder = FileSystemItem(
                name=folder_name,
                type="folder",
                parent_id=parent_id,
                user_id=user_id,
                is_deleted=False
            )
            await new_folder.insert()
            db_item_paths[folder_path] = new_folder
            logger.info(f"Sync: Created folder '{folder_path}' in MongoDB")

    # 2. Create missing files / update changed file sizes in MongoDB
    for file_path, file_info in b2_files.items():
        if file_path not in db_item_paths:
            parts = file_path.split("/")
            file_name = parts[-1]
            parent_path = "/".join(parts[:-1])
            parent_id = None
            if parent_path:
                parent_folder = db_item_paths.get(parent_path)
                if parent_folder:
                    parent_id = str(parent_folder.id)
            
            new_file = FileSystemItem(
                name=file_name,
                type="file",
                parent_id=parent_id,
                user_id=user_id,
                size=file_info["size"],
                is_deleted=False
            )
            await new_file.insert()
            db_item_paths[file_path] = new_file
            logger.info(f"Sync: Created file '{file_path}' in MongoDB")
        else:
            db_item = db_item_paths[file_path]
            if db_item.type == "file" and db_item.size != file_info["size"]:
                db_item.size = file_info["size"]
                await db_item.save()
                logger.info(f"Sync: Updated size of file '{file_path}' in MongoDB to {file_info['size']}")

    # 3. Prune MongoDB items that don't exist in B2 (excluding soft-deleted items)
    for path, db_item in list(db_item_paths.items()):
        if db_item.is_deleted:
            continue
        
        is_absent = False
        if db_item.type == "folder":
            if path not in b2_folders:
                is_absent = True
        elif db_item.type == "file":
            if path not in b2_files:
                is_absent = True
                
        if is_absent:
            await db_item.delete()
            logger.info(f"Sync: Pruned '{path}' ({db_item.type}) from MongoDB")
            
    logger.info(f"B2: 2-way sync complete for user '{user_id}'.")
