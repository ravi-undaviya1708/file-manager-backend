"""API router for file/folder operations (MongoDB/Beanie) secured with JWT authentication.

All routes are prefixed with /api to match the frontend's axios calls.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends

from typing import List, Optional


from app import crud
from app.auth import get_current_user
from app.models import User
from app.schemas import (
    CreateFolderRequest,
    ErrorResponse,
    FileSystemItemResponse,
    MessageResponse,
    MoveItemRequest,
    RenameItemRequest,
)
import logging
import tempfile
import os
import shutil
import asyncio
import anyio

logger = logging.getLogger(__name__)

# Dictionary to hold lock objects for each upload session
upload_locks = {}
upload_locks_mutex = asyncio.Lock()

async def get_upload_lock(upload_id: str) -> asyncio.Lock:
    async with upload_locks_mutex:
        if upload_id not in upload_locks:
            upload_locks[upload_id] = asyncio.Lock()
        return upload_locks[upload_id]

async def clean_upload_lock(upload_id: str):
    async with upload_locks_mutex:
        if upload_id in upload_locks:
            del upload_locks[upload_id]

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
async def list_items(current_user: User = Depends(get_current_user)):
    """Return every file and folder for the authenticated user."""
    from app.b2 import sync_b2_to_mongodb
    await sync_b2_to_mongodb(str(current_user.id))
    items = await crud.get_all_items(str(current_user.id))
    return [_to_response(item) for item in items]


# ── Create Folder ─────────────────────────────────────────────────────────────


@router.post(
    "/folders",
    response_model=FileSystemItemResponse,
    status_code=201,
    summary="Create a new folder",
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def create_folder(
    body: CreateFolderRequest,
    current_user: User = Depends(get_current_user),
):
    """Create a new folder in the given parent directory."""
    if body.type != "folder":
        raise HTTPException(
            status_code=400,
            detail={"error": "Only folder creation is supported via this endpoint."},
        )

    # Validate parent exists (if specified) and belongs to user
    if body.parentId:
        parent = await crud.get_item_by_id(body.parentId, str(current_user.id))
        if not parent:
            raise HTTPException(
                status_code=400,
                detail={"error": f"Parent folder '{body.parentId}' not found."},
            )

    # Check duplicate name
    if await crud.check_duplicate_name(body.name, body.parentId, "folder", str(current_user.id)):
        raise HTTPException(
            status_code=409,
            detail={"error": f'A folder named "{body.name}" already exists here.'},
        )

    item = await crud.create_item(body.name, "folder", str(current_user.id), body.parentId)

    # Sync folder creation to Backblaze B2
    from app.b2 import create_b2_folder, get_item_path, get_user_b2_prefix
    path = await get_item_path(item, str(current_user.id))
    prefix = await get_user_b2_prefix(str(current_user.id))
    create_b2_folder(f"{prefix}/{path}")

    return _to_response(item)


# ── Get Single Item ──────────────────────────────────────────────────────────


@router.get(
    "/folders/{item_id}",
    response_model=FileSystemItemResponse,
    summary="Get a single item by ID",
    responses={404: {"model": ErrorResponse}},
)
async def get_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
):
    """Retrieve a specific file or folder by its ID."""
    item = await crud.get_item_by_id(item_id, str(current_user.id))
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
async def rename_item(
    item_id: str,
    body: RenameItemRequest,
    current_user: User = Depends(get_current_user),
):
    """Rename an existing file or folder."""
    item = await crud.get_item_by_id(item_id, str(current_user.id))
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    # Check duplicate name in same parent
    if await crud.check_duplicate_name(
        body.name, item.parent_id, item.type, str(current_user.id), exclude_id=item_id
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "error": f'An item named "{body.name}" already exists in this location.'
            },
        )

    old_name = item.name
    updated = await crud.rename_item(item, body.name)

    # Sync renaming to Backblaze B2
    from app.b2 import handle_b2_rename
    await handle_b2_rename(updated, str(current_user.id), old_name)

    return _to_response(updated)


# ── Move ──────────────────────────────────────────────────────────────────────


@router.patch(
    "/folders/{item_id}/move",
    response_model=FileSystemItemResponse,
    summary="Move item to a different folder",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def move_item(
    item_id: str,
    body: MoveItemRequest,
    current_user: User = Depends(get_current_user),
):
    """Move a file or folder to a new parent directory."""
    item = await crud.get_item_by_id(item_id, str(current_user.id))
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    if item_id == body.targetParentId:
        raise HTTPException(
            status_code=400,
            detail={"error": "Cannot move an item into itself."},
        )

    # Validate target parent exists and belongs to user
    if body.targetParentId:
        target = await crud.get_item_by_id(body.targetParentId, str(current_user.id))
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

    old_parent_id = item.parent_id
    updated = await crud.move_item(item, body.targetParentId)

    # Sync moving to Backblaze B2
    from app.b2 import handle_b2_move
    await handle_b2_move(updated, str(current_user.id), old_parent_id)

    return _to_response(updated)


# ── Star/Unstar ───────────────────────────────────────────────────────────────


@router.patch(
    "/folders/{item_id}/star",
    response_model=FileSystemItemResponse,
    summary="Toggle starred status",
    responses={404: {"model": ErrorResponse}},
)
async def toggle_star(
    item_id: str,
    current_user: User = Depends(get_current_user),
):
    """Toggle the starred flag on a file or folder."""
    item = await crud.get_item_by_id(item_id, str(current_user.id))
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
async def delete_item(
    item_id: str,
    permanent: bool = False,
    current_user: User = Depends(get_current_user),
):
    """Soft-delete an item (move to bin) by default.

    Pass `?permanent=true` to permanently delete an item already in the bin.
    """
    item = await crud.get_item_by_id(item_id, str(current_user.id))
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    if permanent or item.is_deleted:
        # Sync hard-delete to Backblaze B2 (call before DB hard-deletion to traverse parent hierarchy)
        from app.b2 import handle_b2_delete
        await handle_b2_delete(item, str(current_user.id))

        deleted_ids = await crud.hard_delete_item(item_id, str(current_user.id))
        return MessageResponse(
            message=f"Permanently deleted {len(deleted_ids)} item(s)."
        )
    else:
        # Keep files in Backblaze B2 on soft-delete (do not call handle_b2_delete here).
        deleted_ids = await crud.soft_delete_item(item_id, str(current_user.id))
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
async def restore_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
):
    """Restore a soft-deleted item and all its children from the bin."""
    item = await crud.get_item_by_id(item_id, str(current_user.id))
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    if not item.is_deleted:
        raise HTTPException(
            status_code=400,
            detail={"error": "Item is not in the bin."},
        )

    restored_ids = await crud.restore_item(item_id, str(current_user.id))

    return MessageResponse(message=f"Restored {len(restored_ids)} item(s).")


# ── Duplicate ─────────────────────────────────────────────────────────────────


@router.post(
    "/folders/{item_id}/duplicate",
    response_model=FileSystemItemResponse,
    status_code=201,
    summary="Duplicate an item",
    responses={404: {"model": ErrorResponse}},
)
async def duplicate_item(
    item_id: str,
    targetParentId: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Create a copy of a file or folder with a 'copy' suffix."""
    item = await crud.get_item_by_id(item_id, str(current_user.id))
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    if targetParentId is not None:
        use_target = True
        actual_parent = None if targetParentId in ("root", "null") else targetParentId
    else:
        use_target = False
        actual_parent = None

    new_item = await crud.duplicate_item(item, str(current_user.id), actual_parent, use_target)
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
    current_user: User = Depends(get_current_user),
):
    """Upload a file to the specified parent folder."""
    if not file.filename:
        raise HTTPException(
            status_code=400, detail={"error": "Filename is required."}
        )

    # Validate parent exists and belongs to user
    if parentId:
        parent = await crud.get_item_by_id(parentId, str(current_user.id))
        if not parent:
            raise HTTPException(
                status_code=400,
                detail={"error": f"Parent folder '{parentId}' not found."},
            )

    # Read file to get size
    content = await file.read()
    file_size = len(content)

    item = await crud.create_item(
        file.filename, "file", str(current_user.id), parentId, size=file_size
    )

    # Sync file upload to Backblaze B2
    from app.b2 import upload_b2_file, get_item_path, get_user_b2_prefix
    path = await get_item_path(item, str(current_user.id))
    prefix = await get_user_b2_prefix(str(current_user.id))
    upload_b2_file(f"{prefix}/{path}", content)

    return _to_response(item)


