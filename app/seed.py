"""Seed the database with sample file/folder data matching the frontend mock."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models import FileSystemItem


SEED_DATA = [
    # Root folders
    {
        "name": "Documents",
        "type": "folder",
        "parent_id": None,
        "created_at": datetime(2026, 1, 10, 10, 0, 0, tzinfo=timezone.utc),
        "starred": True,
    },
    {
        "name": "Images",
        "type": "folder",
        "parent_id": None,
        "created_at": datetime(2026, 2, 15, 12, 30, 0, tzinfo=timezone.utc),
        "starred": False,
    },
    {
        "name": "Work",
        "type": "folder",
        "parent_id": None,
        "created_at": datetime(2026, 3, 1, 8, 0, 0, tzinfo=timezone.utc),
        "starred": False,
    },
    {
        "name": "Personal",
        "type": "folder",
        "parent_id": None,
        "created_at": datetime(2026, 4, 12, 14, 45, 0, tzinfo=timezone.utc),
        "starred": False,
    },
]

# Items that need parent references (inserted after root folders)
CHILD_ITEMS = [
    # Under Documents
    {
        "name": "Project Proposal.pdf",
        "type": "file",
        "parent_name": "Documents",
        "created_at": datetime(2026, 5, 1, 11, 0, 0, tzinfo=timezone.utc),
        "size": 2450000,
        "starred": True,
    },
    {
        "name": "Budget.xlsx",
        "type": "file",
        "parent_name": "Documents",
        "created_at": datetime(2026, 5, 15, 9, 30, 0, tzinfo=timezone.utc),
        "size": 1240000,
        "starred": False,
    },
    # Under Images
    {
        "name": "Banner.png",
        "type": "file",
        "parent_name": "Images",
        "created_at": datetime(2026, 5, 20, 16, 15, 0, tzinfo=timezone.utc),
        "size": 4800000,
        "starred": False,
    },
    {
        "name": "Profile.jpg",
        "type": "file",
        "parent_name": "Images",
        "created_at": datetime(2026, 5, 22, 10, 5, 0, tzinfo=timezone.utc),
        "size": 850000,
        "starred": True,
    },
    # Under Work
    {
        "name": "Design Specs",
        "type": "folder",
        "parent_name": "Work",
        "created_at": datetime(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc),
        "starred": True,
    },
    {
        "name": "Codebase Structure.md",
        "type": "file",
        "parent_name": "Work",
        "created_at": datetime(2026, 5, 26, 15, 20, 0, tzinfo=timezone.utc),
        "size": 12000,
        "starred": False,
    },
]

# Grandchild items
GRANDCHILD_ITEMS = [
    # Under Work / Design Specs
    {
        "name": "Wireframe.sketch",
        "type": "file",
        "parent_name": "Design Specs",
        "created_at": datetime(2026, 5, 25, 16, 0, 0, tzinfo=timezone.utc),
        "size": 15400000,
        "starred": False,
    },
]


async def seed_database() -> None:
    """Insert seed data if the database is empty."""
    count = await FileSystemItem.count()
    if count > 0:
        return  # Database already has data

    # Insert root folders
    root_map = {}
    for data in SEED_DATA:
        item = FileSystemItem(**data)
        await item.insert()
        root_map[item.name] = str(item.id)

    # Insert child items (lookup parent ID by name)
    child_map = {}
    for data in CHILD_ITEMS:
        parent_name = data.pop("parent_name")
        parent_id = root_map.get(parent_name)
        item = FileSystemItem(parent_id=parent_id, **data)
        await item.insert()
        child_map[item.name] = str(item.id)

    # Insert grandchild items
    for data in GRANDCHILD_ITEMS:
        parent_name = data.pop("parent_name")
        parent_id = child_map.get(parent_name) or root_map.get(parent_name)
        item = FileSystemItem(parent_id=parent_id, **data)
        await item.insert()

    total = len(SEED_DATA) + len(CHILD_ITEMS) + len(GRANDCHILD_ITEMS)
    print(f"✓ Seeded MongoDB with {total} items.")


async def seed_user_data(user_id: str) -> None:
    """Insert default files/folders for a newly registered user."""
    # Prevent duplicate seeding
    count = await FileSystemItem.find({"user_id": user_id}).count()
    if count > 0:
        return

    # Insert root folders
    root_map = {}
    for data in SEED_DATA:
        item_data = data.copy()
        item_data["user_id"] = user_id
        item = FileSystemItem(**item_data)
        await item.insert()
        root_map[item.name] = str(item.id)

    # Insert child items (lookup parent ID by name)
    child_map = {}
    for data in CHILD_ITEMS:
        item_data = data.copy()
        parent_name = item_data.pop("parent_name")
        parent_id = root_map.get(parent_name)
        item_data["parent_id"] = parent_id
        item_data["user_id"] = user_id
        item = FileSystemItem(**item_data)
        await item.insert()
        child_map[item.name] = str(item.id)

    # Insert grandchild items
    for data in GRANDCHILD_ITEMS:
        item_data = data.copy()
        parent_name = item_data.pop("parent_name")
        parent_id = child_map.get(parent_name) or root_map.get(parent_name)
        item_data["parent_id"] = parent_id
        item_data["user_id"] = user_id
        item = FileSystemItem(**item_data)
        await item.insert()

