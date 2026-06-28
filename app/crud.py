"""Database CRUD operations for file system items (MongoDB/Beanie) with user isolation."""

from __future__ import annotations

import re
from typing import List, Optional

from beanie import PydanticObjectId

from app.models import FileSystemItem


async def get_all_items(user_id: str) -> List[FileSystemItem]:
    """Retrieve all file system items for a specific user."""
    return await FileSystemItem.find(FileSystemItem.user_id == user_id).sort("+created_at").to_list()


async def get_item_by_id(item_id: str, user_id: str) -> Optional[FileSystemItem]:
    """Retrieve a single item by ID and verify it belongs to the user."""
    try:
        return await FileSystemItem.find_one({"_id": PydanticObjectId(item_id), "user_id": user_id})
    except Exception:
        # Also try matching by string id for seeded items
        return await FileSystemItem.find_one({"_id": item_id, "user_id": user_id})


async def check_duplicate_name(
    name: str,
    parent_id: Optional[str],
    item_type: str,
    user_id: str,
    exclude_id: Optional[str] = None,
) -> bool:
    """Check if an item with the same name exists in the same parent directory for the user."""
    query = {
        "name": {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"},
        "type": item_type,
        "is_deleted": False,
        "parent_id": parent_id,
        "user_id": user_id,
    }

    if exclude_id:
        query["_id"] = {"$ne": exclude_id}

    result = await FileSystemItem.find_one(query)
    return result is not None


async def create_item(
    name: str,
    item_type: str,
    user_id: str,
    parent_id: Optional[str] = None,
    size: Optional[int] = None,
    partition_id: Optional[str] = None,
) -> FileSystemItem:
    """Create a new file system item for the user."""
    item = FileSystemItem(
        name=name.strip(),
        type=item_type,
        parent_id=parent_id,
        user_id=user_id,
        size=size,
        starred=False,
        is_deleted=False,
        partition_id=partition_id,
    )
    await item.insert()
    return item


async def rename_item(item: FileSystemItem, new_name: str) -> FileSystemItem:
    """Rename a file system item."""
    item.name = new_name.strip()
    await item.save()
    return item


async def move_item(
    item: FileSystemItem, target_parent_id: Optional[str]
) -> FileSystemItem:
    """Move an item to a different parent folder."""
    item.parent_id = target_parent_id
    
    # Update partition_id based on target parent folder
    new_partition_id = None
    if target_parent_id:
        parent = await FileSystemItem.get(target_parent_id)
        if parent:
            new_partition_id = parent.partition_id
            
    item.partition_id = new_partition_id
    await item.save()
    
    # Recursively update partitions of all children if folder
    if item.type == "folder":
        await _update_descendant_partitions(str(item.id), str(item.user_id), new_partition_id)
        
    return item


async def toggle_star(item: FileSystemItem) -> FileSystemItem:
    """Toggle the starred status of an item."""
    item.starred = not item.starred
    await item.save()
    return item


async def soft_delete_item(item_id: str, user_id: str) -> List[str]:
    """Soft-delete an item and all its descendants. Returns IDs of affected items."""
    affected_ids = await _collect_descendant_ids(item_id, user_id)

    query_ids = []
    for aid in affected_ids:
        try:
            query_ids.append(PydanticObjectId(aid))
        except Exception:
            query_ids.append(aid)

    await FileSystemItem.find(
        {"_id": {"$in": query_ids}, "user_id": user_id}
    ).update_many({"$set": {"is_deleted": True}})

    return affected_ids


async def hard_delete_item(item_id: str, user_id: str) -> List[str]:
    """Permanently delete an item and all its descendants."""
    affected_ids = await _collect_descendant_ids(item_id, user_id)

    query_ids = []
    for aid in affected_ids:
        try:
            query_ids.append(PydanticObjectId(aid))
        except Exception:
            query_ids.append(aid)

    await FileSystemItem.find(
        {"_id": {"$in": query_ids}, "user_id": user_id}
    ).delete()

    return affected_ids


async def restore_item(item_id: str, user_id: str) -> List[str]:
    """Restore a soft-deleted item and all its descendants."""
    affected_ids = await _collect_descendant_ids(item_id, user_id)

    query_ids = []
    for aid in affected_ids:
        try:
            query_ids.append(PydanticObjectId(aid))
        except Exception:
            query_ids.append(aid)

    await FileSystemItem.find(
        {"_id": {"$in": query_ids}, "user_id": user_id}
    ).update_many({"$set": {"is_deleted": False}})

    return affected_ids


async def duplicate_item(
    item: FileSystemItem, user_id: str, target_parent_id: Optional[str] = None, use_target_parent: bool = False
) -> FileSystemItem:
    """Duplicate an item with a 'copy' suffix."""
    base_name = re.sub(r" copy(?: \d+)?$", "", item.name)
    parent_id = target_parent_id if use_target_parent else item.parent_id

    # Count existing copies in the same directory
    existing_copies = await FileSystemItem.find(
        {
            "parent_id": parent_id,
            "user_id": user_id,
            "name": {"$regex": f"^{re.escape(base_name)} copy", "$options": "i"},
            "is_deleted": False,
        }
    ).to_list()

    copy_suffix = "" if len(existing_copies) == 0 else f" {len(existing_copies) + 1}"

    new_item = FileSystemItem(
        name=f"{base_name} copy{copy_suffix}",
        type=item.type,
        parent_id=parent_id,
        user_id=user_id,
        size=item.size,
        starred=False,
        is_deleted=False,
    )
    await new_item.insert()
    return new_item


async def _collect_descendant_ids(root_id: str, user_id: str) -> List[str]:
    """Recursively collect all descendant IDs of a given root item."""
    ids: List[str] = [root_id]
    queue: List[str] = [root_id]

    while queue:
        current_id = queue.pop(0)
        children = await FileSystemItem.find(
            {"parent_id": current_id, "user_id": user_id}
        ).to_list()
        for child in children:
            child_id = str(child.id)
            ids.append(child_id)
            queue.append(child_id)

    return ids


async def get_user_storage_size(user_id: str) -> int:
    """Calculate the total size in bytes of all files for a user (including soft-deleted)."""
    items = await FileSystemItem.find({"user_id": user_id, "type": "file"}).to_list()
    return sum(item.size or 0 for item in items)


async def _update_descendant_partitions(root_id: str, user_id: str, new_partition_id: Optional[str]) -> None:
    """Recursively update the partition ID for all descendant items."""
    descendant_ids = await _collect_descendant_ids(root_id, user_id)
    query_ids = []
    for aid in descendant_ids:
        try:
            query_ids.append(PydanticObjectId(aid))
        except Exception:
            query_ids.append(aid)
            
    await FileSystemItem.find(
        {"_id": {"$in": query_ids}, "user_id": user_id}
    ).update_many({"$set": {"partition_id": new_partition_id}})


async def get_accessible_items(user_id: str, email: str) -> List[FileSystemItem]:
    """Retrieve all items owned by or shared with the specified user (recursively)."""
    # 1. Get all items owned by the user
    owned_items = await FileSystemItem.find(FileSystemItem.user_id == user_id).to_list()
    
    # 2. Get all items directly shared with the user (by user_id or email)
    shared_roots = await FileSystemItem.find({
        "shares.user_id": user_id,
        "is_deleted": False
    }).to_list()
    
    shared_roots_by_email = await FileSystemItem.find({
        "shares.email": email.lower(),
        "is_deleted": False
    }).to_list()
    
    # Merge direct shared roots
    all_shared_roots = {str(item.id): item for item in (shared_roots + shared_roots_by_email)}
    
    # 3. For each shared root, fetch all descendants recursively
    descendant_items = []
    visited_descendants = set()
    queue = [str(item.id) for item in all_shared_roots.values() if item.type == "folder"]
    
    while queue:
        current_folder_id = queue.pop(0)
        children = await FileSystemItem.find({
            "parent_id": current_folder_id,
            "is_deleted": False
        }).to_list()
        
        for child in children:
            child_id = str(child.id)
            if child_id not in visited_descendants:
                visited_descendants.add(child_id)
                descendant_items.append(child)
                if child.type == "folder":
                    queue.append(child_id)
                    
    # Combine all items
    all_items = {str(item.id): item for item in owned_items}
    for item in all_shared_roots.values():
        all_items[str(item.id)] = item
    for item in descendant_items:
        all_items[str(item.id)] = item
        
    return list(all_items.values())
