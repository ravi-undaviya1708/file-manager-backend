"""Workspace management routes for Codespace IDE (Tree, CRUD, Search)."""

from __future__ import annotations

from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.models import User, FileSystemItem
from app import crud
from app.b2 import (
    create_b2_object_async,
    create_b2_folder_async,
    delete_b2_object_async,
    rename_b2_object_async,
    get_item_path,
    get_user_b2_prefix,
    upload_b2_file_async,
)
from app.security_helpers import verify_write_access, is_access_blocked, get_unlocked_passwords

router = APIRouter(prefix="/api/workspace", tags=["Workspace Management"])


class CreateWorkspaceFileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parentId: Optional[str] = Field(default=None)
    content: Optional[str] = Field(default="")


class CreateWorkspaceFolderRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parentId: Optional[str] = Field(default=None)


class RenameWorkspaceItemRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class WorkspaceSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100)


def _get_starter_content(filename: str) -> str:
    """Return friendly starter content for newly created code files."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    templates = {
        "py": 'def main():\n    print("Hello from Codespace!")\n\nif __name__ == "__main__":\n    main()\n',
        "js": 'console.log("Hello from Codespace!");\n',
        "ts": 'interface User {\n  name: string;\n}\n\nconst user: User = { name: "Codespace Developer" };\nconsole.log(`Hello, ${user.name}!`);\n',
        "html": '<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="UTF-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>Codespace App</title>\n  <style>\n    body { font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #0f172a; color: white; }\n  </style>\n</head>\n<body>\n  <h1>Welcome to your Codespace! 🚀</h1>\n</body>\n</html>\n',
        "css": '/* Stylesheet */\n* {\n  box-sizing: border-box;\n  margin: 0;\n  padding: 0;\n}\n',
        "json": '{\n  "name": "my-project",\n  "version": "1.0.0"\n}\n',
        "sh": '#!/bin/bash\necho "Running script..."\n',
        "cpp": '#include <iostream>\n\nint main() {\n    std::cout << "Hello from C++!" << std::endl;\n    return 0;\n}\n',
        "c": '#include <stdio.h>\n\nint main() {\n    printf("Hello from C!\\n");\n    return 0;\n}\n',
    }
    return templates.get(ext, "")


@router.get("/{folder_id}/tree", summary="Get full recursive workspace file tree")
async def get_workspace_tree(
    folder_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Retrieve all non-deleted files and subfolders in a workspace project folder."""
    root_folder = await FileSystemItem.get(folder_id)
    if not root_folder or root_folder.is_deleted:
        raise HTTPException(status_code=404, detail="Workspace folder not found")

    owner_id = root_folder.user_id if root_folder.user_id else str(current_user.id)
    
    # Check locked access
    unlocked_passwords = get_unlocked_passwords(request)
    if await is_access_blocked(root_folder, owner_id, unlocked_passwords):
        raise HTTPException(status_code=403, detail="Workspace folder is locked")

    # Fetch all items belonging to owner that are not deleted
    all_items = await FileSystemItem.find(
        FileSystemItem.user_id == owner_id,
        FileSystemItem.is_deleted == False
    ).to_list()

    # Map items by parent_id to quickly build subtree
    items_by_parent: Dict[Optional[str], List[FileSystemItem]] = {}
    for item in all_items:
        items_by_parent.setdefault(item.parent_id, []).append(item)

    # Recursive collector
    def collect_descendants(parent_id: str) -> List[Dict[str, Any]]:
        result = []
        children = items_by_parent.get(parent_id, [])
        for child in children:
            child_dict = {
                "id": str(child.id),
                "name": child.name,
                "type": child.type,
                "parentId": child.parent_id,
                "size": child.size,
                "createdAt": child.created_at.isoformat() if child.created_at else None,
                "starred": child.starred,
            }
            if child.type == "folder":
                child_dict["children"] = collect_descendants(str(child.id))
            result.append(child_dict)
        return result

    tree = {
        "id": str(root_folder.id),
        "name": root_folder.name,
        "type": "folder",
        "parentId": root_folder.parent_id,
        "createdAt": root_folder.created_at.isoformat() if root_folder.created_at else None,
        "children": collect_descendants(str(root_folder.id)),
    }
    return tree


