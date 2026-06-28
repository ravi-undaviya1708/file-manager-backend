"""API router for file/folder operations (MongoDB/Beanie) secured with JWT authentication.

All routes are prefixed with /api to match the frontend's axios calls.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends, Request

from typing import List, Optional


from app import crud
from app.auth import get_current_user
from app.models import User, FileSystemItem
from app.schemas import (
    CreateFolderRequest,
    ErrorResponse,
    FileSystemItemResponse,
    MessageResponse,
    MoveItemRequest,
    RenameItemRequest,
    LockFolderRequest,
    ItemShareResponse,
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


def _to_response(item, user_email_map: Optional[dict] = None) -> FileSystemItemResponse:
    """Convert a Beanie FileSystemItem document to a response schema."""
    shares_list = getattr(item, "shares", []) or []
    from app.schemas import ItemShareResponse
    shares_response = [
        ItemShareResponse(userId=s.user_id, email=s.email, permission=s.permission)
        for s in shares_list
    ]
    
    owner_email = None
    if item.user_id and user_email_map:
        owner_email = user_email_map.get(str(item.user_id))

    return FileSystemItemResponse(
        id=str(item.id),
        name=item.name,
        type=item.type,
        parentId=item.parent_id,
        createdAt=item.created_at.isoformat() if item.created_at else "",
        size=item.size,
        starred=item.starred,
        isDeleted=item.is_deleted,
        isLocked=getattr(item, "is_locked", False),
        isHidden=getattr(item, "is_hidden", False),
        shares=shares_response,
        ownerId=item.user_id,
        ownerEmail=owner_email,
        partitionId=getattr(item, "partition_id", None),
    )


async def _to_response_async(item) -> FileSystemItemResponse:
    """Convert FileSystemItem to response schema asynchronously, resolving owner email."""
    if not item:
        return None
    user_email_map = {}
    if item.user_id:
        user = await User.get(item.user_id)
        if user:
            user_email_map[str(item.user_id)] = user.email
    return _to_response(item, user_email_map)


# ── List All Items ────────────────────────────────────────────────────────────


@router.get(
    "/folders",
    response_model=List[FileSystemItemResponse],
    summary="List all file system items",
)
async def list_items(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Return every file and folder accessible to the authenticated user, filtering out locked sub-items."""
    from app.b2 import sync_b2_to_mongodb
    await sync_b2_to_mongodb(str(current_user.id))
    # Fetch owned and shared items recursively
    items = await crud.get_accessible_items(str(current_user.id), current_user.email)

    from app.security_helpers import get_unlocked_passwords, is_lineage_blocked
    unlocked_passwords = get_unlocked_passwords(request)
    items_by_id = {str(item.id): item for item in items}

    filtered_items = []
    for item in items:
        # Pass the item's actual owner user_id to is_lineage_blocked since they determine locking bounds
        owner_id = item.user_id if item.user_id else str(current_user.id)
        if await is_lineage_blocked(item, owner_id, unlocked_passwords, items_by_id):
            continue
        filtered_items.append(item)

    # Pre-populate user email map for fast response serialization
    user_ids = {item.user_id for item in filtered_items if item.user_id}
    user_email_map = {}
    if user_ids:
        from beanie import PydanticObjectId
        # Find users by ObjectId
        object_ids = [PydanticObjectId(uid) for uid in user_ids if len(uid) == 24]
        if object_ids:
            users_obj = await User.find({"_id": {"$in": object_ids}}).to_list()
            for u in users_obj:
                user_email_map[str(u.id)] = u.email
        # Find users by string ID
        str_ids = [uid for uid in user_ids if len(uid) != 24]
        if str_ids:
            users_str = await User.find({"_id": {"$in": str_ids}}).to_list()
            for u in users_str:
                user_email_map[str(u.id)] = u.email

    return [_to_response(item, user_email_map) for item in filtered_items]



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
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Create a new folder in the given parent directory."""
    if body.type != "folder":
        raise HTTPException(
            status_code=400,
            detail={"error": "Only folder creation is supported via this endpoint."},
        )

    owner_id = str(current_user.id)
    partition_id = None
    # Validate parent exists (if specified)
    if body.parentId:
        parent = await FileSystemItem.get(body.parentId)
        if not parent:
            raise HTTPException(
                status_code=400,
                detail={"error": f"Parent folder '{body.parentId}' not found."},
            )

        # Verify write access on parent folder
        from app.security_helpers import verify_write_access
        await verify_write_access(parent, current_user)

        owner_id = parent.user_id
        partition_id = parent.partition_id

        from app.security_helpers import get_unlocked_passwords, is_access_blocked
        unlocked_passwords = get_unlocked_passwords(request)
        if await is_access_blocked(parent, owner_id, unlocked_passwords):
            raise HTTPException(
                status_code=403,
                detail={"error": "Parent folder is locked."},
            )
    else:
        partition_id = body.partitionId

    # Check duplicate name
    if await crud.check_duplicate_name(body.name, body.parentId, "folder", owner_id):
        raise HTTPException(
            status_code=409,
            detail={"error": f'A folder named "{body.name}" already exists here.'},
        )

    item = await crud.create_item(body.name, "folder", owner_id, body.parentId, partition_id=partition_id)

    # Sync folder creation to Backblaze B2 using owner_id
    from app.b2 import create_b2_folder, get_item_path, get_user_b2_prefix
    path = await get_item_path(item, owner_id)
    prefix = await get_user_b2_prefix(owner_id)
    create_b2_folder(f"{prefix}/{path}")

    return await _to_response_async(item)


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
    item = await FileSystemItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})
        
    from app.security_helpers import verify_read_access
    await verify_read_access(item, current_user)
    
    return await _to_response_async(item)


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
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Rename an existing file or folder."""
    item = await FileSystemItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    from app.security_helpers import verify_write_access
    await verify_write_access(item, current_user)

    owner_id = item.user_id if item.user_id else str(current_user.id)

    from app.security_helpers import get_unlocked_passwords, is_access_blocked
    unlocked_passwords = get_unlocked_passwords(request)
    if await is_access_blocked(item, owner_id, unlocked_passwords):
        raise HTTPException(
            status_code=403,
            detail={"error": "Item is locked."},
        )

    # Check duplicate name in same parent
    if await crud.check_duplicate_name(
        body.name, item.parent_id, item.type, owner_id, exclude_id=item_id
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "error": f'An item named "{body.name}" already exists in this location.'
            },
        )

    old_name = item.name
    updated = await crud.rename_item(item, body.name)

    # Sync renaming to Backblaze B2 using owner_id
    from app.b2 import handle_b2_rename
    await handle_b2_rename(updated, owner_id, old_name)

    return await _to_response_async(updated)


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
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Move a file or folder to a new parent directory."""
    item = await FileSystemItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    if item_id == body.targetParentId:
        raise HTTPException(
            status_code=400,
            detail={"error": "Cannot move an item into itself."},
        )

    from app.security_helpers import verify_write_access
    await verify_write_access(item, current_user)

    owner_id = item.user_id if item.user_id else str(current_user.id)

    from app.security_helpers import get_unlocked_passwords, is_access_blocked
    unlocked_passwords = get_unlocked_passwords(request)
    if await is_access_blocked(item, owner_id, unlocked_passwords):
        raise HTTPException(
            status_code=403,
            detail={"error": "Item is locked."},
        )

    # Validate target parent exists
    if body.targetParentId:
        target = await FileSystemItem.get(body.targetParentId)
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

        # Verify target parent is owned by the SAME user as the item
        target_owner_id = target.user_id if target.user_id else str(current_user.id)
        if target_owner_id != owner_id:
            raise HTTPException(
                status_code=400,
                detail={"error": "Cannot move items across different shared folders owned by different users."}
            )

        await verify_write_access(target, current_user)

        if await is_access_blocked(target, owner_id, unlocked_passwords):
            raise HTTPException(
                status_code=403,
                detail={"error": "Target parent folder is locked."},
            )

    old_parent_id = item.parent_id
    updated = await crud.move_item(item, body.targetParentId)

    # Sync moving to Backblaze B2 using owner_id
    from app.b2 import handle_b2_move
    await handle_b2_move(updated, owner_id, old_parent_id)

    return await _to_response_async(updated)


# ── Star/Unstar ───────────────────────────────────────────────────────────────


@router.patch(
    "/folders/{item_id}/star",
    response_model=FileSystemItemResponse,
    summary="Toggle starred status",
    responses={404: {"model": ErrorResponse}},
)
async def toggle_star(
    item_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Toggle the starred flag on a file or folder."""
    item = await FileSystemItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    from app.security_helpers import verify_read_access
    await verify_read_access(item, current_user)

    owner_id = item.user_id if item.user_id else str(current_user.id)

    from app.security_helpers import get_unlocked_passwords, is_access_blocked
    unlocked_passwords = get_unlocked_passwords(request)
    if await is_access_blocked(item, owner_id, unlocked_passwords):
        raise HTTPException(
            status_code=403,
            detail={"error": "Item is locked."},
        )

    updated = await crud.toggle_star(item)
    return await _to_response_async(updated)


