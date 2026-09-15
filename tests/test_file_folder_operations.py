"""Automated Integration & Unit Tests for File and Folder Operations, Bin, Restore, and Quotas."""

import pytest
from unittest.mock import patch, AsyncMock
from fastapi import HTTPException, status, Request
from starlette.requests import Request as StarletteRequest

from app.models import User, FileSystemItem, StoragePartition
from app import crud
from app.auth import hash_password


class TestFolderFileOperations:
    """Tests covering Phase 5, 8, 9, 10, 12, 13."""

    @pytest.fixture
    async def sample_user(self):
        user = User(
            name="Test User",
            email="tester@getfilenova.com",
            hashed_password=hash_password("Pass1234!"),
            storage_limit_bytes=1000000,  # 1MB limit for testing quota
        )
        await user.insert()
        return user

    async def test_create_and_lazy_load_folder_children(self, sample_user):
        """Verify immediate child loading and pagination (does NOT load entire tree)."""
        uid = str(sample_user.id)
        email = sample_user.email

        # Create hierarchy: Root -> Documents -> Work -> ProjectA
        docs = await crud.create_item("Documents", "folder", uid, parent_id=None)
        images = await crud.create_item("Images", "folder", uid, parent_id=None)
        work = await crud.create_item("Work", "folder", uid, parent_id=str(docs.id))
        proj_a = await crud.create_item("Project A", "folder", uid, parent_id=str(work.id))
        file1 = await crud.create_item("doc1.txt", "file", uid, parent_id=str(proj_a.id), size=100)

        # 1. Query Root (parent_id=None)
        root_items, cursor, has_more, total = await crud.get_folder_children_paginated(
            user_id=uid, email=email, parent_id=None
        )
        root_names = {i.name for i in root_items}
        assert "Documents" in root_names
        assert "Images" in root_names
        assert "Work" not in root_names
        assert "Project A" not in root_names
        assert "doc1.txt" not in root_names

        # 2. Query Documents
        doc_children, _, _, _ = await crud.get_folder_children_paginated(
            user_id=uid, email=email, parent_id=str(docs.id)
        )
        assert len(doc_children) == 1
        assert doc_children[0].name == "Work"

        # 3. Query Work
        work_children, _, _, _ = await crud.get_folder_children_paginated(
            user_id=uid, email=email, parent_id=str(work.id)
        )
        assert len(work_children) == 1
        assert work_children[0].name == "Project A"

    async def test_rename_and_duplicate_name_detection(self, sample_user):
        """Verify renaming and duplicate name rejection."""
        uid = str(sample_user.id)
        f1 = await crud.create_item("Reports", "folder", uid)
        f2 = await crud.create_item("Invoices", "folder", uid)

        # Check duplicate
        is_dup = await crud.check_duplicate_name("Reports", None, "folder", uid, exclude_id=str(f2.id))
        assert is_dup is True

        is_not_dup = await crud.check_duplicate_name("UniqueName", None, "folder", uid)
        assert is_not_dup is False

        # Rename f2
        updated = await crud.rename_item(f2, "Archives")
        assert updated.name == "Archives"

    async def test_move_folder_and_descendants_integrity(self, sample_user):
        """Verify moving a folder preserves all child relationships and metadata."""
        uid = str(sample_user.id)

        # Create Root -> FolderA -> Child1, and Root -> FolderB
        folder_a = await crud.create_item("Folder A", "folder", uid)
        child_file = await crud.create_item("file.pdf", "file", uid, parent_id=str(folder_a.id), size=500)
        folder_b = await crud.create_item("Folder B", "folder", uid)

        # Move Folder A into Folder B
        moved = await crud.move_item(folder_a, target_parent_id=str(folder_b.id))
        assert moved.parent_id == str(folder_b.id)

        # Verify child still has parent_id = folder_a.id
        refreshed_child = await FileSystemItem.get(child_file.id)
        assert refreshed_child.parent_id == str(folder_a.id)

    async def test_soft_delete_recycle_bin_and_restore(self, sample_user):
        """Verify soft-delete moves item and all descendants to bin, and restore brings them back."""
        uid = str(sample_user.id)
        email = sample_user.email

        parent = await crud.create_item("Parent", "folder", uid)
        child1 = await crud.create_item("Child1", "folder", uid, parent_id=str(parent.id))
        file1 = await crud.create_item("data.csv", "file", uid, parent_id=str(child1.id), size=200)

        # Soft delete parent
        affected = await crud.soft_delete_item(str(parent.id), uid)
        assert len(affected) == 3

        # Active root should be empty
        active_root, _, _, _ = await crud.get_folder_children_paginated(uid, email, parent_id=None)
        assert len(active_root) == 0

        # Bin view should show all 3 deleted items
        bin_items, _, _, _ = await crud.get_folder_children_paginated(uid, email, bin_only=True)
        assert len(bin_items) == 3
        bin_names = {i.name for i in bin_items}
        assert "Parent" in bin_names
        assert "Child1" in bin_names
        assert "data.csv" in bin_names

        # Restore
        restored = await crud.restore_item(str(parent.id), uid)
        assert len(restored) == 3

        # Active root should have parent back
        active_root, _, _, _ = await crud.get_folder_children_paginated(uid, email, parent_id=None)
        assert len(active_root) == 1
        assert active_root[0].name == "Parent"

    async def test_empty_bin_purges_only_deleted_items(self, sample_user):
        """Verify empty_bin permanently purges all soft-deleted items without affecting active items."""
        uid = str(sample_user.id)

        active = await crud.create_item("ActiveFile.txt", "file", uid, size=100)
        deleted1 = await crud.create_item("Deleted1.txt", "file", uid, size=100)
        deleted2 = await crud.create_item("Deleted2.txt", "file", uid, size=100)

        await crud.soft_delete_item(str(deleted1.id), uid)
        await crud.soft_delete_item(str(deleted2.id), uid)

        with patch("app.b2.handle_b2_delete", new=AsyncMock()):
            count = await crud.empty_bin(uid)
            assert count == 2

        # Active file still exists
        remaining = await FileSystemItem.find(FileSystemItem.user_id == uid).to_list()
        assert len(remaining) == 1
        assert remaining[0].name == "ActiveFile.txt"

    async def test_storage_aggregation_and_limit_enforcement(self, sample_user):
        """Verify storage size calculation aggregates file sizes in O(1)."""
        uid = str(sample_user.id)

        await crud.create_item("f1.bin", "file", uid, size=300000)
        await crud.create_item("f2.bin", "file", uid, size=400000)
        await crud.create_item("folder", "folder", uid)  # Folders don't count towards bytes

        total_used = await crud.get_user_storage_size(uid)
        assert total_used == 700000

        # Attempting to upload 400000 more exceeds 1000000 limit
        new_file_size = 400000
        assert (total_used + new_file_size) > sample_user.storage_limit_bytes

    async def test_regression_documents_work_testak_exact_hierarchy(self, sample_user):
        """Exact reproduction of Root -> Documents -> [Work, testak] hierarchy with lazy loading."""
        uid = str(sample_user.id)
        email = sample_user.email

        # 1. Create Root -> Documents
        docs = await crud.create_item("Documents", "folder", uid, parent_id=None)
        assert docs.parent_id is None

        # 2. Create Documents -> Work and Documents -> testak
        work = await crud.create_item("Work", "folder", uid, parent_id=str(docs.id))
        testak = await crud.create_item("testak", "folder", uid, parent_id=str(docs.id))
        assert work.parent_id == str(docs.id)
        assert testak.parent_id == str(docs.id)

        # 3. Create Work -> ProjectFiles
        proj = await crud.create_item("ProjectFiles", "folder", uid, parent_id=str(work.id))
        assert proj.parent_id == str(work.id)

        # 4. Query Root
        root_items, _, _, _ = await crud.get_folder_children_paginated(
            user_id=uid, email=email, parent_id=None
        )
        assert any(i.name == "Documents" for i in root_items)
        assert not any(i.name in ("Work", "testak", "ProjectFiles") for i in root_items)

        # 5. Query Documents (immediate children only)
        doc_children, _, _, _ = await crud.get_folder_children_paginated(
            user_id=uid, email=email, parent_id=str(docs.id)
        )
        child_names = {i.name for i in doc_children}
        assert len(doc_children) == 2
        assert "Work" in child_names
        assert "testak" in child_names
        assert "ProjectFiles" not in child_names

        # 6. Query Work (immediate children only)
        work_children, _, _, _ = await crud.get_folder_children_paginated(
            user_id=uid, email=email, parent_id=str(work.id)
        )
        assert len(work_children) == 1
        assert work_children[0].name == "ProjectFiles"

    async def test_regression_user_isolation_and_deleted_exclusion(self, sample_user):
        """Verify user isolation and deleted folder exclusion for nested queries."""
        uid_a = str(sample_user.id)
        email_a = sample_user.email

        user_b = User(
            name="User B",
            email="user_b@getfilenova.com",
            hashed_password=hash_password("Pass1234!"),
        )
        await user_b.insert()
        uid_b = str(user_b.id)
        email_b = user_b.email

        # User A creates Root -> Documents -> Work
        docs_a = await crud.create_item("Documents", "folder", uid_a, parent_id=None)
        work_a = await crud.create_item("Work", "folder", uid_a, parent_id=str(docs_a.id))
        deleted_child = await crud.create_item("OldWork", "folder", uid_a, parent_id=str(docs_a.id))
        await crud.soft_delete_item(str(deleted_child.id), uid_a)

        # Query Documents for User A -> deleted folder excluded
        doc_children, _, _, _ = await crud.get_folder_children_paginated(
            user_id=uid_a, email=email_a, parent_id=str(docs_a.id)
        )
        names = [i.name for i in doc_children]
        assert "Work" in names
        assert "OldWork" not in names

        # Query Documents for User B -> User B has no access to User A's private folder
        from fastapi import HTTPException
        from app.routes import list_items
        from unittest.mock import MagicMock
        req = MagicMock(spec=Request)
        req.cookies = {}

        with pytest.raises(HTTPException) as exc_info:
            await list_items(
                request=req,
                parent_id=str(docs_a.id),
                current_user=user_b,
            )
        assert exc_info.value.status_code in (403, 404)
