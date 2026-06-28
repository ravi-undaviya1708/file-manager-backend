"""Router for drive partition management."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel

from app.auth import get_current_user, hash_password, verify_password
from app.models import User, FileSystemItem, StoragePartition
from app.schemas import (
    PartitionResponse,
    CreatePartitionRequest,
    ResizePartitionRequest,
    MessageResponse,
    ErrorResponse
)

router = APIRouter(prefix="/api/partitions", tags=["Partitions"])


async def _to_partition_response(partition: StoragePartition) -> PartitionResponse:
    """Calculate actual used size and format partition details."""
    # Find all files belonging to this partition
    files = await FileSystemItem.find(
        FileSystemItem.user_id == partition.user_id,
        FileSystemItem.partition_id == str(partition.id),
        FileSystemItem.type == "file",
        FileSystemItem.is_deleted == False
    ).to_list()
    used_size = sum(f.size or 0 for f in files)
    
    return PartitionResponse(
        id=str(partition.id),
        name=partition.name,
        allocatedSizeBytes=partition.allocated_size_bytes,
        usedSizeBytes=used_size,
        createdAt=partition.created_at.isoformat() if partition.created_at else "",
        isLocked=partition.is_locked
    )


@router.get(
    "",
    response_model=List[PartitionResponse],
    summary="Get all user partitions"
)
async def list_partitions(current_user: User = Depends(get_current_user)):
    """Retrieve all storage partitions created by the current user."""
    partitions = await StoragePartition.find(
        StoragePartition.user_id == str(current_user.id)
    ).to_list()
    
    response = []
    for p in partitions:
        response.append(await _to_partition_response(p))
    return response


@router.post(
    "",
    response_model=PartitionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new partition"
)
async def create_partition(
    body: CreatePartitionRequest,
    current_user: User = Depends(get_current_user)
):
    """Allocate a new storage partition for the user."""
    user_id_str = str(current_user.id)
    
    # 1. Check sum of all allocated partitions
    existing_partitions = await StoragePartition.find(
        StoragePartition.user_id == user_id_str
    ).to_list()
    total_allocated = sum(p.allocated_size_bytes for p in existing_partitions)
    
    # 2. Check user has enough free unallocated space
    max_storage = current_user.storage_limit_bytes
    if total_allocated + body.allocatedSizeBytes > max_storage:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": f"Insufficient storage capacity. You only have {max_storage - total_allocated} bytes of unallocated space remaining."}
        )
        
    # Create the partition
    partition = StoragePartition(
        user_id=user_id_str,
        name=body.name.strip(),
        allocated_size_bytes=body.allocatedSizeBytes
    )
    await partition.insert()
    
    return await _to_partition_response(partition)


@router.put(
    "/{partition_id}",
    response_model=PartitionResponse,
    summary="Resize or rename a partition"
)
async def update_partition(
    partition_id: str,
    body: ResizePartitionRequest,
    current_user: User = Depends(get_current_user)
):
    """Rename or resize an existing partition."""
    user_id_str = str(current_user.id)
    partition = await StoragePartition.get(partition_id)
    if not partition or partition.user_id != user_id_str:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Partition not found."}
        )
        
    updates = {}
    if body.name:
        updates["name"] = body.name.strip()
        
    if body.allocatedSizeBytes is not None:
        # Check that new size is not smaller than current file usage inside partition
        files = await FileSystemItem.find(
            FileSystemItem.user_id == user_id_str,
            FileSystemItem.partition_id == partition_id,
            FileSystemItem.type == "file",
            FileSystemItem.is_deleted == False
        ).to_list()
        used_size = sum(f.size or 0 for f in files)
        
        if body.allocatedSizeBytes < used_size:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": f"Cannot shrink partition to {body.allocatedSizeBytes} bytes. It already contains {used_size} bytes of files."}
            )
            
        # Check sum of all allocations
        existing_partitions = await StoragePartition.find(
            StoragePartition.user_id == user_id_str
        ).to_list()
        
        other_allocated = sum(p.allocated_size_bytes for p in existing_partitions if str(p.id) != partition_id)
        max_storage = current_user.storage_limit_bytes
        
        if other_allocated + body.allocatedSizeBytes > max_storage:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": f"Allocation exceeds your total drive limit. Maximum allowable allocation is {max_storage - other_allocated} bytes."}
            )
            
        updates["allocated_size_bytes"] = body.allocatedSizeBytes
        
    if updates:
        await partition.update({"$set": updates})
        partition = await StoragePartition.get(partition_id)
        
    return await _to_partition_response(partition)


@router.post(
    "/{partition_id}/format",
    response_model=MessageResponse,
    summary="Format a partition (delete all items inside)"
)
async def format_partition(
    partition_id: str,
    current_user: User = Depends(get_current_user)
):
    """Permanently delete all files and folders inside the partition."""
    user_id_str = str(current_user.id)
    partition = await StoragePartition.get(partition_id)
    if not partition or partition.user_id != user_id_str:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Partition not found."}
        )
        
    # Get all items in the partition
    items = await FileSystemItem.find(
        FileSystemItem.user_id == user_id_str,
        FileSystemItem.partition_id == partition_id
    ).to_list()
    
    # 1. Sync hard-delete files from B2
    from app.b2 import handle_b2_delete
    for item in items:
        if item.type == "file":
            try:
                await handle_b2_delete(item, user_id_str)
            except Exception:
                pass  # Ignore missing B2 files in hard delete fallback
                
    # 2. Hard-delete from database
    await FileSystemItem.find(
        FileSystemItem.user_id == user_id_str,
        FileSystemItem.partition_id == partition_id
    ).delete()
    
    return MessageResponse(message="Partition formatted successfully.")


@router.delete(
    "/{partition_id}",
    response_model=MessageResponse,
    summary="Delete a partition entirely"
)
async def delete_partition(
    partition_id: str,
    current_user: User = Depends(get_current_user)
):
    """Delete a partition and format all its contents recursively."""
    user_id_str = str(current_user.id)
    partition = await StoragePartition.get(partition_id)
    if not partition or partition.user_id != user_id_str:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Partition not found."}
        )
        
    # 1. Purge all contents first
    await format_partition(partition_id, current_user)
    
    # 2. Delete the partition document
    await partition.delete()
    
    return MessageResponse(message="Partition deleted successfully.")


class LockPartitionRequest(BaseModel):
    password: str


@router.post(
    "/{partition_id}/lock",
    response_model=PartitionResponse,
    summary="Password lock a partition"
)
async def lock_partition(
    partition_id: str,
    body: LockPartitionRequest,
    current_user: User = Depends(get_current_user)
):
    """Secure a storage partition with a password lock."""
    user_id_str = str(current_user.id)
    partition = await StoragePartition.get(partition_id)
    if not partition or partition.user_id != user_id_str:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Partition not found."}
        )
    
    hashed = hash_password(body.password)
    await partition.update({
        "$set": {
            "is_locked": True,
            "lock_password_hash": hashed
        }
    })
    
    partition = await StoragePartition.get(partition_id)
    return await _to_partition_response(partition)


@router.post(
    "/{partition_id}/unlock",
    response_model=MessageResponse,
    summary="Verify password and unlock a partition temporarily"
)
async def unlock_partition(
    partition_id: str,
    body: LockPartitionRequest,
    current_user: User = Depends(get_current_user)
):
    """Verify password and allow temporary access to the partition's files."""
    user_id_str = str(current_user.id)
    partition = await StoragePartition.get(partition_id)
    if not partition or partition.user_id != user_id_str:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Partition not found."}
        )
        
    if not partition.is_locked or not partition.lock_password_hash:
        return MessageResponse(message="Partition is not locked.")
        
    if not verify_password(body.password, partition.lock_password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Incorrect password. Secure access verification failed."}
        )
        
    return MessageResponse(message="Partition unlocked successfully.")


@router.post(
    "/{partition_id}/disable-lock",
    response_model=PartitionResponse,
    summary="Remove password lock from a partition"
)
async def disable_lock_partition(
    partition_id: str,
    body: LockPartitionRequest,
    current_user: User = Depends(get_current_user)
):
    """Permanently disable password locking on the partition."""
    user_id_str = str(current_user.id)
    partition = await StoragePartition.get(partition_id)
    if not partition or partition.user_id != user_id_str:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Partition not found."}
        )
        
    if not partition.is_locked or not partition.lock_password_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Partition is not locked."}
        )
        
    if not verify_password(body.password, partition.lock_password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Incorrect password. Handshake denied."}
        )
        
    await partition.update({
        "$set": {
            "is_locked": False,
            "lock_password_hash": None
        }
    })
    
    partition = await StoragePartition.get(partition_id)
    return await _to_partition_response(partition)
