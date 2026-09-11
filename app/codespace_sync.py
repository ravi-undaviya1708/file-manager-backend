"""Codespace Sandbox directory management and fast two-way Disk <-> MongoDB/B2 synchronization."""

from __future__ import annotations

import os
import tempfile
import asyncio
import logging
from typing import Optional, Dict, List, Any
import anyio

from app.models import FileSystemItem
from app.b2 import get_user_b2_prefix, get_item_path, get_b2_client, upload_b2_file_async

logger = logging.getLogger("codespace_sync")

# Folders to strictly NOT scan or ingest recursively (prevents 50k dependency files from freezing DB)
SHALLOW_FOLDERS = {
    "node_modules",
    ".git",
    ".next",
    ".cache",
    "dist",
    "build",
    ".turbo",
    ".venv",
    "venv",
    "__pycache__",
    ".output",
    "coverage",
    ".vscode",
    ".idea",
    ".pytest_cache",
}

IGNORED_FILES = {
    ".DS_Store",
    ".shellrc",
    ".zshrc",
    ".bashrc",
    ".bash_history",
    ".zsh_history",
    "thumbs.db",
}


def get_workspace_sandbox_base(workspace_id: str, owner_id: str) -> str:
    """Return persistent sandbox base path for a given workspace and user."""
    safe_owner = str(owner_id)[:12]
    safe_ws = str(workspace_id)
    base_dir = os.path.join(tempfile.gettempdir(), "codespaces", f"{safe_owner}_{safe_ws}")
    os.makedirs(base_dir, exist_ok=True)
    return base_dir


def get_workspace_sandbox_dir(workspace_id: str, owner_id: str) -> str:
    """Return the workspace project root inside the sandbox base."""
    base = get_workspace_sandbox_base(workspace_id, owner_id)
    ws_dir = os.path.join(base, "workspace")
    os.makedirs(ws_dir, exist_ok=True)
    return ws_dir


async def _background_b2_upload(key: str, content_bytes: bytes):
    """Upload file to Backblaze B2 in the background without blocking the UI response."""
    try:
        await upload_b2_file_async(key, content_bytes)
    except Exception as e:
        logger.warning(f"Background B2 upload failed for {key}: {e}")


async def populate_workspace_files(workspace_id: str, owner_id: str, target_dir: str):
    """Download and sync workspace project files from B2/DB into sandbox directory."""
    try:
        root_folder = await FileSystemItem.get(workspace_id)
        if not root_folder:
            return

        all_items = await FileSystemItem.find(
            FileSystemItem.user_id == owner_id,
            FileSystemItem.is_deleted == False
        ).to_list()

        items_by_parent: Dict[Optional[str], List[FileSystemItem]] = {}
        for item in all_items:
            items_by_parent.setdefault(item.parent_id, []).append(item)

        prefix = await get_user_b2_prefix(owner_id)

        async def sync_folder(parent_id: str, rel_path: str):
            children = items_by_parent.get(parent_id, [])
            for child in children:
                child_rel = os.path.join(rel_path, child.name)
                child_full = os.path.join(target_dir, child_rel)

                if child.type == "folder":
                    os.makedirs(child_full, exist_ok=True)
                    if child.name not in SHALLOW_FOLDERS:
                        await sync_folder(str(child.id), child_rel)
                elif child.type == "file":
                    # If file already exists locally, don't re-download
                    if os.path.exists(child_full):
                        continue

                    os.makedirs(os.path.dirname(child_full), exist_ok=True)
                    item_b2_path = await get_item_path(child, owner_id)
                    key = f"{prefix}/{item_b2_path}"

                    def download():
                        try:
                            client = get_b2_client()
                            from app.config import get_settings
                            settings = get_settings()
                            resp = client.get_object(Bucket=settings.B2_BUCKET, Key=key)
                            return resp["Body"].read()
                        except Exception:
                            return b""

                    try:
                        content_bytes = await anyio.to_thread.run_sync(download)
                        with open(child_full, "wb") as f:
                            f.write(content_bytes)
                    except Exception:
                        pass

        await sync_folder(workspace_id, "")
    except Exception as e:
        logger.error(f"Error populating workspace files: {e}")


_active_syncs: set[str] = set()


def schedule_sync_disk_to_workspace_db(workspace_id: str, owner_id: str):
    """Schedule asynchronous disk-to-DB sync if one is not already running."""
    if workspace_id in _active_syncs:
        return
    asyncio.create_task(sync_disk_to_workspace_db(workspace_id, owner_id))


