"""Automated Integration Tests for Search, Admin Protection, and WebSockets."""

import pytest
from unittest.mock import patch, AsyncMock
from fastapi import HTTPException, status

from app.models import User, FileSystemItem, Role
from app.auth import hash_password
from app import crud
from app.admin_routes import list_users, admin_required, delete_user
from app.execution_routes import detect_language_from_filename


class TestSearchAdminWebsockets:
    """Tests covering Phase 15, 16, 21, 23 (Search, WebSockets, Admin Security, Code Runner)."""

    @pytest.fixture
    async def standard_user(self):
        user = User(
            name="Normal User",
            email="normal@user.com",
            hashed_password=hash_password("Pass1234!"),
            is_admin=False,
            user_type="individual",
        )
        await user.insert()
        return user

    @pytest.fixture
    async def admin_user(self):
        user = User(
            name="Admin User",
            email="admin@user.com",
            hashed_password=hash_password("Pass1234!"),
            is_admin=True,
            user_type="admin",
        )
        await user.insert()
        return user

    async def test_search_filename_and_regex_safety(self, standard_user):
        """Verify search finds partial filenames, handles regex special characters safely."""
        uid = str(standard_user.id)
        email = standard_user.email

        await crud.create_item("Quarterly_Report_2026.pdf", "file", uid)
        await crud.create_item("Report_Draft.docx", "file", uid)
        await crud.create_item("Special[Char](File).txt", "file", uid)
        await crud.create_item("Invoice_100.pdf", "file", uid)

        # 1. Partial case-insensitive search "report"
        results, _, _, count = await crud.get_folder_children_paginated(uid, email, search="report")
        assert count == 2
        names = {i.name for i in results}
        assert "Quarterly_Report_2026.pdf" in names
        assert "Report_Draft.docx" in names

        # 2. Search with regex special characters: "[Char]("
        results_regex, _, _, count_regex = await crud.get_folder_children_paginated(uid, email, search="[Char](")
        assert count_regex == 1
        assert results_regex[0].name == "Special[Char](File).txt"

    async def test_category_filters(self, standard_user):
        """Verify category filtering for images, documents, audio, videos."""
        uid = str(standard_user.id)
        email = standard_user.email

        await crud.create_item("photo.jpg", "file", uid)
        await crud.create_item("song.mp3", "file", uid)
        await crud.create_item("movie.mp4", "file", uid)
        await crud.create_item("notes.pdf", "file", uid)

        # Images
        images, _, _, _ = await crud.get_folder_children_paginated(uid, email, category="images")
        assert len(images) == 1
        assert images[0].name == "photo.jpg"

        # Documents
        docs, _, _, _ = await crud.get_folder_children_paginated(uid, email, category="documents")
        assert len(docs) == 1
        assert docs[0].name == "notes.pdf"

        # Audio
        audio, _, _, _ = await crud.get_folder_children_paginated(uid, email, category="audio")
        assert len(audio) == 1
        assert audio[0].name == "song.mp3"

        # Videos
        videos, _, _, _ = await crud.get_folder_children_paginated(uid, email, category="videos")
        assert len(videos) == 1
        assert videos[0].name == "movie.mp4"

    async def test_admin_access_control(self, standard_user, admin_user):
        """Verify normal users cannot access admin endpoints and admins can."""
        # Normal user accessing admin dependency raises 403
        with pytest.raises(HTTPException) as exc:
            await admin_required(standard_user)
        assert exc.value.status_code == status.HTTP_403_FORBIDDEN

        # Admin user succeeds
        admin = await admin_required(admin_user)
        assert admin.is_admin is True

        # List users as admin
        user_list = await list_users(admin)
        assert len(user_list) >= 2

    def test_language_detection(self):
        """Verify compiler/runtime language detection."""
        assert detect_language_from_filename("main.py") == "python"
        assert detect_language_from_filename("index.ts") == "typescript"
        assert detect_language_from_filename("server.js") == "javascript"
        assert detect_language_from_filename("script.sh") == "bash"
        assert detect_language_from_filename("program.cpp") == "cpp"
        assert detect_language_from_filename("lib.rs") == "rust"
        assert detect_language_from_filename("main.go") == "go"
