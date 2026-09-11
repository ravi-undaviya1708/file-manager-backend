"""Execution API routes for running code files and scripts."""

from __future__ import annotations

from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.models import User, FileSystemItem
from app.code_runner import (
    execute_code,
    get_available_runtimes,
    ExecutionRequest,
    ExecutionResult,
)
from app.b2 import get_user_b2_prefix, get_item_path, get_b2_client
import anyio

router = APIRouter(prefix="/api/execution", tags=["Code Execution"])


class RunFileRequest(BaseModel):
    file_id: Optional[str] = Field(default=None, description="FileSystemItem ID to run")
    language: Optional[str] = Field(default=None, description="Language override")
    code: Optional[str] = Field(default=None, description="Direct code override (if unsaved)")
    stdin: Optional[str] = Field(default="", description="STDIN string input")
    timeout: Optional[float] = Field(default=15.0, description="Max timeout in seconds")


def detect_language_from_filename(filename: str) -> str:
    """Guess language from file extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mapping = {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "jsx": "javascript",
        "sh": "bash",
        "bash": "bash",
        "c": "c",
        "cpp": "cpp",
        "cc": "cpp",
        "h": "c",
        "hpp": "cpp",
        "go": "go",
        "rs": "rust",
        "php": "php",
        "rb": "ruby",
    }
    return mapping.get(ext, "bash")


@router.get("/runtimes", summary="List available installed compilers and interpreters")
async def list_runtimes(current_user: User = Depends(get_current_user)):
    """Returns list of installed execution runtimes (Python, Node, GCC, etc.)."""
    return get_available_runtimes()


@router.post("/run", summary="Run code or script and get output")
async def run_code_endpoint(
    req: RunFileRequest,
    current_user: User = Depends(get_current_user)
) -> ExecutionResult:
    """Execute code snippet or stored file securely."""
    code_to_run = req.code
    filename = "snippet"
    language = req.language

    # If file_id is provided, load file info from DB and B2 if code not directly sent
    if req.file_id:
        item = await FileSystemItem.get(req.file_id)
        if not item or item.type != "file":
            raise HTTPException(status_code=404, detail="File not found")
        
        filename = item.name
        if not language:
            language = detect_language_from_language = detect_language_from_filename(filename)

        if code_to_run is None:
            # Fetch content from B2
            owner_id = item.user_id if item.user_id else str(current_user.id)
            prefix = await get_user_b2_prefix(owner_id)
            path = await get_item_path(item, owner_id)
            key = f"{prefix}/{path}"

            def fetch_b2():
                client = get_b2_client()
                from app.config import get_settings
                settings = get_settings()
                resp = client.get_object(Bucket=settings.B2_BUCKET, Key=key)
                return resp["Body"].read().decode("utf-8", errors="replace")

            try:
                code_to_run = await anyio.to_thread.run_sync(fetch_b2)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to load file content from storage: {str(e)}")
    
    if not language:
        language = "python"  # Default fallback

    exec_req = ExecutionRequest(
        language=language,
        code=code_to_run or "",
        filename=filename,
        stdin=req.stdin or "",
        timeout=req.timeout or 15.0
    )

    result = await execute_code(exec_req)
    return result
