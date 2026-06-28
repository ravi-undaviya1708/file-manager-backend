"""Helper functions for folder lock and hierarchy security validation."""

import json
import logging
from typing import Dict, Optional
from fastapi import Request

from app.auth import verify_password
from app.models import FileSystemItem

logger = logging.getLogger(__name__)


def get_unlocked_passwords(request: Request, query_passwords: Optional[str] = None) -> Dict[str, str]:
    """Parses folder passwords from query parameter 'passwords' or X-Folder-Passwords header.
    
    Expected format is a JSON dictionary: {"folder_id": "password"}
    """
    if query_passwords:
        try:
            data = json.loads(query_passwords)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            logger.warning("Failed to parse query passwords: %s", e)

    header_val = request.headers.get("x-folder-passwords") or request.headers.get("X-Folder-Passwords")
    if header_val:
        try:
            data = json.loads(header_val)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            logger.warning("Failed to parse X-Folder-Passwords header: %s", e)

    return {}


async def is_access_blocked(
    item: FileSystemItem,
    user_id: str,
    unlocked_passwords: Dict[str, str],
    items_by_id: Optional[Dict[str, FileSystemItem]] = None,
) -> bool:
    """Recursively check if access to this item is blocked by a locked ancestor or itself.
    
    Used when modifying, deleting, or viewing this item directly.
    """
    current = item
    visited = set()

    while current:
        current_id = str(current.id)
        if current_id in visited:
            # Prevent infinite loop
            break
        visited.add(current_id)

        # Check if the current item is locked and credentials do not match
        if getattr(current, "is_locked", False):
            submitted_pwd = unlocked_passwords.get(current_id)
            if not submitted_pwd or not verify_password(submitted_pwd, current.lock_password_hash or ""):
                return True

        # Walk up the parent chain
        parent_id = current.parent_id
        if not parent_id:
            break

        if items_by_id and parent_id in items_by_id:
            current = items_by_id[parent_id]
        else:
            # Fetch from DB
            from app import crud
            parent_item = await crud.get_item_by_id(parent_id, user_id)
            if not parent_item:
                break
            current = parent_item

    return False


async def is_lineage_blocked(
    item: FileSystemItem,
    user_id: str,
    unlocked_passwords: Dict[str, str],
    items_by_id: Optional[Dict[str, FileSystemItem]] = None,
) -> bool:
    """Check if access to this item is blocked by any of its parent ancestors.
    
    Used in folder listing so the locked folder itself can be rendered, but its children are hidden.
    """
    if not item.parent_id:
        return False

    current_id = item.parent_id
    visited = set()

    while current_id:
        if current_id in visited:
            # Prevent infinite loop
            break
        visited.add(current_id)

        # Fetch current ancestor
        if items_by_id and current_id in items_by_id:
            current_item = items_by_id[current_id]
        else:
            from app import crud
            current_item = await crud.get_item_by_id(current_id, user_id)

        if not current_item:
            break

        # Check if the ancestor is locked and credentials do not match
        if getattr(current_item, "is_locked", False):
            submitted_pwd = unlocked_passwords.get(current_id)
            if not submitted_pwd or not verify_password(submitted_pwd, current_item.lock_password_hash or ""):
                return True

        current_id = current_item.parent_id

    return False


async def get_effective_sharing_permission(item: FileSystemItem, user) -> Optional[str]:
    """Walk up ancestry to find user's permission level ("owner", "editor", "viewer", or None)."""
    if not item or not user:
        return None

    if item.user_id == str(user.id):
        return "owner"

    current = item
    visited = set()

    while current:
        current_id = str(current.id)
        if current_id in visited:
            break
        visited.add(current_id)

        # Check shares list
        shares_list = getattr(current, "shares", []) or []
        for share in shares_list:
            if share.user_id == str(user.id) or share.email.lower() == user.email.lower():
                return share.permission  # "viewer" or "editor"

        if current.user_id == str(user.id):
            return "owner"

        parent_id = current.parent_id
        if not parent_id:
            break

        # Query parent (database-wide, bypass isolation)
        parent_item = await FileSystemItem.get(parent_id)
        if not parent_item:
            break
        current = parent_item

    return None


async def verify_read_access(item: FileSystemItem, user):
    """Verify user has read/view access to item. Raises 404/403 if not."""
    from fastapi import HTTPException
    perm = await get_effective_sharing_permission(item, user)
    if perm is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Item not found or access denied."}
        )


async def verify_write_access(item: FileSystemItem, user):
    """Verify user has write/edit access to item. Raises 404/403 if not."""
    from fastapi import HTTPException
    perm = await get_effective_sharing_permission(item, user)
    if perm is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Item not found or access denied."}
        )
    if perm == "viewer":
        raise HTTPException(
            status_code=403,
            detail={"error": "Access denied. You only have viewer permission for this shared item."}
        )