# ── Delete (soft or hard) ────────────────────────────────────────────────────


@router.delete(
    "/folders/{item_id}",
    response_model=MessageResponse,
    summary="Delete an item (soft-delete or permanent)",
    responses={404: {"model": ErrorResponse}},
)
async def delete_item(
    item_id: str,
    request: Request,
    permanent: bool = False,
    current_user: User = Depends(get_current_user),
):
    """Soft-delete an item (move to bin) by default.

    Pass `?permanent=true` to permanently delete an item already in the bin.
    """
    item = await FileSystemItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    from app.security_helpers import verify_write_access
    await verify_write_access(item, current_user)

    owner_id = item.user_id if item.user_id else str(current_user.id)

    from app.security_helpers import get_unlocked_passwords, is_access_blocked
    unlocked_passwords = get_unlocked_passwords(request)
    if await is_access_blocked(item, owner_id, unlocked_passwords):
        raise HTTPException(
            status_code=403,
            detail={"error": "Item is locked."},
        )

    if permanent or item.is_deleted:
        # Sync hard-delete to Backblaze B2 (call before DB hard-deletion to traverse parent hierarchy)
        from app.b2 import handle_b2_delete
        await handle_b2_delete(item, owner_id)

        deleted_ids = await crud.hard_delete_item(item_id, owner_id)
        return MessageResponse(
            message=f"Permanently deleted {len(deleted_ids)} item(s)."
        )
    else:
        # Keep files in Backblaze B2 on soft-delete (do not call handle_b2_delete here).
        deleted_ids = await crud.soft_delete_item(item_id, owner_id)
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
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Restore a soft-deleted item and all its children from the bin."""
    item = await FileSystemItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    from app.security_helpers import verify_write_access
    await verify_write_access(item, current_user)

    owner_id = item.user_id if item.user_id else str(current_user.id)

    from app.security_helpers import get_unlocked_passwords, is_access_blocked
    unlocked_passwords = get_unlocked_passwords(request)
    if await is_access_blocked(item, owner_id, unlocked_passwords):
        raise HTTPException(
            status_code=403,
            detail={"error": "Item is locked."},
        )

    if not item.is_deleted:
        raise HTTPException(
            status_code=400,
            detail={"error": "Item is not in the bin."},
        )

    restored_ids = await crud.restore_item(item_id, owner_id)

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
    request: Request,
    targetParentId: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Create a copy of a file or folder with a 'copy' suffix."""
    item = await FileSystemItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    from app.security_helpers import verify_read_access, verify_write_access
    await verify_read_access(item, current_user)

    owner_id = item.user_id if item.user_id else str(current_user.id)

    from app.security_helpers import get_unlocked_passwords, is_access_blocked
    unlocked_passwords = get_unlocked_passwords(request)
    if await is_access_blocked(item, owner_id, unlocked_passwords):
        raise HTTPException(
            status_code=403,
            detail={"error": "Item is locked."},
        )

    # Determine parent folder where copy is created and check write access
    if targetParentId is not None:
        use_target = True
        actual_parent = None if targetParentId in ("root", "null") else targetParentId
        if actual_parent:
            parent_item = await FileSystemItem.get(actual_parent)
            if not parent_item:
                raise HTTPException(status_code=404, detail={"error": "Target parent folder not found."})
                
            await verify_write_access(parent_item, current_user)

            # Ensure we do not mix owners
            parent_owner_id = parent_item.user_id if parent_item.user_id else str(current_user.id)
            if parent_owner_id != owner_id:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "Cannot duplicate items across different shared folders owned by different users."}
                )

            if await is_access_blocked(parent_item, owner_id, unlocked_passwords):
                raise HTTPException(
                    status_code=403,
                    detail={"error": "Target parent folder is locked."},
                )
    else:
        use_target = False
        actual_parent = None
        if item.parent_id:
            parent_item = await FileSystemItem.get(item.parent_id)
            if parent_item:
                await verify_write_access(parent_item, current_user)
                if await is_access_blocked(parent_item, owner_id, unlocked_passwords):
                    raise HTTPException(
                        status_code=403,
                        detail={"error": "Parent folder is locked."},
                    )

    new_item = await crud.duplicate_item(item, owner_id, actual_parent, use_target)
    return await _to_response_async(new_item)


