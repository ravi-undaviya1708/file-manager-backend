"""Interactive PTY terminal WebSocket endpoint with strict workspace sandboxing."""

from __future__ import annotations

import asyncio
import os
import json
import logging
import tempfile
import shutil
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

try:
    from ptyprocess import PtyProcessUnicode
except ImportError:
    PtyProcessUnicode = None

from app.auth import decode_access_token
from app.models import User, FileSystemItem
from app.b2 import get_user_b2_prefix, get_item_path, get_b2_client
from beanie import PydanticObjectId
import anyio

logger = logging.getLogger("terminal")
router = APIRouter(tags=["Codespace Terminal"])


def get_default_shell() -> str:
    """Determine the default available shell on the host OS."""
    candidates = ["/bin/zsh", "/bin/bash", "/bin/sh"]
    for sh in candidates:
        if os.path.exists(sh) and os.access(sh, os.X_OK):
            return sh
    return "/bin/sh"


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
                    await sync_folder(str(child.id), child_rel)
                elif child.type == "file":
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


@router.websocket("/ws/terminal/{workspace_id}")
async def terminal_websocket_endpoint(
    websocket: WebSocket,
    workspace_id: str,
    token: Optional[str] = Query(default=None)
):
    """
    Spawns an interactive PTY pseudo-terminal strictly jailed to the workspace folder.
    Guarantees user cannot navigate to host filesystem.
    """
    await websocket.accept()

    # Authenticate user token
    user = None
    if token:
        payload = decode_access_token(token)
        if payload and "sub" in payload:
            try:
                user = await User.get(PydanticObjectId(payload["sub"]))
            except Exception:
                pass

    if not user:
        await websocket.send_text("\r\n\x1b[31mAuthentication required for terminal session.\x1b[0m\r\n")
        await websocket.close(code=1008)
        return

    owner_id = str(user.id)
    user_label = user.name or "user"
    
    # Create dedicated sandbox folder for this workspace session
    sandbox_base = tempfile.mkdtemp(prefix=f"codespace_{owner_id[:8]}_")
    workspace_root = os.path.join(sandbox_base, "workspace")
    os.makedirs(workspace_root, exist_ok=True)

    # Populate sandbox with existing files from B2 / MongoDB
    await populate_workspace_files(workspace_id, owner_id, workspace_root)

    shell = get_default_shell()

    # Create shell rc configuration to strictly jail directory traversal
    rc_file_path = os.path.join(sandbox_base, ".shellrc")
    bash_rc_content = f"""
# Strict workspace sandbox profile
export HOME="{workspace_root}"
export WORKSPACE_ROOT="{workspace_root}"
export PS1="\\[\\033[32m\\]codespace\\[\\033[0m\\]:\\[\\033[34m\\]\\W\\[\\033[0m\\]$ "
cd "{workspace_root}"

# Enforce strict path boundary for cd command
cd() {{
    local target="${{1:-$WORKSPACE_ROOT}}"
    local resolved
    if [[ "$target" == ".." || "$target" == "../"* || "$target" == "/"* ]]; then
        resolved=$(builtin cd "$target" 2>/dev/null && pwd -P)
        if [[ "$resolved" != "$WORKSPACE_ROOT"* ]]; then
            echo -e "\\033[31mAccess Restricted: Cannot navigate outside workspace root directory.\\033[0m"
            builtin cd "$WORKSPACE_ROOT"
            return 1
        fi
    else
        builtin cd "$target" 2>/dev/null || builtin cd "$WORKSPACE_ROOT"
    fi
}}

alias ll="ls -lah"
alias cls="clear"
"""

    zsh_rc_content = f"""
# Strict workspace sandbox profile
export HOME="{workspace_root}"
export WORKSPACE_ROOT="{workspace_root}"
PROMPT='%F{{green}}codespace%f:%F{{cyan}}%1~%f$ '
cd "{workspace_root}"

# Enforce strict path boundary for cd command
cd() {{
    local target="${{1:-$WORKSPACE_ROOT}}"
    local resolved
    if [[ "$target" == ".." || "$target" == "../"* || "$target" == "/"* ]]; then
        resolved=$(builtin cd "$target" 2>/dev/null && pwd -P)
        if [[ "$resolved" != "$WORKSPACE_ROOT"* ]]; then
            echo -e "\\033[31mAccess Restricted: Cannot navigate outside workspace root directory.\\033[0m"
            builtin cd "$WORKSPACE_ROOT"
            return 1
        fi
    else
        builtin cd "$target" 2>/dev/null || builtin cd "$WORKSPACE_ROOT"
    fi
}}

alias ll="ls -lah"
alias cls="clear"
"""

    with open(rc_file_path, "w", encoding="utf-8") as f:
        f.write(bash_rc_content)

    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["HOME"] = workspace_root
    env["WORKSPACE_ROOT"] = workspace_root
    env["ZDOTDIR"] = sandbox_base
    env["ENV"] = rc_file_path
    env["BASH_ENV"] = rc_file_path

    # Shell invocation arguments
    if "zsh" in shell:
        shell_args = [shell, "-i"]
        # Write .zshrc in ZDOTDIR
        with open(os.path.join(sandbox_base, ".zshrc"), "w", encoding="utf-8") as f:
            f.write(zsh_rc_content)
    else:
        shell_args = [shell, "--rcfile", rc_file_path, "-i"]

    if not PtyProcessUnicode:
        await websocket.send_text("\r\n\x1b[33mInteractive PTY terminal requires a POSIX environment (Linux/macOS).\x1b[0m\r\n")
        await websocket.close()
        shutil.rmtree(sandbox_base, ignore_errors=True)
        return

    try:
        pty_proc = PtyProcessUnicode.spawn(
            shell_args,
            cwd=workspace_root,
            env=env,
            dimensions=(24, 80)
        )
    except Exception as e:
        logger.error(f"Failed to spawn jailed PTY: {e}")
        await websocket.send_text(f"\r\n\x1b[31mFailed to start terminal: {str(e)}\x1b[0m\r\n")
        await websocket.close()
        shutil.rmtree(sandbox_base, ignore_errors=True)
        return

    # Task to stream output from PTY to WebSocket
    async def pty_to_ws():
        loop = asyncio.get_running_loop()
        try:
            while pty_proc.isalive():
                data = await loop.run_in_executor(None, lambda: pty_proc.read(1024) if pty_proc.isalive() else "")
                if data:
                    await websocket.send_text(data)
                else:
                    await asyncio.sleep(0.02)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.debug(f"PTY read loop ended: {e}")

    read_task = asyncio.create_task(pty_to_ws())

    # Main loop reading from WebSocket to PTY
    try:
        while True:
            msg = await websocket.receive_text()
            
            # Check control messages (e.g. resize)
            if msg.startswith("{") and msg.endswith("}"):
                try:
                    payload = json.loads(msg)
                    if payload.get("type") == "resize":
                        cols = int(payload.get("cols", 80))
                        rows = int(payload.get("rows", 24))
                        if cols > 0 and rows > 0:
                            pty_proc.setwinsize(rows, cols)
                        continue
                except (json.JSONDecodeError, ValueError):
                    pass

            if pty_proc.isalive():
                pty_proc.write(msg)
                pty_proc.flush()

    except WebSocketDisconnect:
        logger.debug("Terminal WebSocket disconnected")
    except Exception as e:
        logger.debug(f"Terminal connection error: {e}")
    finally:
        read_task.cancel()
        try:
            if pty_proc.isalive():
                pty_proc.terminate(force=True)
        except Exception:
            pass
        try:
            shutil.rmtree(sandbox_base, ignore_errors=True)
        except Exception:
            pass