@router.post("/{folder_id}/file", summary="Create new code file in workspace")
async def create_workspace_file(
    folder_id: str,
    body: CreateWorkspaceFileRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Create a new file in the workspace directory."""
    parent_target_id = body.parentId or folder_id
    parent = await FileSystemItem.get(parent_target_id)
    if not parent or parent.type != "folder":
        raise HTTPException(status_code=400, detail="Target parent folder not found")

    await verify_write_access(parent, current_user)
    owner_id = parent.user_id if parent.user_id else str(current_user.id)

    # Check duplicate
    if await crud.check_duplicate_name(body.name, parent_target_id, "file", owner_id):
        raise HTTPException(status_code=409, detail=f'A file named "{body.name}" already exists here.')

    # Determine initial file content
    initial_content = body.content if body.content is not None and body.content != "" else _get_starter_content(body.name)
    content_bytes = initial_content.encode("utf-8")

    # Create Mongo item
    item = await crud.create_item(
        name=body.name,
        item_type="file",
        user_id=owner_id,
        parent_id=parent_target_id,
        size=len(content_bytes),
        partition_id=parent.partition_id
    )

    # Upload to Backblaze B2
    prefix = await get_user_b2_prefix(owner_id)
    path = await get_item_path(item, owner_id)
    key = f"{prefix}/{path}"
    await upload_b2_file_async(key, content_bytes)

    return {
        "id": str(item.id),
        "name": item.name,
        "type": "file",
        "parentId": item.parent_id,
        "size": len(content_bytes),
        "createdAt": item.created_at.isoformat() if item.created_at else None,
        "content": initial_content,
    }


@router.post("/{folder_id}/folder", summary="Create new subfolder in workspace")
async def create_workspace_folder(
    folder_id: str,
    body: CreateWorkspaceFolderRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Create a new directory in the workspace tree."""
    parent_target_id = body.parentId or folder_id
    parent = await FileSystemItem.get(parent_target_id)
    if not parent or parent.type != "folder":
        raise HTTPException(status_code=400, detail="Target parent folder not found")

    await verify_write_access(parent, current_user)
    owner_id = parent.user_id if parent.user_id else str(current_user.id)

    # Check duplicate
    if await crud.check_duplicate_name(body.name, parent_target_id, "folder", owner_id):
        raise HTTPException(status_code=409, detail=f'A folder named "{body.name}" already exists here.')

    item = await crud.create_item(
        name=body.name,
        item_type="folder",
        user_id=owner_id,
        parent_id=parent_target_id,
        partition_id=parent.partition_id
    )

    # Create B2 folder placeholder
    prefix = await get_user_b2_prefix(owner_id)
    path = await get_item_path(item, owner_id)
    await create_b2_folder_async(f"{prefix}/{path}")

    return {
        "id": str(item.id),
        "name": item.name,
        "type": "folder",
        "parentId": item.parent_id,
        "createdAt": item.created_at.isoformat() if item.created_at else None,
        "children": []
    }


@router.patch("/item/{item_id}/rename", summary="Rename workspace file or folder")
async def rename_workspace_item(
    item_id: str,
    body: RenameWorkspaceItemRequest,
    current_user: User = Depends(get_current_user),
):
    """Rename a file or folder in workspace."""
    item = await FileSystemItem.get(item_id)
    if not item or item.is_deleted:
        raise HTTPException(status_code=404, detail="Item not found")

    await verify_write_access(item, current_user)
    owner_id = item.user_id if item.user_id else str(current_user.id)

    # Check duplicate
    if await crud.check_duplicate_name(body.name, item.parent_id, item.type, owner_id):
        raise HTTPException(status_code=409, detail=f'An item named "{body.name}" already exists here.')

    old_path = await get_item_path(item, owner_id)
    item.name = body.name
    await item.save()
    new_path = await get_item_path(item, owner_id)

    prefix = await get_user_b2_prefix(owner_id)
    if item.type == "file":
        await rename_b2_object_async(f"{prefix}/{old_path}", f"{prefix}/{new_path}")

    return {
        "id": str(item.id),
        "name": item.name,
        "type": item.type,
        "parentId": item.parent_id,
    }


@router.delete("/item/{item_id}", summary="Delete workspace file or folder")
async def delete_workspace_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
):
    """Permanently or soft delete an item from workspace."""
    item = await FileSystemItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    await verify_write_access(item, current_user)
    owner_id = item.user_id if item.user_id else str(current_user.id)

    # Soft delete item and its descendants
    async def soft_delete_recursive(target_id: str):
        target = await FileSystemItem.get(target_id)
        if target:
            target.is_deleted = True
            await target.save()
            children = await FileSystemItem.find(FileSystemItem.parent_id == target_id).to_list()
    await soft_delete_recursive(item_id)
    return {"success": True, "message": f"Item '{item.name}' deleted successfully."}


@router.post("/{folder_id}/search", summary="Search within active workspace tree only")
async def search_workspace(
    folder_id: str,
    body: WorkspaceSearchRequest,
    current_user: User = Depends(get_current_user),
):
    """Search filenames and contents strictly within the active workspace folder subtree."""
    root = await FileSystemItem.get(folder_id)
    if not root or root.is_deleted:
        raise HTTPException(status_code=404, detail="Workspace folder not found")

    owner_id = root.user_id if root.user_id else str(current_user.id)
    query_lower = body.query.lower().strip()

    all_items = await FileSystemItem.find(
        FileSystemItem.user_id == owner_id,
        FileSystemItem.is_deleted == False
    ).to_list()

    items_by_parent: Dict[Optional[str], List[FileSystemItem]] = {}
    for it in all_items:
        items_by_parent.setdefault(it.parent_id, []).append(it)

    # Collect all item IDs belonging to this workspace subtree
    descendant_items: List[FileSystemItem] = []
    def collect(pid: str):
        for child in items_by_parent.get(pid, []):
            descendant_items.append(child)
            if child.type == "folder":
                collect(str(child.id))

    collect(folder_id)

    matches = []
    for item in descendant_items:
        if query_lower in item.name.lower():
            matches.append({
                "id": str(item.id),
                "name": item.name,
                "type": item.type,
                "parentId": item.parent_id,
                "matchType": "filename",
            })

    return {"query": body.query, "matches": matches}

