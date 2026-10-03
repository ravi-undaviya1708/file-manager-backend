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


async def seed_roles() -> None:
    """Seed default system roles if they do not exist in the database."""
    from app.models import Role
    default_roles = [
        {
            "name": "Super Admin",
            "key": "superAdmin",
            "is_default": True,
            "description": "System owner with full administrative and role configuration access.",
            "permissions": ["view_telemetry", "manage_all_users", "manage_roles", "delete_users"]
        },
        {
            "name": "Admin",
            "key": "admin",
            "is_default": True,
            "description": "Organizational admin who can manage team members and assign custom roles.",
            "permissions": ["manage_team_users", "delete_users"]
        },
        {
            "name": "Individual User",
            "key": "individual",
            "is_default": True,
            "description": "Standard individual user managing their own storage and partitions.",
            "permissions": ["manage_self"]
        }
    ]
    for r_data in default_roles:
        existing = await Role.find_one(Role.key == r_data["key"])
        if not existing:
            await Role(**r_data).insert()


async def seed_billing_plans() -> None:
    """Seed default subscription plans into the plans collection if not present."""
    from app.models import Plan

    default_plans = [
        {
            "code": "free",
            "name": "Free Starter",
            "billing_interval": "free",
            "amount_paise": 0,
            "currency": "INR",
            "storage_quota_bytes": 16106127360,  # 15 GB
            "version": 1,
            "is_active": True,
        },
        {
            "code": "personal",
            "name": "Personal Plan",
            "billing_interval": "monthly",
            "amount_paise": 11900,  # ₹119.00
            "currency": "INR",
            "storage_quota_bytes": 53687091200,  # 50 GB
            "version": 1,
            "is_active": True,
        },
        {
            "code": "personal",
            "name": "Personal Plan",
            "billing_interval": "annual",
            "amount_paise": 119000,  # ₹1,190.00
            "currency": "INR",
            "storage_quota_bytes": 53687091200,  # 50 GB
            "version": 1,
            "is_active": True,
        },
        {
            "code": "plus",
            "name": "Plus Plan",
            "billing_interval": "monthly",
            "amount_paise": 29900,  # ₹299.00
            "currency": "INR",
            "storage_quota_bytes": 214748364800,  # 200 GB
            "version": 1,
            "is_active": True,
        },
        {
            "code": "plus",
            "name": "Plus Plan",
            "billing_interval": "annual",
            "amount_paise": 299000,  # ₹2,990.00
            "currency": "INR",
            "storage_quota_bytes": 214748364800,  # 200 GB
            "version": 1,
            "is_active": True,
        },
        {
            "code": "power",
            "name": "Power Plan",
            "billing_interval": "monthly",
            "amount_paise": 99900,  # ₹999.00
            "currency": "INR",
            "storage_quota_bytes": 1099511627776,  # 1 TB
            "version": 1,
            "is_active": True,
        },
        {
            "code": "power",
            "name": "Power Plan",
            "billing_interval": "annual",
            "amount_paise": 999000,  # ₹9,990.00
            "currency": "INR",
            "storage_quota_bytes": 1099511627776,  # 1 TB
            "version": 1,
            "is_active": True,
        },
        {
            "code": "power",
            "name": "Power Lifetime Plan",
            "billing_interval": "lifetime",
            "amount_paise": 1999000,  # ₹19,990.00
            "currency": "INR",
            "storage_quota_bytes": 1099511627776,  # 1 TB
            "version": 1,
            "is_active": True,
        },
    ]

    for p_data in default_plans:
        existing = await Plan.find_one(
            Plan.code == p_data["code"],
            Plan.billing_interval == p_data["billing_interval"],
            Plan.version == p_data["version"],
        )
        if not existing:
            await Plan(**p_data).insert()


async def seed_database() -> None:
    """Insert seed data if the database is empty."""
    # 1. Seed default system roles
    await seed_roles()

    # 2. Seed default billing plans
    await seed_billing_plans()

    # 3. Check for optional initial admin email from environment
    import os
    from app.models import User
    admin_email = os.getenv("INITIAL_ADMIN_EMAIL", "").strip().lower()
    if admin_email:
        super_admin_user = await User.find_one(User.email == admin_email)
        if super_admin_user:
            needs_save = False
            if super_admin_user.user_type != "superAdmin":
                super_admin_user.user_type = "superAdmin"
                needs_save = True
            if not super_admin_user.is_admin:
                super_admin_user.is_admin = True
                needs_save = True
            if needs_save:
                await super_admin_user.save()

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