@router.get(
    "/files/{file_id}/view",
    summary="Get raw file contents or stream file for viewing/previews",
)
async def view_file(
    file_id: str,
    request: Request,
    token: Optional[str] = None,
    passwords: Optional[str] = None,
):
    """Retrieve raw file content from Backblaze B2.
    
    Supports authenticating via standard Authorization header or token query parameter.
    """
    from fastapi import Request
    
    jwt_token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        jwt_token = auth_header.split(" ")[1]
    elif token:
        jwt_token = token
        
    if not jwt_token:
        raise HTTPException(status_code=401, detail={"error": "Not authenticated"})
        
    from app.auth import decode_access_token
    payload = decode_access_token(jwt_token)
    if not payload:
        raise HTTPException(status_code=401, detail={"error": "Invalid token"})
        
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail={"error": "Invalid token payload"})

    user = await User.get(user_id)
    if not user:
        raise HTTPException(status_code=401, detail={"error": "User not found"})
        
    item = await FileSystemItem.get(file_id)
    if not item or item.type != "file":
        raise HTTPException(status_code=404, detail={"error": "File not found"})

    from app.security_helpers import verify_read_access
    await verify_read_access(item, user)
    owner_id = item.user_id if item.user_id else user_id
        
    from app.security_helpers import get_unlocked_passwords, is_access_blocked
    unlocked_passwords = get_unlocked_passwords(request, query_passwords=passwords)
    if await is_access_blocked(item, owner_id, unlocked_passwords):
        raise HTTPException(status_code=403, detail={"error": "Access to locked folder content denied."})
        
    from app.b2 import get_user_b2_prefix, get_item_path, get_b2_client
    prefix = await get_user_b2_prefix(owner_id)
    path = await get_item_path(item, owner_id)
    key = f"{prefix}/{path}"
    
    import mimetypes
    content_type, _ = mimetypes.guess_type(item.name)
    if not content_type:
        content_type = "application/octet-stream"
        
    s3 = get_b2_client()
    from app.config import get_settings
    settings = get_settings()
    
    try:
        response = s3.get_object(Bucket=settings.B2_BUCKET, Key=key)
        
        from fastapi.responses import StreamingResponse
        def iterfile():
            for chunk in response["Body"].iter_chunks(chunk_size=128 * 1024):
                yield chunk
                
        return StreamingResponse(
            iterfile(),
            media_type=content_type,
            headers={
                "Content-Disposition": f"inline; filename={item.name}"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail={"error": f"File content not found: {str(e)}"})


# ── File Upload ───────────────────────────────────────────────────────────────


@router.post(
    "/files/upload",
    response_model=FileSystemItemResponse,
    status_code=201,
    summary="Upload a file",
    responses={400: {"model": ErrorResponse}},
)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    parentId: Optional[str] = Form(None),
    partitionId: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
):
    """Upload a file to the specified parent folder."""
    if not file.filename:
        raise HTTPException(
            status_code=400, detail={"error": "Filename is required."}
        )

    owner_id = str(current_user.id)
    partition_id = None
    # Validate parent exists and verify permissions
    if parentId:
        parent = await FileSystemItem.get(parentId)
        if not parent:
            raise HTTPException(
                status_code=400,
                detail={"error": f"Parent folder '{parentId}' not found."},
            )

        from app.security_helpers import verify_write_access
        await verify_write_access(parent, current_user)

        owner_id = parent.user_id
        partition_id = parent.partition_id

        from app.security_helpers import get_unlocked_passwords, is_access_blocked
        unlocked_passwords = get_unlocked_passwords(request)
        if await is_access_blocked(parent, owner_id, unlocked_passwords):
            raise HTTPException(
                status_code=403,
                detail={"error": "Access to parent folder is locked."}
            )
    else:
        partition_id = partitionId

    # Read file to get size
    content = await file.read()
    file_size = len(content)

    # Fetch owner to check their specific storage limit
    owner_user = await User.get(owner_id)
    limit_bytes = owner_user.storage_limit_bytes if owner_user else current_user.storage_limit_bytes

    # Check overall user storage limit
    current_used = await crud.get_user_storage_size(owner_id)
    if current_used + file_size > limit_bytes:
        raise HTTPException(
            status_code=400,
            detail={"error": f"Storage limit exceeded. Cannot upload file (Size: {file_size} bytes, Used: {current_used} bytes, Limit: {limit_bytes} bytes)."}
        )

    # Check partition storage limit if target is a partition
    if partition_id:
        partition = await StoragePartition.get(partition_id)
        if partition:
            files_in_part = await FileSystemItem.find(
                FileSystemItem.user_id == owner_id,
                FileSystemItem.partition_id == partition_id,
                FileSystemItem.type == "file",
                FileSystemItem.is_deleted == False
            ).to_list()
            partition_used = sum(f.size or 0 for f in files_in_part)
            if partition_used + file_size > partition.allocated_size_bytes:
                raise HTTPException(
                    status_code=400,
                    detail={"error": f"Partition '{partition.name}' storage limit exceeded. Cannot upload file (Size: {file_size} bytes, Used: {partition_used} bytes, Allocation: {partition.allocated_size_bytes} bytes)."}
                )

    item = await crud.create_item(
        file.filename, "file", owner_id, parentId, size=file_size, partition_id=partition_id
    )

    # Sync file upload to Backblaze B2 using owner_id
    from app.b2 import upload_b2_file, get_item_path, get_user_b2_prefix
    path = await get_item_path(item, owner_id)
    prefix = await get_user_b2_prefix(owner_id)
    upload_b2_file(f"{prefix}/{path}", content)

    return await _to_response_async(item)


