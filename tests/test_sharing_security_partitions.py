"""Automated Integration Tests for Sharing, Permissions, IDOR Protection, Safe Folders, and Partitions."""

import pytest
from unittest.mock import patch, AsyncMock
from fastapi import HTTPException, status
from starlette.requests import Request as StarletteRequest

from app.models import User, FileSystemItem, StoragePartition, ItemShare
from app.auth import hash_password
from app.security_helpers import (
    verify_read_access,
    verify_write_access,
    get_effective_sharing_permission,
    is_access_blocked,
)
from app import crud
from app.partition_routes import create_partition, list_partitions, _to_partition_response
from app.schemas import CreatePartitionRequest, ShareItemRequest


class TestSharingSecurityPartitions:
    """Tests covering Phase 11, 12, 14, 21 (Sharing, Permissions, IDOR, Safe Folder, Virtual Partitions)."""

    @pytest.fixture
    async def user_a(self):
        user = User(
            name="Alice Owner",
            email="alice@owner.com",
            hashed_password=hash_password("Pass1234!"),
            storage_limit_bytes=5000000,
        )
        await user.insert()
        return user

    @pytest.fixture
    async def user_b(self):
        user = User(
            name="Bob Collaborator",
            email="bob@collaborator.com",
            hashed_password=hash_password("Pass1234!"),
            storage_limit_bytes=5000000,
        )
        await user.insert()
        return user

    @pytest.fixture
    async def user_c(self):
        user = User(
            name="Eve Attacker",
            email="eve@attacker.com",
            hashed_password=hash_password("Pass1234!"),
            storage_limit_bytes=5000000,
        )
        await user.insert()
        return user

    async def test_sharing_viewer_vs_editor_permissions(self, user_a, user_b):
        """Verify viewer has read-only access while editor has read-write access."""
        uid_a = str(user_a.id)
        uid_b = str(user_b.id)

        # Alice creates a folder and shares with Bob as 'viewer'
        doc = await crud.create_item("Quarterly Review.pdf", "file", uid_a, size=1000)
        doc.shares = [ItemShare(user_id=uid_b, email=user_b.email, permission="viewer")]
        await doc.save()

        # Bob can read
        perm = await get_effective_sharing_permission(doc, user_b)
        assert perm == "viewer"
        await verify_read_access(doc, user_b)  # Should not raise

        # Bob cannot write (raises 403)
        with pytest.raises(HTTPException) as exc:
            await verify_write_access(doc, user_b)
        assert exc.value.status_code == status.HTTP_403_FORBIDDEN

        # Alice promotes Bob to 'editor'
        doc.shares = [ItemShare(user_id=uid_b, email=user_b.email, permission="editor")]
        await doc.save()

        # Bob now has write access
        await verify_write_access(doc, user_b)  # Should not raise

        # Alice revokes access
        doc.shares = []
        await doc.save()

        # Bob now has no access (raises 404/403)
        with pytest.raises(HTTPException) as exc:
            await verify_read_access(doc, user_b)
        assert exc.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_idor_protection_unrelated_user_blocked(self, user_a, user_c):
        """Verify User C cannot access User A's private items."""
        uid_a = str(user_a.id)

        private_item = await crud.create_item("Confidential.docx", "file", uid_a, size=500)

        # Eve attempts to read Alice's private file
        with pytest.raises(HTTPException) as exc:
            await verify_read_access(private_item, user_c)
        assert exc.value.status_code == status.HTTP_404_NOT_FOUND

        # Eve attempts to write to Alice's private file
        with pytest.raises(HTTPException) as exc:
            await verify_write_access(private_item, user_c)
        assert exc.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_safe_folder_password_lock_enforcement(self, user_a):
        """Verify locked folder requires password verification and blocks unauthorized access."""
        uid_a = str(user_a.id)

        # Create locked folder
        secret_folder = await crud.create_item("Vault", "folder", uid_a)
        secret_folder.is_locked = True
        secret_folder.lock_password_hash = hash_password("VaultSecret99!")
        await secret_folder.save()

        secret_file = await crud.create_item("passwords.txt", "file", uid_a, parent_id=str(secret_folder.id), size=100)

        # 1. Access without password should be blocked
        blocked_without_pwd = await is_access_blocked(secret_folder, uid_a, unlocked_passwords={})
        assert blocked_without_pwd is True

        # Descendant without password should be blocked
        child_blocked = await is_access_blocked(secret_file, uid_a, unlocked_passwords={})
        assert child_blocked is True

        # 2. Access with wrong password should be blocked
        wrong_pwd_map = {str(secret_folder.id): "WrongPassword123"}
        assert await is_access_blocked(secret_folder, uid_a, unlocked_passwords=wrong_pwd_map) is True

        # 3. Access with correct password should be granted
        correct_pwd_map = {str(secret_folder.id): "VaultSecret99!"}
        assert await is_access_blocked(secret_folder, uid_a, unlocked_passwords=correct_pwd_map) is False
        assert await is_access_blocked(secret_file, uid_a, unlocked_passwords=correct_pwd_map) is False

    async def test_virtual_partitions_quota_and_isolation(self, user_a, user_b):
        """Verify partition allocation, quota calculations, and isolation."""
        uid_a = str(user_a.id)

        # Alice creates a 2MB partition
        part_req = CreatePartitionRequest(name="Project X Drive", allocatedSizeBytes=2000000)
        part = await create_partition(part_req, user_a)
        assert part.name == "Project X Drive"
        assert part.allocatedSizeBytes == 2000000

        # Alice uploads a file into the partition
        file_in_part = await crud.create_item(
            "code.tar.gz", "file", uid_a, size=500000, partition_id=str(part.id)
        )

        # Verify used size aggregation
        used_map = await crud.get_all_partitions_used_sizes(uid_a)
        assert used_map.get(str(part.id)) == 500000

        # Alice's partitions listed
        alice_parts = await list_partitions(user_a)
        assert len(alice_parts) == 1
        assert alice_parts[0].usedSizeBytes == 500000

        # Bob's partitions listed (should be 0, complete isolation)
        bob_parts = await list_partitions(user_b)
        assert len(bob_parts) == 0

        # Partition quota overflow check: allocating more than user limit fails (400)
        overflow_req = CreatePartitionRequest(name="Too Big", allocatedSizeBytes=10000000)
        with pytest.raises(HTTPException) as exc:
            await create_partition(overflow_req, user_a)
        assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
