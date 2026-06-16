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
    await item.save()
    return item


async def toggle_star(item: FileSystemItem) -> FileSystemItem:
    """Toggle the starred status of an item."""
    item.starred = not item.starred
    await item.save()
    return item


async def soft_delete_item(item_id: str, user_id: str) -> List[str]:
    """Soft-delete an item and all its descendants. Returns IDs of affected items."""
    affected_ids = await _collect_descendant_ids(item_id, user_id)

    await FileSystemItem.find(
        {"_id": {"$in": affected_ids}, "user_id": user_id}
    ).update_many({"$set": {"is_deleted": True}})

    return affected_ids


async def hard_delete_item(item_id: str, user_id: str) -> List[str]:
    """Permanently delete an item and all its descendants."""
    affected_ids = await _collect_descendant_ids(item_id, user_id)

    await FileSystemItem.find(
        {"_id": {"$in": affected_ids}, "user_id": user_id}
    ).delete()

    return affected_ids


async def restore_item(item_id: str, user_id: str) -> List[str]:
    """Restore a soft-deleted item and all its descendants."""
    affected_ids = await _collect_descendant_ids(item_id, user_id)

    await FileSystemItem.find(
        {"_id": {"$in": affected_ids}, "user_id": user_id}
    ).update_many({"$set": {"is_deleted": False}})

    return affected_ids


async def duplicate_item(
    item: FileSystemItem, user_id: str, target_parent_id: Optional[str] = None
) -> FileSystemItem:
    """Duplicate an item with a 'copy' suffix."""
    base_name = re.sub(r" copy(?: \d+)?$", "", item.name)
    parent_id = target_parent_id if target_parent_id is not None else item.parent_id

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