@router.post(
    "/files/upload/chunk",
    status_code=201,
    summary="Upload a file chunk",
)
async def upload_chunk(
    request: Request,
    file: UploadFile = File(...),
    uploadId: str = Form(...),
    chunkIndex: int = Form(...),
    totalChunks: int = Form(...),
    filename: str = Form(...),
    parentId: Optional[str] = Form(None),
    partitionId: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
):
    """Upload a file chunk to the specified parent folder.

    Merges when all chunks are uploaded, then registers in DB and uploads to B2.
    """
    owner_id = str(current_user.id)
    partition_id = None
    if parentId:
        parent = await FileSystemItem.get(parentId)
        if not parent:
            raise HTTPException(
                status_code=400,
                detail={"error": f"Parent folder '{parentId}' not found."},
            )

        from app.security_helpers import verify_write_access
        await verify_write_access(parent, current_user)

        owner_id = parent.user_id
        partition_id = parent.partition_id

        from app.security_helpers import get_unlocked_passwords, is_access_blocked
        unlocked_passwords = get_unlocked_passwords(request)
        if await is_access_blocked(parent, owner_id, unlocked_passwords):
            raise HTTPException(
                status_code=403,
                detail={"error": "Access to parent folder is locked."}
            )
    else:
        partition_id = partitionId

    if not filename:
        raise HTTPException(
            status_code=400, detail={"error": "Filename is required."}
        )

    # Validate name duplicate on the first chunk to prevent wasting time on duplicate uploads
    if chunkIndex == 0:
        if await crud.check_duplicate_name(filename, parentId, "file", owner_id):
            raise HTTPException(
                status_code=409,
                detail={"error": f'A file named "{filename}" already exists in this location.'},
            )

    # Path to temp directory for this upload
    temp_dir = os.path.join(tempfile.gettempdir(), f"getfilenova_upload_{uploadId}")
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
        if await crud.check_duplicate_name(filename, parentId, "file", owner_id):
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

            # Check overall user storage limit under owner_id
            owner_user = await User.get(owner_id)
            limit_bytes = owner_user.storage_limit_bytes if owner_user else current_user.storage_limit_bytes
            current_used = await crud.get_user_storage_size(owner_id)
            if current_used + file_size > limit_bytes:
                # Cleanup temp files
                shutil.rmtree(temp_dir, ignore_errors=True)
                await clean_upload_lock(uploadId)
                raise HTTPException(
                    status_code=400,
                    detail={"error": f"Storage limit exceeded. Cannot upload file (Size: {file_size} bytes, Used: {current_used} bytes, Limit: {limit_bytes} bytes)."}
                )

            # Check partition storage limit if target is a partition
            if partition_id:
                partition = await StoragePartition.get(partition_id)
                if partition:
                    files_in_part = await FileSystemItem.find(
                        FileSystemItem.user_id == owner_id,
                        FileSystemItem.partition_id == partition_id,
                        FileSystemItem.type == "file",
                        FileSystemItem.is_deleted == False
                    ).to_list()
                    partition_used = sum(f.size or 0 for f in files_in_part)
                    if partition_used + file_size > partition.allocated_size_bytes:
                        # Cleanup temp files
                        shutil.rmtree(temp_dir, ignore_errors=True)
                        await clean_upload_lock(uploadId)
                        raise HTTPException(
                            status_code=400,
                            detail={"error": f"Partition '{partition.name}' storage limit exceeded. Cannot upload file (Size: {file_size} bytes, Used: {partition_used} bytes, Allocation: {partition.allocated_size_bytes} bytes)."}
                        )

            # Create MongoDB entry under owner_id
            item = await crud.create_item(
                filename, "file", owner_id, parentId, size=file_size, partition_id=partition_id
            )

            # Sync file upload to Backblaze B2 using owner_id
            from app.b2 import upload_b2_file_from_path, get_item_path, get_user_b2_prefix
            import functools
            path = await get_item_path(item, owner_id)
            prefix = await get_user_b2_prefix(owner_id)
            b2_key = f"{prefix}/{path}"

            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(
                None,
                functools.partial(upload_b2_file_from_path, merged_file_path, b2_key)
            )
            if not success:
                logger.error(f"B2 upload from path failed for {filename}")

            return await _to_response_async(item)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail={"error": f"Failed during merge or upload: {str(e)}"}
            )
        finally:
            # Clean up temp folder & files
            shutil.rmtree(temp_dir, ignore_errors=True)
            await clean_upload_lock(uploadId)


