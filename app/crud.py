"""Database CRUD operations for file system items (MongoDB/Beanie) with user isolation and high performance."""

from __future__ import annotations

import re
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timezone

from beanie import PydanticObjectId

from app.models import FileSystemItem


async def get_all_items(user_id: str) -> List[FileSystemItem]:
    """Retrieve all file system items for a specific user."""
    return await FileSystemItem.find(
        FileSystemItem.user_id == user_id,
        FileSystemItem.is_deleted == False
    ).sort([("type", 1), ("name", 1)]).to_list()


async def get_item_by_id(item_id: str, user_id: str) -> Optional[FileSystemItem]:
    """Retrieve a single item by ID and verify it belongs to the user or is shared."""
    try:
        item = await FileSystemItem.find_one({"_id": PydanticObjectId(item_id)})
    except Exception:
        item = await FileSystemItem.find_one({"_id": item_id})
    return item


async def check_duplicate_name(
    name: str,
    parent_id: Optional[str],
    item_type: str,
    user_id: str,
    exclude_id: Optional[str] = None,
) -> bool:
    """Check if an item with the same name exists in the same parent directory for the user."""
    # Normalize parent_id
    clean_parent = None if (parent_id in (None, "", "root", "null")) else parent_id
    query: Dict[str, Any] = {
        "name": {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"},
        "type": item_type,
        "is_deleted": False,
        "parent_id": clean_parent,
        "user_id": user_id,
    }

    if exclude_id:
        try:
            query["_id"] = {"$ne": PydanticObjectId(exclude_id)}
        except Exception:
            query["_id"] = {"$ne": exclude_id}

    result = await FileSystemItem.find_one(query)
    return result is not None


async def create_item(
    name: str,
    item_type: str,
    user_id: Optional[str],
    parent_id: Optional[str] = None,
    size: Optional[int] = None,
    partition_id: Optional[str] = None,
) -> FileSystemItem:
    """Create a new file system item for the user."""
    clean_parent = None if (parent_id in (None, "", "root", "null")) else parent_id
    item = FileSystemItem(
        name=name.strip(),
        type=item_type,
        parent_id=clean_parent,
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
    clean_parent = None if (target_parent_id in (None, "", "root", "null")) else target_parent_id
    item.parent_id = clean_parent
    
    # Update partition_id based on target parent folder
    new_partition_id = None
    if clean_parent:
        parent = await FileSystemItem.get(clean_parent)
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


async def empty_bin(user_id: str) -> int:
    """Permanently delete all items in bin for a user and clean up Backblaze B2."""
    bin_items = await FileSystemItem.find(
        {"user_id": user_id, "is_deleted": True}
    ).to_list()

    if not bin_items:
        return 0

    # Sync B2 deletes
    try:
        from app.b2 import handle_b2_delete
        for item in bin_items:
            try:
                await handle_b2_delete(item, user_id)
            except Exception:
                pass
    except Exception:
        pass

    await FileSystemItem.find(
        {"user_id": user_id, "is_deleted": True}
    ).delete()

    return len(bin_items)


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
    item: FileSystemItem,
    user_id: str,
    target_parent_id: Optional[str] = None,
    use_target_parent: bool = False,
    partition_id: Optional[str] = None,
    is_root_clone: bool = True,
) -> FileSystemItem:
    """Duplicate an item with a 'copy' suffix, recursively copying children if folder."""
    raw_parent = target_parent_id if use_target_parent else item.parent_id
    clean_parent = None if (raw_parent in (None, "", "root", "null")) else raw_parent

    if is_root_clone:
        base_name = re.sub(r" copy(?: \d+)?$", "", item.name)
        # Count existing copies in the same directory
        existing_copies = await FileSystemItem.find(
            {
                "parent_id": clean_parent,
                "user_id": user_id,
                "name": {"$regex": f"^{re.escape(base_name)} copy", "$options": "i"},
                "is_deleted": False,
            }
        ).to_list()
        copy_suffix = "" if len(existing_copies) == 0 else f" {len(existing_copies) + 1}"
        new_name = f"{base_name} copy{copy_suffix}"
    else:
        new_name = item.name

    new_item = FileSystemItem(
        name=new_name,
        type=item.type,
        parent_id=clean_parent,
        user_id=user_id,
        size=item.size,
        starred=False,
        is_deleted=False,
        partition_id=partition_id if partition_id is not None else item.partition_id,
    )
    await new_item.insert()

    # Sync to B2
    try:
        from app.b2 import (
            get_user_b2_prefix,
            get_item_path,
            copy_b2_object_async,
            create_b2_folder_async,
        )
        src_owner_id = item.user_id if item.user_id else user_id
        src_prefix = await get_user_b2_prefix(src_owner_id)
        src_path = await get_item_path(item, src_owner_id)

        dest_prefix = await get_user_b2_prefix(user_id)
        dest_path = await get_item_path(new_item, user_id)

        if item.type == "file":
            src_key = f"{src_prefix}/{src_path}"
            dest_key = f"{dest_prefix}/{dest_path}"
            await copy_b2_object_async(src_key, dest_key)
        elif item.type == "folder":
            await create_b2_folder_async(f"{dest_prefix}/{dest_path}")
    except Exception as e:
        import logging
        logging.getLogger("b2").error(f"Error syncing duplicated item {new_item.id} to B2: {e}")

    # If folder, recursively duplicate children
    if item.type == "folder":
        children = await FileSystemItem.find(
            {"parent_id": str(item.id), "is_deleted": False}
        ).to_list()
        for child in children:
            await duplicate_item(
                item=child,
                user_id=user_id,
                target_parent_id=str(new_item.id),
                use_target_parent=True,
                partition_id=new_item.partition_id,
                is_root_clone=False,
            )

    return new_item


async def _collect_descendant_ids(root_id: str, user_id: str) -> List[str]:
    """Recursively collect all descendant IDs of a given root item using BFS."""
    ids: List[str] = [root_id]
    queue: List[str] = [root_id]
    visited = {root_id}

    while queue:
        current_id = queue.pop(0)
        children = await FileSystemItem.find(
            {"parent_id": current_id, "user_id": user_id}
        ).to_list()
        for child in children:
            child_id = str(child.id)
            if child_id not in visited:
                visited.add(child_id)
                ids.append(child_id)
                if child.type == "folder":
                    queue.append(child_id)

    return ids


import inspect


async def _run_filesystem_aggregation(pipeline: list, length: int = 1000) -> list:
    """Run an async MongoDB aggregation query safely across all Beanie, Motor, and PyMongo driver environments."""
    col_func = getattr(FileSystemItem, "get_pymongo_collection", None) or getattr(FileSystemItem, "get_motor_collection", None)
    if col_func is not None:
        collection = col_func()
    else:
        from app.database import database
        collection = database["file_system_items"]

    raw = collection.aggregate(pipeline)
    if inspect.isawaitable(raw):
        cursor = await raw
    else:
        cursor = raw

    if hasattr(cursor, "to_list"):
        return await cursor.to_list(length=length)
    elif hasattr(cursor, "__aiter__"):
        return [doc async for doc in cursor]
    elif hasattr(cursor, "__iter__"):
        return list(cursor)
    return []


async def get_user_storage_size(user_id: str) -> int:
    """Calculate the total size in bytes of all files for a user using MongoDB aggregation in O(1) time."""
    pipeline = [
        {"$match": {"user_id": user_id, "type": "file"}},
        {"$group": {"_id": None, "total_size": {"$sum": "$size"}}}
    ]
    results = await _run_filesystem_aggregation(pipeline, length=1)
    if results and "total_size" in results[0]:
        return int(results[0]["total_size"] or 0)
    return 0


async def get_all_partitions_used_sizes(user_id: str) -> Dict[str, int]:
    """Calculate used bytes for all user partitions in a single aggregation query."""
    pipeline = [
        {
            "$match": {
                "user_id": user_id,
                "type": "file",
                "is_deleted": False,
                "partition_id": {"$ne": None, "$exists": True}
            }
        },
        {
            "$group": {
                "_id": "$partition_id",
                "used_size": {"$sum": "$size"}
            }
        }
    ]
    results = await _run_filesystem_aggregation(pipeline, length=500)
    return {str(r["_id"]): int(r["used_size"] or 0) for r in results if r.get("_id")}


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


async def get_folder_children_paginated(
    user_id: str,
    email: str,
    parent_id: Optional[str] = None,
    partition_id: Optional[str] = None,
    starred: Optional[bool] = None,
    bin_only: bool = False,
    safe_folder_only: bool = False,
    shared_with_me: bool = False,
    category: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
    sort_by: str = "name",
    sort_order: str = "asc",
) -> Tuple[List[FileSystemItem], Optional[str], bool, int]:
    """High-performance lazy loader for immediate folder children with cursor pagination and DB sorting.
    
    Returns (items, next_cursor, has_more, total_count).
    """
    clean_parent = None if (parent_id in (None, "", "root", "null")) else parent_id
    limit = max(1, min(limit, 300))

    query: Dict[str, Any] = {}

    if bin_only:
        query["user_id"] = user_id
        if clean_parent is None:
            # Top-level bin items
            query["is_deleted"] = True
        else:
            query["parent_id"] = clean_parent
    elif shared_with_me:
        if clean_parent is None:
            query["$or"] = [
                {"shares.user_id": user_id},
                {"shares.email": email.lower()}
            ]
            query["is_deleted"] = False
        else:
            query["parent_id"] = clean_parent
            query["is_deleted"] = False
    elif safe_folder_only:
        query["user_id"] = user_id
        query["is_locked"] = True
        query["is_deleted"] = False
    elif starred:
        query["user_id"] = user_id
        query["starred"] = True
        query["is_deleted"] = False
    elif search and search.strip():
        query["user_id"] = user_id
        query["is_deleted"] = False
        query["name"] = {"$regex": re.escape(search.strip()), "$options": "i"}
    elif category:
        query["user_id"] = user_id
        query["type"] = "file"
        query["is_deleted"] = False
        cat = category.lower()
        if cat == "images":
            query["name"] = {"$regex": r"\.(jpg|jpeg|png|gif|webp|svg|bmp|ico)$", "$options": "i"}
        elif cat == "videos":
            query["name"] = {"$regex": r"\.(mp4|mkv|webm|avi|mov|wmv|flv)$", "$options": "i"}
        elif cat == "audio":
            query["name"] = {"$regex": r"\.(mp3|wav|ogg|flac|m4a|aac)$", "$options": "i"}
        elif cat == "documents":
            query["name"] = {"$regex": r"\.(pdf|doc|docx|txt|rtf|odt|xls|xlsx|ppt|pptx|csv|json|md)$", "$options": "i"}
    else:
        # Standard folder view (Root or subfolder)
        query["is_deleted"] = False
        if clean_parent is None:
            # Root view
            if partition_id:
                query["user_id"] = user_id
                query["parent_id"] = None
                query["partition_id"] = partition_id
            else:
                # User's root or top-level shared roots
                query["$or"] = [
                    {"user_id": user_id, "parent_id": None, "partition_id": None},
                    {"user_id": user_id, "parent_id": "", "partition_id": None},
                    {"shares.user_id": user_id, "parent_id": None},
                    {"shares.email": email.lower(), "parent_id": None},
                ]
        else:
            query["parent_id"] = clean_parent

    # Cursor pagination: if cursor is provided, filter by _id
    if cursor:
        try:
            query["_id"] = {"$gt": PydanticObjectId(cursor)}
        except Exception:
            query["_id"] = {"$gt": cursor}

    # Sorting direction
    order = 1 if sort_order.lower() == "asc" else -1
    sort_fields = []

    # Always sort folders before files in directory listings
    if not (category or search):
        sort_fields.append(("type", 1))

    if sort_by == "date" or sort_by == "createdAt":
        sort_fields.append(("created_at", order))
    elif sort_by == "size":
        sort_fields.append(("size", order))
    else:
        sort_fields.append(("name", order))

    sort_fields.append(("_id", 1))

    # Fetch limit + 1 items to see if there are more
    items = await FileSystemItem.find(query).sort(sort_fields).limit(limit + 1).to_list()

    has_more = len(items) > limit
    if has_more:
        items = items[:limit]
        next_cursor = str(items[-1].id) if items else None
    else:
        next_cursor = None

    return items, next_cursor, has_more, len(items)


async def get_accessible_items(user_id: str, email: str) -> List[FileSystemItem]:
    """Retrieve items owned by or shared with the user (optimized)."""
    owned_items = await FileSystemItem.find(
        FileSystemItem.user_id == user_id,
        FileSystemItem.is_deleted == False
    ).sort([("type", 1), ("name", 1)]).to_list()
    
    shared_items = await FileSystemItem.find({
        "$or": [
            {"shares.user_id": user_id},
            {"shares.email": email.lower()}
        ],
        "is_deleted": False
    }).to_list()
    
    all_map = {str(i.id): i for i in (owned_items + shared_items)}
    return list(all_map.values())
