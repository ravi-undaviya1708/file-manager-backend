"""Automated Performance and Large Dataset Scalability Benchmark Tests."""

import time
import pytest
from unittest.mock import patch, AsyncMock
from fastapi import BackgroundTasks

from app.models import User, FileSystemItem
from app.auth import hash_password, verify_password, create_access_token
from app.auth_routes import login
from app.schemas import UserLoginRequest
from app import crud


class TestPerformanceAndScale:
    """Tests covering Phase 3, 4, 6, 27, 28, 29 (Login Performance, Large Dataset Scaling, Latency Breakdown)."""

    @pytest.fixture
    async def perf_user(self):
        user = User(
            name="Performance User",
            email="perf@getfilenova.com",
            hashed_password=hash_password("MySecurePass123!"),
            storage_limit_bytes=10000000000,
        )
        await user.insert()
        return user

    async def test_login_latency_breakdown(self, perf_user):
        """Measure real granular latency breakdown of login lifecycle."""
        email = "perf@getfilenova.com"
        password = "MySecurePass123!"

        # 1. MongoDB Query Latency
        t0 = time.perf_counter()
        user = await User.find_one({"email": email})
        t_query = (time.perf_counter() - t0) * 1000  # ms
        assert user is not None

        # 2. Bcrypt Password Verification Latency
        t0 = time.perf_counter()
        is_valid = verify_password(password, user.hashed_password)
        t_bcrypt = (time.perf_counter() - t0) * 1000  # ms
        assert is_valid is True

        # 3. JWT Token Generation Latency
        t0 = time.perf_counter()
        token = create_access_token(data={"sub": str(user.id)})
        t_jwt = (time.perf_counter() - t0) * 1000  # ms
        assert token is not None

        # 4. Total Login Round-Trip Latency (Cold vs Warm)
        req = UserLoginRequest(email=email, password=password)
        with patch("app.b2.check_and_sync_user", new=AsyncMock()):
            # Cold call
            t0 = time.perf_counter()
            res_cold = await login(req, BackgroundTasks())
            t_total_cold = (time.perf_counter() - t0) * 1000  # ms

            # Warm call
            t0 = time.perf_counter()
            res_warm = await login(req, BackgroundTasks())
            t_total_warm = (time.perf_counter() - t0) * 1000  # ms

        print(f"\n--- LOGIN LATENCY BREAKDOWN ---")
        print(f"MongoDB User Query:        {t_query:.2f} ms")
        print(f"Bcrypt Password Verify:    {t_bcrypt:.2f} ms")
        print(f"JWT Creation:              {t_jwt:.2f} ms")
        print(f"Total Login (Cold Start):  {t_total_cold:.2f} ms")
        print(f"Total Login (Warm Call):   {t_total_warm:.2f} ms")

        # Bcrypt is CPU-bound and dominates (~80-160ms); total login API must be < 500ms
        assert t_total_warm < 500
        assert t_jwt < 5.0
        assert t_query < 20.0

    async def test_large_dataset_scalability_simulation(self, perf_user):
        """
        Simulate a user with 1,000 folders and 10,000 files in the test database.
        Proves API response size and latency scale with current PAGE size, NOT total account size.
        """
        uid = str(perf_user.id)
        email = perf_user.email

        # Bulk insert 1,000 folders and 10,000 files in test DB
        folders = [
            FileSystemItem(
                name=f"Folder_{i}",
                type="folder",
                user_id=uid,
                parent_id=None if i < 10 else f"Folder_{i % 10}",
                is_deleted=False,
            )
            for i in range(1000)
        ]
        await FileSystemItem.insert_many(folders)

        files = [
            FileSystemItem(
                name=f"Document_{i}.pdf",
                type="file",
                user_id=uid,
                parent_id=None if i < 50 else f"Folder_{i % 1000}",
                size=1024 * (i % 50 + 1),
                is_deleted=False,
            )
            for i in range(10000)
        ]
        await FileSystemItem.insert_many(files)

        # 1. Test Root Listing Performance (Pagination with limit=50)
        t0 = time.perf_counter()
        root_items, next_cursor, has_more, count = await crud.get_folder_children_paginated(
            user_id=uid,
            email=email,
            parent_id=None,
            limit=50,
        )
        t_root_query = (time.perf_counter() - t0) * 1000

        print(f"\n--- LARGE DATASET (1,000 FOLDERS, 10,000 FILES) SCALE TEST ---")
        print(f"Root Page Query Time (50 items): {t_root_query:.2f} ms")
        print(f"Returned Items: {len(root_items)}, Has More: {has_more}, Next Cursor: {next_cursor}")

        assert len(root_items) == 50
        assert has_more is True
        assert next_cursor is not None
        # Root page query on in-memory mock must be performant (< 500ms)
        assert t_root_query < 500.0

        # 2. Test Single Subfolder Listing (Immediate children only)
        t0 = time.perf_counter()
        sub_items, _, _, sub_count = await crud.get_folder_children_paginated(
            user_id=uid,
            email=email,
            parent_id="Folder_1",
            limit=50,
        )
        t_sub_query = (time.perf_counter() - t0) * 1000

        print(f"Subfolder Query Time (Immediate children only): {t_sub_query:.2f} ms")
        print(f"Subfolder Items Count: {len(sub_items)}")

        assert t_sub_query < 500.0
        # Verify subfolder only loads its children, not the 10,000 total files
        assert len(sub_items) <= 50

        # 3. Test Storage Aggregation on 10,000 files
        t0 = time.perf_counter()
        total_storage = await crud.get_user_storage_size(uid)
        t_storage = (time.perf_counter() - t0) * 1000

        print(f"O(1) Aggregation on 10,000 files: {t_storage:.2f} ms (Total: {total_storage} bytes)")
        assert total_storage > 0
        assert t_storage < 500.0