# ── Lock/Unlock/Hide/Unhide/Disable Folder Lock Endpoints ────────────────────

@router.post(
    "/folders/{item_id}/lock",
    response_model=FileSystemItemResponse,
    summary="Lock a folder with password protection",
    responses={400: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def lock_folder(
    item_id: str,
    body: LockFolderRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Lock a folder with password protection."""
    from app.security_helpers import get_unlocked_passwords, is_access_blocked
    from app.auth import hash_password
    
    item = await FileSystemItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Folder not found."})

    from app.security_helpers import verify_write_access
    await verify_write_access(item, current_user)
        
    if item.type != "folder":
        raise HTTPException(status_code=400, detail={"error": "Only folders can be locked."})
        
    if getattr(item, "is_locked", False):
        raise HTTPException(status_code=400, detail={"error": "Folder is already locked."})
        
    owner_id = item.user_id if item.user_id else str(current_user.id)
    unlocked_passwords = get_unlocked_passwords(request)
    if await is_access_blocked(item, owner_id, unlocked_passwords):
        raise HTTPException(status_code=403, detail={"error": "Access to folder is locked."})
        
    item.is_locked = True
    item.lock_password_hash = hash_password(body.password)
    await item.save()
    
    return await _to_response_async(item)


@router.post(
    "/folders/{item_id}/unlock",
    response_model=MessageResponse,
    summary="Challenge a locked folder's password",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def unlock_folder(
    item_id: str,
    body: LockFolderRequest,
    current_user: User = Depends(get_current_user),
):
    """Verify password for a locked folder."""
    from app.auth import verify_password
    
    item = await FileSystemItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Folder not found."})

    from app.security_helpers import verify_read_access
    await verify_read_access(item, current_user)
        
    if item.type != "folder":
        raise HTTPException(status_code=400, detail={"error": "Only folders can be unlocked."})
        
    if not getattr(item, "is_locked", False):
        return MessageResponse(message="Folder is not locked.")
        
    if not verify_password(body.password, item.lock_password_hash or ""):
        raise HTTPException(status_code=400, detail={"error": "Invalid password."})
        
    return MessageResponse(message="Success")


@router.post(
    "/folders/{item_id}/disable-lock",
    response_model=FileSystemItemResponse,
    summary="Permanently remove password protection from a folder",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def disable_lock(
    item_id: str,
    body: LockFolderRequest,
    current_user: User = Depends(get_current_user),
):
    """Permanently remove password lock protection from a folder."""
    from app.auth import verify_password
    
    item = await FileSystemItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Folder not found."})

    from app.security_helpers import verify_write_access
    await verify_write_access(item, current_user)
        
    if item.type != "folder":
        raise HTTPException(status_code=400, detail={"error": "Only folders can have lock removed."})
        
    if not getattr(item, "is_locked", False):
        return await _to_response_async(item)
        
    if not verify_password(body.password, item.lock_password_hash or ""):
        raise HTTPException(status_code=400, detail={"error": "Invalid password."})
        
    item.is_locked = False
    item.lock_password_hash = None
    await item.save()
    
    return await _to_response_async(item)


@router.post(
    "/folders/{item_id}/hide",
    response_model=FileSystemItemResponse,
    summary="Hide a file or folder",
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def hide_item(
    item_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Hide an existing file or folder."""
    from app.security_helpers import get_unlocked_passwords, is_access_blocked
    
    item = await FileSystemItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})

    from app.security_helpers import verify_write_access
    await verify_write_access(item, current_user)

    owner_id = item.user_id if item.user_id else str(current_user.id)
    unlocked_passwords = get_unlocked_passwords(request)
    if await is_access_blocked(item, owner_id, unlocked_passwords):
        raise HTTPException(status_code=403, detail={"error": "Access to item is locked."})
        
    item.is_hidden = True
    await item.save()
    return _to_response(item)


@router.post(
    "/folders/{item_id}/unhide",
    response_model=FileSystemItemResponse,
    summary="Unhide a file or folder",
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def unhide_item(
    item_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Unhide an existing file or folder."""
    from app.security_helpers import get_unlocked_passwords, is_access_blocked
    
    item = await crud.get_item_by_id(item_id, str(current_user.id))
    if not item:
        raise HTTPException(status_code=404, detail={"error": "Item not found."})
        
    unlocked_passwords = get_unlocked_passwords(request)
    if await is_access_blocked(item, str(current_user.id), unlocked_passwords):
        raise HTTPException(status_code=403, detail={"error": "Access to item is locked."})
        
    item.is_hidden = False
    await item.save()
    return _to_response(item)


# ── Sharing Operations ────────────────────────────────────────────────────────


@router.get(
    "/shares/shared-with-me",
    response_model=List[FileSystemItemResponse],
    summary="List files and folders directly shared with me",
)
async def list_shared_with_me(current_user: User = Depends(get_current_user)):
    """Retrieve all folder and file items shared directly with the current user."""
    items = await FileSystemItem.find({
        "$or": [
            {"shares.user_id": str(current_user.id)},
            {"shares.email": current_user.email.lower()}
        ],
        "is_deleted": False
    }).to_list()

    # Pre-populate user email map
    user_ids = {item.user_id for item in items if item.user_id}
    user_email_map = {}
    if user_ids:
        from beanie import PydanticObjectId
        object_ids = [PydanticObjectId(uid) for uid in user_ids if len(uid) == 24]
        if object_ids:
            users_obj = await User.find({"_id": {"$in": object_ids}}).to_list()
            for u in users_obj:
                user_email_map[str(u.id)] = u.email
        str_ids = [uid for uid in user_ids if len(uid) != 24]
        if str_ids:
            users_str = await User.find({"_id": {"$in": str_ids}}).to_list()
            for u in users_str:
                user_email_map[str(u.id)] = u.email

    return [_to_response(item, user_email_map) for item in items]


@router.get(
    "/folders/{item_id}/shares",
    response_model=List[ItemShareResponse],
    summary="List shares configuration for an item (owner only)",
)
async def get_item_shares(
    item_id: str,
    current_user: User = Depends(get_current_user),
):
    """List all user email shares directly configured on this folder/file."""
    item = await FileSystemItem.get(item_id)
    if not item or item.user_id != str(current_user.id):
        raise HTTPException(
            status_code=404,
            detail={"error": "Item not found or you are not the owner."}
        )
    shares_list = getattr(item, "shares", []) or []
    return [
        ItemShareResponse(userId=s.user_id, email=s.email, permission=s.permission)
        for s in shares_list
    ]


@router.post(
    "/folders/{item_id}/share",
    response_model=MessageResponse,
    summary="Share folder/file with a user (owner only)",
)
async def share_item(
    item_id: str,
    body: ShareItemRequest,
    current_user: User = Depends(get_current_user),
):
    """Grant another user access to this item."""
    item = await FileSystemItem.get(item_id)
    if not item or item.user_id != str(current_user.id):
        raise HTTPException(
            status_code=404,
            detail={"error": "Item not found or you are not the owner."}
        )

    target_email = body.email.strip().lower()
    if target_email == current_user.email.lower():
        raise HTTPException(
            status_code=400,
            detail={"error": "You cannot share files with yourself."}
        )

    # Find recipient user
    recipient = await User.find_one(User.email == target_email)
    if not recipient:
        raise HTTPException(
            status_code=404,
            detail={"error": f"No user found with the email address '{body.email}'."}
        )

    # Update or add share record
    shares_list = getattr(item, "shares", [])
    if shares_list is None:
        shares_list = []

    # Check existing share
    existing = None
    for s in shares_list:
        if s.user_id == str(recipient.id) or s.email.lower() == target_email:
            existing = s
            break

    from app.models import ItemShare
    if existing:
        existing.permission = body.permission
    else:
        shares_list.append(ItemShare(
            user_id=str(recipient.id),
            email=recipient.email,
            permission=body.permission
        ))

    item.shares = shares_list
    await item.save()

    return MessageResponse(
        message=f"Access shared successfully with {recipient.email} ({body.permission})."
    )


@router.delete(
    "/folders/{item_id}/share/{user_id}",
    response_model=MessageResponse,
    summary="Revoke share configuration for a user (owner only)",
)
async def revoke_item_share(
    item_id: str,
    user_id: str,
    current_user: User = Depends(get_current_user),
):
    """Revoke sharing permissions from a user."""
    item = await FileSystemItem.get(item_id)
    if not item or item.user_id != str(current_user.id):
        raise HTTPException(
            status_code=404,
            detail={"error": "Item not found or you are not the owner."}
        )

    shares_list = getattr(item, "shares", []) or []
    new_shares = [s for s in shares_list if s.user_id != user_id]

    item.shares = new_shares
    await item.save()

    return MessageResponse(message="Sharing permissions revoked successfully.")