async def sync_disk_to_workspace_db(workspace_id: str, owner_id: str):
    """
    Fast disk-to-MongoDB synchronization.
    Scans the local sandbox disk, inserts newly created files/folders into MongoDB,
    and schedules background B2 uploads without blocking the request.
    """
    if workspace_id in _active_syncs:
        return
    _active_syncs.add(workspace_id)

    try:
        workspace_root = get_workspace_sandbox_dir(workspace_id, owner_id)
        if not os.path.exists(workspace_root):
            return

        root_folder = await FileSystemItem.get(workspace_id)
        if not root_folder:
            return

        partition_id = root_folder.partition_id
        prefix = await get_user_b2_prefix(owner_id)

        # Get existing items in DB to avoid duplicates
        all_items = await FileSystemItem.find(
            FileSystemItem.user_id == owner_id,
            FileSystemItem.is_deleted == False
        ).to_list()

        # Map by (parent_id, name)
        existing_map: Dict[tuple[Optional[str], str], FileSystemItem] = {}
        for it in all_items:
            existing_map[(it.parent_id, it.name)] = it

        # Scan filesystem in a thread to keep async loop unblocked
        def scan_local_tree():
            entries_to_process = []
            for root_dir, dirs, files in os.walk(workspace_root):
                # Prune shallow folders from deep recursion
                dirs[:] = [d for d in dirs if d not in SHALLOW_FOLDERS and not d.startswith(".")]

                rel_dir = os.path.relpath(root_dir, workspace_root)
                for d in dirs:
                    entries_to_process.append((os.path.join(rel_dir, d), True))

                # Also record shallow folder names at root/subfolder level without entering them
                raw_dirs = []
                try:
                    raw_dirs = os.listdir(root_dir)
                except Exception:
                    pass
                for raw_d in raw_dirs:
                    if raw_d in SHALLOW_FOLDERS and os.path.isdir(os.path.join(root_dir, raw_d)):
                        entries_to_process.append((os.path.join(rel_dir, raw_d), True))

                for f in files:
                    if f not in IGNORED_FILES and not f.endswith(".tmp"):
                        entries_to_process.append((os.path.join(rel_dir, f), False))
            return entries_to_process

        local_entries = await anyio.to_thread.run_sync(scan_local_tree)

        # Process directories first, then files
        dir_entries = [p for p, is_d in local_entries if is_d]
        file_entries = [p for p, is_d in local_entries if not is_d]

        # Helper to resolve or create parent hierarchy
        async def ensure_parent_id(rel_path: str) -> str:
            clean_p = rel_path.lstrip("./").strip("/")
            if not clean_p or clean_p == ".":
                return str(workspace_id)

            parts = clean_p.split("/")
            curr_parent_id = str(workspace_id)

            for part in parts:
                existing = existing_map.get((curr_parent_id, part))
                if existing:
                    curr_parent_id = str(existing.id)
                else:
                    new_folder = FileSystemItem(
                        name=part,
                        type="folder",
                        user_id=owner_id,
                        parent_id=curr_parent_id,
                        partition_id=partition_id,
                    )
                    await new_folder.insert()
                    existing_map[(curr_parent_id, part)] = new_folder
                    curr_parent_id = str(new_folder.id)

            return curr_parent_id

        # 1. Register folders
        for rel_path in sorted(dir_entries, key=lambda x: len(x.split("/"))):
            clean_p = rel_path.lstrip("./").strip("/")
            if not clean_p or clean_p == ".":
                continue
            await ensure_parent_id(clean_p)

        # 2. Register files (capped to prevent overload)
        for rel_file in file_entries[:300]:
            clean_p = rel_file.lstrip("./").strip("/")
            if not clean_p:
                continue

            parent_rel = os.path.dirname(clean_p)
            file_name = os.path.basename(clean_p)
            parent_id = await ensure_parent_id(parent_rel)

            if (parent_id, file_name) not in existing_map:
                full_file_path = os.path.join(workspace_root, clean_p)
                try:
                    f_size = os.path.getsize(full_file_path)
                except Exception:
                    f_size = 0

                file_item = FileSystemItem(
                    name=file_name,
                    type="file",
                    user_id=owner_id,
                    parent_id=parent_id,
                    partition_id=partition_id,
                    size=f_size,
                )
                await file_item.insert()
                existing_map[(parent_id, file_name)] = file_item

                # Read bytes and schedule background B2 upload without blocking
                if f_size < 10485760:  # Under 10MB
                    try:
                        with open(full_file_path, "rb") as f:
                            content_bytes = f.read()
                        key = f"{prefix}/{clean_p}"
                        asyncio.create_task(_background_b2_upload(key, content_bytes))
                    except Exception:
                        pass

    except Exception as e:
        logger.error(f"Error syncing disk to workspace DB: {e}")
    finally:
        _active_syncs.discard(workspace_id)

