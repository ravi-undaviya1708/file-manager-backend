"""API router for file/folder operations (MongoDB/Beanie).

All routes are prefixed with /api to match the frontend's axios calls.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form

from app import crud
from app.schemas import (
    CreateFolderRequest,
    ErrorResponse,
    FileSystemItemResponse,
    MessageResponse,
    MoveItemRequest,
    RenameItemRequest,
)

router = APIRouter(prefix="/api", tags=["File Manager"])


# ── Helper ────────────────────────────────────────────────────────────────────


def _to_response(item) -> FileSystemItemResponse:
    """Convert a Beanie FileSystemItem document to a response schema."""
    return FileSystemItemResponse(
        id=str(item.id),
        name=item.name,
        type=item.type,
        parentId=item.parent_id,
        createdAt=item.created_at.isoformat() if item.created_at else "",
        size=item.size,
        starred=item.starred,
        isDeleted=item.is_deleted,
    )


# ── List All Items ────────────────────────────────────────────────────────────


@router.get(
    "/folders",
    response_model=List[FileSystemItemResponse],
    summary="List all file system items",
)
async def list_items():
    """Return every file and folder (the frontend handles tree-building client-side)."""
    items = await crud.get_all_items()
    return [_to_response(item) for item in items]


# ── Create Folder ─────────────────────────────────────────────────────────────


@router.post(
    "/folders",
    response_model=FileSystemItemResponse,
    status_code=201,
    summary="Create a new folder",
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def create_folder(body: CreateFolderRequest):
    """Create a new folder in the given parent directory."""
    if body.type != "folder":
        raise HTTPException(
            status_code=400,
            detail={"error": "Only folder creation is supported via this endpoint."},
        )

    # Validate parent exists (if specified)
    if body.parentId:
        parent = await crud.get_item_by_id(body.parentId)
        if not parent:
            raise HTTPException(
                status_code=400,
                detail={"error": f"Parent folder '{body.parentId}' not found."},
            )

    # Check duplicate name
    if await crud.check_duplicate_name(body.name, body.parentId, "folder"):
        raise HTTPException(
            status_code=409,
            detail={"error": f'A folder named "{body.name}" already exists here.'},
        )

    item = await crud.create_item(body.name, "folder", body.parentId)
    return _to_response(item)


# ── Get Single Item ──────────────────────────────────────────────────────────


@router.get(
    "/folders/{item_id}",
    response_model=FileSystemItemResponse,
    summary="Get a single item by ID",
    responses={404: {"model": ErrorResponse}},
)
async def get_item(item_id: str):
    """Retrieve a specific file or folder by its ID."""
    item = await crud.get_item_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})
    return _to_response(item)


# ── Rename ────────────────────────────────────────────────────────────────────


@router.patch(
    "/folders/{item_id}/rename",
    response_model=FileSystemItemResponse,
    summary="Rename a file or folder",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def rename_item(item_id: str, body: RenameItemRequest):
    """Rename an existing file or folder."""
    item = await crud.get_item_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    # Check duplicate name in same parent
    if await crud.check_duplicate_name(
        body.name, item.parent_id, item.type, exclude_id=item_id
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "error": f'An item named "{body.name}" already exists in this location.'
            },
        )

    updated = await crud.rename_item(item, body.name)
    return _to_response(updated)


# ── Move ──────────────────────────────────────────────────────────────────────


@router.patch(
    "/folders/{item_id}/move",
    response_model=FileSystemItemResponse,
    summary="Move item to a different folder",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def move_item(item_id: str, body: MoveItemRequest):
    """Move a file or folder to a new parent directory."""
    item = await crud.get_item_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    if item_id == body.targetParentId:
        raise HTTPException(
            status_code=400,
            detail={"error": "Cannot move an item into itself."},
        )

    # Validate target parent exists
    if body.targetParentId:
        target = await crud.get_item_by_id(body.targetParentId)
        if not target:
            raise HTTPException(
                status_code=400,
                detail={"error": "Target folder not found."},
            )
        if target.type != "folder":
            raise HTTPException(
                status_code=400,
                detail={"error": "Target must be a folder."},
            )

    updated = await crud.move_item(item, body.targetParentId)
    return _to_response(updated)


# ── Star/Unstar ───────────────────────────────────────────────────────────────


@router.patch(
    "/folders/{item_id}/star",
    response_model=FileSystemItemResponse,
    summary="Toggle starred status",
    responses={404: {"model": ErrorResponse}},
)
async def toggle_star(item_id: str):
    """Toggle the starred flag on a file or folder."""
    item = await crud.get_item_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    updated = await crud.toggle_star(item)
    return _to_response(updated)


# ── Delete (soft or hard) ────────────────────────────────────────────────────


@router.delete(
    "/folders/{item_id}",
    response_model=MessageResponse,
    summary="Delete an item (soft-delete or permanent)",
    responses={404: {"model": ErrorResponse}},
)
async def delete_item(item_id: str, permanent: bool = False):
    """
    Soft-delete an item (move to bin) by default.
    Pass `?permanent=true` to permanently delete an item already in the bin.
    """
    item = await crud.get_item_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    if permanent or item.is_deleted:
        deleted_ids = await crud.hard_delete_item(item_id)
        return MessageResponse(
            message=f"Permanently deleted {len(deleted_ids)} item(s)."
        )
    else:
        deleted_ids = await crud.soft_delete_item(item_id)
        return MessageResponse(
            message=f"Moved {len(deleted_ids)} item(s) to bin."
        )


# ── Restore ───────────────────────────────────────────────────────────────────


@router.patch(
    "/folders/{item_id}/restore",
    response_model=MessageResponse,
    summary="Restore a soft-deleted item from bin",
    responses={404: {"model": ErrorResponse}},
)
async def restore_item(item_id: str):
    """Restore a soft-deleted item and all its children from the bin."""
    item = await crud.get_item_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    if not item.is_deleted:
        raise HTTPException(
            status_code=400,
            detail={"error": "Item is not in the bin."},
        )

    restored_ids = await crud.restore_item(item_id)
    return MessageResponse(message=f"Restored {len(restored_ids)} item(s).")


# ── Duplicate ─────────────────────────────────────────────────────────────────


@router.post(
    "/folders/{item_id}/duplicate",
    response_model=FileSystemItemResponse,
    status_code=201,
    summary="Duplicate an item",
    responses={404: {"model": ErrorResponse}},
)
async def duplicate_item(item_id: str, targetParentId: Optional[str] = None):
    """Create a copy of a file or folder with a 'copy' suffix."""
    item = await crud.get_item_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    new_item = await crud.duplicate_item(item, targetParentId)
    return _to_response(new_item)


# ── File Upload ───────────────────────────────────────────────────────────────


@router.post(
    "/files/upload",
    response_model=FileSystemItemResponse,
    status_code=201,
    summary="Upload a file",
    responses={400: {"model": ErrorResponse}},
)
async def upload_file(
    file: UploadFile = File(...),
    parentId: Optional[str] = Form(None),
):
    """Upload a file to the specified parent folder."""
    if not file.filename:
        raise HTTPException(
            status_code=400, detail={"error": "Filename is required."}
        )

    # Validate parent exists
    if parentId:
        parent = await crud.get_item_by_id(parentId)
        if not parent:
            raise HTTPException(
                status_code=400,
                detail={"error": f"Parent folder '{parentId}' not found."},
            )

    # Read file to get size (in production you'd save to GridFS/cloud storage)
    content = await file.read()
    file_size = len(content)

    item = await crud.create_item(
        file.filename, "file", parentId, size=file_size
    )
    return _to_response(item)