@router.post(
    "/files/upload/chunk",
    status_code=201,
    summary="Upload a file chunk",
)
async def upload_chunk(
    file: UploadFile = File(...),
    uploadId: str = Form(...),
    chunkIndex: int = Form(...),
    totalChunks: int = Form(...),
    filename: str = Form(...),
    parentId: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
):
    """Upload a file chunk to the specified parent folder.

    Merges when all chunks are uploaded, then registers in DB and uploads to B2.
    """
    if not filename:
        raise HTTPException(
            status_code=400, detail={"error": "Filename is required."}
        )

    # Validate parent exists and belongs to user
    if parentId:
        parent = await crud.get_item_by_id(parentId, str(current_user.id))
        if not parent:
            raise HTTPException(
                status_code=400,
                detail={"error": f"Parent folder '{parentId}' not found."},
            )

    # Validate name duplicate on the first chunk to prevent wasting time on duplicate uploads
    if chunkIndex == 0:
        if await crud.check_duplicate_name(filename, parentId, "file", str(current_user.id)):
            raise HTTPException(
                status_code=409,
                detail={"error": f'A file named "{filename}" already exists in this location.'},
            )

    # Path to temp directory for this upload
    temp_dir = os.path.join(tempfile.gettempdir(), f"stitchdrive_upload_{uploadId}")
    os.makedirs(temp_dir, exist_ok=True)
    chunk_path = os.path.join(temp_dir, f"chunk_{chunkIndex}")

    # Write chunk content to disk
    try:
        content = await file.read()
        async with await anyio.open_file(chunk_path, "wb") as f:
            await f.write(content)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"error": f"Failed to save chunk to disk: {str(e)}"}
        )

    # Acquire lock for this upload to check status and merge safely
    lock = await get_upload_lock(uploadId)
    async with lock:
        # Check if all chunks have been uploaded
        all_chunks_exist = True
        for i in range(totalChunks):
            if not os.path.exists(os.path.join(temp_dir, f"chunk_{i}")):
                all_chunks_exist = False
                break

        if not all_chunks_exist:
            # Not complete yet, return status
            return {
                "status": "chunk_uploaded",
                "chunkIndex": chunkIndex,
                "totalChunks": totalChunks,
            }

        # Double check if we already merged (e.g. final file exists or db entry already exists)
        # Check duplicate name again right before merging
        if await crud.check_duplicate_name(filename, parentId, "file", str(current_user.id)):
            # Cleanup temp files
            shutil.rmtree(temp_dir, ignore_errors=True)
            await clean_upload_lock(uploadId)
            raise HTTPException(
                status_code=409,
                detail={"error": f'A file named "{filename}" already exists in this location.'},
            )

        merged_file_path = os.path.join(temp_dir, "merged_file")
        try:
            # Merge all chunks sequentially
            async with await anyio.open_file(merged_file_path, "wb") as outfile:
                for i in range(totalChunks):
                    curr_chunk_path = os.path.join(temp_dir, f"chunk_{i}")
                    async with await anyio.open_file(curr_chunk_path, "rb") as infile:
                        while True:
                            data = await infile.read(128 * 1024)  # 128KB buffer
                            if not data:
                                break
                            await outfile.write(data)
            
            # Calculate final file size
            file_size = os.path.getsize(merged_file_path)

            # Create MongoDB entry
            item = await crud.create_item(
                filename, "file", str(current_user.id), parentId, size=file_size
            )

            # Sync file upload to Backblaze B2 — run in thread pool to avoid blocking the event loop
            from app.b2 import upload_b2_file_from_path, get_item_path, get_user_b2_prefix
            import functools
            path = await get_item_path(item, str(current_user.id))
            prefix = await get_user_b2_prefix(str(current_user.id))
            b2_key = f"{prefix}/{path}"

            # upload_b2_file_from_path is synchronous (boto3); offload to executor so
            # other concurrent uploads / requests are not blocked while it runs.
            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(
                None,
                functools.partial(upload_b2_file_from_path, merged_file_path, b2_key)
            )
            if not success:
                logger.error(f"B2 upload from path failed for {filename}")

            return _to_response(item)

        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail={"error": f"Failed during merge or upload: {str(e)}"}
            )
        finally:
            # Clean up temp files
            shutil.rmtree(temp_dir, ignore_errors=True)
            await clean_upload_lock(uploadId)
